"""Explicit, durable, one-shot history review requests from the local UI."""
from __future__ import annotations

import json
from pathlib import Path
import time

from .runtime import ContractError, digest, encode


def _identity(runtime, project, request_id):
    return project, runtime._session_scope(), runtime.session_epoch(), request_id


def request_history_review(runtime, project, body):
    from .admin_control import control_status, request_receipt
    from .admin_recovery import _history, _version, _history_review_available
    fields = {"request_id", "job_id", "version", "confirmed", "max_calls", "max_seconds",
              "expected_policy_version", "expected_settings_version", "expected_project_budget_version",
              "expected_session_epoch", "expected_governance_epoch"}
    if not isinstance(body, dict) or set(body) != fields or body["confirmed"] is not True:
        raise ContractError("explicit confirmed history review fields required")
    if any(not isinstance(body[k], str) or not body[k].strip() or len(body[k]) > 200 for k in ("request_id", "job_id", "version")):
        raise ContractError("invalid history review identity")
    if any(type(body[k]) is not int or body[k] < 0 for k in fields if k.startswith("expected_")):
        raise ContractError("invalid history review versions")
    if type(body["max_calls"]) is not int or not 1 <= body["max_calls"] <= 20:
        raise ContractError("history review max_calls must be 1..20")
    if type(body["max_seconds"]) is not int or not 1 <= body["max_seconds"] <= 600:
        raise ContractError("history review max_seconds must be 1..600")
    identity, fingerprint = _identity(runtime, project, body["request_id"]), digest(["history-review", body])
    with runtime.transaction():
        runtime.assert_session_epoch(body["expected_session_epoch"])
        runtime.assert_host_epoch(body["expected_governance_epoch"])
        prior = runtime.db.execute("SELECT input_digest FROM history_review_requests WHERE project=? AND host_scope=? "
            "AND session_epoch=? AND request_id=?", identity).fetchone()
        if prior:
            if prior[0] != fingerprint:
                raise ContractError("history review identity reused with different input")
            return request_receipt(runtime, project, body["request_id"])["response"]
        if runtime.db.execute("SELECT 1 FROM admin_control_receipts WHERE project=? AND host_scope=? AND session_epoch=? AND request_id=?", identity).fetchone():
            raise ContractError("administrator request identity already used")
        status = control_status(runtime, project)
        if (status["settings_version"] != body["expected_settings_version"]
                or status["policy"]["policy_version"] != body["expected_policy_version"]
                or status["project_budget_policy"]["version"] != body["expected_project_budget_version"]):
            raise ContractError("history review settings changed; refresh before starting")
        row = _history(runtime, project, body["job_id"])
        if row is None or _version(row) != body["version"] or not _history_review_available(row):
            raise ContractError("history batch changed or is unavailable")
        if row["host_epoch"] != runtime.host_epoch():
            raise ContractError("history batch requires explicit reauthorization")
        if runtime.db.execute("SELECT 1 FROM history_review_requests q JOIN host_scopes h "
            "ON h.scope=q.host_scope AND h.epoch=q.session_epoch WHERE q.project=? AND q.job_id=? "
            "AND q.state IN ('reserved','running') AND q.lease_until>? "
            "AND json_extract(q.body,'$.expected_governance_epoch')=?",
            (project, row["id"], time.time(), runtime.host_epoch())).fetchone():
            raise ContractError("history batch already has an active review request")
        plan = runtime.resolve_admin_executor(project, require_enabled=False)
        workspace = runtime.db.execute("SELECT workspace FROM history_jobs WHERE id=? AND project=?", (row["id"], project)).fetchone()[0]
        if not Path(workspace).is_dir():
            raise ContractError("history workspace unavailable")
        admission = status["admission"]["admission_status"]
        if admission != "available":
            raise ContractError("history review admission unavailable: " + admission)
        payload = dict(body, executor_signature=plan["executor_signature"], workspace=workspace)
        now = time.time()
        runtime.db.execute("INSERT INTO history_review_requests VALUES(?,?,?,?,?,?,?,'reserved',?,?,?,?)",
            (*identity, fingerprint, row["id"], encode(payload), now, now, now + body["max_seconds"] + 60, "{}"))
        response = {"request_id": body["request_id"], "status": "accepted", "history_review": {"job_id": row["id"],
            "max_calls": body["max_calls"], "max_seconds": body["max_seconds"], "publication_automatic": False},
            "launch": {"status": "unconfirmed"}, "settings": control_status(runtime, project)}
        runtime.db.execute("INSERT INTO admin_control_receipts VALUES(?,?,?,?,?,?,?)", (*identity, fingerprint, encode(response), now))
    try:
        runtime._launch_background(workspace, "history-review-worker", ["--request-id", body["request_id"]])
        response["launch"] = {"status": "dispatched"}
    except (OSError, ValueError):
        response.update(status="launch_failed", launch={"status": "launch_failed"})
        with runtime.transaction():
            runtime.db.execute("UPDATE history_review_requests SET state='launch_failed',updated=?,lease_until=NULL "
                "WHERE project=? AND host_scope=? AND session_epoch=? AND request_id=? AND state='reserved'", (time.time(), *identity))
    with runtime.transaction():
        runtime.db.execute("UPDATE admin_control_receipts SET response=? WHERE project=? AND host_scope=? AND session_epoch=? AND request_id=?",
                           (encode(response), *identity))
    return response


