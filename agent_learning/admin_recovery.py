"""Project/host scoped recovery decisions; never launch models or publish knowledge."""
from __future__ import annotations

import json
import re
import time

from .runtime import ContractError, digest, encode
from .admin_lifecycle import RETRYABLE_STATES, retry_call, unresolved_failure_sql
from .candidate_review import BUDGET_PAUSES


def _version(row):
    return digest(dict(row))


def blocked_review_summary(runtime, project):
    """Classify unresolved attempts without treating another host as this one.

    Old attempts without a frozen execution plan remain unattributed project
    records. They retain their locks but cannot be presented as this host's
    confirmed calls or offered a guessed host recovery action.
    """
    rows = runtime.db.execute(
        "SELECT CASE WHEN p.call_id IS NULL THEN 'unattributed' ELSE 'host' END AS attribution,"
        "CASE WHEN a.state='failed' THEN 'failed' ELSE 'result_unknown' END AS kind,COUNT(*) AS count "
        "FROM admin_calls a LEFT JOIN admin_execution_plans p ON p.call_id=a.id AND p.project=a.project "
        "WHERE a.project=? AND (p.host_scope=? OR p.call_id IS NULL) AND " + unresolved_failure_sql("a")
        + " GROUP BY attribution,kind", (project, runtime._session_scope())).fetchall()
    result = {"host_scope": runtime._session_scope(), "failed": 0, "result_unknown": 0,
              "unattributed": {"failed": 0, "result_unknown": 0}}
    for row in rows:
        target = result if row["attribution"] == "host" else result["unattributed"]
        target[row["kind"]] = row["count"]
    result["total"] = result["failed"] + result["result_unknown"]
    return result


def _call(runtime, project, identity):
    return runtime.db.execute(
        "SELECT a.id,a.problem,a.revision,a.state,a.created,a.finished,a.retry_allowed,a.retry_requested,a.usage "
        "FROM admin_calls a JOIN admin_execution_plans p ON p.call_id=a.id AND p.project=a.project "
        "WHERE a.project=? AND a.id=? AND p.host_scope=?",
        (project, identity, runtime._session_scope())).fetchone()


def _history(runtime, project, identity):
    return runtime.db.execute(
        "SELECT id,state,host_epoch,review_enabled,max_calls,remaining_calls,owner,lease_until,updated,last_result,signature "
        "FROM history_jobs WHERE project=? AND id=?", (project, identity)).fetchone()


def _history_available(row):
    if row["owner"] and (row["lease_until"] or 0) > time.time():
        return False
    return row["state"] in {"materialized", "review_complete", "materializing", "reviewing"} or (
        row["state"] == "paused" and json.loads(row["last_result"]).get("reason") in BUDGET_PAUSES)


def _history_review_available(row):
    """Explicit runs may resume pauses or interrupted frozen work, never live owners."""
    return (not row["owner"] or (row["lease_until"] or 0) <= time.time()) and row["state"] in {
        "materialized", "review_complete", "paused", "materializing", "reviewing"}


def recovery_status(runtime, project, *, include_history=True):
    """Bounded read view. Observations do not prove current-session Hook health."""
    db, scope = runtime.db, runtime._session_scope()
    capture = db.execute(
        "SELECT COUNT(DISTINCT conversation) AS conversations,MAX(created) AS last_event_at FROM events "
        "WHERE project=? AND agent IS NULL AND type IN ('SessionStart','UserPromptSubmit','Stop','PreToolUse','PostToolUse')",
        (project,)).fetchone()
    memory_count = db.execute("SELECT COUNT(*) FROM work_memories WHERE project=?", (project,)).fetchone()[0]
    calls = []
    blocked = blocked_review_summary(runtime, project)
    identities = db.execute(
        "SELECT a.id FROM admin_calls a JOIN admin_execution_plans p ON p.call_id=a.id AND p.project=a.project "
        "WHERE a.project=? AND p.host_scope=? AND " + unresolved_failure_sql("a") + " "
        "ORDER BY a.created DESC,a.id LIMIT 20", (project, scope)).fetchall()
    for item in identities:
        row = _call(runtime, project, item[0])
        try:
            usage = json.loads(row["usage"] or "{}")
            execution = usage.get("execution") or {}
        except (ValueError, TypeError, AttributeError):
            usage, execution = {}, {}
        from .admin_usage import normalize_usage
        try:
            completeness = normalize_usage(usage)["completeness"]
        except (ContractError, ValueError, TypeError, AttributeError):
            completeness = "unknown"
        from .admin_response_store import status as response_status
        from .admin_observations import execution_observations, review_failure_reason
        saved_response = response_status(runtime, project, row["id"])
        local = saved_response.get("can_recheck") is True
        historical = (saved_response.get("history_recovery") or {}).get("can_recheck") is True
        calls.append({"id": row["id"], "version": _version(row), "state": row["state"],
            "failure_class": "failed" if row["state"] == "failed" else "result_unknown",
            "suggested_recovery": "local_recheck" if local else "history_local_recheck" if historical else "explicit_retry",
            "saved_response": saved_response,
            "execution_observations": execution_observations(usage),
            "created": row["created"], "reported_turns": usage.get("reported_turns"),
            "usage_known": completeness == "complete", "usage_completeness": completeness,
            "reason": review_failure_reason(usage),
            "can_retry": row["state"] in RETRYABLE_STATES})
    history = []
    if include_history:
        history = history_recovery_page(runtime, project, limit=10)["items"]
    return {"capture": {**dict(capture), "work_memories": memory_count, "current_hook": "unknown",
                "note": "Existing conversations need a host reload and a fresh supported event; archived files are not chat history."},
            "blocked_reviews": calls, "blocked_review_summary": blocked,
            "blocked_reviews_truncated": blocked["total"] > len(calls),
            "history_jobs": history, "history_separate": not include_history,
            "models_started": False}


