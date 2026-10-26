"""有界后台调度；只安排已授权的审核、正式知识发布及可选分发。

待办状态持久化，有限 runner 退出后不保证离线唤醒。模型和网络工作均在
事务外执行；未知付费调用和未知远端写入仍由各自协议等待显式核对。
"""
from __future__ import annotations

from .phase_review import scope as phase_scope

import json
import math
import time
import uuid
from contextlib import contextmanager


SCHEDULER_DEFAULTS = {
    "enabled": False, "max_jobs_per_tick": 3, "max_tick_seconds": 180,
    "max_run_seconds": 300, "poll_seconds": 5, "retry_seconds": 30,
    "auto_release": False, "release_interval_seconds": 86400,
    "max_release_items": 500, "sync_enabled": False,
    "max_sync_items": 20, "max_sync_seconds": 30,
}
KINDS = ("admin", "release", "sync")


def validate_scheduler_config(config):
    from .runtime import ContractError
    if not isinstance(config, dict) or set(config) - set(SCHEDULER_DEFAULTS):
        raise ContractError("unsupported scheduler settings")
    cfg = {**SCHEDULER_DEFAULTS, **config}
    for key in ("enabled", "auto_release", "sync_enabled"):
        if not isinstance(cfg[key], bool):
            raise ContractError("scheduler enable flags must be booleans")
    limits = {"max_jobs_per_tick": 100, "max_tick_seconds": 600,
              "max_run_seconds": 3600, "poll_seconds": 60, "retry_seconds": 86400,
              "release_interval_seconds": 31536000, "max_release_items": 5000,
              "max_sync_items": 100, "max_sync_seconds": 300}
    for key, maximum in limits.items():
        if isinstance(cfg[key], bool) or not isinstance(cfg[key], int) or not 1 <= cfg[key] <= maximum:
            raise ContractError(f"scheduler {key} must be 1..{maximum}")
    return cfg


def _seconds(value, default, maximum):
    from .runtime import ContractError
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ContractError(f"scheduler time allowance must be finite and <= {maximum}")
    return value


def phase_batch_rollover(runtime, project, batch, deadline, *, fresh_seconds=None):
    """Renew a spent child window only after phase work made real progress.

    The new window must fit the original call timeout and both enclosing
    allowances. This never changes budgets or retries a failed/unknown call.
    """
    if (batch.get("status") != "batch_complete"
            or batch.get("stop_reason") != "batch_time_exhausted"
            or batch.get("reason") != "insufficient_call_window"
            or not batch.get("scanned_count", 0)
            or phase_scope(runtime, project) is None):
        return False
    required = batch.get("required_seconds")
    if (isinstance(required, bool) or not isinstance(required, (int, float))
            or not math.isfinite(required) or required <= 0):
        return False
    cfg = runtime.admin_effective_config(project)
    available = min(cfg["max_batch_seconds"], cfg.get("max_run_seconds", 600),
                    deadline - time.monotonic())
    if fresh_seconds is not None:
        available = min(available, fresh_seconds)
    return cfg["enabled"] and available >= required


