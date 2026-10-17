"""Explicit candidate approval plans and durable, atomic receipts.

Read helpers accept a read-only browser model. Plans are snapshots, not authority:
the caller must keep the prepared plan server-side until the user confirms it.
No administrator or model is invoked by this module.
"""
from __future__ import annotations

import json
import time

from .runtime import ContractError, clean, digest, encode, knowledge_fingerprint


FORMAT = "candidate-review-v1"
MAX_BATCH = 20
STATUSES = {"pending", "candidate", "deferred", "rejected", "published", "all"}
BUDGET_PAUSES = {"review_budget_exhausted", "budget_exhausted", "token_budget_exhausted",
                 "batch_time_exhausted"}


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ContractError(f"{name} must be {minimum}..{maximum}")
    return value


def _project(view, project):
    row = view.db.execute("SELECT * FROM projects WHERE id=?", (project,)).fetchone()
    if row is None:
        raise ContractError("unknown project")
    return dict(row)


def _candidate(view, project, identity):
    if not isinstance(identity, str) or not identity or len(identity) > 200:
        raise ContractError("candidate_id must be a nonempty string of at most 200 characters")
    row = view.db.execute("SELECT * FROM candidates WHERE project=? AND id=?", (project, identity)).fetchone()
    if row is None:
        raise LookupError("candidate not found in this project")
    return dict(row), json.loads(row["body"])


def _history_rows(view, project, problem):
    return [dict(row) for row in view.db.execute(
        "SELECT j.* FROM history_case_links l JOIN history_jobs j ON j.id=l.job_id "
        "WHERE l.project=? AND l.problem_id=? ORDER BY j.created DESC,j.id", (project, problem))]


def history_result_guard(runtime, project, problem, expected_epoch=None):
    """Read completed results after known budget pauses without resuming execution."""
    from .history_import import ACCESS_STATES

    epoch = runtime.assert_host_epoch(expected_epoch)
    rows = _history_rows(runtime, project, problem)
    if not rows:
        return None
    # The newest explicit job state owns approval. An older active link must not
    # bypass a user's later pause/cancellation for the same historical material.
    row = rows[0]
    if row["host_epoch"] != epoch:
        raise ContractError("history host epoch changed; explicitly reapprove the job")
    if row["state"] in ACCESS_STATES:
        if row["state"] == "reviewing" and (not row["owner"] or (row["lease_until"] or 0) <= time.time()):
            raise ContractError("history review lease expired")
        return row
    reason = json.loads(row["last_result"]).get("reason")
    if row["state"] == "paused" and reason in BUDGET_PAUSES and not row["owner"]:
        return row
    raise ContractError("history job is paused or cancelled; explicit approval is unavailable")


def history_publication_guard(runtime, project, problem, expected_epoch=None):
    """Publication retains the same explicit history access boundary."""
    return history_result_guard(runtime, project, problem, expected_epoch)


class _ValidationView:
    def __init__(self, db):
        self.db = db

    def host_epoch(self):
        return self.db.execute("SELECT epoch FROM host_state WHERE id=1").fetchone()[0]

    def assert_host_epoch(self, expected_epoch):
        epoch = self.host_epoch()
        if expected_epoch is not None and expected_epoch != epoch:
            raise ContractError("reconnect_required: account context changed")
        return epoch

    def task_snapshot(self, project, identity, revision):
        return self.db.execute("SELECT * FROM task_snapshots WHERE project=? AND id=? AND revision=?",
                               (project, identity, revision)).fetchone()


