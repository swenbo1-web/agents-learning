"""操作者签发执行范围；任务描述不能自行扩大执行权限。

仅判断声明工具和可解析的资源字段，不把任意 Shell 文本视为可验证的读操作。
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import time


_ACTIONS = {"read", "write", "execute", "network"}
_SCOPE_MODES = {"analysis", "read_only", "implementation", "execute"}
_RULE_KEYS = {"mode", "allowed_tools", "tool_rules", "allowed_paths", "budget", "workspace"}
_SHELL_TOOLS = {"bash", "shell", "powershell", "cmd", "exec_command", "functions.exec_command", "run_command"}
_SCOPE_ROOTS_KEY = "_scope_path_roots"


def _error(message):
    # runtime 导入本模块作 Mixin；异常类型延迟导入避免循环。
    from .runtime import ContractError
    raise ContractError(message)


def _strings(value, name, nonempty=False):
    if (not isinstance(value, list) or (nonempty and not value)
            or any(not isinstance(item, str) or not item.strip() or "\0" in item for item in value)):
        _error(f"{name} must be an array of nonempty strings")


def _budget(value, name, strict=False):
    if not isinstance(value, dict) or "remaining" not in value:
        _error(f"{name} must contain remaining")
    if strict and set(value) - {"remaining", "unit"}:
        _error(f"{name} contains unsupported fields")
    remaining = value["remaining"]
    if remaining is not None and (isinstance(remaining, bool) or not isinstance(remaining, (int, float))
                                  or (isinstance(remaining, float) and not math.isfinite(remaining)) or remaining < 0):
        _error(f"{name}.remaining must be a finite nonnegative number or null")
    if "unit" in value and (not isinstance(value["unit"], str) or not value["unit"].strip()):
        _error(f"{name}.unit must be a nonempty string")


def validate_scope(scope):
    """已知字段严格校验；保留原有项目附加字段，不将其当成授权。"""
    if not isinstance(scope, dict):
        _error("scope must be an object")
    if "mode" in scope and (not isinstance(scope["mode"], str) or scope["mode"] not in _SCOPE_MODES):
        _error("scope.mode must be analysis, read_only, implementation or execute")
    for name in ("allowed_paths", "allowed_tools", "stop_conditions", "verification"):
        if name in scope:
            _strings(scope[name], f"scope.{name}")
    if "allowed_actions" in scope:
        _strings(scope["allowed_actions"], "scope.allowed_actions")
        if set(scope["allowed_actions"]) - _ACTIONS:
            _error("scope.allowed_actions contains unsupported actions")
    if "budget" in scope:
        _budget(scope["budget"], "scope.budget")
    return copy.deepcopy(scope)


def _freeze_roots(roots, workspace):
    try:
        return [str((Path(root) if Path(root).is_absolute()
                     else Path(workspace) / root).resolve()) for root in roots]
    except (OSError, TypeError, ValueError, RuntimeError):
        _error("execution allowed_paths cannot be resolved")


def _frozen_scope(scope, workspace):
    if "allowed_paths" not in scope:
        return None
    return {"paths": copy.deepcopy(scope["allowed_paths"]),
            "roots": _freeze_roots(scope["allowed_paths"], workspace)}


def _approved_scope_roots(rules, scope, assignment_id=None):
    """只读审批派生的根；scope 自身的附加字段不能提供冻结根。"""
    frozen = rules.get(_SCOPE_ROOTS_KEY)
    if not isinstance(frozen, dict) or frozen.get("version") != 1:
        return None
    parent = frozen.get("task")
    entry = parent
    if assignment_id is not None and (not isinstance(parent, dict)
                                     or parent.get("paths") != scope["allowed_paths"]):
        assignments = frozen.get("assignments")
        entry = assignments.get(assignment_id) if isinstance(assignments, dict) else None
    if (not isinstance(entry, dict) or entry.get("paths") != scope["allowed_paths"]
            or not isinstance(entry.get("roots"), list)
            or len(entry["roots"]) != len(scope["allowed_paths"])
            or any(not isinstance(root, str) or not root or "\0" in root
                   or not Path(root).is_absolute() for root in entry["roots"])):
        return None
    return entry["roots"]


def validate_rules(rules, workspace=None, scope=None):
    """签发时冻结工作目录，禁止按未来工具的 cwd 重新解释相对允许根。"""
    if not isinstance(rules, dict) or set(rules) - _RULE_KEYS:
        _error("execution rules contain unsupported fields")
    if not isinstance(rules.get("mode"), str) or rules["mode"] not in {"read_only", "execute"}:
        _error("execution rules.mode must be read_only or execute")
    _strings(rules.get("allowed_tools"), "execution rules.allowed_tools", nonempty=True)
    mapping = rules.get("tool_rules")
    if not isinstance(mapping, dict) or not mapping:
        _error("execution rules.tool_rules must be a nonempty object")
    for name, rule in mapping.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(rule, dict):
            _error("tool_rules must map nonempty tool names to rule objects")
        if (set(rule) - {"action", "path_fields"} or not isinstance(rule.get("action"), str)
                or rule["action"] not in _ACTIONS):
            _error("tool rules require a supported action and optional path_fields")
        if "path_fields" in rule:
            _strings(rule["path_fields"], "tool rule.path_fields")
            if any(any(not part for part in field.split(".")) for field in rule["path_fields"]):
                _error("path_fields must use nonempty dot field components")
    if set(rules["allowed_tools"]) - set(mapping):
        _error("each allowed tool must have a tool rule")
    if "allowed_paths" in rules:
        _strings(rules["allowed_paths"], "execution rules.allowed_paths")
    if "budget" in rules:
        _budget(rules["budget"], "execution rules.budget", strict=True)
    result = copy.deepcopy(rules)
    rule_workspace = result.get("workspace")
    bases = []
    for item in (workspace, rule_workspace):
        if item is not None:
            if not isinstance(item, (str, os.PathLike)) or not str(item).strip() or not Path(item).is_absolute():
                _error("execution workspace must be an absolute path")
            try:
                bases.append(Path(item).resolve())
            except (OSError, ValueError, RuntimeError):
                _error("execution workspace cannot be resolved")
    if len(bases) == 2 and os.path.normcase(str(bases[0])) != os.path.normcase(str(bases[1])):
        _error("execution workspace disagrees with rules.workspace")
    roots = result.get("allowed_paths", []) + (scope or {}).get("allowed_paths", [])
    if any(not Path(root).is_absolute() for root in roots) and not bases:
        _error("relative allowed_paths require an absolute execution workspace")
    if bases:
        result["workspace"] = str(bases[0])
    else:
        result.pop("workspace", None)
    if "allowed_paths" in result:
        # 审批固定实际允许根；随后根自身被替换为 symlink 也不能扩大许可。
        result["allowed_paths"] = _freeze_roots(result["allowed_paths"], result.get("workspace"))
    result[_SCOPE_ROOTS_KEY] = {"version": 1,
                              "task": _frozen_scope(scope or {}, result.get("workspace")),
                              "assignments": {}}
    return result


def _field(value, dotted):
    for part in dotted.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def _paths(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value or any(
            not isinstance(item, str) or not item.strip() or "\0" in item for item in value):
        return None
    # 不依靠工具解释遍历语义；含 .. 的资源参数直接拒绝。
    if any(".." in Path(item).parts for item in value):
        return None
    return value


def _within(path, roots):
    for root in roots:
        try:
            path.relative_to(Path(root))
            return True
        except ValueError:
            continue
    return False


class ExecutionMixin:
    def _execution_policy(self, project, conversation_id):
        row = self.db.execute("SELECT * FROM execution_policies WHERE project=? AND conversation=?",
                              (project, conversation_id)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["rules"] = json.loads(item["rules"])
        return item

    def execution_status(self, project, conversation_id):
        """仅返回当前执行准备状态；worker 没有签发权限的 MCP 工具。"""
        task = self.task(project, conversation_id)
        policy = self._execution_policy(project, conversation_id)
        reason = "ready"
        if task is None:
            reason = "task_required"
        elif task["status"] != "active":
            reason = "task_inactive"
        elif policy is None:
            reason = "execution_approval_required"
        elif policy["host_epoch"] != self.session_epoch():
            reason = "account_context_changed"
        elif (policy["task_id"], policy["task_revision"], policy["execution_epoch"]) != (
                task["id"], task["revision"], task["execution_epoch"]):
            reason = "execution_approval_stale"
        elif ("allowed_paths" in task["scope"]
              and _approved_scope_roots(policy["rules"], task["scope"]) is None):
            reason = "execution_scope_approval_required"
        elif any(source.get("budget", {}).get("remaining") == 0 for source in (policy["rules"], task["scope"])):
            reason = "execution_budget_exhausted"
        return {"ready": reason == "ready", "reason": reason, "policy": policy}

    def approve_execution(self, project, conversation_id, expected_task_id, expected_write_version,
                          rules, expected_epoch=None, workspace=None):
        """仅受信 CLI/宿主调用。与 Agent 可编辑的 task scope 分开存储。"""
        from .runtime import encode
        if not isinstance(expected_task_id, str) or not expected_task_id:
            _error("expected_task_id is required for execution approval")
        if isinstance(expected_write_version, bool) or not isinstance(expected_write_version, int) or expected_write_version < 1:
            _error("expected_write_version is required for execution approval")
        with self.transaction():
            epoch = self.assert_session_epoch(expected_epoch)
            self.bind_session(project, conversation_id)
            task = self.task(project, conversation_id)
            if not task or (task["id"], task["write_version"]) != (expected_task_id, expected_write_version):
                _error("stale execution approval task identity or write version")
            if task["status"] != "active":
                _error("execution approval requires an active task")
            trusted = validate_rules(rules, workspace, task["scope"])
            # 同一合同中已有的子范围也在受信审批时解析。与父范围相同的
            # 子任务直接继承父冻结根；审批后派发也不重新解析目录链接。
            for row in self.db.execute(
                    "SELECT id,scope FROM assignments WHERE project=? AND conversation=? "
                    "AND task_id=? AND task_revision=? AND execution_epoch=? AND host_epoch=?",
                    (project, conversation_id, task["id"], task["revision"], task["execution_epoch"], epoch)):
                scope = validate_scope(json.loads(row["scope"]))
                if ("allowed_paths" in scope
                        and scope["allowed_paths"] != task["scope"].get("allowed_paths")):
                    if (any(not Path(root).is_absolute() for root in scope["allowed_paths"])
                            and not trusted.get("workspace")):
                        _error("relative allowed_paths require an absolute execution workspace")
                    trusted[_SCOPE_ROOTS_KEY]["assignments"][row["id"]] = _frozen_scope(
                        scope, trusted.get("workspace"))
            previous = self._execution_policy(project, conversation_id)
            current_binding = (task["id"], task["revision"], task["execution_epoch"], epoch)
            previous_binding = tuple(previous[key] for key in (
                "task_id", "task_revision", "execution_epoch", "host_epoch")) if previous else None
            # 第一份许可可由已有有效 assignment 继承；替换当前规则使旧许可失效。
            if previous and previous_binding == current_binding and previous["rules"] != trusted:
                self.db.execute("UPDATE tasks SET execution_epoch=execution_epoch+1,write_version=write_version+1,updated=? "
                                "WHERE project=? AND conversation=?", (time.time(), project, conversation_id))
                task = self.task(project, conversation_id)
                self._write_task_state(task)
            now = time.time()
            self.db.execute("INSERT INTO execution_policies(project,conversation,task_id,task_revision,execution_epoch,host_epoch,rules,created) "
                            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project,conversation) DO UPDATE SET "
                            "task_id=excluded.task_id,task_revision=excluded.task_revision,execution_epoch=excluded.execution_epoch,"
                            "host_epoch=excluded.host_epoch,rules=excluded.rules,created=excluded.created",
                            (project, conversation_id, task["id"], task["revision"], task["execution_epoch"], epoch, encode(trusted), now))
            self._event(project, conversation_id, "ExecutionApproval",
                        {"rules": trusted, "write_version": task["write_version"], "execution_epoch": task["execution_epoch"]},
                        task_id=task["id"], task_revision=task["revision"])
            return {"task": task, **self.execution_status(project, conversation_id)}

    def check_execution(self, project, conversation, task, raw, assignment=None):
        """必须在 Hook 的事务内调用；决定只代表该次 Pre，不保证宿主撤销运行中命令。"""
        status = self.execution_status(project, conversation)
        policy = status["policy"]

        def result(reason=None, **extra):
            return {"decision": "denied" if reason else "allowed", "reason": reason or "allowed", "policy": policy, **extra}

        if not status["ready"]:
            return result(status["reason"])
        # 调用者绑定的 task 必须与数据库一致，不能使用旧快照搭配新许可。
        current = self.task(project, conversation)
        if not task or any(task.get(key) != current[key] for key in ("id", "revision", "execution_epoch")):
            return result("execution_task_stale")
        scopes = [current["scope"]]
        if assignment is not None:
            if any(assignment.get(key) != current[column] for key, column in (
                    ("task_id", "id"), ("task_revision", "revision"), ("execution_epoch", "execution_epoch"))):
                return result("assignment_stale")
            if assignment.get("scope") is not None:
                try:
                    scopes.append(validate_scope(assignment["scope"]))
                except ValueError:
                    return result("assignment_scope_invalid")
        rules = policy["rules"]
        tool = raw.get("tool_name")
        if not isinstance(tool, str) or not tool:
            return result("tool_name_required")
        global_tools = self.config.get("allowed_tools", [])
        if global_tools and tool not in global_tools:
            return result("tool_disallowed_by_config")
        if tool not in rules["allowed_tools"] or tool not in rules["tool_rules"]:
            return result("tool_not_approved")
        rule = rules["tool_rules"][tool]
        action = rule["action"]
        shell = str(tool).lower() in _SHELL_TOOLS or str(tool).lower().endswith("__exec_command")
        readonly = rules["mode"] == "read_only" or any(scope.get("mode") in {"analysis", "read_only"} for scope in scopes)
        if readonly and (action != "read" or shell):
            return result("read_only_action_denied", action=action)
        for scope in scopes:
            if "allowed_tools" in scope and tool not in scope["allowed_tools"]:
                return result("tool_disallowed_by_scope")
            if "allowed_actions" in scope and action not in scope["allowed_actions"]:
                return result("action_disallowed_by_scope", action=action)
            if scope.get("budget", {}).get("remaining") == 0:
                return result("execution_budget_exhausted")
        restrictions = [rules["allowed_paths"]] if "allowed_paths" in rules else []
        for index, scope in enumerate(scopes):
            if "allowed_paths" not in scope:
                continue
            assignment_id = None
            if index:
                assignment_id = assignment.get("assignment_id", assignment.get("id")) or ""
            roots = _approved_scope_roots(rules, scope, assignment_id)
            if roots is None:
                return result("execution_scope_approval_required")
            restrictions.append(roots)
        fields = rule.get("path_fields", [])
        if restrictions and not fields:
            return result("resource_fields_required")
        resources = []
        try:
            cwd = raw.get("cwd")
            if fields and (not isinstance(cwd, str) or not Path(cwd).is_absolute()):
                return result("resource_cwd_required")
            for field in fields:
                values = _paths(_field(raw.get("tool_input", {}), field))
                if values is None:
                    return result("resource_field_invalid", field=field)
                for value in values:
                    path = Path(value)
                    path = (path if path.is_absolute() else Path(cwd) / path).resolve()
                    if any(not _within(path, roots) for roots in restrictions):
                        return result("resource_outside_approved_roots", field=field)
                    resources.append(str(path))
        except (OSError, ValueError, RuntimeError):
            return result("resource_path_unresolvable")
        return result(action=action, resources=resources)
