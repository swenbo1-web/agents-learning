"""管理员只提出治理动作；受信入口按精确来源与修订执行并保存回执。"""
from __future__ import annotations

import json
import time


ACTIONS = {"publish", "update", "revoke", "attach", "dismiss_counterexample"}


def install_governance(runtime):
    """历史 reviewed 不证明反例已处置；迁移统一保留 unresolved。"""
    from .context_storage import _dirty_sql

    with runtime._case_transaction():
        columns = {row[1] for row in runtime.db.execute("PRAGMA table_info(knowledge_feedback)")}
        changed = "disposition" not in columns
        for name, declaration in {
            "disposition": "TEXT NOT NULL DEFAULT 'unresolved'",
            "disposition_reason": "TEXT",
            "disposition_receipt": "TEXT",
        }.items():
            if name not in columns:
                runtime.db.execute(f"ALTER TABLE knowledge_feedback ADD COLUMN {name} {declaration}")
        runtime.db.execute("""CREATE TABLE IF NOT EXISTS governance_proposals(
            id TEXT PRIMARY KEY,project TEXT NOT NULL,problem_id TEXT NOT NULL,
            problem_revision INTEGER NOT NULL,action TEXT NOT NULL,candidate_id TEXT,
            knowledge_id TEXT,expected_revision INTEGER,reason TEXT NOT NULL,
            evidence_refs TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'proposed',
            result TEXT,created REAL NOT NULL,completed REAL,
            FOREIGN KEY(project,problem_id) REFERENCES problems(project,id))""")
        runtime.db.execute("CREATE INDEX IF NOT EXISTS governance_pending ON governance_proposals(project,state,created,id)")
        if changed:
            for statement in _dirty_sql("SELECT DISTINCT project,knowledge_id,revision FROM knowledge_feedback").split(";"):
                if statement.strip():
                    runtime.db.execute(statement)
        affected = "SELECT NEW.project AS project,NEW.knowledge_id AS knowledge_id,NEW.revision AS revision"
        runtime.db.execute("CREATE TRIGGER IF NOT EXISTS governance_feedback_disposition "
                           "AFTER UPDATE OF disposition,disposition_reason,disposition_receipt ON knowledge_feedback "
                           "WHEN OLD.disposition IS NOT NEW.disposition "
                           "OR OLD.disposition_reason IS NOT NEW.disposition_reason "
                           "OR OLD.disposition_receipt IS NOT NEW.disposition_receipt "
                           f"BEGIN {_dirty_sql(affected)} END")


def _problem(runtime, project, problem_id, revision):
    from .runtime import ContractError

    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ContractError("problem_revision must be a positive integer")
    row = runtime.db.execute("SELECT * FROM problems WHERE project=? AND id=?", (project, problem_id)).fetchone()
    if not row or row["revision"] != revision:
        raise ContractError("problem changed or is absent; review current evidence")
    return row


def _sources(runtime, project, problem, refs, dismiss=False):
    from .runtime import ContractError

    if not refs:
        raise ContractError("governance requires actual source evidence")
    supporting = False
    for ref in refs:
        event = runtime.db.execute(
            "SELECT e.*,o.conflict,o.decision FROM problem_events p JOIN events e ON p.event=e.id "
            "LEFT JOIN operations o ON o.project=e.project AND o.id=e.operation "
            "WHERE p.problem=? AND e.project=? AND e.id=?", (problem["id"], project, ref)).fetchone()
        if not event or (event["task_id"], event["task_revision"]) != (problem["task_id"], problem["task_revision"]):
            raise ContractError("governance source is absent or outside this problem")
        payload = json.loads(event["payload"])
        # 反例自身不能同时充当驳回反例的唯一依据，允许额外真实检查或观察。
        public_observation = event["type"] in {
            "Feedback", "Stop", "PublicMessageSegment", "AssistantMessage", "PublicMessage", "WorkSummary"}
        supporting |= (not event["conflict"] and event["decision"] != "denied"
                       and (event["type"] == "PostToolUse" and event["outcome"] in {"succeeded", "call_completed"}
                            or public_observation and event["outcome"] == "reported")
                       and not (event["type"] == "Feedback" and payload.get("feedback_kind") == "counterexample"))
    if dismiss and not supporting:
        raise ContractError("dismissing a counterexample requires additional supporting evidence")


