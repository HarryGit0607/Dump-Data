"""Run a SELECT and either preview it or stream it to CSV."""

from __future__ import annotations

from typing import Any, Sequence

from ._dbapi import close_quietly, execute
from .csvexport import _encode


def preview_query(
    connection,
    sql: str,
    *,
    limit: int = 20,
    params: Sequence[Any] = (),
) -> tuple[list[str], list[tuple]]:
    """Fetch the first ``limit`` rows. Does not rewrite the SQL."""
    cursor = connection.cursor()
    try:
        execute(cursor, sql, params)
        columns = [item[0] for item in (cursor.description or ())]
        rows = cursor.fetchmany(limit) if limit else []
        return list(columns), list(rows)
    finally:
        close_quietly(cursor)


def format_preview(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    null_string: str = "",
    delimiter: str = ",",
) -> str:
    """CSV text, header included, suitable for stdout."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_encode(value, null_string) for value in row])
    return buffer.getvalue()
