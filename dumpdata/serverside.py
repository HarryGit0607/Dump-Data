"""Statement builders for the fast path: never move the rows off the server.

When the query and the destination table live in the same database, the whole
result set can be written by the engine itself.  No rows cross the network, no
driver converts values into Python objects, and a load that takes hours through
a client usually finishes in a fraction of the time.
"""

from __future__ import annotations

from typing import Any, Sequence

from .columns import DuplicateColumnError, dedupe_column_names
from .dialects import Dialect, get_dialect


def _strip_statement(query: str) -> str:
    return query.strip().rstrip(";").strip()


def ctas_sql(
    table: str,
    query: str,
    dialect: Any,
    *,
    schema: str | None = None,
    unlogged: bool = False,
    nologging: bool = False,
    parallel: int | None = None,
) -> str:
    """``CREATE TABLE ... AS SELECT`` for a new target table.

    The new table inherits the column names *and* types of the select list, so
    there is no type mapping to get wrong -- but the select list must not
    repeat a column name.  Use :func:`check_duplicate_columns` first.

    ``unlogged`` (PostgreSQL), ``nologging`` (Oracle) and ``parallel`` (Oracle)
    skip or parallelise redo/WAL work.  They make the load markedly faster and
    the table unrecoverable until it is backed up again, which is the right
    trade for a table you can simply rebuild.
    """
    dialect = get_dialect(dialect)
    name = dialect.qualify(table, schema)
    body = _strip_statement(query)

    if unlogged and dialect.name != "postgresql":
        raise ValueError("unlogged tables are PostgreSQL-only")
    if nologging and dialect.name != "oracle":
        raise ValueError("NOLOGGING is Oracle-only")
    if parallel and dialect.name != "oracle":
        raise ValueError("the PARALLEL clause here is Oracle-only")

    if dialect.name == "postgresql":
        keyword = "CREATE UNLOGGED TABLE" if unlogged else "CREATE TABLE"
        return f"{keyword} {name} AS\n{body}"
    if dialect.name in ("mysql", "sqlite"):
        return f"CREATE TABLE {name} AS\n{body}"
    if dialect.name == "oracle":
        clauses = ""
        if nologging:
            clauses += " NOLOGGING"
        if parallel:
            clauses += f" PARALLEL {int(parallel)}"
        return f"CREATE TABLE {name}{clauses} AS\n{body}"
    if dialect.name == "sqlserver":
        return f"SELECT src.*\nINTO {name}\nFROM (\n{body}\n) AS src"
    raise ValueError(f"no CTAS form known for dialect {dialect.name!r}")


def insert_select_sql(
    table: str,
    query: str,
    dialect: Any,
    *,
    columns: Sequence[str] | None = None,
    schema: str | None = None,
    direct_path: bool = False,
) -> str:
    """``INSERT INTO ... SELECT`` for a target table that already exists.

    ``direct_path`` adds the engine's bulk-load hint: Oracle's ``/*+ APPEND */``
    (writes above the high-water mark, bypassing the buffer cache) or SQL
    Server's ``WITH (TABLOCK)`` (enables minimal logging).  Both take stronger
    locks on the target, so run them when nothing else is writing to it.
    """
    dialect = get_dialect(dialect)
    name = dialect.qualify(table, schema)
    body = _strip_statement(query)
    column_list = ""
    if columns:
        joined = ", ".join(dialect.quote(column) for column in columns)
        column_list = f" ({joined})"

    if direct_path and dialect.name == "oracle":
        return f"INSERT /*+ APPEND */ INTO {name}{column_list}\n{body}"
    if direct_path and dialect.name == "sqlserver":
        return f"INSERT INTO {name} WITH (TABLOCK){column_list}\n{body}"
    if direct_path:
        raise ValueError(f"no direct-path hint known for dialect {dialect.name!r}")
    return f"INSERT INTO {name}{column_list}\n{body}"


def result_columns(
    connection,
    query: str,
    dialect: Any,
    params: Sequence[Any] = (),
    *,
    wrap: bool = False,
) -> list:
    """Column names a query returns, without fetching any rows.

    By default the query is executed as written and only ``cursor.description``
    is read: no rows are fetched, so the engine never has to produce the result
    set.  That matters here because the two engines that report repeated column
    names most usefully are also the ones that refuse them inside a derived
    table -- Oracle raises ORA-00918 for ``SELECT * FROM (SELECT a.id, b.id ...)``
    and SQLite silently renames the second one to ``id:1``.

    ``wrap=True`` uses a ``WHERE 1 = 0`` derived table instead, which avoids
    executing the query at all on engines that tolerate the wrapping.
    """
    dialect = get_dialect(dialect)
    probe = (
        f"SELECT * FROM (\n{_strip_statement(query)}\n) src WHERE 1 = 0"
        if wrap
        else _strip_statement(query)
    )
    cursor = connection.cursor()
    try:
        if params:
            cursor.execute(probe, tuple(params))
        else:
            cursor.execute(probe)
        return [str(item[0]) for item in (cursor.description or ())]
    finally:
        try:
            cursor.close()
        except Exception:  # pragma: no cover
            pass


def check_duplicate_columns(names: Sequence[str], dialect: Any = "postgresql") -> dict:
    """Report repeated column names, case-insensitively.

    Returns ``{name: [positions]}`` for every name that appears more than once,
    which is the check to run before a CTAS: a result set may repeat ``id``,
    a table may not.
    """
    dialect = get_dialect(dialect)
    seen: dict = {}
    for position, name in enumerate(names):
        seen.setdefault(str(name).casefold(), []).append(position)
    return {
        str(names[positions[0]]): positions
        for positions in seen.values()
        if len(positions) > 1
    }


def aliased_select_list(names: Sequence[str], dialect: Any = "postgresql") -> str:
    """A select list that renames repeated columns so a CTAS will succeed.

    Given the columns a join returns, produce ``src."id" AS "id",
    src."id" AS "id_2", ...`` -- every column of the join is kept, each under a
    name a table can hold.
    """
    dialect = get_dialect(dialect)
    unique = dedupe_column_names(
        list(names), max_length=dialect.max_identifier_length, on_duplicate="suffix"
    )
    parts = []
    for original, target in zip(names, unique):
        quoted = dialect.quote(str(original))
        parts.append(
            f"src.{quoted}" if original == target else f"src.{quoted} AS {dialect.quote(target)}"
        )
    return ",\n       ".join(parts)


def rewrite_for_ctas(
    table: str,
    query: str,
    names: Sequence[str],
    dialect: Any,
    *,
    schema: str | None = None,
    **ctas_kwargs,
) -> str:
    """CTAS for a query whose select list repeats column names.

    The original query becomes a derived table and the outer select aliases the
    duplicates apart, so ``SELECT c.*, o.* FROM customers c JOIN orders o ...``
    can be dumped as-is without hand-writing an alias for all of its columns.
    """
    dialect = get_dialect(dialect)
    projection = aliased_select_list(names, dialect)
    wrapped = f"SELECT {projection}\nFROM (\n{_strip_statement(query)}\n) src"
    return ctas_sql(table, wrapped, dialect, schema=schema, **ctas_kwargs)


__all__ = [
    "DuplicateColumnError",
    "Dialect",
    "aliased_select_list",
    "check_duplicate_columns",
    "ctas_sql",
    "insert_select_sql",
    "result_columns",
    "rewrite_for_ctas",
]
