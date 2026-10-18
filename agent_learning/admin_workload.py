"""Read-only case stages and bounded grouping; groups never share review authority."""
from __future__ import annotations

from collections import Counter
import json
import re


STAGES = ("pending_review", "reviewing", "awaiting_evidence", "local_deferred",
          "awaiting_approval", "blocked", "skipped", "completed")
GROUP_SCAN_LIMIT = 2000
PAGE_BYTES = 128000

# Keep the storage status contract intact. A pending row can already have a decision.
CASE_VIEW = """WITH source AS (
 SELECT p.*,r.state AS review_state,r.reason AS review_reason,r.revision AS review_revision,
 CASE WHEN EXISTS(SELECT 1 FROM history_case_links h WHERE h.project=p.project AND h.problem_id=p.id)
      THEN 'historical'
      WHEN EXISTS(SELECT 1 FROM work_summary_links w WHERE w.project=p.project AND w.problem_id=p.id)
      THEN 'work_summary' ELSE 'daily' END AS kind,
 (SELECT a.state FROM admin_calls a WHERE a.project=p.project AND a.problem=p.id
    AND a.revision=p.revision AND a.retry_allowed=0 AND a.state NOT IN ('not_started','deduplicated')
    AND NOT EXISTS(SELECT 1 FROM review_receipts z WHERE z.project=a.project AND z.call_id=a.id)
    ORDER BY a.created DESC,a.id DESC LIMIT 1) AS unresolved_call,
 EXISTS(SELECT 1 FROM candidates c WHERE c.project=p.project AND c.problem=p.id
    AND c.problem_revision=p.revision AND c.status IN ('candidate','deferred')) AS pending_candidate,
 EXISTS(SELECT 1 FROM governance_proposals g WHERE g.project=p.project AND g.problem_id=p.id
    AND g.problem_revision=p.revision AND g.state='proposed' AND (g.candidate_id IS NULL OR EXISTS(
        SELECT 1 FROM candidates c WHERE c.project=g.project AND c.id=g.candidate_id
        AND c.status IN ('candidate','deferred')))) AS pending_proposal,
 (SELECT t.scope FROM task_snapshots t WHERE t.project=p.project AND t.id=p.task_id
    AND t.revision=p.task_revision) AS task_scope,
 (SELECT z.decision FROM review_receipts z WHERE z.project=p.project AND z.problem=p.id
    AND z.revision=p.revision AND z.review_signature=r.review_signature) AS decision
 FROM problems p LEFT JOIN review_states r ON r.project=p.project AND r.problem=p.id
    AND r.revision=p.revision WHERE p.project=?
), cases AS (
 SELECT source.*,CASE
 WHEN unresolved_call IN ('reserved','launch_intent','started') THEN 'reviewing'
 WHEN unresolved_call IS NOT NULL THEN 'blocked'
 WHEN pending_candidate OR pending_proposal THEN 'awaiting_approval'
 WHEN status!='pending' THEN 'completed'
 WHEN decision='preflight_blocked' THEN 'local_deferred'
 WHEN review_state='awaiting_evidence' THEN 'awaiting_evidence'
 WHEN review_state='skipped' THEN 'skipped'
 WHEN review_state IN ('candidate_ready','proposal_ready','published','work_summary_ready') THEN 'completed'
 ELSE 'pending_review' END AS queue_state FROM source
) """


def _available(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"problems", "candidates", "admin_calls", "review_states", "review_receipts", "history_case_links",
            "work_summary_links", "governance_proposals", "task_snapshots"} <= tables:
        return False
    return ({"knowledge_revision", "resolution_status"} <= {r[1] for r in db.execute("PRAGMA table_info(problems)")}
            and "retry_allowed" in {r[1] for r in db.execute("PRAGMA table_info(admin_calls)")})