def history_recovery_page(runtime, project, *, offset=0, limit=3):
    from .history_import import _read_snapshot
    with _read_snapshot(runtime):
        return _history_recovery_page(runtime, project, offset=offset, limit=limit)


def _history_recovery_page(runtime, project, *, offset, limit):
    if type(offset) is not int or not 0 <= offset <= 10000 or type(limit) is not int or not 1 <= limit <= 10:
        raise ContractError("invalid history progress page")
    from .history_import import _summaries
    db = runtime.db
    rows = db.execute("SELECT * FROM history_jobs WHERE project=? ORDER BY created DESC,id LIMIT ? OFFSET ?",
                      (project, limit, offset)).fetchall()
    from .history_workflow import head
    selected = head(runtime, project)
    selected_id = selected["id"] if selected else None
    summary_rows = list(rows)
    if selected and all(row["id"] != selected_id for row in rows):
        summary_rows.append(selected)
    progress_by_id = {item["job_id"]: item for item in _summaries(runtime, summary_rows)}
    head_blocks = bool(selected and progress_by_id[selected_id]["workflow"]["blocks_new_batch"])
    history = []
    for item in rows:
        row = _history(runtime, project, item["id"])
        plan = json.loads(item["plan"])
        stats = plan.get("stats") or {}
        pending = plan.get("pending") or []
        unsupported = sum(isinstance(value, dict) and value.get("reason") == "unparsed_file_type" for value in pending)
        progress = progress_by_id[row["id"]]
        review_request = None
        from .history_review_control import current_history_request
        active_request = current_history_request(runtime, project, row["id"], active_only=True)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='history_review_requests' AND type='table'").fetchone():
            request = active_request or current_history_request(runtime, project, row["id"])
            current = request is not None
            request = request or db.execute("SELECT state,lease_until,result FROM history_review_requests WHERE project=? AND job_id=? "
                "ORDER BY created DESC,request_id DESC LIMIT 1", (project, row["id"])).fetchone()
            if request:
                review_request = {"current": current, "state": "superseded" if not current else
                    "result_unknown" if request["state"] in {"reserved", "running"}
                    and (request["lease_until"] or 0) <= time.time() else request["state"],
                    "result": json.loads(request["result"])}
        from .candidate_references import history_grant
        history.append({"id": row["id"], "version": _version(row), "state": row["state"],
            "candidate_reference_grant": history_grant(runtime, project, row["id"]),
            "epoch_matches": row["host_epoch"] == runtime.host_epoch(),
            "review_enabled": bool(row["review_enabled"]), "remaining_calls": row["remaining_calls"],
            "files_read": stats.get("files_read", 0), "incomplete": bool(plan.get("incomplete")),
            "pending": plan.get("pending_count", 0), "unsupported": unsupported,
            "conversation_sources": sum(isinstance(value, dict) and value.get("kind") == "conversation"
                                        for value in plan.get("extra_sources", [])),
            "can_reauthorize": _history_available(row) and row["host_epoch"] != runtime.host_epoch(),
            "counts": progress["counts"], "review_request": review_request,
            "review_progress": progress["coverage"]["review"],
            "awaiting_approval": progress["coverage"]["publication"]["awaiting_approval"],
            "resume_required": progress["resume_required"],
            "workflow": progress["workflow"], "changes": progress.get("changes"),
            "selected": row["id"] == selected_id,
            "blocked_by_batch": selected_id if head_blocks and row["id"] != selected_id else None,
            "can_defer": row["id"] == selected_id and progress["workflow"]["blocks_new_batch"] and not progress["run_active"] and not active_request,
            "can_review": _history_review_available(row) and row["host_epoch"] == runtime.host_epoch()
                and not active_request and progress["resume"]["explicit_run_required"]
                and (not head_blocks or row["id"] == selected_id),
            "publication_automatic": progress.get("publication_automatic", False)})
    total = db.execute("SELECT COUNT(*) FROM history_jobs WHERE project=?", (project,)).fetchone()[0]
    return {"items": history, "total": total, "offset": offset,
            "next_offset": offset + len(history) if offset + len(history) < total else None}


