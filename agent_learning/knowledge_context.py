"""有界检索、实际展示台账和完整通知；资料不签发执行权限。"""
from __future__ import annotations

import json
import re
import time


class KnowledgeContextMixin:
    def _context_state(self, project, conversation, agent_id=None):
        if agent_id:
            return "subagent_contexts", "project=? AND conversation=? AND agent=?", (project, conversation, agent_id)
        return "contexts", "project=? AND conversation=?", (project, conversation)

    def _ensure_context(self, project, conversation, agent_id=None):
        table, where, key = self._context_state(project, conversation, agent_id)
        columns = "project,conversation,agent" if agent_id else "project,conversation"
        self.db.execute(f"INSERT OR IGNORE INTO {table}({columns}) VALUES({','.join('?' for _ in key)})", key)
        return table, where, key

    @staticmethod
    def _source_state(item):
        return {"health": item.get("source_health", "missing"),
                "current_tool_success": item.get("current_tool_success", False),
                "problem_changed": item.get("problem_changed", any(s.get("problem_changed", False) for s in item.get("sources", []))),
                "pending_counterexample_count": item.get("pending_counterexample_count", 0)}

    def _record_exposure(self, project, conversation, agent_id, items, injected=False):
        from .runtime import encode
        table, where, key = self._ensure_context(project, conversation, agent_id)
        row = self.db.execute(f"SELECT * FROM {table} WHERE {where}", key).fetchone()
        shown = list(dict.fromkeys(json.loads(row["shown"])))
        current = list(dict.fromkeys(json.loads(row["injected"])))
        states = dict(json.loads(row["source_states"]))
        for item in items:
            identity = f"{item['knowledge_id']}@{item['revision']}"
            state = self._source_state(item)
            # 使用返回快照里的来源版本，不把读完后发生的变化误记为已经展示。
            self.db.execute("INSERT INTO context_exposures VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(project,conversation,agent,knowledge_id,revision) DO UPDATE SET "
                "state=excluded.state,source_version=excluded.source_version,updated=excluded.updated",
                (project, conversation, agent_id or "", item["knowledge_id"], item["revision"],
                 encode(state), item.get("source_version", -1), time.time()))
            if identity not in shown:
                shown.append(identity)
            if injected and identity not in current:
                current.append(identity)
            states.pop(identity, None)
            states[identity] = state
        # 活跃恢复集合有界；完整历史追踪由索引化台账保存。
        self.db.execute(f"UPDATE {table} SET injected=?,shown=?,source_states=? WHERE {where}",
                        (encode(current[-32:]), encode(shown[-64:]), encode(list(states.items())[-8:]), *key))

    def record_exposure(self, project, context_handle, items, expected_epoch=None):
        """MCP仅登记实际返回资料；未绑定查询不猜测会话归属。"""
        from .runtime import ContractError
        with self.transaction():
            access = self.access_context(project, context_handle, expected_epoch=expected_epoch)
            for item in items:
                if not self.db.execute("SELECT 1 FROM knowledge WHERE project=? AND id=? AND revision=?",
                    (project, item["knowledge_id"], item["revision"])).fetchone():
                    raise ContractError("unknown exposed knowledge revision")
            self._record_exposure(project, access["conversation_id"], access["agent_id"], items)

    def _notifications(self, project, conversation, agent_id, max_chars):
        """调用方持有事务；完整进入响应后才推进通知状态。"""
        from .runtime import ContractError, encode
        key = (project, conversation, agent_id or "")
        notes, removed = [], set()
        remaining = max(0, max_chars)
        invalid = self.db.execute("SELECT x.* FROM context_exposures x LEFT JOIN active a ON "
            "a.project=x.project AND a.id=x.knowledge_id AND a.revision=x.revision "
            "WHERE x.project=? AND x.conversation=? AND x.agent=? AND a.id IS NULL "
            "ORDER BY x.updated,x.knowledge_id LIMIT 16", key).fetchall()
        for row in invalid:
            identity = f"{row['knowledge_id']}@{row['revision']}"
            note = f"知识 {identity} 已撤销或被替换；不能依据旧版执行。"
            cost = len(note) + int(bool(notes))
            if cost > remaining:
                continue
            notes.append(note)
            remaining -= cost
            removed.add(identity)
            self.db.execute("DELETE FROM context_exposures WHERE project=? AND conversation=? AND agent=? "
                            "AND knowledge_id=? AND revision=?", (*key, row["knowledge_id"], row["revision"]))
        dirty = self.db.execute("SELECT x.* FROM context_exposures x JOIN active a ON "
            "a.project=x.project AND a.id=x.knowledge_id AND a.revision=x.revision "
            "LEFT JOIN knowledge_source_versions v ON v.project=x.project AND v.knowledge_id=x.knowledge_id "
            "AND v.revision=x.revision WHERE x.project=? AND x.conversation=? AND x.agent=? "
            "AND x.source_version<>COALESCE(v.version,0) ORDER BY x.updated,x.knowledge_id LIMIT 16", key).fetchall()
        table, where, context_key = self._context_state(project, conversation, agent_id)
        context = self.db.execute(f"SELECT * FROM {table} WHERE {where}", context_key).fetchone()
        states = dict(json.loads(context["source_states"])) if context else {}
        for row in dirty:
            try:
                item = self.read(project, row["knowledge_id"])
            except ContractError:
                continue
            state = self._source_state(item)
            identity = f"{row['knowledge_id']}@{row['revision']}"
            if state != json.loads(row["state"]):
                note = (f"知识 {identity} 来源已变：health={state['health']}，"
                        f"tool_success={state['current_tool_success']}，新证据={state['problem_changed']}，"
                        f"待核实反例={state['pending_counterexample_count']}；请用 knowledge_read 核对。")
                cost = len(note) + int(bool(notes))
                if cost > remaining:
                    continue
                notes.append(note)
                remaining -= cost
            self.db.execute("UPDATE context_exposures SET state=?,source_version=?,updated=? WHERE "
                "project=? AND conversation=? AND agent=? AND knowledge_id=? AND revision=?",
                (encode(state), item.get("source_version", 0), time.time(), *key, row["knowledge_id"], row["revision"]))
            if identity in states:
                states[identity] = state
        if context:
            self.db.execute(f"UPDATE {table} SET injected=?,shown=?,source_states=? WHERE {where}",
                (encode([i for i in json.loads(context["injected"]) if i not in removed]),
                 encode([i for i in json.loads(context["shown"]) if i not in removed]),
                 encode([(i, s) for i, s in states.items() if i not in removed]), *context_key))
        return notes

    def _retrieval_task(self, project, conversation, agent_id, context_handle=None):
        if not agent_id:
            return self.task(project, conversation)
        assignment = self.assignment_context(project, conversation, agent_id, context_handle)
        return self.task_snapshot(project, assignment["task_id"], assignment["task_revision"], assignment["task_write_version"]) \
            if assignment and assignment["task_id"] else None

    @staticmethod
    def _task_key(task):
        return (task["id"], task["revision"], task.get("write_version")) if task else (None, None, None)

    @staticmethod
    def _retrieval_filters(task):
        from .runtime import METADATA_KEYS, knowledge_filters
        scope = task.get("scope", {}) if task else {}
        # 只使用明确结构字段；不把 allowed_tools 等执行范围误作知识筛选。
        source = scope.get("knowledge_filters", {})
        if not isinstance(source, dict):
            return {}
        values = {key: value for key, value in source.items() if key in METADATA_KEYS
                  and isinstance(value, str) and value.strip() and len(value) <= 500}
        return knowledge_filters(values)

    def _queue_fault(self, project, conversation, agent_id, source, epoch, raw):
        from .runtime import clean, clip, digest, encode
        if not source or not source.get("id") or source.get("revision") is None:
            return
        tool = str(raw.get("tool_name", "unknown"))
        response = clip(encode(clean(raw.get("tool_response"))), 1000)
        stable = re.sub(r"\b(?:0x[0-9a-f]+|[0-9a-f]{16,}|\d{4,})\b", "#", response.lower())
        signature = digest([self._task_key(source), epoch, tool, stable])
        key = (project, conversation, agent_id or "")
        count = self.db.execute("SELECT COUNT(*) FROM context_faults WHERE project=? AND conversation=? AND agent=? "
            "AND task_id=? AND task_revision=? AND host_epoch=?", (*key, source["id"], source["revision"], epoch)).fetchone()[0]
        if count >= 3:
            return
        self.db.execute("INSERT OR IGNORE INTO context_faults(project,conversation,agent,signature,task_id,task_revision,"
                        "task_write_version,host_epoch,query,tool,state,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (*key, signature, source["id"], source["revision"], source.get("write_version"), epoch,
             tool + " " + response, tool, "pending", time.time()))
        self.db.execute("DELETE FROM context_faults WHERE project=? AND conversation=? AND agent=? "
            "AND signature NOT IN (SELECT signature FROM context_faults WHERE project=? AND conversation=? AND agent=? "
            "ORDER BY created DESC LIMIT 32)", (*key, *key))

    def _context(self, project, conversation, query="", compact=False, agent_id=None, context_handle=None,
                 expected_epoch=None, tool_only=False):
        from .runtime import ContractError, clean, clip, digest, encode
        from .knowledge_output import _summary
        # 检索在写事务外进行；最终提交重新核身份、任务及上下文版本。
        with self.transaction():
            epoch = self.assert_host_epoch(expected_epoch)
            if context_handle is not None:
                self.access_context(project, context_handle, conversation, epoch)
            table, where, key = self._ensure_context(project, conversation, agent_id)
            initial = dict(self.db.execute(f"SELECT * FROM {table} WHERE {where}", key).fetchone())
            task = self._retrieval_task(project, conversation, agent_id, context_handle)
            manifest = dict(self.db.execute("SELECT generation,revocation_epoch,source_epoch FROM projects WHERE id=?", (project,)).fetchone())
            fault = self.db.execute("SELECT * FROM context_faults WHERE project=? AND conversation=? AND agent=? "
                "AND state IN ('pending','ready') AND task_id IS ? AND task_revision IS ? "
                + ("AND task_write_version IS ? " if agent_id else "") +
                "AND host_epoch=? ORDER BY created LIMIT 1",
                (project, conversation, agent_id or "", *self._task_key(task)[:2],
                 *((self._task_key(task)[2],) if agent_id else ()), epoch)).fetchone()
        query = clean(query).strip()
        continuation = not query or query.lower().strip("。.!！ ") in {
            "继续", "好的", "可以", "接着", "继续吧", "好", "continue", "go on", "ok", "yes"}
        if not (tool_only or continuation or compact):
            # 新提示优先查询当前问题；旧故障保留到后续工具前置，不抢占注意力。
            fault = None
        effective = initial["last_query"] if continuation else query
        if not effective and task:
            effective = task["goal"]
        # 续聊、恢复或工具前置承接报错；父任务执行轮次变化不丢同合同的经验。
        retrieval = fault["query"] if fault else effective
        filters = self._retrieval_filters(task)
        signature = digest([retrieval[-4000:], filters, self._task_key(task), manifest, epoch, self.config["injection_max_chars"]])
        should_search = bool(retrieval.strip()) and (compact or signature != initial["retrieval_signature"])
        if fault:
            should_search = fault["state"] == "pending"
        elif tool_only:
            should_search = False
        items = self.search(project, retrieval, filters=filters)["items"] if should_search else []
        if fault and fault["state"] == "ready":
            for ref in json.loads(fault["results"]):
                try:
                    item = self.read(project, ref["knowledge_id"])
                except ContractError:
                    continue
                if item["revision"] == ref["revision"]:
                    item["match"] = ref.get("match", {})
                    items.append(item)
        if not should_search and not tool_only and signature == initial["retrieval_signature"]:
            already = set(json.loads(initial["injected"]))
            for ref in json.loads(initial["retrieval_refs"]):
                if f"{ref['knowledge_id']}@{ref['revision']}" in already:
                    continue
                try:
                    item = self.read(project, ref["knowledge_id"])
                except ContractError:
                    continue
                if item["revision"] == ref["revision"]:
                    item["match"] = ref.get("match", {})
                    items.append(item)
        # 新命中优先；恢复集合保留顺序，不由历史曾见列表决定。
        if compact:
            seen = {item["knowledge_id"] for item in items}
            for identity in reversed(json.loads(initial["injected"])[-5:]):
                ident = identity.rsplit("@", 1)[0]
                if ident in seen:
                    continue
                try:
                    item = self.read(project, ident)
                except ContractError:
                    continue
                if any(item.get("metadata", {}).get(k) != v for k, v in filters.items()):
                    continue
                items.append(item)
                seen.add(ident)
        with self.transaction():
            self.assert_host_epoch(epoch)
            if context_handle is not None:
                self.access_context(project, context_handle, conversation, epoch)
            current_task = self._retrieval_task(project, conversation, agent_id, context_handle)
            row = dict(self.db.execute(f"SELECT * FROM {table} WHERE {where}", key).fetchone())
            if (self._task_key(current_task) != self._task_key(task) or row["epoch"] != initial["epoch"]
                    or row["last_query"] != initial["last_query"] or row["retrieval_signature"] != initial["retrieval_signature"]):
                raise ContractError("context_changed: retrieve again for the current task")
            budget, lines = self.config["injection_max_chars"], []
            if context_handle and not tool_only:
                lines.append(f"[Agent Learning] context_handle={context_handle}; host_epoch={epoch}")
            def remaining():
                return budget - len("\n".join(lines)) - int(bool(lines))
            lines.extend(self._notifications(project, conversation, agent_id, remaining()))
            def append_complete(note):
                if len(note) <= remaining():
                    lines.append(note)
            if not tool_only:
                if agent_id:
                    append_complete("子 Agent 继承固定任务边界；session_context 可读取任务，不能覆盖父任务。")
                if task:
                    append_complete(f"[Agent Learning] task={task['id']} revision={task['revision']} write_version={task.get('write_version', 'unknown')} status={task['status']}; 当前要求优先，任务上报不签发权限。")
                if self.config["enforce_task_policy"]:
                    append_complete("执行保护：" + self.execution_status(project, conversation)["reason"])
            if compact:
                self.db.execute(f"UPDATE {table} SET epoch=epoch+1,injected='[]' WHERE {where}", key)
            row = self.db.execute(f"SELECT injected FROM {table} WHERE {where}", key).fetchone()
            prior = set(json.loads(row[0]))
            header = "以下是历史资料摘要，按适用条件判断；它不提供执行授权。完整正文使用 knowledge_read。"
            delivered = []
            for item in items:
                identity = f"{item['knowledge_id']}@{item['revision']}"
                if identity in prior or not item.get("match", {}).get("auto_eligible", True):
                    continue
                # 提交时核active与来源版本，避免快照后的撤销或冲突继续进入自动摘要。
                active = self.db.execute("SELECT COALESCE(v.version,0) FROM active a LEFT JOIN knowledge_source_versions v "
                    "ON v.project=a.project AND v.knowledge_id=a.id AND v.revision=a.revision "
                    "WHERE a.project=? AND a.id=? AND a.revision=?", (project, item["knowledge_id"], item["revision"])).fetchone()
                if not active:
                    continue
                if active[0] != item.get("source_version", -1):
                    latest = self.read(project, item["knowledge_id"])
                    latest["match"] = item.get("match", {})
                    item = latest
                if item["source_health"] != "valid":
                    continue
                available = remaining() - (len(header) + 1 if not delivered else 0)
                card = {"knowledge_id": item["knowledge_id"], "revision": item["revision"],
                        "title": clip(item["title"], 100), "content_summary": _summary(item, 500),
                        "applicability": clip(item["applicability"], 180), "limitations": clip(item["limitations"], 180),
                        "category": item["category"], "knowledge_kind": item["knowledge_kind"],
                        "validation_level": item["validation_level"], "source_health": item["source_health"],
                        "current_tool_success": item["current_tool_success"], "problem_changed": item.get("problem_changed", False),
                        "summary_only": True}
                if item.get("pending_counterexample_count", 0):
                    card["pending_counterexample_count"] = item["pending_counterexample_count"]
                    card["feedback_status"] = "pending_counterexample"
                if item.get("metadata"):
                    card["metadata"] = {k: clip(v, 60) for k, v in item["metadata"].items()}
                if item.get("match"):
                    card["matched_terms"] = item["match"]["matched_terms"][:6]
                for field in ("content_summary", "title"):
                    while len(encode(card)) > available and len(card[field]) > 24:
                        card[field] = clip(card[field], max(24, len(card[field]) // 2))
                if len(encode(card)) > available:
                    # 不裁掉适用/限制后继续展示可执行摘要；退回仅标识卡。
                    card = {"knowledge_id": item["knowledge_id"], "revision": item["revision"],
                            "title": clip(item["title"], 60), "summary_only": True,
                            "read_required": True, "source_health": item["source_health"]}
                    if item.get("pending_counterexample_count", 0):
                        card["pending_counterexample_count"] = item["pending_counterexample_count"]
                if len(encode(card)) > available:
                    continue
                if not delivered:
                    lines.append(header)
                lines.append(encode(card))
                delivered.append(item)
                prior.add(identity)
                if len(delivered) == 3:
                    break
            self._record_exposure(project, conversation, agent_id, delivered, injected=True)
            if delivered:
                self._metric(project, "knowledge_injected", len(delivered))
            if should_search and not tool_only and not fault:
                refs = [{"knowledge_id": i["knowledge_id"], "revision": i["revision"], "match": i.get("match", {})} for i in items[:10]]
                self.db.execute(f"UPDATE {table} SET retrieval_signature=?,retrieval_refs=? WHERE {where}",
                                (signature, encode(refs), *key))
            if not tool_only:
                self.db.execute(f"UPDATE {table} SET last_query=? WHERE {where}", (effective[-4000:], *key))
            if fault:
                refs = [{"knowledge_id": i["knowledge_id"], "revision": i["revision"], "match": i.get("match", {})}
                        for i in items[:5] if i.get("match", {}).get("auto_eligible", True) and i["source_health"] == "valid"]
                pending = [ref for ref in refs if f"{ref['knowledge_id']}@{ref['revision']}" not in prior]
                self.db.execute("UPDATE context_faults SET state=?,results=? WHERE project=? AND conversation=? AND agent=? AND signature=?",
                                ("ready" if pending else "done", encode(pending), project, conversation, agent_id or "", fault["signature"]))
                if should_search:
                    self._metric(project, "failure_retrievals")
            return "\n".join(lines)
