"""管理员的项目／宿主策略与可观察模型路由；不启动模型或读取登录凭证。"""
from __future__ import annotations

import contextlib
import json
import re
import time

from .host_sessions import host_scope_key


ROUTING_DEFAULTS = {
    "provider": "host", "model_mode": "default", "model": "",
    "antigravity_command": ["agy"], "dsh_command": ["dsh"],
}
_PROVIDERS = {"host", "codex", "antigravity", "dsh"}
_MODES = {"default", "current", "fixed"}
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")
_MODEL_CREDENTIAL = re.compile(r"(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{25,})")
_SECRET_ARGUMENT = re.compile(r"(?i)(?:api[_-]?key|password|secret|access[_-]?token|refresh[_-]?token|authorization|bearer|github_pat_|gh[pousr]_|sk-(?:proj-|ant-)?[a-z0-9_-]{16,})")
_MODEL_ARGUMENTS = {"-m", "--model", "--model-id", "--provider"}


def _error(message):
    from .runtime import ContractError
    raise ContractError(message)


def _named(value, allowed):
    return isinstance(value, str) and value in allowed


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    from .runtime import digest
    return digest(value)


def validate_model_identifier(model, *, allow_empty=False):
    if model == "" and allow_empty:
        return model
    if not isinstance(model, str) or not _MODEL.fullmatch(model) or _MODEL_CREDENTIAL.search(model):
        _error("administrator model must be a bounded identifier without whitespace or leading options")
    return model


def validate_executor_command(command):
    if (not isinstance(command, list) or not 1 <= len(command) <= 16
            or any(not isinstance(value, str) or not value.strip() or len(value) > 2048
                   or any(ord(character) < 32 for character in value) for value in command)):
        _error("administrator command must be a bounded executable argument array")
    if command[0].startswith("-"):
        _error("administrator executable cannot be an option")
    for argument in command:
        if _SECRET_ARGUMENT.search(argument) or re.search(r"https?://[^/\s]*@", argument, re.I):
            _error("administrator command cannot contain credentials")
    for argument in command[1:]:
        if argument in _MODEL_ARGUMENTS or any(argument.startswith(name + "=") for name in _MODEL_ARGUMENTS):
            _error("administrator model selection must use the routing policy")
    return list(command)


def validate_admin_routing_config(config):
    """保留管理员预算字段，补齐并检查路由字段；默认不打开 enabled。"""
    if not isinstance(config, dict):
        _error("administrator configuration must be an object")
    result = {**ROUTING_DEFAULTS, **config}
    if not _named(result["provider"], _PROVIDERS) or not _named(result["model_mode"], _MODES):
        _error("unsupported administrator provider or model mode")
    validate_model_identifier(result["model"], allow_empty=result["model_mode"] != "fixed")
    if result["model_mode"] != "fixed" and result["model"]:
        _error("administrator model override requires fixed mode")
    result["antigravity_command"] = validate_executor_command(result["antigravity_command"])
    result["dsh_command"] = validate_executor_command(result["dsh_command"])
    if "command" in result:
        result["command"] = validate_executor_command(result["command"])
    if "enabled" in result and not isinstance(result["enabled"], bool):
        _error("administrator enabled must be a boolean")
    return result


def _default_provider_command(config, provider):
    if provider == "codex":
        return config.get("command", ["codex"])
    if provider == "antigravity":
        return config["antigravity_command"]
    if provider == "dsh":
        return config["dsh_command"]
    _error("unsupported administrator provider")


