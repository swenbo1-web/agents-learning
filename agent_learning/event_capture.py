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
BUSINESS_TRANSPORT_FIELDS = {"turn_id", "tool_use_id", "event_id", "conversation_id", "agent_id",
                             "session_id", "sessionId", "timestamp", "created", "updated"}


def normalize_business_payload(payload):
    """只去掉顶层传输身份；嵌套的 ID 和时间仍可能是业务条件。"""
    from .runtime import clean

    result = clean(payload)
    if isinstance(result, dict):
        result = {key: value for key, value in result.items()
                  if key not in BUSINESS_TRANSPORT_FIELDS and key != "agent_identity_known"}
        if isinstance(result.get("metadata"), dict):
            result["metadata"] = {key: value for key, value in result["metadata"].items() if value}
            if not result["metadata"]:
                result.pop("metadata")
    return result


def business_evidence_digest(payload):
    """在投影前计算全量脱敏业务摘要，不保存或发送原始长日志。"""
    from .runtime import digest
    return digest(normalize_business_payload(payload))


def projected_evidence(payload):
    """旧记录含投影标记时，不能从可见首尾恢复全量业务摘要。"""
    if isinstance(payload, dict):
        if any(key in payload for key in ("truncated", "truncated_items", "truncated_fields")):
            return True
        return any(projected_evidence(value) for value in payload.values())
    if isinstance(payload, list):
        return any(projected_evidence(value) for value in payload)
    return isinstance(payload, str) and "...[truncated]..." in payload

_PRIORITY = {
    **{key: 0 for key in _RESULT_FIELDS},
    "tool_response": 1, "tool_name": 1, "observed_outcome": 1, "operation_conflict": 1,
    "legacy_name_conflict": 1,
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
    for key in ("operation_conflict", "legacy_name_conflict", "observed_outcome", "host_epoch", "tool_name"):
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
        "SELECT * FROM events WHERE project=? AND operation=? AND type IN ('PreToolUse','PostToolUse') "
        "AND (tool_input_digest IS NOT NULL OR json_type(payload,'$.tool_input') IS NOT NULL) "
        "ORDER BY created,id LIMIT 1",
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
                "workspace", "cwd", "workdir", "project_path"}
_PATH_KEYS = {"path", "paths", "file", "file_path", "filepath", "filename", "asset_path",
              "workspace", "cwd", "workdir", "project_path"}


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
        # Native hosts use cwd/Cwd/workdir for the same execution directory.
        label = prefix + ("cwd" if key.lower() in {"cwd", "workdir"} else key)
        if key.lower() in _TARGET_KEYS and item is not None:
            if key.lower() in _PATH_KEYS and isinstance(item, str):
                item = os.path.normcase(os.path.normpath(item))
            result[label] = encode(item)
        elif isinstance(item, (dict, list)):
            result.update(_targets(item, label + ".", depth + 1))
    return result


