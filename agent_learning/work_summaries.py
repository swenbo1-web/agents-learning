"""可选的工作摘要：本地增量排队，复用管理员调用账本，不发布知识。"""
from __future__ import annotations

import json
import time


SUMMARY_INSTRUCTION_VERSION = 2
SUMMARY_PREFIX = (
    "本次用途是工作历史摘要，不是知识发布。仅概括可见来源中的尝试、结果、公开决策和未确认事项。"
    "不能将工具成功称为专业正确或用户验收；不能补全缺失的原文或模型内部思考。"
    "candidate.category必须为project、validation_level必须为reported，proposal必须为null。"
    "candidate.content记录工作经过，limitations明确尚未验收、来源覆盖和适用版本；"
    "evidence_refs必须引用实际支持摘要的原事件。无新增价值返回skip，来源不足返回needs_evidence。"
    "摘要只作为model_reported工作历史，不形成正式知识，也不执行任何治理动作。\n"
)


def install_work_summaries(runtime):
    from .runtime import ContractError

    if not runtime.db.in_transaction:
        raise ContractError("work summary migration requires a write transaction")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS work_summary_links(
        project TEXT NOT NULL,problem_id TEXT NOT NULL,conversation TEXT NOT NULL,
        task_id TEXT,task_revision INTEGER,task_write_version INTEGER,workspace_key TEXT NOT NULL,
        source_digest TEXT NOT NULL,source_snapshots TEXT NOT NULL,source_record_ids TEXT NOT NULL,
        policy_digest TEXT NOT NULL,host_scope TEXT NOT NULL,session_epoch INTEGER NOT NULL,
        governance_epoch INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
        memory_id TEXT,call_id TEXT,created REAL NOT NULL,updated REAL NOT NULL,
        PRIMARY KEY(project,problem_id),UNIQUE(project,host_scope,source_digest),
        FOREIGN KEY(problem_id) REFERENCES problems(id))""")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS work_summary_queue "
                       "ON work_summary_links(project,host_scope,state,created)")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS work_summary_conversation "
                       "ON work_summary_links(project,conversation,task_id,task_revision,created)")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS work_summary_call ON work_summary_links(call_id) WHERE call_id IS NOT NULL")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS work_summary_policies(
        project TEXT NOT NULL,host_scope TEXT NOT NULL,enabled INTEGER NOT NULL,
        version INTEGER NOT NULL,updated REAL NOT NULL,PRIMARY KEY(project,host_scope))""")


def _settings(runtime, project=None):
    from .runtime import ContractError

    cfg = runtime.config.get("work_memory", {})
    raw_enabled, master_enabled = cfg.get("summary_enabled", False), cfg.get("enabled", True)
    if project is not None:
        raw_enabled = runtime.work_summary_policy(project)["enabled"]
    enabled = raw_enabled and master_enabled
    minimum, maximum = cfg.get("summary_min_events", 2), cfg.get("summary_max_events", 12)
    if not isinstance(raw_enabled, bool) or not isinstance(master_enabled, bool):
        raise ContractError("work_memory.summary_enabled must be boolean")
    if (any(isinstance(value, bool) or not isinstance(value, int) for value in (minimum, maximum))
            or not 1 <= minimum <= maximum <= 40):
        raise ContractError("work summary event limits must satisfy 1 <= min <= max <= 40")
    return {"summary_enabled": enabled, "summary_min_events": minimum,
            "summary_max_events": maximum, "instruction_version": SUMMARY_INSTRUCTION_VERSION,
            "policy_version": runtime.work_summary_policy(project)["version"] if project is not None else 0}


def _link(runtime, project, problem_id):
    row = runtime.db.execute("SELECT * FROM work_summary_links WHERE project=? AND problem_id=?",
                             (project, problem_id)).fetchone()
    return dict(row) if row else None


