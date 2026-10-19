"""Conservative local deferral of transient plugin status, never a review verdict.

Unknown text, tools, partial capture and independent cases retain normal review.
No model or external service is used, and source evidence is not modified.
"""
from __future__ import annotations

import json
import re
import unicodedata


def _text(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold()).strip("。.!！?？")


def _request(value):
    text = _text(value)
    return bool(len(text) <= 160 and re.fullmatch(
        r"(?:(?:请|帮我|麻烦|只读))*?(?:检查|查看|核对|确认)(?:一下)?(?:当前项目的?|本项目的?)?"
        r"(?:agent[-_]?learning(?:插件)?)(?:的)?(?:连接|状态|连接状态|管理员状态)"
        r"|(?:please)?(?:check|verify|show)(?:the)?agent[-_]?learning(?:plugin)?(?:connection|status|connectionstatus|administratorstatus)", text))


def _reply(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1600:
        return False
    # Full clauses must match; a familiar status word cannot hide new facts.
    clauses = [_text(clause) for clause in re.split(r"[。.!！?？;；\r\n]+", value) if _text(clause)]
    patterns = (
        r"(?:当前)?(?:agent[-_]?learning(?:插件)?的?)?连接(?:正常|已连接|检查通过)",
        r"(?:当前)?(?:agent[-_]?learning(?:插件)?的?)?状态正常",
        r"(?:当前)?管理员(?:已开启|已启用|未开启|未启用|已关闭|运行正常|空闲)",
        r"(?:当前)?无(?:在途|待处理|待审核)(?:管理员)?(?:调用|阶段|任务)",
        r"(?:当前)?(?:待处理|待审核)(?:阶段|任务|案例)[:：]?\d{1,8}(?:个|条)?",
        r"(?:本轮|本次)(?:仅为|为|仅)?只读(?:状态|连接)(?:检查|核对)(?:完成|通过)?",
        r"(?:agent[-_]?learning(?:plugin)?)?(?:connection|status)(?:is)?(?:ok|normal|healthy|connected)",
        r"(?:administrator|admin)(?:is)?(?:enabled|disabled|idle|running)",
        r"noactive(?:administrator|admin)?calls",
    )
    return bool(clauses) and all(any(re.fullmatch(pattern, clause) for pattern in patterns) for clause in clauses)


def _partial(value):
    if isinstance(value, dict):
        if any(value.get(key) is True for key in
               ("truncated", "message_truncated", "prompt_truncated", "last_assistant_message_truncated")):
            return True
        if "completion" in value and value["completion"] != "complete":
            return True
        return any(_partial(item) for item in value.values())
    return isinstance(value, list) and any(_partial(item) for item in value)


def is_routine(runtime, project, phase):
    """Recheck the whole original window, including late revisions, on each scan.

    Deliberately bounded: overflowing this window means normal review. A shell
    command cannot be inferred read-only from its name or a successful exit.
    """
    from .phase_review import _case_source
    if phase["omitted_sources"]:
        return False
    task = runtime.task_snapshot(project, phase["task_id"], phase["task_revision"], phase["delivery_version"])
    if not task or not _request(task["goal"]):
        return False
    sql, args = _case_source(runtime, project, phase)
    cases = runtime.db.execute("SELECT p.id,p.summary,p.knowledge_id,p.revision,"
        "(SELECT state FROM review_states r WHERE r.project=p.project AND r.problem=p.id AND r.revision=p.revision) review_state"
        + sql + "LIMIT 17", args).fetchall()
    if not cases or len(cases) > 16 or any(
            row["knowledge_id"] or row["revision"] != 1 or row["review_state"] == "awaiting_evidence"
            or not row["summary"].startswith("阶段公开记录：")
            or "[" + phase["id"] + "] part " not in row["summary"] for row in cases):
        return False
    stop = runtime.db.execute("SELECT created FROM events WHERE project=? AND id=?", (project, phase["event_id"])).fetchone()
    if not stop:
        return False
    events = runtime.db.execute(
        "SELECT e.type,e.outcome,e.agent,e.operation,length(e.payload) chars,substr(e.payload,1,6001) payload "
        "FROM events e LEFT JOIN events origin ON origin.project=e.project AND origin.id=json_extract(e.payload,'$.public_message_id') "
        "AND e.type='PublicMessageSegment' WHERE e.project=? AND e.conversation=? "
        "AND ((e.task_id=? AND e.task_revision=?) OR e.task_id IS NULL) "
        "AND COALESCE(origin.created,e.created)>=? AND COALESCE(origin.created,e.created)<=? "
        "ORDER BY COALESCE(origin.created,e.created),e.created,e.rowid LIMIT 41",
        (project, phase["conversation"], phase["task_id"], phase["task_revision"], phase["start_time"], stop[0])).fetchall()
    if len(events) > 40 or sum(row["chars"] for row in events) > 16000:
        return False
    requested = replied = False
    for event in events:
        if event["chars"] > 6000 or event["agent"] or event["outcome"] in {"failed", "conflict", "denied"}:
            return False
        body = json.loads(event["payload"])
        if not isinstance(body, dict) or _partial(body):
            return False
        kind = event["type"]
        if kind in {"UserPromptSubmit", "PhaseRequestContext"}:
            prompt = body.get("prompt")
            if _request(prompt):
                requested = True
            elif _text(prompt) not in {"继续", "继续核对", "继续检查", "continue"}:
                return False
        elif kind == "Stop":
            if not _reply(body.get("last_assistant_message")):
                return False
            replied = True
        elif kind == "TaskUpdate":
            if not _request(body.get("goal")):
                return False
        elif kind == "ExecutionFence":
            if (body.get("reason"), body.get("status")) not in {
                    ("new_user_prompt", "active"), ("antigravity_execution_stopped", "delivered")}:
                return False
        elif kind == "AntigravityStop":
            coverage = body.get("capture_coverage", {})
            if (runtime.host != "antigravity" or body.get("fullyIdle") is not True
                    or body.get("terminationReason") != "model_stop" or body.get("error") != ""
                    or coverage.get("workspace_association") != "installed_workspace"
                    or coverage.get("trajectory_identity") != "current"
                    or not _reply(body.get("finalModelOutput"))):
                return False
        elif kind == "SessionStart":
            continue
        elif kind in {"PreToolUse", "PostToolUse"}:
            # Exact plugin status tools only. Unknown/custom/shell tools retain
            # normal review even when their output claims everything is fine.
            name = body.get("tool_name", "")
            if name not in {"mcp__agent_learning__learning_status", "mcp__agent_learning__session_context"}:
                return False
            if kind == "PostToolUse" and event["outcome"] != "succeeded":
                return False
            if event["operation"]:
                operation = runtime.db.execute("SELECT state,conflict,decision,observation FROM operations WHERE project=? AND id=?",
                                               (project, event["operation"])).fetchone()
                if not operation or operation["conflict"] or operation["decision"] == "denied" or operation["observation"] != "succeeded":
                    return False
            else:
                return False
        else:
            return False
    return requested and replied
