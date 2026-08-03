"""Per-engine SQL differences needed to page through a query and load a table.

Only the handful of constructs that actually differ between engines live here:
identifier quoting, parameter markers, row limiting and the physical types used
when the target table has to be created.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


#: Logical column types produced by :mod:`dumpdata.columns`.
LOGICAL_TYPES = ("int", "float", "decimal", "bool", "datetime", "date", "bytes", "text")


@dataclass(frozen=True)
class Dialect:
    """Describes how to speak SQL to one database engine."""

    name: str
    paramstyle: str
    quote_open: str
    quote_close: str
    limit_style: str
    type_map: Dict[str, str]
    max_identifier_length: int = 63
    aliases: tuple = field(default=())

    def quote(self, identifier: str) -> str:
        escaped = identifier.replace(self.quote_close, self.quote_close * 2)
        return f"{self.quote_open}{escaped}{self.quote_close}"

    def qualify(self, table: str, schema: str | None = None) -> str:
        if schema:
            return f"{self.quote(schema)}.{self.quote(table)}"
        return self.quote(table)

    def placeholder(self, position: int) -> str:
        """Parameter marker for the *position*-th bind value (0-based)."""
        if self.paramstyle == "qmark":
            return "?"
        if self.paramstyle == "format":
            return "%s"
        if self.paramstyle == "numeric":
            return f":{position + 1}"
        if self.paramstyle == "named":
            return f":p{position}"
        raise ValueError(f"unsupported paramstyle {self.paramstyle!r}")

    def placeholders(self, count: int) -> str:
        return ", ".join(self.placeholder(i) for i in range(count))

    def apply_limit(self, sql: str, count: int) -> str:
        """Return *sql* restricted to at most *count* rows.

        ``fetch_first`` and ``offset_fetch`` both require the statement to
        already carry an ORDER BY; every caller in this package pages by an
        ordered key, so that holds.
        """
        if count <= 0:
            raise ValueError("limit must be positive")
        if self.limit_style == "limit":
            return f"{sql}\nLIMIT {int(count)}"
        if self.limit_style == "fetch_first":
            return f"{sql}\nFETCH FIRST {int(count)} ROWS ONLY"
        if self.limit_style == "offset_fetch":
            return f"{sql}\nOFFSET 0 ROWS FETCH NEXT {int(count)} ROWS ONLY"
        raise ValueError(f"unsupported limit style {self.limit_style!r}")

    def physical_type(self, logical_type: str) -> str:
        try:
            return self.type_map[logical_type]
        except KeyError:
            return self.type_map["text"]


SQLITE = Dialect(
    name="sqlite",
    paramstyle="qmark",
    quote_open='"',
    quote_close='"',
    limit_style="limit",
    max_identifier_length=1000,
    type_map={
        "int": "INTEGER",
        "float": "REAL",
        "decimal": "NUMERIC",
        "bool": "INTEGER",
        "datetime": "TIMESTAMP",
        "date": "DATE",
        "bytes": "BLOB",
        "text": "TEXT",
    },
)

POSTGRESQL = Dialect(
    name="postgresql",
    paramstyle="format",
    quote_open='"',
    quote_close='"',
    limit_style="limit",
    max_identifier_length=63,
    aliases=("postgres", "pg", "psycopg2", "psycopg", "redshift"),
    type_map={
        "int": "BIGINT",
        "float": "DOUBLE PRECISION",
        "decimal": "NUMERIC",
        "bool": "BOOLEAN",
        "datetime": "TIMESTAMP",
        "date": "DATE",
        "bytes": "BYTEA",
        "text": "TEXT",
    },
)

MYSQL = Dialect(
    name="mysql",
    paramstyle="format",
    quote_open="`",
    quote_close="`",
    limit_style="limit",
    max_identifier_length=64,
    aliases=("mariadb", "pymysql", "mysqldb"),
    type_map={
        "int": "BIGINT",
        "float": "DOUBLE",
        "decimal": "DECIMAL(38,10)",
        "bool": "TINYINT(1)",
        "datetime": "DATETIME(6)",
        "date": "DATE",
        "bytes": "LONGBLOB",
        "text": "LONGTEXT",
    },
)

ORACLE = Dialect(
    name="oracle",
    paramstyle="numeric",
    quote_open='"',
    quote_close='"',
    limit_style="fetch_first",
    max_identifier_length=128,
    aliases=("cx_oracle", "oracledb"),
    type_map={
        "int": "NUMBER(19)",
        "float": "BINARY_DOUBLE",
        "decimal": "NUMBER",
        "bool": "NUMBER(1)",
        "datetime": "TIMESTAMP",
        "date": "DATE",
        "bytes": "BLOB",
        "text": "CLOB",
    },
)

SQLSERVER = Dialect(
    name="sqlserver",
    paramstyle="qmark",
    quote_open="[",
    quote_close="]",
    limit_style="offset_fetch",
    max_identifier_length=128,
    aliases=("mssql", "pyodbc", "tsql"),
    type_map={
        "int": "BIGINT",
        "float": "FLOAT",
        "decimal": "DECIMAL(38,10)",
        "bool": "BIT",
        "datetime": "DATETIME2",
        "date": "DATE",
        "bytes": "VARBINARY(MAX)",
        "text": "NVARCHAR(MAX)",
    },
)

_ALL = (SQLITE, POSTGRESQL, MYSQL, ORACLE, SQLSERVER)

_REGISTRY: Dict[str, Dialect] = {}
for _dialect in _ALL:
    _REGISTRY[_dialect.name] = _dialect
    for _alias in _dialect.aliases:
        _REGISTRY[_alias] = _dialect


def get_dialect(name: str | Dialect) -> Dialect:
    """Look up a dialect by name or alias (``"pg"``, ``"mssql"``, ...)."""
    if isinstance(name, Dialect):
        return name
    try:
        return _REGISTRY[name.strip().lower()]
    except KeyError:
        known = ", ".join(sorted(d.name for d in _ALL))
        raise ValueError(f"unknown dialect {name!r}; known dialects: {known}") from None


def dialect_names() -> tuple:
    return tuple(sorted(d.name for d in _ALL))
