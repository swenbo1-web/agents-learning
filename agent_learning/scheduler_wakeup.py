"""Durable local wake hints; never an execution or model-call permission."""
from __future__ import annotations

import time


def install(runtime):
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS scheduler_wake_requests(
        project TEXT NOT NULL,host_scope TEXT NOT NULL,session_epoch INTEGER NOT NULL,
        governance_epoch INTEGER NOT NULL,event_id TEXT NOT NULL,workspace TEXT NOT NULL,
        created REAL NOT NULL,next_try REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(project,host_scope))""")


def _enabled(runtime, project):
    cfg = runtime.config["scheduler"]
    if not cfg["enabled"]:
        return False
    from .admin_control import admission_enabled
    # A local hint only needs switches. Resolving executors here would repeat
    # filesystem/CLI checks inside every prompt and the read-only status view.
    return (cfg["auto_release"] or cfg["sync_enabled"]
        or runtime.admin_policy(project)["enabled"] and admission_enabled(runtime, project))


def request(runtime, project, event_id, workspace, conversation):
    """Register with the original Stop transaction, before optional projections."""
    if not _enabled(runtime, project):
        return
    event = runtime.db.execute("SELECT * FROM events WHERE project=? AND id=? AND conversation=? "
        "AND type='Stop' AND agent IS NULL", (project, event_id, conversation)).fetchone()
    if event is None or not event["task_id"] or event["task_status"] != "delivered":
        return
    # Fast handoff is for a genuinely armed delivery, not an ordinary reply or
    # a legacy queue probe whose executor may be unavailable. Capture/seal may
    # still fail after this point; the exact original boundary remains pinned.
    phase = runtime.db.execute("SELECT 1 FROM review_phases WHERE project=? AND host_scope=? AND conversation=? "
        "AND task_id=? AND task_revision=? AND delivery_version=? AND state='awaiting_stop' "
        "AND session_epoch=? AND governance_epoch=? AND start_time<=?", (project, runtime._session_scope(),
        conversation, event["task_id"], event["task_revision"], event["task_write_version"],
        runtime.session_epoch(), runtime.host_epoch(), event["created"])).fetchone()
    if phase is None:
        return
    runtime.db.execute("INSERT INTO scheduler_wake_requests VALUES(?,?,?,?,?,?,?,0,0) "
        "ON CONFLICT(project,host_scope) DO UPDATE SET session_epoch=excluded.session_epoch,"
        "governance_epoch=excluded.governance_epoch,event_id=excluded.event_id,workspace=excluded.workspace,"
        "created=excluded.created,next_try=0,attempts=0 WHERE event_id!=excluded.event_id",
        (project, runtime._session_scope(), runtime.session_epoch(), runtime.host_epoch(),
         event_id, str(workspace), time.time()))


def pending(runtime, project):
    if not _enabled(runtime, project):
        return None
    return runtime.db.execute("SELECT * FROM scheduler_wake_requests WHERE project=? AND host_scope=? "
        "AND session_epoch=? AND governance_epoch=?", (project, runtime._session_scope(),
        runtime.session_epoch(), runtime.host_epoch())).fetchone()


def reserve_attempt(runtime, project):
    """A finite cooldown also covers a launcher dying before worker acknowledgement."""
    row = pending(runtime, project)
    if row is None:
        return None
    now = time.time()
    if row["next_try"] > now:
        return False
    if runtime.db.execute("SELECT 1 FROM scheduler_worker WHERE host_epoch=? AND lease_until>?",
                          (runtime.host_epoch(), now)).fetchone():
        return False  # A busy lease is not a failed launch or a new retry cost.
    delay = min(300, 30 * 2 ** min(row["attempts"], 4))
    runtime.db.execute("UPDATE scheduler_wake_requests SET attempts=attempts+1,next_try=? "
        "WHERE project=? AND host_scope=? AND event_id=?", (now + delay, project,
        row["host_scope"], row["event_id"]))
    return True


def acknowledge(runtime, project):
    """Only a successful local refresh transaction covers this exact host boundary."""
    runtime.db.execute("DELETE FROM scheduler_wake_requests WHERE project=? AND host_scope=? "
        "AND session_epoch=? AND governance_epoch=?", (project, runtime._session_scope(),
        runtime.session_epoch(), runtime.host_epoch()))
