"""Project/host scoped recovery decisions; never launch models or publish knowledge."""
from __future__ import annotations

import json
import re
import time

from .runtime import ContractError, digest, encode
from .admin_lifecycle import RETRYABLE_STATES, retry_call
from .candidate_review import BUDGET_PAUSES


def _version(row):
    return digest(dict(row))


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


def recovery_status(runtime, project):
    """Bounded read view. Observations do not prove current-session Hook health."""
    db, scope = runtime.db, runtime._session_scope()
    capture = db.execute(
        "SELECT COUNT(DISTINCT conversation) AS conversations,MAX(created) AS last_event_at FROM events "
        "WHERE project=? AND agent IS NULL AND type IN ('SessionStart','UserPromptSubmit','Stop','PreToolUse','PostToolUse')",
        (project,)).fetchone()
    memory_count = db.execute("SELECT COUNT(*) FROM work_memories WHERE project=?", (project,)).fetchone()[0]
    calls = []
    identities = db.execute(
        "SELECT a.id FROM admin_calls a JOIN admin_execution_plans p ON p.call_id=a.id AND p.project=a.project "
        "WHERE a.project=? AND p.host_scope=? AND a.retry_allowed=0 "
        "AND a.state IN ('failed','timeout_unknown','account_changed_unknown','orphaned_unknown') "
        "AND NOT EXISTS(SELECT 1 FROM review_receipts r WHERE r.project=a.project AND r.call_id=a.id) "
        "ORDER BY a.created DESC,a.id LIMIT 20", (project, scope)).fetchall()
    for item in identities:
        row = _call(runtime, project, item[0])
        try:
            usage = json.loads(row["usage"] or "{}")
            execution = usage.get("execution") or {}
            from .admin_diagnostics import safe_diagnostic
            diagnostic = safe_diagnostic(execution.get("provider_diagnostic")) if isinstance(execution, dict) else None
            diagnostic = diagnostic or safe_diagnostic(usage.get("provider_diagnostic"))
        except (ValueError, TypeError, AttributeError):
            usage, execution, diagnostic = {}, {}, None
        calls.append({"id": row["id"], "version": _version(row), "state": row["state"],
            "created": row["created"], "reported_turns": usage.get("reported_turns"),
            "usage_known": bool(usage.get("token_totals")),
            "reason": diagnostic["reason_code"] if diagnostic else (
                "reviewer_process_failed" if isinstance(execution, dict) and type(execution.get("returncode")) is int
                and execution["returncode"] != 0 else "historical_failure_details_unavailable"),
            "can_retry": row["state"] in RETRYABLE_STATES})
    history = []
    for item in db.execute("SELECT id,plan FROM history_jobs WHERE project=? ORDER BY created DESC,id LIMIT 10", (project,)):
        row = _history(runtime, project, item["id"])
        plan = json.loads(item["plan"])
        stats = plan.get("stats") or {}
        pending = plan.get("pending") or []
        unsupported = sum(isinstance(value, dict) and value.get("reason") == "unparsed_file_type" for value in pending)
        progress = runtime.history_status(project, row["id"])["job"]
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
        history.append({"id": row["id"], "version": _version(row), "state": row["state"],
            "epoch_matches": row["host_epoch"] == runtime.host_epoch(),
            "review_enabled": bool(row["review_enabled"]), "remaining_calls": row["remaining_calls"],
            "files_read": stats.get("files_read", 0), "incomplete": bool(plan.get("incomplete")),
            "pending": plan.get("pending_count", 0), "unsupported": unsupported,
            "conversation_sources": sum(isinstance(value, dict) and value.get("kind") == "conversation"
                                        for value in plan.get("extra_sources", [])),
            "can_reauthorize": _history_available(row) and row["host_epoch"] != runtime.host_epoch(),
            "counts": progress["counts"], "review_request": review_request,
            "resume_required": progress["resume_required"],
            "can_review": _history_review_available(row) and row["host_epoch"] == runtime.host_epoch()
                and not active_request and progress["resume"]["explicit_run_required"],
            "publication_automatic": False})
    return {"capture": {**dict(capture), "work_memories": memory_count, "current_hook": "unknown",
                "note": "Existing conversations need a host reload and a fresh supported event; archived files are not chat history."},
            "blocked_reviews": calls, "history_jobs": history, "models_started": False}


def apply_recovery(runtime, project, body):
    """Explicit, versioned, idempotent permission update. No wake or model calls."""
    required = {"request_id", "expected_policy_version", "expected_session_epoch", "expected_governance_epoch",
                "action", "id", "version", "reason", "confirmed"}
    if (not isinstance(body, dict) or set(body) != required or body["confirmed"] is not True
            or not isinstance(body["action"], str) or body["action"] not in {"retry_call", "reauthorize_history"}
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
            "knowledge_published": False, "settings": control_status(runtime, project),
            "launch": {"status": "not_requested"}}
        runtime.db.execute("INSERT INTO admin_control_receipts VALUES(?,?,?,?,?,?,?)",
                           (*identity, fingerprint, encode(response), time.time()))
        return response