class SchedulerMixin:
    def _migrate_scheduler_storage(self):
        from .runtime import ContractError
        if not self.db.in_transaction:
            raise ContractError("scheduler installation requires a write transaction")
        self.db.execute("""CREATE TABLE IF NOT EXISTS scheduler_jobs(
            project TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('admin','release','sync')),
            state TEXT NOT NULL CHECK(state IN ('ready','running','waiting','idle','disabled')),
            signature TEXT NOT NULL,next_due REAL,wait_reason TEXT NOT NULL DEFAULT '',
            owner TEXT,host_epoch INTEGER,lease_until REAL,last_run REAL NOT NULL DEFAULT 0,
            created REAL NOT NULL,updated REAL NOT NULL,summary TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(project,kind),FOREIGN KEY(project) REFERENCES projects(id))""")
        self.db.execute("CREATE INDEX IF NOT EXISTS scheduler_due ON scheduler_jobs(state,next_due,last_run)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS scheduler_worker(
            id INTEGER PRIMARY KEY CHECK(id=1),token TEXT NOT NULL,host_epoch INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('reserved','running')),lease_until REAL NOT NULL,created REAL NOT NULL)""")

    def _scheduler_config(self):
        return validate_scheduler_config(self.config.get("scheduler", {}))

    def _scheduler_projects(self, project=None):
        from .runtime import ContractError
        if project is not None and (not isinstance(project, str) or not project or
                not self.db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone()):
            raise ContractError("unknown scheduler project")
        return [r[0] for r in self.db.execute("SELECT id FROM projects WHERE (? IS NULL OR id=?) ORDER BY id",
                                             (project, project))]

    def _scheduler_recover(self):
        from .admin_lifecycle import recover_calls
        # 日额度跨项目共享，因此未启动预留的安全回收也必须覆盖全部项目。
        recover_calls(self)
        now, epoch = time.time(), self.host_epoch()
        self.db.execute("DELETE FROM scheduler_worker WHERE lease_until<=? OR host_epoch!=?", (now, epoch))
        self.db.execute("UPDATE scheduler_jobs SET state='waiting',wait_reason='interrupted',owner=NULL,"
                        "lease_until=NULL,next_due=NULL,updated=? WHERE state='running' "
                        "AND (lease_until<=? OR host_epoch!=?)", (now, now, epoch))
        from .admin_queue import recover_dispatch
        recover_dispatch(self, now)

    def _scheduler_admin_scopes(self, project):
        current = self._session_scope()
        scopes = [current]
        for table in ("admin_control_state", "admin_host_policies"):
            exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
            if exists:
                for row in self.db.execute(
                    f"SELECT host_scope FROM {table} WHERE project=? AND enabled=1 ORDER BY updated DESC,host_scope ASC",
                    (project,),
                ).fetchall():
                    scope = row[0]
                    if isinstance(scope, str) and ":" in scope and scope not in scopes:
                        scopes.append(scope)
        last = self.db.execute("SELECT json_extract(summary,'$.host_scope') FROM scheduler_jobs "
                               "WHERE project=? AND kind='admin'", (project,)).fetchone()
        if last and last[0] in scopes and len(scopes) > 1:
            # A different host may wake the next runner. Keep one cycle even
            # when the caller changes, instead of rebuilding caller priority.
            scopes.sort()
            start = scopes.index(last[0]) + 1
            scopes = scopes[start:] + scopes[:start]
        return scopes

    @contextmanager
    def _scheduler_host_scope(self, target_scope):
        from .host_sessions import host_scope_key
        host, instance = target_scope.split(":", 1)
        host_scope_key(host, instance)
        old_host, old_instance, old_scope = self.host, self.host_instance, self.host_scope
        self.host, self.host_instance, self.host_scope = host, instance, target_scope
        try:
            yield
        finally:
            self.host, self.host_instance, self.host_scope = old_host, old_instance, old_scope

    def _scheduler_eval_admin_for_scope(self, project, cfg, target_scope):
        from .admin_queue import eligible_query, scan_signatures, ready_executor, dispatch_diagnostic, routing_scan_pending
        from .admin_usage import admin_admission_status
        from .runtime import digest
        with self._scheduler_host_scope(target_scope):
            # Only a scheduler write entry may advance local source scans.
            # Read-only status remains a view of the durable queue.
            if (self.db.in_transaction and cfg["enabled"]
                    and self.admin_effective_config(project)["enabled"]):
                from .phase_review import prepare as prepare_phases
                prepare_phases(self, project)
            scan, preflight = scan_signatures(self, project)
            signature = digest({"scan": scan, "host_epoch": self.host_epoch(),
                "problems": list(self.db.execute("SELECT COUNT(*),COALESCE(SUM(revision),0),MAX(rowid) "
                                                   "FROM problems WHERE project=? AND status='pending'", (project,)).fetchone()),
                "tasks": self.db.execute("SELECT MAX(updated) FROM tasks WHERE project=?", (project,)).fetchone()[0],
                "operations": [list(r) for r in self.db.execute("SELECT state,COUNT(*),MAX(rowid) FROM operations "
                                                                 "WHERE project=? GROUP BY state", (project,))],
                "retries": list(self.db.execute("SELECT COUNT(*),MAX(finished),COALESCE(SUM(retry_allowed),0) "
                                                  "FROM admin_calls WHERE project=?", (project,)).fetchone())})
            if not cfg["enabled"] or not self.admin_effective_config(project)["enabled"]:
                return signature, "disabled", None, "admin_disabled"
            now = time.time()
            active = self.db.execute("SELECT MIN(lease_until) FROM admin_calls WHERE project=? AND state "
                                      "IN ('reserved','launch_intent','started')", (project,)).fetchone()[0]
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='admin_response_rechecks'").fetchone():
                local = self.db.execute("SELECT MIN(lease_until) FROM admin_response_rechecks WHERE project=? "
                    "AND state='validating' AND lease_until>?", (project, now)).fetchone()[0]
                if local is not None:
                    active = min(active, local) if active is not None else local
            dispatch = self.db.execute("SELECT lease_until FROM admin_dispatch WHERE project=?", (project,)).fetchone()
            if active is not None or dispatch:
                return signature, "waiting", min(v for v in (active, dispatch[0] if dispatch else None) if v is not None), "active_review"
            query, args = eligible_query(scan, preflight_signature=preflight,
                                         summary_enabled=self.work_summary_enabled(project),
                                         summary_host_scope=self._session_scope(), phase_scope=phase_scope(self, project))
            eligible = self.db.execute(query + "LIMIT 1", (project, *args)).fetchone()
            if not eligible:
                from .admin_queue import review_protection_status
                protection = review_protection_status(self, project, scan, preflight_signature=preflight)
                call_blocked = protection["queue_blocked_by_protected_calls"]
                evidence_wait = protection.get("awaiting_evidence", False)
                tool_wait = protection.get("awaiting_tool_completion", False)
                reason = ("awaiting_tool_completion" if tool_wait else "review_required" if call_blocked
                          else "awaiting_evidence" if evidence_wait
                          else protection.get("reason") or "no_review_increment")
                return signature, "waiting" if call_blocked or evidence_wait or tool_wait else "idle", None, reason
            if not ready_executor(self, project, query, args, advance=self.db.in_transaction):
                if routing_scan_pending(self, project, query, args):
                    return signature, "waiting", now + cfg["poll_seconds"], "executor_scan_pending"
                diagnostic = dispatch_diagnostic(self, project)
                return signature, "waiting", diagnostic.get("next_due"), diagnostic["reason"]
            budget = admin_admission_status(self, project)
            admission = budget["admission_status"]
            if admission != "available":
                due = now + cfg["poll_seconds"] if admission == "concurrency_limited" else budget["next_reset_at"]
                return signature, "waiting", due, admission
            return signature, "ready", now, ""

    def _scheduler_admin_selection(self, project, cfg):
        scopes = self._scheduler_admin_scopes(project)
        primary = self._scheduler_eval_admin_for_scope(project, cfg, scopes[0])
        if not cfg["enabled"] or primary[1] == "ready" or len(scopes) == 1:
            return (*primary, scopes[0])
        best, best_scope = primary, scopes[0]
        rank = {"ready": 3, "waiting": 2, "idle": 1, "disabled": 0}
        for scope in scopes[1:]:
            candidate = self._scheduler_eval_admin_for_scope(project, cfg, scope)
            if candidate[1] == "ready":
                return (*candidate, scope)
            if rank[candidate[1]] > rank[best[1]]:
                best, best_scope = candidate, scope
            elif candidate[1] == "waiting" and best[1] == "waiting":
                if candidate[2] is not None and (best[2] is None or candidate[2] < best[2]):
                    best, best_scope = candidate, scope
        return (*best, best_scope)

    def _scheduler_admin_state(self, project, cfg):
        # The shared job reports aggregate readiness; execution must retain the
        # exact scope selected in its claim transaction.
        return self._scheduler_admin_selection(project, cfg)[:4]

    def _scheduler_release_state(self, project, cfg, prior):
        from .runtime import digest
        p = self.db.execute("SELECT generation,revocation_epoch,source_epoch FROM projects WHERE id=?", (project,)).fetchone()
        signature = digest({"project": list(p), "host_epoch": self.host_epoch(),
            "lifecycle": list(self.db.execute("SELECT COUNT(*),COALESCE(SUM(version),0) FROM knowledge_lifecycle_versions "
                                                "WHERE project=?", (project,)).fetchone()),
            "maturity": list(self.db.execute("SELECT COUNT(*),MAX(created) FROM knowledge_maturity WHERE project=?", (project,)).fetchone())})
        if not cfg["enabled"] or not cfg["auto_release"]:
            return signature, "disabled", None, "release_disabled"
        if not self.db.execute("SELECT 1 FROM knowledge WHERE project=? LIMIT 1", (project,)).fetchone():
            return signature, "idle", None, "no_published_knowledge"
        if prior and prior["signature"] == signature and prior["wait_reason"] == "no_release_increment":
            return signature, "idle", None, "no_release_increment"
        due = max(time.time(), (prior["last_run"] if prior else 0) + cfg["release_interval_seconds"])
        return signature, "ready" if due <= time.time() else "waiting", due, "" if due <= time.time() else "release_interval"

    def _scheduler_sync_state(self, project, cfg):
        from .runtime import digest
        self._sync_expire(project)
        dirty = list(self.db.execute("SELECT COUNT(*),MAX(rowid),COALESCE(SUM(dirty_version),0) FROM knowledge_sync_dirty "
                                     "WHERE project=?", (project,)).fetchone())
        rows = [list(r) for r in self.db.execute("SELECT state,COUNT(*),MAX(updated) FROM outbox "
                                                  "WHERE project=? GROUP BY state", (project,))]
        signature = digest({"dirty": dirty, "queue": rows, "host_epoch": self.host_epoch(),
                            "target": self._target_identity(self.config["hindsight"])})
        if not cfg["enabled"] or not cfg["sync_enabled"] or not self.config["hindsight"]["enabled"]:
            return signature, "disabled", None, "sync_disabled"
        now = time.time()
        if dirty[0]:
            return signature, "ready", now, ""
        pending = self.db.execute("SELECT MIN(next_try) FROM outbox WHERE project=? AND state='pending' AND attempts<3",
                                  (project,)).fetchone()[0]
        if pending is not None:
            return signature, "ready" if pending <= now else "waiting", max(now, pending), "" if pending <= now else "sync_retry_time"
        running = self.db.execute("SELECT MIN(lease_until) FROM outbox WHERE project=? AND state='running'", (project,)).fetchone()[0]
        if running is not None:
            return signature, "waiting", running, "active_sync"
        blocked = self.db.execute("SELECT 1 FROM outbox WHERE project=? AND state='blocked' LIMIT 1", (project,)).fetchone()
        return signature, "waiting" if blocked else "idle", None, "sync_review_required" if blocked else "no_sync_increment"

    def _scheduler_refresh(self, project, cfg):
        now = time.time()
        for kind in KINDS:
            prior = self.db.execute("SELECT * FROM scheduler_jobs WHERE project=? AND kind=?", (project, kind)).fetchone()
            if prior and prior["state"] == "running":
                continue
            result = (self._scheduler_admin_state(project, cfg) if kind == "admin" else
                      self._scheduler_release_state(project, cfg, prior) if kind == "release" else
                      self._scheduler_sync_state(project, cfg))
            signature, state, due, reason = result
            # Executor failures and configuration blocks come from the current
            # executor, not a project-wide stale deadline. Only an unclassified
            # scheduling exception retains its short unchanged-signature retry.
            if (state == "ready" and prior and prior["signature"] == signature
                    and prior["wait_reason"] in {"retry_backoff", "insufficient_call_window"}
                    and prior["next_due"] is not None and prior["next_due"] > now):
                state, due, reason = "waiting", prior["next_due"], prior["wait_reason"]
            elif state == "ready" and prior and prior["signature"] == signature and prior["state"] == "ready" and prior["next_due"] is not None and prior["next_due"] > now:
                # 单批剩余工作等待下次轮询；无增量的重复 Hook 不提前耗尽批次。
                due = prior["next_due"]
            self.db.execute("INSERT INTO scheduler_jobs(project,kind,state,signature,next_due,wait_reason,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project,kind) DO UPDATE SET state=excluded.state,"
                "signature=excluded.signature,next_due=excluded.next_due,wait_reason=excluded.wait_reason,updated=excluded.updated",
                (project, kind, state, signature, due, reason, now, now))

    def scheduler_wake(self, project, *, expected_epoch=None, _phase_guard=None):
        cfg = self._scheduler_config()
        self._scheduler_projects(project)
        if not cfg["enabled"]:
            return False
        with self.transaction():
            self.assert_host_epoch(expected_epoch)
            if _phase_guard:
                _phase_guard()
            self._scheduler_recover()
            self._scheduler_refresh(project, cfg)
            return bool(self.db.execute("SELECT 1 FROM scheduler_jobs WHERE project=? AND state IN ('ready','waiting') "
                                         "AND next_due<=? LIMIT 1", (project, time.time() + cfg["max_run_seconds"])).fetchone())

    def claim_scheduler_worker(self, *, expected_epoch=None, _phase_guard=None):
        cfg = self._scheduler_config()
        if not cfg["enabled"]:
            return None
        with self.transaction():
            self.assert_host_epoch(expected_epoch)
            if _phase_guard:
                _phase_guard()
            self._scheduler_recover()
            if self.db.execute("SELECT 1 FROM scheduler_worker").fetchone():
                return None
            now, token = time.time(), str(uuid.uuid4())
            self.db.execute("INSERT INTO scheduler_worker VALUES(1,?,?,'reserved',?,?)",
                            (token, self.host_epoch(), now + max(cfg["max_run_seconds"], cfg["max_tick_seconds"]) + 30, now))
            return token

    def release_scheduler_worker(self, token):
        if token:
            with self.transaction():
                self.db.execute("DELETE FROM scheduler_worker WHERE token=?", (token,))

    def _scheduler_start_worker(self, token, seconds):
        from .runtime import ContractError
        if not isinstance(token, str) or not token:
            raise ContractError("scheduler worker token is required")
        with self.transaction():
            now = time.time()
            changed = self.db.execute("UPDATE scheduler_worker SET state='running',lease_until=? WHERE token=? AND state='reserved' "
                "AND host_epoch=? AND lease_until>?", (now + seconds + 30, token, self.host_epoch(), now)).rowcount
            if not changed:
                raise ContractError("scheduler worker already started or ownership expired")

    def _scheduler_worker_valid(self, token, epoch):
        self.assert_host_epoch(epoch)
        return self.db.execute("SELECT 1 FROM scheduler_worker WHERE token=? AND state='running' AND host_epoch=? "
                               "AND lease_until>?", (token, epoch, time.time())).fetchone() is not None

    @staticmethod
    def _scheduler_summary(result):
        # 不复制模型响应、案例或日志；状态页只保存固定的计数及停止原因。
        keys = ("status", "state", "stop_reason", "processed", "invocations", "scanned_count", "elapsed_seconds", "budget_exhausted", "host_scope",
                "required_seconds", "remaining_seconds")
        return {k: result[k] for k in keys if k in result and isinstance(result[k], (bool, int, float, str))}

    def _scheduler_admin_run_deadline(self, project, scope, limits):
        """One project allowance per finite runner, shared by all host scopes."""
        with self._scheduler_host_scope(scope):
            seconds = self.admin_effective_config(project).get("max_run_seconds", 600)
        now = time.monotonic()
        limit = limits.setdefault(project, {"started": now, "deadline": now + seconds})
        limit["deadline"] = min(limit["deadline"], limit["started"] + seconds)
        return limit["deadline"]

    def _scheduler_tick(self, project, max_jobs, deadline, token, epoch, _admin_run_limits=None,
                        _run_deadline=None):
        from .admin import admin_batch
        from .runtime import ContractError, encode
        cfg, items = self._scheduler_config(), []
        run_limits = {} if _admin_run_limits is None else _admin_run_limits
        exhausted = set()
        with self.transaction():
            if not self._scheduler_worker_valid(token, epoch):
                raise ContractError("scheduler worker ownership changed")
            self._scheduler_recover()
            for identifier in self._scheduler_projects(project):
                self._scheduler_refresh(identifier, cfg)
        attempted = set()
        while len(items) < max_jobs:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with self.transaction():
                if not self._scheduler_worker_valid(token, epoch):
                    raise ContractError("scheduler worker ownership changed")
                rows = self.db.execute("SELECT * FROM scheduler_jobs WHERE state IN ('ready','waiting') AND next_due<=? "
                    "AND (? IS NULL OR project=?) ORDER BY last_run,created,project,CASE kind WHEN 'release' THEN 0 WHEN 'sync' THEN 1 ELSE 2 END LIMIT 100", (time.time(), project, project)).fetchall()
                selected, admin_plan = None, None
                for candidate in rows:
                    key = (candidate["project"], candidate["kind"])
                    if key in attempted:
                        continue
                    if candidate["kind"] == "admin":
                        if run_limits.get(candidate["project"], {}).get("deferred"):
                            attempted.add(key)
                            exhausted.add(candidate["project"])
                            continue
                        selection = self._scheduler_admin_selection(candidate["project"], cfg)
                        if selection[1] != "ready":
                            # A wake-up is only a hint. Do not invoke the current
                            # host when the ready scope disappeared meanwhile.
                            attempted.add(key)
                            self._scheduler_refresh(candidate["project"], cfg)
                            continue
                        run_deadline = self._scheduler_admin_run_deadline(candidate["project"], selection[4], run_limits)
                        if run_deadline <= time.monotonic():
                            attempted.add(key)
                            exhausted.add(candidate["project"])
                            self.db.execute("UPDATE scheduler_jobs SET state='waiting',next_due=NULL,"
                                "wait_reason='run_time_exhausted' WHERE project=? AND kind='admin'",
                                (candidate["project"],))
                            continue
                        from .admin_queue import scan_signature
                        with self._scheduler_host_scope(selection[4]):
                            admin_plan = {"host_scope": selection[4], "session_epoch": self.session_epoch(),
                                          "signature": scan_signature(self, candidate["project"]),
                                          "run_deadline": run_deadline}
                    selected = candidate
                    break
                if selected is None:
                    break
                owner = ((admin_plan["host_scope"] + "/") if admin_plan else "") + str(uuid.uuid4())
                self.db.execute("UPDATE scheduler_jobs SET state='running',owner=?,host_epoch=?,lease_until=?,updated=? "
                    "WHERE project=? AND kind=?", (owner, epoch, time.time() + remaining + 30, time.time(), selected["project"], selected["kind"]))
            identifier, kind = selected["project"], selected["kind"]
            attempted.add((identifier, kind))
            try:
                # 租约在派发前再次校验；外部工作不持 SQLite 写锁。
                self.assert_host_epoch(epoch)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result = {"status": "time_exhausted"}
                elif kind == "admin":
                    from .admin_queue import scan_signature
                    with self._scheduler_host_scope(admin_plan["host_scope"]):
                        with self.transaction():
                            self.assert_session_epoch(admin_plan["session_epoch"])
                            valid_plan = scan_signature(self, identifier) == admin_plan["signature"]
                        project_deadline = self._scheduler_admin_run_deadline(identifier, admin_plan["host_scope"], run_limits)
                        result = (admin_batch(self, identifier, expected_epoch=epoch, _deadline=min(deadline, project_deadline))
                                  if valid_plan else {"status": "state_changed", "invocations": 0})
                        result["host_scope"] = admin_plan["host_scope"]
                elif kind == "release":
                    result = self.daily_knowledge_release(identifier, max_items=cfg["max_release_items"], expected_epoch=epoch)
                else:
                    result = self.sync_batch(identifier, max_items=cfg["max_sync_items"], max_seconds=min(cfg["max_sync_seconds"], remaining), expected_epoch=epoch)
            except Exception as exc:
                result = {"status": "failed", "stop_reason": type(exc).__name__}
            if kind == "admin" and admin_plan:
                result["host_scope"] = admin_plan["host_scope"]
            summary = self._scheduler_summary(result)
            with self.transaction():
                current = self.db.execute("SELECT * FROM scheduler_jobs WHERE project=? AND kind=?", (identifier, kind)).fetchone()
                if not self._scheduler_worker_valid(token, epoch) or not current or current["owner"] != owner or current["lease_until"] <= time.time():
                    raise ContractError("scheduler result ownership changed")
                now = time.time()
                self.db.execute("UPDATE scheduler_jobs SET state='idle',owner=NULL,lease_until=NULL,last_run=?,updated=?,summary=?,wait_reason='' "
                                "WHERE project=? AND kind=? AND owner=?", (now, now, encode(summary), identifier, kind, owner))
                self._scheduler_refresh(identifier, cfg)
                if kind == "admin" and result.get("reason") == "insufficient_call_window":
                    with self._scheduler_host_scope(admin_plan["host_scope"]):
                        outer_deadline = min(project_deadline, _run_deadline if _run_deadline is not None else deadline)
                        rollover = phase_batch_rollover(self, identifier, result, outer_deadline,
                                                        fresh_seconds=cfg["max_tick_seconds"])
                    if rollover:
                        # This tick's tail was short; a new bounded tick still
                        # fits. Keep unrelated projects eligible in this tick.
                        self.db.execute("UPDATE scheduler_jobs SET next_due=? WHERE project=? "
                                        "AND kind='admin' AND state='ready'", (now, identifier))
                    else:
                        # No progress or no complete window remains. A later
                        # finite runner can resume without probing in a loop.
                        run_limits[identifier]["deferred"] = True
                        exhausted.add(identifier)
                        self.db.execute("UPDATE scheduler_jobs SET state='waiting',next_due=?,wait_reason='insufficient_call_window' "
                                        "WHERE project=? AND kind='admin' AND state='ready'",
                                        (now + cfg["poll_seconds"], identifier))
                elif result.get("status") in {"failed", "not_started", "time_exhausted"}:
                    refreshed = self.db.execute("SELECT state FROM scheduler_jobs WHERE project=? AND kind=?",
                                                (identifier, kind)).fetchone()
                    if refreshed["state"] == "ready":
                        self.db.execute("UPDATE scheduler_jobs SET state='waiting',next_due=?,wait_reason='retry_backoff' WHERE project=? AND kind=?",
                                         (now + cfg["retry_seconds"], identifier, kind))
                elif kind == "release":
                    # 输出完成后的新变化不能被本次成功覆盖；保留执行前签名。
                    self.db.execute("UPDATE scheduler_jobs SET state='idle',signature=?,next_due=NULL,wait_reason='no_release_increment' WHERE project=? AND kind=?",
                                    (selected["signature"], identifier, kind))
                elif self.db.execute("SELECT state FROM scheduler_jobs WHERE project=? AND kind=?", (identifier, kind)).fetchone()[0] == "ready":
                    self.db.execute("UPDATE scheduler_jobs SET next_due=? WHERE project=? AND kind=?", (now + cfg["poll_seconds"], identifier, kind))
            items.append({"project_id": identifier, "kind": kind, **summary})
        return {"status": "completed", "items": items, "processed": len(items),
                "admin_run_exhausted_projects": sorted(exhausted),
                "budget_exhausted": len(items) >= max_jobs or time.monotonic() >= deadline}

    def scheduler_tick(self, project=None, max_jobs=None, max_seconds=None, expected_epoch=None):
        from .runtime import ContractError
        cfg = self._scheduler_config()
        max_jobs = cfg["max_jobs_per_tick"] if max_jobs is None else max_jobs
        if isinstance(max_jobs, bool) or not isinstance(max_jobs, int) or not 1 <= max_jobs <= 100:
            raise ContractError("scheduler max_jobs must be 1..100")
        seconds = _seconds(max_seconds, cfg["max_tick_seconds"], 600)
        self._scheduler_projects(project)
        epoch = self.assert_host_epoch(expected_epoch)
        if not cfg["enabled"]:
            return {"status": "disabled", "items": [], "processed": 0}
        token = self.claim_scheduler_worker()
        if not token:
            return {"status": "busy", "items": [], "processed": 0}
        try:
            self._scheduler_start_worker(token, seconds)
        except Exception:
            with self.transaction():
                self.db.execute("DELETE FROM scheduler_worker WHERE token=? AND state='reserved'", (token,))
            raise
        try:
            return self._scheduler_tick(project, max_jobs, time.monotonic() + seconds, token, epoch)
        finally:
            self.release_scheduler_worker(token)

    def scheduler_run(self, project=None, max_seconds=None, worker_token=None):
        cfg = self._scheduler_config()
        seconds = _seconds(max_seconds, cfg["max_run_seconds"], 3600)
        self._scheduler_projects(project)
        if not cfg["enabled"]:
            if worker_token:
                # 禁用后的迟到启动只能取消自己的未启动预留，不能释放活动 runner。
                with self.transaction():
                    self.db.execute("DELETE FROM scheduler_worker WHERE token=? AND state='reserved'", (worker_token,))
            return {"status": "disabled", "processed": 0, "ticks": 0}
        token = worker_token or self.claim_scheduler_worker()
        if not token:
            return {"status": "busy", "processed": 0, "ticks": 0}
        started, processed, ticks = time.monotonic(), 0, 0
        admin_run_limits = {}
        exhausted = set()
        try:
            self._scheduler_start_worker(token, seconds)
        except Exception:
            with self.transaction():
                self.db.execute("DELETE FROM scheduler_worker WHERE token=? AND state='reserved'", (token,))
            raise
        try:
            epoch, deadline = self.host_epoch(), started + seconds
            while time.monotonic() < deadline:
                result = self._scheduler_tick(project, cfg["max_jobs_per_tick"], min(deadline, time.monotonic() + cfg["max_tick_seconds"]),
                                              token, epoch, _admin_run_limits=admin_run_limits, _run_deadline=deadline)
                processed += result["processed"]
                ticks += 1
                exhausted.update(result["admin_run_exhausted_projects"])
                exclusions = (" AND (kind!='admin' OR project NOT IN (" + ",".join("?" for _ in exhausted) + "))") if exhausted else ""
                next_due = self.db.execute("SELECT MIN(next_due) FROM scheduler_jobs WHERE state IN ('ready','waiting') "
                                           "AND (? IS NULL OR project=?)" + exclusions, (project, project, *sorted(exhausted))).fetchone()[0]
                remaining = deadline - time.monotonic()
                if next_due is None or next_due - time.time() >= remaining or remaining <= 0:
                    break
                time.sleep(min(cfg["poll_seconds"], max(0.01, next_due - time.time()), remaining))
            return {"status": "completed", "processed": processed, "ticks": ticks,
                    "admin_run_exhausted_projects": sorted(exhausted),
                    "elapsed_seconds": round(time.monotonic() - started, 3), "budget_exhausted": time.monotonic() >= deadline}
        finally:
            self.release_scheduler_worker(token)

    def scheduler_status(self, project=None):
        self._scheduler_projects(project)
        rows = self.db.execute("SELECT project,kind,state,next_due,wait_reason,last_run,updated,summary FROM scheduler_jobs "
                               "WHERE (? IS NULL OR project=?) ORDER BY project,kind LIMIT 100", (project, project)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["summary"] = json.loads(item["summary"])
            items.append(item)
        worker = self.db.execute("SELECT state,host_epoch,lease_until,created FROM scheduler_worker").fetchone()
        dirty = self.db.execute("SELECT COUNT(*) FROM knowledge_sync_dirty WHERE (? IS NULL OR project=?)", (project, project)).fetchone()[0]
        return {"enabled": self._scheduler_config()["enabled"], "items": items, "worker": dict(worker) if worker else None,
                "sync_dirty_count": dirty, "total_jobs": self.db.execute("SELECT COUNT(*) FROM scheduler_jobs "
                    "WHERE (? IS NULL OR project=?)", (project, project)).fetchone()[0],
                "continuous_service_installed": False}
