"""Bounded public administrator replies and explicit local decision recovery.

Paid-call records are immutable accounting evidence here. A recovery uses its own
short lease and the original frozen source/executor contract; it never calls a
provider, grants a retry, or changes the original call's state/usage.
"""
from __future__ import annotations

import contextlib
import json
import re
import time
import uuid

from .runtime import ContractError, clean, digest, encode

MAX_RESPONSE_BYTES = 60000
MAX_RESERVED_BYTES = 2000000
_FIELDS = {"call_id", "owner", "host_epoch", "packet", "selected", "evidence_signature",
           "review_signature", "scan_signature", "evidence_wait_signature", "history_job_id",
           "summary_context", "phase_context", "history_owner", "publication_enabled_at_reservation",
           "publication_mode_at_reservation", "history_reference_authorized", "history_snapshot", "terminal_observation",
           "history_snapshot_unavailable"}
_CANDIDATE = {"problem_id", "problem_revision", "title", "content", "category", "applicability",
              "limitations", "evidence_refs", "validation_level", "metadata", "knowledge_kind"}
_PUBLIC = {"decision", "reason", "candidate", "candidates", "missing_evidence", "proposal", "items"}
_PROPOSAL = {"action", "knowledge_id", "expected_revision"}
_CODES = {"candidate_batch_ambiguous", "candidate_schema_invalid", "phase_aggregate_ambiguous",
          "response_schema_invalid", "execution_plan_changed", "reservation_too_large", "response_identity_changed"}
_PATH = re.compile(r"^(?:response(?:\.(?:items|candidate|candidates|problem_id|evidence_refs|validation_level|proposal|action|knowledge_id|missing_evidence|reason)(?:\[[0-9]{1,2}\])?)*|context(?:\.(?:phase|sources|executor))?)$")


def install(runtime):
    runtime.db.execute("CREATE TABLE IF NOT EXISTS admin_response_records("
        "call_id TEXT PRIMARY KEY,project TEXT NOT NULL,host_scope TEXT NOT NULL,session_epoch INTEGER NOT NULL,"
        "governance_epoch INTEGER NOT NULL,plan_digest TEXT NOT NULL,reserved TEXT NOT NULL,reserved_digest TEXT NOT NULL,response TEXT,"
        "response_digest TEXT,state TEXT NOT NULL,version INTEGER NOT NULL,rejection_code TEXT,rejection_path TEXT,"
        "result TEXT,created REAL NOT NULL,updated REAL NOT NULL)")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS admin_response_project ON "
                       "admin_response_records(project,host_scope,state,created)")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS admin_response_rechecks("
        "project TEXT NOT NULL,host_scope TEXT NOT NULL,request_id TEXT NOT NULL,request_digest TEXT NOT NULL,"
        "call_id TEXT NOT NULL,state TEXT NOT NULL,owner TEXT NOT NULL,lease_until REAL NOT NULL,response TEXT,"
        "created REAL NOT NULL,updated REAL NOT NULL,PRIMARY KEY(project,host_scope,request_id))")


def _transaction(runtime):
    return contextlib.nullcontext() if runtime.db.in_transaction else runtime.transaction()


