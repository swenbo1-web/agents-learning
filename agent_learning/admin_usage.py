"""管理员的上报用量与准入账；未知消费保留预留，不估算价格。"""
from __future__ import annotations

import contextlib
import json
import time

from .runtime import ContractError, digest, encode

TOKEN_FIELDS = {"input_tokens", "output_tokens", "cached_input_tokens", "total_tokens"}
RESERVATION_ESTIMATE_VERSION = 2
ADMIN_BUDGET_DEFAULTS = {
    "max_calls_per_day": 100, "max_calls_per_batch": 5, "max_cases_per_batch": 40,
    "max_batch_seconds": 180, "max_run_seconds": 600, "timeout_seconds": 120,
    "max_concurrent_calls": 1, "max_reported_tokens_per_day": None,
    "token_reservation_per_call": 4000,
}
PROJECT_BUDGET_FIELDS = frozenset({
    "max_calls_per_day", "max_calls_per_batch", "max_cases_per_batch", "max_batch_seconds",
    "max_run_seconds", "timeout_seconds", "max_reported_tokens_per_day",
})
_BUDGET_RANGES = {
    "max_calls_per_day": (0, 100000), "max_calls_per_batch": (1, 20),
    "max_cases_per_batch": (1, 100), "max_batch_seconds": (1, 600),
    "max_run_seconds": (1, 3600), "timeout_seconds": (1, 600),
    "max_concurrent_calls": (1, 1000000000), "token_reservation_per_call": (1, 1000000000),
}


