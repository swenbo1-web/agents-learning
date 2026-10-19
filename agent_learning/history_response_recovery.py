"""Explicit, narrowly scoped acceptance of a saved historical public reply.

The scope can accept one frozen reply. It cannot resume a history job, reserve a
model call, or authorize any candidate outside that reply. Job rows and original
paid-call ledgers are never changed by this module.
"""
from __future__ import annotations

from contextvars import ContextVar
import json
import re
import time
import uuid

from .runtime import ContractError, clean, digest, encode, metadata

_SCOPE = ContextVar("alp_history_reply_scope", default=None)


def install(runtime):
    runtime.db.execute("CREATE TABLE IF NOT EXISTS history_response_accepts("
        "project TEXT NOT NULL,host_scope TEXT NOT NULL,request_id TEXT NOT NULL,call_id TEXT NOT NULL,"
        "job_id TEXT NOT NULL,state TEXT NOT NULL,body TEXT NOT NULL,result TEXT,created REAL NOT NULL,updated REAL NOT NULL,"
        "PRIMARY KEY(project,host_scope,request_id))")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS history_response_call ON history_response_accepts(project,call_id,state,updated)")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS history_response_artifacts("
        "project TEXT NOT NULL,call_id TEXT NOT NULL,candidate_id TEXT NOT NULL,body_digest TEXT NOT NULL,"
        "PRIMARY KEY(project,call_id,candidate_id))")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS history_response_candidate ON history_response_artifacts(project,candidate_id)")


def _exists(view):
    return bool(view.db.execute("SELECT 1 FROM sqlite_master WHERE name='history_response_accepts'").fetchone())


def _job(view, project, job_id):
    row = view.db.execute("SELECT * FROM history_jobs WHERE project=? AND id=?", (project, job_id)).fetchone()
    if not row:
        raise ContractError("history_snapshot_unavailable")
    return dict(row)


def _selection(packet):
    primary = {"problem_id": packet["problem"]["id"], "revision": packet["problem"]["revision"],
               "events": [item["id"] for item in packet.get("events", [])]}
    related = [{"problem_id": item["problem_id"], "revision": item["revision"],
                "events": [event["id"] for event in item["events"]]} for item in packet.get("related_history", [])]
    if len(related) > 2 or sum(len(item["events"]) for item in [primary, *related]) > 48:
        raise ContractError("history_snapshot_unavailable")
    return [primary, *related]


def _source_digest(view, project, job_id, selection):
    """Only frozen SQLite sources: no project scan, model, or new source selection."""
    result = []
    for index, item in enumerate(selection):
        problem = view.db.execute("SELECT id,revision,summary,category,task_id,task_revision FROM problems "
            "WHERE project=? AND id=?", (project, item["problem_id"])).fetchone()
        unit = view.db.execute("SELECT u.id,u.material_id,u.unit_index,u.body,m.source_key,m.content_hash,s.materialized "
            "FROM history_units u JOIN history_materials m ON m.project=u.project AND m.id=u.material_id "
            "JOIN history_case_links l ON l.project=u.project AND l.unit_id=u.id AND l.problem_id=u.problem_id "
            "JOIN history_job_sources s ON s.job_id=l.job_id AND s.material_id=u.material_id "
            "WHERE u.project=? AND u.problem_id=? AND l.job_id=?", (project, item["problem_id"], job_id)).fetchone()
        if (not problem or not unit or problem["revision"] != item["revision"] or problem["task_id"] is not None
                or problem["task_revision"] is not None or not unit["materialized"]):
            raise ContractError("history_snapshot_changed")
        events = []
        for identity in item["events"]:
            row = view.db.execute("SELECT e.*,pe.role FROM events e JOIN problem_events pe ON pe.event=e.id "
                "WHERE e.project=? AND e.id=? AND pe.problem=? AND pe.role!='history_related'",
                (project, identity, item["problem_id"])).fetchone()
            if (not row or row["type"] != "HistoryImport" or row["outcome"] != "reported"
                    or any(row[key] is not None for key in ("task_id", "task_revision", "agent", "operation", "assignment_id"))):
                raise ContractError("history_snapshot_changed")
            payload = json.loads(row["payload"])
            source = payload.get("source") or {}
            if source.get("source_key") != unit["source_key"] or source.get("content_hash") != unit["content_hash"]:
                raise ContractError("history_snapshot_changed")
            events.append(dict(row))
        if not events:
            raise ContractError("history_snapshot_changed")
        result.append({"problem": dict(problem), "unit": dict(unit), "events": events})
    return digest(result)


