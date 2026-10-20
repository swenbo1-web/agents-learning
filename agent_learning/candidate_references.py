"""Opt-in, source-checked references to unpublished candidates.

Reference access never grants publication or execution authority.  All tokens in
this module describe public data snapshots; an exposure is not proof of reading,
adoption, or independent verification.  SQLite records remain authoritative.
"""
from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import time


INDEX_VERSION = 1
MAX_RECALL = 64
MAX_EVIDENCE = 64
MAX_LINEAGE = 32
MAX_ANCESTORS = 8
MAX_ASSESSMENTS = 128
MAX_SOURCE_VISITS = 512
MAX_LINEAGE_VISITS = 512
PUBLIC_EVENTS = {"Stop", "PublicMessageSegment", "WorkSummary", "AssistantMessage", "PublicMessage"}


class _Assessment:
    """One bounded traversal, including shared DAG branches and recall results."""

    def __init__(self):
        self.memo = {}
        self.remaining = {"candidates": MAX_ASSESSMENTS, "sources": MAX_SOURCE_VISITS,
                          "lineage": MAX_LINEAGE_VISITS}
        self.exhausted = False

    def take(self, category, count=1):
        if count > self.remaining[category]:
            self.exhausted = True
            return False
        self.remaining[category] -= count
        return True


class ReferenceUnavailable(ValueError):
    """An optional projection is unavailable; callers may omit this channel."""


def _helpers():
    from .runtime import ContractError, clean, digest, encode
    return ContractError, clean, digest, encode


def _transaction(runtime):
    return contextlib.nullcontext() if runtime.db.in_transaction else runtime.transaction()


