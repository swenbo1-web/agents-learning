"""持久管理员决定；调用是否计费与业务决定是否可复用分别处理。"""
from __future__ import annotations

import contextlib
import json
import time

from .review_packet import PACKET_FORMAT, REVIEW_PREFIX, REVIEW_SUFFIX


DECISIONS = {"candidate", "governance", "needs_evidence", "skip", "preflight_blocked"}
REUSABLE_DECISIONS = {"candidate", "governance", "needs_evidence", "skip"}
_STATES = {"candidate": "candidate_ready", "needs_evidence": "awaiting_evidence",
           "governance": "proposal_ready", "skip": "skipped", "preflight_blocked": "awaiting_evidence"}
_TRANSPORT = {"created", "updated", "timestamp", "conversation_id", "session_id", "agent_id"}


def _transaction(runtime):
    return contextlib.nullcontext() if runtime.db.in_transaction else runtime.transaction()


def install_decisions(runtime):
    """复用迁移外层事务；不改变案例解决状态或已有调用计账。"""
    with _transaction(runtime):
        runtime.db.execute("""CREATE TABLE IF NOT EXISTS review_receipts(
            project TEXT NOT NULL,problem TEXT NOT NULL,revision INTEGER NOT NULL,
            review_signature TEXT NOT NULL,evidence_signature TEXT NOT NULL,call_id TEXT,
            decision TEXT NOT NULL,reason TEXT NOT NULL,response TEXT NOT NULL,
            created REAL NOT NULL,dependency_signature TEXT NOT NULL,dependencies TEXT NOT NULL,
            duplicate_of TEXT,PRIMARY KEY(project,problem,revision,review_signature))""")
        runtime.db.execute("CREATE INDEX IF NOT EXISTS review_receipts_reuse "
                           "ON review_receipts(project,review_signature,decision,created)")
        runtime.db.execute("CREATE INDEX IF NOT EXISTS review_receipts_call ON review_receipts(project,call_id)")
        runtime.db.execute("CREATE INDEX IF NOT EXISTS review_receipts_candidate_origin "
                           "ON review_receipts(project,json_extract(response,'$.candidate_id'),created DESC) "
                           "WHERE decision='candidate'")
        runtime.db.execute("""CREATE TABLE IF NOT EXISTS review_states(
            project TEXT NOT NULL,problem TEXT NOT NULL,revision INTEGER NOT NULL,
            review_signature TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,
            updated REAL NOT NULL,PRIMARY KEY(project,problem))""")


def _business_reference(item, candidate=False):
    """引用身份/修订保留；候选案例归属与原事件身份不制造新的业务知识。"""
    from .runtime import clean

    if not isinstance(item, dict):
        return clean(item)
    excluded = _TRANSPORT | ({"problem", "problem_id", "problem_revision", "evidence_refs"} if candidate else set())
    result = {key: clean(value) for key, value in item.items() if key not in excluded}
    if candidate and isinstance(result.get("body"), dict):
        result["body"] = {key: value for key, value in result["body"].items()
                          if key not in {"problem_id", "problem_revision", "evidence_refs"}}
    return result


def dependencies(packet, evidence_signature=None):
    """只跟踪本次实际比较资料，避免全库发布唤醒全部旧案例。"""
    from .runtime import encode

    result = {}
    for key in ("related_knowledge", "related_candidates"):
        values = packet.get(key) or []
        if key == "related_candidates" and evidence_signature:
            # 原材料已经产生的候选是该次决定的产物，不能又触发同材料重新调用模型。
            values = [item for item in values if not isinstance(item, dict)
                      or item.get("origin_evidence_signature") != evidence_signature]
        result[key] = sorted((_business_reference(item, key == "related_candidates") for item in values), key=encode)
    result["target_knowledge"] = _business_reference(packet.get("target_knowledge"))
    result["review_context_revision"] = packet.get("review_context_revision", 0)
    return result


def dependency_signature(packet, evidence_signature=None):
    from .runtime import digest
    return digest(dependencies(packet, evidence_signature))


def review_signature(packet, cfg, evidence_signature):
    from .runtime import clean, digest

    # 并发/日额度/超时属于调度；输入预算与发布规则改变审核可见性或结论用途。
    policy = {key: clean(value) for key, value in cfg.items()
              if key not in {"enabled", "max_calls_per_day", "timeout_seconds", "max_cases_per_batch",
                             "max_calls_per_batch", "max_batch_seconds"}}
    return digest({"format": 1, "evidence": evidence_signature, "dependencies": dependencies(packet, evidence_signature),
                   "instructions": REVIEW_PREFIX + REVIEW_SUFFIX, "packet_format": PACKET_FORMAT,
                   "policy": policy})


def _missing_evidence(response):
    """兼容旧 reason；不调用模型猜测缺证字段。"""
    from .runtime import clean

    values = response.get("missing_evidence")
    if isinstance(values, str):
        values = [values]
    if isinstance(values, list):
        result = []
        for value in values[:8]:
            if isinstance(value, str) and value.strip():
                result.append({"description": clean(value).strip()[:240], "structured": True})
            elif isinstance(value, dict):
                item = {key: clean(value[key])[:240] for key in ("field", "description", "required")
                        if isinstance(value.get(key), str)}
                if item:
                    item["structured"] = True
                    result.append(item)
        if result:
            return result
    if response.get("decision") in {"needs_evidence", "preflight_blocked"}:
        return [{"description": clean(response.get("reason", ""))[:1000], "structured": False}]
    return []


