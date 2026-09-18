"""Command line front end: ``python -m dumpdata <command>``.

Commands
--------
``connect``  open a session to Oracle and print who you connected as
``columns``  probe a query, report repeated column names, print a select list
             that aliases them apart
``plan``     print the same-database CTAS / INSERT ... SELECT to run
``copy``     move rows between two DB-API connections, in resumable batches
``csv``      export to CSV and print the target's bulk-load command
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys

from .copier import CopyOptions, Source, Target, copy_query_to_table
from .csvexport import bulk_load_command, export_query_to_csv
from .dialects import dialect_names, get_dialect
from .oracle import (
    OracleConnectError,
    connect_with_fallback,
    format_probe,
    probe_session,
    target_from_inputs,
)
from .serverside import (
    aliased_select_list,
    check_duplicate_columns,
    ctas_sql,
    insert_select_sql,
    result_columns,
    rewrite_for_ctas,
)


def connect(driver: str, dsn: str):
    """Open a DB-API connection.

    ``dsn`` is passed positionally, or expanded as keyword arguments when it
    looks like a JSON object::

        --source-driver sqlite3   --source-dsn warehouse.db
        --source-driver psycopg2  --source-dsn "host=db user=me dbname=sales"
        --source-driver pymysql   --source-dsn '{"host": "db", "user": "me"}'
    """
    module = importlib.import_module(driver)
    text = dsn.strip()
    if text.startswith("{"):
        return module.connect(**json.loads(text))
    return module.connect(text)


def cmd_connect(args) -> int:
    """Open Oracle and print session identity. Does not dump any data."""
    try:
        target = target_from_inputs(
            user=args.user,
            password=args.password,
            host=args.host,
            port=args.port,
            service=args.service,
            sid=args.sid,
            timeout=args.timeout,
        )
        connection, used = connect_with_fallback(target)
        try:
            info = probe_session(connection)
        finally:
            connection.close()
    except OracleConnectError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(format_probe(used, info))
    return 0


def read_query(value: str) -> str:
    """A query given inline, or ``@path/to/query.sql``."""
    if value.startswith("@"):
        with open(value[1:], "r", encoding="utf-8") as handle:
            return handle.read()
    return value


def _print_progress(progress) -> None:
    print(f"  {progress}", file=sys.stderr, flush=True)


def cmd_columns(args) -> int:
    query = read_query(args.query)
    connection = connect(args.driver, args.dsn)
    try:
        names = result_columns(connection, query, args.dialect)
    finally:
        connection.close()

    print(f"{len(names)} columns: {', '.join(names)}\n")
    duplicates = check_duplicate_columns(names, args.dialect)
    if not duplicates:
        print("No repeated column names; this query can be dumped as-is.")
        return 0
    print("Repeated column names (a table cannot hold these as-is):")
    for name, positions in sorted(duplicates.items()):
        print(f"  {name}: positions {positions}")
    print("\nSelect list that keeps every column and aliases the duplicates apart:\n")
    print(f"SELECT {aliased_select_list(names, args.dialect)}")
    return 0


def cmd_plan(args) -> int:
    query = read_query(args.query)
    names = None
    if args.dsn:
        connection = connect(args.driver, args.dsn)
        try:
            names = result_columns(connection, query, args.dialect)
        finally:
            connection.close()

    if args.mode == "insert":
        print(insert_select_sql(
            args.table, query, args.dialect, schema=args.schema, direct_path=args.direct_path
        ))
        return 0

    kwargs = {}
    if args.unlogged:
        kwargs["unlogged"] = True
    if args.nologging:
        kwargs["nologging"] = True
    if args.parallel:
        kwargs["parallel"] = args.parallel

    duplicates = check_duplicate_columns(names, args.dialect) if names else {}
    if duplicates:
        print(f"-- repeated column names aliased apart: {', '.join(sorted(duplicates))}")
        print(rewrite_for_ctas(
            args.table, query, names, args.dialect, schema=args.schema, **kwargs
        ))
    else:
        print(ctas_sql(args.table, query, args.dialect, schema=args.schema, **kwargs))
    return 0


def cmd_copy(args) -> int:
    query = read_query(args.query)
    source_connection = connect(args.source_driver, args.source_dsn)
    target_connection = connect(args.target_driver, args.target_dsn)
    try:
        source = Source(
            connection=source_connection,
            query=query,
            dialect=args.source_dialect,
            arraysize=args.batch_size,
        )
        target = Target(
            connection=target_connection,
            table=args.table,
            schema=args.schema,
            dialect=args.target_dialect,
        )
        options = CopyOptions(
            mode=args.mode,
            batch_size=args.batch_size,
            key_column=args.key_column,
            create_target=not args.no_create,
            truncate_target=args.truncate,
            on_duplicate_columns="error" if args.strict_columns else "suffix",
            checkpoint_path=args.checkpoint,
            max_batches=args.max_batches,
            progress=None if args.quiet else _print_progress,
        )
        result = copy_query_to_table(source, target, options)
    finally:
        source_connection.close()
        target_connection.close()

    renamed = result.renamed_columns
    if renamed:
        print("renamed duplicate columns: " + ", ".join(
            f"{column.source_name} -> {column.name}" for column in renamed
        ))
    print(
        f"copied {result.rows_copied:,} rows in {result.batches:,} batches, "
        f"{result.elapsed_seconds:,.1f}s ({result.rows_per_second:,.0f} rows/s)"
    )
    if not result.complete:
        print(f"stopped early at key {result.last_key!r}; rerun to continue")
    return 0


def cmd_csv(args) -> int:
    query = read_query(args.query)
    connection = connect(args.driver, args.dsn)
    try:
        source = Source(
            connection=connection,
            query=query,
            dialect=args.dialect,
            arraysize=args.batch_size,
        )
        result = export_query_to_csv(
            source,
            args.out,
            batch_size=args.batch_size,
            delimiter=args.delimiter,
            null_string=args.null_string,
            compress=args.gzip,
            max_rows_per_file=args.rows_per_file,
            progress=None if args.quiet else _print_progress,
        )
    finally:
        connection.close()

    print(
        f"exported {result.rows:,} rows to {len(result.files)} file(s) in "
        f"{result.elapsed_seconds:,.1f}s ({result.rows_per_second:,.0f} rows/s)"
    )
    for path in result.files:
        print(f"  {path}")
    if args.table:
        print("\nLoad it with:\n")
        print(bulk_load_command(
            args.load_dialect or args.dialect,
            args.table,
            result.files[0],
            delimiter=args.delimiter,
            null_string=args.null_string,
        ))
    return 0


def build_parser() -> argparse.ArgumentParser:
    dialects = ", ".join(dialect_names())
    parser = argparse.ArgumentParser(
        prog="dumpdata",
        description="Dump the result of a large query into a table.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    connect_command = subparsers.add_parser(
        "connect",
        help="open Oracle (defaults: P10_DEMO @ 10.0.0.18:1532/Qc)",
    )
    connect_command.add_argument("--user", help="defaults to P10_DEMO or ORACLE_USER")
    connect_command.add_argument(
        "--password",
        help="or set ORACLE_PASSWORD; never pass a password on a shared command line if you can avoid it",
    )
    connect_command.add_argument("--host", help="defaults to 10.0.0.18 or ORACLE_HOST")
    connect_command.add_argument("--port", type=int, help="defaults to 1532 or ORACLE_PORT")
    connect_command.add_argument(
        "--service",
        help="Easy Connect service name (tried first). Defaults to Qc",
    )
    connect_command.add_argument(
        "--sid",
        help="SID connect descriptor. If omitted, Qc is retried as a SID after the service name",
    )
    connect_command.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="TCP connect timeout in seconds (default 10)",
    )
    connect_command.set_defaults(func=cmd_connect)

    columns = subparsers.add_parser("columns", help="report repeated column names")
    columns.add_argument("--driver", required=True, help="DB-API module, e.g. psycopg2")
    columns.add_argument("--dsn", required=True)
    columns.add_argument("--dialect", required=True, help=dialects)
    columns.add_argument("--query", required=True, help="SQL, or @file.sql")
    columns.set_defaults(func=cmd_columns)

    plan = subparsers.add_parser("plan", help="print the same-database load statement")
    plan.add_argument("--dialect", required=True, help=dialects)
    plan.add_argument("--query", required=True, help="SQL, or @file.sql")
    plan.add_argument("--table", required=True)
    plan.add_argument("--schema")
    plan.add_argument("--mode", choices=("ctas", "insert"), default="ctas")
    plan.add_argument("--driver", help="probe the query for repeated column names")
    plan.add_argument("--dsn", help="probe the query for repeated column names")
    plan.add_argument("--unlogged", action="store_true", help="PostgreSQL UNLOGGED table")
    plan.add_argument("--nologging", action="store_true", help="Oracle NOLOGGING")
    plan.add_argument("--parallel", type=int, help="Oracle PARALLEL degree")
    plan.add_argument("--direct-path", action="store_true", help="APPEND / TABLOCK hint")
    plan.set_defaults(func=cmd_plan)

    copy = subparsers.add_parser("copy", help="copy rows between two connections")
    copy.add_argument("--source-driver", required=True)
    copy.add_argument("--source-dsn", required=True)
    copy.add_argument("--source-dialect", required=True, help=dialects)
    copy.add_argument("--target-driver", required=True)
    copy.add_argument("--target-dsn", required=True)
    copy.add_argument("--target-dialect", required=True, help=dialects)
    copy.add_argument("--query", required=True, help="SQL, or @file.sql")
    copy.add_argument("--table", required=True)
    copy.add_argument("--schema")
    copy.add_argument("--mode", choices=("stream", "keyset"), default="stream")
    copy.add_argument("--key-column", help="unique sortable column, required for keyset mode")
    copy.add_argument("--batch-size", type=int, default=20_000)
    copy.add_argument("--checkpoint", help="checkpoint file; enables resume (keyset mode)")
    copy.add_argument("--max-batches", type=int, help="stop early, for a trial run")
    copy.add_argument("--no-create", action="store_true", help="target table already exists")
    copy.add_argument("--truncate", action="store_true", help="empty the target first")
    copy.add_argument(
        "--strict-columns",
        action="store_true",
        help="fail on repeated column names instead of renaming them",
    )
    copy.add_argument("--quiet", action="store_true")
    copy.set_defaults(func=cmd_copy)

    csv_command = subparsers.add_parser("csv", help="export to CSV for a bulk loader")
    csv_command.add_argument("--driver", required=True)
    csv_command.add_argument("--dsn", required=True)
    csv_command.add_argument("--dialect", required=True, help=dialects)
    csv_command.add_argument("--query", required=True, help="SQL, or @file.sql")
    csv_command.add_argument("--out", required=True, help="output path")
    csv_command.add_argument("--batch-size", type=int, default=20_000)
    csv_command.add_argument("--delimiter", default=",")
    csv_command.add_argument("--null-string", default="")
    csv_command.add_argument("--gzip", action="store_true")
    csv_command.add_argument("--rows-per-file", type=int, help="split for parallel loading")
    csv_command.add_argument("--table", help="print the loader command for this table")
    csv_command.add_argument("--load-dialect", help="target engine, if it differs from --dialect")
    csv_command.add_argument("--quiet", action="store_true")
    csv_command.set_defaults(func=cmd_csv)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "dialect", None):
        get_dialect(args.dialect)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
