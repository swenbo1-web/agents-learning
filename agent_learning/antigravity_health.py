"""Bounded, read-only capture coverage; never evidence of review or model health."""
from __future__ import annotations

import json
import sqlite3


RECENT_RECEIPTS = 32


def _latest(runtime, project, kind, epoch, prefix):
    # Cap indexed candidates before checking instance and trajectory boundaries.
    # A missing index on an old read-only database degrades to unknown instead
    # of scanning the full table for an optional panel observation.
    rows = runtime.db.execute(
        "SELECT conversation,event_key,created,generation,state FROM antigravity_receipts "
        "INDEXED BY antigravity_receipt_recency "
        "WHERE project=? AND kind=? AND host_epoch=? "
        "ORDER BY created DESC,event_key DESC LIMIT ?",
        (project, kind, epoch, RECENT_RECEIPTS)).fetchall()
    for row in rows:
        if not row["conversation"].startswith(prefix):
            continue
        current = runtime.db.execute(
            "SELECT generation,ambiguous FROM antigravity_trajectories "
            "WHERE project=? AND conversation=?", (project, row["conversation"])).fetchone()
        if current and current["generation"] == row["generation"] and current["ambiguous"] == 0:
            return row
    return None


def _event(runtime, project, row, response):
    from .runtime import digest

    if response:
        ids = [digest([project, ["antigravity-public-response", row["conversation"], row["event_key"]]])]
        kind = "AntigravityPublicResponse"
    else:
        ids = [digest([project, ["antigravity", row["conversation"], row["event_key"]]]),
               digest([project, ["antigravity", row["event_key"]]])]
        kind = "AntigravityStop"
    return runtime.db.execute(
        "SELECT created,json_extract(payload,'$.capture_coverage') AS coverage,"
        "json_extract(payload,'$.fullyIdle') AS idle,"
        "json_extract(payload,'$.terminationReason') AS termination,"
        "json_extract(payload,'$.error') AS error "
        "FROM events WHERE project=? AND conversation=? AND type=? AND id IN ("
        + ",".join("?" for _ in ids) + ") ORDER BY created DESC LIMIT 1",
        (project, row["conversation"], kind, *ids)).fetchone()


def _observation(runtime, project, row, response):
    if row["state"] != "complete":
        return {"state": "pending", "value": "unknown", "reason": "receipt_pending",
                "source": None, "observed_at": row["created"]}
    event = _event(runtime, project, row, response)
    if event is None:
        return {"state": "unavailable", "value": "unknown", "reason": "receipt_unlinked",
                "source": None, "observed_at": row["created"]}
    view = {"state": "observed", "value": "unknown", "reason": "coverage_unavailable",
            "source": None, "observed_at": event["created"]}
    raw = event["coverage"]
    if not isinstance(raw, str) or len(raw) > 8192:
        return view
    coverage = json.loads(raw)
    if not isinstance(coverage, dict):
        raise ValueError("invalid coverage")
    field = coverage.get("finalModelOutput")
    field = field if isinstance(field, dict) else {}
    if field.get("source") in {"native_hook", "antigravity_transcript"}:
        view["source"] = field["source"]
    if (coverage.get("workspace_association") != "installed_workspace"
            or coverage.get("trajectory_identity") != "current"):
        return {**view, "value": "unavailable", "reason": "boundary_unverified"}
    if field.get("available") is not True:
        return {**view, "value": "unavailable", "reason": "public_output_missing"}
    full = field.get("truncated") is False and field.get("full_original_available") is True
    if response:
        # An invocation response is searchable history, never stage completion.
        return {**view, "value": "available" if full else "partial",
                "reason": "execution_completion_unverified"}
    complete = (full and event["idle"] == 1
                and event["termination"] in {"model_stop", "NO_TOOL_CALL"} and not event["error"])
    return {**view, "value": "complete" if complete else "partial",
            "reason": None if complete else "public_output_partial"}


def capture_status(runtime, project):
    """Observe the latest current conversation without replay or text access.

    Pending callbacks cannot borrow old successes; responses in one conversation
    cannot complete another's Stop. Exhausted candidates leave coverage unknown.
    """
    scope = runtime._session_scope()
    result = {"version": 1, "scope": "project_recent_capture", "host_scope": scope,
              "state": "unobserved", "public_delivery": "unknown", "public_response": "unknown",
              "reason": "not_observed", "source": None, "observed_at": None,
              "response_observed_at": None}
    if runtime.host != "antigravity":
        return {**result, "state": "unsupported", "reason": "host_not_supported"}
    try:
        epoch, prefix = runtime.session_epoch(), scope + ":"
        stop = _latest(runtime, project, "Stop", epoch, prefix)
        post = _latest(runtime, project, "PostInvocation", epoch, prefix)
        candidates = [row for row in (stop, post) if row is not None]
        if not candidates:
            return result
        latest = max(candidates, key=lambda row: (row["created"], row["event_key"]))
        current = _observation(runtime, project, latest, latest is post)
        result.update({key: current[key] for key in ("state", "reason", "source", "observed_at")})
        if post is not None and post["conversation"] == latest["conversation"]:
            response = current if latest is post else _observation(runtime, project, post, True)
            result.update(public_response=response["value"], response_observed_at=response["observed_at"])
        if latest is stop:
            result["public_delivery"] = current["value"]
        # An older Stop does not establish that a newer invocation has ended.
        return result
    except (sqlite3.Error, ValueError, TypeError, RecursionError):
        return {**result, "state": "unavailable", "public_delivery": "unknown",
                "public_response": "unknown", "reason": "query_unavailable",
                "source": None, "observed_at": None, "response_observed_at": None}