def install_admin_routing(runtime):

    """由 Runtime schema 迁移在已有写事务内调用。"""
    if not runtime.db.in_transaction:
        _error("administrator routing storage requires an existing write transaction")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS admin_host_policies(
        project TEXT NOT NULL,host_scope TEXT NOT NULL,enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        provider TEXT NOT NULL,model_mode TEXT NOT NULL,model TEXT NOT NULL,command TEXT,
        version INTEGER NOT NULL CHECK(version>=1),session_epoch INTEGER NOT NULL,
        governance_epoch INTEGER NOT NULL,updated REAL NOT NULL,
        PRIMARY KEY(project,host_scope))""")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS admin_model_observations(
        project TEXT NOT NULL,host_scope TEXT NOT NULL,conversation TEXT NOT NULL,
        session_epoch INTEGER NOT NULL,model TEXT NOT NULL,active INTEGER NOT NULL CHECK(active IN (0,1)),
        version INTEGER NOT NULL CHECK(version>=1),observed REAL NOT NULL,
        PRIMARY KEY(project,host_scope,conversation))""")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS admin_model_scope ON admin_model_observations(project,host_scope,session_epoch,active)")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS admin_execution_plans(
        call_id TEXT PRIMARY KEY,project TEXT NOT NULL,host_scope TEXT NOT NULL,
        body TEXT NOT NULL,created REAL NOT NULL)""")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS admin_execution_project ON admin_execution_plans(project,host_scope,created)")


def _transaction(runtime):
    return contextlib.nullcontext() if runtime.db.in_transaction else runtime.transaction()


@contextlib.contextmanager
def _read_snapshot(runtime):
    if runtime.db.in_transaction:
        yield
        return
    runtime.db.execute("BEGIN")
    try:
        yield
        runtime.db.execute("COMMIT")
    except BaseException:
        runtime.db.execute("ROLLBACK")
        raise


class AdminRoutingMixin:
    def admin_policy(self, project, *, host_scope=None):
        """只读策略；已有显式全局设置保持兼容，宿主策略可单独覆盖。"""
        if not isinstance(project, str) or not project.strip():
            _error("administrator routing requires a project identity")
        scope = self._session_scope() if host_scope is None else host_scope
        if not isinstance(scope, str) or ":" not in scope:
            _error("invalid administrator host scope")
        host, instance = scope.split(":", 1)
        host_scope_key(host, instance)
        config = validate_admin_routing_config(self.config["admin"])
        row = self.db.execute("SELECT * FROM admin_host_policies WHERE project=? AND host_scope=?", (project, scope)).fetchone()
        if row:
            policy = {"project": project, "host": host, "host_scope": scope, "enabled": bool(row["enabled"]),
                      "provider": row["provider"], "model_mode": row["model_mode"], "model": row["model"],
                      "command": json.loads(row["command"]) if row["command"] else None,
                      "policy_version": row["version"], "source": "operator_policy",
                      "configured_session_epoch": row["session_epoch"],
                      "configured_governance_epoch": row["governance_epoch"], "updated": row["updated"]}
            validate_admin_routing_config({**config, **{key: policy[key] for key in ("enabled", "provider", "model_mode", "model")}})
            if policy["command"] is not None:
                validate_executor_command(policy["command"])
            return policy
        return {"project": project, "host": host, "host_scope": scope,
                "enabled": bool(config.get("enabled", False)),
                "provider": config["provider"], "model_mode": config["model_mode"], "model": config["model"],
                "command": None, "policy_version": 0, "source": "legacy_config",
                "configured_session_epoch": None, "configured_governance_epoch": None, "updated": None}

    def admin_configure(self, project, enabled, provider="host", model_mode="default", model="", command=None,
                        *, expected_policy_version=None, expected_epoch=None):
        """受信操作者入口；不加入普通或管理员 MCP 的工具集合。"""
        if not isinstance(enabled, bool):
            _error("administrator enabled must be a boolean")
        selected = validate_admin_routing_config({"enabled": enabled, "provider": provider, "model_mode": model_mode, "model": model})
        selected_command = validate_executor_command(command) if command is not None else None
        if expected_policy_version is not None and (isinstance(expected_policy_version, bool)
                or not isinstance(expected_policy_version, int) or expected_policy_version < 0):
            _error("administrator expected policy version must be nonnegative")
        with _transaction(self):
            epoch = self.assert_session_epoch(expected_epoch)
            existing = self.admin_policy(project)
            if selected_command is None:
                config = validate_admin_routing_config(self.config["admin"])
                target = self.host if provider == "host" else provider
                prior_target = self.host if existing["provider"] == "host" else existing["provider"]
                selected_command = (existing["command"] if existing["command"] is not None and prior_target == target
                                    else _default_provider_command(config, target))
                selected_command = validate_executor_command(selected_command)
            if expected_policy_version is not None and existing["policy_version"] != expected_policy_version:
                _error("administrator policy changed; reread before configuring")
            same = (existing["source"] == "operator_policy" and all(existing[key] == selected[key] for key in
                    ("enabled", "provider", "model_mode", "model")) and existing["command"] == selected_command)
            if not same:
                version = existing["policy_version"] + 1
                self.db.execute("INSERT INTO admin_host_policies VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                                "ON CONFLICT(project,host_scope) DO UPDATE SET enabled=excluded.enabled,provider=excluded.provider,"
                                "model_mode=excluded.model_mode,model=excluded.model,command=excluded.command,version=excluded.version,"
                                "session_epoch=excluded.session_epoch,governance_epoch=excluded.governance_epoch,updated=excluded.updated",
                                (project, self._session_scope(), int(enabled), provider, model_mode, model,
                                 _encode(selected_command) if selected_command is not None else None, version, epoch, self.host_epoch(), time.time()))
            result = self.admin_policy(project)
            return {**result, "changed": not same}

    def admin_observe_model(self, project, conversation, model=None, *, agent_id=None, active=True, expected_epoch=None):
        """宿主事件提供的根会话观察；缺失模型不能推定继承另一个会话。"""
        if not isinstance(project, str) or not project.strip() or not isinstance(conversation, str) or not conversation.strip() or len(conversation) > 2000:
            _error("model observation requires project and conversation identities")
        if not isinstance(active, bool):
            _error("model observation active must be a boolean")
        if agent_id is not None:
            return {"recorded": False, "reason": "child_model_not_root"}
        if model is not None:
            validate_model_identifier(model)
        if active and model is None:
            return {"recorded": False, "reason": "model_not_observed"}
        with _transaction(self):
            epoch = self.assert_session_epoch(expected_epoch)
            scope = self._session_scope()
            row = self.db.execute("SELECT * FROM admin_model_observations WHERE project=? AND host_scope=? AND conversation=?",
                                  (project, scope, conversation)).fetchone()
            if not active and model is None:
                if row is None or row["session_epoch"] != epoch:
                    return {"recorded": False, "reason": "root_observation_absent"}
                model = row["model"]
            same = row is not None and row["session_epoch"] == epoch and row["model"] == model and bool(row["active"]) == active
            version = row["version"] if same else row["version"] + 1 if row else 1
            if not same:
                self.db.execute("INSERT INTO admin_model_observations VALUES(?,?,?,?,?,?,?,?) "
                                "ON CONFLICT(project,host_scope,conversation) DO UPDATE SET session_epoch=excluded.session_epoch,"
                                "model=excluded.model,active=excluded.active,version=excluded.version,observed=excluded.observed",
                                (project, scope, conversation, epoch, model, int(active), version, time.time()))
            return {"recorded": True, "changed": not same, "host_scope": scope, "session_epoch": epoch,
                    "conversation": conversation, "model": model, "active": active, "version": version}

    def resolve_admin_executor(self, project, conversation=None, *, require_enabled=True):
        """从当前宿主策略解析执行器；current 仅使用已核对的根会话观察。"""
        with _read_snapshot(self):
            return self._resolve_admin_executor_snapshot(project, conversation, require_enabled=require_enabled)

    def _resolve_admin_executor_snapshot(self, project, conversation=None, *, require_enabled=True):
        if not isinstance(require_enabled, bool):
            _error("administrator require_enabled must be a boolean")
        if conversation is not None and (not isinstance(conversation, str) or not conversation.strip()):
            _error("administrator conversation must be an explicit identity")
        policy = self.admin_policy(project)
        if require_enabled and not policy["enabled"]:
            _error("administrator_disabled: enable the current project and host explicitly")
        provider = self.host if policy["provider"] == "host" else policy["provider"]
        config = validate_admin_routing_config(self.config["admin"])
        command = policy["command"] or _default_provider_command(config, provider)
        command = validate_executor_command(command)
        session_epoch, governance_epoch = self.session_epoch(), self.host_epoch()
        model, source, observation = "", "provider_cli_default", None
        if policy["model_mode"] == "fixed":
            model, source = validate_model_identifier(policy["model"]), "operator_fixed"
        elif policy["model_mode"] == "current":
            if provider != self.host:
                _error("administrator_current_model_provider_mismatch: current model belongs to the observing host")
            args = (project, self._session_scope(), session_epoch)
            sql = "SELECT * FROM admin_model_observations WHERE project=? AND host_scope=? AND session_epoch=? AND active=1"
            if conversation is not None:
                sql += " AND conversation=?"
                args += (conversation,)
            rows = self.db.execute(sql + " ORDER BY conversation", args).fetchall()
            if not rows:
                _error("administrator_current_model_unavailable: no active root model observation")
            models = {row["model"] for row in rows}
            if len(models) != 1:
                _error("administrator_current_model_ambiguous: specify the source conversation")
            model = validate_model_identifier(next(iter(models)))
            source = "host_observation"
            observation = _digest([{key: row[key] for key in ("conversation", "session_epoch", "model", "active", "version")} for row in rows])
        result = {"project": project, "enabled": policy["enabled"], "provider": provider,
                  "provider_selection": policy["provider"], "model": model, "model_mode": policy["model_mode"],
                  "model_source": source, "command": command, "policy_version": policy["policy_version"],
                  "policy_source": policy["source"], "host": self.host, "host_scope": self._session_scope(),
                  "session_epoch": session_epoch, "governance_epoch": governance_epoch,
                  "conversation": conversation, "model_observation_signature": observation}
        result["executor_signature"] = _digest(result)
        return result

    def record_admin_execution(self, project, call_id, snapshot, *, require_enabled=True):
        """认领事务内冻结模型路由；不改变用量预留或判断调用成功。"""
        if not self.db.in_transaction:
            _error("administrator execution plan requires an existing write transaction")
        if not isinstance(call_id, str) or not call_id.strip() or len(call_id) > 200:
            _error("administrator execution plan requires a bounded call identity")
        executor = self.assert_admin_executor_snapshot(project, snapshot, require_enabled=require_enabled)
        body = _encode(executor)
        row = self.db.execute("SELECT * FROM admin_execution_plans WHERE call_id=?", (call_id,)).fetchone()
        if row:
            if row["project"] != project or row["host_scope"] != self._session_scope() or row["body"] != body:
                _error("administrator call already has another immutable execution plan")
            return {"call_id": call_id, "project": project, "host_scope": row["host_scope"],
                    "executor": json.loads(row["body"]), "created": row["created"], "changed": False}
        created = time.time()
        self.db.execute("INSERT INTO admin_execution_plans VALUES(?,?,?,?,?)",
                        (call_id, project, self._session_scope(), body, created))
        return {"call_id": call_id, "project": project, "host_scope": self._session_scope(),
                "executor": executor, "created": created, "changed": True}

    def admin_execution_plan(self, project, call_id):
        """按准确项目／调用读取冻结计划，不推定当前仍可执行。"""
        row = self.db.execute("SELECT * FROM admin_execution_plans WHERE project=? AND call_id=?", (project, call_id)).fetchone()
        if not row:
            _error("administrator execution plan is absent in this project")
        return {"call_id": row["call_id"], "project": row["project"], "host_scope": row["host_scope"],
                "executor": json.loads(row["body"]), "created": row["created"]}

    def assert_admin_executor_snapshot(self, project, snapshot, conversation=None, *, require_enabled=True):
        """认领、启动和回写阶段复核策略、宿主边界与当前模型观察。"""
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("executor_signature"), str):
            _error("administrator executor snapshot is invalid")
        if snapshot.get("project") != project:
            _error("administrator executor snapshot belongs to another project")
        source_conversation = snapshot.get("conversation") if conversation is None else conversation
        if conversation is not None and source_conversation != snapshot.get("conversation"):
            _error("administrator executor snapshot belongs to another conversation")
        current = self.resolve_admin_executor(project, source_conversation, require_enabled=require_enabled)
        expected = {key: value for key, value in snapshot.items() if key != "executor_signature"}
        if _digest(expected) != snapshot["executor_signature"] or current != snapshot:
            _error("administrator_executor_changed: policy, host context or model observation changed")
        return current

    def admin_effective_config(self, project, conversation=None, *, require_enabled=False):
        """供既有预算／执行入口使用的副本，不更改全局配置。"""
        policy = self.admin_policy(project)
        if require_enabled and not policy["enabled"]:
            _error("administrator_disabled: enable the current project and host explicitly")
        provider = self.host if policy["provider"] == "host" else policy["provider"]
        configuration = validate_admin_routing_config(self.config["admin"])
        command = policy["command"] or _default_provider_command(configuration, provider)
        result = json.loads(_encode(self.config["admin"]))
        result.update(enabled=policy["enabled"], command=validate_executor_command(command), provider=provider,
                      model_mode=policy["model_mode"], model=policy["model"])
        # 全局排队／预算配置不能因无会话的 current 歧义而读取失败；实际认领
        # 必须按案例 conversation 调用 resolve，并核对完整 executor 快照。
        try:
            result["executor"] = self.resolve_admin_executor(project, conversation, require_enabled=require_enabled)
            result["routing_ready"], result["routing_reason"] = True, None
        except ValueError as exc:
            result["executor"] = None
            result["routing_ready"], result["routing_reason"] = False, str(exc).split(":", 1)[0]
        return result

    def admin_routing_status(self, project, conversation=None):
        policy = self.admin_policy(project)
        recent = []
        for row in self.db.execute("SELECT call_id,body,created FROM admin_execution_plans WHERE project=? AND host_scope=? "
                                   "ORDER BY created DESC,call_id LIMIT 8", (project, self._session_scope())).fetchall():
            body = json.loads(row["body"])
            recent.append({"call_id": row["call_id"], "created": row["created"],
                           **{key: body.get(key) for key in ("provider", "model", "model_mode", "model_source", "policy_version",
                                                             "session_epoch", "governance_epoch")}})
        try:
            executor = self.resolve_admin_executor(project, conversation, require_enabled=False)
            from .admin_providers import executor_availability
            availability = executor_availability(executor)
            return {"policy": policy, "executor": executor, "routing_ready": True,
                    "ready": availability["cli_available"], "availability": availability,
                    "reason": availability["reason"], "recent_calls": recent}
        except ValueError as exc:
            # 不回传潜在外部执行器错误文本或凭证，仅已定义的契约状态。
            reason = str(exc).split(":", 1)[0]
            return {"policy": policy, "executor": None, "routing_ready": False,
                    "ready": False, "availability": None, "reason": reason, "recent_calls": recent}
