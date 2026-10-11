"""可观察结果采集；案例关联不等于确认修复或业务验收。"""
from __future__ import annotations

import json
import hashlib
import os
import time


def tool_outcome(response):
    """先判断明确终态，session_id 只表示尚无终态的运行观察。"""
    if not isinstance(response, dict):
        return "unknown"
    codes = [response[key] for key in ("exit_code", "exitCode")
             if isinstance(response.get(key), int) and not isinstance(response[key], bool)]
    if (any(code != 0 for code in codes) or response.get("isError") is True
            or response.get("is_error") is True or response.get("success") is False):
        return "failed"
    if codes or response.get("success") is True:
        return "succeeded"
    if response.get("isError") is False or response.get("is_error") is False:
        return "call_completed"
    if response.get("session_id") is not None:
        return "running"
    return "unknown"


_RESULT_FIELDS = ("exit_code", "exitCode", "isError", "is_error", "success", "error_code")
_PRIORITY = {
    **{key: 0 for key in _RESULT_FIELDS},
    "tool_response": 1, "tool_name": 1, "observed_outcome": 1, "operation_conflict": 1,
    "control_decision": 1, "host_epoch": 1, "session_id": 2, "tool_input": 2,
    "error": 2, "stderr": 2, "message": 2, "last_assistant_message": 2,
    "prompt": 2, "command": 2, "path": 2, "resource": 2, "source": 2,
    "output": 3, "stdout": 3,
}


def _project(value, text_limit):
    from .runtime import clip

    if isinstance(value, str):
        return clip(value, text_limit)
    if isinstance(value, list):
        result = [_project(item, text_limit) for item in value[:20]]
        if len(value) > 20:
            result.append({"truncated_items": len(value) - 20})
        return result
    if isinstance(value, dict):
        keys = sorted(value, key=lambda key: _PRIORITY.get(key, 4))
        result = {key: _project(value[key], text_limit) for key in keys[:30]}
        if len(keys) > 30:
            result["truncated_fields"] = len(keys) - 30
        return result
    return value


