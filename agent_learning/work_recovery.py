"""Bounded retries for derived work projections; original events remain authoritative."""
from __future__ import annotations

import json
import os
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


def _capture_digest(event):
    from .runtime import digest
    return event["capture_digest"] or digest(json.loads(event["payload"]))


def projection_links(event, key, projection):
    """Accept only internal bounded observations bound to the original evidence."""
    from .runtime import ContractError
    if (not isinstance(projection, dict) or projection.get("version") != 1
            or projection.get("capture_digest") != _capture_digest(event)
            or projection.get("checkout_key") != key or type(projection.get("complete")) is not bool
            or not isinstance(projection.get("links"), list) or len(projection["links"]) > 16):
        raise ContractError("work projection identity changed")
    for link in projection["links"]:
        if not isinstance(link, dict):
            raise ContractError("invalid work projection link")
        path = link.get("relative_path")
        if (not isinstance(path, str) or not path or len(path) > 2000 or "\\" in path or "\0" in path
                or Path(path).is_absolute() or any(part in {"", ".", ".."} for part in path.split("/"))
                or link.get("path_key") != os.path.normcase(path) or link.get("checkout_key") != key
                or link.get("relation") not in {"read_target", "write_target", "observed_target"}
                or not isinstance(link.get("version"), dict)):
            raise ContractError("invalid work projection link")
    return projection["links"]


def legacy_projection(runtime, project, event, workspace, *, raw=None):
    """Missing original file metadata stays unknown; recovery never samples T2 as T1."""
    return {"version": 1, "checkout_key": checkout_key(workspace), "capture_digest": _capture_digest(event),
            "links": runtime._work_file_projection(project, event, workspace, raw=raw, observe_version=False),
            "complete": event["type"] in {"Stop", "UserPromptSubmit", "PublicMessageSegment"}}


def register(runtime, project, event_id, workspace, raw=None, *, pending=True, projection=None):
    """Retain trusted checkout and bounded public input outside projection rollback."""
    from .runtime import ContractError, clean, encode
    if not runtime.config["work_memory"]["enabled"]:
        return None
    event = runtime.db.execute("SELECT * FROM events WHERE project=? AND id=?",
                               (project, event_id)).fetchone()
    if not event or event["type"] not in {"PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit", "PublicMessageSegment"}:
        return None
    event = dict(event)
    key = checkout_key(workspace)
    previous = runtime.db.execute("SELECT checkout_key FROM work_event_checkouts WHERE project=? AND event_id=?",
                                   (project, event_id)).fetchone()
    if previous and previous[0] != key:
        raise ContractError("work capture checkout changed")
    runtime.db.execute("INSERT OR IGNORE INTO work_event_checkouts VALUES(?,?,?)", (project, event_id, key))
    if projection is None:
        projection = {"version": 1, "checkout_key": key, "capture_digest": _capture_digest(event), "complete": True,
                      "links": runtime._work_file_projection(project, event, workspace, raw=raw)}
    projection_links(event, key, projection)
    if not pending:
        return projection
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
    # The bounded plan contains paths and observed metadata, never tool text,
    # credentials or permission. Preserve it outside the projection savepoint.
    capture["file_projection"] = projection
    if len(encode(capture)) > 150000:
        # Preserve original file observations first. Escaping a public string
        # may need more storage than its character count suggests.
        if field and isinstance(capture.get(field), str):
            text = capture[field]
            capture[field + "_truncated"] = True
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                capture[field] = text[:middle]
                if len(encode(capture)) <= 150000:
                    low = middle
                else:
                    high = middle - 1
            capture[field] = text[:low]
        if len(encode(capture)) > 150000:
            raise ContractError("work recovery capture exceeds bounded storage")
    runtime.db.execute("INSERT OR IGNORE INTO work_capture_pending("
        "project,event_id,checkout_key,workspace,capture_digest,capture,created) VALUES(?,?,?,?,?,?,?)",
        (project, event_id, key, str(Path(workspace).resolve()),
         _capture_digest(event), encode(capture), time.time()))
    return projection


