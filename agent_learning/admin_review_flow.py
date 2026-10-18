"""Bounded, read-only explanations of review work and publication boundaries."""
from __future__ import annotations

from .phase_review import scope as phase_scope

import json


def install_flow(runtime):
    from .runtime import ContractError
    if not runtime.db.in_transaction:
        raise ContractError("review flow installation requires a transaction")
    columns = {row[1] for row in runtime.db.execute("PRAGMA table_info(admin_control_state)")}
    if "auto_publish" not in columns:
        runtime.db.execute("ALTER TABLE admin_control_state ADD COLUMN auto_publish INTEGER")
    if "publication_mode" not in columns:
        runtime.db.execute("ALTER TABLE admin_control_state ADD COLUMN publication_mode TEXT")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS knowledge_publication_title "
                       "ON knowledge(project,CASE WHEN json_valid(body) THEN lower(trim(json_extract(body,'$.title'))) END)")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS history_review_requests(
        project TEXT NOT NULL,host_scope TEXT NOT NULL,session_epoch INTEGER NOT NULL,
        request_id TEXT NOT NULL,input_digest TEXT NOT NULL,job_id TEXT NOT NULL,
        body TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
        lease_until REAL,result TEXT NOT NULL,PRIMARY KEY(project,host_scope,session_epoch,request_id))""")


def publication_policy(runtime, project):
    from .admin_control import control_state
    state = control_state(runtime, project)
    value = state.get("auto_publish")
    mode = state.get("publication_mode") or "verified_operation_solutions"
    return {"enabled": bool(runtime.config.get("admin", {}).get("auto_publish", False) if value is None else value),
            "configured": value is not None, "scope": "project_host", "host_scope": runtime._session_scope(),
            "mode": mode, "history_automatic": False, "history_reference_opt_in": mode == "tiered",
            "first_enable_recommended": value is None and not state["version"]
                and not runtime.admin_policy(project)["policy_version"]
                and not runtime.config.get("admin", {}).get("auto_publish", False)}


def evidence_readiness(packet):
    """Defer bare tool failures; explicit reviews, feedback and history remain available."""
    if packet.get("incomplete") or packet.get("evidence_truncated"):
        return None
    problem, events = packet["problem"], packet.get("events", [])
    if problem.get("category") != "operation" or problem.get("knowledge_id") or not events:
        return None
    if all(e.get("type") in {"PreToolUse", "PostToolUse"} for e in events) and any(
            e.get("type") == "PostToolUse" and e.get("outcome") == "failed" for e in events) and not any(
            e.get("type") == "PostToolUse" and e.get("outcome") == "succeeded" for e in events):
        return "awaiting_recovery_evidence"
    return None


def _case(runtime, project, problem_id):
    row = runtime.db.execute("SELECT id,summary,category,revision,conversation FROM problems WHERE project=? AND id=?",
                             (project, problem_id)).fetchone()
    if row is None:
        return {"problem_id": problem_id, "title": "", "purpose": "unknown"}
    history = runtime.db.execute("SELECT l.job_id,json_extract(m.body,'$.relative_path') AS path FROM history_case_links l "
        "JOIN history_units u ON u.id=l.unit_id JOIN history_materials m ON m.id=u.material_id "
        "JOIN history_jobs j ON j.id=l.job_id WHERE l.project=? AND l.problem_id=? "
        "ORDER BY j.created DESC,l.job_id LIMIT 1", (project, problem_id)).fetchone()
    summary = runtime.db.execute("SELECT 1 FROM work_summary_links WHERE project=? AND problem_id=?",
                                 (project, problem_id)).fetchone()
    events = runtime.db.execute("SELECT e.type,e.outcome,COUNT(*) AS n FROM problem_events pe JOIN events e ON e.id=pe.event "
                               "WHERE pe.problem=? AND e.project=? GROUP BY e.type,e.outcome", (problem_id, project)).fetchall()
    files = [r[0] for r in runtime.db.execute("SELECT DISTINCT w.relative_path FROM problem_events pe JOIN work_file_links w "
        "ON w.event_id=pe.event WHERE pe.problem=? AND w.project=? ORDER BY w.relative_path LIMIT 3", (problem_id, project))]
    title = (history["path"] or "history_review") if history else row["summary"].split(": {", 1)[0][:240]
    return {"problem_id": problem_id, "revision": row["revision"], "title": title, "category": row["category"],
            "purpose": "history_review" if history else "work_summary" if summary else "daily_review",
            "history_job_id": history["job_id"] if history else None,
            "source_file": history["path"] if history else None, "files": files,
            "summary": "" if history else row["summary"][:400],
            "source_host": "history" if history else "dsh" if row["conversation"].startswith("dsh:") else
                "antigravity" if row["conversation"].startswith("antigravity:") else "codex",
            "evidence": {"events": sum(r["n"] for r in events),
                         "successes": sum(r["n"] for r in events if r["type"] == "PostToolUse" and r["outcome"] == "succeeded"),
                         "failures": sum(r["n"] for r in events if r["outcome"] in {"failed", "conflict"}),
                         "public_statements": sum(r["n"] for r in events if r["type"] not in {"PreToolUse", "PostToolUse"})}}


def _candidate_previews(runtime, project, problem, revision, identities):
    """Bounded per-candidate views, including later approval/publication status."""
    items = []
    for identity in identities[:5]:
        row = runtime.db.execute("SELECT body,status FROM candidates WHERE project=? AND id=? "
            "AND problem=? AND problem_revision=?", (project, identity, problem, revision)).fetchone()
        if row is None:
            items.append({"candidate_id": identity, "status": "unavailable"})
            continue
        body = json.loads(row["body"])
        receipt = runtime.db.execute("SELECT response FROM publication_receipts WHERE project=? AND candidate_id=?",
                                     (project, identity)).fetchone()
        refs = body.get("evidence_refs", [])
        items.append({"candidate_id": identity, "status": row["status"], "problem_revision": revision,
            "title": body["title"][:200], "content_summary": body["content"][:400],
            "applicability": body["applicability"][:200], "limitations": body["limitations"][:200],
            "validation_level": body["validation_level"], "category": body["category"],
            "knowledge_kind": body.get("knowledge_kind", "solution"),
            "evidence_refs": refs[:6], "evidence_refs_truncated": len(refs) > 6,
            "publication": json.loads(receipt[0]) if receipt else None})
    return items


def review_activity(runtime, project):
    from .admin_queue import eligible_query, executor_query, scan_signatures, REVIEW_ORDER
    signature, preflight = scan_signatures(runtime, project)
    query, args = eligible_query(signature, preflight_signature=preflight,
        summary_enabled=runtime.work_summary_enabled(project), summary_host_scope=runtime._session_scope(), phase_scope=phase_scope(runtime, project))
    query, args = executor_query(runtime, project, query, args)
    upcoming = [_case(runtime, project, r["id"]) for r in runtime.db.execute(query + REVIEW_ORDER + "LIMIT 5", (project, *args))]
    recent = []
    for row in runtime.db.execute("SELECT c.id,c.problem,c.revision,c.state,c.created,c.finished,r.response,r.reason "
        "FROM admin_calls c JOIN admin_execution_plans x ON x.call_id=c.id AND x.project=c.project "
        "LEFT JOIN review_receipts r ON r.project=c.project AND r.call_id=c.id AND r.problem=c.problem WHERE c.project=? AND x.host_scope=? "
        "ORDER BY c.created DESC,c.id LIMIT 8", (project, runtime._session_scope())):
        item = _case(runtime, project, row["problem"])
        body = json.loads(row["response"] or "{}")
        candidate = body.get("candidate") or {}
        item.update(call_id=row["id"], reviewed_revision=row["revision"], state=row["state"], created=row["created"],
            evidence_scope="current_case", case_changed_since_review=row["revision"] != item["revision"],
            finished=row["finished"], reason=(row["reason"] or "")[:500],
            missing_evidence=[(v.get("description", "") if isinstance(v, dict) else str(v))[:300]
                              for v in body.get("missing_evidence", [])[:3]],
            candidate_ids=body.get("candidate_ids") or ([body["candidate_id"]] if body.get("candidate_id") else []),
            candidate_title=candidate.get("title", "")[:200], publication=body.get("publication"),
            candidate_summary=candidate.get("content", "")[:400],
            applicability=candidate.get("applicability", "")[:200], limitations=candidate.get("limitations", "")[:200])
        item["candidates"] = _candidate_previews(runtime, project, row["problem"], row["revision"], item["candidate_ids"])
        item["candidates_truncated"] = len(item["candidate_ids"]) > len(item["candidates"])
        if item["history_job_id"]:
            from .runtime import ContractError
            from .candidate_review import history_result_guard
            try:
                history_result_guard(runtime, project, row["problem"])
            except ContractError:
                for key in ("candidate_summary", "candidate_title", "reason", "applicability", "limitations"):
                    item[key] = ""
                item["candidates"] = []
                item["missing_evidence"], item["history_access_required"] = [], True
        recent.append(item)
    deferred = []
    for row in runtime.db.execute("SELECT f.problem,f.reason FROM admin_preflights f "
        "JOIN problems p ON p.project=f.project AND p.id=f.problem AND p.revision=f.revision "
        "WHERE f.project=? AND f.signature=? ORDER BY f.updated DESC LIMIT 5", (project, preflight)):
        item = _case(runtime, project, row["problem"])
        item.update(state="local_preflight", reason=(row["reason"] or "")[:300], model_called=False)
        deferred.append(item)
    return {"scope": runtime._session_scope(), "upcoming": upcoming, "recent": recent, "deferred": deferred,
            "automatic_replay": False, "bounded": True}