def _integer(value, name, minimum=0, maximum=1000000000):
    error, _, _, _ = _helpers()
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise error(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _project(runtime, project):
    error, _, _, _ = _helpers()
    if not runtime.db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone():
        raise error("unknown candidate reference project")


def install(runtime):
    """Install candidate storage in the caller's migration transaction."""
    error, _, _, _ = _helpers()
    if not runtime.db.in_transaction:
        raise error("candidate reference migration requires a write transaction")
    statements = (
        "CREATE TABLE IF NOT EXISTS candidate_reference_policy(project TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS candidate_reference_history(project TEXT NOT NULL,job_id TEXT NOT NULL,enabled INTEGER NOT NULL,version INTEGER NOT NULL,signature TEXT NOT NULL,updated REAL NOT NULL,governance_epoch INTEGER NOT NULL DEFAULT -1,PRIMARY KEY(project,job_id))",
        "CREATE TABLE IF NOT EXISTS candidate_reference_exposures(project TEXT NOT NULL,conversation TEXT NOT NULL,agent TEXT NOT NULL,host_epoch INTEGER NOT NULL,candidate_id TEXT NOT NULL,reference_version TEXT NOT NULL,body_digest TEXT NOT NULL,task_id TEXT,task_revision INTEGER,assignment_id TEXT,notified_version TEXT NOT NULL DEFAULT '',checked REAL NOT NULL DEFAULT 0,updated REAL NOT NULL,delivery TEXT NOT NULL DEFAULT 'persistent',context_epoch INTEGER NOT NULL DEFAULT 0,method TEXT NOT NULL DEFAULT 'hook',PRIMARY KEY(project,conversation,agent,host_epoch,candidate_id))",
        "CREATE INDEX IF NOT EXISTS candidate_reference_exposure_task ON candidate_reference_exposures(project,conversation,agent,host_epoch,task_id,task_revision,updated)",
        "CREATE TABLE IF NOT EXISTS candidate_reference_feedback(project TEXT NOT NULL,candidate_id TEXT NOT NULL,reference_version TEXT NOT NULL,problem_id TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'unresolved',summary TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project,candidate_id,reference_version,problem_id))",
        "CREATE TABLE IF NOT EXISTS candidate_reference_receipts(project TEXT NOT NULL,conversation TEXT NOT NULL,agent TEXT NOT NULL,host_epoch INTEGER NOT NULL,idempotency_key TEXT NOT NULL,request_digest TEXT NOT NULL,response TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project,conversation,agent,host_epoch,idempotency_key))",
        "CREATE TABLE IF NOT EXISTS candidate_reference_lineage(project TEXT NOT NULL,event_id TEXT NOT NULL,candidate_id TEXT NOT NULL,reference_version TEXT NOT NULL,relation TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project,event_id,candidate_id,reference_version))",
        "CREATE INDEX IF NOT EXISTS candidate_reference_lineage_event ON candidate_reference_lineage(project,event_id)",
        "CREATE INDEX IF NOT EXISTS candidate_reference_title ON candidates(project,lower(trim(json_extract(body,'$.title'))),status)",
        "CREATE VIRTUAL TABLE IF NOT EXISTS candidate_reference_fts USING fts5(project UNINDEXED,candidate_id UNINDEXED,text,tokenize='trigram')",
        "CREATE TABLE IF NOT EXISTS candidate_reference_index_state(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL)",
    )
    for statement in statements:
        runtime.db.execute(statement)
    if "governance_epoch" not in {row[1] for row in runtime.db.execute("PRAGMA table_info(candidate_reference_history)")}:
        runtime.db.execute("ALTER TABLE candidate_reference_history ADD COLUMN governance_epoch INTEGER NOT NULL DEFAULT -1")
    columns = {row[1] for row in runtime.db.execute("PRAGMA table_info(candidate_reference_exposures)")}
    for name, declaration in {"delivery": "TEXT NOT NULL DEFAULT 'persistent'",
                              "context_epoch": "INTEGER NOT NULL DEFAULT 0",
                              "method": "TEXT NOT NULL DEFAULT 'hook'",
                              "environment_state": "TEXT NOT NULL DEFAULT ''",
                              "pending_notice": "TEXT NOT NULL DEFAULT ''"}.items():
        if name not in columns:
            runtime.db.execute(f"ALTER TABLE candidate_reference_exposures ADD COLUMN {name} {declaration}")
    text = ("coalesce(json_extract(NEW.body,'$.title'),'') || ' ' || "
            "coalesce(json_extract(NEW.body,'$.content'),'') || ' ' || "
            "coalesce(json_extract(NEW.body,'$.applicability'),'') || ' ' || "
            "coalesce(json_extract(NEW.body,'$.limitations'),'')")
    runtime.db.execute("CREATE TRIGGER IF NOT EXISTS candidate_reference_insert AFTER INSERT ON candidates "
        "WHEN NEW.status='candidate' BEGIN INSERT INTO candidate_reference_fts(project,candidate_id,text) "
        f"VALUES(NEW.project,NEW.id,{text}); END")
    runtime.db.execute("CREATE TRIGGER IF NOT EXISTS candidate_reference_delete AFTER DELETE ON candidates "
        "BEGIN DELETE FROM candidate_reference_fts WHERE project=OLD.project AND candidate_id=OLD.id; END")
    runtime.db.execute("CREATE TRIGGER IF NOT EXISTS candidate_reference_update AFTER UPDATE OF body,status,project,id ON candidates "
        "BEGIN DELETE FROM candidate_reference_fts WHERE project=OLD.project AND candidate_id=OLD.id; "
        "INSERT INTO candidate_reference_fts(project,candidate_id,text) "
        f"SELECT NEW.project,NEW.id,{text} WHERE NEW.status='candidate'; END")
    state = runtime.db.execute("SELECT version FROM candidate_reference_index_state WHERE id=1").fetchone()
    if not state or state[0] != INDEX_VERSION:
        runtime.db.execute("DELETE FROM candidate_reference_fts")
        runtime.db.execute("INSERT INTO candidate_reference_fts(project,candidate_id,text) "
            "SELECT project,id,coalesce(json_extract(body,'$.title'),'') || ' ' || "
            "coalesce(json_extract(body,'$.content'),'') || ' ' || coalesce(json_extract(body,'$.applicability'),'') "
            "|| ' ' || coalesce(json_extract(body,'$.limitations'),'') FROM candidates WHERE status='candidate'")
        runtime.db.execute("INSERT OR REPLACE INTO candidate_reference_index_state VALUES(1,?)", (INDEX_VERSION,))
    from .knowledge_lineage import install as install_knowledge_lineage
    install_knowledge_lineage(runtime)


def policy(runtime, project):
    _project(runtime, project)
    if not runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name='candidate_reference_policy'").fetchone():
        return {"enabled": False, "version": 0}
    row = runtime.db.execute("SELECT enabled,version FROM candidate_reference_policy WHERE project=?", (project,)).fetchone()
    return {"enabled": bool(row[0]) if row else False, "version": row[1] if row else 0}


def configure(runtime, project, enabled, expected_version):
    """Trusted UI/CLI only. Worker MCP must not expose this mutation."""
    error, _, _, _ = _helpers()
    if not isinstance(enabled, bool):
        raise error("candidate reference enabled must be boolean")
    _integer(expected_version, "expected_version")
    with _transaction(runtime):
        current = policy(runtime, project)
        if current["version"] != expected_version:
            raise error("candidate reference policy changed; refresh before confirming")
        version = expected_version + 1
        runtime.db.execute("INSERT INTO candidate_reference_policy VALUES(?,?,?,?) ON CONFLICT(project) DO UPDATE SET "
            "enabled=excluded.enabled,version=excluded.version,updated=excluded.updated", (project, int(enabled), version, time.time()))
        return {"enabled": enabled, "version": version}


def history_grant(runtime, project, job_id):
    _project(runtime, project)
    job = runtime.db.execute("SELECT signature FROM history_jobs WHERE project=? AND id=?", (project, job_id)).fetchone()
    error, _, _, _ = _helpers()
    if not job:
        raise error("unknown candidate reference history batch")
    available = runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name='candidate_reference_history'").fetchone()
    row = runtime.db.execute("SELECT * FROM candidate_reference_history WHERE project=? AND job_id=?", (project, job_id)).fetchone() if available else None
    return {"job_id": job_id, "enabled": bool(row and row["enabled"] and row["signature"] == job[0]
            and row["governance_epoch"] == runtime.host_epoch()),
            "version": row["version"] if row else 0, "signature": job[0]}


def grant_history(runtime, project, job_id, enabled, expected_version):
    """Grant only the specified frozen batch, independently of review budgets."""
    error, _, _, _ = _helpers()
    if not isinstance(enabled, bool):
        raise error("history candidate reference grant must be boolean")
    _integer(expected_version, "expected_version")
    with _transaction(runtime):
        current = history_grant(runtime, project, job_id)
        if current["version"] != expected_version:
            raise error("candidate reference history grant changed; refresh before confirming")
        job = runtime.db.execute("SELECT state FROM history_jobs WHERE project=? AND id=?", (project, job_id)).fetchone()
        if enabled and job[0] == "cancelled":
            raise error("cancelled historical batch cannot receive reference access")
        runtime.db.execute("INSERT INTO candidate_reference_history VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(project,job_id) DO UPDATE SET enabled=excluded.enabled,version=excluded.version,"
            "signature=excluded.signature,updated=excluded.updated,governance_epoch=excluded.governance_epoch",
            (project, job_id, int(enabled), expected_version + 1, current["signature"], time.time(), runtime.host_epoch()))
        return {**current, "enabled": enabled, "version": expected_version + 1}


def _access(runtime, project, context_handle):
    access = runtime.access_context(project, context_handle)
    task = runtime.task(project, access["conversation_id"])
    assignment = None
    if access["agent_id"]:
        assignment = runtime.assignment_context(project, access["conversation_id"], access["agent_id"], context_handle)
        error, _, _, _ = _helpers()
        if not assignment or not assignment.get("current"):
            raise error("candidate reference assignment changed; reconnect")
        task = runtime.task_snapshot(project, assignment["task_id"], assignment["task_revision"], assignment["task_write_version"])
    return access, task, assignment


def _redirect(runtime, project, identity):
    row = runtime.db.execute("SELECT knowledge_id,revision FROM knowledge_sources WHERE project=? AND candidate_id=? "
        "ORDER BY created DESC,revision DESC LIMIT 1", (project, identity)).fetchone()
    if not row:
        return None
    active = runtime.db.execute("SELECT revision FROM active WHERE project=? AND id=?", (project, row[0])).fetchone()
    return {"knowledge_id": row[0], "revision": row[1], "active": bool(active and active[0] == row[1])}


def _conflict_state(runtime, project, body, problem):
    from .runtime import knowledge_fingerprint
    snapshot = {}
    fingerprint = knowledge_fingerprint(body)
    matches = [dict(row) for row in runtime.db.execute("SELECT f.id,f.revision,a.revision AS active_revision "
        "FROM knowledge_fingerprints f LEFT JOIN active a ON a.project=f.project AND a.id=f.id "
        "WHERE f.project=? AND f.fingerprint=? LIMIT 17", (project, fingerprint))]
    snapshot["equivalent"] = matches
    if matches:
        if any(row["active_revision"] is None for row in matches):
            return "equivalent_knowledge_revoked", snapshot
        if any(row["revision"] != row["active_revision"] for row in matches):
            return "equivalent_knowledge_superseded", snapshot
        return "equivalent_formal_knowledge", snapshot
    titles = [dict(row) for row in runtime.db.execute("SELECT k.id,k.revision,k.body,a.revision AS active_revision "
        "FROM knowledge k LEFT JOIN active a ON a.project=k.project AND a.id=k.id WHERE k.project=? "
        "AND lower(trim(json_extract(k.body,'$.title')))=lower(trim(?)) LIMIT 17", (project, body["title"]))]
    snapshot["titles"] = titles
    if titles:
        return "knowledge_title_requires_review", snapshot
    from .knowledge_store import problem_feedback_target
    target, _ = problem_feedback_target(runtime, project, problem)
    if target:
        snapshot["target"] = target
        if runtime.db.execute("SELECT 1 FROM knowledge_feedback WHERE project=? AND knowledge_id=? "
                "AND disposition='unresolved' LIMIT 1", (project, target)).fetchone():
            return "unresolved_knowledge_counterexample", snapshot
        return "existing_knowledge_governance", snapshot
    return None, snapshot


def feedback_state(runtime, project, candidate_id):
    """Bounded operator preview; a snapshot token is not approval authority."""
    _, clean, digest, _ = _helpers()
    if not runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name='candidate_reference_feedback'").fetchone():
        return {"unresolved": 0, "version": digest([candidate_id, []]), "items": []}
    rows = [dict(row) for row in runtime.db.execute("SELECT reference_version,problem_id,state,summary,created "
        "FROM candidate_reference_feedback WHERE project=? AND candidate_id=? AND state='unresolved' "
        "ORDER BY created,problem_id LIMIT 33", (project, candidate_id))]
    return {"unresolved": len(rows), "truncated": len(rows) > 32, "version": digest([candidate_id, rows]),
            "items": [{"problem_id": row["problem_id"], "summary": clean(row["summary"])[:240]} for row in rows[:5]]}


def resolve_feedback(runtime, project, candidate_id, expected_version, reason):
    error, clean, _, encode = _helpers()
    if (not isinstance(candidate_id, str) or not 1 <= len(candidate_id) <= 200
            or not isinstance(expected_version, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_version)):
        raise error("invalid candidate concern resolution identity")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise error("resolving candidate concerns requires a bounded explicit reason")
    with _transaction(runtime):
        current = feedback_state(runtime, project, candidate_id)
        if current["version"] != expected_version or current.get("truncated"):
            raise error("candidate concerns changed; review the current records")
        if not current["unresolved"]:
            raise error("candidate has no unresolved reference concerns")
        runtime.db.execute("UPDATE candidate_reference_feedback SET state='resolved' WHERE project=? AND candidate_id=? AND state='unresolved'", (project, candidate_id))
        runtime._event(project, "candidate-reference-control", "CandidateReferenceResolution",
                       {"candidate_id": candidate_id, "reason": clean(reason), "previous_version": expected_version}, outcome="reported")
        return {"candidate_id": candidate_id, "resolved_concerns": current["unresolved"],
                "published": False, "models_started": False}


def held_equivalent(runtime, project, body, *, include_held=True, compare_variants=True):
    """Find an exact risky equivalent without limiting ordinary source history.

    Equality follows ``knowledge_fingerprint``: evidence and source identity do
    not distinguish a claim, while every business field and metadata value do.
    The database filters the risk before returning at most one row.  Sampling
    the latest same-title candidates both locked healthy duplicate merges after
    32 sources and could hide an older hold or concern behind those sources.
    """
    if not runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name='candidate_reference_feedback'").fetchone():
        return None
    from .runtime import METADATA_KEYS
    _, _, _, encode = _helpers()
    # Candidates are validated business objects: these scalar fields are strings
    # and metadata is a bounded string-valued object. JSON extraction handles
    # escaping; symmetric set comparison preserves metadata key-order freedom.
    # No dynamic SQLite UDF registration is needed while approval cursors exist.
    fields = ("title", "content", "category", "applicability", "limitations", "validation_level")
    scalar = " AND ".join(f"json_extract(c.body,'$.{field}') IS json_extract(t.body,'$.{field}')" for field in fields)
    equality = scalar + (" AND coalesce(json_extract(c.body,'$.knowledge_kind'),'solution')="
        "coalesce(json_extract(t.body,'$.knowledge_kind'),'solution') "
        "AND NOT EXISTS (SELECT key,type,atom FROM json_each(c.body,'$.metadata') "
        "EXCEPT SELECT key,type,atom FROM json_each(t.body,'$.metadata')) "
        "AND NOT EXISTS (SELECT key,type,atom FROM json_each(t.body,'$.metadata') "
        "EXCEPT SELECT key,type,atom FROM json_each(c.body,'$.metadata'))")
    source = ("WITH target(body) AS (VALUES (?)) SELECT 1 FROM candidates c,target t WHERE c.project=? "
        "AND lower(trim(json_extract(c.body,'$.title')))=lower(trim(?))")
    args = (encode(body), project, body["title"])
    if include_held and runtime.db.execute(source + " AND c.status IN ('deferred','rejected') AND " + equality + " LIMIT 1", args).fetchone():
        return "equivalent candidate is held"
    if runtime.db.execute(source + " AND EXISTS (SELECT 1 FROM candidate_reference_feedback f "
            "WHERE f.project=c.project AND f.candidate_id=c.id AND f.state='unresolved') AND " + equality + " LIMIT 1", args).fetchone():
        return "equivalent candidate has unresolved concerns"
    # An explicit active-revision update has already selected its comparison
    # target. Exact attachment likewise has a fixed active claim. Neither needs
    # an unbounded generic title comparison; exact concerns above still apply.
    from .runtime import knowledge_fingerprint
    if not compare_variants or runtime.db.execute("SELECT 1 FROM knowledge_fingerprints f JOIN active a "
            "ON a.project=f.project AND a.id=f.id AND a.revision=f.revision "
            "WHERE f.project=? AND f.fingerprint=? LIMIT 1", (project, knowledge_fingerprint(body))).fetchone():
        return None
    # Keep the comparison bound for genuinely different unresolved claims.
    # Rejecting an unrelated alternative is an explicit recovery path; rejected
    # exact equivalents and concerns were checked above, regardless of age.
    variants = [f"json_extract(c.body,'$.{field}')" for field in fields]
    variants.append("coalesce(json_extract(c.body,'$.knowledge_kind'),'solution')")
    variants.extend(f"json_extract(c.body,'$.metadata.{field}')" for field in sorted(METADATA_KEYS))
    if runtime.db.execute("SELECT 1 FROM (SELECT DISTINCT " + ",".join(variants) + " FROM candidates c "
            "WHERE c.project=? AND c.status!='rejected' AND lower(trim(json_extract(c.body,'$.title')))=lower(trim(?)) "
            "LIMIT 33) LIMIT 1 OFFSET 32", (project, body["title"])).fetchone():
        return "candidate comparison requires review"
    return None


def assess(runtime, project, candidate_id, *, _ancestors=(), _respect_policy=True, _evaluation=None):
    """Read one current snapshot. Eligible means usable as a clue, not correct."""
    error, clean, digest, _ = _helpers()
    if not isinstance(candidate_id, str) or not candidate_id or len(candidate_id) > 200:
        raise error("invalid candidate reference identity")
    evaluation = _evaluation if _evaluation is not None else _Assessment()
    # Check the active path before using a shared result: a shorter-path cache
    # must never bypass a longer path's depth limit or an actual cycle.
    blocked = ("candidate_lineage_cycle" if candidate_id in _ancestors or len(_ancestors) >= MAX_ANCESTORS
               else "candidate_lineage_budget_exceeded" if evaluation.exhausted else None)
    if blocked:
        return {"candidate_id": candidate_id, "eligible": False, "reason": blocked,
                "reference_version": digest([project, candidate_id, blocked]), "body_digest": ""}
    memo_key = (candidate_id, MAX_ANCESTORS - len(_ancestors), _respect_policy)
    if memo_key in evaluation.memo:
        return dict(evaluation.memo[memo_key])
    if not evaluation.take("candidates"):
        return {"candidate_id": candidate_id, "eligible": False, "reason": "candidate_lineage_budget_exceeded",
                "reference_version": digest([project, candidate_id, "candidate_lineage_budget_exceeded"]), "body_digest": ""}
    _project(runtime, project)
    row = runtime.db.execute("SELECT * FROM candidates WHERE project=? AND id=?", (project, candidate_id)).fetchone()
    if not row:
        return {"candidate_id": candidate_id, "eligible": False, "reason": "candidate_missing",
                "reference_version": digest([project, candidate_id, "missing"]), "body_digest": ""}
    current_policy = policy(runtime, project)
    snapshot = {"candidate": dict(row), "policy": current_policy}
    try:
        if len(row["body"]) > 30000:
            raise ValueError("oversize body")
        body = json.loads(row["body"])
        if not isinstance(body, dict):
            raise ValueError("invalid body")
        required = ("problem_id", "problem_revision", "title", "content", "category", "applicability", "limitations", "evidence_refs", "validation_level")
        if any(key not in body for key in required):
            raise ValueError("missing body fields")
    except (ValueError, TypeError):
        return {"candidate_id": candidate_id, "eligible": False, "reason": "candidate_body_invalid",
                "reference_version": digest(snapshot), "body_digest": digest(row["body"])}
    reason = None
    if row["status"] != "candidate":
        reason = "candidate_" + row["status"]
    elif _respect_policy and not current_policy["enabled"]:
        reason = "reference_disabled"
    if candidate_id in _ancestors or len(_ancestors) >= MAX_ANCESTORS:
        reason = "candidate_lineage_cycle"
    problem = runtime.db.execute("SELECT * FROM problems WHERE project=? AND id=?", (project, row["problem"])).fetchone()
    snapshot["problem"] = dict(problem) if problem else None
    if (not problem or body["problem_id"] != row["problem"] or body["problem_revision"] != row["problem_revision"]
            or problem["revision"] != row["problem_revision"]):
        reason = reason or "source_case_changed"
    task_snapshot = None
    if problem and problem["task_id"]:
        task_snapshot = runtime.db.execute("SELECT * FROM task_snapshots WHERE project=? AND id=? AND revision=?",
            (project, problem["task_id"], problem["task_revision"])).fetchone()
        if not task_snapshot:
            reason = reason or "source_task_missing"
    snapshot["task"] = dict(task_snapshot) if task_snapshot else None
    refs = body.get("evidence_refs")
    if (not isinstance(refs, list) or not refs or len(refs) > MAX_EVIDENCE
            or any(not isinstance(ref, str) or not ref or len(ref) > 200 for ref in refs)):
        refs = []
        reason = reason or "source_reference_invalid"
    events, successes, failed_sequence, succeeded_sequence = [], False, 0, 0
    for ref in dict.fromkeys(refs):
        if not evaluation.take("sources"):
            reason = reason or "candidate_lineage_budget_exceeded"
            break
        observed = runtime.db.execute("SELECT e.*,e.rowid AS sequence,pe.role,o.conflict AS operation_conflict,o.decision AS operation_decision "
            "FROM problem_events pe JOIN events e ON e.id=pe.event LEFT JOIN operations o ON o.project=e.project AND o.id=e.operation "
            "WHERE pe.problem=? AND e.project=? AND e.id=?", (row["problem"], project, ref)).fetchone()
        if not observed:
            events.append({"id": ref, "missing": True})
            reason = reason or "source_missing"
            continue
        event = dict(observed)
        events.append(event)
        if not problem or (event["conversation"], event["task_id"], event["task_revision"]) != (
                problem["conversation"], problem["task_id"], problem["task_revision"]):
            reason = reason or "source_identity_mismatch"
        if event["operation_conflict"] or event["operation_decision"] == "denied" or event["outcome"] == "conflict":
            reason = reason or "source_operation_conflict"
        # A failed operation can provide a case clue, but an unresolved failure
        # cannot be automatically injected as a recommended pending solution.
        if event["role"] in {"conflict", "feedback"}:
            reason = reason or "source_unresolved_failure"
        if event["outcome"] == "failed":
            failed_sequence = max(failed_sequence, event["sequence"])
        if event["type"] == "PostToolUse" and event["outcome"] == "succeeded" and not (
                event["operation_conflict"] or event["operation_decision"] == "denied"):
            successes = True
            succeeded_sequence = max(succeeded_sequence, event["sequence"])
        if candidate_id not in _ancestors and len(_ancestors) < MAX_ANCESTORS:
            risk = _lineage_risk(runtime, project, ref, (*_ancestors, candidate_id), evaluation)
            snapshot.setdefault("lineage", []).append(risk)
            if risk and risk["blocked"]:
                reason = reason or "source_candidate_lineage_blocked"
    snapshot["events"] = events
    if failed_sequence >= succeeded_sequence and failed_sequence:
        reason = reason or "source_unresolved_failure"
    if body["validation_level"] == "tool_success" and not successes:
        reason = reason or "source_tool_success_missing"
    if body["validation_level"] not in {"reported", "tool_success"}:
        reason = reason or "candidate_validation_invalid"
    histories = [dict(link) for link in runtime.db.execute("SELECT j.id,j.signature,j.created,j.state FROM history_case_links l "
        "JOIN history_jobs j ON j.id=l.job_id AND j.project=l.project WHERE l.project=? AND l.problem_id=? "
        "ORDER BY j.created DESC,j.id DESC LIMIT 1", (project, row["problem"]))]
    if histories:
        grant = history_grant(runtime, project, histories[0]["id"])
        snapshot["history"] = grant
        snapshot["history_state"] = histories[0]["state"]
        if histories[0]["state"] == "cancelled":
            reason = reason or "history_reference_cancelled"
        if not grant["enabled"]:
            reason = reason or "history_reference_not_authorized"
        for event in events:
            try:
                provenance = json.loads(event.get("payload", "{}"))
            except (ValueError, TypeError):
                provenance = {}
            source = provenance.get("source") or {}
            if event.get("type") != "HistoryImport" or not isinstance(source, dict) or not source.get("source_key") or not source.get("content_hash"):
                reason = reason or "history_provenance_incomplete"
    disputed = [dict(item) for item in runtime.db.execute("SELECT reference_version,problem_id,state FROM candidate_reference_feedback "
        "WHERE project=? AND candidate_id=? AND state='unresolved' ORDER BY problem_id LIMIT 33", (project, candidate_id))]
    snapshot["feedback"] = disputed
    if disputed:
        reason = reason or "candidate_disputed"
    proposals = [dict(item) for item in runtime.db.execute("SELECT action,knowledge_id,expected_revision,state "
        "FROM governance_proposals WHERE project=? AND candidate_id=? AND state='proposed' ORDER BY created DESC LIMIT 17",
        (project, candidate_id))]
    snapshot["proposals"] = proposals
    if any(item["action"] != "publish" for item in proposals):
        reason = reason or "existing_knowledge_governance"
    if problem:
        conflict, state = _conflict_state(runtime, project, body, problem)
        snapshot["formal_state"] = state
        reason = reason or conflict
    # Same-title pending alternatives and held duplicates need comparison; a new
    # candidate ID must not silently revive a rejected or deferred equivalent.
    alternatives = [dict(item) for item in runtime.db.execute("SELECT id,status,body FROM candidates WHERE project=? AND id<>? "
        "AND lower(trim(json_extract(body,'$.title')))=lower(trim(?)) ORDER BY created DESC,id LIMIT 17",
        (project, candidate_id, body["title"]))]
    snapshot["alternatives"] = alternatives
    from .runtime import knowledge_fingerprint
    for alternative in alternatives:
        if alternative["status"] == "published":
            continue
        equal = knowledge_fingerprint(json.loads(alternative["body"])) == knowledge_fingerprint(body)
        if equal and alternative["status"] in {"deferred", "rejected"}:
            reason = reason or "equivalent_candidate_held"
        elif not equal and alternative["status"] == "candidate":
            reason = reason or "candidate_title_requires_review"
        if equal and runtime.db.execute("SELECT 1 FROM candidate_reference_feedback WHERE project=? AND candidate_id=? "
                "AND state='unresolved' LIMIT 1", (project, alternative["id"])).fetchone():
            reason = reason or "equivalent_candidate_disputed"
    if len(alternatives) == 17:
        reason = reason or "candidate_title_requires_review"
    if evaluation.exhausted:
        reason = reason or "candidate_lineage_budget_exceeded"
    result = {"candidate_id": candidate_id, "reference_version": digest(snapshot), "body_digest": digest(body),
        "eligible": reason is None, "reason": reason, "status": row["status"], "reference_only": True, "execution_authority": False,
        "unpublished": True, "title": clean(body["title"])[:100], "content_summary": clean(body["content"])[:180],
        "applicability": clean(body["applicability"])[:120], "limitations": clean(body["limitations"])[:140],
        "validation_level": body["validation_level"], "current_tool_success": successes,
        "category": body["category"], "knowledge_kind": body.get("knowledge_kind", "solution"),
        "metadata": clean(body.get("metadata", {})), "problem_id": row["problem"], "problem_revision": row["problem_revision"],
        "source_health": "valid" if reason is None else "blocked", "evidence_count": len(refs),
        "source_event_refs": list(dict.fromkeys(refs)),
        "missing_verification": "Not published or independently accepted; verify against the current task and environment."}
    redirect = _redirect(runtime, project, candidate_id)
    if redirect:
        result["redirect"] = redirect
    evaluation.memo[memo_key] = result
    return result


def _recall(runtime, project, query, anchors=()):
    from .query_language import canonical_text
    original = " ".join(anchors) + " " + query[-8000:] if anchors else query[-8000:]
    canonical = canonical_text(original)
    chunks = []
    for word in re.findall(r"[\u3400-\u9fff]+|[a-z0-9_]+(?:[./:+#-][a-z0-9_+#-]*)*", (original + " " + canonical).lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            chunks.extend(word[index:index + 3] for index in range(max(0, len(word) - 2)))
        elif len(word) >= 3:
            chunks.append(word)
    # Candidate FTS stores raw text. A slash-normalized query must also recall
    # a Windows-spelled source; complete path qualification still happens after
    # recall, so matching a basename cannot borrow another directory's claim.
    chunks = [part for anchor in anchors for part in anchor.split("/") if len(part) >= 3] + chunks
    chunks = list(dict.fromkeys(chunks))[:96]
    if chunks:
        expression = " OR ".join('"' + word.replace('"', '""') + '"' for word in chunks)
        try:
            return runtime.db.execute("SELECT candidate_id FROM candidate_reference_fts WHERE candidate_reference_fts MATCH ? "
                "AND project=? ORDER BY bm25(candidate_reference_fts),candidate_id LIMIT ?",
                (expression, project, MAX_RECALL)).fetchall()
        except sqlite3.OperationalError as exc:
            raise ReferenceUnavailable("candidate reference index unavailable") from exc
    # FTS trigram has no representation for a two-character query. Only this
    # explicit narrow query uses the bounded title/content fallback; empty or
    # single-character prompts never enumerate recent candidates.
    narrow = canonical.strip()
    if len(narrow) != 2 or not re.fullmatch(r"[\w+#]+", narrow):
        return []
    escaped = narrow.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return runtime.db.execute("SELECT id AS candidate_id FROM candidates WHERE project=? AND status='candidate' "
        "AND (json_extract(body,'$.title') LIKE ? ESCAPE '\\' OR json_extract(body,'$.content') LIKE ? ESCAPE '\\') "
        "ORDER BY created DESC,id LIMIT ?", (project, "%" + escaped + "%", "%" + escaped + "%", MAX_RECALL)).fetchall()


def search(runtime, project, query, limit=1, context_handle=None, *, delivery=None,
           context_epoch=0, task_state=None, compact=False, filters=None, workspace=None):
    error, _, _, _ = _helpers()
    _integer(limit, "limit", 1, 3)
    if not isinstance(query, str) or len(query) > 30000:
        raise error("candidate reference query must be a bounded string")
    access = runtime.access_context(project, context_handle) if context_handle is not None else None
    if delivery not in {None, "persistent", "ephemeral"}:
        raise error("unsupported candidate reference delivery")
    _integer(context_epoch, "context_epoch")
    from .runtime import knowledge_filters
    filters = knowledge_filters(filters)
    if not policy(runtime, project)["enabled"] or not query.strip():
        return []
    if context_handle is not None:
        access = _access(runtime, project, context_handle)[0]
    from .knowledge_index import normalized_query_plan, _match_info, _positive_anchors_allowed
    plan = normalized_query_plan(query)
    selected = plan["terms"]
    if not selected or not plan["automatic_safe"]:
        return []
    weights = {term: 2 if len(term) > 1 else 1 for term in selected}
    denominator = sum(weights.values())
    ranked = []
    for row in _recall(runtime, project, query, plan["exact_anchors"]):
        candidate = runtime.db.execute("SELECT body FROM candidates WHERE project=? AND id=? AND status='candidate'",
            (project, row["candidate_id"])).fetchone()
        if not candidate:
            continue
        body = json.loads(candidate[0])
        if any((body.get(key, "solution" if key == "knowledge_kind" else "") if key in {"category", "knowledge_kind"}
                else body.get("metadata", {}).get(key)) != value for key, value in filters.items()):
            continue
        info = _match_info(candidate[0], weights, denominator, plan["meaningful"], _anchors=plan["exact_anchors"])
        if not info["auto_eligible"] or not _positive_anchors_allowed(candidate[0], plan["exact_anchors"]):
            continue
        ranked.append((row["candidate_id"], info))
    ranked.sort(key=lambda match: (-match[1]["score"], match[0]))
    matches = []
    evaluation = _Assessment()
    for identity, info in ranked:
        item = assess(runtime, project, identity, _evaluation=evaluation)
        if evaluation.exhausted:
            break
        if item["eligible"]:
            item = _environment_check(runtime, project, item, workspace)
            if not item:
                continue
            origin = runtime.db.execute("SELECT conversation FROM problems WHERE project=? AND id=?", (project, item["problem_id"])).fetchone()
            if access and origin and origin[0] == access["conversation_id"]:
                continue  # Do not feed an origin conversation its own pending conclusion.
            if context_handle and delivery == "persistent" and not compact and exposed(
                    runtime, project, context_handle, item, context_epoch=context_epoch):
                continue
            item["matched_terms"] = info["matched_terms"]
            item["match_score"] = info["score"]
            matches.append(item)
            if len(matches) >= limit:
                break
    return matches[:limit]


def _environment_state(runtime, project, item, workspace):
    """Bounded observable applicability/file metadata, never a content hash."""
    if workspace is None:
        return None
    from .knowledge_applicability import project_environment, automatic_applicability
    from .work_files import checkout_key, current_file_version
    environment = project_environment(workspace)
    state = {"checkout": checkout_key(workspace), "environment": environment,
             "applicability": automatic_applicability(item, environment), "files": []}
    refs = list(dict.fromkeys(item.get("source_event_refs", ())))[:20]
    if refs:
        marks = ",".join("?" for _ in refs)
        rows = runtime.db.execute(f"SELECT relative_path,version FROM work_file_links WHERE project=? "
            f"AND checkout_key=? AND event_id IN ({marks}) ORDER BY event_id,relative_path LIMIT 16",
            (project, state["checkout"], *refs)).fetchall()
        for row in rows:
            try:
                recorded = json.loads(row["version"])
            except (ValueError, TypeError):
                continue
            if not isinstance(recorded, dict) or recorded.get("status") != "stat_observed":
                continue
            current = current_file_version(row["relative_path"], workspace)
            fields = lambda version: {key: version.get(key) for key in ("status", "size", "mtime_ns")}
            state["files"].append({"path": row["relative_path"], "recorded": fields(recorded),
                                   "current": fields(current)})
    return state


def _environment_blocked(state):
    return bool(state and (state["applicability"]["status"] == "incompatible" or
        any(item["recorded"] != item["current"] for item in state["files"])))


def _environment_check(runtime, project, item, workspace):
    if workspace is None:
        return item
    from .knowledge_applicability import project_environment, automatic_applicability
    from .work_files import knowledge_file_state
    if automatic_applicability(item, project_environment(workspace))["status"] == "incompatible":
        return None
    evidence = {"evidence": [{"event_id": ref} for ref in item.get("source_event_refs", ())]}
    if knowledge_file_state(runtime, project, evidence, workspace).get("source_files_changed"):
        return None
    return item


def revalidate(runtime, project, item, context_handle=None, *, workspace=None):
    if context_handle is not None:
        _access(runtime, project, context_handle)
    current = assess(runtime, project, item["candidate_id"])
    if not current["eligible"] or current["reference_version"] != item.get("reference_version"):
        return None
    if not _environment_check(runtime, project, current, workspace):
        return None
    for key in ("matched_terms", "match_score"):
        if key in item:
            current[key] = item[key]
    return current


def record_exposure(runtime, project, context_handle, item, *, delivery="persistent", context_epoch=0,
                    task_state=None, method="mcp", workspace=None):
    """Record only a complete response actually returned by the caller."""
    with _transaction(runtime):
        access, task, assignment = _access(runtime, project, context_handle)
        error, _, _, _ = _helpers()
        if delivery not in {"persistent", "ephemeral"} or method not in {"hook", "mcp"}:
            raise error("invalid candidate reference exposure method")
        _integer(context_epoch, "context_epoch")
        if task_state is not None and (task_state.get("id"), task_state.get("revision"), task_state.get("assignment_id")) != (
                task["id"] if task else None, task["revision"] if task else None, assignment["assignment_id"] if assignment else None):
            raise error("candidate reference task changed before exposure")
        current = revalidate(runtime, project, item, workspace=workspace)
        if current is None:
            return False
        environment_state = _environment_state(runtime, project, current, workspace)
        now = time.time()
        runtime.db.execute("INSERT INTO candidate_reference_exposures(project,conversation,agent,host_epoch,candidate_id,"
            "reference_version,body_digest,task_id,task_revision,assignment_id,notified_version,checked,updated,delivery,context_epoch,method,environment_state,pending_notice) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'',?,?,?,?,?,?,'') ON CONFLICT(project,conversation,agent,host_epoch,candidate_id) DO UPDATE SET "
            "reference_version=excluded.reference_version,body_digest=excluded.body_digest,task_id=excluded.task_id,"
            "task_revision=excluded.task_revision,assignment_id=excluded.assignment_id,notified_version='',checked=excluded.checked,updated=excluded.updated,"
            "delivery=excluded.delivery,context_epoch=excluded.context_epoch,method=excluded.method,"
            "environment_state=excluded.environment_state,pending_notice=''",
            (project, access["conversation_id"], access["agent_id"] or "", access["host_epoch"], current["candidate_id"],
             current["reference_version"], current["body_digest"], task["id"] if task else None,
             task["revision"] if task else None, assignment["assignment_id"] if assignment else None, now, now,
             delivery, context_epoch, method, json.dumps(environment_state, ensure_ascii=False, sort_keys=True) if environment_state is not None else ""))
        return True


def exposed(runtime, project, context_handle, item, *, context_epoch=0):
    """A persistent-context optimization only; ephemeral hosts must re-present."""
    access, task, assignment = _access(runtime, project, context_handle)
    row = runtime.db.execute("SELECT * FROM candidate_reference_exposures WHERE project=? AND conversation=? "
        "AND agent=? AND host_epoch=? AND candidate_id=?", (project, access["conversation_id"], access["agent_id"] or "",
        access["host_epoch"], item["candidate_id"])).fetchone()
    return bool(row and row["reference_version"] == item["reference_version"] and row["context_epoch"] == context_epoch
        and (row["task_id"], row["task_revision"], row["assignment_id"]) ==
        (task["id"] if task else None, task["revision"] if task else None, assignment["assignment_id"] if assignment else None))


def read(runtime, project, context_handle, candidate_id, expected_version, section="content", offset=0, max_chars=2000, *, workspace=None):
    error, clean, _, encode = _helpers()
    _integer(offset, "offset", 0, 30000)
    _integer(max_chars, "max_chars", 512, 4000)
    if section not in {"content", "applicability", "limitations", "evidence", "summary"}:
        raise error("unsupported candidate reference section")
    with _transaction(runtime):
        _access(runtime, project, context_handle)
        item = assess(runtime, project, candidate_id)
        if item.get("status") == "published" and item.get("redirect"):
            return {"candidate_id": candidate_id, "status": "published", "redirect": item["redirect"], "content": None}
        if not item["eligible"]:
            raise error("candidate reference unavailable: " + item["reason"])
        if not _environment_check(runtime, project, item, workspace):
            raise error("candidate reference file or environment changed; review current sources")
        if not isinstance(expected_version, str) or expected_version != item["reference_version"]:
            raise error("candidate reference changed; retrieve the current reference version")
        body = json.loads(runtime.db.execute("SELECT body FROM candidates WHERE project=? AND id=?", (project, candidate_id)).fetchone()[0])
        if section == "summary":
            content = item["content_summary"]
        elif section == "evidence":
            # Provenance is paged without expanding every event payload.
            content = json.dumps({"problem_id": body["problem_id"], "problem_revision": body["problem_revision"],
                "evidence_refs": body["evidence_refs"]}, ensure_ascii=False, sort_keys=True)
        else:
            content = clean(body[section])
        result = {"candidate_id": candidate_id, "reference_version": item["reference_version"],
            "reference_only": True, "execution_authority": False, "validation_level": item["validation_level"], "source_health": "valid",
            "warning": "Unpublished; verify before use.", "section": section, "offset": offset,
            "total_chars": len(content), "content": "", "next_offset": offset}
        remaining = max(0, len(content) - offset)
        low, high = 0, min(remaining, max_chars)
        while low < high:
            middle = (low + high + 1) // 2
            trial = {**result, "content": content[offset:offset + middle],
                "next_offset": offset + middle if middle < remaining else None}
            if len(encode(trial)) <= max_chars:
                low = middle
            else:
                high = middle - 1
        result["content"] = content[offset:offset + low]
        result["next_offset"] = offset + low if low < remaining else None
        if len(encode(result)) > max_chars or remaining and low == 0:
            raise error("candidate reference read budget too small for identity and status")
        if not record_exposure(runtime, project, context_handle, item, workspace=workspace):
            raise error("candidate reference changed before response")
        return result


def _notification_state(runtime, project, row, item, workspace):
    _, _, digest, _ = _helpers()
    environment = _environment_state(runtime, project, item, workspace)
    prior_environment = json.loads(row["environment_state"]) if row["environment_state"] else None
    environmental_change = bool(environment is not None and (
        prior_environment is not None and environment != prior_environment or
        prior_environment is None and _environment_blocked(environment)))
    changed = item["reference_version"] != row["reference_version"] or environmental_change
    reason = item["reason"] or ("reference_environment_or_files_changed" if environmental_change else "reference_source_changed")
    # An older/non-file caller must not manufacture a second notice merely
    # because it cannot re-observe the workspace captured by a Hook.
    token_environment = environment if environment is not None else prior_environment
    token = digest([row["reference_version"], item["reference_version"], reason, token_environment])
    return changed, reason, token, environment


def notifications(runtime, project, context_handle, *, workspace=None):
    """Bounded rotating checks. Only explicit acknowledgements dismiss notices."""
    _, _, digest, _ = _helpers()
    with _transaction(runtime):
        access = runtime.access_context(project, context_handle)
        rows = runtime.db.execute("SELECT * FROM candidate_reference_exposures WHERE project=? AND conversation=? "
            "AND agent=? AND host_epoch=? ORDER BY checked,candidate_id LIMIT 32",
            (project, access["conversation_id"], access["agent_id"] or "", access["host_epoch"])).fetchall()
        notes, evaluation = [], _Assessment()
        for row in rows:
            item = assess(runtime, project, row["candidate_id"], _evaluation=evaluation)
            changed, reason, notice, environment = _notification_state(runtime, project, row, item, workspace)
            pending = json.loads(row["pending_notice"]) if row["pending_notice"] else None
            if changed and row["notified_version"] != notice:
                pending = {"id": notice, "notice_version": notice, "candidate_id": row["candidate_id"],
                    "reference_version": item["reference_version"], "previous_version": row["reference_version"],
                    "reason": reason, "eligible": item["eligible"],
                    "text": f"待定参考 {row['candidate_id']} 状态、来源或文件环境已变化（{reason}）；请停止依据旧摘要执行，重新检索。"}
                runtime.db.execute("UPDATE candidate_reference_exposures SET pending_notice=? WHERE project=? AND conversation=? "
                    "AND agent=? AND host_epoch=? AND candidate_id=?", (json.dumps(pending, ensure_ascii=False, sort_keys=True),
                    project, access["conversation_id"], access["agent_id"] or "", access["host_epoch"], row["candidate_id"]))
            if pending:
                # A transient change cannot erase an already queued, incomplete
                # warning simply by restoring the captured size/mtime.
                notes.append(pending)
            runtime.db.execute("UPDATE candidate_reference_exposures SET checked=? WHERE project=? AND conversation=? "
                "AND agent=? AND host_epoch=? AND candidate_id=?", (time.time(), project, access["conversation_id"],
                access["agent_id"] or "", access["host_epoch"], row["candidate_id"]))
        return notes


def ack_notifications(runtime, project, context_handle, items, *, workspace=None):
    """Caller passes only notices fully included in its final response budget."""
    error, _, digest, _ = _helpers()
    if not isinstance(items, list) or len(items) > 32:
        raise error("invalid candidate reference notification acknowledgement")
    with _transaction(runtime):
        access = runtime.access_context(project, context_handle)
        count, evaluation = 0, _Assessment()
        for item in items:
            current = assess(runtime, project, item["candidate_id"], _evaluation=evaluation)
            row = runtime.db.execute("SELECT * FROM candidate_reference_exposures WHERE project=? "
                "AND conversation=? AND agent=? AND host_epoch=? AND candidate_id=?", (project, access["conversation_id"],
                access["agent_id"] or "", access["host_epoch"], item["candidate_id"])).fetchone()
            if not row:
                continue
            pending = json.loads(row["pending_notice"]) if row["pending_notice"] else None
            changed, _, expected, environment = _notification_state(runtime, project, row, current, workspace)
            identity = item.get("id", item.get("notice_version"))
            if pending and pending.get("reason") == "reference_environment_or_files_changed" and workspace is None:
                continue  # No current file observation means no environment acknowledgement.
            if not pending or pending.get("id") != identity or changed and identity != expected:
                continue
            state = json.dumps(environment, ensure_ascii=False, sort_keys=True) if environment is not None else row["environment_state"]
            count += runtime.db.execute("UPDATE candidate_reference_exposures SET notified_version=?,pending_notice='',environment_state=? WHERE project=? "
                "AND conversation=? AND agent=? AND host_epoch=? AND candidate_id=?", (identity, state, project,
                access["conversation_id"], access["agent_id"] or "", access["host_epoch"], item["candidate_id"])).rowcount
        return count


def feedback(runtime, project, context_handle, candidate_id, expected_version, problem_id=None, *,
             kind="counterexample", summary="", idempotency_key=None, event_refs=None,
             expected_task_id=None, expected_task_revision=None):
    error, clean, digest, encode = _helpers()
    if kind != "counterexample" or not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
        raise error("candidate counterexample requires a bounded nonempty summary")
    if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 200:
        raise error("candidate counterexample requires an idempotency key")
    request = digest([candidate_id, expected_version, problem_id, summary, event_refs, expected_task_id, expected_task_revision])
    with _transaction(runtime):
        access, task, _ = _access(runtime, project, context_handle)
        key = (project, access["conversation_id"], access["agent_id"] or "", access["host_epoch"], idempotency_key)
        receipt = runtime.db.execute("SELECT request_digest,response FROM candidate_reference_receipts "
            "WHERE project=? AND conversation=? AND agent=? AND host_epoch=? AND idempotency_key=?", key).fetchone()
        if receipt:
            if receipt[0] != request:
                raise error("candidate counterexample idempotency key reused with different input")
            return json.loads(receipt[1])
        exposure = runtime.db.execute("SELECT reference_version FROM candidate_reference_exposures WHERE project=? "
            "AND conversation=? AND agent=? AND host_epoch=? AND candidate_id=?", (project, access["conversation_id"],
            access["agent_id"] or "", access["host_epoch"], candidate_id)).fetchone()
        if not exposure or exposure[0] != expected_version:
            raise error("candidate counterexample requires the reference actually returned in this context")
        current = assess(runtime, project, candidate_id)
        if current.get("status") == "published":
            raise error("candidate was published; report the formal knowledge revision instead")
        if current["reference_version"] != expected_version or not current.get("body_digest"):
            raise error("candidate reference changed; review current sources before feedback")
        if task and (expected_task_id != task["id"] or expected_task_revision != task["revision"]):
            raise error("candidate counterexample requires the current task identity and revision")
        if problem_id:
            observed = runtime.db.execute("SELECT * FROM problems WHERE project=? AND id=?", (project, problem_id)).fetchone()
            if not observed or (observed["conversation"], observed["task_id"], observed["task_revision"]) != (
                    access["conversation_id"], task["id"] if task else None, task["revision"] if task else None):
                raise error("candidate counterexample case must belong to the current task")
        else:
            observed = runtime.feedback(project, access["conversation_id"], summary, category="mixed", event_refs=event_refs,
                context_handle=context_handle, expected_epoch=access["host_epoch"], expected_task_id=expected_task_id,
                expected_task_revision=expected_task_revision, feedback_kind="observation")
            problem_id = observed["id"]
        runtime.db.execute("INSERT OR IGNORE INTO candidate_reference_feedback VALUES(?,?,?,?,?,?,?)",
            (project, candidate_id, expected_version, problem_id, "unresolved", clean(summary), time.time()))
        response = {"candidate_id": candidate_id, "reference_version": expected_version, "problem_id": problem_id,
            "state": "disputed", "published": False, "verification": "reported_counterexample"}
        runtime.db.execute("INSERT INTO candidate_reference_receipts VALUES(?,?,?,?,?,?,?,?)", (*key, request, encode(response), time.time()))
        return response


def record_lineage(runtime, project, event_id, references, relation="explicit_derivation"):
    """Store explicit citations separately from possible temporal influence."""
    error, _, _, _ = _helpers()
    if relation not in {"explicit_derivation", "explicit_reference", "possible_influence"}:
        raise error("invalid candidate lineage relation")
    if not isinstance(references, list) or len(references) > MAX_LINEAGE:
        raise error("candidate lineage must contain at most 32 references")
    with _transaction(runtime):
        event = runtime.db.execute("SELECT 1 FROM events WHERE project=? AND id=?", (project, event_id)).fetchone()
        if not event:
            raise error("candidate lineage event is outside this project")
        for item in references:
            identity, version = item.get("candidate_id"), item.get("reference_version")
            if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version) or not runtime.db.execute(
                    "SELECT 1 FROM candidates WHERE project=? AND id=?", (project, identity)).fetchone():
                raise error("invalid candidate lineage reference")
            runtime.db.execute("INSERT INTO candidate_reference_lineage VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(project,event_id,candidate_id,reference_version) DO UPDATE SET relation="
                "CASE WHEN excluded.relation='possible_influence' THEN candidate_reference_lineage.relation ELSE excluded.relation END",
                (project, event_id, identity, version, relation, time.time()))
        return len(references)


def influence_for_event(runtime, project, event):
    if isinstance(event, str):
        event = runtime.db.execute("SELECT * FROM events WHERE project=? AND id=?", (project, event)).fetchone()
    if not event or event["project"] != project or event["type"] not in PUBLIC_EVENTS:
        return []
    # Session epoch is taken from the event's verified host-owned conversation;
    # foreign host callers cannot attach another host's current context.
    if not runtime.owns_conversation(event["conversation"]):
        return []
    rows = runtime.db.execute("SELECT candidate_id,reference_version FROM candidate_reference_exposures "
        "WHERE project=? AND conversation=? AND agent=? AND host_epoch=? AND task_id IS ? AND task_revision IS ? "
        "AND assignment_id IS ? "
        "AND updated<=? ORDER BY updated DESC,candidate_id LIMIT ?", (project, event["conversation"], event["agent"] or "",
        runtime.session_epoch(), event["task_id"], event["task_revision"], event["assignment_id"], event["created"], MAX_LINEAGE)).fetchall()
    return [dict(row) for row in rows]


def record_possible_influence(runtime, project, event_id):
    items = influence_for_event(runtime, project, event_id)
    return record_lineage(runtime, project, event_id, items, "possible_influence") if items else 0


def _lineage_risk(runtime, project, event_id, ancestors=(), evaluation=None):
    evaluation = evaluation if evaluation is not None else _Assessment()
    rows = runtime.db.execute("SELECT candidate_id,reference_version,relation FROM candidate_reference_lineage "
        "WHERE project=? AND event_id=? ORDER BY candidate_id,reference_version LIMIT ?", (project, event_id, MAX_LINEAGE + 1)).fetchall()
    if not rows:
        return None
    if not evaluation.take("lineage", len(rows)):
        return {"blocked": True, "reason": "candidate_lineage_budget_exceeded", "references": [],
                "possible_influence": any(row["relation"] == "possible_influence" for row in rows)}
    refs, blocked = [], len(rows) > MAX_LINEAGE
    for row in rows[:MAX_LINEAGE]:
        current = assess(runtime, project, row["candidate_id"], _ancestors=ancestors, _respect_policy=False,
                         _evaluation=evaluation)
        reason = current["reason"]
        # A successful publication redirects provenance. It does not resurrect
        # a candidate if that formal revision was later revoked or superseded.
        formal = None
        if reason == "candidate_published":
            redirect = current.get("redirect")
            from .knowledge_lineage import published_reference_state
            formal = published_reference_state(runtime, project, redirect, evaluation,
                                               (*ancestors, row["candidate_id"]))
            bad = formal["blocked"]
            if bad:
                reason = formal["reason"]
        else:
            bad = reason is not None
        blocked |= bad or evaluation.exhausted
        refs.append({**dict(row), "reason": reason, "blocked": bad,
                     "current_version": current.get("reference_version"),
                     **({"formal_state": formal} if formal is not None else {})})
        if evaluation.exhausted:
            break
    return {"blocked": blocked, "reason": "candidate_reference_lineage_risk" if blocked else None,
        "references": refs, "possible_influence": any(row["relation"] == "possible_influence" for row in rows)}


def lineage_risk(runtime, project, event_id):
    if not runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name='candidate_reference_lineage'").fetchone():
        return None
    return _lineage_risk(runtime, project, event_id)
