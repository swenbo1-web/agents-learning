"""Narrow SQLite contention rules; never interpret other failures as retryable."""
from __future__ import annotations

from contextlib import contextmanager
import sqlite3


def is_busy_error(error):
    """Recognize only SQLite BUSY/LOCKED, including Python 3.10 exceptions."""
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 255 in {5, 6}
    return str(error).lower().startswith((
        "database is locked", "database table is locked", "database schema is locked"))


def knowledge_busy_response(operation, error):
    """One public read-error contract for MCP and the native DSH adapter."""
    if operation not in {"knowledge_search", "knowledge_read"} or not is_busy_error(error):
        return None
    return {"status": "error", "error": "database_busy", "retryable": True,
            "operation": operation,
            "message": "Local knowledge storage is busy. Retry this read after the current write finishes."}


@contextmanager
def busy_timeout(database, milliseconds):
    """Temporarily change this connection's wait without changing its transaction."""
    if type(milliseconds) is not int or milliseconds < 0:
        raise ValueError("busy timeout must be a nonnegative integer")
    previous = database.execute("PRAGMA busy_timeout").fetchone()[0]
    database.execute(f"PRAGMA busy_timeout={milliseconds}")
    try:
        yield
    finally:
        database.execute(f"PRAGMA busy_timeout={int(previous)}")
