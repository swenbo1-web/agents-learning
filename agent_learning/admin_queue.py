"""送审扫描与后台派发的有界状态；缓存失效只唤醒比较，不直接调用模型。"""
from __future__ import annotations

from .phase_review import scope as phase_scope, source_conversation as phase_conversation

import time
import uuid


# A known executor failure must not permanently pin the oldest problem at the
# head of every bounded scan. Unknown/paid calls remain excluded by eligibility.
REVIEW_ORDER = ("ORDER BY COALESCE((SELECT MAX(c.finished) FROM admin_calls c WHERE c.project=p.project "
                "AND c.problem=p.id AND c.revision=p.revision AND c.state='not_started'),0),"
                "CASE WHEN p.knowledge_id IS NOT NULL THEN 0 "
                "WHEN p.resolution_status='recovery_observed' THEN 1 "
                "WHEN EXISTS(SELECT 1 FROM problem_events pe JOIN events e ON e.id=pe.event "
                "WHERE pe.problem=p.id AND e.project=p.project AND e.type='PostToolUse' AND e.outcome='succeeded') THEN 2 "
                "WHEN EXISTS(SELECT 1 FROM problem_events pe JOIN events e ON e.id=pe.event "
                "WHERE pe.problem=p.id AND e.project=p.project AND e.type NOT IN ('PreToolUse','PostToolUse')) THEN 3 "
                "ELSE 4 END,p.created,p.id ")


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
    columns = {row[1] for row in runtime.db.execute("PRAGMA table_info(admin_dispatch)")}
    for name, declaration in {"phase": "TEXT NOT NULL DEFAULT 'reserved'", "claimed_at": "REAL", "worker_pid": "INTEGER",
                              "host_scope": "TEXT NOT NULL DEFAULT ''"}.items():
        if name not in columns:
            runtime.db.execute("ALTER TABLE admin_dispatch ADD COLUMN " + name + " " + declaration)
    # Legacy reservations never had a worker acknowledgement. Bound their
    # remaining life rather than declaring an old dispatch to be running.
    runtime.db.execute("UPDATE admin_dispatch SET lease_until=MIN(lease_until,created+30) WHERE claimed_at IS NULL")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS admin_executor_failures("
                       "project TEXT NOT NULL,host_scope TEXT NOT NULL,signature TEXT NOT NULL,"
                       "failures INTEGER NOT NULL,next_try REAL NOT NULL,reason TEXT NOT NULL,updated REAL NOT NULL,"
                       "PRIMARY KEY(project,host_scope,signature))")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS admin_dispatch_diagnostics("
                       "project TEXT NOT NULL,host_scope TEXT NOT NULL,reason TEXT NOT NULL,details TEXT NOT NULL,"
                       "updated REAL NOT NULL,PRIMARY KEY(project,host_scope))")
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


def _scan_state(runtime, project):
    from .review_packet import PACKET_FORMAT, REVIEW_PREFIX
    row = runtime.db.execute("SELECT generation,source_epoch FROM projects WHERE id=?", (project,)).fetchone()
    candidates = runtime.db.execute("SELECT candidates FROM admin_queue_epochs WHERE project=?", (project,)).fetchone()
    # 发布变化允许重新比较；真正复审仍由实际比较资料的review_signature判定。
    scope = runtime._session_scope()
    policy = runtime.admin_policy(project)
    if policy["model_mode"] == "first":
        observations = runtime.admin_first_model_binding(project)
    else:
        observations = [list(item) for item in runtime.db.execute(
            "SELECT conversation,model,active,version FROM admin_model_observations "
            "WHERE project=? AND host_scope=? AND session_epoch=? ORDER BY conversation",
            (project, scope, runtime.session_epoch()))]
    provider = runtime.host if policy["provider"] == "host" else policy["provider"]
    origins = list(runtime.db.execute("SELECT COUNT(*),MAX(rowid) FROM events WHERE project=? AND type='DshProfileBound'",
                                     (project,)).fetchone()) if provider == "dsh" else None
    return {"policy": runtime.admin_effective_config(project), "model_observations": observations,
            "dsh_profile_origins": origins,
            "work_summary_policy": {"configuration": runtime.config["work_memory"],
                                    "effective": runtime.work_summary_policy(project)},
            "host_scope": scope, "packet_format": PACKET_FORMAT,
            "instructions": REVIEW_PREFIX, "knowledge_changes": list(row) if row else None,
            "candidate_changes": candidates[0] if candidates else 0}