def _comparison_sources(view, project, *, knowledge_id=None, revision=None, candidate_id=None):
    """Track only the compared objects' bounded refs and live operation health."""
    if knowledge_id is not None:
        source = ("FROM knowledge_sources s LEFT JOIN problems p ON p.project=s.project AND p.id=s.problem_id "
            "LEFT JOIN json_each(s.evidence_refs) refs LEFT JOIN events e ON e.project=s.project AND e.id=refs.value "
            "LEFT JOIN operations o ON o.project=e.project AND o.id=e.operation "
            "WHERE s.project=? AND s.knowledge_id=? AND s.revision=?")
        args = project, knowledge_id, revision
        identity = "s.candidate_id,s.problem_id,s.problem_revision,p.revision AS current_problem_revision"
    else:
        source = ("FROM candidates s LEFT JOIN problems p ON p.project=s.project AND p.id=s.problem "
            "LEFT JOIN json_each(s.body,'$.evidence_refs') refs LEFT JOIN events e ON e.project=s.project AND e.id=refs.value "
            "LEFT JOIN operations o ON o.project=e.project AND o.id=e.operation WHERE s.project=? AND s.id=?")
        args = project, candidate_id
        identity = "s.id AS candidate_id,s.problem AS problem_id,s.problem_revision,p.revision AS current_problem_revision"
    rows = view.db.execute("SELECT " + identity + ",refs.value AS ref,e.type,e.outcome,e.business_digest,"
        "e.task_id,e.task_revision,e.agent,e.assignment_id,o.state AS operation_state,o.conflict,o.decision "
        + source + " ORDER BY candidate_id,ref LIMIT 129", args).fetchall()
    if len(rows) > 128:
        raise ContractError("history_comparison_budget_exceeded")
    return [dict(row) for row in rows]


def _comparison_digest(view, project, packet):
    values = []
    knowledge = packet.get("related_knowledge", []) + ([packet["target_knowledge"]] if packet.get("target_knowledge") else [])
    for item in knowledge[:4]:
        identity, revision = item.get("knowledge_id", item.get("id")), item.get("revision")
        row = view.db.execute("SELECT k.body,a.revision FROM knowledge k LEFT JOIN active a "
            "ON a.project=k.project AND a.id=k.id WHERE k.project=? AND k.id=? AND k.revision=?",
            (project, identity, revision)).fetchone()
        feedback = [dict(value) for value in view.db.execute("SELECT * FROM knowledge_feedback WHERE project=? AND knowledge_id=? "
                    "AND revision=? ORDER BY problem_id LIMIT 129", (project, identity, revision))]
        if len(feedback) > 128:
            raise ContractError("history_comparison_budget_exceeded")
        if not row or row[1] != revision:
            raise ContractError("history_snapshot_changed")
        values.append([identity, revision, row[0], row[1], feedback,
                       _comparison_sources(view, project, knowledge_id=identity, revision=revision)])
    for item in packet.get("related_candidates", [])[:3]:
        row = view.db.execute("SELECT c.id,c.body,c.status,p.revision FROM candidates c JOIN problems p "
            "ON p.project=c.project AND p.id=c.problem WHERE c.project=? AND c.id=?",
            (project, item.get("candidate_id", item.get("id")))).fetchone()
        if not row:
            raise ContractError("history_snapshot_changed")
        values.append([list(row), _comparison_sources(view, project, candidate_id=row[0])])
    epoch = view.db.execute("SELECT revision FROM admin_case_epochs WHERE project=? AND problem=?",
                            (project, packet["problem"]["id"])).fetchone()
    values.append(epoch[0] if epoch else 0)
    return digest(values)


