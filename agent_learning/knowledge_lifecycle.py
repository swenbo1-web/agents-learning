"""知识生命周期动作回执与状态版本；迁移不编造历史操作。"""
from __future__ import annotations

import json
import time
import uuid


def install_lifecycle(runtime):
    with runtime._case_transaction():
        runtime.db.execute("""CREATE TABLE IF NOT EXISTS knowledge_lifecycle_versions(
            project TEXT NOT NULL,knowledge_id TEXT NOT NULL,version INTEGER NOT NULL CHECK(version>=0),
            PRIMARY KEY(project,knowledge_id))""")
        runtime.db.execute("""CREATE TABLE IF NOT EXISTS knowledge_actions(
            project TEXT NOT NULL,request_id TEXT NOT NULL,input_digest TEXT NOT NULL,input TEXT NOT NULL,
            action TEXT NOT NULL,knowledge_id TEXT NOT NULL,revision INTEGER NOT NULL,
            actor TEXT NOT NULL,reason TEXT NOT NULL,evidence_refs TEXT NOT NULL,
            before_state TEXT NOT NULL,after_state TEXT NOT NULL,response TEXT NOT NULL,created REAL NOT NULL,
            PRIMARY KEY(project,request_id),
            FOREIGN KEY(project,knowledge_id,revision) REFERENCES knowledge(project,id,revision))""")
        runtime.db.execute("CREATE INDEX IF NOT EXISTS knowledge_actions_history "
                           "ON knowledge_actions(project,knowledge_id,created,request_id)")


def lifecycle_state(runtime, project, knowledge_id):
    active = runtime.db.execute("SELECT revision FROM active WHERE project=? AND id=?", (project, knowledge_id)).fetchone()
    latest = runtime.db.execute("SELECT MAX(revision) FROM knowledge WHERE project=? AND id=?", (project, knowledge_id)).fetchone()[0]
    version = runtime.db.execute("SELECT version FROM knowledge_lifecycle_versions WHERE project=? AND knowledge_id=?",
                                 (project, knowledge_id)).fetchone()
    return {"active_revision": active[0] if active else None, "latest_revision": latest,
            "lifecycle_version": version[0] if version else 0}


def publication_lineage_state(runtime, project, body, knowledge_id=None, candidate_id=None):
    """A new publication cannot silently restore an equivalent retired claim.

    Explicit updates keep their own active-revision contract. A revoked record
    can only be reactivated through restore; a counterexample has its separate
    governance action. Return the exact comparison state for approval snapshots.
    """
    from .runtime import knowledge_fingerprint
    from .candidate_references import feedback_state, held_equivalent

    concerns = feedback_state(runtime, project, candidate_id) if candidate_id else None
    held = held_equivalent(runtime, project, body, include_held=False)
    issues = []
    if concerns and concerns["unresolved"]:
        issues.append({"code": "candidate_reference_disputed", "message": "Candidate reference concerns require explicit resolution"})
    elif held:
        issues.append({"code": "equivalent_candidate_disputed" if "unresolved concerns" in held else "candidate_comparison_limit",
                       "message": held})
    result = {"issues": issues, "matches": [], "feedback": [], "candidate_concerns": concerns,
              "equivalent_candidate_hold": held}
    from .knowledge_lineage import source_state
    lineage = source_state(runtime, project, body.get("evidence_refs", ()),
                           ancestors=(candidate_id,) if candidate_id else ())
    result["source_lineage"] = lineage
    if lineage["blocked"]:
        issues.append({"code": "candidate_reference_source_blocked",
                       "message": "Candidate source lineage requires review; approval does not resolve upstream concerns"})
    if knowledge_id is not None:
        return result
    rows = runtime.db.execute("SELECT f.id,f.revision,a.revision AS active_revision "
        "FROM knowledge_fingerprints f LEFT JOIN active a ON a.project=f.project AND a.id=f.id "
        "WHERE f.project=? AND f.fingerprint=? ORDER BY f.id,f.revision LIMIT 65",
        (project, knowledge_fingerprint(body))).fetchall()
    matches, feedback = [dict(row) for row in rows], []
    result.update(matches=matches, feedback=feedback)
    if len(rows) > 64:
        issues.append({"code": "equivalent_knowledge_limit", "message": "Too many equivalent knowledge revisions; explicit governance is required"})
        return result
    current_equivalent = {row["id"] for row in rows if row["revision"] == row["active_revision"]}
    if any(row["active_revision"] is None for row in rows):
        issues.append({"code": "equivalent_knowledge_revoked", "message": "Equivalent knowledge was revoked; use explicit restore after checking the evidence"})
    elif any(row["id"] not in current_equivalent for row in rows):
        issues.append({"code": "equivalent_knowledge_superseded", "message": "Equivalent knowledge was superseded; compare the current revision through explicit governance"})
    for identity in sorted({row["id"] for row in rows}):
        feedback.extend(dict(row) for row in runtime.db.execute(
            "SELECT knowledge_id,revision,problem_id,disposition,disposition_reason,disposition_receipt "
            "FROM knowledge_feedback WHERE project=? AND knowledge_id=? AND kind='counterexample' "
            "AND disposition='unresolved' ORDER BY revision,problem_id LIMIT 65", (project, identity)))
    if feedback:
        issues.append({"code": "equivalent_knowledge_disputed", "message": "Equivalent knowledge has an unresolved counterexample; resolve it through explicit governance"})
    return result