def workload_summary(db, project, *, history_stages=True):
    """Mutually exclusive stages, with historical text units counted separately."""
    if not _available(db):
        return {"available": False, "reason": "workload_upgrade_required", "stages": {stage: None for stage in STAGES},
                "kinds": {}, "pending_by_kind": {}, "pending_total": None, "history_pending_units": None,
                "daily_pending_rows": None, "summary_pending_rows": None,
                "history_in_daily_queue": False, "grouping_changes_authority": False}
    kinds = {kind: {stage: 0 for stage in STAGES} for kind in ("daily", "work_summary", "historical")}
    pending = {kind: 0 for kind in kinds}
    view = CASE_VIEW if history_stages else CASE_VIEW.replace("WHERE p.project=?", "WHERE p.project=? "
        "AND NOT EXISTS(SELECT 1 FROM history_case_links h WHERE h.project=p.project AND h.problem_id=p.id)")
    for row in db.execute(view +
            "SELECT kind,queue_state,status,COUNT(*) AS n FROM cases GROUP BY kind,queue_state,status", (project,)):
        kinds[row["kind"]][row["queue_state"]] += row["n"]
        if row["status"] == "pending":
            pending[row["kind"]] += row["n"]
    if not history_stages:
        pending["historical"] = db.execute("SELECT COUNT(*) FROM problems p WHERE p.project=? AND p.status='pending' "
            "AND EXISTS(SELECT 1 FROM history_case_links h WHERE h.project=p.project AND h.problem_id=p.id)", (project,)).fetchone()[0]
        kinds["historical"] = {stage: None for stage in STAGES}
    stages = {stage: kinds["daily"][stage] + kinds["work_summary"][stage] for stage in STAGES}
    return {"available": True, "stages": stages, "kinds": kinds, "pending_by_kind": pending,
            "pending_total": sum(pending.values()), "history_pending_units": pending["historical"],
            "daily_pending_rows": pending["daily"], "summary_pending_rows": pending["work_summary"],
            "history_in_daily_queue": False, "historical_stages_available": history_stages,
            "grouping_changes_authority": False}


def _host(conversation):
    return "dsh" if conversation.startswith("dsh:") else "antigravity" if conversation.startswith("antigravity:") else "codex"


def _group_identity(row):
    from .runtime import digest
    # Only named transport fields are normalized. Unlabelled UUIDs, versions,
    # paths and numbers can identify different business failures and stay intact.
    summary = re.sub(r'(\b(?:session_id|sessionId|turn_id|tool_use_id|event_id|timestamp)\b"?\s*[:=]\s*)'
                     r'(?:"[^"\n]*"|[^\s,}\]]+)', r'\1<transport>', row["summary"])
    summary = " ".join(summary.split())
    return digest([row["project"], row["kind"], row["category"], _host(row["conversation"]),
                   row["knowledge_id"], row["knowledge_revision"], row["task_scope"], summary])[:32]


def _preview(db, row):
    from .runtime import clean
    result = {key: row[key] for key in ("id", "revision", "category", "status", "created", "kind", "queue_state",
                                       "review_state", "review_revision", "conversation")}
    result.update(summary=clean(row["summary"])[:800], review_reason=clean(row["review_reason"] or "")[:800],
                  conversation=clean(row["conversation"])[:500],
                  review_current=row["review_revision"] == row["revision"], source_host=_host(row["conversation"]))
    result["candidates"] = [{"id": c["id"], "status": c["status"],
                              "title": clean(json.loads(c["body"]).get("title", ""))[:250]}
                             for c in db.execute("SELECT id,status,body FROM candidates WHERE project=? AND problem=? "
                                 "AND problem_revision=? ORDER BY created DESC,id LIMIT 5",
                                 (row["project"], row["id"], row["revision"]))]
    call = db.execute("SELECT id,state,created,finished,retry_allowed FROM admin_calls WHERE project=? AND problem=? "
                      "ORDER BY created DESC,id DESC LIMIT 1", (row["project"], row["id"])).fetchone()
    result["last_call"] = dict(call) if call else None
    return result