def freeze(runtime, project, reserved):
    job = _job(runtime, project, reserved["history_job_id"])
    packet = reserved["packet"]
    selected = _selection(packet)
    return {"format": 1, "job_id": job["id"], "job_signature": job["signature"], "job_plan_digest": digest(json.loads(job["plan"])),
            "origin_job_epoch": job["host_epoch"], "original_owner": reserved.get("history_owner"),
            "original_publication_authorized": bool(reserved.get("history_reference_authorized")
                                                     and reserved.get("publication_enabled_at_reservation")),
            "original_publication_mode": reserved.get("publication_mode_at_reservation"),
            "packet_digest": digest(packet), "selection": selected,
            "source_digest": _source_digest(runtime, project, job["id"], selected),
            "comparison_digest": _comparison_digest(runtime, project, packet)}


def _record(view, project, call_id):
    row = view.db.execute("SELECT * FROM admin_response_records WHERE project=? AND call_id=?", (project, call_id)).fetchone()
    return dict(row) if row else None


def _latest_jobs(view, project, selection):
    return [[item["problem_id"], (view.db.execute("SELECT l.job_id FROM history_case_links l JOIN history_jobs j ON j.id=l.job_id "
        "WHERE l.project=? AND l.problem_id=? ORDER BY j.created DESC,j.id DESC LIMIT 1",
        (project, item["problem_id"])).fetchone() or [None])[0]] for item in selection]


def _current_epochs(view, scope):
    session = view.db.execute("SELECT epoch FROM host_scopes WHERE scope=?", (scope,)).fetchone()
    governance = view.db.execute("SELECT epoch FROM host_state WHERE id=1").fetchone()
    return (session[0] if session else None, governance[0] if governance else None)


def _history_work_active(view, project):
    now = time.time()
    if view.db.execute("SELECT 1 FROM history_jobs WHERE project=? AND owner IS NOT NULL AND lease_until>? LIMIT 1",
                       (project, now)).fetchone():
        return True
    if not view.db.execute("SELECT 1 FROM sqlite_master WHERE name='history_review_requests'").fetchone():
        return False
    return bool(view.db.execute("SELECT 1 FROM history_review_requests q JOIN host_scopes h "
        "ON h.scope=q.host_scope AND h.epoch=q.session_epoch WHERE q.project=? AND q.state IN ('reserved','running') "
        "AND q.lease_until>? AND json_extract(q.body,'$.expected_governance_epoch')="
        "(SELECT epoch FROM host_state WHERE id=1) LIMIT 1", (project, now)).fetchone())