def request_identity(request_id, reason, actor, evidence_refs=None):
    from .runtime import ContractError, clean

    if request_id is not None and (not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200):
        raise ContractError("request_id must be a nonempty string of at most 200 characters")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise ContractError("a nonempty reason of at most 4000 characters is required")
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        raise ContractError("actor must be a nonempty string of at most 200 characters")
    if evidence_refs is not None and (not isinstance(evidence_refs, list) or len(evidence_refs) > 100 or
            not all(isinstance(ref, str) and ref.strip() and len(ref) <= 200 for ref in evidence_refs)):
        raise ContractError("evidence_refs must contain at most 100 event IDs")
    return request_id or uuid.uuid4().hex, clean(reason.strip()), clean(actor.strip()), sorted(set(evidence_refs or []))


def action_request(runtime, project, request_id, request):
    from .runtime import ContractError, clean, digest

    row = runtime.db.execute("SELECT input_digest,response FROM knowledge_actions WHERE project=? AND request_id=?",
                             (project, request_id)).fetchone()
    if row:
        if row["input_digest"] != digest(clean(request)):
            raise ContractError("request_id reused with different knowledge action")
        return json.loads(row["response"])
    return None


def record_action(runtime, project, request_id, request, action, knowledge_id, revision, response,
                  *, actor, reason, evidence_refs, before_state):
    """调用方持有知识变更事务；回执或同步失败时所有状态一起回滚。"""
    from .runtime import clean, digest, encode

    request, response = clean(request), clean(response)
    changed = response.get("changed") is not False and response.get("status") != "unchanged"
    if changed:
        runtime.db.execute("INSERT INTO knowledge_lifecycle_versions VALUES(?,?,1) "
                           "ON CONFLICT(project,knowledge_id) DO UPDATE SET version=version+1", (project, knowledge_id))
    after = lifecycle_state(runtime, project, knowledge_id)
    before_state = dict(before_state)
    before_state["event_watermark"] = runtime.db.execute("SELECT COALESCE(MAX(rowid),0) FROM events WHERE project=?", (project,)).fetchone()[0]
    runtime.db.execute("INSERT INTO knowledge_actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (project, request_id, digest(request), encode(request), action, knowledge_id, revision,
                        clean(actor), clean(reason), encode(evidence_refs), encode(clean(before_state)), encode(clean(after)), encode(response), time.time()))
    callback = getattr(runtime, "on_lifecycle_change", None)
    if changed and callback:
        callback(project, knowledge_id, revision, action)
    return response