def _validate_budget_values(values, *, complete=False):
    if not isinstance(values, dict) or set(values) - set(ADMIN_BUDGET_DEFAULTS):
        raise ContractError("unsupported administrator budget fields")
    if complete and set(values) != set(ADMIN_BUDGET_DEFAULTS):
        raise ContractError("administrator budget policy is incomplete")
    for key, value in values.items():
        if key == "max_reported_tokens_per_day":
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ContractError("invalid admin.max_reported_tokens_per_day")
        else:
            low, high = _BUDGET_RANGES[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ContractError(f"invalid admin.{key}")
    return values


def admin_budget_policy(runtime):
    """Read shared persistent limits on every admission; never initializes storage."""
    values = {key: runtime.config["admin"].get(key, value) for key, value in ADMIN_BUDGET_DEFAULTS.items()}
    version, updated = 0, None
    db = getattr(runtime, "db", None)
    if db is not None and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_runtime_settings'").fetchone():
        row = db.execute("SELECT version,body,updated FROM admin_runtime_settings WHERE id=1").fetchone()
        if row:
            try:
                stored = json.loads(row["body"])
            except (TypeError, ValueError) as exc:
                raise ContractError("administrator budget policy could not be read") from exc
            _validate_budget_values(stored, complete=True)
            values.update(stored)
            version, updated = row["version"], row["updated"]
    _validate_budget_values(values, complete=True)
    return {"version": version, "values": values, "scope": "ALP_HOME", "updated": updated}


def admin_budget_config(runtime):
    """Global configuration copy with the authoritative ALP_HOME budget applied."""
    result = dict(runtime.config["admin"])
    result.update(admin_budget_policy(runtime)["values"])
    return result


@contextlib.contextmanager
def _read_snapshot(runtime):
    """Keep the two budget scopes in one read snapshot, without initializing state."""
    if runtime.db.in_transaction:
        yield
        return
    runtime.db.execute("BEGIN")
    try:
        yield
        runtime.db.execute("COMMIT")
    except BaseException:
        runtime.db.execute("ROLLBACK")
        raise


def _require_project(runtime, project):
    if (not isinstance(project, str) or not project.strip()
            or not runtime.db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone()):
        raise ContractError("unknown administrator budget project")


def project_budget_policy(runtime, project):
    """Project defaults inherit configuration, never another project's shared override."""
    _require_project(runtime, project)
    values = {key: runtime.config["admin"].get(key, ADMIN_BUDGET_DEFAULTS[key])
              for key in PROJECT_BUDGET_FIELDS}
    _validate_budget_values(values)
    version, updated, configured = 0, None, False
    if runtime.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_project_budget_policies'").fetchone():
        row = runtime.db.execute("SELECT version,body,updated FROM admin_project_budget_policies WHERE project=?",
                                 (project,)).fetchone()
        if row:
            try:
                stored = json.loads(row["body"])
            except (TypeError, ValueError) as exc:
                raise ContractError("administrator project budget policy could not be read") from exc
            if not isinstance(stored, dict) or set(stored) != PROJECT_BUDGET_FIELDS:
                raise ContractError("administrator project budget policy is incomplete")
            _validate_budget_values(stored)
            values.update(stored)
            version, updated, configured = row["version"], row["updated"], True
    return {"scope": "project", "project_id": project, "version": version,
            "configured": configured, "values": values, "updated": updated}


def configure_project_budget(runtime, project, values, *, expected_version, expected_epoch=None):
    """CAS-update this project's limits across hosts; leave the shared guard and ledger intact."""
    if not isinstance(values, dict) or not values or set(values) - PROJECT_BUDGET_FIELDS:
        raise ContractError("unsupported administrator project budget fields")
    _validate_budget_values(values)
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
        raise ContractError("administrator expected project budget version must be nonnegative")
    with contextlib.nullcontext() if runtime.db.in_transaction else runtime.transaction():
        runtime.assert_session_epoch(expected_epoch)
        current = project_budget_policy(runtime, project)
        if current["version"] != expected_version:
            raise ContractError("administrator project budget changed; reread before configuring")
        merged = dict(current["values"], **values)
        changed = merged != current["values"] or not current["configured"]
        if changed:
            runtime.db.execute(
                "INSERT INTO admin_project_budget_policies(project,version,body,updated) VALUES(?,?,?,?) "
                "ON CONFLICT(project) DO UPDATE SET version=excluded.version,body=excluded.body,updated=excluded.updated",
                (project, current["version"] + 1, encode(merged), time.time()))
        return dict(project_budget_policy(runtime, project), changed=changed)


def configure_admin_budget(runtime, values, *, expected_version, expected_epoch=None):
    """Explicit CAS update shared by every project/host; leaves usage and routing intact."""
    if not isinstance(values, dict) or not values:
        raise ContractError("administrator budget update must contain values")
    _validate_budget_values(values)
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
        raise ContractError("administrator expected budget version must be nonnegative")
    with contextlib.nullcontext() if runtime.db.in_transaction else runtime.transaction():
        runtime.assert_session_epoch(expected_epoch)
        current = admin_budget_policy(runtime)
        if current["version"] != expected_version:
            raise ContractError("administrator budget changed; reread before configuring")
        merged = dict(current["values"], **values)
        changed = merged != current["values"] or current["version"] == 0
        if changed:
            runtime.db.execute(
                "INSERT INTO admin_runtime_settings(id,version,body,updated) VALUES(1,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET version=excluded.version,body=excluded.body,updated=excluded.updated",
                (current["version"] + 1, encode(merged), time.time()))
        return dict(admin_budget_policy(runtime), changed=changed)


def budget_settings(runtime):
    cfg = admin_budget_config(runtime)
    values = {"max_concurrent_calls": cfg.get("max_concurrent_calls", 1),
              "token_reservation_per_call": cfg.get("token_reservation_per_call", 4000),
              "max_reported_tokens_per_day": cfg.get("max_reported_tokens_per_day")}
    for key in ("max_concurrent_calls", "token_reservation_per_call"):
        value = values[key]
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 1000000000:
            raise ContractError(f"invalid admin.{key}")
    maximum = values["max_reported_tokens_per_day"]
    if maximum is not None and (not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 0):
        raise ContractError("invalid admin.max_reported_tokens_per_day")
    calls = cfg["max_calls_per_day"]
    if not isinstance(calls, int) or isinstance(calls, bool) or not 0 <= calls <= 100000:
        raise ContractError("invalid administrator daily budget")
    return values


def normalize_usage(usage):
    """兼容旧预览记录；完整报告才允许实际结算，空补量不意味着零。"""
    if not isinstance(usage, dict):
        raise ContractError("invalid administrator usage observation")
    result = dict(usage)
    records = usage.get("token_usage", [])
    if not isinstance(records, list):
        raise ContractError("invalid administrator token usage")
    totals = usage.get("token_totals")
    if totals is None:
        totals = {}
        complete = bool(records)
        for record in records:
            if not isinstance(record, dict):
                complete = False
                continue
            numbers = {k: v for k, v in record.items() if k in TOKEN_FIELDS
                       and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
            complete &= {"input_tokens", "output_tokens"} <= numbers.keys()
            for key, value in numbers.items():
                totals[key] = totals.get(key, 0) + value
        completeness = "complete" if complete and not usage.get("log_tail_truncated") else "partial" if totals else "unknown"
    else:
        if not isinstance(totals, dict) or set(totals) - TOKEN_FIELDS or any(
                not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in totals.values()):
            raise ContractError("invalid administrator token totals")
        totals = dict(totals)
        completeness = usage.get("completeness", "partial" if totals else "unknown")
        if completeness not in {"complete", "partial", "unknown"}:
            raise ContractError("invalid administrator usage completeness")
        if completeness == "complete" and not {"input_tokens", "output_tokens"} <= totals.keys():
            completeness = "partial" if totals else "unknown"
    if ("total_tokens" in totals and {"input_tokens", "output_tokens"} <= totals.keys()
            and totals["total_tokens"] != totals["input_tokens"] + totals["output_tokens"]):
        completeness = "partial"
    if usage.get("unreported_turns", 0) or usage.get("call_outcome_unknown"):
        completeness = "partial" if totals else "unknown"
    result.update(token_totals=totals, completeness=completeness)
    return result


def install_budget(runtime):
    """迁移入口在写事务内安装；旧调用缺少真实用量时保守保留预留。"""
    if not runtime.db.in_transaction:
        raise ContractError("administrator budget migration requires a write transaction")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS admin_runtime_settings("
                       "id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL CHECK(version>=1),"
                       "body TEXT NOT NULL,updated REAL NOT NULL)")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS admin_project_budget_policies("
                       "project TEXT PRIMARY KEY,version INTEGER NOT NULL CHECK(version>=1),"
                       "body TEXT NOT NULL,updated REAL NOT NULL,FOREIGN KEY(project) REFERENCES projects(id))")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS admin_call_budgets("
                       "call_id TEXT PRIMARY KEY,reserved_tokens INTEGER NOT NULL,reported_tokens INTEGER NOT NULL DEFAULT 0,"
                       "completeness TEXT NOT NULL DEFAULT 'unknown',conflict INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL)")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS admin_usage_observations("
                       "call_id TEXT NOT NULL,observation_id TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL,"
                       "PRIMARY KEY(call_id,observation_id))")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS admin_calls_daily_budget ON admin_calls(day,state)")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS admin_calls_project_daily_budget ON admin_calls(project,day,state)")
    reservation = budget_settings(runtime)["token_reservation_per_call"]
    for row in runtime.db.execute("SELECT id,usage FROM admin_calls WHERE state!='deduplicated' AND "
                                  "NOT EXISTS(SELECT 1 FROM admin_call_budgets b WHERE b.call_id=admin_calls.id)").fetchall():
        runtime.db.execute("INSERT INTO admin_call_budgets(call_id,reserved_tokens,updated) VALUES(?,?,?)",
                           (row["id"], reservation, time.time()))
        if row["usage"]:
            record_usage(runtime, row["id"], json.loads(row["usage"]))


def reserve_usage(runtime, call_id, *, reserved_tokens=None):
    reservation = budget_settings(runtime)["token_reservation_per_call"] if reserved_tokens is None else reserved_tokens
    if type(reservation) is not int or not 1 <= reservation <= 1000000000:
        raise ContractError("invalid administrator token reservation")
    runtime.db.execute("INSERT INTO admin_call_budgets(call_id,reserved_tokens,updated) VALUES(?,?,?)",
                       (call_id, reservation, time.time()))


def estimate_reservation(runtime, project, instruction, *, phase=False):
    """Local conservative estimate, never a guarantee of provider billing limits."""
    if not phase:
        return {"admitted": True, "reserved_tokens": budget_settings(runtime)["token_reservation_per_call"]}
    from .phase_review import policy, response_schema
    cfg = policy(runtime, project)
    # Host system prompts are not fully visible. Reserve a floor plus the visible UTF-8 payload.
    instruction_bytes = len(instruction.encode("utf-8"))
    schema_bytes = len(encode(response_schema()).encode("utf-8"))
    estimated_input = 12000 + (instruction_bytes + schema_bytes + 1) // 2
    amount = estimated_input + cfg["max_output_tokens"]
    admitted = estimated_input <= cfg["max_input_tokens"]
    return {"admitted": admitted, "reserved_tokens": amount, "estimated_input_tokens": estimated_input,
            "estimated_output_tokens": cfg["max_output_tokens"], "estimate_only": True,
            "estimate_version": RESERVATION_ESTIMATE_VERSION,
            "instruction_chars": len(instruction), "instruction_bytes": instruction_bytes,
            "schema_bytes": schema_bytes, "host_reserve_tokens": 12000,
            "reason": "ready" if admitted else "phase_input_token_budget_exceeded"}


def record_usage(runtime, call_id, usage):
    """追加幂等快照，保留冲突；日志快照是累计量，补量不重复相加。"""
    result = normalize_usage(usage)
    totals = result["token_totals"]
    observation_id = digest({"totals": totals, "completeness": result["completeness"],
                             "source": result.get("observation_id")})
    prior = runtime.db.execute("SELECT * FROM admin_call_budgets WHERE call_id=?", (call_id,)).fetchone()
    if prior is None:
        reserve_usage(runtime, call_id)
        prior = runtime.db.execute("SELECT * FROM admin_call_budgets WHERE call_id=?", (call_id,)).fetchone()
    inserted = runtime.db.execute("INSERT OR IGNORE INTO admin_usage_observations VALUES(?,?,?,?)",
                                  (call_id, observation_id, encode(result), time.time())).rowcount
    existing = runtime.db.execute("SELECT usage FROM admin_calls WHERE id=?", (call_id,)).fetchone()
    previous = normalize_usage(json.loads(existing[0])) if existing and existing[0] else None
    has_numbers = bool(totals)
    # 没数字的观察保留在账中，但不能擦掉先前数字或把未知变为零。
    if not has_numbers and previous and previous["token_totals"]:
        return previous
    reported = max(totals.get("input_tokens", 0) + totals.get("output_tokens", 0), totals.get("total_tokens", 0))
    conflict = bool(prior["conflict"])
    if previous and previous["token_totals"] and has_numbers:
        old = previous["token_totals"]
        if inserted:
            conflict |= any(key in totals and totals[key] < value for key, value in old.items())
            conflict |= previous["completeness"] == "complete" and old != totals
        # 较小或不完整补量不能使已有报告降为免费；冲突仍显示保守最大值。
        for key, value in old.items():
            totals[key] = max(value, totals.get(key, 0))
        reported = max(reported, prior["reported_tokens"],
                       totals.get("input_tokens", 0) + totals.get("output_tokens", 0), totals.get("total_tokens", 0))
    completeness = "partial" if conflict else result["completeness"]
    result.update(token_totals=totals, completeness=completeness, conflict=conflict)
    runtime.db.execute("UPDATE admin_call_budgets SET reported_tokens=?,completeness=?,conflict=?,updated=? WHERE call_id=?",
                       (max(reported, prior["reported_tokens"]), completeness, int(conflict), time.time(), call_id))
    return result


def _usage_status(runtime, policy, settings, *, now=None, project=None, required_tokens=None):
    """Derive counters from the same call ledger; no duplicate debit or stored totals."""
    cfg = policy["values"]
    current = time.time() if now is None else now
    day = time.strftime("%Y-%m-%d", time.gmtime(current))
    next_reset = (int(current) // 86400 + 1) * 86400
    receipts_exist = runtime.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_receipts'").fetchone()
    accepted = ("EXISTS(SELECT 1 FROM review_receipts r WHERE r.project=c.project AND r.problem=c.problem "
                "AND r.revision=c.revision AND r.call_id=c.id AND r.decision=c.state AND r.duplicate_of IS NULL)"
                if receipts_exist else "0")
    responses_exist = runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name='admin_response_records'").fetchone()
    recovered = ("EXISTS(SELECT 1 FROM admin_response_records s JOIN review_receipts r "
        "ON r.project=s.project AND r.call_id=s.call_id WHERE s.project=c.project AND s.call_id=c.id "
        "AND s.state='accepted' AND c.state IN ('failed','orphaned_unknown','account_changed_unknown') "
        "AND r.problem=c.problem AND r.revision=c.revision "
        "AND r.decision IN ('candidate','needs_evidence','skip','governance') AND r.duplicate_of IS NULL)"
        if receipts_exist and responses_exist else "0")
    rows = runtime.db.execute("SELECT c.state,c.started," + accepted + " AS accepted_review,"
                              + recovered + " AS locally_recovered_review,b.* "
                              "FROM admin_calls c LEFT JOIN admin_call_budgets b ON b.call_id=c.id "
                              "WHERE c.day=?" + (" AND c.project=?" if project is not None else ""),
                              (day, project) if project is not None else (day,)).fetchall()
    active = runtime.db.execute("SELECT COUNT(*) FROM admin_calls WHERE state IN ('reserved','launch_intent','started')").fetchone()[0]
    project_active = (runtime.db.execute("SELECT COUNT(*) FROM admin_calls WHERE project=? "
                                       "AND state IN ('reserved','launch_intent','started')", (project,)).fetchone()[0]
                      if project is not None else None)
    usage_states = {"complete": 0, "partial": 0, "unknown": 0, "conflict": 0}
    reported, reservations, accounted = 0, 0, 0
    counts = {key: 0 for key in ("counted_attempts", "started_attempts", "completed_reviews", "failed_attempts",
                                "locally_recovered_reviews", "effective_completed_reviews",
                                "result_unknown_attempts", "usage_unknown_attempts", "active_attempts",
                                "not_started_attempts", "deduplicated_attempts")}
    for row in rows:
        if row["state"] in {"not_started", "deduplicated"}:
            counts[row["state"] + "_attempts"] += 1
            continue
        counts["counted_attempts"] += 1
        counts["started_attempts"] += int(row["started"] is not None)
        counts["completed_reviews"] += int(row["state"] in {"candidate", "needs_evidence", "skip", "governance"}
                                           and bool(row["accepted_review"]))
        counts["locally_recovered_reviews"] += int(bool(row["locally_recovered_review"]))
        counts["failed_attempts"] += int(row["state"] == "failed")
        counts["result_unknown_attempts"] += int(row["state"] in {"timeout_unknown", "account_changed_unknown", "orphaned_unknown"})
        counts["active_attempts"] += int(row["state"] in {"reserved", "launch_intent", "started"})
        completeness = row["completeness"] or "unknown"
        usage_states[completeness] += 1
        usage_states["conflict"] += int(bool(row["conflict"]))
        counts["usage_unknown_attempts"] += int(completeness == "unknown")
        known = row["reported_tokens"] or 0
        reserved = row["reserved_tokens"] if row["reserved_tokens"] is not None else settings["token_reservation_per_call"]
        pending = row["state"] in {"reserved", "launch_intent", "started", "timeout_unknown",
                                   "account_changed_unknown", "orphaned_unknown"} or completeness != "complete"
        reported += known
        reservations += reserved if pending else 0
        accounted += max(reserved, known) if pending else known
    counts["effective_completed_reviews"] = counts["completed_reviews"] + counts["locally_recovered_reviews"]
    maximum = cfg["max_reported_tokens_per_day"]
    phase_mode = False
    if project is not None:
        from .phase_review import policy as phase_policy
        phase_cfg = phase_policy(runtime, project)
        phase_mode = phase_cfg["mode"] == "phase"
        if phase_mode:
            maximum = min(maximum, phase_cfg["daily_tokens"]) if maximum is not None else phase_cfg["daily_tokens"]
    remaining = None if maximum is None else max(0, maximum - accounted)
    remaining_calls = max(0, cfg["max_calls_per_day"] - counts["counted_attempts"])
    required = settings["token_reservation_per_call"] if required_tokens is None else required_tokens
    admission = ("budget_exhausted" if remaining_calls == 0 and not phase_mode else
                 "concurrency_limited" if project is None and active >= settings["max_concurrent_calls"] else
                 "token_budget_exhausted" if remaining is not None and remaining < required else "available")
    scope = "project" if project is not None else "ALP_HOME"
    return {"utc_day": day, "used_calls": counts["counted_attempts"], "remaining_calls": remaining_calls,
            "next_reset_at": next_reset, "global_active_calls": active, **settings, **counts,
            "max_reported_tokens_per_day": maximum,
            "reported_tokens": reported, "token_reservations": reservations, "accounted_tokens": accounted,
            "remaining_reported_tokens": remaining, "usage_states": usage_states, "admission_status": admission,
            **({"required_token_reservation": required} if required_tokens is not None else {}),
            "scope": scope, "budget_scope": scope, "budget_policy": policy,
            **({"project_id": project, "project_active_calls": project_active} if project is not None else {}),
            "max_calls_per_day": cfg["max_calls_per_day"], "budget_mode": "tokens" if phase_mode else "legacy",
            "call_limit_enforced": not phase_mode,
            "limitation": "Counted attempts are not confirmed paid model requests. Reported usage admission does not guarantee an executor token or fee cap."}


def admin_budget_status(runtime, *, now=None, required_tokens=None):
    """Read the unchanged ALP_HOME budget scope; unknown usage remains reserved."""
    with _read_snapshot(runtime):
        policy = admin_budget_policy(runtime)
        settings = {key: policy["values"][key] for key in
                    ("max_concurrent_calls", "token_reservation_per_call", "max_reported_tokens_per_day")}
        return _usage_status(runtime, policy, settings, now=now, required_tokens=required_tokens)


def admin_project_budget_status(runtime, project, *, now=None, required_tokens=None):
    """Read this project's own ledger and daily cap, summed across every host."""
    with _read_snapshot(runtime):
        policy = project_budget_policy(runtime, project)
        global_policy = admin_budget_policy(runtime)
        settings = {key: global_policy["values"][key] for key in
                    ("max_concurrent_calls", "token_reservation_per_call")}
        return _usage_status(runtime, policy, settings, now=now, project=project, required_tokens=required_tokens)


def pending_phase_reservation(runtime, project):
    """Use the same frozen request estimate as claim_call, without preparing work."""
    from .phase_review import policy as phase_policy, instruction
    cfg = phase_policy(runtime, project)
    if cfg["mode"] != "phase":
        return None
    amounts = []
    for row in runtime.db.execute("SELECT b.packet FROM phase_review_batches b JOIN problems p "
        "ON p.project=b.project AND p.id=b.problem_id WHERE b.project=? AND b.host_scope=? "
        "AND b.session_epoch=? AND b.governance_epoch=? AND b.policy_version=? "
        "AND b.state='pending' AND p.status='pending' ORDER BY b.created,b.problem_id LIMIT 40",
        (project, runtime._session_scope(), runtime.session_epoch(), runtime.host_epoch(), cfg["version"])):
        estimate = estimate_reservation(runtime, project, instruction(json.loads(row[0])), phase=True)
        if estimate["admitted"]:
            amounts.append(estimate["reserved_tokens"])
    return min(amounts) if amounts else None


def admin_admission_status(runtime, project, *, now=None, required_tokens=None):
    """Both scopes must admit the same call; legacy terminal status names stay stable."""
    current = time.time() if now is None else now
    with _read_snapshot(runtime):
        from .phase_review import policy as phase_policy
        phase_mode = phase_policy(runtime, project)["mode"] == "phase"
        if required_tokens is None:
            required_tokens = pending_phase_reservation(runtime, project)
            if required_tokens is None and phase_mode:
                # Before local preparation there is no complete request.
                # Its empty-payload estimate is only a lower-bound gate;
                # dispatch and claim recheck the eventual frozen request.
                required_tokens = estimate_reservation(runtime, project, "", phase=True)["reserved_tokens"]
        if required_tokens is not None:
            if type(required_tokens) is not int or not 1 <= required_tokens <= 1000000000:
                raise ContractError("invalid administrator token reservation")
            if not phase_mode:
                required_tokens = max(required_tokens, budget_settings(runtime)["token_reservation_per_call"])
        project_usage = admin_project_budget_status(runtime, project, now=current, required_tokens=required_tokens)
        global_usage = admin_budget_status(runtime, now=current, required_tokens=required_tokens)
        if project_usage["admission_status"] != "available":
            status, scope = project_usage["admission_status"], "project"
        elif global_usage["admission_status"] != "available":
            status, scope = global_usage["admission_status"], "ALP_HOME"
        else:
            status, scope = "available", None
        token_remaining = [usage["remaining_reported_tokens"] for usage in (project_usage, global_usage)
                           if usage["remaining_reported_tokens"] is not None]
        return dict(global_usage, admission_status=status, blocking_scope=scope,
                    admission_scope="project_and_ALP_HOME", project_id=project,
                    remaining_calls=min(project_usage["remaining_calls"], global_usage["remaining_calls"]),
                    remaining_reported_tokens=min(token_remaining) if token_remaining else None,
                    project_usage=project_usage, global_usage=global_usage)