def scan_signature(runtime, project, *, include_candidates=True):
    from .runtime import digest
    state = _scan_state(runtime, project)
    if not include_candidates:
        state["candidate_changes"] = None
    return digest(state)


def scan_signatures(runtime, project):
    """同一次扫描读取状态，生成原有审核／补证签名；不跨调用缓存。"""
    from .runtime import digest
    state = _scan_state(runtime, project)
    return digest(state), digest({**state, "candidate_changes": None})


def eligible_query(signature, problem_id=None, *, include_cached=False, preflight_signature=None, history_job_id=None,
                   summary_enabled=False, summary_host_scope=None, phase_scope=None):
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
    if phase_scope is not None and problem_id is None and history_job_id is None:
        sql += ("AND EXISTS(SELECT 1 FROM phase_review_batches b WHERE b.project=p.project AND b.problem_id=p.id "
                "AND b.host_scope=? AND b.state='pending' AND b.session_epoch=(SELECT epoch FROM host_scopes WHERE scope=b.host_scope) "
                "AND b.governance_epoch=(SELECT epoch FROM host_state WHERE id=1)) ")
        args += (phase_scope,)
    elif problem_id is None and history_job_id is None:
        # Also used by read-only pre-v20 browsers, where phase tables do not
        # exist. The reserved conversation prefix identifies internal batches.
        sql += "AND p.conversation NOT LIKE 'phase-review:%' "
    if summary_enabled and summary_host_scope:
        sql += ("AND NOT EXISTS(SELECT 1 FROM work_summary_links w WHERE w.project=p.project AND w.problem_id=p.id "
                "AND (w.state!='pending' OR w.host_scope!=?)) ")
        args += (summary_host_scope,)
    else:
        sql += "AND NOT EXISTS(SELECT 1 FROM work_summary_links w WHERE w.project=p.project AND w.problem_id=p.id) "
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


def routing_signature(runtime, project):
    """Shared executor changes invalidate scans; source-model changes are per case."""
    from .runtime import digest
    cfg = runtime.admin_effective_config(project)
    # Resolving `current` without a source conversation observes every chat and
    # may alternate between an aggregate model and ambiguity. Neither changes
    # an existing case's own executor, so do not put that aggregate in this key.
    plan = (cfg.get("executor") if cfg["model_mode"] != "current" else None) or {
        "provider": cfg["provider"], "command": cfg["command"], "model": cfg["model"],
        "session_epoch": runtime.session_epoch()}
    if plan["provider"] == "dsh" and not plan.get("dsh_profile"):
        # A current-model aggregate may be unavailable even while a trusted
        # profile is configured. Profile/configuration drift still invalidates
        # every affected executor check; resolving identity grants no routing.
        from .dsh_profile import resolve_profile
        try:
            plan = {**plan, "dsh_profile": resolve_profile(cfg.get("dsh_profile", ""))}
        except ValueError:
            pass
    failures = [list(row) for row in runtime.db.execute(
        "SELECT signature,reason,next_try,updated FROM admin_executor_failures WHERE project=? AND host_scope=? "
        "AND (next_try>? OR reason='configuration_change_required') ORDER BY signature",
        (project, runtime._session_scope(), time.time()))]
    return digest([runtime._session_scope(), runtime.host_epoch(), runtime.admin_policy(project),
                   executor_signature(plan), cfg.get("dsh_profile"), failures])


def _case_route_signature(runtime, signature):
    """SQL expression, evaluated for one `problems p` in the same queue snapshot."""
    # The policy mode is part of signature. Only current-model cases depend on
    # a mutable root observation; default/fixed/first resolve independently.
    source = ("COALESCE((SELECT json_extract(batch.packet,'$.executor_conversation') "
              "FROM phase_review_batches batch WHERE batch.project=p.project "
              "AND batch.problem_id=p.id),p.conversation)")
    expression = ("(? || ':' || COALESCE((SELECT json_array(model.conversation,model.model,model.active,model.version) "
        "FROM admin_model_observations model WHERE model.project=p.project AND model.host_scope=? "
        "AND model.session_epoch=? AND model.conversation=" + source + "),'missing'))")
    return expression, (signature, runtime._session_scope(), runtime.session_epoch())