def apply_recovery(runtime, project, body, *, details=True):
    """Explicit, versioned, idempotent permission update. No wake or model calls."""
    required = {"request_id", "expected_policy_version", "expected_session_epoch", "expected_governance_epoch",
                "action", "id", "version", "reason", "confirmed"}
    if (not isinstance(body, dict) or set(body) != required or body["confirmed"] is not True
                or not isinstance(body["action"], str) or body["action"] not in {"retry_call", "reauthorize_history", "defer_history"}
            or any(not isinstance(body[key], str) or not body[key].strip() or len(body[key]) > maximum
                   for key, maximum in (("request_id", 200), ("id", 200), ("reason", 2000)))
            or not isinstance(body["version"], str) or not re.fullmatch(r"[a-f0-9]{64}", body["version"])
            or any(type(body[key]) is not int or body[key] < 0 for key in
                   ("expected_policy_version", "expected_session_epoch", "expected_governance_epoch"))):
        raise ContractError("invalid explicit administrator recovery request")
    identity = (project, runtime._session_scope(), body["expected_session_epoch"], body["request_id"])
    fingerprint = digest({"kind": "administrator_recovery_v1", "body": body})
    with runtime.transaction():
        runtime.assert_session_epoch(body["expected_session_epoch"])
        runtime.assert_host_epoch(body["expected_governance_epoch"])
        prior = runtime.db.execute("SELECT request_digest,response FROM admin_control_receipts "
            "WHERE project=? AND host_scope=? AND session_epoch=? AND request_id=?", identity).fetchone()
        if prior:
            if prior["request_digest"] != fingerprint:
                raise ContractError("administrator recovery request identity changed")
            return json.loads(prior["response"])
        if runtime.admin_policy(project)["policy_version"] != body["expected_policy_version"]:
            raise ContractError("administrator policy changed; reread before recovery")
        row = (_call(runtime, project, body["id"]) if body["action"] == "retry_call"
               else _history(runtime, project, body["id"]))
        if row is None or _version(row) != body["version"]:
            raise ContractError("administrator recovery target changed or is outside this project/host")
        if body["action"] == "retry_call":
            if runtime.db.execute("SELECT 1 FROM review_receipts WHERE project=? AND call_id=?", (project, body["id"])).fetchone():
                raise ContractError("administrator call already has an accepted review receipt")
            result = retry_call(runtime, project, body["id"], body["reason"])
        elif body["action"] == "defer_history":
            result = runtime.history_defer(project, body["id"], body["reason"], body["expected_governance_epoch"])
        else:
            if not _history_available(row):
                raise ContractError("history job is running, cancelled or explicitly paused; use its explicit resume workflow")
            runtime.db.execute("UPDATE history_jobs SET host_epoch=?,state='materialized',review_enabled=0,owner=NULL,"
                "lease_until=NULL,updated=?,last_result=? WHERE project=? AND id=?",
                (runtime.host_epoch(), time.time(), encode({"reason": "manual_review_available",
                 "reauthorization_reason": body["reason"], "models_started": False}), project, body["id"]))
            result = {"status": "history_reauthorized", "job_id": body["id"]}
        from .admin_control import control_status
        response = {"request_id": body["request_id"], "status": result["status"],
            "recovery_action": body["action"], "result": result, "models_started": False,
            "knowledge_published": False, "settings": control_status(runtime, project, details=details),
            "launch": {"status": "not_requested"}}
        runtime.db.execute("INSERT INTO admin_control_receipts VALUES(?,?,?,?,?,?,?)",
                           (*identity, fingerprint, encode(response), time.time()))
        return response
