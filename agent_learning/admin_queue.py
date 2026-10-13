"""送审扫描与后台派发的有界状态；缓存失效只唤醒比较，不直接调用模型。"""
from __future__ import annotations

import time
import uuid


def install_queue(runtime):
    runtime.db.execute("CREATE TABLE IF NOT EXISTS admin_review_checks("
                       "project TEXT NOT NULL,problem TEXT NOT NULL,revision INTEGER NOT NULL,"
                       "signature TEXT NOT NULL,task_updated REAL,updated REAL NOT NULL,"
                       "PRIMARY KEY(project,problem))")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS admin_preflights("
                       "project TEXT NOT NULL,problem TEXT NOT NULL,revision INTEGER NOT NULL,"
                       "signature TEXT NOT NULL,task_updated REAL,reason TEXT NOT NULL,updated REAL NOT NULL,"
                       "PRIMARY KEY(project,problem))")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS admin_dispatch("
                       "project TEXT PRIMARY KEY,token TEXT NOT NULL,host_epoch INTEGER NOT NULL,"
                       "lease_until REAL NOT NULL,created REAL NOT NULL)")
    if "blocked_call" not in {row[1] for row in runtime.db.execute("PRAGMA table_info(admin_review_checks)")}:
        runtime.db.execute("ALTER TABLE admin_review_checks ADD COLUMN blocked_call TEXT")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS admin_queue_epochs(project TEXT PRIMARY KEY,candidates INTEGER NOT NULL DEFAULT 0)")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS admin_case_epochs(project TEXT NOT NULL,problem TEXT NOT NULL,"
                       "revision INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(project,problem))")
    for event, row in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
        runtime.db.execute("CREATE TRIGGER IF NOT EXISTS admin_candidates_" + event.lower() + " AFTER " + event + " ON candidates "
                           "BEGIN INSERT INTO admin_queue_epochs(project,candidates) VALUES(" + row + ".project,1) "
                           "ON CONFLICT(project) DO UPDATE SET candidates=candidates+1; END")
    runtime.db.execute("CREATE TRIGGER IF NOT EXISTS admin_review_retry_wakeup AFTER UPDATE OF state,retry_allowed ON admin_calls "
                       "WHEN OLD.state IS NOT NEW.state OR OLD.retry_allowed IS NOT NEW.retry_allowed "
                       "BEGIN DELETE FROM admin_review_checks WHERE blocked_call=NEW.id; END")
    runtime.db.execute("CREATE TRIGGER IF NOT EXISTS admin_review_origin_wakeup AFTER UPDATE OF revision ON problems "
                       "WHEN OLD.revision IS NOT NEW.revision BEGIN "
                       "INSERT INTO admin_case_epochs(project,problem,revision) "
                       "SELECT DISTINCT r.project,r.problem,1 FROM review_receipts r "
                       "WHERE r.project=NEW.project AND r.duplicate_of IN (SELECT call_id FROM review_receipts "
                       "WHERE project=NEW.project AND problem=NEW.id AND duplicate_of IS NULL) "
                       "ON CONFLICT(project,problem) DO UPDATE SET revision=revision+1; "
                       "DELETE FROM admin_review_checks "
                       "WHERE project=NEW.project AND blocked_call IN (SELECT call_id FROM review_receipts "
                       "WHERE project=NEW.project AND problem=NEW.id); END")


def scan_signature(runtime, project, *, include_candidates=True):
    from .review_packet import PACKET_FORMAT, REVIEW_PREFIX
    from .runtime import digest
    row = runtime.db.execute("SELECT generation,source_epoch FROM projects WHERE id=?", (project,)).fetchone()
    candidates = runtime.db.execute("SELECT candidates FROM admin_queue_epochs WHERE project=?", (project,)).fetchone()
    # 发布变化允许重新比较；真正复审仍由实际比较资料的review_signature判定。
    scope = runtime._session_scope()
    observations = [list(item) for item in runtime.db.execute(
        "SELECT conversation,model,active,version FROM admin_model_observations "
        "WHERE project=? AND host_scope=? AND session_epoch=? ORDER BY conversation",
        (project, scope, runtime.session_epoch()))]
    return digest({"policy": runtime.admin_effective_config(project), "model_observations": observations,
                   "host_scope": scope, "packet_format": PACKET_FORMAT,
                   "instructions": REVIEW_PREFIX, "knowledge_changes": list(row) if row else None,
                   "candidate_changes": (candidates[0] if candidates else 0) if include_candidates else None})