def workload_page(db, project, *, view="groups", stage="all", group_id="", offset=0, limit=20):
    from .runtime import ContractError, clean
    if view not in {"groups", "cases"} or stage not in {"all", *STAGES}:
        raise ContractError("invalid administrator workload view or stage")
    if not isinstance(group_id, str) or group_id and (view != "cases" or not re.fullmatch(r"[0-9a-f]{32}", group_id)):
        raise ContractError("invalid administrator workload group")
    if (type(offset) is not int or not 0 <= offset <= 100000 or type(limit) is not int or not 1 <= limit <= 100):
        raise ContractError("invalid administrator workload page")
    summary = workload_summary(db, project)
    if not summary["available"]:
        return {"view": view, "stage": stage, "group_id": group_id, "items": [], "total": 0, "offset": offset,
                "next_offset": None, "total_cases": None, "sampled_cases": 0, "groups_truncated": False,
                "history_excluded": True, "summary": summary}
    where = "WHERE kind!='historical'" + (" AND queue_state=?" if stage != "all" else "")
    args = (project, stage) if stage != "all" else (project,)
    total_cases = db.execute(CASE_VIEW + "SELECT COUNT(*) FROM cases " + where, args).fetchone()[0]
    order = " ORDER BY (queue_state IN ('pending_review','reviewing')) DESC,created DESC,id"
    if view == "cases" and not group_id:
        rows = db.execute(CASE_VIEW + "SELECT * FROM cases " + where + order + " LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        items, total = [_preview(db, row) for row in rows], total_cases
        sampled = total_cases
    elif group_id:
        rows = db.execute(CASE_VIEW + "SELECT * FROM cases " + where + order + " LIMIT ?", (*args, GROUP_SCAN_LIMIT)).fetchall()
        matches = [row for row in rows if _group_identity(row) == group_id]
        total, sampled = len(matches), len(rows)
        items = [_preview(db, row) for row in matches[offset:offset + limit]]
    else:
        rows = db.execute(CASE_VIEW + "SELECT * FROM cases " + where + order + " LIMIT ?", (*args, GROUP_SCAN_LIMIT)).fetchall()
        groups = {}
        for row in rows:
            identity = _group_identity(row)
            group = groups.setdefault(identity, {"group_id": identity, "summary": clean(row["summary"])[:500],
                "kind": row["kind"], "source_host": _host(row["conversation"]), "member_count": 0,
                "stages": Counter(), "members": [], "grouping_only": True})
            group["member_count"] += 1
            group["stages"][row["queue_state"]] += 1
            if len(group["members"]) < 5:
                group["members"].append(row)
        total = len(groups)
        items = list(groups.values())[offset:offset + limit]
        for group in items:
            group["members"] = [_preview(db, row) for row in group["members"]]
            group["members_truncated"] = group["member_count"] > len(group["members"])
        sampled = len(rows)
    selected, used = [], 0
    for item in items:
        size = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        if selected and used + size > PAGE_BYTES:
            break
        selected.append(item)
        used += size
    return {"view": view, "stage": stage, "group_id": group_id, "items": selected, "total": total, "offset": offset,
            "next_offset": offset + len(selected) if offset + len(selected) < total else None,
            "page_budget_limited": len(selected) < len(items),
            "total_cases": total_cases, "sampled_cases": sampled, "groups_truncated": sampled < total_cases,
            "history_excluded": True, "summary": summary}


def evidence_wait_signature(packet, cfg, evidence):
    from .admin_decisions import review_signature
    from .runtime import digest
    # Model selection alone does not supply missing facts. All other policy,
    # format, instruction, provider and comparison dependencies still apply.
    config = {key: value for key, value in cfg.items() if key not in {"model", "model_mode", "model_source"}}
    return digest(["evidence-wait-v1", review_signature(packet, config, evidence)])


def hold_unchanged_review(runtime, project, packet, evidence, cfg):
    """Missing-evidence decisions wait for changed evidence or dependencies.

    Explicit case review bypasses this helper. Evidence fingerprints include current
    health and complete business digests; a similar message is never sufficient.
    """
    if packet["problem"].get("knowledge_id"):
        return False
    row = runtime.db.execute("SELECT r.evidence_signature,json_extract(r.response,'$.evidence_wait_signature') AS wait_signature "
        "FROM review_states s JOIN review_receipts r "
        "ON r.project=s.project AND r.problem=s.problem AND r.revision=s.revision AND r.review_signature=s.review_signature "
        "WHERE s.project=? AND s.problem=? AND s.revision=? AND r.decision='needs_evidence' "
        "AND r.duplicate_of IS NULL", (project, packet["problem"]["id"], packet["problem"]["revision"])).fetchone()
    # Older receipts have no scheduling signature. Normal exact receipt reuse
    # remains available; never invent their former model policy or instructions.
    return bool(row and row["evidence_signature"] == evidence and row["wait_signature"] and
                row["wait_signature"] == evidence_wait_signature(packet, cfg, evidence))