def _route_check(runtime, project, signature):
    if runtime.admin_policy(project)["model_mode"] == "current":
        return _case_route_signature(runtime, signature)
    return "?", (signature,)


def remember_executor_check(runtime, project, selected, signature):
    expression, args = _route_check(runtime, project, signature)
    current = runtime.db.execute("SELECT " + expression + " FROM problems p WHERE p.project=? AND p.id=?",
                                  (*args, project, selected["id"])).fetchone()
    if current is not None:
        remember_check(runtime, project, selected, current[0], blocked_call="executor_scan")


def executor_review_order(runtime, project, signature):
    # Existing checks are a disposable projection. A routing check neither
    # accepts evidence nor hides a case from eligible_query's review signature.
    expression, args = _route_check(runtime, project, signature)
    return ("ORDER BY COALESCE((SELECT route.updated FROM admin_review_checks route "
            "WHERE route.project=p.project AND route.problem=p.id AND route.revision=p.revision "
            "AND route.blocked_call='executor_scan' AND route.signature=" + expression + "),0),"
            + REVIEW_ORDER.removeprefix("ORDER BY "), args)


def routing_scan_pending(runtime, project, query, args):
    query, args = executor_query(runtime, project, query, args)
    signature = routing_signature(runtime, project)
    expression, check_args = _route_check(runtime, project, signature)
    return bool(runtime.db.execute(query + "AND NOT EXISTS(SELECT 1 FROM admin_review_checks route "
        "WHERE route.project=p.project AND route.problem=p.id AND route.revision=p.revision "
        "AND route.blocked_call='executor_scan' AND route.signature=" + expression + ") LIMIT 1",
        (project, *args, *check_args)).fetchone())


def executor_query(runtime, project, query, args):
    """current 仅排入有本宿主根模型观察的日常案例，避免异宿主旧案例堵塞。"""
    policy = runtime.admin_policy(project)
    provider = runtime.host if policy["provider"] == "host" else policy["provider"]
    if provider == "dsh":
        from .admin_routing import validate_admin_routing_config, _default_provider_command
        from .admin_providers import dsh_sdk_launcher
        config = validate_admin_routing_config(runtime.config["admin"])
        command = policy["command"] or _default_provider_command(config, provider)
        try:
            sdk = dsh_sdk_launcher(command)
        except (OSError, ValueError):
            sdk = None  # Preserve existing CLI availability diagnostics.
        if sdk:
            from .dsh_profile import resolve_profile, profile_key
            try:
                key = profile_key(resolve_profile(config["dsh_profile"]))
            except ValueError:
                return query + "AND 0 ", args
            # Filter before bounded scans: a different profile's oldest cases
            # must neither leak evidence nor starve this profile's ready case.
            query += ("AND (p.conversation NOT LIKE 'dsh:%' OR EXISTS(SELECT 1 FROM events dp "
                      "WHERE dp.project=p.project AND dp.conversation=p.conversation AND dp.type='DshProfileBound' "
                      "AND json_extract(dp.payload,'$.profile_key')=?)) ")
            args += (key,)
    if policy["model_mode"] == "current" and phase_scope(runtime, project) is None:
        provider = runtime.host if policy["provider"] == "host" else policy["provider"]
        if provider != runtime.host:
            return query + "AND 0 ", args
        query += ("AND EXISTS(SELECT 1 FROM admin_model_observations m WHERE m.project=p.project "
                  "AND m.conversation=p.conversation AND m.host_scope=? AND m.session_epoch=? AND m.active=1) ")
        args += (runtime._session_scope(), runtime.session_epoch())
    return query, args


def ready_executor(runtime, project, query, args, *, advance=False):
    from .admin_providers import executor_availability
    query, args = executor_query(runtime, project, query, args)
    signature = routing_signature(runtime, project)
    order, order_args = executor_review_order(runtime, project, signature)
    seen = []
    for _ in range(3 if advance else 1):
        suffix = "AND p.id NOT IN (" + ",".join("?" for _ in seen) + ") " if seen else ""
        rows = runtime.db.execute(query + suffix + order + "LIMIT 40",
                                  (project, *args, *seen, *order_args)).fetchall()
        for item in rows:
            seen.append(item["id"])
            try:
                plan = runtime.resolve_admin_executor(project, phase_conversation(runtime, project, item["id"], item["conversation"]))
                available = executor_availability(plan)["cli_available"]
            except ValueError:
                available, plan = False, None
            if available and not executor_backoff(runtime, project, plan):
                return True
            if advance:
                remember_executor_check(runtime, project, item, signature)
        if len(rows) < 40:
            break
    return False


