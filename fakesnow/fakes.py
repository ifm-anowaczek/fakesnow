from __future__ import annotations

import re
from string import Template
from types import TracebackType
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Literal, Optional, Sequence, Type, Union, cast

import duckdb

if TYPE_CHECKING:
    import pandas as pd
    import pyarrow.lib
import pyarrow
import snowflake.connector.errors
import sqlglot
from duckdb import DuckDBPyConnection
from snowflake.connector.cursor import DictCursor, ResultMetadata, SnowflakeCursor
from snowflake.connector.result_batch import ResultBatch
from sqlglot import exp, parse_one
from typing_extensions import Self

import fakesnow.checks as checks
import fakesnow.expr as expr
import fakesnow.transforms as transforms

SCHEMA_UNSET = "schema_unset"

# use ext prefix in columns to disambiguate when joining with information_schema.tables
SQL_CREATE_INFORMATION_SCHEMA_TABLES_EXT = Template(
    """
create table ${catalog}.information_schema.tables_ext (
    ext_table_catalog varchar,
    ext_table_schema varchar,
    ext_table_name varchar,
    comment varchar,
    PRIMARY KEY(ext_table_catalog, ext_table_schema, ext_table_name)
)
"""
)
SQL_CREATE_INFORMATION_SCHEMA_COLUMNS_EXT = Template(
    """
create table ${catalog}.information_schema.columns_ext (
    ext_table_catalog varchar,
    ext_table_schema varchar,
    ext_table_name varchar,
    ext_column_name varchar,
    ext_character_maximum_length integer,
    ext_character_octet_length integer,
    PRIMARY KEY(ext_table_catalog, ext_table_schema, ext_table_name, ext_column_name)
)
"""
)

# only include fields applicable to snowflake (as mentioned by describe table information_schema.columns)
# snowflake integers are 38 digits, base 10, See https://docs.snowflake.com/en/sql-reference/data-types-numeric
SQL_CREATE_INFORMATION_SCHEMA_COLUMNS_VIEW = Template(
    """
create view ${catalog}.information_schema.columns_snowflake AS
select table_catalog, table_schema, table_name, column_name, ordinal_position, column_default, is_nullable, data_type,
ext_character_maximum_length as character_maximum_length, ext_character_octet_length as character_octet_length,
case when data_type='BIGINT' then 38 else numeric_precision end as numeric_precision,
case when data_type='BIGINT' then 10 else numeric_precision_radix end as numeric_precision_radix,
numeric_scale,
collation_name, is_identity, identity_generation, identity_cycle
from ${catalog}.information_schema.columns
left join ${catalog}.information_schema.columns_ext ext
on ext_table_catalog = table_catalog AND ext_table_schema = table_schema
AND ext_table_name = table_name AND ext_column_name = column_name
"""
)


