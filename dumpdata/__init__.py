"""Dump the result of a large, slow query into a table.

Three routes, fastest first:

1. Same database -- :mod:`dumpdata.serverside` builds the
   ``CREATE TABLE ... AS SELECT`` / ``INSERT ... SELECT`` that keeps every row
   inside the engine.
2. Different databases, bulk load available -- :mod:`dumpdata.csvexport`
   streams to CSV and prints the native loader command.
3. Different databases, row-by-row -- :func:`dumpdata.copy_query_to_table`
   moves batches over a DB-API connection with checkpointing and resume.

All three keep the column names the query produced, renaming only the
duplicates a multi-table join inevitably returns.
"""

from .columns import (
    Column,
    DuplicateColumnError,
    create_table_sql,
    dedupe_column_names,
    describe_columns,
    infer_logical_type,
    insert_sql,
)
from .copier import (
    Checkpoint,
    CopyOptions,
    CopyResult,
    Progress,
    Source,
    Target,
    build_keyset_query,
    copy_query_to_table,
)
from .csvexport import ExportResult, bulk_load_command, export_query_to_csv
from .dialects import Dialect, dialect_names, get_dialect
from .serverside import (
    aliased_select_list,
    check_duplicate_columns,
    ctas_sql,
    insert_select_sql,
    result_columns,
    rewrite_for_ctas,
)

__version__ = "0.1.0"

__all__ = [
    "Checkpoint",
    "Column",
    "CopyOptions",
    "CopyResult",
    "Dialect",
    "DuplicateColumnError",
    "ExportResult",
    "Progress",
    "Source",
    "Target",
    "aliased_select_list",
    "build_keyset_query",
    "bulk_load_command",
    "check_duplicate_columns",
    "copy_query_to_table",
    "create_table_sql",
    "ctas_sql",
    "dedupe_column_names",
    "describe_columns",
    "dialect_names",
    "export_query_to_csv",
    "get_dialect",
    "infer_logical_type",
    "insert_select_sql",
    "result_columns",
    "rewrite_for_ctas",
]