def unrouted_reason(runtime, project, query, args, fallback):
    """Expose a precise origin diagnostic without reserving or revealing case evidence."""
    row = runtime.db.execute(query + REVIEW_ORDER + "LIMIT 1", (project, *args)).fetchone()
    if row:
        try:
            runtime.resolve_admin_executor(project, phase_conversation(runtime, project, row["id"], row["conversation"]))
        except ValueError as exc:
            reason = str(exc).split(":", 1)[0]
            if reason.startswith("dsh_profile_"):
                return reason
    return fallback


def claim_dispatch(runtime, project, *, expected_epoch=None, _phase_guard=None):
    """Hook先预留后台进程位置；实际模型额度仍由claim_call统一预留。"""
    from .admin_lifecycle import ACTIVE_STATES, recover_calls
    from .admin_usage import admin_admission_status, budget_settings
    cfg = runtime.admin_effective_config(project)
    if not cfg["enabled"] or cfg["max_calls_per_day"] <= 0 and phase_scope(runtime, project) is None:
        return None
    with runtime.transaction():
        runtime.assert_host_epoch(expected_epoch)
        if _phase_guard:
            _phase_guard()
        cfg = runtime.admin_effective_config(project)
        if not cfg["enabled"] or cfg["max_calls_per_day"] <= 0 and phase_scope(runtime, project) is None:
            return None
        recover_calls(runtime)
        now = time.time()
        recover_dispatch(runtime, now)
        runtime.db.execute("DELETE FROM admin_dispatch WHERE lease_until<=? OR host_epoch!=?",
                           (now, runtime.host_epoch()))
        if runtime.db.execute("SELECT 1 FROM admin_dispatch WHERE project=?", (project,)).fetchone():
            return None
        if executor_backoff(runtime, project, background_plan(runtime)):
            return None
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        if runtime.db.execute("SELECT 1 FROM admin_calls WHERE project=? AND state IN (" + placeholders + ")",
                              (project, *ACTIVE_STATES)).fetchone():
            return None
        budget = admin_admission_status(runtime, project, now=now,
            required_tokens=budget_settings(runtime)["token_reservation_per_call"])
        if budget["admission_status"] != "available":
            return None
        # 已在审核的派发与调用占同一槽，不能重复计算并发占用。
        dispatched = runtime.db.execute("SELECT COUNT(*) FROM admin_dispatch d WHERE NOT EXISTS("
                                        "SELECT 1 FROM admin_calls c WHERE c.project=d.project "
                                        "AND c.state IN ('reserved','launch_intent','started'))").fetchone()[0]
        if dispatched + budget["global_active_calls"] >= budget["max_concurrent_calls"]:
            return None
        # A bounded local pass can reach later ready sources or notice new
        # evidence without a model job already existing. Exhausted passes sleep.
        from .phase_review import prepare as prepare_phases
        prepare_phases(runtime, project)
        if admin_admission_status(runtime, project)["admission_status"] != "available":
            return None
        signature, preflight_signature = scan_signatures(runtime, project)
        query, args = eligible_query(signature, preflight_signature=preflight_signature,
                                     summary_enabled=runtime.work_summary_enabled(project),
                                     summary_host_scope=runtime._session_scope(), phase_scope=phase_scope(runtime, project))
        if not ready_executor(runtime, project, query, args, advance=True):
            return None
        token = str(uuid.uuid4())
        runtime.db.execute("INSERT INTO admin_dispatch(project,token,host_epoch,lease_until,created,host_scope) VALUES(?,?,?,?,?,?)",
                           (project, token, runtime.host_epoch(), now + 30, now, runtime._session_scope()))
        record_dispatch_diagnostic(runtime, project, "worker_reserved")
        return token


def release_dispatch(runtime, project, token):
    if token:
        runtime.db.execute("DELETE FROM admin_dispatch WHERE project=? AND token=?", (project, token))


