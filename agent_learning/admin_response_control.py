"""Explicit local revalidation through the existing operator capability."""
from __future__ import annotations


def apply_response_control(runtime, project, body):
    from .runtime import ContractError
    from .admin_response_store import recheck
    from .admin_control import control_status
    fields = {"request_id", "call_id", "expected_version", "expected_session_epoch", "expected_governance_epoch", "confirmed"}
    if not isinstance(body, dict) or set(body) != fields:
        raise ContractError("invalid local response recheck request")
    result = recheck(runtime, project, body["call_id"], **{key: value for key, value in body.items() if key != "call_id"})
    return {"request_id": body["request_id"], "status": result["status"], "action": "response_recheck",
            "result": result, "settings": control_status(runtime, project, details=False)}


def apply_history_response_control(runtime, project, body):
    """Keep a retrievable UI receipt after the independent local recovery lease."""
    import json
    import time
    from .runtime import ContractError, digest, encode
    from .admin_control import control_status
    from .history_response_recovery import recheck
    fields = {"request_id", "call_id", "expected_version", "expected_session_epoch", "expected_governance_epoch",
              "expected_job_version", "acknowledge_boundary", "reason", "allow_publication", "confirmed"}
    if not isinstance(body, dict) or set(body) != fields:
        raise ContractError("invalid historical response recheck request")
    if (not isinstance(body["request_id"], str) or not 1 <= len(body["request_id"]) <= 200
            or any(type(body[key]) is not int or body[key] < 0
                   for key in ("expected_session_epoch", "expected_governance_epoch"))):
        raise ContractError("invalid historical response recheck identity")
    signature = digest(["history-response-recheck", body])
    key = (project, runtime._session_scope(), body["expected_session_epoch"], body["request_id"])

    def previous():
        runtime.assert_session_epoch(body["expected_session_epoch"])
        runtime.assert_host_epoch(body["expected_governance_epoch"])
        row = runtime.db.execute("SELECT request_digest,response FROM admin_control_receipts "
            "WHERE project=? AND host_scope=? AND session_epoch=? AND request_id=?", key).fetchone()
        if row:
            if row["request_digest"] != signature:
                raise ContractError("historical response recheck request identity reused")
            return json.loads(row["response"])
        return None

    with runtime.transaction():
        old = previous()
        if old:
            return old
    # The core must commit its own lease before acceptance. Never wrap it in a
    # control transaction or start a background reviewer here.
    result = recheck(runtime, project, body["call_id"], **{key: value for key, value in body.items() if key != "call_id"})
    with runtime.transaction():
        old = previous()
        if old:
            return old
        answer = {"request_id": body["request_id"], "status": result["status"], "action": "history_response_recheck",
                  "result": result, "settings": control_status(runtime, project, details=False)}
        runtime.db.execute("INSERT INTO admin_control_receipts VALUES(?,?,?,?,?,?,?)",
                           (*key, signature, encode(answer), time.time()))
        return answer