def complete(runtime, project, event_id):
    runtime.db.execute("DELETE FROM work_capture_pending WHERE project=? AND event_id=?", (project, event_id))


def failed(runtime, project, event_id, error):
    # Store a stable error class, never a raw tool output or private exception text.
    runtime.db.execute("UPDATE work_capture_pending SET attempts=attempts+1,"
        "next_try=?+min(300,2 << min(attempts,7)),last_error=? WHERE project=? AND event_id=?",
        (time.time(), type(error).__name__, project, event_id))


def restore(runtime, project, event_id, workspace, *, raw=None, reindex=False):
    """Both retry queues confirm the same projection in their existing transaction."""
    from .runtime import ContractError
    if not runtime.db.in_transaction:
        raise ContractError("work restoration requires an existing write transaction")
    event = runtime.db.execute("SELECT * FROM events WHERE project=? AND id=?", (project, event_id)).fetchone()
    if event is None:
        raise ContractError("work recovery source missing")
    event = dict(event)
    job = runtime.db.execute("SELECT * FROM work_capture_pending WHERE project=? AND event_id=?",
                             (project, event_id)).fetchone()
    capture = {}
    if job:
        if _capture_digest(event) != job["capture_digest"]:
            raise ContractError("work recovery source changed")
        if checkout_key(job["workspace"]) != job["checkout_key"]:
            raise ContractError("work recovery checkout changed")
        workspace, capture = job["workspace"], json.loads(job["capture"])
    key = checkout_key(workspace)
    recorded = runtime.db.execute("SELECT checkout_key FROM work_event_checkouts WHERE project=? AND event_id=?",
                                   (project, event_id)).fetchone()
    if recorded and recorded[0] != key:
        raise ContractError("work recovery checkout changed")
    existing = runtime.db.execute("SELECT id,source_hash,workspace_key,body FROM work_memories WHERE project=? AND event_id=?",
                                   (project, event_id)).fetchone()
    if existing and existing["source_hash"] == _capture_digest(event):
        if existing["workspace_key"] != key:
            raise ContractError("work recovery checkout changed")
        if reindex:
            links = [dict(row) for row in runtime.db.execute(
                "SELECT relative_path FROM work_file_links WHERE project=? AND event_id=?", (project, event_id))]
            runtime._work_index(project, existing["id"], json.loads(existing["body"]), links)
        complete(runtime, project, event_id)
        return {"memory_id": existing["id"], "indexed": False, "already_captured": True, "reindexed": reindex}
    if not runtime.config["work_memory"]["enabled"]:
        return None
    observed = json.loads(event["payload"])
    if isinstance(raw, dict):
        # Only trusted public capture is supplied by the phase recovery queue.
        observed.update({key: value for key, value in raw.items() if key in {
            "last_assistant_message", "prompt", "completion", "capture_coverage",
            "last_assistant_message_truncated", "prompt_truncated", "truncated"}})
    observed.update({key: value for key, value in capture.items() if key != "file_projection"})
    projection = capture.get("file_projection")
    if projection is None:
        projection = legacy_projection(runtime, project, event, workspace, raw=observed)
    projection_links(event, checkout_key(workspace), projection)
    result = runtime._work_capture_event(project, event_id, workspace, raw=observed, _file_projection=projection)
    complete(runtime, project, event_id)
    return result


def recover(runtime, project, *, exclude_event=None, event_id=None, limit=2):
    """Writer-only bounded repair. It indexes original evidence, granting no authority."""
    from .runtime import ContractError
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
            restore(runtime, project, job["event_id"], job["workspace"])
        except (OSError, ValueError, TypeError, sqlite3.Error, ContractError) as exc:
            runtime.db.execute("ROLLBACK TO work_projection_retry")
            runtime.db.execute("RELEASE work_projection_retry")
            failed(runtime, project, job["event_id"], exc)
        else:
            runtime.db.execute("RELEASE work_projection_retry")
            repaired += 1
    return repaired
