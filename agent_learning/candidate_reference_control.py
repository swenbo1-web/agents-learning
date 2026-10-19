"""Same-origin operator controls, separate from worker tools and publication."""
from __future__ import annotations

import json
import time


def apply_reference_control(runtime, project, body):
    from .runtime import ContractError, digest, encode
    from .candidate_references import configure, grant_history, resolve_feedback
    required = {"request_id", "expected_session_epoch", "expected_governance_epoch", "expected_version", "enabled"}
    if (not isinstance(body, dict) or not required <= body.keys()
            or body.keys() - required - {"job_id", "candidate_id", "reason"} or not isinstance(body["enabled"], bool)):
        raise ContractError("invalid candidate reference control request")
    for field in ("expected_session_epoch", "expected_governance_epoch", *(() if "candidate_id" in body else ("expected_version",))):
        if isinstance(body[field], bool) or not isinstance(body[field], int) or body[field] < 0:
            raise ContractError("invalid candidate reference control version")
    if not isinstance(body["request_id"], str) or not 1 <= len(body["request_id"]) <= 200:
        raise ContractError("invalid candidate reference request identity")
    if "job_id" in body and (not isinstance(body["job_id"], str) or not 1 <= len(body["job_id"]) <= 200):
        raise ContractError("invalid candidate reference history identity")
    signature = digest(["candidate-reference", body])
    with runtime.transaction():
        runtime.assert_session_epoch(body["expected_session_epoch"])
        runtime.assert_host_epoch(body["expected_governance_epoch"])
        key = (project, runtime._session_scope(), runtime.session_epoch(), body["request_id"])
        old = runtime.db.execute("SELECT request_digest,response FROM admin_control_receipts WHERE project=? AND host_scope=? AND session_epoch=? AND request_id=?", key).fetchone()
        if old:
            if old["request_digest"] != signature:
                raise ContractError("candidate reference request identity reused")
            return json.loads(old["response"])
        if "candidate_id" in body:
            if "job_id" in body or body["enabled"] is not True:
                raise ContractError("candidate concern resolution cannot change reference access")
            result = resolve_feedback(runtime, project, body["candidate_id"], body["expected_version"], body.get("reason"))
        elif "job_id" in body:
            result = grant_history(runtime, project, body["job_id"], body["enabled"], body["expected_version"])
        else:
            result = configure(runtime, project, body["enabled"], body["expected_version"])
        from .admin_control import control_status
        response = {"request_id": body["request_id"], "action": "candidate_reference", "status": "configured",
                    "result": result, "settings": control_status(runtime, project, details=False)}
        runtime.db.execute("INSERT INTO admin_control_receipts VALUES(?,?,?,?,?,?,?)",
                           (*key, signature, encode(response), time.time()))
        return response