def _check_snapshot(view, project, record, *, comparisons=True):
    reserved = json.loads(record["reserved"])
    snapshot = reserved.get("history_snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("format") != 1:
        raise ContractError("history_snapshot_unavailable")
    if digest(reserved) != record["reserved_digest"] or snapshot["packet_digest"] != digest(reserved["packet"]):
        raise ContractError("history_snapshot_changed")
    response = json.loads(record["response"] or "null")
    if not isinstance(response, dict) or digest(response) != record["response_digest"]:
        raise ContractError("history_snapshot_changed")
    job = _job(view, project, snapshot["job_id"])
    if job["signature"] != snapshot["job_signature"] or digest(json.loads(job["plan"])) != snapshot["job_plan_digest"]:
        raise ContractError("history_snapshot_changed")
    if _source_digest(view, project, job["id"], snapshot["selection"]) != snapshot["source_digest"]:
        raise ContractError("history_snapshot_changed")
    if comparisons and _comparison_digest(view, project, reserved["packet"]) != snapshot["comparison_digest"]:
        raise ContractError("history_comparison_changed")
    plan = view.db.execute("SELECT body FROM admin_execution_plans WHERE project=? AND call_id=?", (project, record["call_id"])).fetchone()
    if not plan or digest(json.loads(plan[0])) != record["plan_digest"]:
        raise ContractError("history_snapshot_changed")
    return reserved, snapshot, job


def _terminal_eligible(view, project, record, reserved):
    call = view.db.execute("SELECT state,retry_allowed,host_epoch,usage FROM admin_calls WHERE project=? AND id=?",
                           (project, record["call_id"])).fetchone()
    if (not call or call["retry_allowed"] != 0 or call["host_epoch"] != record["governance_epoch"]
            or call["state"] not in {"failed", "orphaned_unknown", "account_changed_unknown"}):
        return False
    terminal = reserved.get("terminal_observation")
    if (isinstance(terminal, dict) and terminal.get("version") == 1 and type(terminal.get("returncode")) is int
            and terminal["returncode"] == 0 and terminal.get("bounded_public_response_collected") is True
            and terminal.get("response_digest") == record["response_digest"]):
        return True
    # Older captured replies can use the same independently persisted terminal
    # observation in usage, but unknown outcomes never infer it from JSON alone.
    from .admin_response_store import _terminal
    return _terminal(view, record)


def _artifact_integrity(view, project, record, problem):
    result = json.loads(record["result"] or "{}")
    expected = result.get("candidate_ids") or ([result["candidate_id"]] if result.get("candidate_id") else [])
    if not isinstance(expected, list) or len(expected) > 5:
        raise ContractError("history_recovered_artifact_changed")
    rows = view.db.execute("SELECT a.candidate_id,a.body_digest,c.body,c.problem,c.problem_revision FROM history_response_artifacts a "
        "LEFT JOIN candidates c ON c.project=a.project AND c.id=a.candidate_id WHERE a.project=? AND a.call_id=? LIMIT 6",
        (project, record["call_id"])).fetchall()
    revision = json.loads(record["reserved"])["packet"]["problem"]["revision"]
    if (len(rows) > 5 or {row[0] for row in rows} != set(expected)
            or any(row[2] is None or row[3] != problem or row[4] != revision or digest(json.loads(row[2])) != row[1] for row in rows)):
        raise ContractError("history_recovered_artifact_changed")
    if not view.db.execute("SELECT 1 FROM review_receipts WHERE project=? AND call_id=? AND problem=? "
        "AND revision=? AND duplicate_of IS NULL", (project, record["call_id"], problem, revision)).fetchone():
        raise ContractError("history_recovered_artifact_receipt_missing")


def status(runtime, project, call_id):
    record = _record(runtime, project, call_id)
    unavailable = {"available": False, "can_recheck": False, "job_id": None, "job_version": None,
                   "ack_required": False, "boundary_reasons": [], "original_publication_authorized": False,
                   "can_allow_publication": False, "reason": "history_snapshot_unavailable"}
    if not record or record["host_scope"] != runtime._session_scope():
        return unavailable
    try:
        reserved, snapshot, job = _check_snapshot(runtime, project, record, comparisons=record["state"] != "accepted")
        if record["state"] != "accepted" and not _terminal_eligible(runtime, project, record, reserved):
            return dict(unavailable, reason="history_terminal_unconfirmed", job_id=job["id"], job_version=digest(job))
        if _history_work_active(runtime, project):
            return dict(unavailable, reason="history_run_active", job_id=job["id"], job_version=digest(job))
        reasons = []
        if record["session_epoch"] != runtime.session_epoch(): reasons.append("account_boundary")
        if record["governance_epoch"] != runtime.host_epoch() or job["host_epoch"] != runtime.host_epoch(): reasons.append("governance_boundary")
        last = json.loads(job["last_result"])
        if job["state"] == "paused" and last.get("reason") == "user_paused": reasons.append("history_paused")
        if job["state"] == "cancelled": reasons.append("history_cancelled")
        if last.get("workflow_deferred"): reasons.append("history_deferred")
        if job["state"] in {"reviewing", "materializing"}: reasons.append("history_interrupted")
        if any(value[1] != job["id"] for value in _latest_jobs(runtime, project, snapshot["selection"])): reasons.append("history_superseded")
        from .admin_review_flow import publication_policy
        policy = publication_policy(runtime, project)
        access_valid, needs_reconfirmation = False, False
        if record["state"] == "accepted":
            _artifact_integrity(runtime, project, record, reserved["packet"]["problem"]["id"])
            try:
                access_valid = result_access(runtime, project, reserved["packet"]["problem"]["id"], call_id=call_id) is not None
            except ContractError:
                needs_reconfirmation = True
        return {"available": True, "can_recheck": record["state"] != "accepted" or needs_reconfirmation,
                "job_id": job["id"], "job_version": digest(job),
                "ack_required": bool(reasons), "boundary_reasons": reasons,
                "original_publication_authorized": snapshot["original_publication_authorized"],
                "can_allow_publication": bool(snapshot["original_publication_authorized"] and policy["enabled"]
                    and policy["mode"] == snapshot["original_publication_mode"] == "tiered"),
                "result_access_valid": access_valid, "needs_access_reconfirmation": needs_reconfirmation,
                "access_reconfirmation": record["state"] == "accepted", "reason": "access_reconfirmation" if record["state"] == "accepted" else "ready"}
    except (ValueError, TypeError, KeyError) as exc:
        code = str(exc) if str(exc) in {"history_snapshot_unavailable", "history_snapshot_changed", "history_comparison_changed"} else "history_snapshot_changed"
        return dict(unavailable, reason=code)


def assert_no_model_work(runtime):
    current = _SCOPE.get()
    if current and current["runtime"] == id(runtime):
        raise ContractError("history_local_acceptance_cannot_run_models")


def validate(runtime, reserved, *, comparisons=False):
    current = _SCOPE.get()
    if not current or current["runtime"] != id(runtime) or not runtime.db.in_transaction:
        raise ContractError("history_local_acceptance_scope_unavailable")
    record = _record(runtime, current["project"], current["call_id"])
    request = runtime.db.execute("SELECT * FROM admin_response_rechecks WHERE project=? AND host_scope=? AND request_id=?",
        (current["project"], current["host_scope"], current["request_id"])).fetchone()
    if (not request or request["call_id"] != current["call_id"] or request["owner"] != current["owner"]
            or request["state"] != "validating" or request["lease_until"] <= time.time()
            or reserved["call_id"] != current["call_id"]):
        raise ContractError("history_local_acceptance_lease_changed")
    if _current_epochs(runtime, current["host_scope"]) != (current["session_epoch"], current["governance_epoch"]):
        raise ContractError("history_local_acceptance_epoch_changed")
    if _history_work_active(runtime, current["project"]):
        raise ContractError("history_run_active")
    frozen, snapshot, job = _check_snapshot(runtime, current["project"], record, comparisons=comparisons)
    if record["state"] != "accepted" and not _terminal_eligible(runtime, current["project"], record, frozen):
        raise ContractError("history_terminal_unconfirmed")
    if digest(job) != current["job_version"] or _latest_jobs(runtime, current["project"], snapshot["selection"]) != current["latest_jobs"]:
        raise ContractError("history_local_acceptance_job_changed")
    if runtime.db.execute("SELECT 1 FROM admin_calls WHERE project=? AND state IN ('reserved','launch_intent','started') "
        "AND lease_until>? LIMIT 1", (current["project"], time.time())).fetchone():
        raise ContractError("history_local_acceptance_concurrent_model")
    return job


def local_access(runtime, project, problem_id, job_id, *, action, candidate=None):
    current = _SCOPE.get()
    if not current or current["runtime"] != id(runtime):
        return None
    if project != current["project"] or problem_id != current["problem_id"] or (job_id is not None and job_id != current["job_id"]):
        raise ContractError("history_local_acceptance_identity_changed")
    if action not in {"evidence", "submit", "propose", "publish"}:
        raise ContractError("history_local_acceptance_operation_unavailable")
    job = validate(runtime, {"call_id": current["call_id"]})
    if action != "evidence":
        if isinstance(candidate, str):
            row = runtime.db.execute("SELECT body FROM candidates WHERE project=? AND id=?", (project, candidate)).fetchone()
            body = json.loads(row[0]) if row else None
        else:
            body = candidate
        if not isinstance(body, dict) or digest(body) not in current["candidate_digests"]:
            raise ContractError("history_local_acceptance_candidate_outside_reply")
        if action == "publish" and not current["allow_publication"]:
            raise ContractError("history_local_acceptance_publication_not_authorized")
        if action == "publish":
            from .admin_review_flow import publication_policy
            policy = publication_policy(runtime, project)
            if (not current["snapshot"]["original_publication_authorized"] or not policy["enabled"]
                    or policy["mode"] != current["snapshot"]["original_publication_mode"] or policy["mode"] != "tiered"):
                raise ContractError("history_local_acceptance_publication_not_authorized")
    return job


def _candidate_digests(response, packet):
    values = response.get("candidates") or ([response["candidate"]] if response.get("candidate") else [])
    result = []
    for body in values:
        body = clean(dict(body))
        body["metadata"] = metadata(body.get("metadata"))
        body["knowledge_kind"] = body.get("knowledge_kind", "solution")
        body["evidence_refs"] = sorted(set(body["evidence_refs"]))
        if packet.get("evidence_truncated"):
            from .admin import _TRUNCATION_NOTE
            body["limitations"] += "\n" + _TRUNCATION_NOTE
        result.append(digest(body))
    return result


def recheck(runtime, project, call_id, *, request_id, expected_version, expected_session_epoch,
            expected_governance_epoch, expected_job_version, acknowledge_boundary=False, reason="",
            allow_publication=False, confirmed=False):
    from .admin_response_store import mark_accepted, rejection
    if (confirmed is not True or type(acknowledge_boundary) is not bool or type(allow_publication) is not bool
            or type(expected_version) is not int or expected_version < 1
            or any(type(v) is not int or v < 0 for v in (expected_session_epoch, expected_governance_epoch))
            or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in (call_id, request_id))
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000
            or not isinstance(expected_job_version, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_job_version)):
        raise ContractError("invalid_explicit_history_response_recheck")
    identity = project, runtime._session_scope(), request_id
    fingerprint = digest([call_id, expected_version, expected_session_epoch, expected_governance_epoch,
                          expected_job_version, acknowledge_boundary, reason, allow_publication])
    owner, now = uuid.uuid4().hex, time.time()
    with runtime.transaction():
        runtime.assert_session_epoch(expected_session_epoch)
        runtime.assert_host_epoch(expected_governance_epoch)
        old = runtime.db.execute("SELECT * FROM admin_response_rechecks WHERE project=? AND host_scope=? AND request_id=?", identity).fetchone()
        if old:
            if old["request_digest"] != fingerprint: raise ContractError("history_local_request_identity_changed")
            if old["response"]: return json.loads(old["response"])
            raise ContractError("history_local_request_running_or_interrupted")
        record = _record(runtime, project, call_id)
        info = status(runtime, project, call_id)
        if not record or record["version"] != expected_version or not info["available"]:
            raise ContractError(info.get("reason", "history_snapshot_changed"))
        if info["job_version"] != expected_job_version: raise ContractError("history_local_job_version_changed")
        if info["ack_required"] and not acknowledge_boundary: raise ContractError("history_local_boundary_acknowledgement_required")
        if allow_publication and (not info["can_allow_publication"] or info.get("access_reconfirmation")):
            raise ContractError("history_local_publication_not_authorized")
        if runtime.db.execute("SELECT 1 FROM admin_response_rechecks WHERE project=? AND state='validating' AND lease_until>? "
            "UNION ALL SELECT 1 FROM admin_calls WHERE project=? AND state IN ('reserved','launch_intent','started') AND lease_until>? LIMIT 1",
            (project, now, project, now)).fetchone(): raise ContractError("history_local_work_active")
        frozen, snapshot, job = _check_snapshot(runtime, project, record, comparisons=record["state"] != "accepted")
        request = {"project": project, "host_scope": runtime._session_scope(), "call_id": call_id,
            "request_id": request_id, "owner": owner, "session_epoch": expected_session_epoch,
            "governance_epoch": expected_governance_epoch, "job_id": job["id"], "job_version": digest(job),
            "latest_jobs": _latest_jobs(runtime, project, snapshot["selection"]), "reason": clean(reason),
            "acknowledge_boundary": acknowledge_boundary, "allow_publication": allow_publication,
            "problem_id": frozen["packet"]["problem"]["id"], "source_digest": snapshot["source_digest"],
            "snapshot": snapshot, "response_digest": record["response_digest"],
            "candidate_digests": _candidate_digests(json.loads(record["response"]), frozen["packet"])}
        runtime.db.execute("INSERT INTO admin_response_rechecks VALUES(?,?,?,?,?,'validating',?,?,NULL,?,?)",
                           (*identity, fingerprint, call_id, owner, now + 30, now, now))
        runtime.db.execute("INSERT INTO history_response_accepts VALUES(?,?,?,?,?,'validating',?,NULL,?,?)",
                           (*identity, call_id, job["id"], encode(request), now, now))
    current = dict(request, runtime=id(runtime))
    token = _SCOPE.set(current)
    try:
        with runtime.transaction():
            record = _record(runtime, project, call_id)
            frozen, snapshot, job = _check_snapshot(runtime, project, record, comparisons=record["state"] != "accepted")
            validate(runtime, {"call_id": call_id})
            if record["state"] == "accepted":
                _artifact_integrity(runtime, project, record, frozen["packet"]["problem"]["id"])
                result = json.loads(record["result"] or "{}")
                result = dict(result, access_reconfirmation=True)
                outcome = "access_reconfirmed"
            else:
                from .admin import _accept_response
                response = json.loads(record["response"])
                if response.get("decision") == "governance": raise ContractError("history_local_governance_not_supported")
                reserved = dict(frozen, provider_plan=runtime.admin_execution_plan(project, call_id)["executor"],
                    host_epoch=expected_governance_epoch, _history_local_recovery=True)
                reserved["history_reference_authorized"] = bool(snapshot["original_publication_authorized"] and allow_publication)
                result = _accept_response(runtime, project, reserved, response)
                result.update(local_recheck=True, history_local_recheck=True)
                mark_accepted(runtime, call_id, result)
                from .admin_queue import clear_recovered_executor_failure
                clear_recovered_executor_failure(runtime, project, reserved["provider_plan"], call_id)
                for candidate_id in result.get("candidate_ids", [result.get("candidate_id")]):
                    if not candidate_id: continue
                    row = runtime.db.execute("SELECT body FROM candidates WHERE project=? AND id=?", (project, candidate_id)).fetchone()
                    runtime.db.execute("INSERT INTO history_response_artifacts VALUES(?,?,?,?)",
                                       (project, call_id, candidate_id, digest(json.loads(row[0]))))
                outcome = "accepted"
            validate(runtime, {"call_id": call_id})
            answer = {"status": outcome, "call_id": call_id, "local_recheck": True, "history_local_recheck": True,
                      "models_started": False, "result": result}
            runtime.db.execute("UPDATE history_response_accepts SET state='accepted',result=?,updated=? "
                "WHERE project=? AND host_scope=? AND request_id=?", (encode(clean(answer)), time.time(), *identity))
            runtime.db.execute("UPDATE admin_response_rechecks SET state='accepted',response=?,updated=? "
                "WHERE project=? AND host_scope=? AND request_id=?", (encode(clean(answer)), time.time(), *identity))
            return answer
    except (ValueError, TypeError, OSError) as exc:
        detail = rejection(exc)
        if isinstance(exc, ContractError) and str(exc).startswith("history_"):
            detail = {"code": str(exc)[:100], "path": "context.history"}
        answer = {"status": "rejected", "call_id": call_id, "models_started": False, "rejection": detail}
        with runtime.transaction():
            runtime.db.execute("UPDATE history_response_accepts SET state='rejected',result=?,updated=? "
                "WHERE project=? AND host_scope=? AND request_id=?", (encode(answer), time.time(), *identity))
            runtime.db.execute("UPDATE admin_response_rechecks SET state='rejected',response=?,updated=? "
                "WHERE project=? AND host_scope=? AND request_id=?", (encode(answer), time.time(), *identity))
        return answer
    finally:
        _SCOPE.reset(token)


