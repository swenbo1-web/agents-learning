"""One publication gate used by automatic acceptance and read-only previews."""
from __future__ import annotations

import json
import re


def knowledge_tier(body):
    return "source_reference" if body.get("validation_level") == "reported" else "tool_verified"


def _conflict_gate(runtime, project, packet, body, proposal):
    """Never let deduplication or a fresh ID evade disputed/revoked knowledge."""
    from .knowledge_equivalence import same_presentation_claim
    from .candidate_references import lineage_risk, held_equivalent
    held = held_equivalent(runtime, project, body)
    if held:
        return held
    for ref in body.get("evidence_refs", ()):
        risk = lineage_risk(runtime, project, ref)
        if risk and risk.get("blocked", True):
            return "candidate reference source requires review"
    targets = {value for value in (packet["problem"].get("knowledge_id"), proposal.get("knowledge_id")) if value}
    targets.update(item.get("knowledge_id", item.get("id")) for item in packet.get("related_knowledge", [])
                   if item.get("knowledge_id", item.get("id")))
    from .knowledge_lifecycle import publication_lineage_state
    lineage = publication_lineage_state(runtime, project, body)
    messages = {"equivalent_knowledge_revoked": "equivalent knowledge was revoked",
                "equivalent_knowledge_superseded": "equivalent knowledge was superseded",
                "equivalent_knowledge_disputed": "unresolved knowledge counterexample"}
    if lineage["issues"]:
        issue = lineage["issues"][0]
        return messages.get(issue["code"], issue["message"])
    for row in lineage["matches"]:
        targets.add(row["id"])
    for identity in targets:
        if runtime.db.execute("SELECT 1 FROM knowledge_feedback WHERE project=? AND knowledge_id=? "
                "AND kind='counterexample' AND disposition='unresolved' LIMIT 1", (project, identity)).fetchone():
            return "unresolved knowledge counterexample"
    if proposal["action"] == "publish":
        # Recheck the indexed title even when retrieval did not offer the old
        # entry. The program detects exact-title differences, not every semantic
        # contradiction in natural language.
        rows = runtime.db.execute("SELECT k.body,a.revision AS active_revision,k.revision FROM knowledge k "
            "LEFT JOIN active a ON a.project=k.project AND a.id=k.id WHERE k.project=? "
            "AND (CASE WHEN json_valid(k.body) THEN lower(trim(json_extract(k.body,'$.title'))) END)="
            "lower(trim(?)) LIMIT 17", (project, body["title"])).fetchall()
        if len(rows) > 16:
            return "conflicting knowledge requires review"
        for row in rows:
            if row["active_revision"] is None:
                return "previous knowledge title requires review"
            if row["revision"] == row["active_revision"] and not same_presentation_claim(body, json.loads(row["body"])):
                return "conflicting knowledge requires review"
        title = re.sub(r"\s+", " ", body["title"].strip()).casefold()
        for related in packet.get("related_knowledge", []):
            identity = related.get("knowledge_id", related.get("id"))
            row = runtime.db.execute("SELECT k.body FROM active a JOIN knowledge k ON k.project=a.project "
                "AND k.id=a.id AND k.revision=a.revision WHERE a.project=? AND a.id=?", (project, identity)).fetchone()
            if row:
                existing = json.loads(row[0])
                if (re.sub(r"\s+", " ", existing["title"].strip()).casefold() == title
                        and not same_presentation_claim(body, existing)):
                    return "conflicting knowledge requires review"
    return None


