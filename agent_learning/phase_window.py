"""Logical review windows are independent of execution permission fences."""
from __future__ import annotations

import json
import time


def install(runtime):
    runtime.db.execute("CREATE TABLE IF NOT EXISTS phase_review_windows("
        "project TEXT NOT NULL,host_scope TEXT NOT NULL,conversation TEXT NOT NULL,"
        "task_id TEXT NOT NULL,task_revision INTEGER NOT NULL,session_epoch INTEGER NOT NULL,"
        "governance_epoch INTEGER NOT NULL,start_time REAL NOT NULL,"
        "PRIMARY KEY(project,host_scope,conversation))")


def _enabled(runtime, project):
    from .phase_review import policy
    return policy(runtime, project)["mode"] == "phase"


def _current(runtime, project, conversation, task):
    row = runtime.db.execute("SELECT * FROM phase_review_windows WHERE project=? AND host_scope=? AND conversation=?",
        (project, runtime._session_scope(), conversation)).fetchone()
    return row if row and (row["task_id"], row["task_revision"], row["session_epoch"], row["governance_epoch"]) == (
        task["id"], task["revision"], runtime.session_epoch(), runtime.host_epoch()) else None


def _open(runtime, project, conversation, task, start, *, ordered=True, inclusive_event=None):
    runtime.db.execute("INSERT INTO phase_review_windows VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(project,host_scope,conversation) DO UPDATE SET task_id=excluded.task_id,"
        "task_revision=excluded.task_revision,session_epoch=excluded.session_epoch,"
        "governance_epoch=excluded.governance_epoch,start_time=excluded.start_time",
        (project, runtime._session_scope(), conversation, task["id"], task["revision"],
         runtime.session_epoch(), runtime.host_epoch(), start))
    # Wall clocks may return equal values across pause/resume and opt-in. Keep
    # an ordered local boundary without changing the source timestamps.
    runtime._event(project, conversation, "PhaseWindowStarted", {
        "host_scope": runtime._session_scope(), "host_epoch": runtime.session_epoch(),
        "governance_epoch": runtime.host_epoch(), "start_time": start, "ordered_boundary": ordered,
        "inclusive_event_id": inclusive_event},
        task_id=task["id"], task_revision=task["revision"],
        task_write_version=task["write_version"], outcome="reported")


def event_bounds(runtime, project, phase):
    """Original active suffix plus an optional ordered boundary for new windows.

    Older windows have no marker; their immutable task history still excludes
    observations from an earlier paused/completed suffix at the same timestamp.
    """
    last = runtime.db.execute("SELECT COALESCE(MAX(write_version),0) FROM task_states "
        "WHERE project=? AND id=? AND revision=? AND status!='active' AND write_version<?",
        (project, phase["task_id"], phase["task_revision"], phase["delivery_version"])).fetchone()[0]
    first = runtime.db.execute("SELECT MIN(write_version) FROM task_states WHERE project=? AND id=? "
        "AND revision=? AND status='active' AND write_version>? AND write_version<?",
        (project, phase["task_id"], phase["task_revision"], last, phase["delivery_version"])).fetchone()[0]
    marker = runtime.db.execute("SELECT rowid,payload FROM events WHERE project=? AND conversation=? "
        "AND type='PhaseWindowStarted' AND task_id=? AND task_revision=? AND task_write_version<=? "
        "AND json_extract(payload,'$.host_scope')=? AND json_extract(payload,'$.host_epoch')=? "
        "AND json_extract(payload,'$.governance_epoch')=? AND json_extract(payload,'$.start_time')=? "
        "AND json_extract(payload,'$.ordered_boundary')=1 "
        "ORDER BY rowid DESC LIMIT 1", (project, phase["conversation"], phase["task_id"],
        phase["task_revision"], phase["delivery_version"], phase["host_scope"],
        phase["session_epoch"], phase["governance_epoch"], phase["start_time"])).fetchone()
    sequence = marker[0] if marker else 0
    if marker:
        initiating = json.loads(marker[1]).get("inclusive_event_id")
        event = runtime.db.execute("SELECT rowid FROM events WHERE project=? AND conversation=? AND id=?",
            (project, phase["conversation"], initiating)).fetchone() if initiating else None
        if event and event[0] <= sequence:
            sequence = event[0] - 1
    return first or 0, sequence


def activate(runtime, project):
    """Explicitly entering phase mode starts here, never backfills old work."""
    now = time.time()
    for row in runtime.db.execute("SELECT * FROM tasks WHERE project=? AND status='active'", (project,)).fetchall():
        if runtime.owns_conversation(row["conversation"]):
            _open(runtime, project, row["conversation"], row, now)