def _exists(runtime):
    return bool(runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name='admin_response_records'").fetchone())


def rejection(exc):
    """Known machine codes only. Never persist arbitrary exception messages/keys."""
    from .admin import ResponseRejected, ReviewStateChanged
    if isinstance(exc, ResponseRejected):
        return {"code": exc.code if exc.code in _CODES else "local_decision_rejected",
                "path": exc.path if isinstance(exc.path, str) and len(exc.path) <= 160 and _PATH.fullmatch(exc.path) else "response"}
    if isinstance(exc, ReviewStateChanged):
        return {"code": "review_context_changed", "path": "context"}
    known = {
        "invalid administrator response": ("response_schema_invalid", "response"),
        "invalid missing evidence requirements": ("missing_evidence_invalid", "response.missing_evidence"),
        "history response exceeds total output budget": ("response_too_large", "response"),
        "administrator changed review identity or revision": ("candidate_identity_changed", "response.candidate.problem_id"),
        "historical public text must remain reported": ("history_validation_level_invalid", "response.candidate.validation_level"),
        "candidate references evidence outside the review packet": ("candidate_evidence_outside_packet", "response.candidate.evidence_refs"),
        "no observed completed tool success": ("candidate_tool_success_missing", "response.candidate.validation_level"),
        "source is absent or outside this problem": ("candidate_source_missing", "response.candidate.evidence_refs"),
        "source belongs to another task revision": ("candidate_source_identity_changed", "response.candidate.evidence_refs"),
        "source task snapshot is missing": ("candidate_task_snapshot_missing", "context.sources"),
        "non-candidate response must use candidate=null": ("candidate_decision_ambiguous", "response.candidate"),
        "work summary cannot propose knowledge governance": ("summary_governance_disallowed", "response.proposal"),
        "invalid administrator governance proposal": ("proposal_invalid", "response.proposal"),
        "governance action does not match decision": ("proposal_decision_ambiguous", "response.proposal.action"),
        "governance target is outside the visible review context": ("proposal_target_outside_packet", "response.proposal.knowledge_id"),
        "governance decision requires a proposal": ("proposal_required", "response.proposal"),
        "phase review snapshot or output changed": ("phase_snapshot_changed", "context.phase"),
        "phase review must return exactly one result per visible case": ("phase_items_mismatch", "response.items"),
        "phase case evidence or comparison changed": ("phase_source_changed", "context.sources"),
        "duplicate phase evidence or comparison changed": ("phase_source_changed", "context.sources"),
        "phase review context changed": ("phase_context_changed", "context.phase"),
        "local recheck ownership or lease changed": ("local_recheck_lease_changed", "context"),
        "active administrator work conflicts with local recheck": ("local_recheck_concurrent_work", "context"),
        "local recheck execution plan changed": ("execution_plan_changed", "context.executor"),
        "local recheck response fingerprint changed": ("response_snapshot_changed", "response"),
        "local recheck reservation fingerprint changed": ("reservation_snapshot_changed", "context"),
    }
    code, path = known.get(str(exc), ("local_decision_rejected", "response"))
    return {"code": code, "path": path}


def _public_shape(value, *, item=False):
    """Do not persist unknown model fields (including reasoning/analysis blocks)."""
    if not isinstance(value, dict) or set(value) - (_PUBLIC | ({"problem_id"} if item else set())):
        return False
    if any(key in value and not isinstance(value[key], str) for key in ("decision", "reason", "problem_id")):
        return False
    if "missing_evidence" in value and (not isinstance(value["missing_evidence"], list)
            or len(value["missing_evidence"]) > 8 or not all(isinstance(text, str) for text in value["missing_evidence"])):
        return False
    if "items" in value:
        if item or not isinstance(value["items"], list) or len(value["items"]) > 8:
            return False
        if not all(_public_shape(child, item=True) for child in value["items"]):
            return False
    candidates = ([value["candidate"]] if value.get("candidate") is not None else [])
    if "candidates" in value:
        if not isinstance(value["candidates"], list) or len(value["candidates"]) > 5:
            return False
        candidates += value["candidates"]
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) - _CANDIDATE:
            return False
        if any(not isinstance(text, str) for key, text in candidate.items()
               if key not in {"metadata", "problem_revision", "evidence_refs"}):
            return False
        if "problem_revision" in candidate and type(candidate["problem_revision"]) is not int:
            return False
        if "evidence_refs" in candidate and (not isinstance(candidate["evidence_refs"], list)
                or not all(isinstance(ref, str) for ref in candidate["evidence_refs"])):
            return False
        metadata = candidate.get("metadata", {})
        if (not isinstance(metadata, dict) or set(metadata) - {"tool", "tool_version", "error_code", "environment", "verification"}
                or not all(isinstance(text, str) for text in metadata.values())):
            return False
    proposal = value.get("proposal")
    return proposal is None or (isinstance(proposal, dict) and not set(proposal) - _PROPOSAL
        and all(item is None or isinstance(item, str) or type(item) is int for item in proposal.values()))