def eligible_query(signature, problem_id=None, *, include_cached=False, preflight_signature=None, history_job_id=None):
    sql = ("SELECT p.*,t.updated AS source_task_updated FROM problems p LEFT JOIN task_snapshots t "
           "ON t.project=p.project AND t.id=p.task_id AND t.revision=p.task_revision "
           "WHERE p.project=? AND p.status='pending' AND (? IS NULL OR p.id=?) "
           "AND (p.task_id IS NULL OR t.id IS NOT NULL) "
           "AND NOT EXISTS(SELECT 1 FROM operations o WHERE o.project=p.project AND o.conversation=p.conversation "
           "AND o.task_id IS p.task_id AND o.task_revision IS p.task_revision AND o.state IN ('running','started')) "
           "AND NOT EXISTS(SELECT 1 FROM admin_calls c WHERE c.project=p.project AND c.problem=p.id "
           "AND c.revision=p.revision AND c.retry_allowed=0 AND c.state NOT IN ('not_started','deduplicated') "
           "AND NOT EXISTS(SELECT 1 FROM review_receipts r WHERE r.project=c.project AND r.call_id=c.id)) ")
    args = (problem_id, problem_id)
    if history_job_id is None:
        sql += "AND NOT EXISTS(SELECT 1 FROM history_case_links h WHERE h.project=p.project AND h.problem_id=p.id) "
    else:
        sql += "AND EXISTS(SELECT 1 FROM history_case_links h WHERE h.project=p.project AND h.problem_id=p.id AND h.job_id=?) "
        args += (history_job_id,)
    if problem_id is None and not include_cached:
        for table in ("admin_preflights", "admin_review_checks"):
            sql += ("AND NOT EXISTS(SELECT 1 FROM " + table + " f WHERE f.project=p.project AND f.problem=p.id "
                    "AND f.revision=p.revision AND f.signature=? AND f.task_updated IS t.updated) ")
            args += (preflight_signature or signature,) if table == "admin_preflights" else (signature,)
    return sql, args


def remember_check(runtime, project, selected, signature, blocked_call=None):
    runtime.db.execute("INSERT INTO admin_review_checks VALUES(?,?,?,?,?,?,?) ON CONFLICT(project,problem) "
                       "DO UPDATE SET revision=excluded.revision,signature=excluded.signature,"
                       "task_updated=excluded.task_updated,updated=excluded.updated,blocked_call=excluded.blocked_call",
                       (project, selected["id"], selected["revision"], signature,
                        selected["source_task_updated"], time.time(), blocked_call))


def executor_query(runtime, project, query, args):
    """current 仅排入有本宿主根模型观察的日常案例，避免异宿主旧案例堵塞。"""
    policy = runtime.admin_policy(project)
    if policy["model_mode"] == "current":
        provider = runtime.host if policy["provider"] == "host" else policy["provider"]
        if provider != runtime.host:
            return query + "AND 0 ", args
        query += ("AND EXISTS(SELECT 1 FROM admin_model_observations m WHERE m.project=p.project "
                  "AND m.conversation=p.conversation AND m.host_scope=? AND m.session_epoch=? AND m.active=1) ")
        args += (runtime._session_scope(), runtime.session_epoch())
    return query, args


def ready_executor(runtime, project, query, args):
    from .admin_providers import executor_availability
    query, args = executor_query(runtime, project, query, args)
    for item in runtime.db.execute(query + "ORDER BY p.created,p.id LIMIT 10", (project, *args)).fetchall():
        try:
            plan = runtime.resolve_admin_executor(project, item["conversation"])
            available = executor_availability(plan)["cli_available"]
        except ValueError:
            continue
        if available:
            return True
    return False


def claim_dispatch(runtime, project, *, expected_epoch=None, _phase_guard=None):
    """Hook先预留后台进程位置；实际模型额度仍由claim_call统一预留。"""
    from .admin_lifecycle import ACTIVE_STATES, recover_calls
    from .admin_usage import admin_budget_status
    if not runtime.admin_policy(project)["enabled"] or runtime.config["admin"]["max_calls_per_day"] <= 0:
        return None
    with runtime.transaction():
        runtime.assert_host_epoch(expected_epoch)
        if _phase_guard:
            _phase_guard()
        recover_calls(runtime)
        now = time.time()
        runtime.db.execute("DELETE FROM admin_dispatch WHERE lease_until<=? OR host_epoch!=?",
                           (now, runtime.host_epoch()))
        if runtime.db.execute("SELECT 1 FROM admin_dispatch WHERE project=?", (project,)).fetchone():
            return None
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        if runtime.db.execute("SELECT 1 FROM admin_calls WHERE project=? AND state IN (" + placeholders + ")",
                              (project, *ACTIVE_STATES)).fetchone():
            return None
        budget = admin_budget_status(runtime, now=now)
        if budget["admission_status"] != "available":
            return None
        # 已在审核的派发与调用占同一槽，不能重复计算并发占用。
        dispatched = runtime.db.execute("SELECT COUNT(*) FROM admin_dispatch d WHERE NOT EXISTS("
                                        "SELECT 1 FROM admin_calls c WHERE c.project=d.project "
                                        "AND c.state IN ('reserved','launch_intent','started'))").fetchone()[0]
        if dispatched + budget["global_active_calls"] >= budget["max_concurrent_calls"]:
            return None
        query, args = eligible_query(scan_signature(runtime, project),
                                     preflight_signature=scan_signature(runtime, project, include_candidates=False))
        if not ready_executor(runtime, project, query, args):
            return None
        token = str(uuid.uuid4())
        runtime.db.execute("INSERT INTO admin_dispatch VALUES(?,?,?,?,?)",
                           (project, token, runtime.host_epoch(), now + runtime.config["admin"]["max_batch_seconds"] + 30, now))
        return token


def release_dispatch(runtime, project, token):
    if token:
        runtime.db.execute("DELETE FROM admin_dispatch WHERE project=? AND token=?", (project, token))