def observe(runtime, project, conversation, task, event):
    if (not task or task["status"] != "active" or not _enabled(runtime, project)
            or event["type"] not in {"UserPromptSubmit", "SessionStart"}):
        return
    if not _current(runtime, project, conversation, task):
        old = runtime.db.execute("SELECT 1 FROM phase_review_windows WHERE project=? AND host_scope=? AND conversation=?",
                                (project, runtime._session_scope(), conversation)).fetchone()
        # A known window crossing an account/contract boundary starts at this
        # new event. A missing upgrade cache can reconstruct its active suffix.
        start = event["created"] if old else start_time(runtime, project, conversation, task)
        _open(runtime, project, conversation, task, min(start, event["created"]), ordered=bool(old),
              inclusive_event=event["id"])


def task_changed(runtime, project, conversation, previous, task, *, changed_contract, resume):
    if task["status"] != "active" or not _enabled(runtime, project) or not (changed_contract or resume):
        return
    old = runtime.db.execute("SELECT session_epoch,governance_epoch FROM phase_review_windows "
        "WHERE project=? AND host_scope=? AND conversation=?", (project, runtime._session_scope(), conversation)).fetchone()
    _open(runtime, project, conversation, task, task["updated"])
    if old and tuple(old) != (runtime.session_epoch(), runtime.host_epoch()):
        return
    # A user normally speaks before the Agent declares the new task. Preserve
    # just that unfinished root request as explicit derived context; never
    # rewrite the source contract or borrow its tool-success evidence.
    prompt = runtime.db.execute("SELECT rowid AS source_rowid,* FROM events WHERE project=? AND conversation=? "
        "AND agent IS NULL AND type='UserPromptSubmit' ORDER BY created DESC,rowid DESC LIMIT 1",
        (project, conversation)).fetchone()
    if not prompt:
        return
    cfg = runtime.db.execute("SELECT updated FROM phase_review_policies WHERE project=? AND host_scope=?",
                             (project, runtime._session_scope())).fetchone()
    payload = json.loads(prompt["payload"])
    if (prompt["created"] < (cfg[0] if cfg else task["updated"])
            or payload.get("host_epoch") != runtime.session_epoch()):
        return
    intervening = runtime.db.execute("SELECT 1 FROM events WHERE project=? AND conversation=? AND agent IS NULL "
        "AND rowid>? AND type IN ('Stop','TaskUpdate','Interrupt') AND NOT (type='TaskUpdate' AND task_id=? "
        "AND json_extract(payload,'$.write_version')=?) LIMIT 1",
        (project, conversation, prompt["source_rowid"], task["id"], task["write_version"])).fetchone()
    if intervening:
        return
    runtime._event(project, conversation, "PhaseRequestContext", {
        "original_event_id": prompt["id"], "original_task_id": prompt["task_id"],
        "original_task_revision": prompt["task_revision"], "original_created": prompt["created"],
        "derived_context": True, "validation_level": "reported", "prompt": payload.get("prompt", ""),
        "truncated": bool(payload.get("truncated")), "host_epoch": runtime.session_epoch(),
        "limitations": "Initiating request reference only; not independent or tool-success evidence."},
        event_id=["phase-request", runtime._session_scope(), task["id"], task["revision"],
                  task["write_version"], prompt["id"]], task_id=task["id"], task_revision=task["revision"], outcome="reported")


def start_time(runtime, project, conversation, task):
    row = _current(runtime, project, conversation, task)
    if row:
        return row["start_time"]
    # On upgrading an already-active task, use the current logical active
    # suffix rather than the latest permission-fence write. Respect the phase
    # opt-in and current account/governance boundary; completed phases stay put.
    last = runtime.db.execute("SELECT COALESCE(MAX(write_version),0) FROM task_states WHERE project=? AND id=? "
        "AND revision=? AND status!='active' AND write_version<?",
        (project, task["id"], task["revision"], task["write_version"])).fetchone()[0]
    first = runtime.db.execute("SELECT updated FROM task_states WHERE project=? AND id=? AND revision=? "
        "AND status='active' AND write_version>? ORDER BY write_version LIMIT 1",
        (project, task["id"], task["revision"], last)).fetchone()
    policy = runtime.db.execute("SELECT updated FROM phase_review_policies WHERE project=? AND host_scope=?",
                               (project, runtime._session_scope())).fetchone()
    boundary = runtime.db.execute("SELECT MIN(created) FROM events WHERE project=? AND conversation=? "
        "AND agent IS NULL AND json_extract(payload,'$.host_epoch')=? AND type IN ('UserPromptSubmit','SessionStart')",
        (project, conversation, runtime.session_epoch())).fetchone()[0]
    start = max(first[0] if first else task["updated"], policy[0] if policy else task["updated"],
                boundary if boundary is not None else task["updated"])
    _open(runtime, project, conversation, task, start, ordered=False)
    return start
