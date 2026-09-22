"""Export a query to CSV so the target's native bulk loader can ingest it.

Every engine has a loader that is far faster than INSERT statements (``COPY``,
``LOAD DATA INFILE``, ``bcp``, SQL*Loader).  They all read flat files, so when
the source and target are different servers the quickest route is usually
export once, load once.
"""

from __future__ import annotations

import csv
import gzip
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ._dbapi import close_quietly, execute
from .columns import dedupe_column_names
from .copier import Progress, Source
from .dialects import get_dialect


@dataclass
class ExportResult:
    rows: int
    files: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def rows_per_second(self) -> float:
        return self.rows / self.elapsed_seconds if self.elapsed_seconds > 0 else 0.0


def _open(path: str, compress: bool):
    if compress:
        return gzip.open(path, "wt", newline="", encoding="utf-8")
    return open(path, "w", newline="", encoding="utf-8")


def _encode(value: Any, null_string: str) -> Any:
    if value is None:
        return null_string
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, bool):
        return "1" if value else "0"
    return value


def export_query_to_csv(
    source: Source,
    path: str,
    *,
    batch_size: int = 20_000,
    delimiter: str = ",",
    null_string: str = "",
    header: bool = True,
    compress: bool = False,
    max_rows_per_file: int | None = None,
    max_rows: int | None = None,
    on_duplicate_columns: str = "suffix",
    progress: Callable[[Progress], None] | None = None,
) -> ExportResult:
    """Stream a query into one or more CSV files.

    Splitting with ``max_rows_per_file`` lets several loader processes run
    against the same target in parallel, which is where most of the remaining
    speed-up lives once INSERT statements are out of the picture.
    """
    started = time.monotonic()
    cursor = source.cursor()
    files: list = []
    columns: list = []
    total = 0
    batch_number = 0
    handle = None
    writer = None
    rows_in_file = 0
    part = 0

    def open_next():
        nonlocal handle, writer, rows_in_file, part
        if handle is not None:
            handle.close()
        if max_rows_per_file is None:
            target_path = f"{path}.gz" if compress and not path.endswith(".gz") else path
        else:
            target_path = _part_path(path, part, compress)
        handle = _open(target_path, compress)
        writer = csv.writer(handle, delimiter=delimiter, lineterminator="\n")
        if header:
            writer.writerow(columns)
        files.append(target_path)
        rows_in_file = 0
        part += 1

    try:
        execute(cursor, source.query, source.params)
        columns = dedupe_column_names(
            [item[0] for item in (cursor.description or ())],
            max_length=source.dialect.max_identifier_length,
            on_duplicate=on_duplicate_columns,
        )
        open_next()
        while True:
            remaining = None if max_rows is None else max_rows - total
            if remaining is not None and remaining <= 0:
                break
            fetch_size = batch_size if remaining is None else min(batch_size, remaining)
            rows = cursor.fetchmany(fetch_size)
            if not rows:
                break
            for row in rows:
                if max_rows_per_file is not None and rows_in_file >= max_rows_per_file:
                    open_next()
                writer.writerow([_encode(value, null_string) for value in row])
                rows_in_file += 1
            total += len(rows)
            batch_number += 1
            if progress:
                progress(
                    Progress(
                        batch=batch_number,
                        rows_in_batch=len(rows),
                        rows_copied=total,
                        elapsed_seconds=time.monotonic() - started,
                    )
                )
    finally:
        if handle is not None:
            handle.close()
        close_quietly(cursor)

    return ExportResult(
        rows=total,
        files=files,
        columns=columns,
        elapsed_seconds=time.monotonic() - started,
    )


def _part_path(path: str, part: int, compress: bool) -> str:
    suffix = ".csv.gz" if compress else ".csv"
    base = path
    for candidate in (".csv.gz", ".csv"):
        if base.endswith(candidate):
            base = base[: -len(candidate)]
            break
    return f"{base}.part{part:04d}{suffix}"


def bulk_load_command(
    dialect: Any,
    table: str,
    csv_path: str,
    *,
    schema: str | None = None,
    delimiter: str = ",",
    header: bool = True,
    null_string: str = "",
) -> str:
    """The native bulk-load invocation for a CSV produced above."""
    dialect = get_dialect(dialect)
    name = dialect.qualify(table, schema)

    if dialect.name == "postgresql":
        options = ["FORMAT csv", f"DELIMITER '{delimiter}'", f"NULL '{null_string}'"]
        if header:
            options.append("HEADER true")
        joined = ", ".join(options)
        return (
            f"\\copy {name} FROM '{csv_path}' WITH ({joined})\n"
            "-- run from psql; use COPY (without the backslash) to read a file "
            "on the server itself"
        )
    if dialect.name == "mysql":
        ignore = "IGNORE 1 LINES\n" if header else ""
        return (
            f"LOAD DATA LOCAL INFILE '{csv_path}'\n"
            f"INTO TABLE {name}\n"
            f"FIELDS TERMINATED BY '{delimiter}' OPTIONALLY ENCLOSED BY '\"'\n"
            f"LINES TERMINATED BY '\\n'\n"
            f"{ignore}"
            "-- needs local_infile=1 on both client and server"
        )
    if dialect.name == "sqlserver":
        first_row = "-F 2 " if header else ""
        return (
            f'bcp {table} in "{csv_path}" -c -t "{delimiter}" {first_row}'
            "-b 50000 -S <server> -d <database> -T\n"
            "-- add -h \"TABLOCK\" for minimal logging"
        )
    if dialect.name == "oracle":
        skip = "SKIP=1\n" if header else ""
        return (
            f"sqlldr userid=<user>/<pass>@<tns> control=load.ctl direct=true parallel=true\n"
            "-- load.ctl:\n"
            "LOAD DATA\n"
            f"INFILE '{csv_path}'\n"
            f"{skip}"
            f"APPEND INTO TABLE {table}\n"
            f"FIELDS TERMINATED BY '{delimiter}' OPTIONALLY ENCLOSED BY '\"'\n"
            "TRAILING NULLCOLS\n(<column list>)"
        )
    if dialect.name == "sqlite":
        return (
            ".mode csv\n"
            f".import --skip {1 if header else 0} '{csv_path}' {table}\n"
            "-- run from the sqlite3 shell"
        )
    raise ValueError(f"no bulk loader known for dialect {dialect.name!r}")
