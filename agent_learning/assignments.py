"""子 Agent 的任务归属固定于派发时；身份和任务版本分别维护。"""
from __future__ import annotations

import json
import time
import uuid


class AssignmentMixin:
    def _bind_assignment(self, project, conversation, agent, handle):
        from .runtime import encode
        old = self.db.execute("SELECT id FROM assignments WHERE context_handle=?", (handle,)).fetchone()
        if not old:
            task = self.task(project, conversation)
            self.db.execute(
                "INSERT INTO assignments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), project, conversation, agent, handle,
                 task["id"] if task else None, task["revision"] if task else None,
                 task["write_version"] if task else None, task["execution_epoch"] if task else None,
                 self.session_epoch(), encode(task["scope"] if task else {}), time.time()))
        return self.assignment_context(project, conversation, agent, handle)

    def assignment_context(self, project, conversation, agent_id=None, context_handle=None):
        if context_handle:
            row = self.db.execute(
                "SELECT * FROM assignments WHERE project=? AND conversation=? AND context_handle=? AND host_epoch=?",
                (project, conversation, context_handle, self.session_epoch())).fetchone()
        elif agent_id:
            row = self.db.execute(
                "SELECT * FROM assignments WHERE project=? AND conversation=? AND agent=? AND host_epoch=? "
                "ORDER BY created DESC LIMIT 1", (project, conversation, agent_id, self.session_epoch())).fetchone()
        else:
            return None
        if not row:
            return None
        result = dict(row)
        result["scope"] = json.loads(result["scope"])
        current = self.task(project, conversation)
        result["assignment_id"] = result["id"]
        result["current"] = bool(current and (current["id"], current["revision"], current["execution_epoch"]) ==
                                 (result["task_id"], result["task_revision"], result["execution_epoch"]))
        return result

    def hook_binding(self, project, conversation, agent_id):
        assigned = self.assignment_context(project, conversation, agent_id)
        if not assigned or not assigned["task_id"]:
            return None
        return {"id": assigned["task_id"], "revision": assigned["task_revision"],
                "write_version": assigned["task_write_version"], "execution_epoch": assigned["execution_epoch"],
                "assignment_id": assigned["assignment_id"]}