def _inspection(view, project, candidate, body, knowledge_id=None, expected_revision=None):
    """All source data contributes to the snapshot; output stays bounded."""
    from .runtime import Runtime
    from .knowledge_lifecycle import lifecycle_state

    probe, issues, warnings = _ValidationView(view.db), [], []
    problem = view.db.execute("SELECT * FROM problems WHERE project=? AND id=?",
                              (project, body["problem_id"])).fetchone()
    events = []
    for ref in body["evidence_refs"]:
        row = view.db.execute("SELECT e.*,p.role FROM events e JOIN problem_events p ON p.event=e.id "
                              "WHERE e.project=? AND e.id=? AND p.problem=?",
                              (project, ref, body["problem_id"])).fetchone()
        event = dict(row) if row else {"id": ref, "missing": True}
        operation = view.db.execute("SELECT * FROM operations WHERE project=? AND id=?",
                                    (project, event.get("operation"))).fetchone()
        event["operation_state"] = dict(operation) if operation else None
        events.append(event)
    if candidate["status"] not in {"candidate", "deferred"}:
        issues.append({"code": "candidate_state", "message": "candidate is already published or rejected"})
    if candidate["problem"] != body["problem_id"] or candidate["problem_revision"] != body["problem_revision"]:
        issues.append({"code": "candidate_identity", "message": "candidate source identity does not match its body"})
    try:
        Runtime._validate_sources(probe, project, body)
    except ContractError as exc:
        issues.append({"code": "source_invalid", "message": str(exc)})
    history = _history_rows(view, project, body["problem_id"])
    history_budget = False
    try:
        allowed = history_publication_guard(probe, project, body["problem_id"])
        history_budget = bool(allowed and allowed["state"] == "paused")
    except ContractError as exc:
        issues.append({"code": "history_blocked", "message": str(exc)})
    target = None
    if knowledge_id is not None:
        if not isinstance(knowledge_id, str) or not knowledge_id or len(knowledge_id) > 200:
            raise ContractError("invalid knowledge update target")
        _integer(expected_revision, "expected_revision", 1, 1000000)
        target = lifecycle_state(view, project, knowledge_id)
        if target["active_revision"] != expected_revision:
            issues.append({"code": "target_changed", "message": "knowledge revision changed or is revoked"})
    elif expected_revision is not None:
        raise ContractError("expected_revision requires a knowledge_id")
    duplicates = [dict(row) for row in view.db.execute(
        "SELECT a.id,a.revision FROM active a JOIN knowledge_fingerprints f ON "
        "f.project=a.project AND f.id=a.id AND f.revision=a.revision "
        "WHERE a.project=? AND f.fingerprint=? ORDER BY a.id", (project, knowledge_fingerprint(body)))]
    if duplicates:
        warnings.append({"code": "equivalent_active", "message": "equivalent active knowledge will share its existing record"})
        if knowledge_id is not None and any(row["id"] != knowledge_id for row in duplicates):
            issues.append({"code": "target_duplicate", "message": "equivalent knowledge has another active ID"})
    if body["validation_level"] == "reported":
        warnings.append({"code": "reported", "message": "publication preserves reported evidence; it does not verify correctness"})
    if any(event.get("operation_state") and (event["operation_state"]["conflict"] or
           event["operation_state"]["decision"] == "denied") for event in events):
        warnings.append({"code": "operation_conflict", "message": "a source operation is conflicted or denied"})
    task = probe.task_snapshot(project, problem["task_id"], problem["task_revision"]) if problem and problem["task_id"] else None
    snapshot = {"candidate": candidate, "problem": dict(problem) if problem else None,
                "task_snapshot": dict(task) if task else None,
                "events": events, "history": history, "target": target, "duplicates": duplicates}
    preflight = {"publishable": not issues, "issues": issues, "warnings": warnings,
                 "history_budget_paused": history_budget}
    return snapshot, preflight


def _preview(candidate, body, preflight):
    return {"candidate_id": candidate["id"], "status": candidate["status"], "title": clean(body["title"])[:250],
            "category": body["category"], "knowledge_kind": body.get("knowledge_kind", "solution"),
            "validation_level": body["validation_level"], "created": candidate["created"],
            "problem_id": candidate["problem"], "problem_revision": candidate["problem_revision"],
            "preflight": preflight}