def result_snapshot(view, project, problem=None, candidate_id=None, call_id=None):
    if not _exists(view): return None
    if candidate_id is None and call_id is None and problem is not None:
        candidate_id = problem
    if candidate_id is not None:
        artifact = view.db.execute("SELECT a.call_id FROM history_response_artifacts a JOIN history_response_accepts g "
                                   "ON g.project=a.project AND g.call_id=a.call_id WHERE a.project=? AND a.candidate_id=? "
                                   "AND g.state='accepted' ORDER BY g.updated DESC,g.request_id DESC LIMIT 1",
                                   (project, candidate_id)).fetchone()
        if not artifact: return None
        call_id = artifact[0]
    if call_id is None: return None
    row = view.db.execute("SELECT * FROM history_response_accepts WHERE project=? AND call_id=? AND state='accepted' "
                         "ORDER BY updated DESC,request_id DESC LIMIT 1", (project, call_id)).fetchone()
    if not row: return None
    body = json.loads(row["body"])
    return {"call_id": call_id, "request_id": row["request_id"], "host_scope": row["host_scope"],
            "state": row["state"], "grant_digest": digest(body), "updated": row["updated"],
            "job_id": row["job_id"], "job_version": body["job_version"], "problem_id": body["problem_id"]}


def result_access(view, project, problem, *, candidate_id=None, call_id=None, expected_epoch=None):
    marker = result_snapshot(view, project, problem, candidate_id, call_id)
    if marker is None: return None
    row = view.db.execute("SELECT body FROM history_response_accepts WHERE project=? AND host_scope=? AND request_id=?",
                         (project, marker["host_scope"], marker["request_id"])).fetchone()
    grant = json.loads(row[0])
    if grant["problem_id"] != problem: raise ContractError("history_recovered_artifact_identity_changed")
    epochs = _current_epochs(view, grant["host_scope"])
    if epochs != (grant["session_epoch"], grant["governance_epoch"]) or expected_epoch is not None and expected_epoch != epochs[1]:
        raise ContractError("history_recovered_artifact_epoch_changed")
    record = _record(view, project, marker["call_id"])
    if record is None:
        raise ContractError("history_recovered_artifact_changed")
    _, snapshot, job = _check_snapshot(view, project, record, comparisons=False)
    if digest(job) != grant["job_version"] or _latest_jobs(view, project, snapshot["selection"]) != grant["latest_jobs"]:
        raise ContractError("history_recovered_artifact_authorization_changed")
    _artifact_integrity(view, project, record, problem)
    if candidate_id is not None:
        artifact = view.db.execute("SELECT a.body_digest,c.body FROM history_response_artifacts a JOIN candidates c "
            "ON c.project=a.project AND c.id=a.candidate_id WHERE a.project=? AND a.call_id=? AND a.candidate_id=? AND c.problem=?",
            (project, marker["call_id"], candidate_id, problem)).fetchone()
        if not artifact or digest(json.loads(artifact[1])) != artifact[0]: raise ContractError("history_recovered_artifact_changed")
    return dict(job, local_response_recovery=True)
