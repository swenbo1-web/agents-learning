"""可选的工作摘要：本地增量排队，复用管理员调用账本，不发布知识。"""
from __future__ import annotations

import json
import time


SUMMARY_INSTRUCTION_VERSION = 1
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


def _settings(runtime):
    from .runtime import ContractError

    cfg = runtime.config.get("work_memory", {})
    raw_enabled, master_enabled = cfg.get("summary_enabled", False), cfg.get("enabled", True)
    enabled = raw_enabled and master_enabled
    minimum, maximum = cfg.get("summary_min_events", 2), cfg.get("summary_max_events", 12)
    if not isinstance(raw_enabled, bool) or not isinstance(master_enabled, bool):
        raise ContractError("work_memory.summary_enabled must be boolean")
    if (any(isinstance(value, bool) or not isinstance(value, int) for value in (minimum, maximum))
            or not 1 <= minimum <= maximum <= 40):
        raise ContractError("work summary event limits must satisfy 1 <= min <= max <= 40")
    return {"summary_enabled": enabled, "summary_min_events": minimum,
            "summary_max_events": maximum, "instruction_version": SUMMARY_INSTRUCTION_VERSION}


def _link(runtime, project, problem_id):
    row = runtime.db.execute("SELECT * FROM work_summary_links WHERE project=? AND problem_id=?",
                             (project, problem_id)).fetchone()
    return dict(row) if row else None


