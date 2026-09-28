"""Turn a DB-API ``cursor.description`` into a target table definition.

A join across several tables almost always returns repeated column names
(``id``, ``name``, ``created_at`` ...).  A result set is happy to carry them,
but a *table* cannot, so ``CREATE TABLE AS SELECT`` fails with an error such as
``column "id" specified more than once``.  This module is where that is dealt
with: duplicates are either reported clearly or renamed deterministically so
every column of the join survives the dump.
"""

from __future__ import annotations

import datetime as _dt
import decimal
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .dialects import Dialect


class DuplicateColumnError(ValueError):
    """Raised when a query returns repeated column names and renaming is off."""

    def __init__(self, duplicates: dict):
        self.duplicates = duplicates
        detail = "; ".join(
            f"{name!r} at positions {positions}" for name, positions in sorted(duplicates.items())
        )
        super().__init__(
            "the source query returns repeated column names: "
            f"{detail}. Alias them in the SELECT list (e.g. "
            "'c.id AS customer_id, o.id AS order_id') or pass "
            "on_duplicate='suffix' to rename them automatically."
        )


@dataclass(frozen=True)
class Column:
    """One column of the target table."""

    name: str
    source_name: str
    logical_type: str

    @property
    def renamed(self) -> bool:
        return self.name != self.source_name

    def ddl(self, dialect: Dialect) -> str:
        return f"{dialect.quote(self.name)} {dialect.physical_type(self.logical_type)}"


def _clean(name: Any, position: int) -> str:
    text = "" if name is None else str(name).strip()
    return text or f"column_{position + 1}"


def _fit(name: str, max_length: int) -> str:
    return name if len(name) <= max_length else name[:max_length]


def dedupe_column_names(
    names: Sequence[Any],
    *,
    max_length: int = 63,
    on_duplicate: str = "suffix",
) -> list:
    """Return unique, length-bounded column names in source order.

    Comparison is case-insensitive because most engines fold identifiers, so
    ``ID`` and ``id`` would collide in the target table even though the driver
    reports them as distinct.
    """
    if on_duplicate not in ("suffix", "error"):
        raise ValueError("on_duplicate must be 'suffix' or 'error'")

    cleaned = [_clean(name, position) for position, name in enumerate(names)]

    if on_duplicate == "error":
        seen: dict = {}
        for position, name in enumerate(cleaned):
            seen.setdefault(name.casefold(), []).append(position)
        duplicates = {
            cleaned[positions[0]]: positions for positions in seen.values() if len(positions) > 1
        }
        if duplicates:
            raise DuplicateColumnError(duplicates)
        return [_fit(name, max_length) for name in cleaned]

    used: set = set()
    result: list = []
    for name in cleaned:
        candidate = _fit(name, max_length)
        counter = 2
        while candidate.casefold() in used:
            suffix = f"_{counter}"
            candidate = _fit(name, max_length - len(suffix)) + suffix
            counter += 1
        used.add(candidate.casefold())
        result.append(candidate)
    return result


def infer_logical_type(values: Iterable[Any]) -> str:
    """Infer a logical column type from sample values.

    Falls back to ``text`` for all-NULL or mixed columns, which is the only
    choice that never loses data.
    """
    found: set = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            found.add("bool")
        elif isinstance(value, int):
            found.add("int")
        elif isinstance(value, float):
            found.add("float")
        elif isinstance(value, decimal.Decimal):
            found.add("decimal")
        elif isinstance(value, _dt.datetime):
            found.add("datetime")
        elif isinstance(value, _dt.date):
            found.add("date")
        elif isinstance(value, (bytes, bytearray, memoryview)):
            found.add("bytes")
        else:
            found.add("text")
        if len(found) > 1:
            break

    if not found:
        return "text"
    if len(found) == 1:
        return found.pop()
    if found <= {"int", "float", "decimal", "bool"}:
        return "decimal"
    if found <= {"date", "datetime"}:
        return "datetime"
    return "text"


def describe_columns(
    description: Sequence[Any],
    sample_rows: Sequence[Sequence[Any]],
    dialect: Dialect,
    *,
    on_duplicate: str = "suffix",
) -> list:
    """Build the target column list from ``cursor.description`` plus samples.

    Driver type codes are not portable, so the physical type is inferred from
    the values actually returned.  Pass a representative ``sample_rows`` (the
    first batch is normally enough); anything unclear becomes ``text``.
    """
    source_names = [_clean(item[0], position) for position, item in enumerate(description)]
    names = dedupe_column_names(
        source_names,
        max_length=dialect.max_identifier_length,
        on_duplicate=on_duplicate,
    )
    columns = []
    for position, (name, source_name) in enumerate(zip(names, source_names)):
        logical_type = infer_logical_type(
            row[position] for row in sample_rows if position < len(row)
        )
        columns.append(Column(name=name, source_name=source_name, logical_type=logical_type))
    return columns


def create_table_sql(
    table: str,
    columns: Sequence[Column],
    dialect: Dialect,
    *,
    schema: str | None = None,
    if_not_exists: bool = True,
) -> str:
    """CREATE TABLE statement for the described columns.

    Deliberately free of indexes, primary keys and constraints: build those
    *after* the load, otherwise every inserted row pays to maintain them.
    """
    if not columns:
        raise ValueError("cannot create a table with no columns")
    exists_clause = ""
    if if_not_exists and dialect.name in ("sqlite", "postgresql", "mysql"):
        exists_clause = "IF NOT EXISTS "
    body = ",\n".join(f"    {column.ddl(dialect)}" for column in columns)
    return f"CREATE TABLE {exists_clause}{dialect.qualify(table, schema)} (\n{body}\n)"


def insert_sql(
    table: str,
    columns: Sequence[Column],
    dialect: Dialect,
    *,
    schema: str | None = None,
) -> str:
    """Parameterised INSERT used with ``executemany`` for batch loading."""
    if not columns:
        raise ValueError("cannot insert without columns")
    column_list = ", ".join(dialect.quote(column.name) for column in columns)
    values = dialect.placeholders(len(columns))
    return f"INSERT INTO {dialect.qualify(table, schema)} ({column_list}) VALUES ({values})"
