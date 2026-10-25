"""Durable local completion of one original Stop, without model execution.

This queue preserves the stage boundary when optional work-history capture or
batch preparation fails. It cannot borrow a later task or account's permission.
"""
from __future__ import annotations

import json
import time


def install(runtime):
    runtime.db.execute("CREATE TABLE IF NOT EXISTS phase_seal_requests("
        "phase_id TEXT PRIMARY KEY,project TEXT NOT NULL,host_scope TEXT NOT NULL,"
        "session_epoch INTEGER NOT NULL,governance_epoch INTEGER NOT NULL,event_id TEXT NOT NULL,"
        "workspace TEXT NOT NULL,capture TEXT NOT NULL,created REAL NOT NULL,"
        "attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,next_try REAL NOT NULL DEFAULT 0)")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS phase_seal_due ON phase_seal_requests("
                       "project,host_scope,session_epoch,governance_epoch,next_try,created)")


def request(runtime, project, event_id, workspace, conversation, raw=None):
    from .phase_review import scope
    from .runtime import clean, encode
    if scope(runtime, project) is None:
        return None
    event = runtime.db.execute("SELECT * FROM events WHERE project=? AND id=? AND conversation=? "
        "AND type='Stop' AND agent IS NULL", (project, event_id, conversation)).fetchone()
    if not event or not event["task_id"] or event["task_status"] != "delivered":
        return None
    phase = runtime.db.execute("SELECT * FROM review_phases WHERE project=? AND host_scope=? AND conversation=? "
        "AND task_id=? AND task_revision=? AND delivery_version=? AND state='awaiting_stop' "
        "AND session_epoch=? AND governance_epoch=? AND start_time<=?", (project, runtime._session_scope(),
        conversation, event["task_id"], event["task_revision"], event["task_write_version"],
        runtime.session_epoch(), runtime.host_epoch(), event["created"])).fetchone()
    if not phase:
        return None
    payload = json.loads(event["payload"])
    capture = {key: payload[key] for key in ("last_assistant_message", "completion", "capture_coverage",
               "last_assistant_message_truncated", "truncated") if key in payload}
    # Match the existing bounded public segmentation limit. Keep no private
    # reasoning, arbitrary tool output or extra host configuration in this queue.
    if isinstance(raw, dict) and isinstance(raw.get("last_assistant_message"), str):
        text = clean(raw["last_assistant_message"])
        capture["last_assistant_message"] = text[:65536]
        for key in ("completion", "capture_coverage", "last_assistant_message_truncated"):
            if key in raw:
                capture[key] = clean(raw[key])
        if len(text) > 65536:
            capture["last_assistant_message_truncated"] = True
    serialized = encode(capture)
    if len(serialized) > 100000:
        capture = {"last_assistant_message": capture.get("last_assistant_message", "")[:65536],
                   "last_assistant_message_truncated": True}
    runtime.db.execute("INSERT OR IGNORE INTO phase_seal_requests(phase_id,project,host_scope,session_epoch,"
        "governance_epoch,event_id,workspace,capture,created) VALUES(?,?,?,?,?,?,?,?,?)",
        (phase["id"], project, runtime._session_scope(), runtime.session_epoch(), runtime.host_epoch(),
         event_id, str(workspace), encode(capture), time.time()))
    return dict(runtime.db.execute("SELECT * FROM phase_seal_requests WHERE phase_id=?", (phase["id"],)).fetchone())


def pinned_event(runtime, project, phase_id, fallback):
    row = runtime.db.execute("SELECT event_id FROM phase_seal_requests WHERE project=? AND phase_id=? "
        "AND host_scope=? AND session_epoch=? AND governance_epoch=?", (project, phase_id,
        runtime._session_scope(), runtime.session_epoch(), runtime.host_epoch())).fetchone()
    return row[0] if row else fallback


