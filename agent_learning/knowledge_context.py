"""有界检索、实际展示台账和完整通知；资料不签发执行权限。"""
from __future__ import annotations

import json
import re
import sqlite3
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
                "maturity": item.get("maturity", "candidate"),
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
        # 保存准确任务下的有限展示身份，不把展示推断为知识采用。
        self.record_task_exposure(project, conversation, agent_id, items,
                                  method="hook" if injected else "mcp")

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
                if state["pending_counterexample_count"]:
                    note += "存在未解决反例，已暂停自动注入；不能继续依赖旧结论，需治理确认。"
                if state["maturity"] != json.loads(row["state"]).get("maturity", "candidate"):
                    note = note.replace("；请用", f"，成熟度={state['maturity']}；请用")
                cost = len(note) + int(bool(notes))
                if cost > remaining:
                    # 极小预算也交付完整的ID和复查要求，不裁断后确认送达。
                    note = (f"知识 {identity} 状态已变（{state['health']}/{state['maturity']}，"
                            f"反例{state['pending_counterexample_count']}）；执行前用 knowledge_read 核对。")
                    if state["pending_counterexample_count"]:
                        note = f"知识 {identity} 有未解决反例；暂停依赖旧结论，knowledge_read 复核。"
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

    def _work_task_state(self, project, conversation, agent_id, context_handle, task):
        """工作索引的去重范围包括固定派发身份，不能猜同名子 Agent 的边界。"""
        state = ({"id": task["id"], "revision": task["revision"],
                  "write_version": task.get("write_version")} if task else None)
        if agent_id:
            assignment = self.assignment_context(project, conversation, agent_id, context_handle)
            if assignment:
                state = dict(state or {}, assignment_id=assignment["id"])
        return state

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
                 expected_epoch=None, tool_only=False, delivery="persistent", _phase_guard=None, _outcome=None,
                 workspace=None, raw=None, _source_event_id=None):
        if delivery not in {"persistent", "ephemeral"}:
            from .runtime import ContractError
            raise ContractError("unsupported context delivery")
        from .runtime import ContractError, clean, clip, digest, encode
        if _outcome is not None and not isinstance(_outcome, dict):
            raise ContractError("context outcome must be a dictionary")
        from .knowledge_index import normalized_query_plan, _MAX_QUERY_CHARS
        from .knowledge_output import _summary
        # 检索在写事务外进行；最终提交重新核身份、任务及上下文版本。
        with self.transaction():
            epoch = self.assert_session_epoch(expected_epoch)
            if _phase_guard:
                _phase_guard()
            if context_handle is not None:
                self.access_context(project, context_handle, conversation, epoch)
            table, where, key = self._ensure_context(project, conversation, agent_id)
            initial = dict(self.db.execute(f"SELECT * FROM {table} WHERE {where}", key).fetchone())
            task = self._retrieval_task(project, conversation, agent_id, context_handle)
            task_state = self._work_task_state(project, conversation, agent_id, context_handle, task)
            manifest = dict(self.db.execute("SELECT generation,revocation_epoch,source_epoch FROM projects WHERE id=?", (project,)).fetchone())
            fault = self.db.execute("SELECT * FROM context_faults WHERE project=? AND conversation=? AND agent=? "
                "AND state IN ('pending','ready') AND task_id IS ? AND task_revision IS ? "
                + ("AND task_write_version IS ? " if agent_id else "") +
                "AND host_epoch=? ORDER BY created LIMIT 1",
                (project, conversation, agent_id or "", *self._task_key(task)[:2],
                 *((self._task_key(task)[2],) if agent_id else ()), epoch)).fetchone()
        query = clean(query).strip()
        def continuation_query(value):
            return not value or value.lower().strip("。.!！ ") in {
                "继续", "好的", "可以", "接着", "继续吧", "好", "确认", "确定", "同意",
                "continue", "go on", "ok", "yes", "confirm", "confirmed"}
        continuation = continuation_query(query)
        if not (tool_only or continuation or compact):
            # 新提示优先查询当前问题；旧故障保留到后续工具前置，不抢占注意力。
            fault = None
        # Older contexts may already contain an acknowledgement as last_query.
        # Recover the current task focus instead of recalling that generic word.
        effective = (initial["last_query"] if not continuation_query(initial["last_query"]) else "") if continuation else query
        if compact and effective:
            # 恢复当前讨论焦点；旧故障继续排队到工具前置，不能抢占当前
            # 查询后再通过历史展示 ID 补回一批未经相关度核对的旧卡。
            fault = None
        if not effective and task:
            effective = task["goal"]
        # 续聊、恢复或工具前置承接报错；父任务执行轮次变化不丢同合同的经验。
        retrieval = fault["query"] if fault else effective
        effective_plan = normalized_query_plan(effective)
        filters = self._retrieval_filters(task)
        from .knowledge_applicability import project_environment, automatic_applicability
        environment = project_environment(workspace)
        semantic_signature = self.semantic_signature(project)
        # 预算变化只影响待发卡容纳，不必重新召回；知识、来源、模型与任务变化需重查。
        fault_result_signature = digest([normalized_query_plan(retrieval), filters, self._task_key(task),
                                         manifest, epoch, semantic_signature, environment])
        signature = digest([normalized_query_plan(retrieval), filters, self._task_key(task), manifest,
                            epoch, self.config["injection_max_chars"], semantic_signature, environment])
        # 跨短时Hook进程保留重试冷却，不将临时嵌入失败永久缓存成无结果。
        previous_signature = initial["retrieval_signature"]
        retry_at = 0.0
        if previous_signature.startswith("retry:"):
            parts = previous_signature.split(":", 2)
            if len(parts) == 3:
                try:
                    retry_at = float(parts[1])
                    previous_signature = parts[2]
                except ValueError:
                    pass
        cooling = signature == previous_signature and retry_at > time.time()
        should_search = bool(retrieval.strip()) and not cooling and (
            compact or signature != previous_signature or bool(retry_at))
        fault_refs = json.loads(fault["results"]) if fault else []
        if fault:
            stale_results = any(ref.get("retrieval_signature") != fault_result_signature for ref in fault_refs)
            should_search = (fault["state"] == "pending" or stale_results) and not cooling
        elif tool_only:
            should_search = False
        search_result = self.search(project, retrieval, filters=filters, _automatic=True,
                                    _environment=environment) if should_search else {}
        items = search_result.get("items", [])
        retryable = bool(search_result.get("retrieval", {}).get("retryable", False))
        if fault and fault["state"] == "ready" and not should_search:
            for ref in fault_refs:
                try:
                    item = self.read(project, ref["knowledge_id"])
                except ContractError:
                    continue
                if item["revision"] == ref["revision"]:
                    item["match"] = ref.get("match", {})
                    items.append(item)
        if not should_search and not tool_only and signature == previous_signature:
            already = set(json.loads(initial["injected"]))
            for ref in json.loads(initial["retrieval_refs"]):
                if delivery == "persistent" and f"{ref['knowledge_id']}@{ref['revision']}" in already:
                    continue
                try:
                    item = self.read(project, ref["knowledge_id"])
                except ContractError:
                    continue
                if item["revision"] == ref["revision"]:
                    item["match"] = ref.get("match", {})
                    items.append(item)
        # 压缩后的恢复也依据当前有效查询重新召回。历史展示身份只用于
        # 失效通知，不能绕过相关度门槛把无关旧卡重新塞回当前上下文。
        work_settings = self.config.get("work_memory", {})
        work_items = []
        candidate_items = []
        candidate_context_failed = False
        work_context_failed = False
        work_context_epoch = initial["epoch"] + int(compact)
        if (work_settings.get("enabled", True) and work_settings.get("automatic_context", True)
                and work_settings.get("max_context_chars", 600)):
            # 文件触发独立于故障队列；工作历史只给索引，不能转成解法或执行许可。
            # 在写事务外召回，最终只登记完整进入共享预算的卡片。
            work_query = ("" if tool_only or (self.host == "antigravity" and delivery == "ephemeral"
                                              and not query.strip()) else effective)
            try:
                if effective_plan["automatic_safe"] or not work_query or (compact and not tool_only):
                    work_items = self.work_context_items(
                        project, conversation, query=work_query, workspace=workspace, raw=raw,
                        agent_id=agent_id, limit=work_settings.get("max_items", 3),
                        task_state=task_state, delivery=delivery, compact=compact,
                        context_epoch=work_context_epoch, file_only=tool_only,
                        exclude_event_id=_source_event_id,
                        handoff_only=bool(work_query and not effective_plan["automatic_safe"]))
                else:
                    # Safety/expiry notices stay independent of optional recall.
                    work_items = self.work_exposure_updates(project, conversation, agent_id, workspace=workspace)
            except ContractError:
                # 身份与任务契约失败仍必须由主上下文拒绝，不能当作派生索引故障。
                raise
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
                work_context_failed = True
        if (context_handle and not tool_only and effective.strip() and (not agent_id or task)
                and effective_plan["automatic_safe"] and len(effective) <= 30000):
            from .candidate_references import search as candidate_search
            try:
                if _phase_guard:
                    _phase_guard()
                found = candidate_search(self, project, effective, limit=1, context_handle=context_handle,
                                         delivery=delivery, context_epoch=work_context_epoch, task_state=task_state,
                                         compact=compact, filters=filters, workspace=workspace)
                candidate_items = found.get("items", []) if isinstance(found, dict) else found
            except ContractError:
                raise
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
                candidate_context_failed = True
        if project_environment(workspace) != environment:
            raise ContractError("context_changed: project version changed during retrieval")
        with self.transaction():
            self.assert_session_epoch(epoch)
            if _phase_guard:
                _phase_guard()
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
            if context_handle:
                from .candidate_references import notifications, ack_notifications
                self.db.execute("SAVEPOINT candidate_notifications")
                notice_start = len(lines)
                try:
                    complete_notices = []
                    for notice in notifications(self, project, context_handle, workspace=workspace):
                        text = notice["text"]
                        if len(text) <= remaining():
                            lines.append(text)
                            complete_notices.append(notice)
                    if complete_notices:
                        ack_notifications(self, project, context_handle, complete_notices, workspace=workspace)
                except ContractError:
                    self.db.execute("ROLLBACK TO candidate_notifications")
                    self.db.execute("RELEASE candidate_notifications")
                    raise
                except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
                    self.db.execute("ROLLBACK TO candidate_notifications")
                    del lines[notice_start:]
                    candidate_context_failed = True
                    self.db.execute("RELEASE candidate_notifications")
                else:
                    self.db.execute("RELEASE candidate_notifications")
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
            from .context_dedup import ContextDedup
            dedup = ContextDedup(self, project, conversation, agent_id, workspace, task_state,
                                 work_context_epoch, delivery, environment)
            header = "以下是历史资料摘要，按适用条件判断；它不提供执行授权。完整正文使用 knowledge_read。"
            delivered, eligible = [], 0
            for item in items:
                identity = f"{item['knowledge_id']}@{item['revision']}"
                if (delivery == "persistent" and identity in prior) or not item.get("match", {}).get("auto_eligible", False):
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
                if item["source_health"] != "valid" or item.get("pending_counterexample_count", 0):
                    continue
                if automatic_applicability(item, environment)["status"] == "incompatible":
                    continue
                # 防线：若某条正式知识的来源案例正属于当前同一个会话且案例已发生新变化（正在继续修改/重修中），
                # 暂停将其作为定型正式知识反哺注入当前会话，防止模型拿上一轮的半成品当佐证自我锚定。
                evolving_in_current_session = False
                if item.get("problem_changed"):
                    for src in item.get("sources", []):
                        if src.get("problem_changed") and src.get("problem_id"):
                            prob_conv = self.db.execute(
                                "SELECT conversation,status FROM problems WHERE project=? AND id=?",
                                (project, src["problem_id"]),
                            ).fetchone()
                            if prob_conv and prob_conv["conversation"] == conversation and prob_conv["status"] == "pending":
                                evolving_in_current_session = True
                                break
                if evolving_in_current_session:
                    continue
                from .work_files import knowledge_file_state
                file_state = knowledge_file_state(self, project, item, workspace)
                eligible += 1
                available = remaining() - (len(header) + 1 if not delivered else 0)
                card = {"knowledge_id": item["knowledge_id"], "revision": item["revision"],
                        "title": clip(item["title"], 100), "content_summary": _summary(item, 500),
                        "applicability": item["applicability"], "limitations": item["limitations"],
                        "category": item["category"], "knowledge_kind": item["knowledge_kind"],
                        "validation_level": item["validation_level"], "source_health": item["source_health"],
                        "knowledge_tier": item.get("knowledge_tier", "source_reference"),
                        "maturity": item.get("maturity", "candidate"),
                        "current_tool_success": item["current_tool_success"], "problem_changed": item.get("problem_changed", False),
                        "summary_only": True}
                card.update(file_state)
                if item.get("pending_counterexample_count", 0):
                    card["pending_counterexample_count"] = item["pending_counterexample_count"]
                    card["feedback_status"] = "pending_counterexample"
                if item.get("metadata"):
                    card["metadata"] = {k: clip(v, 60) for k, v in item["metadata"].items()}
                if item.get("match"):
                    card["matched_terms"] = item["match"]["matched_terms"][:6]
                    if item["match"].get("method") in {"semantic", "hybrid"}:
                        card["retrieval_method"] = item["match"]["method"]
                        card["semantic_score"] = item["match"].get("semantic_score")
                for field in ("content_summary", "title"):
                    while len(encode(card)) > available and len(card[field]) > 24:
                        card[field] = clip(card[field], max(24, len(card[field]) // 2))
                if len(encode(card)) > available:
                    # 不裁掉适用/限制后继续展示可执行摘要；退回仅标识卡。
                    card = {"knowledge_id": item["knowledge_id"], "revision": item["revision"],
                            "title": clip(item["title"], 60), "summary_only": True,
                            "read_required": True, "source_health": item["source_health"],
                            "knowledge_tier": item.get("knowledge_tier", "source_reference"),
                            "maturity": item.get("maturity", "candidate")}
                    card.update(file_state)
                    if item.get("pending_counterexample_count", 0):
                        card["pending_counterexample_count"] = item["pending_counterexample_count"]
                    # Tiny budgets retain identity, source risk and the evidence
                    # tier before optional display labels. Never truncate JSON.
                    for optional in ("title", "maturity"):
                        if len(encode(card)) > available:
                            card.pop(optional, None)
                if len(encode(card)) > available:
                    continue
                delivery_entry = dedup.prepare("formal", item, card)
                if dedup.repeated(delivery_entry):
                    # An equivalent card is already present. Do not fabricate
                    # an exposure/adoption for the hidden alias or leave a fault
                    # retry pending solely because that alias was omitted.
                    prior.add(identity)
                    eligible -= 1
                    continue
                if not delivered:
                    lines.append(header)
                lines.append(encode(card))
                delivered.append(item)
                dedup.record(delivery_entry)
                prior.add(identity)
                if len(delivered) == 3:
                    break
            new_delivered = [item for item in delivered
                             if f"{item['knowledge_id']}@{item['revision']}" not in json.loads(row[0])]
            self._record_exposure(project, conversation, agent_id, delivered, injected=True)
            # Candidate references and work history use one optional pool. A
            # candidate fault must not remove formal cards or task boundaries.
            optional_budget = min(max(0, remaining()), work_settings.get("max_context_chars", 600))
            candidate_delivered, candidate_used = [], 0
            if candidate_items and context_handle:
                from .candidate_references import revalidate, record_exposure
                from .candidate_reference_context import card
                self.db.execute("SAVEPOINT optional_candidate_context")
                candidate_start = len(lines)
                candidate_coverage = list(dedup.entries)
                candidate_pending = list(dedup.pending)
                try:
                    for selected in candidate_items[:1]:
                        item = revalidate(self, project, selected, context_handle=context_handle, workspace=workspace)
                        if not item:
                            continue
                        note = card(item, max_chars=min(400, optional_budget, max(0, remaining())))
                        if note is None:
                            continue
                        candidate_card = json.loads(note[note.index("{"):])
                        delivery_entry = dedup.prepare("candidate", item, candidate_card)
                        if dedup.repeated(delivery_entry):
                            continue
                        if not record_exposure(self, project, context_handle, item, delivery=delivery,
                                               context_epoch=work_context_epoch, task_state=task_state, method="hook", workspace=workspace):
                            continue
                        lines.append(note)
                        candidate_used += len(note) + 1
                        candidate_delivered.append(item)
                        dedup.record(delivery_entry)
                except ContractError:
                    dedup.entries = candidate_coverage
                    dedup.pending = candidate_pending
                    self.db.execute("ROLLBACK TO optional_candidate_context")
                    self.db.execute("RELEASE optional_candidate_context")
                    raise
                except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
                    dedup.entries = candidate_coverage
                    dedup.pending = candidate_pending
                    self.db.execute("ROLLBACK TO optional_candidate_context")
                    del lines[candidate_start:]
                    candidate_delivered, candidate_used = [], 0
                    candidate_context_failed = True
                    self.db.execute("RELEASE optional_candidate_context")
                else:
                    self.db.execute("RELEASE optional_candidate_context")
            work_delivered = []
            if work_items:
                work_line_start = len(lines)
                self.db.execute("SAVEPOINT optional_work_context")
                work_coverage = list(dedup.entries)
                work_pending = list(dedup.pending)
                try:
                    work_header = "工作历史索引（非正式解法/执行授权；仅相关且索引不足时用 work_memory_read）："
                    # 正式知识、完整失效提醒及任务边界已优先分配预算。工作历史只能使用
                    # 剩余空间，并共享 injection_max_chars，不额外叠加一个注入预算。
                    work_budget = min(remaining(), max(0, optional_budget - candidate_used))
                    work_used = 0
                    for work_item in self.work_validate_items(project, work_items, workspace=workspace):
                        from .candidate_reference_context import suppress_work
                        changed_work = bool(work_item.get("source_changed") or work_item.get("file_state_changed"))
                        if (suppress_work(self, project, work_item, [*delivered, *candidate_delivered])
                                and (not changed_work or work_item.get("candidate_reference_risk"))):
                            continue
                        card = {"memory_id": work_item["memory_id"], "revision": work_item["revision"],
                                "title": clip(work_item.get("title", ""), 50),
                                "summary": clip(work_item.get("summary", ""), 80),
                                "state": work_item.get("state", "reported"),
                                "validation_level": work_item.get("validation_level", "reported"),
                                "read_required": True}
                        if work_item.get("reference_role") == "task_handoff":
                            card["reference_role"] = "task_handoff"
                        if work_item.get("completion", "complete") != "complete":
                            card["completion"] = work_item["completion"]
                        if work_item.get("task_binding_known") is False:
                            card["task_binding_known"] = False
                        if work_item.get("task_affinity") == "context_only":
                            card["task_affinity"] = "context_only"
                        if work_item.get("source_changed"):
                            card["source_changed"] = True
                        if work_item.get("file_state_changed"):
                            card["file_state_changed"] = True
                            card["file_version_changed"] = True
                        if changed_work:
                            # A risk update survives duplicate-body suppression,
                            # without replaying the stale statement as advice.
                            card.pop("summary", None)
                            card["reference_role"] = "source_update"
                        files = work_item.get("files", [])
                        if files:
                            if any(file.get("version_changed") is True for file in files if isinstance(file, dict)):
                                card["file_version_changed"] = True
                            card["files"] = [{"path": clip(file.get("relative_path", ""), 100),
                                              "relation": file.get("relation", "observed_target"),
                                              "version_changed": file.get("version_changed", "unknown")}
                                             for file in files[:2] if isinstance(file, dict)]
                        note = "[工作历史] " + encode(card)
                        prefix_cost = len(work_header) + 1 if not work_delivered else 0
                        cost = len(note) + 1 + prefix_cost
                        if cost > work_budget - work_used:
                            # 保留准确身份及状态；不能把截断的卡记为已经展示。
                            card.pop("files", None)
                            card.pop("summary", None)
                            card["title"] = clip(card["title"], 40)
                            note = "[工作历史] " + encode(card)
                            cost = len(note) + 1 + prefix_cost
                        if cost > work_budget - work_used:
                            continue
                        delivery_entry = dedup.prepare("work", work_item, card)
                        if dedup.repeated(delivery_entry):
                            continue
                        if not work_delivered:
                            lines.append(work_header)
                        lines.append(note)
                        work_used += cost
                        work_delivered.append(work_item)
                        dedup.record(delivery_entry)
                    if work_delivered:
                        self.work_record_exposure(
                            project, conversation, work_delivered, agent_id=agent_id, delivery=delivery,
                            task_state=task_state, compact=compact, context_epoch=work_context_epoch,
                            method="hook", context_handle=context_handle, expected_epoch=epoch)
                        self._metric(project, "work_indices_delivered", len(work_delivered))
                except ContractError:
                    dedup.entries = work_coverage
                    dedup.pending = work_pending
                    self.db.execute("ROLLBACK TO optional_work_context")
                    self.db.execute("RELEASE optional_work_context")
                    raise
                except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
                    dedup.entries = work_coverage
                    dedup.pending = work_pending
                    self.db.execute("ROLLBACK TO optional_work_context")
                    self.db.execute("RELEASE optional_work_context")
                    del lines[work_line_start:]
                    work_delivered = []
                    work_context_failed = True
                else:
                    self.db.execute("RELEASE optional_work_context")
            if work_context_failed:
                self._metric(project, "work_context_failed")
            if candidate_context_failed:
                self._metric(project, "candidate_context_failed")
            if candidate_delivered:
                self._metric(project, "candidate_reference_injected", len(candidate_delivered))
            if new_delivered:
                self._metric(project, "knowledge_injected", len(new_delivered))
            if delivered and delivery == "ephemeral":
                self._metric(project, "ephemeral_cards_delivered", len(delivered))
            if should_search and (not tool_only or fault):
                refs = [{"knowledge_id": i["knowledge_id"], "revision": i["revision"], "match": i.get("match", {})} for i in items[:10]]
                saved_signature = signature
                if retryable:
                    # 最短五秒、最长一分钟；提醒链在冷却期间仍独立运行。
                    delay = search_result.get("retrieval", {}).get("retry_after_seconds", 5)
                    delay = max(5, min(60, delay if isinstance(delay, (int, float)) else 5))
                    saved_signature = f"retry:{time.time() + delay}:{signature}"
                self.db.execute(f"UPDATE {table} SET retrieval_signature=?,retrieval_refs=? WHERE {where}",
                                (saved_signature, encode(refs), *key))
            if not tool_only:
                # Preserve the identity guard on continuation/compaction. One
                # extra character preserves the unsafe boundary for huge input;
                # its truncated prefix can never silently become a safe query.
                self.db.execute(f"UPDATE {table} SET last_query=? WHERE {where}",
                                (effective[:_MAX_QUERY_CHARS + 1], *key))
            if fault:
                refs = [{"knowledge_id": i["knowledge_id"], "revision": i["revision"], "match": i.get("match", {})}
                        for i in items[:5] if i.get("match", {}).get("auto_eligible", False) and i["source_health"] == "valid"]
                for ref in refs:
                    ref["retrieval_signature"] = fault_result_signature
                pending = [ref for ref in refs if f"{ref['knowledge_id']}@{ref['revision']}" not in prior]
                self.db.execute("UPDATE context_faults SET state=?,results=? WHERE project=? AND conversation=? AND agent=? AND signature=?",
                                ("pending" if retryable or fault["state"] == "pending" and not should_search
                                 else "ready" if pending else "done", encode(pending), project, conversation, agent_id or "", fault["signature"]))
                if should_search:
                    self._metric(project, "failure_retrievals")
            if _outcome is not None:
                # 原生无任务故障与上下文故障共用检索/冷却结果；没有容纳任何
                # 有效卡时保留待办，不能把生成句柄误记为已交付解决经验。
                _outcome.update(searched=should_search, cooling=cooling,
                                retryable=retryable, delivered_cards=len(delivered),
                                delivery_pending=bool(eligible and not delivered),
                                delivered_work_indices=len(work_delivered))
            output = "\n".join(lines)
        # Cache failures cannot roll back notification acknowledgements or
        # authoritative exposure bookkeeping for the complete rendered output.
        dedup.flush()
        return output