def persist_receipt(runtime, project, packet, evidence_signature, review_signature, call_id,
                    response, duplicate_of=None):
    from .runtime import ContractError, clean, digest, encode

    decision = response.get("decision", response.get("status")) if isinstance(response, dict) else None
    if not isinstance(decision, str) or decision not in DECISIONS:
        raise ContractError("only completed administrator decisions or preflight blocks have review receipts")
    if not isinstance(response.get("reason"), str):
        raise ContractError("review receipt requires a reason")
    body = clean(response)
    body["decision"] = decision
    body["reason"] = body["reason"][:1000]
    body["missing_evidence"] = _missing_evidence(body)
    review_state = _STATES[decision]
    if decision == "candidate" and isinstance(body.get("publication"), dict) and body["publication"].get("status") == "published":
        review_state = "published"
    if len(encode(body)) > 50000:
        raise ContractError("review receipt exceeds response limit")
    problem = packet["problem"]
    dependency = dependencies(packet, evidence_signature)
    created = time.time()
    with _transaction(runtime):
        install_decisions(runtime)
        current = runtime.db.execute("SELECT revision FROM problems WHERE project=? AND id=?",
                                     (project, problem["id"])).fetchone()
        if current is None or current["revision"] != problem["revision"]:
            raise ContractError("problem changed; cannot persist a stale review decision")
        previous = runtime.db.execute(
            "SELECT response FROM review_receipts WHERE project=? AND problem=? AND revision=? AND review_signature=?",
            (project, problem["id"], problem["revision"], review_signature)).fetchone()
        if previous and previous["response"] != encode(body):
            raise ContractError("review decision identity already has a different response")
        runtime.db.execute("INSERT OR IGNORE INTO review_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (project, problem["id"], problem["revision"], review_signature, evidence_signature,
                            call_id, body["decision"], body["reason"], encode(body), created,
                            digest(dependency), encode(dependency), duplicate_of))
        runtime.db.execute("INSERT INTO review_states VALUES(?,?,?,?,?,?,?) "
                           "ON CONFLICT(project,problem) DO UPDATE SET revision=excluded.revision,"
                           "review_signature=excluded.review_signature,state=excluded.state,reason=excluded.reason,updated=excluded.updated",
                           (project, problem["id"], problem["revision"], review_signature,
                            review_state, body["reason"], created))
    return {"problem_id": problem["id"], "problem_revision": problem["revision"],
            "review_signature": review_signature, "review_state": review_state}


def _receipt(row):
    result = dict(row)
    result["response"] = json.loads(result["response"])
    result["dependencies"] = json.loads(result["dependencies"])
    return result


def find_receipt(runtime, project, signature):
    install_decisions(runtime)
    row = runtime.db.execute("SELECT r.* FROM review_receipts r JOIN problems p ON p.project=r.project AND p.id=r.problem "
                             "AND p.revision=r.revision WHERE r.project=? AND r.review_signature=? AND r.duplicate_of IS NULL "
                             "AND r.decision IN ('candidate','governance','needs_evidence','skip') ORDER BY r.created DESC,r.problem LIMIT 1",
                             (project, signature)).fetchone()
    return _receipt(row) if row else None


def receipt_for_problem(runtime, project, problem_id, revision, signature):
    install_decisions(runtime)
    row = runtime.db.execute("SELECT r.* FROM review_receipts r WHERE r.project=? AND r.problem=? AND r.revision=? "
                             "AND r.review_signature=? AND (r.duplicate_of IS NULL OR EXISTS(SELECT 1 FROM review_receipts origin "
                             "JOIN problems p ON p.project=origin.project AND p.id=origin.problem AND p.revision=origin.revision "
                             "WHERE origin.project=r.project AND origin.call_id=r.duplicate_of AND origin.duplicate_of IS NULL))",
                             (project, problem_id, revision, signature)).fetchone()
    return _receipt(row) if row else None


def get_reviews(runtime, project, problem_id=None, limit=20):
    """列表返回真实决定的轻量预览，完整响应仍持久保存供内部复用。"""
    from .runtime import ContractError, encode

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ContractError("review list limit must be an integer from 1 to 100")
    install_decisions(runtime)
    rows = runtime.db.execute("SELECT r.*,s.state,p.revision AS current_problem_revision,COALESCE(e.revision,0) AS current_context_revision FROM review_receipts r "
                              "LEFT JOIN problems p ON p.project=r.project AND p.id=r.problem LEFT JOIN review_states s "
                              "ON s.project=r.project AND s.problem=r.problem AND s.revision=r.revision "
                              "AND s.review_signature=r.review_signature LEFT JOIN admin_case_epochs e ON e.project=r.project "
                              "AND e.problem=r.problem WHERE r.project=? AND (? IS NULL OR r.problem=?) "
                              "ORDER BY r.created DESC,r.problem LIMIT ?", (project, problem_id, problem_id, limit + 1)).fetchall()
    items = []
    for row in rows[:limit]:
        body = json.loads(row["response"])
        item = {key: row[key] for key in ("problem", "revision", "review_signature", "call_id", "decision", "reason",
                                         "created", "duplicate_of", "state")}
        item["missing_evidence"] = body.get("missing_evidence", [])
        item["current_problem_revision"] = row["current_problem_revision"]
        item["current_material"] = (row["revision"] == row["current_problem_revision"]
                                    and json.loads(row["dependencies"]).get("review_context_revision", 0) == row["current_context_revision"])
        for key in ("candidate_id", "proposal_id", "previous_decision"):
            if key in body:
                item[key] = body[key]
        candidate = body.get("candidate")
        if isinstance(candidate, dict):
            item["candidate"] = {key: candidate[key][:200] for key in ("title", "category", "validation_level", "knowledge_kind")
                                 if isinstance(candidate.get(key), str)}
        if len(encode({"items": items + [item], "truncated": False, "limit": limit})) > 12000:
            break
        items.append(item)
    return {"items": items, "truncated": len(items) < len(rows), "limit": limit}