def publication_gate(runtime, project, packet, body, proposal, *, mode="verified_operation_solutions",
                     history_reference=False):
    if not history_reference and runtime.db.execute("SELECT 1 FROM history_case_links WHERE project=? "
            "AND problem_id=? LIMIT 1", (project, packet["problem"]["id"])).fetchone():
        return "history_requires_approval"
    conflict = _conflict_gate(runtime, project, packet, body, proposal)
    if conflict:
        return conflict
    if mode != "tiered" or body["validation_level"] != "reported":
        return _verified_gate(runtime, project, packet, body, proposal)
    from .candidate_references import lineage_risk
    if any(lineage_risk(runtime, project, ref) for ref in body.get("evidence_refs", ())):
        return "candidate reference requires independent source evidence"
    if packet.get("evidence_truncated") or packet.get("incomplete"):
        return "evidence truncated"
    if proposal["action"] != "publish" or packet["problem"].get("knowledge_id"):
        return "existing knowledge requires trusted governance"
    if not history_reference and not (body["category"] == "project" and body.get("knowledge_kind") == "fact"):
        return "requires sourced project fact"
    if not body["applicability"].strip() or not body["limitations"].strip():
        return "reference requires scope and limitations"
    # Provenance is checked again at acceptance/publication. A bare tool error
    # cannot become a project fact just because a model changed its category.
    public_source = False
    for ref in body["evidence_refs"]:
        row = runtime.db.execute("SELECT e.*,o.conflict,o.decision FROM problem_events pe JOIN events e ON e.id=pe.event "
            "LEFT JOIN operations o ON o.project=e.project AND o.id=e.operation WHERE pe.problem=? AND e.project=? AND e.id=?",
            (packet["problem"]["id"], project, ref)).fetchone()
        if not row or row["conflict"] or row["decision"] == "denied" or row["outcome"] in {"failed", "conflict"}:
            return "reference source is unhealthy"
        payload = json.loads(row["payload"])
        if row["type"] == "Feedback" and payload.get("feedback_kind") == "counterexample":
            return "unresolved knowledge counterexample"
        if history_reference:
            source = payload.get("source") or {}
            if (row["type"] != "HistoryImport" or row["outcome"] != "reported"
                    or not source.get("source_key") or not source.get("content_hash")):
                return "reference requires archived provenance"
            public_source = True
        else:
            from .public_observations import reference_public
            if reference_public(row["type"], payload, row["outcome"]):
                public_source = True
            elif row["type"] in {"UserPromptSubmit", "Stop", "PublicMessageSegment"}:
                return "reference requires complete public source evidence"
    return None if public_source else "reference requires public source evidence"


def publication_preview(runtime, project, candidate_id):
    from .runtime import ContractError
    from .admin_review_flow import publication_policy
    row = runtime.db.execute("SELECT * FROM candidates WHERE project=? AND id=?", (project, candidate_id)).fetchone()
    if not row:
        raise ContractError("candidate absent from this project")
    body = json.loads(row["body"])
    policy = publication_policy(runtime, project)
    reason = None
    if row["status"] not in {"candidate", "deferred"}:
        reason = "candidate_not_pending"
    elif runtime.db.execute("SELECT 1 FROM history_case_links WHERE project=? AND problem_id=?", (project, row["problem"])).fetchone():
        reason = "history_requires_approval"
    else:
        problem = runtime.db.execute("SELECT revision FROM problems WHERE project=? AND id=?", (project, row["problem"])).fetchone()
        if not problem or problem[0] != row["problem_revision"]:
            reason = "candidate_evidence_changed"
        else:
            packet = runtime.review_packet(project, row["problem"])
            proposal = runtime.db.execute("SELECT action,knowledge_id,expected_revision FROM governance_proposals "
                "WHERE project=? AND candidate_id=? AND state='proposed' ORDER BY created DESC LIMIT 1", (project, candidate_id)).fetchone()
            reason = publication_gate(runtime, project, packet, body, dict(proposal) if proposal else
                {"action": "publish", "knowledge_id": None, "expected_revision": None}, mode=policy["mode"])
    return {"candidate_id": candidate_id, "title": body["title"][:200], "category": body["category"],
            "knowledge_kind": body.get("knowledge_kind", "solution"), "validation_level": body["validation_level"],
            "knowledge_tier": knowledge_tier(body), "publication_mode": policy["mode"],
            "policy_enabled": policy["enabled"], "evidence_eligible": reason is None,
            "reason": reason or ("automatic_publication_disabled" if not policy["enabled"] else "eligible_future_review_only"),
            "existing_candidate_requires_approval": True, "model_called": False, "published": False}