def run_history_review(runtime, project, request_id):
    from .admin_control import control_state
    from .admin_recovery import _history, _version
    from .admin_usage import project_budget_policy
    identity = _identity(runtime, project, request_id)
    with runtime.transaction():
        row = runtime.db.execute("SELECT * FROM history_review_requests WHERE project=? AND host_scope=? AND session_epoch=? AND request_id=?", identity).fetchone()
        if not row or row["state"] != "reserved" or (row["lease_until"] or 0) <= time.time():
            return {"status": "not_started", "reason": "history_review_request_unavailable"}
        body = json.loads(row["body"])
        try:
            runtime.assert_session_epoch(body["expected_session_epoch"])
            runtime.assert_host_epoch(body["expected_governance_epoch"])
            current_job = _history(runtime, project, body["job_id"])
            changed = (control_state(runtime, project)["version"] != body["expected_settings_version"]
                or runtime.admin_policy(project)["policy_version"] != body["expected_policy_version"]
                or project_budget_policy(runtime, project)["version"] != body["expected_project_budget_version"]
                or runtime.resolve_admin_executor(project, require_enabled=False)["executor_signature"] != body["executor_signature"]
                or current_job is None or _version(current_job) != body["version"])
        except ValueError:
            changed = True
        if changed:
            runtime.db.execute("UPDATE history_review_requests SET state='cancelled',updated=?,lease_until=NULL,result=? "
                "WHERE project=? AND host_scope=? AND session_epoch=? AND request_id=?",
                (time.time(), encode({"reason": "history_review_context_changed"}), *identity))
            return {"status": "not_started", "reason": "history_review_context_changed"}
        runtime.db.execute("UPDATE history_review_requests SET state='running',updated=?,lease_until=? "
            "WHERE project=? AND host_scope=? AND session_epoch=? AND request_id=?",
            (time.time(), time.time() + body["max_seconds"] + 30, *identity))
    try:
        status = runtime.history_run(project, body["job_id"], allow_review=True, max_calls=body["max_calls"],
                                     max_seconds=body["max_seconds"], expected_epoch=body["expected_governance_epoch"])
        job = status["job"]
        result = {"status": "completed", "job_state": job["state"], "counts": job["counts"],
                  "reason": (job.get("last_result") or {}).get("reason"), "publication_automatic": False}
    except (OSError, ValueError) as exc:
        result = {"status": "failed", "reason": type(exc).__name__, "automatic_retry": False}
    with runtime.transaction():
        runtime.db.execute("UPDATE history_review_requests SET state=?,updated=?,lease_until=NULL,result=? "
            "WHERE project=? AND host_scope=? AND session_epoch=? AND request_id=? AND state='running'",
            (result["status"], time.time(), encode(result), *identity))
    return result