def background_plan(runtime):
    import sys
    from pathlib import Path
    return {"provider": "background_python", "command": [sys.executable],
            "model": str(Path(__file__).resolve()), "session_epoch": runtime.session_epoch()}


def recover_dispatch(runtime, now=None):
    """An unacknowledged expired Python reservation is never active model work."""
    now = time.time() if now is None else now
    rows = runtime.db.execute("SELECT project,host_scope FROM admin_dispatch WHERE phase='reserved' "
                             "AND host_epoch=? AND lease_until<=?", (runtime.host_epoch(), now)).fetchall()
    for row in rows:
        if row["host_scope"] == runtime._session_scope():
            record_executor_failure(runtime, row["project"], background_plan(runtime), "worker_ack_expired")
        elif row["host_scope"]:
            # The recoverer may be another host sharing this SQLite database.
            # Without the original session's frozen executor signature, record
            # its failure in the owning host; never penalize the recoverer.
            record_dispatch_diagnostic(runtime, row["project"], "worker_ack_expired", host_scope=row["host_scope"])
    runtime.db.execute("DELETE FROM admin_dispatch WHERE lease_until<=? OR host_epoch!=?", (now, runtime.host_epoch()))


def claim_dispatch_worker(runtime, project, token, seconds):
    """Only the worker can acknowledge its reservation; duplicate workers fail closed."""
    import os
    with runtime.transaction():
        now = time.time()
        cursor = runtime.db.execute("UPDATE admin_dispatch SET phase='worker_claimed',claimed_at=?,worker_pid=?,lease_until=? "
            "WHERE project=? AND token=? AND host_epoch=? AND lease_until>? AND phase='reserved' AND host_scope IN ('',?)",
            (now, os.getpid(), now + seconds + 30, project, token, runtime.host_epoch(), now, runtime._session_scope()))
        if cursor.rowcount != 1:
            return False
        clear_executor_failure(runtime, project, background_plan(runtime))
        record_dispatch_diagnostic(runtime, project, "worker_claimed")
        return True


def record_dispatch_diagnostic(runtime, project, reason, details=None, *, host_scope=None):
    from .runtime import encode
    # details must be fixed fields; never persist CLI stderr/exception messages.
    numeric = {"errno", "winerror", "returncode", "failures", "next_try"}
    labels = {"error_type", "phase", "reason_code"}
    flags = {"input_submission_attempted", "terminal_observed"}
    details = {key: value for key, value in (details or {}).items()
               if (key in numeric and type(value) in {int, float})
               or (key in labels and isinstance(value, str) and len(value) <= 80 and value.isidentifier())
               or (key in flags and type(value) is bool)}
    runtime.db.execute("INSERT INTO admin_dispatch_diagnostics VALUES(?,?,?,?,?) ON CONFLICT(project,host_scope) "
                       "DO UPDATE SET reason=excluded.reason,details=excluded.details,updated=excluded.updated",
                       (project, host_scope or runtime._session_scope(), reason, encode(details), time.time()))


def executor_signature(plan):
    from .runtime import digest
    from .admin_providers import resolve_command, dsh_sdk_launcher
    sdk = None
    try:
        sdk = dsh_sdk_launcher(plan["command"]) if plan.get("provider") == "dsh" else None
        command = sdk["args"] if sdk else resolve_command(plan["command"])["args"]
        from pathlib import Path
        files = [(item, Path(item).stat().st_mtime_ns, Path(item).stat().st_size)
                 for item in command if not item.startswith("-") and Path(item).is_file()]
    except (OSError, ValueError, KeyError, TypeError):
        command, files = plan.get("command"), []
    identity = {"command": command, "files": files, "provider": plan.get("provider"),
                "model": plan.get("model"), "session_epoch": plan.get("session_epoch")}
    if plan.get("provider") == "dsh" and isinstance(plan.get("dsh_profile"), dict):
        profile = plan["dsh_profile"]
        identity["profile"] = {key: profile.get(key) for key in ("source", "profile_directory", "sha256")}
        from .dsh_profile import configuration_identity
        identity["configuration"] = configuration_identity(profile)
    if sdk:
        # Updating the trusted adapter must invalidate its old configuration
        # failure, even when the CLI and reviewer paths stay the same.
        from pathlib import Path
        anchor = sdk.get("anchor") if isinstance(sdk, dict) else None
        if not isinstance(anchor, (str, Path)) or not str(anchor).strip():
            # A diagnostic signature is not an availability decision. Partial
            # SDK metadata must stay signable while the normal launcher/profile
            # guards decide whether the executor can actually run.
            identity["sdk_versions"] = "unavailable"
        else:
            package = Path(anchor).parent.parent
            names = ("@deepseek-ai/dsh-llm-pi-ai", "@earendil-works/pi-ai", "@deepseek-ai/dsh-settings-file")
            packages = [package / "package.json", *(package / "node_modules" / name / "package.json" for name in names)]
            try:
                identity["sdk_versions"] = [(str(path), path.stat().st_mtime_ns, path.stat().st_size)
                                            for path in packages if path.is_file()]
            except OSError:
                identity["sdk_versions"] = "unavailable"
    return digest(identity)