def propose_governance(runtime, project, problem_id, problem_revision, action, candidate_id=None,
                       knowledge_id=None, expected_revision=None, reason="", evidence_refs=None,
                       expected_epoch=None):
    from .runtime import ContractError, clean, digest, encode

    if not isinstance(action, str) or action not in ACTIONS or not isinstance(reason, str) or not reason.strip():
        raise ContractError("valid governance action and nonempty reason are required")
    has_candidate = action in {"publish", "update", "attach"}
    has_target = action != "publish"
    if has_candidate != bool(candidate_id) or has_target != bool(knowledge_id):
        raise ContractError("governance action has incorrect candidate or knowledge target")
    if has_target and (isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1):
        raise ContractError("target actions require a positive expected_revision")
    if not has_target and expected_revision is not None:
        raise ContractError("publication has no target revision")
    if evidence_refs is not None and (not isinstance(evidence_refs, list) or
            not all(isinstance(ref, str) and ref.strip() for ref in evidence_refs)):
        raise ContractError("evidence_refs must be event IDs")
    with runtime._case_transaction():
        runtime.assert_host_epoch(expected_epoch)
        runtime.history_guard(project, problem_id, expected_epoch=expected_epoch,
                              _local_action="propose", _local_candidate=candidate_id)
        problem = _problem(runtime, project, problem_id, problem_revision)
        candidate = None
        if has_candidate:
            candidate = runtime.db.execute("SELECT * FROM candidates WHERE project=? AND id=?", (project, candidate_id)).fetchone()
            if not candidate or (candidate["problem"], candidate["problem_revision"]) != (problem_id, problem_revision):
                raise ContractError("candidate belongs to another problem or revision")
            runtime._validate_sources(project, json.loads(candidate["body"]))
        refs = sorted(set(evidence_refs or (json.loads(candidate["body"])["evidence_refs"] if candidate else [])))
        _sources(runtime, project, problem, refs, dismiss=action == "dismiss_counterexample")
        if has_target:
            if not runtime.db.execute("SELECT 1 FROM knowledge WHERE project=? AND id=? AND revision=?",
                                      (project, knowledge_id, expected_revision)).fetchone():
                raise ContractError("unknown target knowledge revision")
            from .knowledge_store import problem_feedback_target
            feedback_target = problem_feedback_target(runtime, project, problem)
            if feedback_target[0] and feedback_target != (knowledge_id, expected_revision):
                raise ContractError("governance target differs from the case knowledge revision")
        if action == "dismiss_counterexample" and not runtime.db.execute(
                "SELECT 1 FROM knowledge_feedback WHERE project=? AND problem_id=? AND knowledge_id=? "
                "AND revision=? AND kind='counterexample'", (project, problem_id, knowledge_id, expected_revision)).fetchone():
            raise ContractError("case has no matching counterexample")
        body = clean(dict(project=project, problem_id=problem_id, problem_revision=problem_revision, action=action,
                          candidate_id=candidate_id, knowledge_id=knowledge_id, expected_revision=expected_revision,
                          reason=reason.strip(), evidence_refs=refs))
        if len(encode(body)) > 30000:
            raise ContractError("governance proposal is too large")
        ident = digest(body)[:32]
        previous = runtime.db.execute("SELECT state,result FROM governance_proposals WHERE project=? AND id=?", (project, ident)).fetchone()
        if previous:
            return {"proposal_id": ident, "state": previous["state"], "action": action}
        runtime.db.execute("INSERT INTO governance_proposals(id,project,problem_id,problem_revision,action,candidate_id,"
                           "knowledge_id,expected_revision,reason,evidence_refs,state,created) VALUES(?,?,?,?,?,?,?,?,?,?,'proposed',?)",
                           (ident, project, problem_id, problem_revision, action, candidate_id, knowledge_id,
                            expected_revision, body["reason"], encode(refs), time.time()))
        return {"proposal_id": ident, "state": "proposed", "action": action}