def capture(runtime, project, reserved, response):
    """Persist only a cleaned public reply and minimal reservation, never commands."""
    from .admin import ResponseRejected
    plan = runtime.admin_execution_plan(project, reserved["call_id"])
    if plan["executor"] != reserved["provider_plan"]:
        raise ResponseRejected("execution_plan_changed", "context.executor")
    frozen = {key: reserved[key] for key in _FIELDS if key in reserved}
    # phase_context is the same frozen packet, not a second source snapshot.
    if frozen.get("phase_context"):
        frozen["phase_context"] = True
    if isinstance(frozen.get("selected"), dict):
        frozen["selected"] = {key: frozen["selected"].get(key) for key in ("id", "revision", "source_task_updated")}
    if isinstance(frozen.get("summary_context"), dict):
        frozen["summary_context"] = {key: frozen["summary_context"].get(key) for key in ("source_digest", "instruction_version")}
    reserved_body = encode(clean(frozen))
    response_body = encode(clean(response)) if _public_shape(response) else None
    if len(reserved_body.encode("utf-8")) > MAX_RESERVED_BYTES:
        raise ResponseRejected("reservation_too_large", "context")
    if response_body is not None and len(response_body.encode("utf-8")) > MAX_RESPONSE_BYTES:
        response_body = None
    now = time.time()
    state = "captured" if response_body is not None else "unavailable"
    with _transaction(runtime):
        install(runtime)
        prior = runtime.db.execute("SELECT * FROM admin_response_records WHERE call_id=?", (reserved["call_id"],)).fetchone()
        if prior:
            if prior["response"] != response_body or prior["reserved"] != reserved_body or prior["plan_digest"] != digest(plan["executor"]):
                raise ResponseRejected("response_identity_changed", "response")
            return
        runtime.db.execute("INSERT INTO admin_response_records VALUES(?,?,?,?,?,?,?,?,?,?,?,1,NULL,NULL,NULL,?,?)",
            (reserved["call_id"], project, runtime._session_scope(), plan["executor"]["session_epoch"],
             reserved["host_epoch"], digest(plan["executor"]), reserved_body, digest(json.loads(reserved_body)), response_body,
             digest(json.loads(response_body)) if response_body else None, state, now, now))


def reject(runtime, call_id, exc):
    if not _exists(runtime):
        return None
    detail = rejection(exc)
    with _transaction(runtime):
        runtime.db.execute("UPDATE admin_response_records SET state=CASE WHEN response IS NULL THEN 'unavailable' ELSE 'rejected' END,"
            "version=version+1,rejection_code=?,rejection_path=?,updated=? WHERE call_id=? AND state!='accepted'",
            (detail["code"], detail["path"], time.time(), call_id))
    return detail


def mark_accepted(runtime, call_id, result):
    runtime.db.execute("UPDATE admin_response_records SET state='accepted',version=version+1,rejection_code=NULL,"
                       "rejection_path=NULL,result=?,updated=? WHERE call_id=?",
                       (encode(clean(result)), time.time(), call_id))


def _record(runtime, project, call_id):
    if not _exists(runtime):
        return None
    return runtime.db.execute("SELECT * FROM admin_response_records WHERE project=? AND host_scope=? AND call_id=?",
                              (project, runtime._session_scope(), call_id)).fetchone()


def _terminal(runtime, row):
    call = runtime.db.execute("SELECT * FROM admin_calls WHERE project=? AND id=?", (row["project"], row["call_id"])).fetchone()
    try:
        usage = json.loads(call["usage"] or "{}") if call else {}
        execution = usage.get("execution") or {}
    except (ValueError, TypeError, AttributeError):
        return False
    return bool(call and call["state"] == "failed" and call["retry_allowed"] == 0
                and call["host_epoch"] == row["governance_epoch"]
                and isinstance(execution, dict) and execution.get("terminal_observed") is True
                and type(execution.get("returncode")) is int and execution["returncode"] == 0)