def validate_restore_support(runtime, project, knowledge_id, revision, reason, refs, validation_level):
    """反例相关恢复必须另有新观察，历史来源和反例自身不能证明已修复。"""
    from .runtime import ContractError

    feedback = runtime.db.execute("SELECT created FROM knowledge_feedback WHERE project=? AND knowledge_id=? "
                                  "AND revision=? AND kind='counterexample' AND disposition IN ('unresolved','revoked')",
                                  (project, knowledge_id, revision)).fetchall()
    if not feedback and not refs:
        return
    if feedback and (reason == "explicit restore" or not refs):
        raise ContractError("counterexample restoration requires a specific reason and new supporting evidence")
    revoked = runtime.db.execute("SELECT before_state FROM knowledge_actions WHERE project=? AND knowledge_id=? "
                                 "AND revision=? AND action='revoke' ORDER BY created DESC,rowid DESC LIMIT 1",
                                 (project, knowledge_id, revision)).fetchone()
    # Windows 时钟分辨率会使相邻写入同刻；项目事件序号明确区分撤销前后观察。
    watermark = json.loads(revoked[0]).get("event_watermark", 0) if revoked else 0
    counterexamples = runtime.db.execute("SELECT COALESCE(MAX(e.rowid),0) FROM knowledge_feedback f "
        "JOIN problem_events pe ON pe.problem=f.problem_id JOIN events e ON e.id=pe.event AND e.project=f.project "
        "WHERE f.project=? AND f.knowledge_id=? AND f.revision=? AND f.disposition IN ('unresolved','revoked') "
        "AND ((e.type='Feedback' AND json_extract(e.payload,'$.feedback_kind')='counterexample') "
        "OR e.outcome IN ('failed','conflict'))", (project, knowledge_id, revision)).fetchone()[0]
    watermark = max(watermark, counterexamples)
    old_refs = {ref for row in runtime.db.execute("SELECT evidence_refs FROM knowledge_sources WHERE project=? AND knowledge_id=?",
                                                 (project, knowledge_id)) for ref in json.loads(row[0])}
    supporting = False
    for ref in refs:
        event = runtime.db.execute("SELECT e.rowid AS event_sequence,e.*,o.conflict,o.decision FROM events e LEFT JOIN operations o "
                                   "ON o.project=e.project AND o.id=e.operation WHERE e.project=? AND e.id=?", (project, ref)).fetchone()
        if not event or event["conflict"] or event["decision"] == "denied" or event["outcome"] in {"failed", "conflict", "denied"}:
            raise ContractError("restoration evidence is absent or no longer valid")
        payload = json.loads(event["payload"])
        new_support = ((not feedback or (ref not in old_refs and event["event_sequence"] > watermark))
                       and not (event["type"] == "Feedback" and payload.get("feedback_kind") == "counterexample"))
        if not new_support:
            continue
        # 来源仍必须属于一个当前案例及其真实任务快照，不接受游离事件。
        problems = runtime.db.execute("SELECT p.* FROM problems p JOIN problem_events pe ON pe.problem=p.id "
                                      "WHERE p.project=? AND pe.event=?", (project, ref)).fetchall()
        valid = False
        for problem in problems:
            if problem["knowledge_id"] and (problem["knowledge_id"], problem["knowledge_revision"]) != (knowledge_id, revision):
                continue
            try:
                runtime._validate_sources(project, {"problem_id": problem["id"], "problem_revision": problem["revision"],
                    "evidence_refs": [ref], "validation_level": validation_level})
            except ContractError:
                continue
            valid = True
            break
        supporting |= valid
    if not supporting:
        raise ContractError("restoration requires new valid supporting evidence beyond original sources and counterexamples")
