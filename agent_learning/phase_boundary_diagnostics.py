"""Bounded, read-only explanations for stages waiting for their original Stop.

This projection never seals a stage or borrows a later delivery's receipt. A
bounded event tail cannot prove absence outside its coverage, so that case is
explicitly unknown. No public text, task goal or captured payload is read.
"""
from __future__ import annotations

from collections import Counter
import math
import sqlite3


SAMPLE_LIMIT = 8
EVENT_SCAN_LIMIT = 128
_UNKNOWN = frozenset({"boundary_unverified", "stop_lookup_limited"})


def _identity(value):
    return value if isinstance(value, str) and 0 < len(value) <= 250 else None


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _version(value):
    return value if type(value) is int and value >= 0 else None


def _matches_stop(event, phase):
    return bool(event and event["project"] == phase["project"]
        and event["conversation"] == phase["conversation"] and event["type"] == "Stop"
        and event["agent"] is None and event["task_status"] == "delivered"
        and event["task_id"] == phase["task_id"] and event["task_revision"] == phase["task_revision"]
        and event["task_write_version"] == phase["delivery_version"]
        and event["created"] >= phase["start_time"])


def _relation(row):
    if row["current_id"] is None:
        return "unavailable"
    if row["current_id"] != row["task_id"]:
        return "task_changed"
    if row["current_revision"] != row["task_revision"]:
        return "revision_changed"
    if row["current_status"] != "delivered":
        return "delivery_interrupted"
    if row["current_write_version"] != row["delivery_version"]:
        # A delivered-state fence can advance a write without being another
        # logical delivery. Do not diagnose a missing receipt from that alone.
        return "snapshot_advanced"
    return "same_delivery"


def delivery_boundary_diagnostics(runtime, project):
    """Describe at most eight latest awaiting stages in this project/host.

    Counts describe this sample, not a total queue count. Historical epochs
    stay visible as historical and never use current capture or recovery.
    """
    result = {"checked": True, "sample_limit": SAMPLE_LIMIT, "sampled": 0, "truncated": False,
              "event_scan_limit": EVENT_SCAN_LIMIT, "counts": {}, "unknown_count": 0,
              "items": [], "counts_scope": "sample"}
    try:
        return _diagnose(runtime, project, result)
    except (sqlite3.Error, TypeError, ValueError, OverflowError):
        # Optional diagnostics must not prevent settings/queue inspection when
        # a legacy table or the read budget is unavailable. Never reveal SQL.
        return {**result, "checked": False, "sampled": 0, "counts": {}, "unknown_count": None,
                "items": [], "reason": "diagnostics_unavailable"}


def _diagnose(runtime, project, result):
    host = runtime._session_scope()
    session, governance = runtime.session_epoch(), runtime.host_epoch()
    rows = runtime.db.execute("SELECT p.*,t.id AS current_id,t.revision AS current_revision,"
        "t.write_version AS current_write_version,t.status AS current_status "
        "FROM review_phases p LEFT JOIN tasks t ON t.project=p.project AND t.conversation=p.conversation "
        "WHERE p.project=? AND p.host_scope=? AND p.state='awaiting_stop' "
        "ORDER BY p.created DESC,p.rowid DESC LIMIT ?", (project, host, SAMPLE_LIMIT + 1)).fetchall()
    result["truncated"] = len(rows) > SAMPLE_LIMIT
    rows = rows[:SAMPLE_LIMIT]
    tails = {}
    for row in rows:
        item = {"phase_id": _identity(row["id"]), "conversation_id": _identity(row["conversation"]),
                "task_id": _identity(row["task_id"]), "task_revision": _version(row["task_revision"]),
                "delivery_version": _version(row["delivery_version"]), "created": _number(row["created"]),
                "reason": "boundary_unverified", "current_task_relation": _relation(row),
                "stop_observed": None, "request_pending": False, "attempts": None}
        result["items"].append(item)
        if (row["session_epoch"], row["governance_epoch"]) != (session, governance):
            item["reason"] = "historical_boundary"
            continue
        if (not runtime.owns_conversation(row["conversation"]) or item["phase_id"] is None
                or item["task_id"] is None or item["conversation_id"] is None
                or item["task_revision"] is None or item["delivery_version"] is None
                or _number(row["start_time"]) is None or row["event_id"] is not None):
            continue
        request = runtime.db.execute("SELECT project,host_scope,session_epoch,governance_epoch,event_id,attempts "
            "FROM phase_seal_requests WHERE phase_id=?", (row["id"],)).fetchone()
        if request is not None:
            item["request_pending"] = True
            if (request["project"], request["host_scope"], request["session_epoch"], request["governance_epoch"]) \
                    != (project, host, session, governance):
                continue
            stop = runtime.db.execute("SELECT id,project,conversation,type,agent,task_status,task_id,"
                "task_revision,task_write_version,created FROM events WHERE project=? AND id=?",
                (project, request["event_id"])).fetchone()
            if not _matches_stop(stop, row):
                continue
            attempts = request["attempts"]
            item.update(stop_observed=True, attempts=attempts if type(attempts) is int and attempts >= 0 else None,
                        reason="local_seal_retry" if type(attempts) is int and attempts > 0 else "awaiting_local_seal")
            continue
        conversation = row["conversation"]
        if conversation not in tails:
            # Reuse one indexed metadata tail for stages of the same chat.
            # Read at most limit+1 rows, even when no exact Stop exists.
            first = min(phase["start_time"] for phase in rows if phase["conversation"] == conversation
                        and _number(phase["start_time"]) is not None)
            tails[conversation] = runtime.db.execute("SELECT id,project,conversation,type,agent,task_status,task_id,"
                "task_revision,task_write_version,created FROM events WHERE project=? AND conversation=? "
                "AND created>=? ORDER BY created DESC,rowid DESC LIMIT ?",
                (project, conversation, first, EVENT_SCAN_LIMIT + 1)).fetchall()
        tail = tails[conversation]
        events = [event for event in tail if event["created"] >= row["start_time"]]
        if any(_matches_stop(event, row) for event in events[:EVENT_SCAN_LIMIT]):
            item.update(reason="awaiting_local_seal", stop_observed=True)
        elif len(events) > EVENT_SCAN_LIMIT:
            item["reason"] = "stop_lookup_limited"
        else:
            item["stop_observed"] = False
            if item["current_task_relation"] == "unavailable":
                continue
            item["reason"] = ("missing_host_stop_after_boundary" if item["current_task_relation"]
                in {"task_changed", "revision_changed", "delivery_interrupted"} else "awaiting_host_stop")
    result.update(sampled=len(result["items"]),
                  counts=dict(Counter(item["reason"] for item in result["items"])),
                  unknown_count=sum(item["reason"] in _UNKNOWN for item in result["items"]))
    return result