def status(runtime, project, call_id):
    row = _record(runtime, project, call_id)
    if not row:
        return {"call_id": call_id, "available": False, "can_recheck": False, "version": None,
                "reason": "public_response_unavailable", "models_started": False}
    ready = row["state"] in {"captured", "rejected"} and row["response"] is not None and _terminal(runtime, row)
    boundary = row["session_epoch"] == runtime.session_epoch() and row["governance_epoch"] == runtime.host_epoch()
    active = runtime.db.execute("SELECT 1 FROM admin_response_rechecks WHERE project=? AND call_id=? "
        "AND state='validating' AND lease_until>? LIMIT 1", (project, call_id, time.time())).fetchone()
    historical = bool(json.loads(row["reserved"]).get("history_job_id"))
    historical_status = None
    if historical:
        from .history_response_recovery import status as history_recovery_status
        historical_status = history_recovery_status(runtime, project, call_id)
    return {"call_id": call_id, "available": row["response"] is not None, "state": row["state"],
            "version": row["version"], "can_recheck": bool(ready and boundary and not active and not historical),
            **({"reason": "history_run_reapproval_required"} if historical else {}),
            **({"history_recovery": historical_status} if historical else {}),
            "rejection": {"code": row["rejection_code"], "path": row["rejection_path"]} if row["rejection_code"] else None,
            "models_started": False, "local_recheck": True}


def validate_recheck(runtime, reserved):
    """Called before and after acceptance inside its write transaction."""
    if not runtime.db.in_transaction:
        raise ContractError("local recheck requires an existing write transaction")
    lease = reserved.get("_local_recheck") or {}
    row = runtime.db.execute("SELECT * FROM admin_response_rechecks WHERE project=? AND host_scope=? AND request_id=?",
        (reserved["provider_plan"]["project"], runtime._session_scope(), lease.get("request_id"))).fetchone()
    record = _record(runtime, reserved["provider_plan"]["project"], reserved["call_id"])
    if (not row or not record or row["call_id"] != reserved["call_id"] or row["owner"] != lease.get("owner")
            or row["state"] != "validating" or row["lease_until"] <= time.time() or not _terminal(runtime, record)):
        raise ContractError("local recheck ownership or lease changed")
    if runtime.db.execute("SELECT 1 FROM admin_calls WHERE project=? AND state IN ('reserved','launch_intent','started') "
                          "AND lease_until>? LIMIT 1", (record["project"], time.time())).fetchone():
        raise ContractError("active administrator work conflicts with local recheck")
    runtime.assert_session_epoch(record["session_epoch"])
    runtime.assert_host_epoch(record["governance_epoch"])
    plan = runtime.admin_execution_plan(record["project"], record["call_id"])
    if digest(plan["executor"]) != record["plan_digest"] or plan["executor"] != reserved["provider_plan"]:
        raise ContractError("local recheck execution plan changed")
    return dict(record)