# Apply before LIMIT: covered previews must not fill the bounded scan and hide
# new work. The derived message index also finds already-consumed segments.
_PUBLIC_PREVIEW_FILTER = (
    "AND NOT (m.event_id IS json_extract(m.body,'$.public_message_id') AND EXISTS("
    "SELECT COUNT(*) FROM work_memories pm JOIN events se ON se.project=pm.project AND se.id=pm.event_id "
    "WHERE pm.project=m.project AND pm.conversation=m.conversation AND json_valid(pm.body) "
    "AND json_extract(pm.body,'$.public_message_id')=m.event_id AND se.type='PublicMessageSegment' "
    "AND pm.workspace_key=m.workspace_key AND pm.task_id IS m.task_id AND pm.task_revision IS m.task_revision AND pm.agent IS m.agent "
    "AND json_extract(pm.body,'$.type')=json_extract(m.body,'$.type') "
    "AND json_extract(pm.body,'$.assignment_id') IS json_extract(m.body,'$.assignment_id') "
    "AND json_extract(pm.body,'$.task_write_version') IS json_extract(m.body,'$.task_write_version') "
    "AND COALESCE(json_extract(pm.body,'$.completion'),'complete')='complete' "
    "AND COALESCE(json_extract(pm.body,'$.coverage.truncated'),0)=0 "
    "AND json_type(pm.body,'$.coverage.segment')='integer' AND json_type(pm.body,'$.coverage.segments')='integer' "
    "AND json_extract(pm.body,'$.coverage.segments') BETWEEN 1 AND 55 "
    "HAVING COUNT(*)=MIN(json_extract(pm.body,'$.coverage.segments')) "
    "AND MIN(json_extract(pm.body,'$.coverage.segments'))=MAX(json_extract(pm.body,'$.coverage.segments')) "
    "AND COUNT(DISTINCT json_extract(pm.body,'$.coverage.segment'))=MIN(json_extract(pm.body,'$.coverage.segments')) "
    "AND MIN(json_extract(pm.body,'$.coverage.segment'))=0 "
    "AND MAX(json_extract(pm.body,'$.coverage.segment'))=MIN(json_extract(pm.body,'$.coverage.segments'))-1)) "
)


