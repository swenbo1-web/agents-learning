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