def recheck(runtime, project, call_id, *, request_id, expected_version, expected_session_epoch,
            expected_governance_epoch, confirmed=False):
    """Trusted CLI/UI entry only. No provider launcher, token reservation or retry."""
    from .admin import _accept_response
    if (confirmed is not True or type(expected_version) is not int or expected_version < 1
            or any(type(value) is not int or value < 0 for value in (expected_session_epoch, expected_governance_epoch))
            or any(not isinstance(value, str) or not value.strip() or len(value) > 200 for value in (call_id, request_id))):
        raise ContractError("invalid explicit local recheck request")
    identity = (project, runtime._session_scope(), request_id)
    request_digest = digest([call_id, expected_version, expected_session_epoch, expected_governance_epoch])
    owner, now = str(uuid.uuid4()), time.time()
    with runtime.transaction():
        runtime.assert_session_epoch(expected_session_epoch)
        runtime.assert_host_epoch(expected_governance_epoch)
        previous = runtime.db.execute("SELECT * FROM admin_response_rechecks WHERE project=? AND host_scope=? AND request_id=?", identity).fetchone() if _exists(runtime) else None
        if previous:
            if previous["request_digest"] != request_digest:
                raise ContractError("local recheck request identity changed")
            if previous["response"]:
                return json.loads(previous["response"])
            raise ContractError("local recheck already running or interrupted; inspect before a new request")
        record = _record(runtime, project, call_id)
        if record is None or record["response"] is None:
            raise ContractError("public response unavailable; model must not be rerun implicitly")
        if record["version"] != expected_version:
            raise ContractError("local recheck response changed; refresh before confirming")
        if record["session_epoch"] != expected_session_epoch or record["governance_epoch"] != expected_governance_epoch:
            raise ContractError("local recheck response belongs to an old account or governance epoch")
        if record["state"] == "accepted":
            return {"status": "already_accepted", "call_id": call_id, "local_recheck": True, "models_started": False,
                    "result": json.loads(record["result"] or "{}")}
        if json.loads(record["reserved"]).get("history_job_id"):
            raise ContractError("history_run_reapproval_required: retained history output requires its explicit batch workflow")
        if record["state"] not in {"captured", "rejected"} or not _terminal(runtime, record):
            raise ContractError("local recheck requires a failed call with a successful observed terminal response")
        if runtime.db.execute("SELECT 1 FROM admin_calls WHERE project=? AND state IN ('reserved','launch_intent','started') "
                              "AND lease_until>? LIMIT 1", (project, now)).fetchone():
            raise ContractError("wait for active administrator work before local recheck")
        if runtime.db.execute("SELECT 1 FROM admin_response_rechecks WHERE project=? AND state='validating' AND lease_until>? LIMIT 1",
                              (project, now)).fetchone():
            raise ContractError("local recheck already running")
        runtime.db.execute("INSERT INTO admin_response_rechecks VALUES(?,?,?,?,?,'validating',?,?,NULL,?,?)",
                           (*identity, request_digest, call_id, owner, now + 30, now, now))
    # The claim is durable and independent from the short acceptance transaction.
    try:
        with runtime.transaction():
            record = _record(runtime, project, call_id)
            response = json.loads(record["response"])
            if digest(response) != record["response_digest"]:
                raise ContractError("local recheck response fingerprint changed")
            reserved = json.loads(record["reserved"])
            if digest(reserved) != record["reserved_digest"]:
                raise ContractError("local recheck reservation fingerprint changed")
            reserved["provider_plan"] = runtime.admin_execution_plan(project, call_id)["executor"]
            reserved["_local_recheck"] = {"request_id": request_id, "owner": owner}
            validate_recheck(runtime, reserved)
            result = _accept_response(runtime, project, reserved, response)
            validate_recheck(runtime, reserved)
            result["local_recheck"] = True
            mark_accepted(runtime, call_id, result)
            from .admin_queue import clear_recovered_executor_failure
            clear_recovered_executor_failure(runtime, project, reserved["provider_plan"], call_id)
            answer = {"status": "accepted", "call_id": call_id, "local_recheck": True,
                      "models_started": False, "result": result}
            runtime.db.execute("UPDATE admin_response_rechecks SET state='accepted',response=?,updated=? "
                "WHERE project=? AND host_scope=? AND request_id=? AND owner=?", (encode(clean(answer)), time.time(), *identity, owner))
            return answer
    except (ValueError, TypeError, OSError) as exc:
        detail = rejection(exc)
        with runtime.transaction():
            reject(runtime, call_id, exc)
            answer = {"status": "rejected", "call_id": call_id, "local_recheck": True,
                      "models_started": False, "rejection": detail}
            runtime.db.execute("UPDATE admin_response_rechecks SET state='rejected',response=?,updated=? "
                "WHERE project=? AND host_scope=? AND request_id=? AND owner=?", (encode(answer), time.time(), *identity, owner))
        return answer
