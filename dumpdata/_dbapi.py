"""Small helpers that smooth over DB-API driver differences."""

from __future__ import annotations

from typing import Any, Sequence


def execute(cursor, sql: str, params: Sequence[Any] = ()):
    """Execute with parameters only when there are any.

    Some drivers reject an empty parameter sequence for a statement that has no
    placeholders, so the two cases have to be kept apart.
    """
    if params:
        return cursor.execute(sql, tuple(params))
    return cursor.execute(sql)


def close_quietly(cursor) -> None:
    try:
        cursor.close()
    except Exception:  # pragma: no cover - drivers disagree about double close
        pass