def _command_target(text):
    """只解释单条明确运行器命令；完整目标相同才可作为潜在恢复。

    不使用词重合、脚本叶名或公共运行器证明相关性；复杂 shell 保持未知。
    参数变化本身不证明修复，只允许后续证据继续关联同一测试/脚本。
    """
    import re
    import shlex
    if (not isinstance(text, str) or not text.strip() or len(text) > 8000
            or any(mark in text for mark in (";", "|", "&", "<", ">", "`", "$", "\n", "\r"))):
        return None
    try:
        parts = [part.strip("\"'") for part in shlex.split(text, posix=False)]
    except ValueError:
        return None
    if not parts:
        return None
    runner = os.path.basename(parts.pop(0).replace("\\", "/")).lower().removesuffix(".exe")
    mode = runner
    if re.fullmatch(r"python(?:\d+(?:\.\d+)?)?|py", runner):
        while parts and parts[0] in {"-B", "-u", "-I", "-E", "-s", "-S", "-q", "-3"}:
            parts.pop(0)
        if not parts:
            return None
        if parts[0] == "-m" and len(parts) > 1:
            module = parts[1]
            if module not in {"pytest", "unittest"}:
                return None
            mode, parts = module, parts[2:]
        elif parts[0].startswith("-"):
            return None
        else:
            return ("python-script", os.path.normcase(os.path.normpath(parts[0])))
    if mode in {"pytest", "py.test", "unittest"}:
        options_with_value = {"-k", "-m", "-c", "-o", "--override-ini", "--rootdir", "--confcutdir",
                              "--maxfail", "--tb", "--capture", "--color", "--junitxml", "--log-level"}
        if mode == "unittest":
            options_with_value = {"-s", "--start-directory", "-p", "--pattern", "-t", "--top-level-directory"}
        targets, index = [], 0
        while index < len(parts):
            value = parts[index]
            if value in options_with_value:
                # unittest discovery 的目录/模式也是目标身份，不能当通用开关抛弃。
                if index + 1 >= len(parts):
                    return None
                if mode == "unittest":
                    targets.append(value + "=" + os.path.normcase(os.path.normpath(parts[index + 1])))
                index += 2
                continue
            if not value.startswith("-"):
                targets.append(os.path.normcase(os.path.normpath(value)))
            elif mode == "unittest" and value.startswith(("--start-directory=", "--pattern=", "--top-level-directory=")):
                targets.append(value)
            index += 1
        return ("unittest" if mode == "unittest" else "pytest", tuple(sorted(set(targets)))) if targets else None
    if mode in {"node", "nodejs", "bash", "sh", "pwsh", "powershell"}:
        if parts and parts[0] in {"-File", "-file"} and mode in {"pwsh", "powershell"}:
            parts.pop(0)
        if parts and not parts[0].startswith("-"):
            return (mode, os.path.normcase(os.path.normpath(parts[0])))
    return None