def complete(runtime, project, phase_id, event_id):
    runtime.db.execute("DELETE FROM phase_seal_requests WHERE project=? AND phase_id=? AND event_id=? "
        "AND EXISTS(SELECT 1 FROM review_phases p WHERE p.id=phase_seal_requests.phase_id "
        "AND p.project=? AND p.state!='awaiting_stop')",
        (project, phase_id, event_id, project))


def recover(runtime, project, *, limit=8, force=False):
    """Writer entry only. Retry original capture and seal; never call prepare."""
    from .phase_review import seal_phase, scope
    from .runtime import ContractError
    import sqlite3
    if not runtime.db.in_transaction:
        raise ContractError("stage recovery requires a writer transaction")
    if scope(runtime, project) is None:
        return
    # A pre-savepoint write can itself fail. Reconstruct only an exact original
    # Stop bound to the delivered write version, never the latest conversation.
    missing = runtime.db.execute("SELECT p.*,e.id original_stop FROM review_phases p JOIN events e "
        "ON e.project=p.project AND e.conversation=p.conversation AND e.task_id=p.task_id "
        "AND e.task_revision=p.task_revision AND e.task_write_version=p.delivery_version "
        "AND e.task_status='delivered' AND e.type='Stop' AND e.agent IS NULL AND e.created>=p.start_time "
        "WHERE p.project=? AND p.host_scope=? AND p.session_epoch=? AND p.governance_epoch=? "
        "AND p.state='awaiting_stop' AND NOT EXISTS(SELECT 1 FROM phase_seal_requests q WHERE q.phase_id=p.id) "
        "ORDER BY p.created,e.created,e.rowid LIMIT ?", (project, runtime._session_scope(),
        runtime.session_epoch(), runtime.host_epoch(), limit)).fetchall()
    for phase in missing:
        path = runtime.db.execute("SELECT path FROM project_paths WHERE project=? ORDER BY length(path),path LIMIT 1",
                                  (project,)).fetchone()
        if path:
            request(runtime, project, phase["original_stop"], path[0], phase["conversation"])
    now = time.time()
    rows = runtime.db.execute("SELECT q.* FROM phase_seal_requests q JOIN review_phases p ON p.id=q.phase_id "
        "AND p.project=q.project WHERE q.project=? AND q.host_scope=? AND q.session_epoch=? AND q.governance_epoch=? "
        "AND p.state='awaiting_stop' AND (? OR q.next_try<=?) ORDER BY q.next_try,q.created LIMIT ?",
        (project, runtime._session_scope(), runtime.session_epoch(), runtime.host_epoch(), int(force), now, limit)).fetchall()
    for row in rows:
        runtime.db.execute("SAVEPOINT phase_seal_retry")
        try:
            from .work_recovery import restore
            restored = restore(runtime, project, row["event_id"], row["workspace"], raw=json.loads(row["capture"]))
            if restored["status"] == "deferred":
                # Rotate disabled captures behind other due requests without
                # classifying the user's disabled channel as a local failure.
                runtime.db.execute("UPDATE phase_seal_requests SET next_try=? WHERE phase_id=?",
                                   (now + 2, row["phase_id"]))
            else:
                phase = runtime.db.execute("SELECT * FROM review_phases WHERE project=? AND id=?",
                                           (project, row["phase_id"])).fetchone()
                if not seal_phase(runtime, project, phase, row["event_id"]):
                    raise ContractError("original stage boundary is unavailable")
                complete(runtime, project, row["phase_id"], row["event_id"])
        except (OSError, ValueError, TypeError, sqlite3.Error):
            runtime.db.execute("ROLLBACK TO phase_seal_retry")
            runtime.db.execute("RELEASE phase_seal_retry")
            # Classify the failure without persisting exception text or secrets.
            runtime.db.execute("UPDATE phase_seal_requests SET attempts=attempts+1,last_error='local_seal_failed',"
                "next_try=? WHERE phase_id=?", (now + min(60, 2 ** min(row["attempts"], 5)), row["phase_id"]))
        else:
            runtime.db.execute("RELEASE phase_seal_retry")
