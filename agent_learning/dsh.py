"""DSH (DeepSeek Harness) 原生事件与协议适配；只采集可观察消息与工具结果，丢弃 reasoning/thinking。

DSH 提供确定性的 SessionId、ToolCallId (callId) 与 Turn 序号；持久台账同时保护
幂等重放、会话重置 (dsh-session-reset)、跨账户隔离与 1.0.0 工作记忆/文件提示。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import time

from .runtime import ContractError, clean, digest, encode


EVENTS = {
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "Stop", "Interrupt", "SubagentStart", "SubagentStop",
}
MODES = {"observe", "enforce"}
PATH_FIELDS = (
    "cwd", "workdir", "file_path", "path", "filepath", "file", "filename",
    "Cwd", "AbsolutePath", "TargetFile", "DirectoryPath", "SearchDirectory", "SearchPath",
)
_BASE_RECOVERY_NAMES = (
    "knowledge_search", "knowledge_read", "work_memory_search", "work_memory_read",
    "session_context", "task_update", "case_feedback", "knowledge_use", "learning_status",
    "learning_history", "learning_browser",
)
RECOVERY_TOOLS = frozenset({
    *_BASE_RECOVERY_NAMES,
    *("mcp__agent_learning__" + name for name in _BASE_RECOVERY_NAMES),
    *("agent_learning_" + name for name in _BASE_RECOVERY_NAMES),
})
_WRITE_LEARNING_TOOLS = frozenset({
    "task_update", "case_feedback", "knowledge_use",
    "mcp__agent_learning__task_update", "mcp__agent_learning__case_feedback", "mcp__agent_learning__knowledge_use",
    "agent_learning_task_update", "agent_learning_case_feedback", "agent_learning_knowledge_use",
})
_DETERMINISTIC_LOCAL_TOOLS = frozenset({
    "read", "write", "edit", "glob", "grep", "read_image", "present", "todo_write",
})
_SHELL_EXEC_TOOLS = frozenset({
    "pwsh", "bash", "shell", "powershell", "cmd", "exec_command", "run_command",
})
_EXIT_CODE_MARKER = re.compile(r"\[exit code:\s*(-?\d+)\]\s*\Z", re.I)
_INTERNAL_BLOCK_TYPES = frozenset({"reasoning", "thinking", "redacted_thinking", "analysis", "reasoning-chunks"})


def install_storage(runtime):
    """由 Runtime 迁移事务调用；持久化 DSH 会话代次、幂等回执与无任务故障队列。"""
    if not runtime.db.in_transaction:
        raise ContractError("DSH storage installation requires an existing write transaction")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS dsh_sessions(
        project TEXT NOT NULL,conversation TEXT NOT NULL,generation INTEGER NOT NULL DEFAULT 1,
        last_turn INTEGER NOT NULL DEFAULT -1,last_step INTEGER NOT NULL DEFAULT -1,
        ambiguous INTEGER NOT NULL DEFAULT 0,reset_generation INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(project,conversation))""")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS dsh_receipts(
        project TEXT NOT NULL,conversation TEXT NOT NULL,event_key TEXT NOT NULL,
        kind TEXT NOT NULL,host_epoch INTEGER NOT NULL,generation INTEGER NOT NULL,
        raw_digest TEXT NOT NULL DEFAULT '',task_signature TEXT NOT NULL,
        output TEXT NOT NULL DEFAULT '{}',state TEXT NOT NULL DEFAULT 'pending',
        created REAL NOT NULL,PRIMARY KEY(project,conversation,event_key))""")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS dsh_faults(
        project TEXT NOT NULL,conversation TEXT NOT NULL,signature TEXT NOT NULL,
        host_epoch INTEGER NOT NULL,query TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
        created REAL NOT NULL,PRIMARY KEY(project,conversation,signature))""")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS dsh_receipt_raw ON dsh_receipts(project,conversation,raw_digest)")


def _absolute(value):
    if (not isinstance(value, str) or not value.strip() or "\0" in value
            or not Path(value).is_absolute()):
        raise ContractError("dsh_absolute_workspace_required")
    return Path(value).resolve()


def _path_key(value):
    return os.path.normcase(str(value))


