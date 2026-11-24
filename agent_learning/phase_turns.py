"""Bind review deliveries to observed native parent turns and exact Stop receipts."""
from __future__ import annotations

import json


NATIVE_HOSTS = {"codex", "dsh"}
RECENT_TURN_LIMIT = 16


def install(runtime):
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS phase_root_turns(
        project TEXT NOT NULL,host_scope TEXT NOT NULL,conversation TEXT NOT NULL,
        session_epoch INTEGER NOT NULL,governance_epoch INTEGER NOT NULL,
        prompt_event TEXT NOT NULL,turn_key TEXT,closed INTEGER NOT NULL DEFAULT 0,recent_turn_keys TEXT NOT NULL,
        PRIMARY KEY(project,host_scope,conversation))""")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS phase_delivery_turns(
        phase_id TEXT PRIMARY KEY,prompt_event TEXT NOT NULL,turn_key TEXT NOT NULL,stop_event TEXT,
        FOREIGN KEY(phase_id) REFERENCES review_phases(id) ON DELETE CASCADE)""")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS phase_delivery_turn_key ON phase_delivery_turns(turn_key)")


def _key(turn):
    from .runtime import digest
    return digest(turn) if isinstance(turn, str) and turn.strip() and len(turn) <= 250 else None


def _root(runtime, project, conversation):
    return runtime.db.execute("SELECT prompt_event,turn_key,closed,recent_turn_keys FROM phase_root_turns "
        "WHERE project=? AND host_scope=? AND conversation=? AND session_epoch=? AND governance_epoch=?",
        (project, runtime._session_scope(), conversation, runtime.session_epoch(), runtime.host_epoch())).fetchone()


def prompt_boundary(runtime, project, conversation, turn):
    """Accept a new prompt or identify the current prompt's replay receipt."""
    from .phase_review import scope
    if runtime.host not in NATIVE_HOSTS or scope(runtime, project) is None:
        return True, None
    key = _key(turn)
    previous = _root(runtime, project, conversation)
    if key and previous and previous["turn_key"] == key:
        return False, previous["prompt_event"]
    stale = key and ((previous and key in json.loads(previous["recent_turn_keys"]))
                     or _previous_delivery(runtime, project, conversation, key, current_epoch=True))
    return not stale, None


def observe_prompt(runtime, project, conversation, event_id, turn):
    from .phase_review import scope
    if runtime.host not in NATIVE_HOSTS or scope(runtime, project) is None:
        return
    previous = _root(runtime, project, conversation)
    recent = json.loads(previous["recent_turn_keys"]) if previous else []
    if previous and previous["turn_key"]:
        recent.append(previous["turn_key"])
    runtime.db.execute("INSERT INTO phase_root_turns VALUES(?,?,?,?,?,?,?,0,?) "
        "ON CONFLICT(project,host_scope,conversation) DO UPDATE SET session_epoch=excluded.session_epoch,"
        "governance_epoch=excluded.governance_epoch,prompt_event=excluded.prompt_event,"
        "turn_key=excluded.turn_key,closed=0,recent_turn_keys=excluded.recent_turn_keys",
        (project, runtime._session_scope(), conversation, runtime.session_epoch(), runtime.host_epoch(),
         event_id, _key(turn), json.dumps(recent[-RECENT_TURN_LIMIT:])))


def reset(runtime, project, conversation):
    if runtime.host in NATIVE_HOSTS:
        runtime.db.execute("DELETE FROM phase_root_turns WHERE project=? AND host_scope=? AND conversation=?",
            (project, runtime._session_scope(), conversation))


def live_turn(runtime, project, conversation):
    row = _root(runtime, project, conversation) if runtime.host in NATIVE_HOSTS else None
    return row if row and row["turn_key"] and not row["closed"] else None


def pin(runtime, phase_id, turn):
    if turn:
        runtime.db.execute("INSERT OR IGNORE INTO phase_delivery_turns(phase_id,prompt_event,turn_key) VALUES(?,?,?)",
            (phase_id, turn["prompt_event"], turn["turn_key"]))


def _previous_delivery(runtime, project, conversation, key, *, current_epoch=False):
    params = (project, runtime._session_scope(), conversation, key)
    epochs = "AND p.session_epoch=? AND p.governance_epoch=? " if current_epoch else ""
    if current_epoch:
        params += (runtime.session_epoch(), runtime.host_epoch())
    return runtime.db.execute("SELECT 1 FROM review_phases p JOIN phase_delivery_turns b ON b.phase_id=p.id "
        "WHERE p.project=? AND p.host_scope=? AND p.conversation=? AND b.turn_key=? " + epochs + "LIMIT 1",
        params).fetchone() is not None


def stop_boundary(runtime, project, conversation, task, turn):
    """Return whether this Stop belongs at the current boundary and any pin to close."""
    from .phase_review import scope
    if runtime.host not in NATIVE_HOSTS or scope(runtime, project) is None:
        return True, None
    root, seen = _root(runtime, project, conversation), _key(turn)
    if root and root["turn_key"] and (root["turn_key"] != seen or root["closed"]):
        return False, None
    if not task or task["status"] != "delivered":
        return True, None
    row = runtime.db.execute("SELECT b.phase_id,b.prompt_event,b.turn_key,b.stop_event FROM review_phases p "
        "JOIN phase_delivery_turns b ON b.phase_id=p.id WHERE p.project=? AND p.host_scope=? AND p.conversation=? "
        "AND p.task_id=? AND p.task_revision=? AND p.delivery_version=? AND p.session_epoch=? AND p.governance_epoch=?",
        (project, runtime._session_scope(), conversation, task["id"], task["revision"], task["write_version"],
         runtime.session_epoch(), runtime.host_epoch())).fetchone()
    if row:
        matches = bool(root and seen == row["turn_key"] and root["prompt_event"] == row["prompt_event"]
                       and row["stop_event"] is None)
        return matches, row["phase_id"] if matches else None
    # A previously pinned native turn cannot close a later legacy delivery.
    if seen and _previous_delivery(runtime, project, conversation, seen):
        return False, None
    return True, None


def observe_stop(runtime, project, conversation, event_id, turn, phase_id):
    runtime.db.execute("UPDATE phase_delivery_turns SET stop_event=? WHERE phase_id=? AND stop_event IS NULL",
                       (event_id, phase_id))
    runtime.db.execute("UPDATE phase_root_turns SET closed=1 WHERE project=? AND host_scope=? AND conversation=? "
        "AND session_epoch=? AND governance_epoch=? AND turn_key=?", (project, runtime._session_scope(), conversation,
        runtime.session_epoch(), runtime.host_epoch(), _key(turn)))


def matches_receipt(runtime, phase_id, event_id):
    row = runtime.db.execute("SELECT stop_event FROM phase_delivery_turns WHERE phase_id=?", (phase_id,)).fetchone()
    return row is None or row["stop_event"] == event_id
