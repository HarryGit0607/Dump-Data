"""Build the Office 771001 TPA upload dashboard from PGIPH_STG_TPA_UPLOAD."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Optional

from ._dbapi import close_quietly, execute
from .tpa_insights import (
    DEFAULT_OFFICE,
    DEFAULT_TABLE,
    TpaDashboardError,
    assert_ident,
    compute_insights,
    dump_insights,
    map_columns,
    office_sql,
)


STATIC_DIR = Path(__file__).resolve().parent / "static" / "tpa_dashboard"


def table_columns(connection, table: str) -> list[str]:
    table = assert_ident(table)
    cursor = connection.cursor()
    try:
        execute(cursor, f"SELECT * FROM {table} WHERE 1=0")
        return [item[0] for item in (cursor.description or ())]
    finally:
        close_quietly(cursor)


def fetch_office_rows(
    connection,
    *,
    table: str = DEFAULT_TABLE,
    office: str = DEFAULT_OFFICE,
    pol_column: str = "PSTU_POL_NO",
    sqlite: bool = False,
    max_rows: Optional[int] = None,
) -> tuple[list[str], list[dict[str, Any]], int, int]:
    """Return (columns, office_rows, table_row_count, office_row_count)."""
    table = assert_ident(table)
    pol_column = assert_ident(pol_column)
    columns = table_columns(connection, table)
    if not any(name.upper() == pol_column.upper() for name in columns):
        raise TpaDashboardError(
            f"{table} has no {pol_column} column; found: {', '.join(columns) or '(none)'}"
        )
    actual_pol = next(name for name in columns if name.upper() == pol_column.upper())
    expr = office_sql(actual_pol)
    placeholder = "?" if sqlite else ":1"
    cursor = connection.cursor()
    try:
        execute(cursor, f"SELECT COUNT(*) FROM {table}")
        table_rows = int(cursor.fetchone()[0])
        execute(cursor, f"SELECT COUNT(*) FROM {table} WHERE {expr} = {placeholder}", (office,))
        office_rows = int(cursor.fetchone()[0])
        execute(cursor, f"SELECT * FROM {table} WHERE {expr} = {placeholder}", (office,))
        names = [item[0] for item in (cursor.description or ())]
        rows: list[dict[str, Any]] = []
        while True:
            remaining = None if max_rows is None else max_rows - len(rows)
            if remaining is not None and remaining <= 0:
                break
            batch = cursor.fetchmany(5000 if remaining is None else min(5000, remaining))
            if not batch:
                break
            for raw in batch:
                rows.append(dict(zip(names, raw)))
        return names, rows, table_rows, office_rows
    finally:
        close_quietly(cursor)


def copy_static(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if not STATIC_DIR.is_dir():
        raise TpaDashboardError(f"dashboard templates missing at {STATIC_DIR}")
    for name in ("index.html", "styles.css", "app.js"):
        src = STATIC_DIR / name
        if not src.is_file():
            raise TpaDashboardError(f"missing template {src}")
        shutil.copyfile(src, output_dir / name)


def write_dashboard(payload: dict[str, Any], output_dir: str | Path) -> Path:
    dest = Path(output_dir)
    copy_static(dest)
    dump_insights(payload, str(dest / "data.json"))
    return dest / "index.html"


def empty_payload(*, office: str, table: str, error: str, source: str = "oracle") -> dict[str, Any]:
    return compute_insights(
        [],
        office=office,
        table=table,
        columns=[],
        mapped={},
        source=source,
        table_rows=0,
        error=error,
        reachable=False,
    )


def insights_from_connection(
    connection,
    *,
    office: str = DEFAULT_OFFICE,
    table: str = DEFAULT_TABLE,
    sqlite: bool = False,
    max_rows: Optional[int] = None,
    source: str = "oracle",
) -> dict[str, Any]:
    columns, rows, table_rows, office_rows = fetch_office_rows(
        connection,
        table=table,
        office=office,
        sqlite=sqlite,
        max_rows=max_rows,
    )
    mapped = map_columns(columns)
    if "policy" not in mapped:
        raise TpaDashboardError(f"{table} has no policy-number column to derive office code from")
    payload = compute_insights(
        rows,
        office=office,
        table=table,
        columns=columns,
        mapped=mapped,
        source=source,
        table_rows=table_rows,
        reachable=True,
    )
    payload["meta"]["office_row_count"] = office_rows
    payload["quality"]["table_rows"] = table_rows
    payload["quality"]["office_rows"] = office_rows
    payload["quality"]["office_share_pct"] = (
        round(100.0 * office_rows / table_rows, 2) if table_rows else 0.0
    )
    return payload