def executor_backoff(runtime, project, plan):
    if not runtime.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_executor_failures'").fetchone():
        return None
    row = runtime.db.execute("SELECT failures,next_try,reason FROM admin_executor_failures "
        "WHERE project=? AND host_scope=? AND signature=? AND (next_try>? OR reason='configuration_change_required')",
        (project, runtime._session_scope(), executor_signature(plan), time.time())).fetchone()
    if not row:
        return None
    result = dict(row)
    if result["reason"] == "configuration_change_required":
        result["next_try"] = None
    return result


def executor_wait_reason(backoff):
    return ("configuration_change_required" if backoff["reason"] == "configuration_change_required" else
            "executor_circuit_open" if backoff["failures"] >= 3 else "executor_backoff")


def record_executor_failure(runtime, project, plan, reason, details=None):
    key, now = executor_signature(plan), time.time()
    row = runtime.db.execute("SELECT failures FROM admin_executor_failures WHERE project=? AND host_scope=? AND signature=?",
                            (project, runtime._session_scope(), key)).fetchone()
    failures = min(30, (row[0] if row else 0) + 1)
    delay = 30 if failures == 1 else 120 if failures == 2 else 900
    next_try = 0 if reason == "configuration_change_required" else now + delay
    runtime.db.execute("INSERT INTO admin_executor_failures VALUES(?,?,?,?,?,?,?) ON CONFLICT(project,host_scope,signature) "
                       "DO UPDATE SET failures=excluded.failures,next_try=excluded.next_try,reason=excluded.reason,updated=excluded.updated",
                       (project, runtime._session_scope(), key, failures, next_try, reason, now))
    record_dispatch_diagnostic(runtime, project, reason, dict(details or {}, failures=failures, next_try=next_try))


def clear_executor_failure(runtime, project, plan):
    runtime.db.execute("DELETE FROM admin_executor_failures WHERE project=? AND host_scope=? AND signature=?",
                       (project, runtime._session_scope(), executor_signature(plan)))


def clear_recovered_executor_failure(runtime, project, plan, call_id):
    """Clear only the recovered decision failure, never a newer provider failure."""
    call = runtime.db.execute("SELECT finished FROM admin_calls WHERE project=? AND id=?", (project, call_id)).fetchone()
    if call and call[0] is not None:
        runtime.db.execute("DELETE FROM admin_executor_failures WHERE project=? AND host_scope=? AND signature=? "
            "AND reason='executor_decision_uncommitted' AND updated<=?",
            (project, runtime._session_scope(), executor_signature(plan), call[0]))


def dispatch_diagnostic(runtime, project, *, check_queue=True):
    import json
    result = _dispatch_diagnostic(runtime, project, check_queue=check_queue)
    exists = runtime.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_dispatch_diagnostics'").fetchone()
    last = runtime.db.execute("SELECT reason,details,updated FROM admin_dispatch_diagnostics WHERE project=? AND host_scope=?",
                              (project, runtime._session_scope())).fetchone() if exists else None
    if last:
        result["last_observation"] = {"reason": last["reason"], "details": json.loads(last["details"]), "updated": last["updated"]}
    return result