def execute_governance(runtime, project, proposal_id, expected_epoch=None):
    """只由受信操作者入口调用；提议工具自身不能执行、发布或撤销。"""
    from .runtime import ContractError, encode

    with runtime._case_transaction():
        runtime.assert_host_epoch(expected_epoch)
        proposal = runtime.db.execute("SELECT * FROM governance_proposals WHERE project=? AND id=?", (project, proposal_id)).fetchone()
        if not proposal:
            raise ContractError("unknown governance proposal")
        if proposal["state"] == "executed":
            return json.loads(proposal["result"])
        if proposal["state"] != "proposed":
            raise ContractError("governance proposal is not executable")
        runtime.history_guard(project, proposal["problem_id"], expected_epoch=expected_epoch,
                              _local_action="publish", _local_candidate=proposal["candidate_id"])
        problem = _problem(runtime, project, proposal["problem_id"], proposal["problem_revision"])
        action = proposal["action"]
        _sources(runtime, project, problem, json.loads(proposal["evidence_refs"]), dismiss=action == "dismiss_counterexample")
        target = None
        if action != "publish":
            from .knowledge_store import problem_feedback_target
            feedback_target = problem_feedback_target(runtime, project, problem)
            if feedback_target[0] and feedback_target != (proposal["knowledge_id"], proposal["expected_revision"]):
                raise ContractError("governance target differs from the case knowledge revision")
            target = runtime.db.execute("SELECT k.* FROM active a JOIN knowledge k ON k.project=a.project AND k.id=a.id "
                                        "AND k.revision=a.revision WHERE a.project=? AND a.id=?", (project, proposal["knowledge_id"])).fetchone()
            if not target or target["revision"] != proposal["expected_revision"]:
                raise ContractError("knowledge active revision changed or is revoked")
        result = {"proposal_id": proposal_id, "state": "executed", "action": action}
        if action in {"publish", "update", "attach"}:
            candidate = runtime.db.execute("SELECT * FROM candidates WHERE project=? AND id=?", (project, proposal["candidate_id"])).fetchone()
            if not candidate or (candidate["problem"], candidate["problem_revision"]) != (problem["id"], problem["revision"]):
                raise ContractError("candidate changed or belongs to another problem")
            body = json.loads(candidate["body"])
            runtime._validate_sources(project, body)
            if target is not None:
                from .knowledge_equivalence import same_presentation_claim
                equivalent = same_presentation_claim(body, json.loads(target["body"]))
                if action == "attach" and not equivalent:
                    raise ContractError("attach requires equivalent knowledge; use an explicit update")
                if action == "update" and equivalent:
                    raise ContractError("update requires changed knowledge; use attach or explicit counterexample dismissal")
            result["publication"] = runtime.publish(project, proposal["candidate_id"],
                knowledge_id=proposal["knowledge_id"], expected_revision=proposal["expected_revision"], expected_epoch=expected_epoch)
            if action == "update" and result["publication"]["revision"] <= proposal["expected_revision"]:
                raise ContractError("update did not create a new revision; counterexample remains unresolved")
        elif action == "revoke":
            result["revocation"] = runtime.revoke(project, proposal["knowledge_id"], proposal["expected_revision"],
                request_id="governance:" + proposal_id, reason=proposal["reason"], actor="governance_operator",
                expected_epoch=expected_epoch)
            runtime.db.execute("UPDATE problems SET status='reviewed' WHERE project=? AND id=?", (project, problem["id"]))
        else:
            runtime.db.execute("UPDATE problems SET status='reviewed' WHERE project=? AND id=?", (project, problem["id"]))
        disposition = {"update": "revised", "revoke": "revoked", "dismiss_counterexample": "dismissed"}.get(action)
        if disposition:
            changed = runtime.db.execute("UPDATE knowledge_feedback SET disposition=?,disposition_reason=?,disposition_receipt=? "
                "WHERE project=? AND problem_id=? AND knowledge_id=? AND revision=? AND kind='counterexample'",
                (disposition, proposal["reason"], proposal_id, project, problem["id"], proposal["knowledge_id"], proposal["expected_revision"])).rowcount
            result["disposition"] = {"state": disposition, "changed_count": changed}
            # 发布/撤销已排队；最终反例处置也必须进入同一同步状态快照。
            if target is not None:
                current = runtime.db.execute("SELECT * FROM knowledge WHERE project=? AND id=? ORDER BY revision DESC LIMIT 1",
                                             (project, proposal["knowledge_id"])).fetchone()
                if current:
                    runtime._queue_sync(project, current["id"], current["revision"], json.loads(current["body"]))
        runtime.db.execute("UPDATE governance_proposals SET state='executed',result=?,completed=? WHERE project=? AND id=?",
                           (encode(result), time.time(), project, proposal_id))
        runtime.db.execute("UPDATE review_states SET state=?,updated=? WHERE project=? AND problem=? AND revision=?",
                           ("published" if action in {"publish", "update", "attach"} else "handled",
                            time.time(), project, problem["id"], problem["revision"]))
        return result