def candidate_review_list(view, project, status="pending", q="", offset=0, limit=20):
    _project(view, project)
    _integer(offset, "offset", 0, 10000)
    _integer(limit, "limit", 1, 50)
    if not isinstance(status, str) or status not in STATUSES or not isinstance(q, str) or len(q) > 200:
        raise ContractError("invalid candidate list filter")
    source, args = "FROM candidates WHERE project=?", [project]
    if status == "pending":
        source += " AND status IN ('candidate','deferred')"
    elif status != "all":
        source += " AND status=?"
        args.append(status)
    if q:
        source += " AND (json_extract(body,'$.title') LIKE ? ESCAPE '\\' OR json_extract(body,'$.content') LIKE ? ESCAPE '\\')"
        term = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args.extend((term, term))
    total = view.db.execute("SELECT COUNT(*) " + source, args).fetchone()[0]
    items = []
    for row in view.db.execute("SELECT * " + source + " ORDER BY created DESC,id LIMIT ? OFFSET ?", (*args, limit, offset)):
        candidate, body = dict(row), json.loads(row["body"])
        _, preflight = _inspection(view, project, candidate, body)
        items.append(_preview(candidate, body, preflight))
    return {"items": items, "total": total, "offset": offset,
            "next_offset": offset + len(items) if offset + len(items) < total else None}


def candidate_review_detail(view, project, candidate_id, section="content", offset=0, limit=4000,
                            evidence_offset=0, evidence_limit=10):
    _project(view, project)
    _integer(offset, "offset", 0, 1000000)
    _integer(limit, "limit", 1, 8000)
    _integer(evidence_offset, "evidence_offset", 0, 100000)
    _integer(evidence_limit, "evidence_limit", 1, 20)
    if section not in {"content", "applicability", "limitations"}:
        raise ContractError("invalid candidate section")
    candidate, body = _candidate(view, project, candidate_id)
    snapshot, preflight = _inspection(view, project, candidate, body)
    text = clean(body[section])
    evidence = []
    for event in snapshot["events"][evidence_offset:evidence_offset + evidence_limit]:
        item = {key: event.get(key) for key in ("id", "type", "outcome", "created", "task_id", "task_revision", "role", "missing")}
        payload = clean(event.get("payload", ""))
        item.update(payload_text=payload[:4000], payload_chars=len(payload), truncated=len(payload) > 4000,
                    operation=clean(event.get("operation_state")))
        evidence.append(item)
    problem = snapshot["problem"]
    return {**_preview(candidate, body, preflight),
            "body": clean({key: value for key, value in body.items() if key not in {"content", "applicability", "limitations"}}),
            "problem": {key: clean(problem[key]) for key in ("id", "revision", "summary", "status")} if problem else None,
            "field": {"name": section, "text": text[offset:offset + limit], "total_chars": len(text), "offset": offset,
                      "next_offset": offset + limit if offset + limit < len(text) else None},
            "evidence": {"items": evidence, "total": len(snapshot["events"]), "offset": evidence_offset,
                         "next_offset": evidence_offset + len(evidence) if evidence_offset + len(evidence) < len(snapshot["events"]) else None}}


def prepare_review(runtime, project, selections, action, reason="", expected_epoch=None):
    if not isinstance(action, str) or action not in {"approve", "reject", "defer"}:
        raise ContractError("unknown candidate review action")
    if not isinstance(reason, str) or len(reason) > 2000:
        raise ContractError("review reason must be at most 2000 characters")
    if not isinstance(selections, list) or not 1 <= len(selections) <= MAX_BATCH:
        raise ContractError("select 1..20 candidates")
    version = _project(runtime, project)
    epoch = runtime.assert_host_epoch(expected_epoch)
    scope, session_epoch = runtime._session_scope(), runtime.session_epoch()
    items, seen = [], set()
    for selection in selections:
        selection = {"candidate_id": selection} if isinstance(selection, str) else selection
        if not isinstance(selection, dict) or set(selection) - {"candidate_id", "knowledge_id", "expected_revision"}:
            raise ContractError("invalid candidate selection")
        identity = selection.get("candidate_id")
        candidate, body = _candidate(runtime, project, identity)
        if identity in seen:
            raise ContractError("candidate selected more than once")
        seen.add(identity)
        snapshot, preflight = _inspection(runtime, project, candidate, body, selection.get("knowledge_id"), selection.get("expected_revision"))
        state_allowed = candidate["status"] in {"candidate", "deferred"}
        ready = preflight["publishable"] if action == "approve" else state_allowed
        items.append({"candidate_id": identity, "status": candidate["status"], "title": clean(body["title"])[:250],
                      "knowledge_id": selection.get("knowledge_id"), "expected_revision": selection.get("expected_revision"),
                      "snapshot_digest": digest(snapshot), "preflight": preflight, "ready": ready})
    plan = {"format": FORMAT, "project_id": project, "action": action, "reason": clean(reason),
            "host_epoch": epoch, "host_scope": scope, "session_epoch": session_epoch,
            "generation": version["generation"], "items": items, "ready": all(item["ready"] for item in items)}
    return {**plan, "plan_digest": digest(plan)}