class WorkSummaryMixin:
    def work_summary_policy(self, project):
        exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='work_summary_policies'").fetchone()
        row = self.db.execute("SELECT * FROM work_summary_policies WHERE project=? AND host_scope=?",
                              (project, self._session_scope())).fetchone() if exists else None
        return {"enabled": bool(row["enabled"]) if row else bool(self.config["work_memory"]["summary_enabled"]),
                "configured": bool(row), "version": row["version"] if row else 0,
                "source": "project_host" if row else "configuration"}

    def work_summary_enabled(self, project):
        return bool(self.config["work_memory"]["enabled"] and self.work_summary_policy(project)["enabled"])

    def configure_work_summary(self, project, enabled):
        from .runtime import ContractError
        if type(enabled) is not bool:
            raise ContractError("work summary enable flag must be boolean")
        with self._case_transaction():
            policy = self.work_summary_policy(project)
            if not policy["configured"] or policy["enabled"] != enabled:
                self.db.execute("INSERT INTO work_summary_policies VALUES(?,?,?,?,?) "
                    "ON CONFLICT(project,host_scope) DO UPDATE SET enabled=excluded.enabled,version=excluded.version,updated=excluded.updated",
                    (project, self._session_scope(), int(enabled), policy["version"] + 1, time.time()))
            return self.work_summary_policy(project)

    def _work_queue_summary(self, project, conversation, workspace, *, expected_epoch=None, _phase_guard=None,
                            _checkout_key=None, _continuation=False):
        """阶段结束或已接受窗口的本地续排；不扫描项目或会话文件，不调用模型。"""
        from .runtime import digest, encode

        settings = _settings(self, project)
        if not settings["summary_enabled"] or not self.admin_policy(project)["enabled"]:
            return {"status": "disabled", "invocations": 0}
        with self._case_transaction():
            epoch = self.assert_host_epoch(expected_epoch)
            if _phase_guard:
                _phase_guard()
            if not self.owns_conversation(conversation):
                return {"status": "wrong_host", "invocations": 0}
            task = self.task(project, conversation)
            task = task or {}
            # 工作投影的 workspace_key 来自采集时的实际目录；不由当前分支名称猜文件版本。
            from .work_files import checkout_key
            checkout = _checkout_key if _checkout_key is not None else checkout_key(workspace)
            # A policy/account transition invalidates an unstarted window. Retire
            # it before selecting its observations again under the new lifecycle.
            pending = self.db.execute("SELECT problem_id FROM work_summary_links WHERE project=? "
                "AND conversation=? AND host_scope=? AND state='pending' ORDER BY created LIMIT 40",
                (project, conversation, self._session_scope())).fetchall()
            for previous in pending:
                self.work_summary_select(project, previous["problem_id"])
            rows = self.db.execute(
                "SELECT m.*,e.task_write_version AS source_task_write_version,e.business_digest AS business_digest FROM work_memories m "
                "JOIN events e ON e.id=m.event_id AND e.project=m.project "
                "WHERE m.project=? AND m.conversation=? AND m.workspace_key=? "
                "AND ((m.task_id IS ? AND m.task_revision IS ?) OR m.task_id IS NULL) AND m.agent IS NULL "
                "AND COALESCE(json_extract(m.body,'$.completion'),'complete')='complete' "
                "AND e.type NOT IN ('HistoryImport','WorkSummaryRequested') "
                "AND NOT EXISTS(SELECT 1 FROM work_summary_links s,json_each(s.source_record_ids) j "
                "WHERE s.project=m.project AND s.host_scope=? AND s.state IN ('pending','accepted','skipped','superseded') "
                "AND j.value=m.id) "
                "AND NOT EXISTS(SELECT 1 FROM work_summary_links s,json_each(s.source_snapshots) j "
                "WHERE s.project=m.project AND s.host_scope=? AND s.conversation=m.conversation AND s.workspace_key=m.workspace_key "
                "AND ((json_extract(j.value,'$.task_id') IS m.task_id AND json_extract(j.value,'$.task_revision') IS m.task_revision) "
                "OR (s.task_id IS m.task_id AND s.task_revision IS m.task_revision)) "
                "AND s.state IN ('pending','accepted','skipped','superseded') "
                "AND e.business_digest IS NOT NULL AND json_extract(j.value,'$.business_digest')=e.business_digest "
                "AND json_extract(j.value,'$.projection.classification_input_digest') IS json_extract(m.body,'$.classification_input_digest')) "
                "AND NOT EXISTS(SELECT 1 FROM work_summary_links s,json_each(s.source_record_ids) j "
                "JOIN admin_calls c ON c.project=s.project AND c.problem=s.problem_id "
                "WHERE s.project=m.project AND s.host_scope=? AND j.value=m.id "
                "AND c.state NOT IN ('not_started','deduplicated') AND c.retry_allowed=0 "
                "AND NOT EXISTS(SELECT 1 FROM review_receipts r WHERE r.project=c.project AND r.call_id=c.id)) "
                + _PUBLIC_PREVIEW_FILTER + "ORDER BY m.created,m.id LIMIT ?",
                (project, conversation, checkout, task.get("id"), task.get("revision"), self._session_scope(),
                 self._session_scope(), self._session_scope(), settings["summary_max_events"] * 4)).fetchall()
            # A full needs-evidence window must make room for a new observation.
            # Its prior sources have already been reviewed, but are not accepted
            # summaries; retain their raw memories and the original receipt.
            awaiting_rows = self.db.execute(
                "SELECT problem_id,source_record_ids FROM work_summary_links WHERE project=? AND conversation=? "
                "AND host_scope=? AND workspace_key=? AND task_id IS ? AND task_revision IS ? "
                "AND state='awaiting_evidence' AND policy_digest=? AND session_epoch=? AND governance_epoch=? "
                "ORDER BY created,problem_id",
                (project, conversation, self._session_scope(), checkout, task.get("id"), task.get("revision"),
                 digest(settings), self.session_epoch(), epoch),
            ).fetchall()
            awaiting_ids = {ident for old in awaiting_rows for ident in json.loads(old["source_record_ids"])}
            new_rows = [row for row in rows if row["id"] not in awaiting_ids]
            if awaiting_rows and not new_rows:
                return {"status": "awaiting_evidence", "problem_id": awaiting_rows[0]["problem_id"],
                        "reused": True, "invocations": 0}
            if awaiting_rows:
                # First reserve one genuinely new source, then retain as much
                # old context as the same count/character limits allow.
                rows = new_rows[:1] + [row for row in rows if row["id"] in awaiting_ids] + new_rows[1:]
            distinct, seen, used = [], set(), 0
            blocked_by_budget, min_blocked_cost = 0, None
            configured_max = self.config["admin"]["max_input_chars"]
            overhead = 2200 if configured_max <= 8000 else 5000
            window_budget = max(0, configured_max - overhead)
            for row in rows:
                signature = (row["business_digest"] or row["id"], json.loads(row["body"]).get("classification_input_digest"))
                if signature not in seen:
                    cost = len(row["body"]) + 350
                    if cost > window_budget:
                        seen.add(signature)
                        blocked_by_budget += 1
                        min_blocked_cost = cost if min_blocked_cost is None else min(min_blocked_cost, cost)
                        continue
                    if used + cost > window_budget:
                        blocked_by_budget += 1
                        needed = used + cost
                        min_blocked_cost = needed if min_blocked_cost is None else min(min_blocked_cost, needed)
                        break
                    distinct.append(row)
                    seen.add(signature)
                    used += cost
                if len(distinct) >= settings["summary_max_events"]:
                    break
            # Selection priority reserves space for supplementation; evidence
            # presentation retains the original chronological order.
            rows = sorted(distinct, key=lambda row: (row["created"], row["id"]))
            if awaiting_rows and not any(row["id"] not in awaiting_ids for row in rows):
                return {"status": "input_budget_exceeded", "source_count": 0,
                        "blocked_count": blocked_by_budget,
                        "required_max_input_chars": (min_blocked_cost or 0) + overhead, "invocations": 0}
            minimum = 1 if _continuation else settings["summary_min_events"]
            if len(rows) < minimum:
                if blocked_by_budget > 0 and len(rows) + blocked_by_budget >= minimum:
                    return {"status": "input_budget_exceeded", "source_count": len(rows),
                            "blocked_count": blocked_by_budget,
                            "required_max_input_chars": (min_blocked_cost or 0) + overhead, "invocations": 0}
                return {"status": "insufficient_delta", "source_count": len(rows), "invocations": 0}
            snapshots = [dict(self.work_source_snapshot(project, row["event_id"]), business_digest=row["business_digest"],
                              projection=json.loads(row["body"]), task_id=row["task_id"],
                              task_revision=row["task_revision"], task_binding_known=row["task_id"] is not None,
                              agent=row["agent"])
                         for row in rows]
            if any(snapshot.get("state") in {"missing", "changed", "unavailable"} for snapshot in snapshots):
                return {"status": "needs_evidence", "source_count": len(rows), "invocations": 0}
            identity = digest({"conversation": conversation, "workspace_key": checkout,
                               "task_id": task.get("id"), "task_revision": task.get("revision"), "sources": snapshots,
                               "policy": settings, "host_scope": self._session_scope(),
                               "session_epoch": self.session_epoch(), "governance_epoch": epoch})
            old = self.db.execute("SELECT problem_id,state FROM work_summary_links WHERE project=? "
                                  "AND host_scope=? AND source_digest=?",
                                  (project, self._session_scope(), identity)).fetchone()
            if old:
                return {"status": old["state"], "problem_id": old["problem_id"], "reused": True, "invocations": 0}
            selected_ids = {row["id"] for row in rows}
            superseded_problems = [old["problem_id"] for old in awaiting_rows]
            supplementation = ({"previous_problem_ids": superseded_problems,
                                 "omitted_previous_sources": len(awaiting_ids - selected_ids)}
                                if superseded_problems else {})
            request, _ = self._event(project, conversation, "WorkSummaryRequested",
                                     {"purpose": "work_summary", "source_event_refs": [row["event_id"] for row in rows],
                                      "source_digest": identity, "source_count": len(rows),
                                      "validation_level": "reported", "publication_automatic": False,
                                      **({"supplementation": supplementation} if supplementation else {})},
                                     event_id="work-summary:" + identity, task_id=task.get("id"),
                                     task_revision=task.get("revision"), task_write_version=task.get("write_version"),
                                     execution_epoch=task.get("execution_epoch"), outcome="reported")
            goal = task.get("goal") or "会话阶段记录"
            title = "工作历史摘要（reported，非正式知识）：" + goal[:240] + " [" + identity[:16] + "]"
            # The request belongs to the task, while original observations retain their own binding.
            problem = self._problem(project, conversation, title, "project", [request["id"]],
                                    task_binding=task or {"id": None, "revision": None})
            now = time.time()
            for prior_problem in superseded_problems:
                self.db.execute("UPDATE work_summary_links SET state='superseded',updated=? WHERE project=? AND problem_id=? AND state='awaiting_evidence'",
                                (now, project, prior_problem))
                self.db.execute("UPDATE problems SET status='superseded' WHERE project=? AND id=? AND status='pending'",
                                (project, prior_problem))
            self.db.execute("INSERT INTO work_summary_links(project,problem_id,conversation,task_id,task_revision,"
                            "task_write_version,workspace_key,source_digest,source_snapshots,source_record_ids,"
                            "policy_digest,host_scope,session_epoch,governance_epoch,state,created,updated) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                            (project, problem["id"], conversation, task.get("id"), task.get("revision"), task.get("write_version"),
                             checkout, identity, encode(snapshots), encode([row["id"] for row in rows]),
                             digest(settings), self._session_scope(), self.session_epoch(), epoch, now, now))
            self._metric(project, "work_summary_requests")
            if _phase_guard:
                _phase_guard()
            out = {"status": "pending", "problem_id": problem["id"], "request_event_id": request["id"],
                   "source_count": len(rows), "invocations": 0, "purpose": "work_summary"}
            if superseded_problems:
                out["superseded_problem_ids"] = superseded_problems
                out["omitted_previous_sources"] = supplementation["omitted_previous_sources"]
            return out

    def _work_summary_supplementation(self, project, problem_id):
        row = self.db.execute(
            "SELECT e.payload FROM events e JOIN problem_events pe ON pe.event=e.id "
            "WHERE e.project=? AND pe.problem=? AND e.type='WorkSummaryRequested' ORDER BY e.created LIMIT 1",
            (project, problem_id),
        ).fetchone()
        return json.loads(row["payload"]).get("supplementation", {}) if row else {}

    def work_summary_guard(self, project, problem_id, *, accepted_call_id=None):
        """返回用途快照；关闭、换号、改任务、变更来源时禁止旧摘要写回。"""
        from .runtime import ContractError, digest

        link = _link(self, project, problem_id)
        if link is None:
            return None
        if link["state"] != "pending" and not (
            accepted_call_id and link["state"] in {"accepted", "skipped", "awaiting_evidence"}
            and link["call_id"] == accepted_call_id
        ):
            raise ContractError("work_summary_not_pending")
        settings = _settings(self, project)
        if not settings["summary_enabled"] or not self.admin_policy(project)["enabled"]:
            raise ContractError("work_summary_disabled")
        if link["host_scope"] != self._session_scope():
            raise ContractError("work_summary_wrong_host")
        if (link["session_epoch"] != self.session_epoch() or link["governance_epoch"] != self.host_epoch()):
            raise ContractError("work_summary_stale_epoch")
        if link["policy_digest"] != digest(settings):
            raise ContractError("work_summary_policy_changed")
        task = self.task(project, link["conversation"])
        if link["task_id"] is not None and (not task or (task["id"], task["revision"]) != (
                link["task_id"], link["task_revision"])):
            raise ContractError("work_summary_task_changed")
        for snapshot in json.loads(link["source_snapshots"]):
            current = self.work_source_snapshot(project, snapshot["event_id"])
            if (current.get("capture_digest"), current.get("source_version"), current.get("state")) != (
                    snapshot.get("capture_digest"), snapshot.get("source_version"), snapshot.get("state")):
                raise ContractError("work_summary_source_changed")
        link["purpose"] = "work_summary"
        link["scope"] = "task" if link["task_id"] else "conversation"
        link["instruction_version"] = SUMMARY_INSTRUCTION_VERSION
        return link

    def work_summary_packet(self, project, problem_id, max_chars=None):
        """Frozen visible projections have their own coverage contract, never formal publication evidence."""
        from .runtime import ContractError
        from .review_packet import review_instruction
        limit = self.config["admin"]["max_input_chars"] if max_chars is None else max_chars
        if type(limit) is not int or not 1000 <= limit <= 200000:
            raise ContractError("invalid work summary input budget")
        link = self.work_summary_guard(project, problem_id)
        if link is None:
            raise ContractError("work_summary_link_missing")
        problem = dict(self.db.execute("SELECT * FROM problems WHERE project=? AND id=?", (project, problem_id)).fetchone())
        snapshots = json.loads(link["source_snapshots"])
        packet = {"problem": problem, "task": None, "purpose": "work_summary", "scope": link["scope"],
                  "source_digest": link["source_digest"], "events": [], "evidence_count": len(snapshots),
                  "related_knowledge": [], "related_candidates": [], "incomplete": False,
                  "evidence_truncated": False, "coverage": "frozen_visible_projections", "omitted_count": 0}
        supplementation = self._work_summary_supplementation(project, problem_id)
        if supplementation:
            packet["supplementation"] = supplementation
        for snapshot in snapshots:
            projection = snapshot.get("projection")
            if projection is None:
                # Older requests recover their already stored work projection, not the full transcript.
                row = self.db.execute("SELECT body FROM work_memories WHERE project=? AND event_id=?",
                                      (project, snapshot["event_id"])).fetchone()
                if row is None:
                    packet["incomplete"] = True
                    break
                projection = json.loads(row["body"])
            item = {"id": snapshot["event_id"], "type": "WorkObservation", "outcome": "reported",
                    "role": "observation", "health": "valid", "current_tool_success": False,
                    "payload": projection, "task_binding_known": snapshot.get("task_binding_known", False),
                    "task_id": snapshot.get("task_id"), "task_revision": snapshot.get("task_revision")}
            packet["events"].append(item)
            packet["evidence_truncated"] |= bool(projection.get("coverage", {}).get("truncated"))
            if len(SUMMARY_PREFIX + review_instruction(packet)) > limit:
                packet["events"].pop()
                packet["incomplete"] = True
                break
        packet["omitted_count"] = len(snapshots) - len(packet["events"])
        return packet

    def work_summary_select(self, project, problem_id):
        """扫描阶段只本地检查；失效批次退出队列，禁用或异宿主批次仍保留。"""
        from .runtime import ContractError

        try:
            return self.work_summary_guard(project, problem_id)
        except ContractError as exc:
            reason = str(exc)
            if reason not in {"work_summary_disabled", "work_summary_wrong_host"}:
                self.db.execute("UPDATE work_summary_links SET state='stale',updated=? WHERE project=? AND problem_id=?",
                                (time.time(), project, problem_id))
                self.db.execute("UPDATE problems SET status='superseded' WHERE project=? AND id=?", (project, problem_id))
            return {"purpose": "work_summary", "eligible": False, "reason": reason}

    def work_summary_accept(self, project, problem_id, body, call_id):
        """调用方已在管理员接受事务中校验schema；摘要没有候选/发布权限。"""
        from .runtime import ContractError, clean, clip, digest

        link = self.work_summary_guard(project, problem_id)
        if link is None:
            raise ContractError("work_summary_link_missing")
        snapshots = json.loads(link["source_snapshots"])
        refs = {snapshot["event_id"] for snapshot in snapshots}
        if (body["category"] != "project" or body["validation_level"] != "reported"
                or not body["evidence_refs"] or set(body["evidence_refs"]) - refs):
            raise ContractError("work summary requires reported project sources")
        # 条件与限制各有保留空间，长正文不能把未验收提醒挤掉。
        content = (clip(clean(body["content"]), 2800) + "\n适用范围：" + clip(clean(body["applicability"]), 400)
                   + "\n限制与未确认事项：" + clip(clean(body["limitations"]), 600))
        record = {"type": "work_summary", "title": clip(clean(body["title"]), 160),
                  "summary": clip(content, 4000), "validation_level": "model_reported",
                  "applicability": clip(clean(body["applicability"]), 1000),
                  "task_write_version": link["task_write_version"], "purpose": "work_summary",
                  "administrator_call_id": call_id, "publication_automatic": False,
                  "coverage": {"source_count": len(snapshots), "incomplete": any(
                      snapshot.get("projection", {}).get("coverage", {}).get("truncated", False) for snapshot in snapshots),
                               "scope": link["scope"], "projection_only": True,
                               "task_acceptance": "not_verified", "origin": "administrator_model"}}
        supplementation = self._work_summary_supplementation(project, problem_id)
        if supplementation:
            record["coverage"]["supplementation"] = supplementation
        ident = digest([project, "work-summary", link["source_digest"]])[:32]
        mixed = any(self.db.execute("SELECT task_id,task_revision FROM work_memories WHERE project=? AND event_id=?",
                    (project, snapshot["event_id"])).fetchone()[:] != (link["task_id"], link["task_revision"])
                    for snapshot in snapshots)
        scope = "conversation" if mixed or link["task_id"] is None else "task"
        record["scope"] = scope
        record["coverage"]["scope"] = scope
        result = self.record_work_summary(project, ident, record, snapshots, link["conversation"],
                                          task_id=link["task_id"] if scope == "task" else None,
                                          task_revision=link["task_revision"] if scope == "task" else None)
        memory_id = result.get("memory_id", result.get("id", ident))
        self.db.execute("UPDATE work_summary_links SET state='accepted',memory_id=?,call_id=?,updated=? "
                        "WHERE project=? AND problem_id=? AND state='pending'",
                        (memory_id, call_id, time.time(), project, problem_id))
        self.db.execute("UPDATE problems SET status='reviewed' WHERE project=? AND id=?", (project, problem_id))
        self._metric(project, "work_summaries_created")
        return {"purpose": "work_summary", "memory_id": memory_id,
                "memory_revision": result.get("revision", 1), "validation_level": "model_reported",
                "continuation": self._work_queue_summary(project, link["conversation"], None,
                    expected_epoch=link["governance_epoch"], _checkout_key=link["workspace_key"], _continuation=True),
                "publication": {"status": "not_published", "reason": "work history summary is not formal knowledge"}}

    def work_summary_skip(self, project, problem_id, call_id):
        """A validated no-value decision consumes this window without publishing anything."""
        from .runtime import ContractError
        link = self.work_summary_guard(project, problem_id)
        if link is None:
            raise ContractError("work_summary_link_missing")
        self.db.execute("UPDATE work_summary_links SET state='skipped',call_id=?,updated=? "
                        "WHERE project=? AND problem_id=? AND state='pending'",
                        (call_id, time.time(), project, problem_id))
        self.db.execute("UPDATE problems SET status='reviewed' WHERE project=? AND id=?", (project, problem_id))
        return {"continuation": self._work_queue_summary(project, link["conversation"], None,
                    expected_epoch=link["governance_epoch"], _checkout_key=link["workspace_key"], _continuation=True)}

    def work_summary_needs_evidence(self, project, problem_id, call_id):
        """Mark the window as awaiting supplementary observations so new evidence can form a bounded replacement."""
        from .runtime import ContractError
        link = self.work_summary_guard(project, problem_id)
        if link is None:
            raise ContractError("work_summary_link_missing")
        self.db.execute("UPDATE work_summary_links SET state='awaiting_evidence',call_id=?,updated=? "
                        "WHERE project=? AND problem_id=? AND state='pending'",
                        (call_id, time.time(), project, problem_id))
        return {"summary_state": "awaiting_evidence"}
