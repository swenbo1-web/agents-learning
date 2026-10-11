"""有界的可选分发队列；本地知识状态始终是权威。

正文、来源健康和生命周期组成不可变状态记录。未确认的远端写入需要
显式对账后重试；过期租约和迟到回执不能将未知写入误记为未发送。
"""
from __future__ import annotations

import json
import math
import time
import uuid


SYNC_STATES = {"pending", "running", "blocked", "superseded", "synced"}


def install_sync(runtime):
    """幂等补齐派生队列字段，保留旧载荷及未知结果，不推进总 schema。"""
    db = runtime.db
    columns = {r[1] for r in db.execute("PRAGMA table_info(outbox)")}
    additions = {
        "prepare_attempts": "INTEGER NOT NULL DEFAULT 0",
        "write_outcome": "TEXT NOT NULL DEFAULT 'not_sent'",
        "lease_counted": "INTEGER NOT NULL DEFAULT 0",
        "created": "REAL NOT NULL DEFAULT 0",
        "updated": "REAL NOT NULL DEFAULT 0",
    }
    first_install = "prepare_attempts" not in columns
    for name, declaration in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE outbox ADD COLUMN {name} {declaration}")
    db.execute("""CREATE TABLE IF NOT EXISTS knowledge_sync_dirty(
        project TEXT NOT NULL,knowledge_id TEXT NOT NULL,revision INTEGER NOT NULL,
        dirty_version INTEGER NOT NULL DEFAULT 1,updated REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(project,knowledge_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS sync_receipts(
        id TEXT PRIMARY KEY,project TEXT NOT NULL,operation_id TEXT NOT NULL,
        lease TEXT NOT NULL,state TEXT NOT NULL,diagnostic TEXT NOT NULL,created REAL NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS sync_receipts_operation ON sync_receipts(project,operation_id,created)")
    db.execute("CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(project,state,next_try)")
    db.execute("CREATE INDEX IF NOT EXISTS outbox_knowledge_state ON outbox(project,"
               "json_extract(payload,'$.knowledge_id'),state,write_outcome) WHERE json_valid(payload)")
    if first_install:
        # 旧代码在预检前计 attempts；明确 not_sent 的失败恢复为实际发送次数0。
        for row in db.execute("SELECT id,state,attempts,error FROM outbox").fetchall():
            error = {}
            try:
                error = json.loads(row["error"] or "{}")
            except (ValueError, TypeError):
                pass
            known_not_sent = isinstance(error, dict) and error.get("write_outcome") == "not_sent"
            attempts = 0 if known_not_sent else row["attempts"]
            outcome = ("confirmed" if row["state"] == "synced" else "not_sent"
                       if known_not_sent or not attempts and row["state"] != "running" else "unknown")
            db.execute("UPDATE outbox SET prepare_attempts=?,attempts=?,write_outcome=?,lease_counted=? WHERE id=?",
                       (row["attempts"], attempts, outcome, int(row["state"] == "running"), row["id"]))


class KnowledgeSyncMixin:
    @staticmethod
    def _target_identity(target):
        return {k: target.get(k) for k in ("url", "bank_id", "api_key_env", "strategy")}

    def _sync_snapshot(self, project, knowledge_id, revision):
        from .runtime import clean, digest
        snapshot = clean(self.knowledge_state_snapshot(project, knowledge_id, revision))
        return snapshot, digest(snapshot)

    def _queue_sync(self, project, knowledge_id, revision, body):
        """调用者持有本地写事务；排队内容与最终快照逐字段一致。"""
        from .runtime import ContractError, clean, digest, encode
        if not self.config["hindsight"]["enabled"]:
            return 0
        snapshot, state_digest = self._sync_snapshot(project, knowledge_id, revision)
        if snapshot.get("body") != clean(body):
            raise ContractError("sync body does not match authoritative state snapshot")
        target = clean(self.config["hindsight"])
        identity = self._target_identity(target)
        payload = {"knowledge_id": knowledge_id, "revision": revision, "body": snapshot["body"],
                   "target": target, "envelope": snapshot, "snapshot_digest": state_digest}
        ident = digest([project, knowledge_id, revision, state_digest, identity])
        now = time.time()
        # 仅取消同一目标、确定尚未发送的旧状态。未知写入仍保留待对账记录。
        for row in self.db.execute("SELECT id,payload FROM outbox WHERE project=? AND json_valid(payload) "
                                   "AND json_extract(payload,'$.knowledge_id')=? AND state IN ('pending','blocked') "
                                   "AND write_outcome='not_sent'", (project, knowledge_id)).fetchall():
            try:
                old = json.loads(row["payload"])
                stale = (old.get("knowledge_id") == knowledge_id and old.get("revision") == revision and
                         self._target_identity(old.get("target", {})) == identity and row["id"] != ident)
            except (ValueError, TypeError, AttributeError):
                stale = False
            if stale:
                self.db.execute("UPDATE outbox SET state='superseded',lease=NULL,lease_until=0,updated=? WHERE id=?",
                                (now, row["id"]))
        created = self.db.execute("INSERT OR IGNORE INTO outbox(id,project,kind,payload,created,updated) VALUES(?,?,?,?,?,?)",
                                  (ident, project, "published_sync", encode(payload), now, now)).rowcount
        # 当前事务中的dirty已包含在快照里；后续治理修改会重新登记。
        self.db.execute("DELETE FROM knowledge_sync_dirty WHERE project=? AND knowledge_id=? AND revision=?",
                        (project, knowledge_id, revision))
        return created

    def _consume_sync_dirty(self, project, limit=20):
        if not self.config["hindsight"]["enabled"]:
            return 0
        count = 0
        rows = self.db.execute("SELECT * FROM knowledge_sync_dirty WHERE project=? ORDER BY rowid LIMIT ?",
                               (project, limit)).fetchall()
        for row in rows:
            knowledge = self.db.execute("SELECT body FROM knowledge WHERE project=? AND id=? AND revision=?",
                                        (project, row["knowledge_id"], row["revision"])).fetchone()
            if knowledge:
                count += self._queue_sync(project, row["knowledge_id"], row["revision"], json.loads(knowledge[0]))
            self.db.execute("DELETE FROM knowledge_sync_dirty WHERE project=? AND knowledge_id=? AND revision=? "
                            "AND dirty_version=?", (project, row["knowledge_id"], row["revision"], row["dirty_version"]))
        return count

    def enqueue_sync(self, project):
        if not self.config["hindsight"]["enabled"]:
            return {"status": "disabled", "enqueued": 0}
        with self.transaction():
            count = self._consume_sync_dirty(project)
            # 包含已撤销条目的末版，便于重新启用分发时补齐 tombstone。
            rows = self.db.execute("SELECT k.* FROM knowledge k JOIN (SELECT id,MAX(revision) revision FROM knowledge "
                                   "WHERE project=? GROUP BY id) latest ON k.id=latest.id AND k.revision=latest.revision "
                                   "WHERE k.project=? ORDER BY k.id", (project, project)).fetchall()
            count += sum(self._queue_sync(project, row["id"], row["revision"], json.loads(row["body"])) for row in rows)
        return {"status": "enqueued", "enqueued": count}

    def _sync_expire(self, project):
        from .runtime import encode
        now = time.time()
        diagnostic = encode({"kind": "expired_lease", "write_outcome": "unknown"})
        self.db.execute("UPDATE outbox SET state='blocked',attempts=attempts+(1-lease_counted),write_outcome='unknown',"
                        "lease=NULL,lease_until=0,lease_counted=0,error=?,updated=? "
                        "WHERE project=? AND state='running' AND lease_until<?", (diagnostic, now, project, now))

    def _sync_current(self, project, payload):
        if "envelope" not in payload:
            # 迁移保留历史 document_id 与正文语义，不伪造已有远端状态记录。
            active = self.db.execute("SELECT revision FROM active WHERE project=? AND id=?",
                                     (project, payload["knowledge_id"])).fetchone()
            return bool(active and active[0] == payload["revision"])
        snapshot, state_digest = self._sync_snapshot(project, payload["knowledge_id"], payload["revision"])
        return state_digest == payload.get("snapshot_digest") and snapshot == payload["envelope"]

    @staticmethod
    def _sync_confirmation(result):
        # Adapter已经严格验证同步确认；队列也只暴露固定且有界字段。
        if not isinstance(result, dict):
            return {}
        accepted = {}
        for key in ("success", "async", "items_count", "bank_id", "status"):
            value = result.get(key)
            if isinstance(value, (bool, int)) or isinstance(value, str) and len(value) <= 256:
                accepted[key] = value
        return accepted

    def sync_once(self, project, retry=None, *, max_seconds=None):
        """只分发知识；blocked必须显式恢复，网络调用不占数据库事务。"""
        from .runtime import ContractError, clean, encode
        from .hindsight import HindsightClient, HindsightError
        if max_seconds is not None and (isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
                or not math.isfinite(max_seconds) or not 0 < max_seconds <= 300):
            raise ContractError("sync time allowance must be finite and <=300")
        if not self.config["hindsight"]["enabled"]:
            return {"status": "disabled"}
        lease = str(uuid.uuid4())
        with self.transaction():
            self._sync_expire(project)
            self._consume_sync_dirty(project)
            if retry is not None:
                if not isinstance(retry, str) or not retry:
                    raise ContractError("sync retry requires operation ID")
                previous = self.db.execute("SELECT * FROM outbox WHERE project=? AND id=? AND state='blocked'",
                                            (project, retry)).fetchone()
                if not previous or previous["write_outcome"] != "not_sent" and previous["attempts"] >= 3:
                    raise ContractError("no retryable knowledge operation; verify remote state before requesting retry")
                self.db.execute("UPDATE outbox SET state='pending',error=NULL,next_try=0,updated=? WHERE project=? AND id=?",
                                (time.time(), project, retry))
            row = self.db.execute("SELECT * FROM outbox WHERE project=? AND attempts<3 AND next_try<=? AND state='pending' "
                                   "AND (? IS NULL OR id=?) ORDER BY rowid LIMIT 1", (project, time.time(), retry, retry)).fetchone()
            if not row:
                return {"status": "idle"}
            payload = clean(json.loads(row["payload"]))
            if self._target_identity(payload["target"]) != self._target_identity(clean(self.config["hindsight"])):
                diagnostic = encode({"kind": "target_changed", "write_outcome": row["write_outcome"]})
                self.db.execute("UPDATE outbox SET state='blocked',error=?,updated=? WHERE id=?",
                                (diagnostic, time.time(), row["id"]))
                return {"status": "blocked", "operation_id": row["id"], "reason": "target configuration changed"}
            if not self._sync_current(project, payload):
                state = "superseded" if row["write_outcome"] == "not_sent" else "blocked"
                diagnostic = encode({"kind": "state_changed", "write_outcome": row["write_outcome"]})
                self.db.execute("UPDATE outbox SET state=?,error=?,updated=? WHERE id=?",
                                (state, diagnostic, time.time(), row["id"]))
                return {"status": state, "operation_id": row["id"]}
            # timeout校验交给适配器；合法值决定保守租约，非法值仍可记录not_sent。
            target_config = clean(self.config["hindsight"])
            try:
                timeout = float(target_config.get("timeout_seconds", 10))
                if max_seconds is not None and math.isfinite(timeout) and timeout > 0:
                    # 一次retain有两个HTTP阶段；保留响应处理余量。
                    timeout = min(timeout, max_seconds / 3)
                    target_config["timeout_seconds"] = timeout
                duration = max(30, timeout * 3) if math.isfinite(timeout) and timeout > 0 else 30
            except (ValueError, TypeError, OverflowError):
                duration = 30
            self.db.execute("UPDATE outbox SET state='running',prepare_attempts=prepare_attempts+1,lease=?,lease_until=?,"
                            "lease_counted=0,write_outcome='unknown',updated=? WHERE id=?",
                            (lease, time.time() + duration, time.time(), row["id"]))
        try:
            suffix = ":state:" + payload["snapshot_digest"] if "envelope" in payload else ""
            result = HindsightClient(target_config).retain(
                f"{project}:{payload['knowledge_id']}@{payload['revision']}{suffix}",
                encode(payload.get("envelope", payload["body"])), project, payload["revision"], row["id"])
            result = self._sync_confirmation(result)
            state, outcome, error = "synced", "confirmed", None
        except Exception as exc:
            diagnostic = {"error_type": type(exc).__name__, "write_outcome": "unknown"}
            if isinstance(exc, HindsightError):
                diagnostic.update(kind=exc.kind, status_code=exc.status_code, retryable=exc.retryable,
                                  write_outcome="not_sent" if exc.write_outcome == "not_sent" else "unknown")
            result = {"status": "failed", **diagnostic}
            state, outcome, error = "blocked", diagnostic["write_outcome"], encode(diagnostic)
        with self.transaction():
            # 迟到回执单独保存供人工对账，不修改已失租的队列结果。
            attempt_outcome = outcome
            if row["write_outcome"] == "unknown" and outcome == "not_sent":
                # 本次预检失败不能证明此前未知的远端写入没有发生。
                outcome = "unknown"
                result.update(attempt_write_outcome=attempt_outcome, write_outcome=outcome)
                error = encode(result)
            changed = self.db.execute("UPDATE outbox SET state=?,error=?,write_outcome=?,attempts=attempts+?,"
                                      "lease=NULL,lease_until=0,lease_counted=0,updated=? WHERE project=? AND id=? "
                                      "AND state='running' AND lease=?",
                                      (state, error, outcome, int(attempt_outcome != "not_sent"), time.time(), project, row["id"], lease)).rowcount
            receipt_state = state if changed else "late_" + state
            self.db.execute("INSERT INTO sync_receipts VALUES(?,?,?,?,?,?,?)",
                            (str(uuid.uuid4()), project, row["id"], lease, receipt_state, encode(result), time.time()))
            if not changed:
                current = self.db.execute("SELECT state FROM outbox WHERE project=? AND id=?", (project, row["id"])).fetchone()
                return {"status": "lease_lost", "operation_id": row["id"], "current_state": current[0] if current else "missing"}
        return {"status": state, "operation_id": row["id"], "result": result}

    def _sync_summary(self, row):
        from .runtime import digest
        payload = json.loads(row["payload"])
        target = payload.get("target", {})
        diagnostic = {}
        try:
            diagnostic = json.loads(row["error"] or "{}")
        except (ValueError, TypeError):
            diagnostic = {"kind": "legacy_error"}
        if not isinstance(diagnostic, dict):
            diagnostic = {"kind": "legacy_error"}
        error = {k: v for k, v in diagnostic.items() if k in {"kind", "error_type", "status_code", "retryable", "write_outcome"}
                 and (isinstance(v, (bool, int)) or v is None or isinstance(v, str) and len(v) <= 128)}
        return {"operation_id": row["id"], "state": row["state"], "knowledge_id": payload.get("knowledge_id"),
                "revision": payload.get("revision"), "snapshot_digest": payload.get("snapshot_digest"),
                "active": payload.get("envelope", {}).get("active"), "attempts": row["attempts"],
                "prepare_attempts": row["prepare_attempts"], "write_outcome": row["write_outcome"],
                "target_identity": digest(self._target_identity(target)), "error": error,
                "legacy_payload": "envelope" not in payload, "created": row["created"], "updated": row["updated"]}

    def sync_list(self, project, state=None, limit=20, offset=0):
        from .runtime import ContractError
        if state is not None and state not in SYNC_STATES:
            raise ContractError("unsupported sync state")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ContractError("sync list limit must be 1..100")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 100000:
            raise ContractError("sync list offset must be 0..100000")
        rows = self.db.execute("SELECT * FROM outbox WHERE project=? AND (? IS NULL OR state=?) ORDER BY rowid LIMIT ? OFFSET ?",
                               (project, state, state, limit + 1, offset)).fetchall()
        return {"items": [self._sync_summary(row) for row in rows[:limit]], "has_more": len(rows) > limit,
                "next_offset": offset + limit if len(rows) > limit else None}

    def sync_read(self, project, operation_id):
        from .runtime import ContractError
        row = self.db.execute("SELECT * FROM outbox WHERE project=? AND id=?", (project, operation_id)).fetchone()
        if not row:
            raise ContractError("unknown knowledge sync operation")
        result = self._sync_summary(row)
        receipts = self.db.execute("SELECT id,state,diagnostic,created FROM sync_receipts WHERE project=? AND operation_id=? "
                                   "ORDER BY created DESC,rowid DESC LIMIT 10", (project, operation_id)).fetchall()
        result["receipts"] = [{"receipt_id": r["id"], "state": r["state"], "result": json.loads(r["diagnostic"]),
                               "created": r["created"]} for r in receipts]
        return result

    def sync_batch(self, project, max_items=20, max_seconds=30):
        from .runtime import ContractError
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 100:
            raise ContractError("sync batch max_items must be 1..100")
        if isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) or not math.isfinite(max_seconds) or not 0 < max_seconds <= 300:
            raise ContractError("sync batch max_seconds must be finite and <=300")
        started, items = time.monotonic(), []
        for _ in range(max_items):
            if time.monotonic() - started >= max_seconds:
                break
            item = self.sync_once(project, max_seconds=max_seconds - (time.monotonic() - started))
            if item["status"] in {"idle", "disabled"}:
                break
            items.append({k: item[k] for k in ("status", "operation_id", "current_state") if k in item})
        return {"status": "disabled" if not self.config["hindsight"]["enabled"] else "completed",
                "items": items, "processed": len(items), "elapsed_seconds": round(time.monotonic() - started, 3),
                "budget_exhausted": len(items) >= max_items or time.monotonic() - started >= max_seconds}
