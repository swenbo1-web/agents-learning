"""有界后台调度；只安排已授权的审核、正式知识发布及可选分发。

待办状态持久化，有限 runner 退出后不保证离线唤醒。模型和网络工作均在
事务外执行；未知付费调用和未知远端写入仍由各自协议等待显式核对。
"""
from __future__ import annotations

import json
import math
import time
import uuid


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
        self.db.execute("DELETE FROM admin_dispatch WHERE lease_until<=? OR host_epoch!=?", (now, epoch))

    def _scheduler_admin_state(self, project, cfg):
        from .admin_queue import eligible_query, scan_signature
        from .admin_usage import admin_budget_status
        from .runtime import digest
        signature = digest({"scan": scan_signature(self, project), "host_epoch": self.host_epoch(),
            "problems": list(self.db.execute("SELECT COUNT(*),COALESCE(SUM(revision),0),MAX(rowid) "
                                               "FROM problems WHERE project=? AND status='pending'", (project,)).fetchone()),
            "tasks": self.db.execute("SELECT MAX(updated) FROM tasks WHERE project=?", (project,)).fetchone()[0],
            "operations": [list(r) for r in self.db.execute("SELECT state,COUNT(*),MAX(rowid) FROM operations "
                                                             "WHERE project=? GROUP BY state", (project,))],
            "retries": list(self.db.execute("SELECT COUNT(*),MAX(finished),COALESCE(SUM(retry_allowed),0) "
                                              "FROM admin_calls WHERE project=?", (project,)).fetchone())})
        if not cfg["enabled"] or not self.config["admin"]["enabled"]:
            return signature, "disabled", None, "admin_disabled"
        now = time.time()
        active = self.db.execute("SELECT MIN(lease_until) FROM admin_calls WHERE project=? AND state "
                                  "IN ('reserved','launch_intent','started')", (project,)).fetchone()[0]
        dispatch = self.db.execute("SELECT lease_until FROM admin_dispatch WHERE project=?", (project,)).fetchone()
        if active is not None or dispatch:
            return signature, "waiting", min(v for v in (active, dispatch[0] if dispatch else None) if v is not None), "active_review"
        query, args = eligible_query(scan_signature(self, project),
                                     preflight_signature=scan_signature(self, project, include_candidates=False))
        eligible = self.db.execute(query + "LIMIT 1", (project, *args)).fetchone()
        if not eligible:
            unknown = self.db.execute("SELECT 1 FROM admin_calls WHERE project=? AND retry_allowed=0 "
                "AND state IN ('timeout_unknown','account_changed_unknown','orphaned_unknown','failed') LIMIT 1", (project,)).fetchone()
            blocked = self.db.execute("SELECT 1 FROM admin_preflights f JOIN problems p ON p.id=f.problem AND p.project=f.project "
                "WHERE f.project=? AND p.status='pending' AND f.revision=p.revision LIMIT 1", (project,)).fetchone()
            awaiting = self.db.execute("SELECT 1 FROM review_states s JOIN problems p ON p.id=s.problem AND p.project=s.project "
                "WHERE s.project=? AND p.status='pending' AND s.revision=p.revision AND s.state='awaiting_evidence' LIMIT 1", (project,)).fetchone()
            reason = "review_required" if unknown else "awaiting_evidence" if blocked or awaiting else "no_review_increment"
            return signature, "waiting" if unknown or blocked or awaiting else "idle", None, reason
        budget = admin_budget_status(self)
        admission = budget["admission_status"]
        if admission != "available":
            due = now + cfg["poll_seconds"] if admission == "concurrency_limited" else budget["next_reset_at"]
            return signature, "waiting", due, admission
        return signature, "ready", now, ""

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
            # 未变化的启动失败采用有限退避；材料变化允许提前重新检查。
            if state != "disabled" and prior and prior["signature"] == signature and prior["wait_reason"] == "retry_backoff" and prior["next_due"] is not None and prior["next_due"] > now:
                state, due, reason = "waiting", prior["next_due"], "retry_backoff"
            elif state == "ready" and prior and prior["signature"] == signature and prior["state"] == "ready" and prior["next_due"] is not None and prior["next_due"] > now:
                # 单批剩余工作等待下次轮询；无增量的重复 Hook 不提前耗尽批次。
                due = prior["next_due"]
            self.db.execute("INSERT INTO scheduler_jobs(project,kind,state,signature,next_due,wait_reason,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project,kind) DO UPDATE SET state=excluded.state,"
                "signature=excluded.signature,next_due=excluded.next_due,wait_reason=excluded.wait_reason,updated=excluded.updated",
                (project, kind, state, signature, due, reason, now, now))

    def scheduler_wake(self, project):
        cfg = self._scheduler_config()
        self._scheduler_projects(project)
        if not cfg["enabled"]:
            return False
        with self.transaction():
            self._scheduler_recover()
            self._scheduler_refresh(project, cfg)
            return bool(self.db.execute("SELECT 1 FROM scheduler_jobs WHERE project=? AND state IN ('ready','waiting') "
                                         "AND next_due<=? LIMIT 1", (project, time.time() + cfg["max_run_seconds"])).fetchone())

    def claim_scheduler_worker(self):
        cfg = self._scheduler_config()
        if not cfg["enabled"]:
            return None
        with self.transaction():
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
        keys = ("status", "state", "stop_reason", "processed", "invocations", "scanned_count", "elapsed_seconds", "budget_exhausted")
        return {k: result[k] for k in keys if k in result and isinstance(result[k], (bool, int, float, str))}

    def _scheduler_tick(self, project, max_jobs, deadline, token, epoch):
        from .admin import admin_batch
        from .runtime import ContractError, encode
        cfg, items = self._scheduler_config(), []
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
                selected = next((r for r in rows if (r["project"], r["kind"]) not in attempted), None)
                if selected is None:
                    break
                owner = str(uuid.uuid4())
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
                    result = admin_batch(self, identifier, _deadline=deadline)
                elif kind == "release":
                    result = self.daily_knowledge_release(identifier, max_items=cfg["max_release_items"], expected_epoch=epoch)
                else:
                    result = self.sync_batch(identifier, max_items=cfg["max_sync_items"], max_seconds=min(cfg["max_sync_seconds"], remaining))
            except Exception as exc:
                result = {"status": "failed", "stop_reason": type(exc).__name__}
            summary = self._scheduler_summary(result)
            with self.transaction():
                current = self.db.execute("SELECT * FROM scheduler_jobs WHERE project=? AND kind=?", (identifier, kind)).fetchone()
                if not self._scheduler_worker_valid(token, epoch) or not current or current["owner"] != owner or current["lease_until"] <= time.time():
                    raise ContractError("scheduler result ownership changed")
                now = time.time()
                self.db.execute("UPDATE scheduler_jobs SET state='idle',owner=NULL,lease_until=NULL,last_run=?,updated=?,summary=?,wait_reason='' "
                                "WHERE project=? AND kind=? AND owner=?", (now, now, encode(summary), identifier, kind, owner))
                self._scheduler_refresh(identifier, cfg)
                if result.get("status") in {"failed", "not_started", "time_exhausted"} or result.get("stop_reason") == "not_started":
                    self.db.execute("UPDATE scheduler_jobs SET state='waiting',next_due=?,wait_reason='retry_backoff' WHERE project=? AND kind=?",
                                    (now + cfg["retry_seconds"], identifier, kind))
                elif kind == "release":
                    # 输出完成后的新变化不能被本次成功覆盖；保留执行前签名。
                    self.db.execute("UPDATE scheduler_jobs SET state='idle',signature=?,next_due=NULL,wait_reason='no_release_increment' WHERE project=? AND kind=?",
                                    (selected["signature"], identifier, kind))
                elif self.db.execute("SELECT state FROM scheduler_jobs WHERE project=? AND kind=?", (identifier, kind)).fetchone()[0] == "ready":
                    self.db.execute("UPDATE scheduler_jobs SET next_due=? WHERE project=? AND kind=?", (now + cfg["poll_seconds"], identifier, kind))
            items.append({"project_id": identifier, "kind": kind, **summary})
        return {"status": "completed", "items": items, "processed": len(items), "budget_exhausted": len(items) >= max_jobs or time.monotonic() >= deadline}

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
        try:
            self._scheduler_start_worker(token, seconds)
        except Exception:
            with self.transaction():
                self.db.execute("DELETE FROM scheduler_worker WHERE token=? AND state='reserved'", (token,))
            raise
        try:
            epoch, deadline = self.host_epoch(), started + seconds
            while time.monotonic() < deadline:
                result = self._scheduler_tick(project, cfg["max_jobs_per_tick"], min(deadline, time.monotonic() + cfg["max_tick_seconds"]), token, epoch)
                processed += result["processed"]
                ticks += 1
                next_due = self.db.execute("SELECT MIN(next_due) FROM scheduler_jobs WHERE state IN ('ready','waiting') "
                                           "AND (? IS NULL OR project=?)", (project, project)).fetchone()[0]
                remaining = deadline - time.monotonic()
                if next_due is None or next_due - time.time() >= remaining or remaining <= 0:
                    break
                time.sleep(min(cfg["poll_seconds"], max(0.01, next_due - time.time()), remaining))
            return {"status": "completed", "processed": processed, "ticks": ticks,
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