def _verified_gate(runtime, project, packet, body, proposal):
    prob_id = packet["problem"]["id"]
    bound_kid = packet["problem"].get("knowledge_id")
    target_kid = proposal.get("knowledge_id")
    same_lineage_update = False
    target_operation_solution = False
    if proposal["action"] in {"update", "attach"} and target_kid:
        target_row = runtime.db.execute(
            "SELECT k.body FROM active a JOIN knowledge k ON k.project=a.project AND k.id=a.id "
            "AND k.revision=a.revision WHERE a.project=? AND a.id=? AND a.revision=?",
            (project, target_kid, proposal["expected_revision"]),
        ).fetchone()
        target_body = json.loads(target_row[0]) if target_row else {}
        target_operation_solution = (target_body.get("category") == "operation"
                                     and target_body.get("knowledge_kind", "solution") == "solution")
        has_unresolved_counterexample = runtime.db.execute(
            "SELECT 1 FROM knowledge_feedback WHERE project=? AND knowledge_id=? AND disposition='unresolved' LIMIT 1",
            (project, target_kid),
        ).fetchone()
        if not has_unresolved_counterexample:
            # 1) 同源案例多轮演进升级 (v1 -> v2)
            same_problem_source = (
                runtime.db.execute(
                    "SELECT 1 FROM knowledge_sources WHERE project=? AND knowledge_id=? AND problem_id=? LIMIT 1",
                    (project, target_kid, prob_id),
                ).fetchone() is not None
            )
            # 2) 日后跨会话修改同一目标源码文件 (write_target) 的迭代升级
            has_file_links = runtime.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_file_links'"
            ).fetchone()
            same_file_target = bool(
                not same_problem_source
                and has_file_links
                and runtime.db.execute(
                    "SELECT 1 FROM problem_events pe "
                    "JOIN work_file_links wfl ON wfl.project=? AND wfl.event_id=pe.event "
                    "JOIN work_file_links kwfl ON kwfl.project=wfl.project AND kwfl.checkout_key=wfl.checkout_key AND kwfl.path_key=wfl.path_key "
                    "JOIN problem_events kpe ON kpe.event=kwfl.event_id "
                    "JOIN knowledge_sources ks ON ks.project=kwfl.project AND ks.problem_id=kpe.problem "
                    "WHERE pe.problem=? AND ks.knowledge_id=? AND wfl.relation='write_target' AND kwfl.relation='write_target' LIMIT 1",
                    (project, prob_id, target_kid),
                ).fetchone()
            )
            same_lineage_update = bool(same_problem_source or same_file_target)
    # 振荡回退边界检查：若本案例在最后一次成功工具验证之后又发生了新的失败/冲突事件，禁止抢发半成品
    latest_fail_seq = runtime.db.execute(
        "SELECT COALESCE(MAX(e.rowid),0) FROM problem_events pe JOIN events e ON e.id=pe.event "
        "WHERE pe.problem=? AND e.project=? AND (pe.role IN ('failure','conflict') OR e.outcome IN ('failed','conflict'))",
        (prob_id, project),
    ).fetchone()[0]
    latest_succ_seq = runtime.db.execute(
        "SELECT COALESCE(MAX(e.rowid),0) FROM problem_events pe JOIN events e ON e.id=pe.event "
        "LEFT JOIN operations o ON o.project=e.project AND o.id=e.operation "
        "WHERE pe.problem=? AND e.project=? AND e.type='PostToolUse' AND e.outcome='succeeded' "
        "AND COALESCE(o.conflict,0)=0 AND COALESCE(o.decision,'')!='denied'",
        (prob_id, project),
    ).fetchone()[0]
    # The accepted candidate must cite the recovery evidence itself.  A newer
    # success elsewhere in the packet cannot validate a stale quoted success.
    cited_success_seq = 0
    for ref in body["evidence_refs"]:
        observed = runtime.db.execute(
            "SELECT e.rowid FROM problem_events pe JOIN events e ON e.id=pe.event "
            "LEFT JOIN operations o ON o.project=e.project AND o.id=e.operation "
            "WHERE pe.problem=? AND e.project=? AND e.id=? AND e.type='PostToolUse' "
            "AND e.outcome='succeeded' AND COALESCE(o.conflict,0)=0 AND COALESCE(o.decision,'')!='denied'",
            (prob_id, project, ref),
        ).fetchone()
        if observed:
            cited_success_seq = max(cited_success_seq, observed[0])
    latest_write_seq = runtime.db.execute(
        "SELECT COALESCE(MAX(e.rowid),0) FROM problem_events pe JOIN events e ON e.id=pe.event "
        "WHERE pe.problem=? AND e.project=? AND e.type='PostToolUse' "
        "AND EXISTS(SELECT 1 FROM work_file_links wfl WHERE wfl.project=e.project "
        "AND wfl.event_id=e.id AND wfl.relation='write_target')",
        (prob_id, project),
    ).fetchone()[0]
    regressed = bool(latest_fail_seq > latest_succ_seq)
    stale_citation = (not cited_success_seq or cited_success_seq <= latest_fail_seq
                      or cited_success_seq < latest_write_seq)
    reason = ("evidence truncated" if packet.get("evidence_truncated") or packet.get("incomplete") else
              "requires trusted tool-success evidence" if body["validation_level"] != "tool_success" else
              "latest observation is unresolved failure" if regressed else
              "requires current cited tool-success evidence" if stale_citation else
              "requires operation solution" if body["category"] != "operation" or body.get("knowledge_kind", "solution") != "solution" else
              "target knowledge requires trusted governance" if target_kid and not target_operation_solution else
              "existing knowledge requires trusted governance" if not (
                  (proposal["action"] == "publish" and not bound_kid) or same_lineage_update
              ) else None)
    return reason