def _within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _extract_observable_text(value):
    """只提取可观察文本块；显式丢弃 reasoning/thinking 块，绝不记录模型内部思考。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("type") in _INTERNAL_BLOCK_TYPES:
            return ""
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return value["text"]
        if value.get("type") == "text-chunks" and isinstance(value.get("texts"), list):
            return "".join(item for item in value["texts"] if isinstance(item, str))
        if "content" in value:
            return _extract_observable_text(value["content"])
        if "message" in value:
            return _extract_observable_text(value["message"])
        return ""
    if isinstance(value, list):
        parts = [_extract_observable_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    return ""


def _extract_model(raw):
    """提取根会话模型标识，支持 '<provider>/<model>' 或单模型 ID。"""
    context = raw.get("requestContext") or raw.get("request_context")
    if isinstance(context, dict):
        provider = context.get("provider")
        model = context.get("model")
        if isinstance(model, str) and model.strip():
            model = model.strip()
            if isinstance(provider, str) and provider.strip() and "/" not in model:
                return f"{provider.strip()}/{model}"[:200]
            return model[:200]
    for key in ("model", "modelName", "model_name"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            provider = raw.get("provider")
            text = value.strip()
            if isinstance(provider, str) and provider.strip() and "/" not in text:
                return f"{provider.strip()}/{text}"[:200]
            return text[:200]
    return None


def reset_session(runtime, workspace, native_conversation):
    """受信 CLI 专用；重置指定 DSH 会话代次，暂停任务并失效旧句柄、执行许可与文件提示。"""
    if runtime.host != "dsh":
        raise ContractError("dsh_host_required")
    project = runtime.project(_absolute(str(workspace)))
    conversation = runtime.host_conversation(native_conversation)
    with runtime.transaction():
        runtime.bind_session(project, conversation)
        runtime.admin_observe_model(project, conversation, active=False)
        runtime.db.execute("INSERT OR IGNORE INTO dsh_sessions(project,conversation) VALUES(?,?)", (project, conversation))
        runtime.db.execute("UPDATE dsh_sessions SET generation=generation+1,reset_generation=generation+1,"
                           "last_turn=-1,last_step=-1,ambiguous=0 WHERE project=? AND conversation=?",
                           (project, conversation))
        runtime.db.execute("DELETE FROM context_access WHERE project=? AND conversation=?", (project, conversation))
        for table in ("contexts", "subagent_contexts"):
            runtime.db.execute(f"UPDATE {table} SET epoch=epoch+1,injected='[]',last_query='',retrieval_signature='',retrieval_refs='[]' "
                               "WHERE project=? AND conversation=?", (project, conversation))
        runtime.db.execute("UPDATE dsh_faults SET state='superseded' WHERE project=? AND conversation=? AND state='pending'",
                           (project, conversation))
        runtime._execution_fence(project, conversation, "dsh_trusted_session_reset", pause=True)
        generation = runtime.db.execute("SELECT generation FROM dsh_sessions WHERE project=? AND conversation=?",
                                        (project, conversation)).fetchone()[0]
        runtime._event(project, conversation, "DshSessionReset", {"generation": generation})
        return {"project_id": project, "conversation_id": conversation, "generation": generation,
                "rebind_required": True, "execution_approval_required": True}


class DshAdapter:
    def __init__(self, runtime, workspace=None, hook_mode=None):
        self.runtime = runtime
        self.workspace = _absolute(str(workspace)) if workspace is not None else None
        self.mode = hook_mode or ("enforce" if runtime.config["enforce_task_policy"] else "observe")
        if self.mode not in MODES:
            raise ContractError("dsh_invalid_hook_mode")
        if getattr(runtime, "host", "dsh") != "dsh":
            raise ContractError("dsh_host_required")
        runtime.config["enforce_task_policy"] = self.mode == "enforce"

    def _epoch(self):
        return self.runtime.session_epoch()

    def _resolve_identities(self, raw):
        if not isinstance(raw, dict):
            raise ContractError("dsh_input_object_required")
        # 支持根会话与子 Agent 会话映射：若提供 rootSessionId/parentSessionId 且当前为子 Agent，
        # 归属到根会话 conversation 并以子会话作为 agent_id。
        native_session = raw.get("session_id") or raw.get("sessionId") or raw.get("conversationId")
        parent_session = raw.get("rootSessionId") or raw.get("root_session_id") or raw.get("parentSessionId") or raw.get("parent_session_id")
        agent_id = raw.get("agent_id") or raw.get("agentId") or raw.get("childSessionId")
        if parent_session is not None:
            if not isinstance(parent_session, str) or not parent_session.strip() or "\0" in parent_session or len(parent_session) > 200:
                raise ContractError("dsh_invalid_parent_session")
            if agent_id is None and isinstance(native_session, str) and native_session.strip() and native_session != parent_session:
                agent_id = native_session.strip()
            native_session = parent_session.strip()
        if not isinstance(native_session, str) or not native_session.strip() or "\0" in native_session or len(native_session) > 200:
            raise ContractError("dsh_session_required")
        if agent_id is not None:
            if not isinstance(agent_id, str) or not agent_id.strip() or "\0" in agent_id or len(agent_id) > 200:
                raise ContractError("dsh_invalid_agent_id")
            agent_id = agent_id.strip()
        return native_session.strip(), agent_id

    def _resolve_workspace(self, raw, event):
        cwd = raw.get("cwd") or raw.get("workspace")
        paths = raw.get("workspacePaths")
        if cwd is None and isinstance(paths, list) and paths:
            cwd = paths[0]
        if cwd is None:
            if self.workspace is not None:
                return self.workspace
            if event in {"SessionStart", "UserPromptSubmit", "Stop", "Interrupt"}:
                return None
            raise ContractError("dsh_workspace_required")
        resolved = _absolute(str(cwd))
        if self.workspace is not None and not _within(resolved, self.workspace):
            raise ContractError("dsh_workspace_outside_bound_project")
        return self.workspace or resolved

    def _normalize_tool_call(self, raw):
        tool_call = raw.get("toolCall") if isinstance(raw.get("toolCall"), dict) else {}
        name = raw.get("tool_name") or raw.get("name") or tool_call.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ContractError("dsh_tool_name_required")
        args = raw.get("tool_input") if "tool_input" in raw else (
            raw.get("arguments") if "arguments" in raw else (
                raw.get("args") if "args" in raw else tool_call.get("args", {})))
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError as exc:
                raise ContractError("dsh_tool_arguments_invalid_json") from exc
        if not isinstance(args, dict):
            raise ContractError("dsh_tool_arguments_must_be_object")
        call_id = raw.get("tool_use_id") or raw.get("callId") or raw.get("call_id") or tool_call.get("id")
        if not isinstance(call_id, str) or not call_id.strip() or "\0" in call_id or len(call_id) > 200:
            raise ContractError("dsh_tool_call_id_required")
        return name.strip(), args, call_id.strip()

    def _normalize_tool_response(self, tool_name, tool_args, raw):
        """将 DSH ToolExecutionResult 精确映射为 runtime.tool_outcome 可判定的响应结构。"""
        response = raw.get("tool_response") if "tool_response" in raw else raw.get("result", raw.get("toolResult"))
        if not isinstance(response, dict):
            raise ContractError("dsh_tool_response_object_required")
        # 若已经是标准 tool_response 且带 exit_code / session_id，保留并补充检查
        lower_name = tool_name.lower()
        is_error = bool(response.get("isError") is True or response.get("is_error") is True or response.get("success") is False)
        meta = response.get("meta") if isinstance(response.get("meta"), dict) else {}
        value = response.get("value") if isinstance(response.get("value"), dict) else {}
        text = _extract_observable_text(response.get("content", response.get("output", response.get("stdout", ""))))
        if not text and isinstance(response.get("value"), str):
            text = response["value"]
        # 后台任务（尚未取得终态退出码）标为 running，绝不误判为 succeeded
        job_id = (response.get("session_id") or response.get("job_id") or response.get("jobId")
                  or value.get("job_id") or value.get("jobId") or meta.get("job_id") or meta.get("jobId"))
        if is_error:
            err_obj = response.get("error")
            err_msg = err_obj.get("message", "") if isinstance(err_obj, dict) else (str(err_obj) if err_obj else text)
            err_code = (err_obj.get("info", {}).get("code") if isinstance(err_obj, dict) and isinstance(err_obj.get("info"), dict)
                        else response.get("error_code"))
            out = {"isError": True, "error": err_msg[:4000]}
            if isinstance(err_code, str) and err_code.strip():
                out["error_code"] = err_code.strip()[:120]
            for key in ("exit_code", "exitCode"):
                if isinstance(response.get(key), int) and not isinstance(response.get(key), bool):
                    out[key] = response[key]
            return out
        # 检查显式退出码或 DSH pwsh/bash 输出末尾的 [exit code: N] 标记
        exit_code = None
        for source in (response, meta, value):
            for key in ("exitCode", "exit_code"):
                candidate = source.get(key)
                if isinstance(candidate, int) and not isinstance(candidate, bool):
                    exit_code = candidate
                    break
            if exit_code is not None:
                break
        termination_signal = None
        for source in (response, meta, value):
            sig = source.get("signal") or source.get("terminationSignal") or source.get("termSignal")
            if isinstance(sig, str) and sig.strip():
                termination_signal = sig.strip()[:80]
                break
            if isinstance(sig, int) and not isinstance(sig, bool) and sig > 0:
                termination_signal = f"SIG({sig})"
                break
        aborted_flag = any(
            source.get(flag) is True
            for source in (response, meta, value)
            for flag in ("timedOut", "timed_out", "killed", "aborted", "interrupted", "cancelled", "canceled")
        ) or any(
            isinstance(source.get("status"), str)
            and source.get("status").strip().lower() in {
                "failed", "error", "killed", "aborted", "interrupted",
                "cancelled", "canceled", "timed_out", "timeout", "terminated",
            }
            for source in (response, meta, value)
        )
        if termination_signal is not None or aborted_flag:
            reason = f"terminated by signal {termination_signal}" if termination_signal else "command terminated or timed out"
            out = {"isError": True, "success": False, "error": (text or reason)[:4000]}
            if termination_signal:
                out["signal"] = termination_signal
                out["error_code"] = f"SIGNAL_{termination_signal}"[:120]
            if exit_code is not None:
                out["exitCode"] = exit_code
            return out
        if isinstance(job_id, str) and job_id.strip() and exit_code is None:
            marker = _EXIT_CODE_MARKER.search(text)
            if marker:
                exit_code = int(marker.group(1))
            else:
                return {"session_id": job_id.strip(), "status": "running", "output": text[:4000]}
        if exit_code is None and (lower_name in _SHELL_EXEC_TOOLS or "pwsh" in lower_name or "bash" in lower_name):
            match = _EXIT_CODE_MARKER.search(text)
            if match:
                exit_code = int(match.group(1))
            elif not tool_args.get("run_in_background"):
                explicit_null_exit = any(
                    key in source and source[key] is None
                    for source in (response, meta, value)
                    for key in ("exitCode", "exit_code")
                )
                if not explicit_null_exit:
                    exit_code = 0
        if exit_code is not None:
            return {"exitCode": exit_code, "isError": exit_code != 0, "success": exit_code == 0, "output": text[:4000]}
        if response.get("success") is True or lower_name in _DETERMINISTIC_LOCAL_TOOLS:
            return {"success": True, "isError": False, "output": text[:4000]}
        return {"isError": False, "output": text[:4000]}

    def _task_signature(self, project, conversation):
        task = self.runtime.task(project, conversation)
        return digest([task[key] for key in ("id", "revision", "write_version", "execution_epoch", "status")]) if task else digest(None)

    def _assert_phase(self, project, conversation, epoch, generation, task_signature=None):
        self.runtime.assert_session_epoch(epoch)
        row = self.runtime.db.execute("SELECT generation FROM dsh_sessions WHERE project=? AND conversation=?",
                                      (project, conversation)).fetchone()
        if not row or row[0] != generation:
            raise ContractError("context_changed: DSH session trajectory was reset")
        if task_signature is not None and self._task_signature(project, conversation) != task_signature:
            raise ContractError("context_changed: DSH task changed during hook")

    def _session_state(self, project, conversation):
        self.runtime.db.execute("INSERT OR IGNORE INTO dsh_sessions(project,conversation) VALUES(?,?)",
                                (project, conversation))
        return dict(self.runtime.db.execute("SELECT * FROM dsh_sessions WHERE project=? AND conversation=?",
                                            (project, conversation)).fetchone())

    def _orphan_fault(self, project, conversation, epoch, normalized):
        r = self.runtime
        current = r.task(project, conversation)
        if current:
            r._queue_fault(project, conversation, None, current, epoch, normalized)
            return
        response = encode(clean(normalized["tool_response"]))
        query = normalized["tool_name"] + " " + response[:1000]
        signature = digest([epoch, query])
        count = r.db.execute(
            "SELECT COUNT(*) FROM dsh_faults WHERE project=? AND conversation=? AND host_epoch=? AND state='pending'",
            (project, conversation, epoch),
        ).fetchone()[0]
        if count < 3:
            r.db.execute(
                "INSERT OR IGNORE INTO dsh_faults VALUES(?,?,?,?,?,'pending',?)",
                (project, conversation, signature, epoch, query, time.time()),
            )
        r.db.execute(
            "DELETE FROM dsh_faults WHERE project=? AND conversation=? AND signature NOT IN "
            "(SELECT signature FROM dsh_faults WHERE project=? AND conversation=? ORDER BY created DESC LIMIT 32)",
            (project, conversation, project, conversation),
        )

    def _deny(self, kind, reason):
        message = "Agent Learning: " + reason
        return {
            "decision": "deny",
            "reason": message,
            "hookSpecificOutput": {
                "hookEventName": kind,
                "permissionDecision": "deny",
                "permissionDecisionReason": message,
            },
        }

    def handle(self, event, raw=None):
        if raw is None and isinstance(event, dict):
            raw = event
            event = raw.get("hook_event_name") or raw.get("event")
        if event not in EVENTS:
            raise ContractError("dsh_unsupported_event")
        if not isinstance(raw, dict):
            raise ContractError("dsh_input_object_required")
        workspace = self._resolve_workspace(raw, event)
        if workspace is None:
            # 尚未绑定目录的空白会话不猜项目，安全返回空对象
            return {}
        native_session, agent_id = self._resolve_identities(raw)
        if event in {"SubagentStart", "SubagentStop"} and not agent_id:
            return {"hookSpecificOutput": {"hookEventName": event, "additionalContext":
                    "Agent Learning: 子 Agent 身份未知，未分配写入句柄；继承当前任务边界，可按项目检索知识，不猜测会话归属。"}}
        r = self.runtime
        project = r.project(workspace)
        conversation = r.host_conversation(native_session)
        with r.transaction():
            epoch = self._epoch()
            if not agent_id:
                from .dsh_profile import bind_conversation_profile
                bind_conversation_profile(r, project, conversation)
            r.bind_session(project, conversation)
            session_state = self._session_state(project, conversation)
            generation = session_state["generation"]
            phase_task_signature = self._task_signature(project, conversation)

        def phase_guard():
            self._assert_phase(project, conversation, epoch, generation, phase_task_signature)

        normalized = {
            "hook_event_name": event,
            "cwd": str(workspace),
            "session_id": conversation,
        }
        if agent_id is not None:
            normalized["agent_id"] = agent_id
        model = _extract_model(raw)
        if model is not None:
            normalized["model"] = model
        turn_val = raw.get("turn_id") if "turn_id" in raw else raw.get("turn")
        if isinstance(turn_val, int) and not isinstance(turn_val, bool):
            normalized["turn_id"] = f"turn-{turn_val}"
        elif isinstance(turn_val, str) and turn_val.strip():
            normalized["turn_id"] = turn_val.strip()[:200]

        queried_fault_signature = None
        if event == "SessionStart":
            source = raw.get("source", "startup")
            if source not in {"startup", "resume", "clear", "compact"}:
                raise ContractError("dsh_invalid_session_start_source")
            normalized["source"] = source
            prompt = _extract_observable_text(raw.get("prompt", raw.get("messages", "")))
            if not prompt and not agent_id:
                fault = r.db.execute(
                    "SELECT signature,query FROM dsh_faults WHERE project=? AND conversation=? AND host_epoch=? AND state='pending' "
                    "ORDER BY created LIMIT 1",
                    (project, conversation, epoch),
                ).fetchone()
                if fault:
                    prompt = fault["query"]
                    queried_fault_signature = fault["signature"]
            if prompt:
                normalized["prompt"] = prompt
        elif event in {"UserPromptSubmit", "SubagentStart"}:
            prompt = _extract_observable_text(raw.get("prompt", raw.get("messages", "")))
            if not prompt and event == "UserPromptSubmit" and not agent_id:
                fault = r.db.execute(
                    "SELECT signature,query FROM dsh_faults WHERE project=? AND conversation=? AND host_epoch=? AND state='pending' "
                    "ORDER BY created LIMIT 1",
                    (project, conversation, epoch),
                ).fetchone()
                if fault:
                    prompt = fault["query"]
                    queried_fault_signature = fault["signature"]
            normalized["prompt"] = prompt
            if raw.get("prompt_truncated") is True:
                normalized["prompt_truncated"] = True
            if type(raw.get("prompt_chars")) is int and raw["prompt_chars"] >= len(prompt):
                normalized["prompt_chars"] = raw["prompt_chars"]
            if isinstance(raw.get("message_ids"), list):
                normalized["message_ids"] = [value[:200] for value in raw["message_ids"][:32] if isinstance(value, str)]
            if event == "SubagentStart" and isinstance(raw.get("agent_type") or raw.get("provider"), str):
                normalized["agent_type"] = (raw.get("agent_type") or raw.get("provider")).strip()[:100]
        elif event in {"Stop", "SubagentStop"}:
            last_msg = _extract_observable_text(
                raw.get("last_assistant_message", raw.get("lastAssistantMessage", raw.get("message", raw.get("content", ""))))
            )
            if last_msg:
                normalized["last_assistant_message"] = last_msg
                if raw.get("last_assistant_message_truncated") is True:
                    normalized["last_assistant_message_truncated"] = True
                length = raw.get("last_assistant_message_chars")
                if type(length) is int and length >= len(last_msg):
                    normalized["last_assistant_message_chars"] = length
        elif event in {"PreToolUse", "PostToolUse"}:
            tool_name, tool_args, call_id = self._normalize_tool_call(raw)
            # 校验显式传入的 context_handle 是否属于当前 DSH 会话，防止跨会话句柄混用
            if "context_handle" in tool_args:
                try:
                    r.access_context(project, tool_args["context_handle"], conversation, epoch)
                except ContractError:
                    return self._deny(event, "context_handle_dsh_conversation_mismatch") if event == "PreToolUse" else {}
            if event == "PreToolUse" and tool_name in _WRITE_LEARNING_TOOLS and "context_handle" not in tool_args:
                return self._deny(event, "context_handle_required_for_dsh_write")
            normalized.update(tool_name=tool_name, tool_input=tool_args, tool_use_id=call_id)
            if event == "PostToolUse":
                normalized["tool_response"] = self._normalize_tool_response(tool_name, tool_args, raw)

        context_outcome = {}
        result = r.handle_hook(
            normalized, context_delivery=True, expected_epoch=epoch,
            _phase_guard=phase_guard, _context_outcome=context_outcome,
        )
        if event == "PostToolUse" and normalized["tool_response"].get("isError") is True and not agent_id:
            with r.transaction():
                self._assert_phase(project, conversation, epoch, generation)
                operation_id = digest([conversation, normalized["tool_use_id"]])
                binding = r.db.execute("SELECT task_id FROM operations WHERE project=? AND id=?", (project, operation_id)).fetchone()
                if binding and binding["task_id"] is None:
                    self._orphan_fault(project, conversation, epoch, normalized)
        elif event in {"SessionStart", "UserPromptSubmit"} and not agent_id:
            if queried_fault_signature is not None:
                with r.transaction():
                    self._assert_phase(project, conversation, epoch, generation)
                    pending = (
                        context_outcome.get("retryable")
                        or context_outcome.get("cooling")
                        or context_outcome.get("delivery_pending")
                    )
                    if not pending:
                        r.db.execute(
                            "UPDATE dsh_faults SET state='queried' WHERE project=? AND conversation=? AND signature=?",
                            (project, conversation, queried_fault_signature),
                        )
            if event == "SessionStart" and normalized.get("source") == "startup":
                try:
                    hist = r.history_status(project)
                    if hist.get("onboarding", {}).get("state") == "unoffered":
                        notice = (
                            "Agent Learning: 当前项目尚未执行首次历史知识吸收（onboarding=unoffered）。"
                            "可在 DSH 输入 /learning-history 一键吸收，或明确要求调用 "
                            "learning_history(action=\"auto\", confirm=true) 吸收项目历史，或调用 learning_history(action=\"skip\") 跳过。"
                        )
                        result = dict(result)
                        result["history_onboarding"] = {
                            "state": "unoffered",
                            "tool": "learning_history",
                            "slash_command": "/learning-history",
                            "choices": ["auto", "prepare", "skip"],
                        }
                        result["systemMessage"] = "\n".join(filter(None, (result.get("systemMessage"), notice)))
                except Exception:
                    pass
        if event == "PreToolUse":
            denial = result.get("hookSpecificOutput", {})
            if denial.get("permissionDecision") == "deny":
                return {
                    "decision": "deny",
                    "reason": denial.get("permissionDecisionReason", "Agent Learning: execution denied"),
                    **result,
                }
            if self.mode == "enforce":
                return {
                    "decision": "allow",
                    **result,
                }
        return result


Adapter = DshAdapter
