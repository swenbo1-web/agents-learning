"""轻量展示与采用回执；工具观测和声明分开，不推断因果或业务验收。"""
from __future__ import annotations

from collections import Counter
import time
import uuid


class LearningEvaluationMixin:
    def _migrate_learning_evaluation(self):
        # 由 Runtime 的既有写事务调用，不启动嵌套事务、不更改 schema 版本。
        if not self.db.in_transaction:
            raise RuntimeError("learning evaluation migration requires a transaction")
        statements = (
            "CREATE TABLE IF NOT EXISTS learning_exposures(id TEXT PRIMARY KEY,project TEXT NOT NULL,"
            "conversation TEXT NOT NULL,agent TEXT NOT NULL,task_id TEXT NOT NULL,task_revision INTEGER NOT NULL,"
            "task_write_version INTEGER NOT NULL,execution_epoch INTEGER NOT NULL,assignment_id TEXT NOT NULL,"
            "host_epoch INTEGER NOT NULL,knowledge_id TEXT NOT NULL,knowledge_revision INTEGER NOT NULL,"
            "method TEXT NOT NULL,created REAL NOT NULL)",
            "CREATE INDEX IF NOT EXISTS learning_exposures_window ON learning_exposures(project,created)",
            "CREATE TABLE IF NOT EXISTS knowledge_adoptions(id TEXT PRIMARY KEY,project TEXT NOT NULL,"
            "conversation TEXT NOT NULL,agent TEXT NOT NULL,task_id TEXT NOT NULL,task_revision INTEGER NOT NULL,"
            "task_write_version INTEGER NOT NULL,execution_epoch INTEGER NOT NULL,assignment_id TEXT NOT NULL,"
            "host_epoch INTEGER NOT NULL,knowledge_id TEXT NOT NULL,knowledge_revision INTEGER NOT NULL,"
            "operation_id TEXT,exposure_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,request_digest TEXT NOT NULL,"
            "created REAL NOT NULL,UNIQUE(project,idempotency_key))",
            "CREATE INDEX IF NOT EXISTS knowledge_adoptions_window ON knowledge_adoptions(project,created)",
        )
        for statement in statements:
            self.db.execute(statement)

    def _learning_scope(self, project, conversation, agent_id=None, context_handle=None, require_current=False):
        from .runtime import ContractError
        assignment = self.assignment_context(project, conversation, agent_id, context_handle) if agent_id else None
        if agent_id:
            if not assignment or not assignment["task_id"]:
                if require_current:
                    raise ContractError("assignment_required: knowledge use requires a task assignment")
                return None
            if require_current and not assignment["current"]:
                raise ContractError("assignment_stale: knowledge use requires the current task assignment")
            task = self.task_snapshot(project, assignment["task_id"], assignment["task_revision"],
                                      assignment["task_write_version"])
        else:
            task = self.task(project, conversation)
        if not task:
            if require_current:
                raise ContractError("task_required: knowledge use requires a current task")
            return None
        return {"project": project, "conversation": conversation, "agent": agent_id or "",
                "task_id": task["id"], "task_revision": task["revision"],
                "task_write_version": task["write_version"], "execution_epoch": task["execution_epoch"],
                "assignment_id": assignment["assignment_id"] if assignment else "",
                "host_epoch": self.session_epoch()}

    def record_task_exposure(self, project, conversation, agent_id, items, method="mcp"):
        """只记录实际返回的知识；同一精确任务边界和方式的重复展示去重。"""
        from .runtime import ContractError, digest
        if not self.db.in_transaction:
            raise ContractError("task exposure requires the caller's transaction")
        if method not in {"hook", "mcp"}:
            raise ContractError("invalid exposure method")
        if not items:
            return 0
        scope = self._learning_scope(project, conversation, agent_id)
        if scope is None:
            return 0
        inserted = 0
        for item in items:
            identity = {**scope, "knowledge_id": item["knowledge_id"],
                        "knowledge_revision": item["revision"], "method": method}
            inserted += self.db.execute("INSERT OR IGNORE INTO learning_exposures VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (digest(identity), *scope.values(), item["knowledge_id"], item["revision"], method, time.time())).rowcount
        return inserted

    @staticmethod
    def _learning_binding(row):
        return (row["project"], row["conversation"], row["agent"] or "", row["task_id"], row["task_revision"],
                row["task_write_version"], row["execution_epoch"], row["assignment_id"] or "", row["host_epoch"])

    def _adoption_operation(self, scope, operation_id, *, require_pre=False, exposure_created=None):
        from .runtime import ContractError
        if not operation_id:
            return {"operation_id": None, "outcome": "unlinked", "tool_success_observed": False}
        row = self.db.execute("SELECT * FROM operations WHERE project=? AND id=?",
                              (scope["project"], operation_id)).fetchone()
        if not row or self._learning_binding(row) != self._learning_binding(scope):
            if require_pre:
                raise ContractError("operation_scope_mismatch: operation must belong to this exact task and agent")
            return {"operation_id": operation_id, "outcome": "missing" if not row else "scope_mismatch",
                    "tool_success_observed": False}
        pre = self.db.execute("SELECT * FROM events WHERE project=? AND operation=? AND type='PreToolUse' "
                              "ORDER BY created,id LIMIT 1", (scope["project"], operation_id)).fetchone()
        # 操作身份由真实前置事件绑定。仅有 Post 的回补记录不能作为采用的工具证据。
        matched_pre = pre and (pre["conversation"], pre["agent"] or "", pre["task_id"], pre["task_revision"],
            pre["task_write_version"], pre["execution_epoch"], pre["assignment_id"] or "") == (
            scope["conversation"], scope["agent"] or "", scope["task_id"], scope["task_revision"],
            scope["task_write_version"], scope["execution_epoch"], scope["assignment_id"] or "")
        if not matched_pre:
            if require_pre:
                raise ContractError("operation_pre_required: operation needs a matching PreToolUse binding")
            return {"operation_id": operation_id, "outcome": "unbound", "tool_success_observed": False}
        if require_pre and exposure_created is not None and pre["created"] < exposure_created:
            raise ContractError("exposure_after_operation: knowledge must be exposed before the operation starts")
        outcome = "conflict" if row["conflict"] else "denied" if row["decision"] == "denied" else row["observation"]
        # 换号/中断等 unknown 状态不能沿用旧的成功观测。
        if row["state"] == "unknown" and outcome not in {"conflict", "denied"}:
            outcome = "unknown"
        return {"operation_id": operation_id, "outcome": outcome, "state": row["state"],
                "decision": row["decision"], "tool_success_observed": outcome == "succeeded"}

    def _adoption_knowledge(self, project, knowledge_id, revision):
        row = self.db.execute("SELECT * FROM knowledge WHERE project=? AND id=? AND revision=?",
                              (project, knowledge_id, revision)).fetchone()
        active = bool(self.db.execute("SELECT 1 FROM active WHERE project=? AND id=? AND revision=?",
                                      (project, knowledge_id, revision)).fetchone())
        if not row:
            return {"active": False, "source_health": "missing", "pending_counterexample_count": 0,
                    "source_tool_success_observed": False, "problem_changed": False}
        public = self._public(project, row, include_maturity=False)
        return {"active": active, "source_health": public["source_health"],
                "pending_counterexample_count": public["pending_counterexample_count"],
                "source_tool_success_observed": public["current_tool_success"],
                "problem_changed": public["problem_changed"]}

    def _adoption_public(self, row):
        item = dict(row)
        return {"adoption_id": item["id"], "status": "reported", "knowledge_id": item["knowledge_id"],
                "knowledge_revision": item["knowledge_revision"], "task_id": item["task_id"],
                "task_revision": item["task_revision"], "task_write_version": item["task_write_version"],
                "execution_epoch": item["execution_epoch"], "host_epoch": item["host_epoch"],
                "assignment_id": item["assignment_id"] or None, "agent_id": item["agent"] or None,
                "created": item["created"], "knowledge_state": self._adoption_knowledge(
                    item["project"], item["knowledge_id"], item["knowledge_revision"]),
                "operation_observation": self._adoption_operation(item, item["operation_id"]),
                "assessment": "reported_use_and_current_observation_only"}

    def knowledge_use(self, project, context_handle, knowledge_id, knowledge_revision, expected_task_id,
                      expected_task_revision, expected_write_version, operation_id=None,
                      idempotency_key=None, expected_epoch=None):
        from .runtime import ContractError, digest
        for name, value in (("knowledge_id", knowledge_id), ("expected_task_id", expected_task_id)):
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ContractError(f"{name} must be a nonempty string of at most 200 characters")
        for name, value in (("knowledge_revision", knowledge_revision), ("expected_task_revision", expected_task_revision),
                            ("expected_write_version", expected_write_version)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ContractError(f"{name} must be a positive integer")
        for name, value in (("operation_id", operation_id), ("idempotency_key", idempotency_key)):
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 200):
                raise ContractError(f"{name} must be a nonempty string of at most 200 characters")
        if expected_epoch is not None and (isinstance(expected_epoch, bool) or not isinstance(expected_epoch, int)
                                           or expected_epoch < 0):
            raise ContractError("expected_epoch must be a nonnegative integer")
        with self.transaction():
            access = self.access_context(project, context_handle, expected_epoch=expected_epoch)
            scope = self._learning_scope(project, access["conversation_id"], access["agent_id"], context_handle, True)
            if (scope["task_id"], scope["task_revision"], scope["task_write_version"]) != (
                    expected_task_id, expected_task_revision, expected_write_version):
                raise ContractError("stale task identity or version for knowledge use")
            request = {**scope, "context_handle": context_handle, "knowledge_id": knowledge_id,
                       "knowledge_revision": knowledge_revision, "operation_id": operation_id}
            fingerprint = digest(request)
            key = idempotency_key or fingerprint
            previous = self.db.execute("SELECT * FROM knowledge_adoptions WHERE project=? AND idempotency_key=?",
                                       (project, key)).fetchone()
            if previous:
                if previous["request_digest"] != fingerprint:
                    raise ContractError("idempotency key reused with different knowledge use content")
                return self._adoption_public(previous)
            if not self.db.execute("SELECT 1 FROM active WHERE project=? AND id=? AND revision=?",
                                    (project, knowledge_id, knowledge_revision)).fetchone():
                raise ContractError("knowledge is not currently active at the reported revision")
            exposure = self.db.execute("SELECT * FROM learning_exposures WHERE project=? AND conversation=? AND agent=? "
                "AND task_id=? AND task_revision=? AND task_write_version=? AND execution_epoch=? AND assignment_id=? "
                "AND host_epoch=? AND knowledge_id=? AND knowledge_revision=? ORDER BY created,id LIMIT 1",
                (*scope.values(), knowledge_id, knowledge_revision)).fetchone()
            if not exposure:
                raise ContractError("task_exposure_required: knowledge was not returned to this task and agent")
            self._adoption_operation(scope, operation_id, require_pre=True, exposure_created=exposure["created"])
            identity = str(uuid.uuid4())
            self.db.execute("INSERT INTO knowledge_adoptions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity, *scope.values(), knowledge_id, knowledge_revision, operation_id, exposure["id"], key,
                 fingerprint, time.time()))
            return self._adoption_public(self.db.execute("SELECT * FROM knowledge_adoptions WHERE id=?", (identity,)).fetchone())

    def learning_effects(self, project, days=30):
        """时间窗计数与有限样本的当前状态；不把相关观测换算成净节省。"""
        from .runtime import ContractError
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise ContractError("days must be an integer from 1 to 365")
        owned = not self.db.in_transaction
        if owned:
            self.db.execute("BEGIN")
        try:
            until = time.time()
            since = until - days * 86400
            exposure_counts = {row["method"]: row["n"] for row in self.db.execute(
                "SELECT method,COUNT(*) n FROM learning_exposures WHERE project=? AND created>=? AND created<=? "
                "GROUP BY method", (project, since, until))}
            total = self.db.execute("SELECT COUNT(*) FROM knowledge_adoptions WHERE project=? AND created>=? AND created<=?",
                                    (project, since, until)).fetchone()[0]
            rows = self.db.execute("SELECT * FROM knowledge_adoptions WHERE project=? AND created>=? AND created<=? "
                                   "ORDER BY created DESC,id LIMIT 200", (project, since, until)).fetchall()
            operations, health = Counter(), Counter()
            knowledge_cache = {}
            revoked = pending = changed = 0
            for row in rows:
                observation = self._adoption_operation(row, row["operation_id"])
                operations[observation["outcome"]] += 1
                key = (row["knowledge_id"], row["knowledge_revision"])
                if key not in knowledge_cache:
                    knowledge_cache[key] = self._adoption_knowledge(project, *key)
                state = knowledge_cache[key]
                health[state["source_health"]] += 1
                revoked += int(not state["active"])
                pending += int(state["pending_counterexample_count"] > 0)
                changed += int(state["problem_changed"])
            result = {"window": {"days": days, "since": since, "until": until},
                      "exposures": {"hook": exposure_counts.get("hook", 0), "mcp": exposure_counts.get("mcp", 0),
                                    "counting": "first_unique_task_version_agent_epoch_knowledge_revision_method"},
                      "reported_adoptions": total, "current_observation_sample": {
                          "size": len(rows), "limit": 200, "truncated": total > len(rows),
                          "selection": "latest_reported_adoptions_in_window", "operation_outcomes": dict(operations),
                          "knowledge_source_health": dict(health), "inactive_knowledge": revoked,
                          "pending_counterexamples": pending, "changed_source_problems": changed},
                      "limitations": ["Exposure records returned content, not reading or adoption.",
                                      "Adoption is reported by the agent; linked tool outcomes are observations.",
                                      "Knowledge exposure preceding a tool does not prove causal benefit.",
                                      "No professional correctness, user acceptance, or net token savings is inferred.",
                                      "Repeated exposure in the same exact scope and method is deduplicated; window uses first exposure time."]}
            if owned:
                self.db.execute("COMMIT")
            return result
        except BaseException:
            if owned:
                self.db.execute("ROLLBACK")
            raise
