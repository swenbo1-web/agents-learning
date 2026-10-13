"""宿主会话边界；项目知识和治理队列仍共用全局版本。"""
from __future__ import annotations

import re
import time


HOSTS = frozenset({"codex", "antigravity", "dsh"})
_INSTANCE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_RESERVED = ("codex:", "antigravity:", "dsh:")


def host_scope_key(host, instance="default"):
    from .runtime import ContractError

    if host not in HOSTS or not isinstance(instance, str) or not _INSTANCE.fullmatch(instance):
        raise ContractError("host must be codex, antigravity or dsh and host_instance must be 1..64 letters, digits, '_' or '-'")
    return f"{host}:{instance}"


class HostSessionMixin:
    def _session_scope(self):
        host = getattr(self, "host", "codex")
        instance = getattr(self, "host_instance", "default")
        scope = host_scope_key(host, instance)
        if getattr(self, "host_scope", scope) != scope:
            from .runtime import ContractError
            raise ContractError("host scope does not match the configured host instance")
        return scope

    def _install_host_storage(self):
        """迁移调用方持有写事务；旧 Codex epoch 和通知保持幂等。"""
        from .runtime import ContractError

        if not self.db.in_transaction:
            raise ContractError("host storage installation requires a write transaction")
        self.db.execute("""CREATE TABLE IF NOT EXISTS host_scopes(
            scope TEXT PRIMARY KEY,epoch INTEGER NOT NULL CHECK(epoch>=0))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS host_scope_notifications(
            scope TEXT NOT NULL,id TEXT NOT NULL,epoch INTEGER NOT NULL,created REAL NOT NULL,
            PRIMARY KEY(scope,id))""")
        global_epoch = self.host_epoch()
        self.db.execute("INSERT OR IGNORE INTO host_scopes VALUES('codex:default',?)", (global_epoch,))
        self.db.execute("INSERT OR IGNORE INTO host_scopes VALUES(?,?)", (self._session_scope(), global_epoch))
        # 老通知没有宿主字段，只能属于原来的 Codex 默认宿主。
        self.db.execute("INSERT OR IGNORE INTO host_scope_notifications(scope,id,epoch,created) "
                        "SELECT 'codex:default',id,epoch,created FROM host_notifications")

    def session_epoch(self):
        from .runtime import ContractError

        row = self.db.execute("SELECT epoch FROM host_scopes WHERE scope=?", (self._session_scope(),)).fetchone()
        if row is None:
            raise ContractError("host scope is not initialized")
        return row[0]

    def assert_session_epoch(self, expected_epoch):
        from .runtime import ContractError

        epoch = self.session_epoch()
        if expected_epoch is not None and expected_epoch != epoch:
            raise ContractError("reconnect_required: host session context changed")
        return epoch

    def host_conversation(self, native):
        """只转换原生身份；不能把另一个宿主的数据库身份当作原生输入。"""
        from .runtime import ContractError

        if not isinstance(native, str) or not native.strip():
            raise ContractError("native conversation identity must be a nonempty string")
        if native.startswith(_RESERVED):
            raise ContractError("native conversation identity uses a reserved host namespace")
        scope = self._session_scope()
        return native if scope == "codex:default" else f"{scope}:{native}"

    def owns_conversation(self, conversation):
        if not isinstance(conversation, str) or not conversation.strip():
            return False
        scope = self._session_scope()
        if scope == "codex:default":
            return not conversation.startswith(_RESERVED)
        prefix = scope + ":"
        return conversation.startswith(prefix) and bool(conversation[len(prefix):].strip())

    def scope_account_changed(self, notification_id=None):
        """只失效本宿主会话；共享治理执行器因全局 fence 停止接受旧回写。"""
        from .runtime import ContractError

        if notification_id is not None and (not isinstance(notification_id, str)
                or not notification_id.strip() or len(notification_id) > 200):
            raise ContractError("notification_id must be a nonempty string of at most 200 characters")
        scope = self._session_scope()
        with self.transaction():
            if notification_id and self.db.execute(
                    "SELECT 1 FROM host_scope_notifications WHERE scope=? AND id=?", (scope, notification_id)).fetchone():
                return {"host_scope": scope, "host_epoch": self.session_epoch(),
                        "governance_epoch": self.host_epoch(), "changed": False,
                        "knowledge_scope": "shared_project"}
            # 全局治理 epoch 单调增加，仍由 admin/scheduler/sync 的原契约校验。
            self.db.execute("UPDATE host_state SET epoch=epoch+1 WHERE id=1")
            governance_epoch = self.host_epoch()
            self.db.execute("UPDATE host_scopes SET epoch=? WHERE scope=?", (governance_epoch, scope))
            epoch = self.session_epoch()
            if notification_id:
                self.db.execute("INSERT INTO host_scope_notifications VALUES(?,?,?,?)",
                                (scope, notification_id, epoch, time.time()))
            conversations = [row[0] for row in self.db.execute("SELECT conversation FROM session_projects")
                             if self.owns_conversation(row[0])]
            for conversation in conversations:
                self.db.execute("UPDATE operations SET state=CASE WHEN state IN ('started','running') "
                                "THEN 'unknown' ELSE state END,host_epoch=-1 WHERE conversation=?", (conversation,))
                for task in self.db.execute("SELECT project FROM tasks WHERE conversation=?", (conversation,)).fetchall():
                    self._execution_fence(task["project"], conversation, "account_changed")
                for table in ("contexts", "subagent_contexts"):
                    self.db.execute(f"UPDATE {table} SET epoch=epoch+1,injected='[]',last_query='',"
                                    "retrieval_signature='',retrieval_refs='[]' WHERE conversation=?", (conversation,))
            # 共用的治理队列可能正在审核任一宿主的案例；旧执行器保留未知额度。
            self.db.execute("UPDATE admin_calls SET state='account_changed_unknown',finished=?,lease_until=NULL "
                            "WHERE state IN ('reserved','launch_intent','started')", (time.time(),))
            return {"host_scope": scope, "host_epoch": epoch, "governance_epoch": governance_epoch,
                    "changed": True, "knowledge_scope": "shared_project", "reconnect_required": True,
                    "governance_reconnect_required": True}
