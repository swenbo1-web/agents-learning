"""Disposable initialization receipt; authoritative records and schema stay unchanged."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
import time


@contextmanager
def initialization_guard(runtime, timeout=1.5):
    """Serialize bootstrap across processes; warm opens never need this lock."""
    from .software_updates import _locked
    deadline = time.monotonic() + timeout
    while True:
        with _locked(runtime.home / ".runtime-initialization.lock") as acquired:
            if acquired:
                yield
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise sqlite3.OperationalError("runtime initialization busy; retry")
        time.sleep(min(.02, remaining))


def _signature():
    from . import __version__
    from .knowledge_index import INDEX_VERSION
    from .work_memory import WORK_INDEX_VERSION
    return f"{__version__}:schema24:knowledge{INDEX_VERSION}:work{WORK_INDEX_VERSION}:startup9"


def storage_ready(runtime):
    """Check one read snapshot; an external schema/index change invalidates the receipt."""
    runtime.db.execute("BEGIN")
    try:
        if runtime.db.execute("PRAGMA user_version").fetchone()[0] != 24:
            return False
        receipt = runtime.db.execute(
            "SELECT signature,schema_cookie FROM runtime_storage_receipt WHERE id=1").fetchone()
        if not receipt or receipt[0] != _signature():
            return False
        if receipt[1] != runtime.db.execute("PRAGMA schema_version").fetchone()[0]:
            return False
        from .knowledge_index import INDEX_VERSION
        index = runtime.db.execute("SELECT version FROM knowledge_index_state WHERE id=1").fetchone()
        return bool(index and index[0] == INDEX_VERSION)
    except sqlite3.OperationalError as exc:
        # A missing or old disposable table is a cache miss, never proof that
        # initialization succeeded. Other database failures still reach callers.
        if "no such table" not in str(exc).lower() and "no such column" not in str(exc).lower():
            raise
        return False
    finally:
        runtime.db.rollback()


def ensure_host_scope(runtime):
    scope = runtime._session_scope()
    if runtime.db.execute("SELECT 1 FROM host_scopes WHERE scope=?", (scope,)).fetchone():
        return
    # First use of a new host really needs a write; ordinary opens don't.
    with runtime.transaction():
        runtime.db.execute("INSERT OR IGNORE INTO host_scopes(scope,epoch) "
                           "SELECT ?,epoch FROM host_state WHERE id=1", (scope,))


def mark_storage_ready(runtime):
    if not runtime.db.in_transaction:
        raise RuntimeError("initialization receipt requires a write transaction")
    columns = [(row[1], row[2].upper(), row[5]) for row in
               runtime.db.execute("PRAGMA table_info(runtime_storage_receipt)")]
    expected = [("id", "INTEGER", 1), ("signature", "TEXT", 0), ("schema_cookie", "INTEGER", 0)]
    if columns and columns != expected:
        # Only this disposable cache belongs to us. Never rebuild authority
        # tables or fabricate epochs as a side effect of receipt recovery.
        runtime.db.execute("DROP TABLE runtime_storage_receipt")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS runtime_storage_receipt("
                       "id INTEGER PRIMARY KEY CHECK(id=1),signature TEXT NOT NULL,schema_cookie INTEGER NOT NULL)")
    cookie = runtime.db.execute("PRAGMA schema_version").fetchone()[0]
    runtime.db.execute("INSERT INTO runtime_storage_receipt VALUES(1,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET signature=excluded.signature,schema_cookie=excluded.schema_cookie",
                       (_signature(), cookie))