class FakeSnowflakeCursor:
    def __init__(
        self,
        conn: FakeSnowflakeConnection,
        duck_conn: DuckDBPyConnection,
        use_dict_result: bool = False,
    ) -> None:
        """Create a fake snowflake cursor backed by DuckDB.

        Args:
            conn (FakeSnowflakeConnection): Used to maintain current database and schema.
            duck_conn (DuckDBPyConnection): DuckDB connection.
            use_dict_result (bool, optional): If true rows are returned as dicts otherwise they
                are returned as tuples. Defaults to False.
        """
        self._conn = conn
        self._duck_conn = duck_conn
        self._use_dict_result = use_dict_result
        self._last_sql = None
        self._last_params = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]] = ...,
        exc_value: Optional[BaseException] = ...,
        traceback: Optional[TracebackType] = ...,
    ) -> bool:
        return False

    def describe(self, command: str, *args: Any, **kwargs: Any) -> list[ResultMetadata]:
        """Return the schema of the result without executing the query.

        Takes the same arguments as execute

        Returns:
            list[ResultMetadata]: _description_
        """

        describe = transforms.as_describe(parse_one(command, read="snowflake"))
        self.execute(describe, *args, **kwargs)
        return FakeSnowflakeCursor._describe_as_result_metadata(self._duck_conn.fetchall())  # noqa: SLF001

    @property
    def description(self) -> list[ResultMetadata]:
        # use a cursor to avoid destroying an unfetched result on the main connection
        with self._duck_conn.cursor() as cur:
            assert self._conn.database, "Not implemented when database is None"
            assert self._conn.schema, "Not implemented when database is None"

            # match database and schema used on the main connection
            cur.execute(f"SET SCHEMA = '{self._conn.database}.{self._conn.schema}'")
            cur.execute(f"DESCRIBE {self._last_sql}", self._last_params)
            meta = FakeSnowflakeCursor._describe_as_result_metadata(cur.fetchall())  # noqa: SLF001

        return meta  # type: ignore see https://github.com/duckdb/duckdb/issues/7816

    def execute(
        self,
        command: str | exp.Expression,
        params: Sequence[Any] | dict[Any, Any] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> FakeSnowflakeCursor:
        self._arrow_table = None

        if isinstance(command, exp.Expression):
            expression = command
        else:
            expression = parse_one(self._rewrite_params(command, params), read="snowflake")

        cmd = expr.key_command(expression)

        no_database, no_schema = checks.is_unqualified_table_expression(expression)

        if no_database and not self._conn.database_set:
            raise snowflake.connector.errors.ProgrammingError(
                msg=f"Cannot perform {cmd}. This session does not have a current database. Call 'USE DATABASE', or use a qualified name.",  # noqa: E501
                errno=90105,
                sqlstate="22000",
            ) from None
        elif no_schema and not self._conn.schema_set:
            raise snowflake.connector.errors.ProgrammingError(
                msg=f"Cannot perform {cmd}. This session does not have a current schema. Call 'USE SCHEMA', or use a qualified name.",  # noqa: E501
                errno=90106,
                sqlstate="22000",
            ) from None

        transformed = (
            expression.transform(transforms.upper_case_unquoted_identifiers)
            .transform(transforms.set_schema, current_database=self._conn.database)
            .transform(transforms.create_database)
            .transform(transforms.extract_comment)
            .transform(transforms.information_schema_columns_snowflake)
            .transform(transforms.information_schema_tables_ext)
            .transform(transforms.drop_schema_cascade)
            .transform(transforms.tag)
            .transform(transforms.regex)
            .transform(transforms.semi_structured_types)
            .transform(transforms.parse_json)
            .transform(transforms.indices_to_array)
            .transform(transforms.indices_to_object)
            .transform(transforms.values_columns)
            .transform(transforms.to_date)
            .transform(transforms.object_construct)
            .transform(transforms.timestamp_ntz_ns)
            .transform(transforms.float_to_double)
            .transform(transforms.integer_precision)
            .transform(transforms.extract_text_length)
        )
        sql = transformed.sql(dialect="duckdb")

        try:
            self._last_sql = sql
            self._last_params = params
            self._duck_conn.execute(sql, params)
        except duckdb.BinderException as e:
            msg = e.args[0]
            raise snowflake.connector.errors.ProgrammingError(msg=msg, errno=2043, sqlstate="02000") from None
        except duckdb.CatalogException as e:
            # minimal processing to make it look like a snowflake exception, message content may differ
            msg = cast(str, e.args[0]).split("\n")[0]
            raise snowflake.connector.errors.ProgrammingError(msg=msg, errno=2003, sqlstate="42S02") from None

        if cmd == "USE DATABASE" and (ident := expression.find(exp.Identifier)) and isinstance(ident.this, str):
            self._conn.database = ident.this.upper()
            self._conn.database_set = True

        if cmd == "USE SCHEMA" and (ident := expression.find(exp.Identifier)) and isinstance(ident.this, str):
            self._conn.schema = ident.this.upper()
            self._conn.schema_set = True

        if create_db_name := transformed.args.get("create_db_name"):
            # we created a new database, so create the info schema extensions
            self._conn._create_info_schema_ext(create_db_name)  # noqa: SLF001

        if table_comment := transformed.args.get("table_comment"):
            # record table comment
            table, comment = table_comment
            catalog = table.catalog or self._conn.database
            schema = table.db or self._conn.schema
            self._duck_conn.execute(
                f"""
                INSERT INTO {catalog}.information_schema.tables_ext
                values ('{catalog}', '{schema}', '{table.name}', '{comment}')
                ON CONFLICT (ext_table_catalog, ext_table_schema, ext_table_name)
                DO UPDATE SET comment = excluded.comment
                """
            )

        if (text_lengths := transformed.args.get("text_lengths")) and (table := transformed.find(exp.Table)):
            # record text lengths
            catalog = table.catalog or self._conn.database
            schema = table.db or self._conn.schema
            values = ", ".join(
                f"('{catalog}', '{schema}', '{table.name}', '{col_name}', {size}, {min(size*4,16777216)})"
                for (col_name, size) in text_lengths
            )

            self._duck_conn.execute(
                f"""
                INSERT INTO {catalog}.information_schema.columns_ext
                values {values}
                ON CONFLICT (ext_table_catalog, ext_table_schema, ext_table_name, ext_column_name)
                DO UPDATE SET ext_character_maximum_length = excluded.ext_character_maximum_length,
                                ext_character_octet_length = excluded.ext_character_octet_length
                """
            )

        return self

    def executemany(
        self,
        command: str,
        seqparams: Sequence[Any] | dict[str, Any],
        **kwargs: Any,
    ) -> FakeSnowflakeCursor:
        if isinstance(seqparams, dict):
            # see https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-api
            raise NotImplementedError("dict params not supported yet")

        # TODO: support insert optimisations
        # the snowflake connector will optimise inserts into a single query
        # unless num_statements != 1 .. but for simplicity we execute each
        # query one by one, which means the response differs
        for p in seqparams:
            self.execute(command, p)

        return self

    def fetchall(self) -> list[tuple] | list[dict]:
        if self._use_dict_result:
            return self._duck_conn.fetch_arrow_table().to_pylist()
        else:
            return self._duck_conn.fetchall()

    def fetch_pandas_all(self, **kwargs: dict[str, Any]) -> pd.DataFrame:
        return self._duck_conn.fetch_df()

    def fetchone(self) -> dict | tuple | None:
        if not self._use_dict_result:
            return cast(Union[tuple, None], self._duck_conn.fetchone())

        if not self._arrow_table:
            self._arrow_table = self._duck_conn.fetch_arrow_table()
            self._arrow_table_fetch_one_index = -1

        self._arrow_table_fetch_one_index += 1

        try:
            return self._arrow_table.take([self._arrow_table_fetch_one_index]).to_pylist()[0]
        except pyarrow.lib.ArrowIndexError:
            return None

    def get_result_batches(self) -> list[ResultBatch] | None:
        # rows_per_batch is approximate
        # see https://github.com/duckdb/duckdb/issues/4755
        reader = self._duck_conn.fetch_record_batch(rows_per_batch=1000)

        batches = []
        while True:
            try:
                batches.append(FakeResultBatch(self._use_dict_result, reader.read_next_batch()))
            except StopIteration:
                break

        return batches

    @staticmethod
    def _describe_as_result_metadata(describe_results: list) -> list[ResultMetadata]:
        # fmt: off
        def as_result_metadata(column_name: str, column_type: str, _: str) -> ResultMetadata:
            # see https://docs.snowflake.com/en/user-guide/python-connector-api.html#type-codes
            # and https://arrow.apache.org/docs/python/api/datatypes.html#type-checking
            # type ignore because of https://github.com/snowflakedb/snowflake-connector-python/issues/1423
            if column_type == "BIGINT":
                return ResultMetadata(
                    name=column_name, type_code=0, display_size=None, internal_size=None, precision=38, scale=0, is_nullable=True                    # type: ignore # noqa: E501
                )
            elif column_type.startswith("DECIMAL"):
                match = re.search(r'\((\d+),(\d+)\)', column_type)
                if match:
                    precision = int(match[1])
                    scale = int(match[2])
                else:
                    precision = scale = None
                return ResultMetadata(
                    name=column_name, type_code=0, display_size=None, internal_size=None, precision=precision, scale=scale, is_nullable=True # type: ignore # noqa: E501
                )
            elif column_type == "VARCHAR":
                # TODO: fetch internal_size from varchar size
                return ResultMetadata(
                    name=column_name, type_code=2, display_size=None, internal_size=16777216, precision=None, scale=None, is_nullable=True   # type: ignore # noqa: E501
                )
            elif column_type == "DOUBLE":
                return ResultMetadata(
                    name=column_name, type_code=1, display_size=None, internal_size=None, precision=None, scale=None, is_nullable=True       # type: ignore # noqa: E501
                )
            elif column_type == "BOOLEAN":
                return ResultMetadata(
                    name=column_name, type_code=13, display_size=None, internal_size=None, precision=None, scale=None, is_nullable=True      # type: ignore # noqa: E501
                )
            elif column_type == "DATE":
                return ResultMetadata(
                    name=column_name, type_code=3, display_size=None, internal_size=None, precision=None, scale=None, is_nullable=True       # type: ignore # noqa: E501
                )
            elif column_type in {"TIMESTAMP", "TIMESTAMP_NS"}:
                return ResultMetadata(
                    name=column_name, type_code=8, display_size=None, internal_size=None, precision=0, scale=9, is_nullable=True             # type: ignore # noqa: E501
                )
            else:
                # TODO handle more types
                raise NotImplementedError(f"for column type {column_type}")

        # fmt: on

        meta = [
            as_result_metadata(column_name, column_type, null)
            for (column_name, column_type, null, _, _, _) in describe_results
        ]
        return meta

    def _rewrite_params(
        self,
        command: str,
        params: Sequence[Any] | dict[Any, Any] | None = None,
    ) -> str:
        if isinstance(params, dict):
            # see https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-api
            raise NotImplementedError("dict params not supported yet")

        if params and self._conn._paramstyle in ("pyformat", "format"):  # noqa: SLF001
            # duckdb uses question mark style params
            return command.replace("%s", "?")

        return command


class FakeSnowflakeConnection:
    def __init__(
        self,
        duck_conn: DuckDBPyConnection,
        database: Optional[str] = None,
        schema: Optional[str] = None,
        create_database: bool = True,
        create_schema: bool = True,
        *args: Any,
        **kwargs: Any,
    ):
        self._duck_conn = duck_conn
        # upper case database and schema like snowflake
        self.database = database and database.upper()
        self.schema = schema and schema.upper()
        self.database_set = False
        self.schema_set = False
        self._paramstyle = "pyformat"

        # create database if needed
        if (
            create_database
            and self.database
            and not duck_conn.execute(
                f"""select * from information_schema.schemata
                where catalog_name = '{self.database}'"""
            ).fetchone()
        ):
            duck_conn.execute(f"ATTACH DATABASE ':memory:' AS {self.database}")
            self._create_info_schema_ext(self.database)

        # create schema if needed
        if (
            create_schema
            and self.database
            and self.schema
            and not duck_conn.execute(
                f"""select * from information_schema.schemata
                where catalog_name = '{self.database}' and schema_name = '{self.schema}'"""
            ).fetchone()
        ):
            duck_conn.execute(f"CREATE SCHEMA {self.database}.{self.schema}")

        # set database and schema if both exist
        if (
            self.database
            and self.schema
            and duck_conn.execute(
                f"""select * from information_schema.schemata
                where catalog_name = '{self.database}' and schema_name = '{self.schema}'"""
            ).fetchone()
        ):
            duck_conn.execute(f"SET schema='{self.database}.{self.schema}'")
            self.database_set = True
            self.schema_set = True
        # set database if only that exists
        elif (
            self.database
            and duck_conn.execute(
                f"""select * from information_schema.schemata
                where catalog_name = '{self.database}'"""
            ).fetchone()
        ):
            duck_conn.execute(f"SET schema='{self.database}.main'")
            self.database_set = True

        # use UTC instead of local time zone for consistent testing
        duck_conn.execute("SET TimeZone = 'UTC'")

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]] = ...,
        exc_value: Optional[BaseException] = ...,
        traceback: Optional[TracebackType] = ...,
    ) -> bool:
        return False

    def commit(self) -> None:
        self.cursor().execute("COMMIT")

    def cursor(self, cursor_class: Type[SnowflakeCursor] = SnowflakeCursor) -> FakeSnowflakeCursor:
        return FakeSnowflakeCursor(conn=self, duck_conn=self._duck_conn, use_dict_result=cursor_class == DictCursor)

    def execute_string(
        self,
        sql_text: str,
        remove_comments: bool = False,
        return_cursors: bool = True,
        cursor_class: Type[SnowflakeCursor] = SnowflakeCursor,
        **kwargs: dict[str, Any],
    ) -> Iterable[FakeSnowflakeCursor]:
        cursors = [
            self.cursor(cursor_class).execute(e.sql(dialect="snowflake"))
            for e in sqlglot.parse(sql_text, read="snowflake")
            if e
        ]
        return cursors if return_cursors else []

    def rollback(self) -> None:
        self.cursor().execute("ROLLBACK")

    def _insert_df(
        self, df: pd.DataFrame, table_name: str, database: str | None = None, schema: str | None = None
    ) -> int:
        # dicts in dataframes are written as parquet structs, and snowflake loads parquet structs as json strings
        # whereas duckdb loads them as a struct, so we convert them to json here
        cols = [f"TO_JSON({c})" if isinstance(df[c][0], dict) else c for c in df.columns]
        cols = ",".join(cols)

        self._duck_conn.execute(f"INSERT INTO {table_name}({','.join(df.columns.to_list())}) SELECT {cols} FROM df")
        return self._duck_conn.fetchall()[0][0]

    def _create_info_schema_ext(self, catalog: str) -> None:
        """Create the info schema extension tables/views used for storing snowflake metadata not captured by duckdb."""
        self._duck_conn.execute(SQL_CREATE_INFORMATION_SCHEMA_TABLES_EXT.substitute(catalog=catalog))
        self._duck_conn.execute(SQL_CREATE_INFORMATION_SCHEMA_COLUMNS_EXT.substitute(catalog=catalog))
        self._duck_conn.execute(SQL_CREATE_INFORMATION_SCHEMA_COLUMNS_VIEW.substitute(catalog=catalog))