def _dispatch_diagnostic(runtime, project, *, check_queue=True):
    """Read-only admission/worker/model observation, shared by all hosts and UI."""
    from .admin_usage import admin_admission_status
    from .admin_providers import executor_availability
    now = time.time()
    detail = {"phase": "idle", "reason": "no_increment", "next_due": None}
    active = runtime.db.execute("SELECT state,lease_until FROM admin_calls WHERE project=? "
        "AND state IN ('reserved','launch_intent','started') AND lease_until>? ORDER BY created LIMIT 1", (project, now)).fetchone()
    if active:
        phase = {"reserved": "model_reserved", "launch_intent": "model_launching", "started": "model_started"}[active["state"]]
        return dict(detail, phase=phase, reason=phase, next_due=active["lease_until"])
    if not runtime.admin_effective_config(project)["enabled"]:
        return dict(detail, reason="disabled")
    dispatch_columns = {row[1] for row in runtime.db.execute("PRAGMA table_info(admin_dispatch)")}
    phase_column = "phase" if "phase" in dispatch_columns else "'reserved' AS phase"
    dispatch = runtime.db.execute("SELECT " + phase_column + ",lease_until FROM admin_dispatch WHERE project=? AND host_epoch=? AND lease_until>?",
                                  (project, runtime.host_epoch(), now)).fetchone()
    if dispatch:
        phase = "worker_claimed" if dispatch["phase"] == "worker_claimed" else "worker_reserved"
        return dict(detail, phase=phase, reason=phase, next_due=dispatch["lease_until"])
    if not check_queue:
        return dict(detail, reason="queue_not_checked", queue_checked=False)
    signature, preflight_signature = scan_signatures(runtime, project)
    query, args = eligible_query(signature,
        preflight_signature=preflight_signature,
        summary_enabled=runtime.work_summary_enabled(project),
        summary_host_scope=runtime._session_scope(), phase_scope=phase_scope(runtime, project))
    routed, routed_args = executor_query(runtime, project, query, args)
    order, order_args = executor_review_order(runtime, project, routing_signature(runtime, project))
    rows = runtime.db.execute(routed + order + "LIMIT 40", (project, *routed_args, *order_args)).fetchall()
    if not rows:
        if runtime.db.execute(query + "LIMIT 1", (project, *args)).fetchone():
            return dict(detail, reason=unrouted_reason(runtime, project, query, args, "model_missing"))
        if runtime.db.execute("SELECT 1 FROM operations o JOIN problems p ON p.project=o.project AND p.conversation=o.conversation "
            "AND p.task_id IS o.task_id AND p.task_revision IS o.task_revision WHERE p.project=? AND p.status='pending' "
            "AND o.state IN ('running','started') LIMIT 1", (project,)).fetchone():
            return dict(detail, reason="awaiting_tool_completion")
        from .admin_lifecycle import unresolved_failure_sql
        if runtime.db.execute("SELECT 1 FROM admin_calls c WHERE c.project=? AND "
            + unresolved_failure_sql("c") + " LIMIT 1", (project,)).fetchone():
            return dict(detail, reason="unknown_call_requires_review")
        if runtime.db.execute("SELECT 1 FROM admin_preflights WHERE project=? LIMIT 1", (project,)).fetchone():
            return dict(detail, reason="awaiting_evidence")
        return detail
    budget = admin_admission_status(runtime, project)
    if budget["admission_status"] != "available":
        return dict(detail, reason=budget["admission_status"], next_due=budget.get("next_reset_at"),
                    blocking_scope=budget.get("blocking_scope"))
    worker_backoff = executor_backoff(runtime, project, background_plan(runtime))
    if worker_backoff:
        return dict(detail, reason="worker_launch_backoff", next_due=worker_backoff["next_try"],
                    failure_reason=worker_backoff["reason"])
    last = dict(detail, reason="model_missing")
    for row in rows:
        try:
            plan = runtime.resolve_admin_executor(project, phase_conversation(runtime, project, row["id"], row["conversation"]))
        except ValueError:
            continue
        availability = executor_availability(plan)
        if not availability["cli_available"]:
            last = dict(detail, reason=availability["reason"])
            continue
        backoff = executor_backoff(runtime, project, plan)
        if backoff:
            last = dict(detail, reason=executor_wait_reason(backoff),
                        next_due=backoff["next_try"], failure_reason=backoff["reason"])
            continue
        return dict(detail, reason="ready")
    return last
