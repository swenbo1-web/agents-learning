"""Bounded, read-only capture coverage; never evidence of review or model health."""
from __future__ import annotations

import json
import sqlite3


RECENT_RECEIPTS = 32
_INPUT_WINDOW_REASONS = frozenset({
    "bound", "native_prompt_empty", "native_prompt_unsupported_type",
    "transcript_path_missing", "transcript_path_invalid", "transcript_conversation_mismatch",
    "transcript_path_redirected", "transcript_not_regular", "transcript_unavailable",
    "transcript_changed_during_read", "transcript_scan_budget", "transcript_incomplete_line",
    "transcript_read_deadline", "transcript_line_budget", "transcript_invalid_json",
    "transcript_schema_unknown", "transcript_step_conflict", "transcript_user_boundary_missing",
    "transcript_ahead_of_invocation", "transcript_native_prompt_mismatch",
    "transcript_pre_receipt_missing", "transcript_window_unbound",
    "workspace_association_unknown", "trajectory_identity_ambiguous",
})


def _latest(runtime, project, kind, epoch, prefix):
    # Cap indexed candidates before checking instance and trajectory boundaries.
    # A missing index on an old read-only database degrades to unknown instead
    # of scanning the full table for an optional panel observation.
    rows = runtime.db.execute(
        "SELECT conversation,event_key,created,generation,state,host_epoch FROM antigravity_receipts "
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
            return row, False
    return None, len(rows) >= RECENT_RECEIPTS


def _event(runtime, project, row, response, *, input_event=False):
    from .runtime import digest

    if input_event:
        ids = [digest([project, ["antigravity-public-input", row["conversation"], row["event_key"]]])]
        kind = "AntigravityPublicInput"
    elif response:
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
        "json_extract(payload,'$.error') AS error,"
        "json_extract(payload,'$.public_window') AS input_window,"
        "json_extract(payload,'$.host_epoch') AS host_epoch,"
        "json_extract(payload,'$.generation') AS generation "
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


def _input_status(runtime, project, row):
    """Return only current Pre metadata; old receipts cannot supply a new window."""
    view = {"state": "unobserved", "public_input": "unknown", "available": None,
            "reason": "not_observed", "source": None, "observed_at": None}
    if row is None:
        return view
    view["observed_at"] = row["created"]
    if row["state"] != "complete":
        return {**view, "state": "pending", "reason": "receipt_pending"}
    event = _event(runtime, project, row, False, input_event=True)
    if event is None:
        return {**view, "state": "unavailable", "reason": "receipt_unlinked"}
    if event["host_epoch"] != row["host_epoch"] or event["generation"] != row["generation"]:
        return {**view, "state": "unavailable", "reason": "boundary_unverified"}
    raw, raw_window = event["coverage"], event["input_window"]
    if (not isinstance(raw, str) or len(raw) > 8192
            or not isinstance(raw_window, str) or len(raw_window) > 2048):
        return {**view, "state": "unavailable", "reason": "coverage_unavailable"}
    coverage, window = json.loads(raw), json.loads(raw_window)
    if not isinstance(coverage, dict) or not isinstance(window, dict):
        return {**view, "state": "unavailable", "reason": "coverage_unavailable"}
    if (coverage.get("workspace_association") != "installed_workspace"
            or coverage.get("trajectory_identity") != "current"):
        return {**view, "state": "unavailable", "reason": "boundary_unverified"}
    field = coverage.get("lastUserInput")
    field = field if isinstance(field, dict) else {}
    source = field.get("source")
    if source in {"native_hook", "antigravity_transcript"}:
        view["source"] = source
    full = field.get("truncated") is False and field.get("full_original_available") is True
    public = ("available" if full else "partial") if field.get("available") is True else "unavailable"
    reason = window.get("reason")
    # Source files and arbitrary diagnostic text never enter the status response.
    known_reason = isinstance(reason, str) and reason in _INPUT_WINDOW_REASONS
    reason = reason if known_reason else "window_unverified"
    available = window.get("available")
    if not known_reason or type(available) is not bool or available != (reason == "bound"):
        available, reason = None, "window_unverified"
    view.update(state="observed", public_input=public, available=available, reason=reason)
    trailing = window.get("trailing_system_steps")
    if available and type(trailing) is int and 0 <= trailing <= 1024:
        view["trailing_system_steps"] = trailing
    return view


def capture_status(runtime, project):
    """Observe the latest current conversation without replay or text access.

    Pending callbacks cannot borrow old successes; responses in one conversation
    cannot complete another's Stop. Exhausted candidates leave coverage unknown.
    """
    scope = runtime._session_scope()
    result = {"version": 1, "scope": "project_recent_capture", "host_scope": scope,
              "state": "unobserved", "public_delivery": "unknown", "public_response": "unknown",
              "reason": "not_observed", "source": None, "observed_at": None,
              "response_observed_at": None, "scan_limited": False,
              "input_window": _input_status(runtime, project, None)}
    if runtime.host != "antigravity":
        return {**result, "state": "unsupported", "reason": "host_not_supported"}
    try:
        epoch, prefix = runtime.session_epoch(), scope + ":"
        stop, stop_limited = _latest(runtime, project, "Stop", epoch, prefix)
        post, post_limited = _latest(runtime, project, "PostInvocation", epoch, prefix)
        pre, pre_limited = _latest(runtime, project, "PreInvocation", epoch, prefix)
        if stop_limited or post_limited or pre_limited:
            # No further COUNT/scan: an unseen callback may supersede any old
            # success in another kind, especially a newer Pre in this instance.
            return {**result, "state": "unavailable", "reason": "recent_window_exhausted",
                    "scan_limited": True,
                    "input_window": {**result["input_window"], "state": "unavailable",
                                     "reason": "recent_window_exhausted"}}
        candidates = [row for row in (stop, post, pre) if row is not None]
        if not candidates:
            return result
        latest = max(candidates, key=lambda row: (row["created"], row["event_key"]))
        if pre is not None and pre["conversation"] == latest["conversation"]:
            result["input_window"] = _input_status(runtime, project, pre)
        if latest is pre:
            current_input = result["input_window"]
            return {**result, "state": current_input["state"],
                    "reason": ("execution_completion_unverified" if current_input["state"] == "observed"
                               else current_input["reason"]),
                    "source": current_input["source"], "observed_at": current_input["observed_at"]}
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
                "source": None, "observed_at": None, "response_observed_at": None,
                "input_window": {**_input_status(runtime, project, None),
                                 "state": "unavailable", "reason": "query_unavailable"}}