class WorkSummaryMixin:
    def _work_queue_summary(self, project, conversation, workspace, *, expected_epoch=None, _phase_guard=None):
        """仅在可信阶段结束调用；只查看已记录的工作投影，不扫描项目或会话文件。"""
        from .runtime import digest, encode

        settings = _settings(self)
        if not settings["summary_enabled"] or not self.admin_policy(project)["enabled"]:
            return {"status": "disabled", "invocations": 0}
        with self._case_transaction():
            epoch = self.assert_host_epoch(expected_epoch)
            if _phase_guard:
                _phase_guard()
            if not self.owns_conversation(conversation):
                return {"status": "wrong_host", "invocations": 0}
            task = self.task(project, conversation)
            if not task:
                return {"status": "task_required", "invocations": 0}
            # 工作投影的 workspace_key 来自采集时的实际目录；不由当前分支名称猜文件版本。
            from .work_files import checkout_key
            checkout = checkout_key(workspace)
            rows = self.db.execute(
                "SELECT m.*,e.task_write_version AS source_task_write_version,e.business_digest AS business_digest FROM work_memories m "
                "JOIN events e ON e.id=m.event_id AND e.project=m.project "
                "WHERE m.project=? AND m.conversation=? AND m.workspace_key=? "
                "AND m.task_id=? AND m.task_revision=? AND m.agent IS NULL "
                "AND e.type NOT IN ('HistoryImport','WorkSummaryRequested') "
                "AND NOT EXISTS(SELECT 1 FROM work_summary_links s,json_each(s.source_record_ids) j "
                "WHERE s.project=m.project AND s.host_scope=? AND s.state IN ('pending','accepted') "
                "AND j.value=m.id) "
                "AND NOT EXISTS(SELECT 1 FROM work_summary_links s,json_each(s.source_snapshots) j "
                "WHERE s.project=m.project AND s.host_scope=? AND s.task_id=m.task_id "
                "AND s.task_revision=m.task_revision AND s.state IN ('pending','accepted') "
                "AND e.business_digest IS NOT NULL AND json_extract(j.value,'$.business_digest')=e.business_digest) "
                "ORDER BY m.created,m.id LIMIT ?",
                (project, conversation, checkout, task["id"], task["revision"], self._session_scope(),
                 self._session_scope(), settings["summary_max_events"] * 4)).fetchall()
            distinct, seen = [], set()
            for row in rows:
                signature = row["business_digest"] or row["id"]
                if signature not in seen:
                    distinct.append(row)
                    seen.add(signature)
                if len(distinct) >= settings["summary_max_events"]:
                    break
            rows = distinct
            if len(rows) < settings["summary_min_events"]:
                return {"status": "insufficient_delta", "source_count": len(rows), "invocations": 0}
            snapshots = [dict(self.work_source_snapshot(project, row["event_id"]), business_digest=row["business_digest"])
                         for row in rows]
            if any(snapshot.get("state") in {"missing", "changed", "unavailable"} for snapshot in snapshots):
                return {"status": "needs_evidence", "source_count": len(rows), "invocations": 0}
            identity = digest({"conversation": conversation, "workspace_key": checkout,
                               "task_id": task["id"], "task_revision": task["revision"],
                               "task_write_version": task["write_version"], "sources": snapshots})
            old = self.db.execute("SELECT problem_id,state FROM work_summary_links WHERE project=? "
                                  "AND host_scope=? AND source_digest=?",
                                  (project, self._session_scope(), identity)).fetchone()
            if old:
                return {"status": old["state"], "problem_id": old["problem_id"], "reused": True, "invocations": 0}
            request, _ = self._event(project, conversation, "WorkSummaryRequested",
                                     {"purpose": "work_summary", "source_event_refs": [row["event_id"] for row in rows],
                                      "source_digest": identity, "source_count": len(rows),
                                      "validation_level": "reported", "publication_automatic": False},
                                     event_id="work-summary:" + identity, task_id=task["id"],
                                     task_revision=task["revision"], task_write_version=task["write_version"],
                                     execution_epoch=task["execution_epoch"], outcome="reported")
            title = "工作历史摘要（reported，非正式知识）：" + task["goal"][:240] + " [" + identity[:16] + "]"
            refs = [row["event_id"] for row in rows]
            roles = {ref: "observation" for ref in refs}
            problem = self._problem(project, conversation, title, "project", refs, task_binding=task,
                                    evidence_roles=roles)
            now = time.time()
            self.db.execute("INSERT INTO work_summary_links(project,problem_id,conversation,task_id,task_revision,"
                            "task_write_version,workspace_key,source_digest,source_snapshots,source_record_ids,"
                            "policy_digest,host_scope,session_epoch,governance_epoch,state,created,updated) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                            (project, problem["id"], conversation, task["id"], task["revision"], task["write_version"],
                             checkout, identity, encode(snapshots), encode([row["id"] for row in rows]),
                             digest(settings), self._session_scope(), self.session_epoch(), epoch, now, now))
            self._metric(project, "work_summary_requests")
            if _phase_guard:
                _phase_guard()
            return {"status": "pending", "problem_id": problem["id"], "request_event_id": request["id"],
                    "source_count": len(rows), "invocations": 0, "purpose": "work_summary"}

    def work_summary_guard(self, project, problem_id, *, accepted_call_id=None):
        """返回用途快照；关闭、换号、改任务、变更来源时禁止旧摘要写回。"""
        from .runtime import ContractError, digest

        link = _link(self, project, problem_id)
        if link is None:
            return None
        if link["state"] != "pending" and not (accepted_call_id and link["state"] == "accepted"
                                                 and link["call_id"] == accepted_call_id):
            raise ContractError("work_summary_not_pending")
        settings = _settings(self)
        if not settings["summary_enabled"] or not self.admin_policy(project)["enabled"]:
            raise ContractError("work_summary_disabled")
        if link["host_scope"] != self._session_scope():
            raise ContractError("work_summary_wrong_host")
        if (link["session_epoch"] != self.session_epoch() or link["governance_epoch"] != self.host_epoch()):
            raise ContractError("work_summary_stale_epoch")
        if link["policy_digest"] != digest(settings):
            raise ContractError("work_summary_policy_changed")
        task = self.task(project, link["conversation"])
        if not task or (task["id"], task["revision"], task["write_version"]) != (
                link["task_id"], link["task_revision"], link["task_write_version"]):
            raise ContractError("work_summary_task_changed")
        for snapshot in json.loads(link["source_snapshots"]):
            current = self.work_source_snapshot(project, snapshot["event_id"])
            if (current.get("capture_digest"), current.get("source_version"), current.get("state")) != (
                    snapshot.get("capture_digest"), snapshot.get("source_version"), snapshot.get("state")):
                raise ContractError("work_summary_source_changed")
        link["purpose"] = "work_summary"
        link["instruction_version"] = SUMMARY_INSTRUCTION_VERSION
        return link

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
                  "coverage": {"source_count": len(snapshots), "incomplete": False,
                               "task_acceptance": "not_verified", "origin": "administrator_model"}}
        ident = digest([project, "work-summary", link["source_digest"]])[:32]
        result = self.record_work_summary(project, ident, record, snapshots, link["conversation"],
                                          task_id=link["task_id"], task_revision=link["task_revision"])
        memory_id = result.get("memory_id", result.get("id", ident))
        self.db.execute("UPDATE work_summary_links SET state='accepted',memory_id=?,call_id=?,updated=? "
                        "WHERE project=? AND problem_id=? AND state='pending'",
                        (memory_id, call_id, time.time(), project, problem_id))
        self.db.execute("UPDATE problems SET status='reviewed' WHERE project=? AND id=?", (project, problem_id))
        self._metric(project, "work_summaries_created")
        return {"purpose": "work_summary", "memory_id": memory_id,
                "memory_revision": result.get("revision", 1), "validation_level": "model_reported",
                "publication": {"status": "not_published", "reason": "work history summary is not formal knowledge"}}