def commit_review(runtime, project, plan, request_id, expected_epoch=None):
    if not isinstance(plan, dict) or plan.get("format") != FORMAT or plan.get("project_id") != project:
        raise ContractError("invalid candidate review plan")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
        raise ContractError("review request_id must be a nonempty string of at most 200 characters")
    unsigned = {key: value for key, value in plan.items() if key != "plan_digest"}
    if plan.get("plan_digest") != digest(unsigned):
        raise ContractError("candidate review plan changed")
    request_digest = digest(plan)
    event_id = digest(["candidate-review", project, request_id])
    with runtime._case_transaction():
        runtime.assert_host_epoch(expected_epoch)
        if plan["host_epoch"] != runtime.host_epoch() or plan["host_scope"] != runtime._session_scope() or plan["session_epoch"] != runtime.session_epoch():
            raise ContractError("candidate review identity changed; prepare again")
        old = runtime.db.execute("SELECT type,payload FROM events WHERE id=? AND project=?", (event_id, project)).fetchone()
        if old:
            receipt = json.loads(old["payload"])
            if old["type"] != "CandidateReview" or receipt.get("input_digest") != request_digest:
                raise ContractError("review request_id reused with different content")
            return receipt["response"]
        selections = [{key: item[key] for key in ("candidate_id", "knowledge_id", "expected_revision")} for item in plan["items"]]
        current = prepare_review(runtime, project, selections, plan["action"], plan["reason"], expected_epoch=plan["host_epoch"])
        if current["plan_digest"] != plan["plan_digest"]:
            raise ContractError("candidate, evidence, history or publication target changed; prepare again")
        if not current["ready"]:
            raise ContractError("selected candidates do not pass review preflight")
        results = []
        for item in plan["items"]:
            if plan["action"] == "approve":
                result = runtime.publish(project, item["candidate_id"], knowledge_id=item["knowledge_id"],
                                         expected_revision=item["expected_revision"], expected_epoch=plan["host_epoch"],
                                         _review_approval=True)
                results.append({"candidate_id": item["candidate_id"], **result})
            else:
                status = "rejected" if plan["action"] == "reject" else "deferred"
                runtime.db.execute("UPDATE candidates SET status=? WHERE project=? AND id=?", (status, project, item["candidate_id"]))
                results.append({"candidate_id": item["candidate_id"], "status": status})
        response = {"request_id": request_id, "action": plan["action"], "status": "committed", "atomic": True, "results": results}
        # Receipt is an internal bounded record, never clipped by observation
        # event budgets; all changes and the exact replay response commit together.
        payload = encode({"format": FORMAT, "input_digest": request_digest, "plan_digest": plan["plan_digest"],
                          "reason": plan["reason"], "actor": "explicit-review", "response": response})
        runtime.db.execute("INSERT INTO events(id,project,conversation,type,outcome,payload,created,business_digest) "
                           "VALUES(?,?,?,?,?,?,?,?)", (event_id, project, "candidate-review", "CandidateReview", "reported",
                                                     payload, time.time(), digest(json.loads(payload))))
        return response
