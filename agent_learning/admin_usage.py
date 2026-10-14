"""管理员的上报用量与准入账；未知消费保留预留，不估算价格。"""
from __future__ import annotations

import contextlib
import json
import time

from .runtime import ContractError, digest, encode

TOKEN_FIELDS = {"input_tokens", "output_tokens", "cached_input_tokens", "total_tokens"}
ADMIN_BUDGET_DEFAULTS = {
    "max_calls_per_day": 100, "max_calls_per_batch": 5, "max_cases_per_batch": 40,
    "max_batch_seconds": 180, "max_run_seconds": 600, "timeout_seconds": 120,
    "max_concurrent_calls": 1, "max_reported_tokens_per_day": None,
    "token_reservation_per_call": 4000,
}
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
    runtime.db.execute("CREATE TABLE IF NOT EXISTS admin_call_budgets("
                       "call_id TEXT PRIMARY KEY,reserved_tokens INTEGER NOT NULL,reported_tokens INTEGER NOT NULL DEFAULT 0,"
                       "completeness TEXT NOT NULL DEFAULT 'unknown',conflict INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL)")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS admin_usage_observations("
                       "call_id TEXT NOT NULL,observation_id TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL,"
                       "PRIMARY KEY(call_id,observation_id))")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS admin_calls_daily_budget ON admin_calls(day,state)")
    reservation = budget_settings(runtime)["token_reservation_per_call"]
    for row in runtime.db.execute("SELECT id,usage FROM admin_calls WHERE state!='deduplicated' AND "
                                  "NOT EXISTS(SELECT 1 FROM admin_call_budgets b WHERE b.call_id=admin_calls.id)").fetchall():
        runtime.db.execute("INSERT INTO admin_call_budgets(call_id,reserved_tokens,updated) VALUES(?,?,?)",
                           (row["id"], reservation, time.time()))
        if row["usage"]:
            record_usage(runtime, row["id"], json.loads(row["usage"]))


def reserve_usage(runtime, call_id):
    reservation = budget_settings(runtime)["token_reservation_per_call"]
    runtime.db.execute("INSERT INTO admin_call_budgets(call_id,reserved_tokens,updated) VALUES(?,?,?)",
                       (call_id, reservation, time.time()))


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


def admin_budget_status(runtime, *, now=None):
    """只读当前 UTC 日全局账；调度器回收后读取，查询本身不释放未知额度。"""
    policy = admin_budget_policy(runtime)
    cfg = policy["values"]
    settings = {key: cfg[key] for key in ("max_concurrent_calls", "token_reservation_per_call", "max_reported_tokens_per_day")}
    current = time.time() if now is None else now
    day = time.strftime("%Y-%m-%d", time.gmtime(current))
    next_reset = (int(current) // 86400 + 1) * 86400
    rows = runtime.db.execute("SELECT c.state,b.* FROM admin_calls c LEFT JOIN admin_call_budgets b ON b.call_id=c.id "
                              "WHERE c.day=? AND c.state NOT IN ('not_started','deduplicated')", (day,)).fetchall()
    active = runtime.db.execute("SELECT COUNT(*) FROM admin_calls WHERE state IN ('reserved','launch_intent','started')").fetchone()[0]
    usage_states = {"complete": 0, "partial": 0, "unknown": 0, "conflict": 0}
    reported, reservations, accounted = 0, 0, 0
    for row in rows:
        completeness = row["completeness"] or "unknown"
        usage_states[completeness] += 1
        usage_states["conflict"] += int(bool(row["conflict"]))
        known = row["reported_tokens"] or 0
        reserved = row["reserved_tokens"] if row["reserved_tokens"] is not None else settings["token_reservation_per_call"]
        pending = row["state"] in {"reserved", "launch_intent", "started", "timeout_unknown",
                                   "account_changed_unknown", "orphaned_unknown"} or completeness != "complete"
        reported += known
        reservations += reserved if pending else 0
        accounted += max(reserved, known) if pending else known
    maximum = settings["max_reported_tokens_per_day"]
    remaining = None if maximum is None else max(0, maximum - accounted)
    remaining_calls = max(0, cfg["max_calls_per_day"] - len(rows))
    admission = ("budget_exhausted" if remaining_calls == 0 else
                 "concurrency_limited" if active >= settings["max_concurrent_calls"] else
                 "token_budget_exhausted" if remaining is not None and remaining < settings["token_reservation_per_call"] else "available")
    return {"utc_day": day, "used_calls": len(rows), "remaining_calls": remaining_calls,
            "next_reset_at": next_reset, "global_active_calls": active, **settings,
            "reported_tokens": reported, "token_reservations": reservations, "accounted_tokens": accounted,
            "remaining_reported_tokens": remaining, "usage_states": usage_states, "admission_status": admission,
            "budget_scope": "ALP_HOME", "budget_policy": policy,
            "max_calls_per_day": cfg["max_calls_per_day"],
            "limitation": "reported usage admission; executor token or fee cap is not guaranteed"}