def _targets_compatible(first, second):
    a, b = _targets(first), _targets(second)
    common = a.keys() & b.keys()
    contexts = {"workspace", "cwd", "workdir", "project_path"}
    if ({key: value for key, value in a.items() if key.rsplit(".", 1)[-1].lower() in contexts}
            != {key: value for key, value in b.items() if key.rsplit(".", 1)[-1].lower() in contexts}):
        # An omitted directory means host default, not the other call's cwd.
        return False
    if any(a[key] != b[key] for key in common):
        return False
    # 共享 cwd/workspace 不能证明两条命令相关；明确资源或相同/同目标命令才形成潜在恢复。
    if any(key.rsplit(".", 1)[-1].lower() not in contexts
           for key in common):
        return True
    if a and b and not common:
        return False
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    for key in ("command", "cmd", "CommandLine"):
        c1, c2 = first.get(key), second.get(key)
        if isinstance(c1, str) and c1.strip() and isinstance(c2, str) and c2.strip():
            if c1.strip() == c2.strip():
                return True
            target = _command_target(c1)
            if target is not None and target == _command_target(c2):
                return True
    return False


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
            failure_input = raw.get("tool_input")
            if failure_input is None and attempts:
                failure_input = _payload(attempts[0]).get("tool_input")
            prior_recovered = self.db.execute(
                "SELECT p.id AS problem_id,e.* FROM problems p JOIN problem_events pe ON pe.problem=p.id "
                "JOIN events e ON e.id=pe.event JOIN operations o ON o.project=e.project AND o.id=e.operation "
                "WHERE p.project=? AND p.conversation=? AND p.task_id IS ? AND p.task_revision IS ? "
                "AND (p.resolution_status='recovery_observed' OR p.knowledge_id IS NOT NULL OR EXISTS "
                "(SELECT 1 FROM knowledge_sources ks WHERE ks.project=p.project AND ks.problem_id=p.id)) "
                "AND e.type='PostToolUse' AND e.outcome='failed' AND e.agent IS ? "
                "AND e.assignment_id IS ? AND o.host_epoch=? "
                "AND json_extract(e.payload,'$.tool_name')=? "
                "ORDER BY (EXISTS (SELECT 1 FROM knowledge_sources ks WHERE ks.project=p.project "
                "AND ks.problem_id=p.id)) DESC,e.created DESC,e.id DESC LIMIT 10",
                (project, conversation, event.get("task_id"), event.get("task_revision"),
                 agent_id, event.get("assignment_id"), epoch, raw.get("tool_name")),
            ).fetchall()
            problem = None
            for prior_row in prior_recovered:
                prior_dict = dict(prior_row)
                prior_input = _payload(prior_dict).get("tool_input")
                if prior_input is None:
                    prior_attempts = self._operation_attempts(project, prior_dict["operation"])
                    if prior_attempts:
                        prior_input = _payload(prior_attempts[0]).get("tool_input")
                if _targets_compatible(prior_input, failure_input):
                    regressed_refs = [(item["id"], "attempt") for item in attempts] + [
                        (event["id"], "conflict" if outcome == "conflict" else "failure")
                    ]
                    problem = self._attach_case_evidence(prior_dict["problem_id"], regressed_refs, resolution_status="open")
                    break
            if problem is None:
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
        if outcome != "succeeded":
            return
        failures = self.db.execute(
            "SELECT p.id AS problem_id,e.* FROM problems p JOIN problem_events pe ON pe.problem=p.id "
            "JOIN events e ON e.id=pe.event JOIN operations o ON o.project=e.project AND o.id=e.operation "
            "WHERE p.project=? AND p.conversation=? AND p.task_id IS ? AND p.task_revision IS ? "
            "AND e.type='PostToolUse' AND e.outcome='failed' AND e.agent IS ? "
            "AND e.assignment_id IS ? AND o.host_epoch=? "
            "AND json_extract(e.payload,'$.tool_name')=? "
            "ORDER BY e.created DESC,e.id DESC LIMIT 20",
            (project, conversation, event.get("task_id"), event.get("task_revision"), agent_id,
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
            recoveries = self.db.execute(
                "SELECT observed.* FROM problem_events recovery JOIN events observed ON observed.id=recovery.event "
                "JOIN operations ro ON ro.project=observed.project AND ro.id=observed.operation "
                "WHERE recovery.problem=? AND recovery.role='potential_recovery' AND observed.created>=? "
                "AND observed.agent IS ? AND observed.assignment_id IS ? AND observed.outcome='succeeded' "
                "AND ro.host_epoch=? AND ro.conflict=0 AND ro.decision!='denied' AND ro.observation='succeeded' "
                "ORDER BY observed.created DESC LIMIT 20",
                (failure["problem_id"], failure["created"], agent_id, event.get("assignment_id"), epoch)).fetchall()
            related = False
            latest_recovery_time = None
            for recovery in recoveries:
                recovery = dict(recovery)
                recovery_input = _payload(recovery).get("tool_input")
                if recovery_input is None:
                    recovery_attempts = self._operation_attempts(project, recovery["operation"])
                    if recovery_attempts:
                        recovery_input = _payload(recovery_attempts[0]).get("tool_input")
                if _targets_compatible(failure_input, recovery_input):
                    related = True
                    latest_recovery_time = recovery["created"]
                    break
            if related and latest_recovery_time is not None:
                intervening_change = self.db.execute(
                    "SELECT 1 FROM events e WHERE e.project=? AND e.conversation=? AND e.task_id IS ? AND e.task_revision IS ? "
                    "AND e.agent IS ? AND e.created>=? AND e.id!=? AND ("
                    "e.outcome IN ('failed','conflict') OR "
                    "LOWER(COALESCE(json_extract(e.payload,'$.tool_name'),'')) IN ('edit','write','apply_patch','str_replace_editor')"
                    ") LIMIT 1",
                    (project, conversation, event.get("task_id"), event.get("task_revision"),
                     agent_id, latest_recovery_time, event["id"]),
                ).fetchone()
                if not intervening_change:
                    continue
            since_time = latest_recovery_time if latest_recovery_time is not None else failure["created"]
            edits = self.db.execute(
                "SELECT e.id FROM events e WHERE e.project=? AND e.conversation=? AND e.task_id IS ? AND e.task_revision IS ? "
                "AND e.agent IS ? AND e.type='PostToolUse' AND e.outcome IN ('succeeded','call_completed') "
                "AND e.created>=? AND e.id!=? "
                "AND LOWER(COALESCE(json_extract(e.payload,'$.tool_name'),'')) IN ('edit','write','apply_patch','str_replace_editor') "
                "ORDER BY e.created DESC,e.id DESC LIMIT 3",
                (project, conversation, event.get("task_id"), event.get("task_revision"),
                 agent_id, since_time, event["id"]),
            ).fetchall()
            edit_refs = [(row["id"], "observation") for row in reversed(edits)]
            refs = edit_refs + [(item["id"], "attempt") for item in attempts] + [(event["id"], "potential_recovery")]
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
