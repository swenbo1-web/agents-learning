"""Bounded retries for derived work projections; original events remain authoritative."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

from .work_files import checkout_key


def install(runtime):
    runtime.db.execute("CREATE TABLE IF NOT EXISTS work_capture_pending("
        "project TEXT NOT NULL,event_id TEXT NOT NULL,checkout_key TEXT NOT NULL,workspace TEXT NOT NULL,"
        "capture_digest TEXT NOT NULL,capture TEXT NOT NULL,created REAL NOT NULL,"
        "attempts INTEGER NOT NULL DEFAULT 0,next_try REAL NOT NULL DEFAULT 0,last_error TEXT,"
        "PRIMARY KEY(project,event_id))")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS work_capture_due ON work_capture_pending(project,next_try,created)")


def register(runtime, project, event_id, workspace, raw=None, *, pending=True):
    """Retain trusted checkout and bounded public input outside projection rollback."""
    from .runtime import ContractError, clean, digest, encode
    if not runtime.config["work_memory"]["enabled"]:
        return False
    event = runtime.db.execute("SELECT type,payload,capture_digest FROM events WHERE project=? AND id=?",
                               (project, event_id)).fetchone()
    if not event or event["type"] not in {"PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit", "PublicMessageSegment"}:
        return False
    key = checkout_key(workspace)
    previous = runtime.db.execute("SELECT checkout_key FROM work_event_checkouts WHERE project=? AND event_id=?",
                                   (project, event_id)).fetchone()
    if previous and previous[0] != key:
        raise ContractError("work capture checkout changed")
    runtime.db.execute("INSERT OR IGNORE INTO work_event_checkouts VALUES(?,?,?)", (project, event_id, key))
    if not pending:
        return True
    capture = {}
    field = {"Stop": "last_assistant_message", "UserPromptSubmit": "prompt"}.get(event["type"])
    if field and isinstance(raw, dict) and isinstance(raw.get(field), str):
        text = clean(raw[field])
        capture[field] = text[:65536]
        for name in ("completion", "capture_coverage", field + "_truncated"):
            if name in raw:
                capture[name] = clean(raw[name])
        if len(text) > 65536:
            capture[field + "_truncated"] = True
        if len(encode(capture)) > 100000:
            capture = {field: text[:65536], field + "_truncated": True}
    runtime.db.execute("INSERT OR IGNORE INTO work_capture_pending("
        "project,event_id,checkout_key,workspace,capture_digest,capture,created) VALUES(?,?,?,?,?,?,?)",
        (project, event_id, key, str(Path(workspace).resolve()),
         event["capture_digest"] or digest(json.loads(event["payload"])), encode(capture), time.time()))
    return True


def complete(runtime, project, event_id):
    runtime.db.execute("DELETE FROM work_capture_pending WHERE project=? AND event_id=?", (project, event_id))


def failed(runtime, project, event_id, error):
    # Store a stable error class, never a raw tool output or private exception text.
    runtime.db.execute("UPDATE work_capture_pending SET attempts=attempts+1,"
        "next_try=?+min(300,2 << min(attempts,7)),last_error=? WHERE project=? AND event_id=?",
        (time.time(), type(error).__name__, project, event_id))


def recover(runtime, project, *, exclude_event=None, event_id=None, limit=2):
    """Writer-only bounded repair. It indexes original evidence, granting no authority."""
    from .runtime import ContractError, digest
    if not runtime.config["work_memory"]["enabled"]:
        return 0
    if not runtime.db.in_transaction:
        raise ContractError("work recovery requires an existing write transaction")
    rows = runtime.db.execute("SELECT * FROM work_capture_pending WHERE project=? AND next_try<=? "
        "AND (? IS NULL OR event_id!=?) AND (? IS NULL OR event_id=?) ORDER BY next_try,created,event_id LIMIT ?",
        (project, time.time(), exclude_event, exclude_event, event_id, event_id, limit)).fetchall()
    repaired = 0
    for job in rows:
        runtime.db.execute("SAVEPOINT work_projection_retry")
        try:
            event = runtime.db.execute("SELECT payload,capture_digest FROM events WHERE project=? AND id=?",
                                       (project, job["event_id"])).fetchone()
            if not event or (event["capture_digest"] or digest(json.loads(event["payload"]))) != job["capture_digest"]:
                raise ContractError("work recovery source changed")
            if checkout_key(job["workspace"]) != job["checkout_key"]:
                raise ContractError("work recovery checkout changed")
            raw = json.loads(event["payload"])
            raw.update(json.loads(job["capture"]))
            runtime._work_capture_event(project, job["event_id"], job["workspace"], raw=raw)
            complete(runtime, project, job["event_id"])
        except (OSError, ValueError, TypeError, sqlite3.Error, ContractError) as exc:
            runtime.db.execute("ROLLBACK TO work_projection_retry")
            runtime.db.execute("RELEASE work_projection_retry")
            failed(runtime, project, job["event_id"], exc)
        else:
            runtime.db.execute("RELEASE work_projection_retry")
            repaired += 1
    return repaired