class FakeResultBatch(ResultBatch):
    def __init__(self, use_dict_result: bool, batch: pyarrow.RecordBatch):
        self._use_dict_result = use_dict_result
        self._batch = batch

    def create_iter(
        self, **kwargs: dict[str, Any]
    ) -> Iterator[dict | Exception] | Iterator[tuple | Exception] | Iterator[pyarrow.Table] | Iterator[pd.DataFrame]:
        if self._use_dict_result:
            return iter(self._batch.to_pylist())

        return iter(tuple(d.values()) for d in self._batch.to_pylist())

    @property
    def rowcount(self) -> int:
        return self._batch.num_rows

    def to_pandas(self) -> pd.DataFrame:
        raise NotImplementedError()

    def to_arrow(self) -> pyarrow.Table:
        raise NotImplementedError()


CopyResult = tuple[
    str,
    str,
    int,
    int,
    int,
    int,
    Optional[str],
    Optional[int],
    Optional[int],
    Optional[str],
]

WritePandasResult = tuple[
    bool,
    int,
    int,
    Sequence[CopyResult],
]


def write_pandas(
    conn: FakeSnowflakeConnection,
    df: pd.DataFrame,
    table_name: str,
    database: str | None = None,
    schema: str | None = None,
    chunk_size: int | None = None,
    compression: str = "gzip",
    on_error: str = "abort_statement",
    parallel: int = 4,
    quote_identifiers: bool = True,
    auto_create_table: bool = False,
    create_temp_table: bool = False,
    overwrite: bool = False,
    table_type: Literal["", "temp", "temporary", "transient"] = "",
    **kwargs: Any,
) -> WritePandasResult:
    count = conn._insert_df(df, table_name, database, schema)  # noqa: SLF001

    # mocks https://docs.snowflake.com/en/sql-reference/sql/copy-into-table.html#output
    mock_copy_results = [("fakesnow/file0.txt", "LOADED", count, count, 1, 0, None, None, None, None)]

    # return success
    return (True, len(mock_copy_results), count, mock_copy_results)
