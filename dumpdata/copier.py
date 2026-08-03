"""Copy the result of a slow, wide join into a table, in resumable batches.

Two read strategies are offered because they fail in opposite ways:

``stream``
    Execute the query once and pull it with ``fetchmany``.  The expensive join
    runs a single time, which is what you want when the query itself is slow.
    It cannot resume: if the job dies after 90 minutes, the query runs again
    from the beginning.  Use a server-side cursor (see ``Source.cursor_factory``)
    or the client will buffer every row in memory.

``keyset``
    Re-run the query per batch, each time asking for the next slice after the
    last key seen.  This resumes from a checkpoint and bounds memory, but the
    join is re-executed once per batch, so it is only sane when a single batch
    is cheap -- typically when reading from a staging table rather than from
    the original join.

For a two-hour join the winning combination is usually: materialise once with
``CREATE TABLE ... AS SELECT`` (see :mod:`dumpdata.serverside`), then ``keyset``
off the staging table.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Sequence

from ._dbapi import close_quietly, execute
from .columns import Column, create_table_sql, describe_columns, insert_sql
from .dialects import Dialect, get_dialect

#: Exception class names treated as transient (connection dropped, deadlock...).
DEFAULT_RETRYABLE = ("OperationalError", "InterfaceError", "InternalError")


@dataclass
class Source:
    """Where the rows come from.

    ``cursor_factory`` exists for server-side cursors, which is how you stream
    millions of rows without buffering them client-side::

        Source(conn, sql, dialect="postgresql",
               cursor_factory=lambda c: c.cursor(name="dump_cursor"))
    """

    connection: Any
    query: str
    params: Sequence[Any] = ()
    dialect: Any = "sqlite"
    cursor_factory: Callable[[Any], Any] | None = None
    arraysize: int | None = None

    def __post_init__(self):
        self.dialect = get_dialect(self.dialect)

    def cursor(self):
        if self.cursor_factory:
            cursor = self.cursor_factory(self.connection)
        else:
            cursor = self.connection.cursor()
        if self.arraysize:
            try:
                cursor.arraysize = self.arraysize
            except AttributeError:
                pass
        return cursor


@dataclass
class Target:
    """Where the rows go."""

    connection: Any
    table: str
    schema: str | None = None
    dialect: Any = "sqlite"

    def __post_init__(self):
        self.dialect = get_dialect(self.dialect)

    @property
    def qualified_name(self) -> str:
        return self.dialect.qualify(self.table, self.schema)


@dataclass
class Progress:
    """Snapshot handed to the progress callback after every committed batch."""

    batch: int
    rows_in_batch: int
    rows_copied: int
    elapsed_seconds: float
    last_key: Any = None

    @property
    def rows_per_second(self) -> float:
        return self.rows_copied / self.elapsed_seconds if self.elapsed_seconds > 0 else 0.0

    def eta_seconds(self, total_rows: int) -> float | None:
        rate = self.rows_per_second
        if not rate or total_rows <= self.rows_copied:
            return None
        return (total_rows - self.rows_copied) / rate

    def __str__(self) -> str:
        return (
            f"batch {self.batch}: {self.rows_copied:,} rows in "
            f"{self.elapsed_seconds:,.1f}s ({self.rows_per_second:,.0f} rows/s)"
        )


@dataclass
class CopyOptions:
    """Tuning knobs for :func:`copy_query_to_table`.

    ``column_types`` overrides the inferred physical type of a target column,
    keyed by target column name and using the logical names from
    :mod:`dumpdata.dialects` (``int``, ``decimal``, ``datetime``, ``text``...).
    Types are inferred from the first batch, so override any column that starts
    out entirely NULL.
    """

    mode: str = "stream"
    batch_size: int = 20_000
    key_column: str | None = None
    key_start: Any = None
    create_target: bool = True
    truncate_target: bool = False
    require_unique_key: bool = True
    on_duplicate_columns: str = "suffix"
    column_types: dict = field(default_factory=dict)
    checkpoint_path: str | None = None
    max_batches: int | None = None
    retries: int = 3
    retry_backoff_seconds: float = 2.0
    progress: Callable[["Progress"], None] | None = None
    is_retryable: Callable[[BaseException], bool] | None = None

    def __post_init__(self):
        if self.mode not in ("stream", "keyset"):
            raise ValueError("mode must be 'stream' or 'keyset'")
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if self.mode == "keyset" and not self.key_column:
            raise ValueError("keyset mode needs key_column (a unique, sortable column)")
        if self.checkpoint_path and self.mode != "keyset":
            raise ValueError("checkpoints require mode='keyset'; stream mode cannot resume")


@dataclass
class CopyResult:
    rows_copied: int
    batches: int
    elapsed_seconds: float
    columns: list = field(default_factory=list)
    last_key: Any = None
    resumed_from: Any = None
    complete: bool = True

    @property
    def rows_per_second(self) -> float:
        return self.rows_copied / self.elapsed_seconds if self.elapsed_seconds > 0 else 0.0

    @property
    def renamed_columns(self) -> list:
        return [column for column in self.columns if column.renamed]


def default_is_retryable(error: BaseException) -> bool:
    return type(error).__name__ in DEFAULT_RETRYABLE


# --------------------------------------------------------------------------- #
# checkpoints
# --------------------------------------------------------------------------- #

def _encode_key(value: Any) -> dict:
    if value is None:
        return {"type": "none", "value": None}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "int", "value": value}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, decimal.Decimal):
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, _dt.datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, _dt.date):
        return {"type": "date", "value": value.isoformat()}
    return {"type": "str", "value": str(value)}


def _decode_key(payload: dict) -> Any:
    kind = payload.get("type")
    value = payload.get("value")
    if kind in (None, "none"):
        return None
    if kind == "decimal":
        return decimal.Decimal(value)
    if kind == "datetime":
        return _dt.datetime.fromisoformat(value)
    if kind == "date":
        return _dt.date.fromisoformat(value)
    return value


class Checkpoint:
    """Last successfully committed key, persisted next to the job.

    Written after each batch commits, so a restart re-reads no committed rows
    and the copy picks up exactly where it stopped.
    """

    def __init__(self, path: str):
        self.path = path

    def load(self) -> dict | None:
        if not os.path.exists(self.path):
            return None
        with open(self.path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["last_key"] = _decode_key(payload.get("last_key") or {})
        return payload

    def save(self, *, last_key: Any, rows_copied: int, table: str) -> None:
        payload = {
            "table": table,
            "rows_copied": rows_copied,
            "last_key": _encode_key(last_key),
            "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        }
        temporary = f"{self.path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(temporary, self.path)

    def clear(self) -> None:
        if os.path.exists(self.path):
            os.remove(self.path)


# --------------------------------------------------------------------------- #
# readers
# --------------------------------------------------------------------------- #

def _strip_statement(query: str) -> str:
    return query.strip().rstrip(";").strip()


def build_keyset_query(
    query: str,
    key_column: str,
    dialect: Dialect,
    batch_size: int,
    *,
    param_offset: int = 0,
    inclusive: bool = False,
    with_bound: bool = True,
) -> str:
    """Wrap *query* so it returns the next ``batch_size`` rows after a key.

    The original query is left untouched inside a derived table, which keeps
    its own ORDER BY, GROUP BY and bind parameters valid.  The key bind is
    appended after the query's own parameters so positional paramstyles line
    up.
    """
    key = dialect.quote(key_column)
    operator = ">=" if inclusive else ">"
    predicate = (
        f"\nWHERE src.{key} {operator} {dialect.placeholder(param_offset)}" if with_bound else ""
    )
    wrapped = f"SELECT * FROM (\n{_strip_statement(query)}\n) src{predicate}\nORDER BY src.{key}"
    return dialect.apply_limit(wrapped, batch_size)


def _key_index(description: Sequence[Any], key_column: str) -> int:
    matches = [
        index
        for index, item in enumerate(description)
        if str(item[0]).casefold() == key_column.casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        available = ", ".join(str(item[0]) for item in description)
        raise ValueError(f"key column {key_column!r} is not in the result set; got: {available}")
    raise ValueError(
        f"key column {key_column!r} appears {len(matches)} times in the result set; "
        "paging needs an unambiguous key, so alias it in the SELECT list"
    )


def _stream_batches(source: Source, batch_size: int) -> Iterator[tuple]:
    """Yield ``(description, rows, None)`` from a single execution."""
    cursor = source.cursor()
    try:
        execute(cursor, source.query, source.params)
        description = cursor.description
        first = True
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                if first:
                    yield description, [], None
                return
            first = False
            yield description, rows, None
    finally:
        close_quietly(cursor)


def _keyset_batches(source: Source, options: CopyOptions, start_key: Any) -> Iterator[tuple]:
    """Yield ``(description, rows, last_key)``, re-querying after each batch."""
    dialect = source.dialect
    params = list(source.params)
    last_key = start_key
    key_index = None
    first = True
    while True:
        sql = build_keyset_query(
            source.query,
            options.key_column,
            dialect,
            options.batch_size,
            param_offset=len(params),
            with_bound=last_key is not None,
        )
        bind = params + ([last_key] if last_key is not None else [])
        cursor = source.cursor()
        try:
            try:
                execute(cursor, sql, bind)
            except Exception as error:
                if not first:
                    raise
                raise ValueError(
                    "the paged query failed on its first page; check that key column "
                    f"{options.key_column!r} exists exactly once in the result set and is "
                    f"sortable ({type(error).__name__}: {error})"
                ) from error
            description = cursor.description
            rows = cursor.fetchall()
        finally:
            close_quietly(cursor)

        if not rows:
            if first:
                yield description, [], last_key
            return
        if key_index is None:
            key_index = _key_index(description, options.key_column)
        first = False

        keys = [row[key_index] for row in rows]
        if options.require_unique_key and len(set(keys)) != len(keys):
            raise ValueError(
                f"key column {options.key_column!r} is not unique: paging by it would skip "
                "rows that share a key across a batch boundary. Page by a unique column "
                "(a primary key, or ROWID / ROW_NUMBER() in the query), or set "
                "require_unique_key=False if you have another reason to believe it is safe."
            )

        last_key = keys[-1]
        yield description, rows, last_key
        if len(rows) < options.batch_size:
            return


# --------------------------------------------------------------------------- #
# the copy itself
# --------------------------------------------------------------------------- #

def _prepare_target(
    target: Target,
    columns: Sequence[Column],
    *,
    create: bool,
    truncate: bool,
) -> None:
    if not (create or truncate):
        return
    cursor = target.connection.cursor()
    try:
        if create:
            cursor.execute(
                create_table_sql(target.table, columns, target.dialect, schema=target.schema)
            )
        if truncate:
            cursor.execute(f"DELETE FROM {target.qualified_name}")
    finally:
        close_quietly(cursor)
    target.connection.commit()


def _write_batch(
    target: Target,
    statement: str,
    rows: Sequence[Sequence[Any]],
    options: CopyOptions,
) -> None:
    is_retryable = options.is_retryable or default_is_retryable
    payload = [tuple(row) for row in rows]
    attempt = 0
    while True:
        cursor = target.connection.cursor()
        try:
            cursor.executemany(statement, payload)
            target.connection.commit()
            return
        except Exception as error:
            try:
                target.connection.rollback()
            except Exception:  # pragma: no cover - rollback can fail too
                pass
            attempt += 1
            if attempt > options.retries or not is_retryable(error):
                raise
            time.sleep(options.retry_backoff_seconds * (2 ** (attempt - 1)))
        finally:
            close_quietly(cursor)


def _apply_type_overrides(columns: Sequence[Column], overrides: dict) -> list:
    if not overrides:
        return list(columns)
    lookup = {name.casefold(): logical for name, logical in overrides.items()}
    result = []
    for column in columns:
        logical = lookup.get(column.name.casefold()) or lookup.get(column.source_name.casefold())
        result.append(
            Column(column.name, column.source_name, logical) if logical else column
        )
    return result


def copy_query_to_table(
    source: Source,
    target: Target,
    options: CopyOptions | None = None,
) -> CopyResult:
    """Copy every row of ``source.query`` into ``target.table``.

    The target table is created from the query's own column names, so it
    mirrors the shape of the result set; names repeated by the join are
    renamed (``id``, ``id_2``, ...) rather than dropped.  Rows are committed
    per batch, which keeps transaction size bounded on a multi-hour load.
    """
    options = options or CopyOptions()
    started = time.monotonic()

    checkpoint = Checkpoint(options.checkpoint_path) if options.checkpoint_path else None
    start_key = options.key_start
    rows_copied = 0
    resumed_from = None
    if checkpoint:
        saved = checkpoint.load()
        if saved and saved.get("last_key") is not None:
            start_key = saved["last_key"]
            resumed_from = start_key
            rows_copied = int(saved.get("rows_copied") or 0)

    resuming = resumed_from is not None
    create_target = options.create_target and not resuming
    truncate_target = options.truncate_target and not resuming

    if options.mode == "keyset":
        batches = _keyset_batches(source, options, start_key)
    else:
        batches = _stream_batches(source, options.batch_size)

    columns: list = []
    statement = None
    batch_number = 0
    last_key = resumed_from
    complete = True

    for description, rows, batch_key in batches:
        if statement is None:
            columns = _apply_type_overrides(
                describe_columns(
                    description,
                    rows,
                    target.dialect,
                    on_duplicate=options.on_duplicate_columns,
                ),
                options.column_types,
            )
            _prepare_target(target, columns, create=create_target, truncate=truncate_target)
            statement = insert_sql(target.table, columns, target.dialect, schema=target.schema)

        if not rows:
            continue

        _write_batch(target, statement, rows, options)
        rows_copied += len(rows)
        batch_number += 1
        last_key = batch_key if batch_key is not None else last_key

        if checkpoint and last_key is not None:
            checkpoint.save(last_key=last_key, rows_copied=rows_copied, table=target.table)

        if options.progress:
            options.progress(
                Progress(
                    batch=batch_number,
                    rows_in_batch=len(rows),
                    rows_copied=rows_copied,
                    elapsed_seconds=time.monotonic() - started,
                    last_key=last_key,
                )
            )

        if options.max_batches and batch_number >= options.max_batches:
            complete = False
            break

    if checkpoint and complete:
        checkpoint.clear()

    return CopyResult(
        rows_copied=rows_copied,
        batches=batch_number,
        elapsed_seconds=time.monotonic() - started,
        columns=columns,
        last_key=last_key,
        resumed_from=resumed_from,
        complete=complete,
    )
