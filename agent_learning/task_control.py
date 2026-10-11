"""任务合同、写入版本与执行轮次分别保存，避免旧会话结果覆盖新任务。"""
from __future__ import annotations

import json
import time
import uuid


class TaskControlMixin:
    def task_update(self, project, conversation_id, goal, scope=None, expected_revision=None, status=None,
                    new_task=False, context_handle=None, expected_epoch=None, *, expected_task_id=None,
                    expected_write_version=None, resume=False):
        # 延迟导入避免 Runtime 组合本 mixin 时形成循环依赖。
        from .execution import validate_scope
        from .runtime import ContractError, TASK_STATUSES, clean, encode

        if not isinstance(conversation_id, str) or not conversation_id.strip() or not isinstance(goal, str) or not goal.strip():
            raise ContractError("conversation_id and goal are required")
        if status is not None and (not isinstance(status, str) or status not in TASK_STATUSES):
            raise ContractError("invalid task status")
        if not isinstance(new_task, bool) or not isinstance(resume, bool):
            raise ContractError("new_task and resume must be booleans")
        if scope is not None and not isinstance(scope, dict):
            raise ContractError("scope must be an object")
        for name, value in (("expected_revision", expected_revision), ("expected_write_version", expected_write_version)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ContractError(f"{name} must be a nonnegative integer")
        if expected_task_id is not None and (not isinstance(expected_task_id, str) or not expected_task_id):
            raise ContractError("expected_task_id must be a nonempty string")
        if resume and (new_task or status not in {None, "active"}):
            raise ContractError("resume requires an existing task and active status")

        with self.transaction():
            self.assert_host_epoch(expected_epoch)
            if context_handle is not None:
                access = self.access_context(project, context_handle, conversation_id, expected_epoch)
                if access["agent_id"]:
                    raise ContractError("parent_task_read_only: subagents cannot overwrite the parent task")
            self.bind_session(project, conversation_id)
            previous = self.task(project, conversation_id)
            revision = previous["revision"] if previous else 0
            write_version = previous["write_version"] if previous else 0

            if previous:
                # 合同 revision 允许状态变化且会在新任务重置，不能作为并发写入凭据。
                has_expectation = any(value is not None for value in
                                      (expected_task_id, expected_write_version, expected_revision))
                if (context_handle is not None or has_expectation) and (
                        expected_task_id is None or expected_write_version is None):
                    raise ContractError("task_expectation_required: supply expected_task_id and expected_write_version")
                if expected_task_id is not None and expected_task_id != previous["id"]:
                    raise ContractError("stale task identity")
            elif expected_task_id is not None:
                raise ContractError("stale task identity")
            if expected_write_version is not None and write_version != expected_write_version:
                raise ContractError("stale task write version")
            if expected_revision is not None and revision != expected_revision:
                raise ContractError("stale task revision")
            if resume and not previous:
                raise ContractError("resume requires an existing task")

            effective_scope = scope if scope is not None else previous["scope"] if previous and not new_task else {}
            goal, effective_scope = clean(goal), validate_scope(clean(effective_scope))
            # 更新目标时未传状态不能默默重新开启已取消或已交付任务。
            effective_status = "active" if resume else status if status is not None else (
                previous["status"] if previous and not new_task else "active")
            if previous and not new_task and previous["status"] != "active" and effective_status == "active" and not resume:
                raise ContractError("resume_required: explicitly resume the inactive task")
            if previous and not new_task and (previous["goal"], previous["scope"], previous["status"]) == (
                    goal, effective_scope, effective_status):
                return previous

            changed_contract = not previous or new_task or previous["goal"] != goal or previous["scope"] != effective_scope
            next_revision = 1 if new_task or not previous else revision + int(changed_contract)
            task_id = previous["id"] if previous and not new_task else str(uuid.uuid4())
            next_write_version = write_version + 1
            fence = changed_contract or resume or (previous and previous["status"] != effective_status and effective_status != "active")
            execution_epoch = previous["execution_epoch"] + int(bool(fence)) if previous else 1
            now = time.time()
            self.db.execute(
                "INSERT INTO tasks(id,project,conversation,revision,goal,scope,status,updated,write_version,execution_epoch) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project,conversation) DO UPDATE SET "
                "id=excluded.id,revision=excluded.revision,goal=excluded.goal,scope=excluded.scope,status=excluded.status,"
                "updated=excluded.updated,write_version=excluded.write_version,execution_epoch=excluded.execution_epoch",
                (task_id, project, conversation_id, next_revision, goal, encode(effective_scope), effective_status,
                 now, next_write_version, execution_epoch))
            # 合同快照只在首次形成时写入；状态变化另留精确历史。
            self.db.execute("INSERT OR IGNORE INTO task_snapshots VALUES(?,?,?,?,?,?,?,?)",
                            (project, task_id, next_revision, conversation_id, goal, encode(effective_scope), effective_status, now))
            self.db.execute("INSERT INTO task_states(project,id,revision,write_version,conversation,status,execution_epoch,updated) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            (project, task_id, next_revision, next_write_version, conversation_id, effective_status,
                             execution_epoch, now))
            self._event(project, conversation_id, "TaskUpdate",
                        {"goal": goal, "scope": effective_scope, "status": effective_status,
                         "write_version": next_write_version, "execution_epoch": execution_epoch},
                        task_id=task_id, task_revision=next_revision)
            if changed_contract:
                for table in ("contexts", "subagent_contexts"):
                    self.db.execute(f"UPDATE {table} SET epoch=epoch+1,injected='[]',last_query='',retrieval_signature='',retrieval_refs='[]' WHERE project=? AND conversation=?",
                                    (project, conversation_id))
            # 在事务内取得回执，避免提交后被并发写入替换成另一项任务。
            return self.task(project, conversation_id)

    def task_snapshot(self, project, task_id, revision, write_version=None):
        from .runtime import clean

        row = self.db.execute("SELECT * FROM task_snapshots WHERE project=? AND id=? AND revision=?",
                              (project, task_id, revision)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["scope"] = json.loads(result["scope"])
        if write_version is not None:
            state = self.db.execute("SELECT * FROM task_states WHERE project=? AND id=? AND revision=? AND write_version=?",
                                    (project, task_id, revision, write_version)).fetchone()
            if not state:
                return None
            for key in ("status", "write_version", "execution_epoch", "updated"):
                result[key] = state[key]
        return clean(result)