def prepare_event(payload, budget):
    """返回有限 JSON 与完整脱敏内容摘要；完整摘要在独立数据库字段保存。"""
    from .runtime import clean, clip, encode

    sanitized = clean(payload)
    original = encode(sanitized)
    capture_digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
    if len(original) <= budget:
        return original, capture_digest

    text_limit = max(1, budget // 4)
    while True:
        projected = _project(sanitized, text_limit)
        projected["truncated"] = True
        text = encode(projected)
        if len(text) <= budget:
            return text, capture_digest
        if text_limit == 1:
            break
        text_limit = max(1, text_limit // 2)

    # 极小预算先保留已识别的结果结构，再分配可选的工具名和错误摘要。
    result = {"truncated": True}
    response = sanitized.get("tool_response")
    if isinstance(response, dict):
        fields = {}
        for key in _RESULT_FIELDS:
            value = response.get(key)
            if isinstance(value, (int, bool)) and len(encode(value)) <= 16:
                fields[key] = value
            elif key == "error_code" and isinstance(value, str):
                fields[key] = clip(value, 24)
        if fields:
            result["tool_response"] = fields
        # 同一结果含大量矛盾标记时，统一失败判定仍不可被预算抹掉。
        if len(encode(result)) > budget:
            fields = {"is_error": tool_outcome(response) == "failed"}
            for key in ("exit_code", "exitCode", "error_code"):
                if key in response and isinstance(response[key], (int, str)):
                    value = clip(response[key], 16) if isinstance(response[key], str) else response[key]
                    candidate = {**fields, key: value}
                    if len(encode({"truncated": True, "tool_response": candidate})) <= budget:
                        fields = candidate
            result["tool_response"] = fields
    for key in ("operation_conflict", "observed_outcome", "host_epoch", "tool_name"):
        if key not in sanitized:
            continue
        value = sanitized[key]
        if isinstance(value, str):
            value = clip(value, 24)
        candidate = {**result, key: value}
        if len(encode(candidate)) <= budget:
            result = candidate
    return encode(result), capture_digest


def _payload(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    if not isinstance(value, dict):
        return {}
    if "payload" in value:
        return _payload(value["payload"])
    return value


def operation_input_conflict(runtime, project, operation, raw):
    """旧截断事件不猜完整输入；新事件优先使用独立输入签名。"""
    from .runtime import clean, digest

    if not operation or "tool_input" not in raw:
        return False
    row = runtime.db.execute(
        "SELECT * FROM events WHERE project=? AND operation=? AND type='PreToolUse' ORDER BY created,id LIMIT 1",
        (project, operation)).fetchone()
    if not row:
        return False
    pre_event = dict(row)
    payload = _payload(pre_event)
    signature = pre_event.get("tool_input_digest") if isinstance(pre_event, dict) else None
    if signature:
        return signature != digest(clean(raw["tool_input"]))
    if payload.get("truncated") or "tool_input" not in payload:
        return False
    return digest(clean(payload["tool_input"])) != digest(clean(raw["tool_input"]))


_TARGET_KEYS = {"path", "paths", "file", "file_path", "filepath", "filename", "target",
                "resource", "resource_id", "resource_uri", "asset", "asset_id", "asset_path",
                "workspace", "cwd", "project_path"}
_PATH_KEYS = {"path", "paths", "file", "file_path", "filepath", "filename", "asset_path",
              "workspace", "cwd", "project_path"}


def _targets(value, prefix="", depth=0):
    from .runtime import encode

    if depth > 5:
        return {}
    if isinstance(value, list):
        result = {}
        for index, item in enumerate(value[:50]):
            result.update(_targets(item, prefix + str(index) + ".", depth + 1))
        return result
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        label = prefix + key
        if key.lower() in _TARGET_KEYS and item is not None:
            if key.lower() in _PATH_KEYS and isinstance(item, str):
                item = os.path.normcase(os.path.normpath(item))
            result[label] = encode(item)
        elif isinstance(item, (dict, list)):
            result.update(_targets(item, label + ".", depth + 1))
    return result


def _targets_compatible(first, second):
    a, b = _targets(first), _targets(second)
    common = a.keys() & b.keys()
    if a and b and not common:
        return False
    return all(a[key] == b[key] for key in common)


class CaseCaptureMixin:
    def _operation_attempts(self, project, operation):
        if not operation:
            return []
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM events WHERE project=? AND operation=? AND type='PreToolUse' ORDER BY created,id",
            (project, operation)).fetchall()]

    def _capture_tool_case(self, project, conversation, agent_id, event, outcome, raw,
                           task_binding, source, epoch, operation):
        from .runtime import clean, clip, encode

        if str(raw.get("tool_name", "")).startswith("mcp__agent_learning__"):
            return
        attempts = self._operation_attempts(project, operation)
        if outcome in {"failed", "conflict"}:
            self._queue_fault(project, conversation, agent_id, source, epoch, raw)
            summary = f"{raw.get('tool_name', 'unknown tool')} {outcome}: " + clip(
                encode(clean(raw.get("tool_response"))), 300)
            refs = [item["id"] for item in attempts] + [event["id"]]
            roles = {item["id"]: "attempt" for item in attempts}
            roles[event["id"]] = "conflict" if outcome == "conflict" else "failure"
            problem = self._problem(project, conversation, summary, "operation", refs,
                                    task_binding=task_binding, evidence_roles=roles)
            if outcome == "conflict" and operation:
                affected = self.db.execute(
                    "SELECT DISTINCT pe.problem FROM problem_events pe JOIN events e ON e.id=pe.event "
                    "WHERE e.project=? AND e.operation=?", (project, operation)).fetchall()
                for item in affected:
                    if item[0] != problem["id"]:
                        self._attach_case_evidence(item[0], [(event["id"], "conflict")])
            return
        if outcome != "succeeded" or not event.get("task_id"):
            return
        # 无已验证任务的孤儿结果不猜历史归属；身份、assignment 和宿主轮次都相同才比较。
        failures = self.db.execute(
            "SELECT p.id AS problem_id,e.* FROM problems p JOIN problem_events pe ON pe.problem=p.id "
            "JOIN events e ON e.id=pe.event JOIN operations o ON o.project=e.project AND o.id=e.operation "
            "WHERE p.project=? AND p.conversation=? AND p.task_id=? AND p.task_revision=? "
            "AND e.type='PostToolUse' AND e.outcome='failed' AND e.agent IS ? "
            "AND e.assignment_id IS ? AND o.host_epoch=? "
            "AND json_extract(e.payload,'$.tool_name')=? "
            "AND NOT EXISTS(SELECT 1 FROM problem_events recovery JOIN events observed ON observed.id=recovery.event "
            "WHERE recovery.problem=p.id AND recovery.role='potential_recovery' AND observed.created>=e.created "
            "AND observed.agent IS e.agent AND observed.assignment_id IS e.assignment_id) "
            "ORDER BY e.created DESC,e.id DESC LIMIT 20",
            (project, conversation, event["task_id"], event["task_revision"], agent_id,
             event.get("assignment_id"), epoch, raw.get("tool_name"))).fetchall()
        success_input = raw.get("tool_input")
        if success_input is None and attempts:
            success_input = _payload(attempts[0]).get("tool_input")
        for failure in failures:
            failure = dict(failure)
            failure_input = _payload(failure).get("tool_input")
            prior = self._operation_attempts(project, failure["operation"])
            if failure_input is None and prior:
                failure_input = _payload(prior[0]).get("tool_input")
            if not _targets_compatible(failure_input, success_input):
                continue
            refs = [(item["id"], "attempt") for item in attempts] + [(event["id"], "potential_recovery")]
            self._attach_case_evidence(failure["problem_id"], refs, resolution_status="recovery_observed")
            break

    def _capture_delivery(self, project, conversation, task, event, raw):
        from .runtime import clip

        if (not self.config["capture_deliveries"] or not task or task["status"] != "delivered"
                or not raw.get("last_assistant_message")):
            return
        write_version = task["write_version"]
        if self.db.execute(
                "SELECT 1 FROM delivery_rounds WHERE project=? AND task_id=? AND revision=? AND write_version=?",
                (project, task["id"], task["revision"], write_version)).fetchone():
            return
        refs = [event["id"]]
        roles = {event["id"]: "delivery"}
        active = self.db.execute(
            "SELECT execution_epoch FROM task_states WHERE project=? AND id=? AND revision=? "
            "AND write_version<? AND status='active' ORDER BY write_version DESC LIMIT 1",
            (project, task["id"], task["revision"], write_version)).fetchone()
        observed_epoch = active[0] if active else task["execution_epoch"]
        observations = self.db.execute(
            "SELECT * FROM events WHERE project=? AND conversation=? AND task_id=? AND task_revision=? "
            "AND agent IS NULL AND execution_epoch=? AND type='PostToolUse' "
            "AND outcome IN ('succeeded','failed','conflict') ORDER BY created DESC,id DESC LIMIT 3",
            (project, conversation, task["id"], task["revision"], observed_epoch)).fetchall()
        for observed in observations:
            refs.append(observed["id"])
            roles[observed["id"]] = ("failure" if observed["outcome"] == "failed" else
                                     "conflict" if observed["outcome"] == "conflict" else "observation")
            for attempt in self._operation_attempts(project, observed["operation"]):
                if attempt["id"] not in roles:
                    refs.append(attempt["id"])
                    roles[attempt["id"]] = "attempt"
        prior = self.db.execute("SELECT problem_id FROM deliveries WHERE project=? AND task_id=? AND revision=?",
                                (project, task["id"], task["revision"])).fetchone()
        if prior:
            problem_id = prior[0]
            self._attach_case_evidence(problem_id, [(ident, roles[ident]) for ident in refs])
        else:
            problem = self._problem(project, conversation,
                                    "已交付任务结果（待审核）：" + clip(task["goal"], 200), "mixed", refs,
                                    task_binding=task, evidence_roles=roles)
            problem_id = problem["id"]
            self.db.execute("INSERT INTO deliveries(project,task_id,revision,problem_id) VALUES(?,?,?,?)",
                            (project, task["id"], task["revision"], problem_id))
        self.db.execute(
            "INSERT INTO delivery_rounds(project,task_id,revision,write_version,problem_id,event_id,created) "
            "VALUES(?,?,?,?,?,?,?)",
            (project, task["id"], task["revision"], write_version, problem_id, event["id"], time.time()))
