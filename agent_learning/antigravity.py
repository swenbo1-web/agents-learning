"""Antigravity 原生事件适配；仅采集约定的公开字段，不保留模型内部思考。

原生只有轨迹内序号，没有可验证的工具调用 UUID。持久台账隔离编号回退、
重复投递和矛盾输入；无法确定归属的完成事件不绑定当前任务。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import time

from .runtime import ContractError, clean, clip, digest, encode
from .public_observations import observed_public


EVENTS = {"PreInvocation", "PostInvocation", "PreToolUse", "PostToolUse", "Stop"}
MODES = {"observe", "enforce"}
PATH_FIELDS = ("Cwd", "AbsolutePath", "TargetFile", "DirectoryPath", "SearchDirectory", "SearchPath")
RECOVERY_TOOLS = {"mcp__agent_learning__" + name for name in (
    "knowledge_search", "knowledge_read", "work_memory_search", "work_memory_read",
    "session_context", "task_update", "case_feedback", "knowledge_use", "learning_status")}
PUBLIC_TEXT_MAX_CHARS = 16000


def _public_field(raw, field):
    """仅接收宿主明确提供的公开字符串；不解析对象或回读对话文件。"""
    value = raw.get(field)
    coverage = {"available": isinstance(value, str), "truncated": False, "source": "native_hook",
                "full_original_available": False}
    if not isinstance(value, str):
        coverage["reason"] = "missing" if field not in raw else "unsupported_type"
        return None, coverage
    if not value.strip():
        coverage.update(available=False, reason="empty")
        return None, coverage
    # 先脱敏再裁剪；截断不能留下半个未脱敏的凭据。
    value = clean(value)
    coverage["truncated"] = len(value) > PUBLIC_TEXT_MAX_CHARS
    coverage["full_original_available"] = not coverage["truncated"]
    return clip(value, PUBLIC_TEXT_MAX_CHARS), coverage


def _native_digest(observed):
    # Optional public capture and local diagnostics cannot change native replay
    # identity, including compatibility with receipts from before this reader.
    return "v2:" + digest({key: value for key, value in observed.items() if key not in {
        "modelName", "lastUserInput", "finalModelOutput", "capture_coverage",
        "host_epoch", "generation"}})


def install_storage(runtime):
    """由 Runtime 迁移事务调用；各安装共用台账，不在源码保存运行记录。"""
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS antigravity_trajectories(
        project TEXT NOT NULL,conversation TEXT NOT NULL,generation INTEGER NOT NULL DEFAULT 1,
        last_invocation INTEGER NOT NULL DEFAULT -1,last_steps INTEGER NOT NULL DEFAULT -1,
        max_step INTEGER NOT NULL DEFAULT -1,ambiguous INTEGER NOT NULL DEFAULT 0,
        reset_generation INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(project,conversation))""")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS antigravity_steps(
        project TEXT NOT NULL,conversation TEXT NOT NULL,generation INTEGER NOT NULL,
        step_index INTEGER NOT NULL,operation_key TEXT NOT NULL,fingerprint TEXT NOT NULL,
        host_epoch INTEGER NOT NULL,closed INTEGER NOT NULL DEFAULT 0,
        conflict INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(project,conversation,generation,step_index))""")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS antigravity_receipts(
        project TEXT NOT NULL,conversation TEXT NOT NULL,event_key TEXT NOT NULL,
        kind TEXT NOT NULL,host_epoch INTEGER NOT NULL,generation INTEGER NOT NULL,
        raw_digest TEXT NOT NULL DEFAULT '',
        task_signature TEXT NOT NULL,output TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL DEFAULT 'pending',created REAL NOT NULL,
        PRIMARY KEY(project,conversation,event_key))""")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS antigravity_faults(
        project TEXT NOT NULL,conversation TEXT NOT NULL,signature TEXT NOT NULL,
        host_epoch INTEGER NOT NULL,query TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
        created REAL NOT NULL,PRIMARY KEY(project,conversation,signature))""")
    # 开发期隔离数据库可能来自前一份适配器；只补列，不改原始回执。
    for table, column, declaration in (("antigravity_trajectories", "reset_generation", "INTEGER NOT NULL DEFAULT 1"),
                                       ("antigravity_receipts", "raw_digest", "TEXT NOT NULL DEFAULT ''")):
        if column not in {row[1] for row in runtime.db.execute("PRAGMA table_info(" + table + ")")}:
            runtime.db.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " " + declaration)
    runtime.db.execute("CREATE INDEX IF NOT EXISTS antigravity_receipt_raw ON antigravity_receipts(project,conversation,raw_digest)")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS antigravity_receipt_recency ON "
                       "antigravity_receipts(project,kind,host_epoch,created DESC,event_key DESC)")


def _number(raw, field):
    value = raw.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2 ** 53 - 1:
        raise ContractError("antigravity_invalid_" + field)
    return value


def _absolute(value):
    if (not isinstance(value, str) or not value.strip() or "\0" in value
            or not Path(value).is_absolute()):
        raise ContractError("antigravity_absolute_workspace_required")
    return Path(value).resolve()


def _path_key(value):
    return os.path.normcase(str(value))


def _within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def reset_session(runtime, workspace, native_conversation):
    """受信 CLI 专用；编号重置须使旧句柄、执行许可和查询缓存失效。"""
    if runtime.host != "antigravity":
        raise ContractError("antigravity_host_required")
    project = runtime.project(_absolute(str(workspace)))
    conversation = runtime.host_conversation(native_conversation)
    with runtime.transaction():
        runtime.bind_session(project, conversation)
        runtime.admin_observe_model(project, conversation, active=False)
        runtime.db.execute("INSERT OR IGNORE INTO antigravity_trajectories(project,conversation) VALUES(?,?)", (project, conversation))
        runtime.db.execute("UPDATE antigravity_trajectories SET generation=generation+1,reset_generation=generation+1,"
                           "last_invocation=-1,last_steps=-1,max_step=-1,ambiguous=0 WHERE project=? AND conversation=?", (project, conversation))
        runtime.db.execute("DELETE FROM context_access WHERE project=? AND conversation=?", (project, conversation))
        for table in ("contexts", "subagent_contexts"):
            runtime.db.execute(f"UPDATE {table} SET epoch=epoch+1,injected='[]',last_query='',retrieval_signature='',retrieval_refs='[]' "
                               "WHERE project=? AND conversation=?", (project, conversation))
        runtime.db.execute("UPDATE antigravity_faults SET state='superseded' WHERE project=? AND conversation=? AND state='pending'", (project, conversation))
        runtime._execution_fence(project, conversation, "antigravity_trusted_session_reset", pause=True)
        generation = runtime.db.execute("SELECT generation FROM antigravity_trajectories WHERE project=? AND conversation=?", (project, conversation)).fetchone()[0]
        runtime._event(project, conversation, "AntigravitySessionReset", {"generation": generation})
        return {"project_id": project, "conversation_id": conversation, "generation": generation,
                "rebind_required": True, "execution_approval_required": True}


class AntigravityAdapter:
    def __init__(self, runtime, workspace, hook_mode=None):
        self.runtime = runtime
        self.workspace = _absolute(str(workspace))
        self.mode = hook_mode or ("enforce" if runtime.config["enforce_task_policy"] else "observe")
        if self.mode not in MODES:
            raise ContractError("antigravity_invalid_hook_mode")
        if getattr(runtime, "host", "antigravity") != "antigravity":
            raise ContractError("antigravity_host_required")
        # 由固定入口选择正常守卫与故障策略，不让原生 JSON 覆盖。
        runtime.config["enforce_task_policy"] = self.mode == "enforce"

    def _epoch(self):
        return self.runtime.session_epoch()

    def _common(self, raw):
        if not isinstance(raw, dict):
            raise ContractError("antigravity_input_object_required")
        native = raw.get("conversationId")
        if (not isinstance(native, str) or not native.strip() or "\0" in native
                or len(native) > 200):
            raise ContractError("antigravity_conversation_required")
        paths = raw.get("workspacePaths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 64:
            raise ContractError("antigravity_workspace_paths_required")
        mounted = list(dict.fromkeys(_absolute(item) for item in paths))
        if _path_key(self.workspace) not in {_path_key(item) for item in mounted}:
            raise ContractError("antigravity_installed_workspace_not_mounted")
        project = self.runtime.project(self.workspace)
        conversation = self.runtime.host_conversation(native)
        self.runtime.bind_session(project, conversation)
        return project, conversation, mounted

    def _observable(self, kind, raw):
        value = {"kind": kind}
        if kind in {"PreToolUse", "PostToolUse"}:
            tool = raw.get("toolCall")
            if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                    or not tool["name"].strip() or len(tool["name"]) > 200
                    or not isinstance(tool.get("args"), dict)):
                raise ContractError("antigravity_tool_call_required")
            value.update(toolCall={"name": tool["name"], "args": tool["args"]},
                         stepIdx=_number(raw, "stepIdx"))
            if kind == "PostToolUse":
                error = raw.get("error", "")
                if not isinstance(error, str):
                    raise ContractError("antigravity_error_must_be_string")
                value["error"] = error
        elif kind in {"PreInvocation", "PostInvocation"}:
            value.update(invocationNum=_number(raw, "invocationNum"),
                         initialNumSteps=_number(raw, "initialNumSteps"))
            field = "lastUserInput" if kind == "PreInvocation" else "finalModelOutput"
            text, coverage = _public_field(raw if kind == "PreInvocation" else {}, field)
            value["capture_coverage"] = {field: coverage, "native_message_id_available": False}
            if text is not None:
                value[field] = text
        else:
            reason = raw.get("terminationReason")
            if not isinstance(reason, str) or not reason.strip() or not isinstance(raw.get("fullyIdle"), bool):
                raise ContractError("antigravity_stop_state_required")
            error = raw.get("error", "")
            if not isinstance(error, str):
                raise ContractError("antigravity_error_must_be_string")
            value.update(executionNum=_number(raw, "executionNum"),
                         terminationReason=reason, fullyIdle=raw["fullyIdle"], error=error)
            text, coverage = _public_field(raw, "finalModelOutput")
            value["capture_coverage"] = {"finalModelOutput": coverage}
            if text is not None:
                value["finalModelOutput"] = text
        # modelName is diagnostic metadata, never a query or an identity credential.
        if isinstance(raw.get("modelName"), str):
            value["modelName"] = raw["modelName"][:200]
        return value

    def _tool_workspace(self, tool, mounted):
        """显式安装根可以从多根中选择；没有工具资源时不猜其项目。"""
        arguments, paths = tool["args"], []
        for field in PATH_FIELDS:
            if field not in arguments:
                continue
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or "\0" in value:
                return False
            try:
                path = Path(value)
                path = (path if path.is_absolute() else self.workspace / path).resolve()
            except (ValueError, OSError, RuntimeError):
                return False
            paths.append(path)
        if paths and any(not _within(path, self.workspace) for path in paths):
            return False
        return bool(paths) or len(mounted) == 1

    def _task_signature(self, project, conversation):
        task = self.runtime.task(project, conversation)
        return digest([task[key] for key in ("id", "revision", "write_version", "execution_epoch", "status")]) if task else digest(None)

    def _receipt_signature(self, project, conversation, event):
        signature = self._task_signature(project, conversation)
        if event == "PreInvocation":
            # 同一次原生 invocation 的重播不能再次提供已撤销或来源已变的摘要。
            manifest = self.runtime.db.execute("SELECT generation,revocation_epoch,source_epoch FROM projects WHERE id=?", (project,)).fetchone()
            return digest([signature, list(manifest)])
        return signature

    def _assert_phase(self, project, conversation, epoch, generation, task_signature=None):
        """在调用者写事务内核对快照；旧回调不能穿过换号、重置或任务切换。"""
        self.runtime.assert_session_epoch(epoch)
        row = self.runtime.db.execute("SELECT generation FROM antigravity_trajectories WHERE project=? AND conversation=?",
                                      (project, conversation)).fetchone()
        if not row or row[0] != generation:
            raise ContractError("context_changed: native trajectory was reset")
        if task_signature is not None and self._task_signature(project, conversation) != task_signature:
            raise ContractError("context_changed: native task changed during hook")

    def _trajectory(self, project, conversation, observed):
        self.runtime.db.execute("INSERT OR IGNORE INTO antigravity_trajectories(project,conversation) VALUES(?,?)",
                                (project, conversation))
        row = dict(self.runtime.db.execute("SELECT * FROM antigravity_trajectories WHERE project=? AND conversation=?",
                                          (project, conversation)).fetchone())
        if observed["kind"] == "PreInvocation":
            steps, invocation = observed["initialNumSteps"], observed["invocationNum"]
            # 序号回退没有可信轨迹 ID；永久隔离这一会话，不恢复旧执行许可。
            regression = (steps < row["last_steps"] or steps < row["max_step"])
            if regression and not row["ambiguous"]:
                row["generation"] += 1
                row["ambiguous"] = 1
                self.runtime._execution_fence(project, conversation, "antigravity_trajectory_ambiguous")
                self.runtime.admin_observe_model(project, conversation, active=False)
            self.runtime.db.execute("UPDATE antigravity_trajectories SET generation=?,ambiguous=?,last_invocation=?,last_steps=? "
                                    "WHERE project=? AND conversation=?",
                                    (row["generation"], row["ambiguous"], invocation, steps, project, conversation))
        return row

    def _step(self, project, conversation, observed, trajectory, epoch):
        index = observed["stepIdx"]
        # 脱敏只应用于记录正文；实际输入变化必须仍能触发同序号冲突。
        fingerprint = digest(observed["toolCall"])
        generation = trajectory["generation"]
        row = self.runtime.db.execute("SELECT * FROM antigravity_steps WHERE project=? AND conversation=? "
                                      "AND generation=? AND step_index=?", (project, conversation, generation, index)).fetchone()
        conflict = bool(row and (row["conflict"] or row["fingerprint"] != fingerprint))
        if not row:
            operation = digest(["antigravity", conversation, generation, index])
            self.runtime.db.execute("INSERT INTO antigravity_steps(project,conversation,generation,step_index,operation_key,fingerprint,host_epoch) "
                                    "VALUES(?,?,?,?,?,?,?)", (project, conversation, generation, index, operation, fingerprint, epoch))
        else:
            operation = row["operation_key"]
            if conflict:
                self.runtime.db.execute("UPDATE antigravity_steps SET conflict=1 WHERE project=? AND conversation=? AND generation=? AND step_index=?",
                                        (project, conversation, generation, index))
        self.runtime.db.execute("UPDATE antigravity_trajectories SET max_step=MAX(max_step,?) WHERE project=? AND conversation=?",
                                (index, project, conversation))
        if observed["kind"] == "PostToolUse":
            self.runtime.db.execute("UPDATE antigravity_steps SET closed=1 WHERE project=? AND conversation=? AND generation=? AND step_index=?",
                                    (project, conversation, generation, index))
        return operation, conflict, bool(row and row["host_epoch"] != epoch)

    def _deny(self, reason):
        return {"decision": "deny", "reason": "Agent Learning: " + reason}

    def _record_unassociated(self, project, conversation, observed, event_key, reason, phase_guard):
        identity = ["antigravity", conversation, event_key]
        legacy = ["antigravity", event_key]
        prior = self.runtime.db.execute("SELECT conversation FROM events WHERE id=?",
                                       (digest([project, legacy]),)).fetchone()
        if prior and prior["conversation"] == conversation:
            identity = legacy
        self.runtime.record_event(project, conversation, "Antigravity" + observed["kind"],
                                  {**observed, "association": "unassociated", "reason": reason},
                                  event_id=identity, _phase_guard=phase_guard)

    def _orphan_fault(self, project, conversation, epoch, normalized):
        r = self.runtime
        current = r.task(project, conversation)
        if current:
            r._queue_fault(project, conversation, None, current, epoch, normalized)
            return
        # 没有任务时也能补查真实错误；独立队列没有伪造任务身份。
        response = encode(clean(normalized["tool_response"]))
        query = normalized["tool_name"] + " " + response[:1000]
        signature = digest([epoch, query])
        count = r.db.execute("SELECT COUNT(*) FROM antigravity_faults WHERE project=? AND conversation=? AND host_epoch=? AND state='pending'",
                             (project, conversation, epoch)).fetchone()[0]
        if count < 3:
            r.db.execute("INSERT OR IGNORE INTO antigravity_faults VALUES(?,?,?,?,?,'pending',?)",
                         (project, conversation, signature, epoch, query, time.time()))
        r.db.execute("DELETE FROM antigravity_faults WHERE project=? AND conversation=? AND signature NOT IN "
                     "(SELECT signature FROM antigravity_faults WHERE project=? AND conversation=? ORDER BY created DESC LIMIT 32)",
                     (project, conversation, project, conversation))

    def _transcript_public(self, project, conversation, observed, raw,
                           epoch, generation, phase_guard):
        """Public input/response recovery uses the native invocation pair only.

        PostInvocation proves a model invocation, not completion of the user's
        task or execution. It must never manufacture a Stop or a review phase.
        """
        from .antigravity_transcript import read_public
        before = observed["kind"] == "PreInvocation"
        field = "lastUserInput" if before else "finalModelOutput"
        native_prompt = before and field in raw
        if native_prompt and (not isinstance(raw[field], str) or not raw[field].strip()):
            return
        coverage = {"available": False, "truncated": False, "full_original_available": False,
                    "source": "antigravity_transcript"}
        if not native_prompt:
            observed["capture_coverage"][field] = coverage
        initial, invocation = observed["initialNumSteps"], observed["invocationNum"]
        pre_key = digest([epoch, generation, _native_digest({"kind": "PreInvocation",
                         "invocationNum": invocation, "initialNumSteps": initial})])
        window_id = digest([project, ["antigravity-public-window", conversation, pre_key]])
        if not before:
            # No transcript I/O for an orphan Post. These checks are repeated in
            # the guarded commit after the bounded file read.
            receipt = self.runtime.db.execute("SELECT state FROM antigravity_receipts WHERE project=? "
                "AND conversation=? AND event_key=? AND host_epoch=? AND generation=?",
                (project, conversation, pre_key, epoch, generation)).fetchone()
            if (receipt is None or receipt["state"] != "complete"
                    or not self.runtime.db.execute("SELECT 1 FROM events WHERE project=? AND id=? "
                        "AND conversation=? AND type='AntigravityPublicWindow'",
                        (project, window_id, conversation)).fetchone()):
                coverage["reason"] = "transcript_window_unbound"
                return
        snapshot = read_public(raw)
        if not snapshot["available"]:
            coverage["reason"] = snapshot["reason"]
            return
        with self.runtime.transaction():
            phase_guard()
            records = snapshot["records"]
            users = [item for item in records if item["type"] == "USER_INPUT"
                     and item["source"] == "USER_EXPLICIT" and item["status"] == "DONE"
                     and isinstance(item.get("content"), str) and item["content"].strip()]
            user = users[-1] if users else None
            if user is None:
                coverage["reason"] = "transcript_user_boundary_missing"
                return
            user_digest = digest(clean(user["content"]))
            if before:
                if snapshot["last_step"] >= initial or user["step_index"] >= initial:
                    coverage["reason"] = "transcript_ahead_of_invocation"
                    return
                if native_prompt and digest(clean(raw[field])) != user_digest:
                    # Keep the host's direct prompt. A different public file
                    # cannot establish this invocation's response window.
                    return
                receipt = self.runtime.db.execute("SELECT task_signature FROM antigravity_receipts "
                    "WHERE project=? AND conversation=? AND event_key=?", (project, conversation, pre_key)).fetchone()
                if receipt is None:
                    coverage["reason"] = "transcript_pre_receipt_missing"
                    return
                window = {"version": 1, "host_epoch": epoch, "generation": generation,
                          "invocation_num": invocation, "initial_steps": initial,
                          "user_step": user["step_index"], "user_digest": user_digest,
                          "file_identity": snapshot["file_identity"], "path_digest": snapshot["path_digest"],
                          "task_signature": self._task_signature(project, conversation),
                          "pre_receipt_key": pre_key, "pre_receipt_signature": receipt["task_signature"]}
                self.runtime._event(project, conversation, "AntigravityPublicWindow", window,
                    event_id=["antigravity-public-window", conversation, pre_key])
                if native_prompt:
                    return
                text, public_coverage = _public_field({field: user["content"]}, field)
                public_coverage.update(source="antigravity_transcript", step_index=user["step_index"])
                if user.get("truncated"):
                    public_coverage.update(truncated=True, full_original_available=False,
                                           reason="transcript_content_truncated")
                observed[field], observed["capture_coverage"][field] = text, public_coverage
                return
            receipt = self.runtime.db.execute("SELECT state,task_signature FROM antigravity_receipts "
                "WHERE project=? AND conversation=? AND event_key=? AND host_epoch=? AND generation=?",
                (project, conversation, pre_key, epoch, generation)).fetchone()
            saved = self.runtime.db.execute("SELECT payload FROM events WHERE project=? AND id=? AND conversation=? "
                "AND type='AntigravityPublicWindow'", (project, window_id, conversation)).fetchone()
            if receipt is None or receipt["state"] != "complete" or saved is None:
                coverage["reason"] = "transcript_window_unbound"
                return
            try:
                window = json.loads(saved["payload"])
            except (ValueError, TypeError):
                coverage["reason"] = "transcript_window_invalid"
                return
            if not isinstance(window, dict):
                coverage["reason"] = "transcript_window_invalid"
                return
            trajectory = self.runtime.db.execute("SELECT last_invocation,last_steps FROM antigravity_trajectories "
                "WHERE project=? AND conversation=?", (project, conversation)).fetchone()
            if not trajectory or tuple(trajectory) != (invocation, initial):
                coverage["reason"] = "transcript_invocation_superseded"
                return
            if (window.get("host_epoch") != epoch or window.get("generation") != generation
                    or window.get("pre_receipt_signature") != receipt["task_signature"]):
                coverage["reason"] = "transcript_window_boundary_changed"
                return
            if window.get("task_signature") != self._task_signature(project, conversation):
                coverage["reason"] = "transcript_task_changed"
                return
            if (window.get("file_identity") != snapshot["file_identity"]
                    or window.get("path_digest") != snapshot["path_digest"]):
                coverage["reason"] = "transcript_file_changed"
                return
            if (window.get("user_step") != user["step_index"] or window.get("user_digest") != user_digest
                    or any(item["type"] == "USER_INPUT" and item["step_index"] >= initial for item in records)):
                coverage["reason"] = "transcript_user_boundary_changed"
                return
            responses = [item for item in records if item["type"] == "PLANNER_RESPONSE"
                         and item["source"] == "MODEL" and item["step_index"] >= initial]
            if len(responses) != 1:
                coverage["reason"] = "transcript_response_window_ambiguous"
                return
            final = responses[0]
            if (final["status"] != "DONE" or not isinstance(final.get("content"), str)
                    or not final["content"].strip()):
                coverage["reason"] = "transcript_final_public_missing"
                return
            text, public_coverage = _public_field({field: final["content"]}, field)
            public_coverage.update(source="antigravity_transcript", step_index=final["step_index"],
                                   user_step=user["step_index"], execution_completion="unverified")
            if final.get("truncated"):
                public_coverage.update(truncated=True, full_original_available=False,
                                       reason="transcript_content_truncated")
            observed[field], observed["capture_coverage"][field] = text, public_coverage

    def _public_response(self, project, conversation, observed, event_key, phase_guard):
        with self.runtime.transaction():
            phase_guard()
            task = self.runtime.task(project, conversation)
            metadata = {key: observed[key] for key in ("host_epoch", "generation", "capture_coverage")}
            metadata.update(invocation_num=observed["invocationNum"], initial_steps=observed["initialNumSteps"],
                            completion="partial", execution_completion="unverified")
            source, fresh = self.runtime._event(project, conversation, "AntigravityPublicResponse", metadata,
                event_id=["antigravity-public-response", conversation, event_key],
                task_id=task["id"] if task else None, task_revision=task["revision"] if task else None,
                task_write_version=task["write_version"] if task else None,
                execution_epoch=task["execution_epoch"] if task else None)
            text = observed.get("finalModelOutput")
            if fresh and isinstance(text, str) and text.strip():
                # Existing progressive public segments preserve the full bounded
                # message. No Stop, delivery, AI summary or administrator wake.
                self.runtime._work_capture_public_segments(project, source, str(self.workspace), text,
                    "public_summary", "公开模型回复（阶段完成未验证）", "partial", observed["capture_coverage"],
                    {"last_assistant_message_truncated": observed["capture_coverage"]["finalModelOutput"]["truncated"]})
            return {}

    def _capture_prompt(self, project, conversation, observed, generation, epoch, phase_guard, mounted):
        """同一执行阶段重复提供的 lastUserInput 不是多条人工消息。"""
        text = observed.get("lastUserInput")
        if not isinstance(text, str) or not text.strip() or len(mounted) != 1:
            return
        with self.runtime.transaction():
            phase_guard()
            boundary = self.runtime.db.execute(
                "SELECT id FROM events WHERE project=? AND conversation=? AND type='AntigravityStop' "
                "AND json_extract(payload,'$.fullyIdle')=1 ORDER BY created DESC,id DESC LIMIT 1",
                (project, conversation)).fetchone()
            legacy_key = ["antigravity-public-prompt", epoch, generation,
                          boundary[0] if boundary else None, digest(text)]
            key = ["antigravity-public-prompt", conversation, *legacy_key[1:]]
            # 同阶段后续 invocation 可以复用升级前已捕获的本会话消息；另一
            # 会话占用的旧 ID 则使用新身份，不借用或更改旧来源。
            prior = self.runtime.db.execute("SELECT conversation FROM events WHERE id=?",
                                           (digest([project, legacy_key]),)).fetchone()
            if prior and prior["conversation"] == conversation:
                key = legacy_key
            public_coverage = observed["capture_coverage"].get("lastUserInput", {})
            if public_coverage.get("source") == "antigravity_transcript":
                key = ["antigravity-public-prompt-step", conversation, epoch, generation,
                       public_coverage["step_index"], digest(text)]
            payload = {"prompt": text, "source": "antigravity_public_hook",
                       "capture_coverage": observed["capture_coverage"],
                       "host_epoch": epoch}
            task = self.runtime.task(project, conversation)
            item, fresh = self.runtime._event(project, conversation, "UserPromptSubmit", payload,
                event_id=key, task_id=task["id"] if task else None,
                task_revision=task["revision"] if task else None,
                task_write_version=task["write_version"] if task else None,
                execution_epoch=task["execution_epoch"] if task else None,
                outcome="reported" if observed_public("UserPromptSubmit", payload) else "unknown")
            if fresh:
                self.runtime._work_capture_safely(project, item["id"], str(self.workspace),
                                                 raw=payload, conversation=conversation)

    def _stop(self, project, conversation, observed, event_key, phase_guard):
        with self.runtime.transaction():
            return self._stop_locked(project, conversation, observed, event_key, phase_guard)

    def _stop_locked(self, project, conversation, observed, event_key, phase_guard):
        """调用者持写事务；旧碰撞恢复须与完成回执原子提交。"""
        phase_guard()
        task = self.runtime.task(project, conversation)
        event, fresh = self.runtime._event(project, conversation, "AntigravityStop", observed,
                            event_id=["antigravity", conversation, event_key],
                            task_id=task["id"] if task else None,
                            task_revision=task["revision"] if task else None)
        if fresh and observed["fullyIdle"]:
            self.runtime._execution_fence(project, conversation, "antigravity_execution_stopped")
        if fresh:
            text = observed.get("finalModelOutput")
            complete = (observed["fullyIdle"] and observed["terminationReason"] in {"model_stop", "NO_TOOL_CALL"}
                        and not observed["error"]
                        and observed["capture_coverage"].get("workspace_association") == "installed_workspace"
                        and observed["capture_coverage"].get("trajectory_identity") == "current")
            final_coverage = observed["capture_coverage"].get("finalModelOutput", {})
            if isinstance(text, str) and (final_coverage.get("truncated")
                                         or final_coverage.get("full_original_available") is False):
                complete = False
            # 原生终止事实与公开说明分别留证。停止不等于任务交付，部分输出
            # 仍可作为 reported 工作记录，但不能触发交付案例或AI阶段摘要。
            if isinstance(text, str) and text.strip():
                payload = {"last_assistant_message": text, "source": "antigravity_public_hook",
                           "completion": "complete" if complete else "partial",
                           "termination_reason": observed["terminationReason"],
                           "capture_coverage": observed["capture_coverage"],
                           "host_epoch": self._epoch()}
                public, public_fresh = self.runtime._event(project, conversation, "Stop", payload,
                    event_id=["antigravity-public-stop", conversation, event_key],
                    task_id=task["id"] if task else None,
                    task_revision=task["revision"] if task else None,
                    task_write_version=task["write_version"] if task else None,
                    execution_epoch=task["execution_epoch"] if task else None,
                    outcome="reported" if observed_public("Stop", payload) else "unknown")
                if public_fresh:
                    if complete:
                        self.runtime._capture_delivery(project, conversation, task, public, payload)
                    self.runtime._work_capture_safely(project, public["id"], str(self.workspace), raw=payload,
                                                      summarize=complete, conversation=conversation)
            else:
                self.runtime._work_capture_safely(project, event["id"], str(self.workspace), raw=observed,
                                                  summarize=complete, conversation=conversation)
        # 后置签名也必须在相同快照内取得，不能接纳提交后出现的新任务。
        return {"decision": "stop"}, self._task_signature(project, conversation)

    def _pending_stop_recovery_reason(self, project, conversation, observed, event_key, old, epoch, generation):
        """只恢复可证实在旧 native ID 碰撞处回滚的 Stop；其他 pending 保留。"""
        r = self.runtime
        if old["state"] != "pending" or old["kind"] != "Stop":
            return "receipt_not_pending_stop"
        if old["host_epoch"] != epoch or old["generation"] != generation:
            return "receipt_boundary_changed"
        if old["task_signature"] != self._task_signature(project, conversation):
            return "receipt_task_changed"
        native_digest = _native_digest(observed)
        if native_digest != old["raw_digest"]:
            return "receipt_native_identity_changed"
        legacy_id = digest([project, ["antigravity", event_key]])
        collision = r.db.execute("SELECT conversation,type,payload FROM events WHERE id=?", (legacy_id,)).fetchone()
        if not collision or collision["conversation"] == conversation or collision["type"] != "AntigravityStop":
            return "legacy_cross_conversation_collision_unproven"
        recorded = json.loads(collision["payload"])
        if not isinstance(recorded, dict) or _native_digest(recorded) != old["raw_digest"]:
            return "legacy_native_identity_mismatch"
        # 新／旧原生与公开 Stop 任一已经落地都不能重放。即使出现不完整
        # 数据或关联投影，也不能仅凭另一会话占用旧 ID 推断副作用未发生。
        ids = [digest([project, value]) for value in (
            ["antigravity", conversation, event_key],
            ["antigravity-public-stop", conversation, event_key],
            ["antigravity-public-stop", event_key])]
        if r.db.execute("SELECT 1 FROM events WHERE conversation=? AND id IN (?,?,?) LIMIT 1",
                        (conversation, *ids)).fetchone():
            return "stop_effect_already_recorded"
        if r.db.execute("SELECT 1 FROM events WHERE project=? AND conversation=? AND created>=? LIMIT 1",
                        (project, conversation, old["created"])).fetchone():
            return "later_conversation_effects_present"
        if r.db.execute("SELECT 1 FROM work_memories WHERE project=? AND conversation=? AND created>=? LIMIT 1",
                        (project, conversation, old["created"])).fetchone():
            return "later_work_projection_present"
        if r.db.execute("SELECT 1 FROM delivery_rounds d JOIN task_states t ON t.project=d.project "
                        "AND t.id=d.task_id AND t.revision=d.revision AND t.write_version=d.write_version "
                        "WHERE d.project=? AND t.conversation=? AND d.created>=? LIMIT 1",
                        (project, conversation, old["created"])).fetchone():
            return "later_delivery_present"
        if r.db.execute("SELECT 1 FROM review_phases WHERE project=? AND conversation=? AND event_id IN (?,?,?) LIMIT 1",
                        (project, conversation, *ids)).fetchone():
            return "phase_effect_already_recorded"
        return None

    def _schedule(self, project, governance_epoch, phase_guard):
        r, token = self.runtime, None
        try:
            if r.config["scheduler"]["enabled"]:
                if r.scheduler_wake(project, expected_epoch=governance_epoch, _phase_guard=phase_guard):
                    token = r.claim_scheduler_worker(expected_epoch=governance_epoch, _phase_guard=phase_guard)
                    if token:
                        r._launch_scheduler(str(self.workspace), token)
            elif r.admin_policy(project)["enabled"]:
                from .admin import _settings
                from .admin_queue import claim_dispatch
                _settings(r.admin_effective_config(project))
                token = claim_dispatch(r, project, expected_epoch=governance_epoch, _phase_guard=phase_guard)
                if token:
                    r._launch_admin(str(self.workspace), token)
        except (OSError, ValueError, TypeError, sqlite3.Error):
            if token:
                try:
                    if r.config["scheduler"]["enabled"]:
                        r.release_scheduler_worker(token)
                    else:
                        from .admin_queue import release_dispatch
                        release_dispatch(r, project, token)
                except sqlite3.Error:
                    pass
            r._metric(project, "antigravity_schedule_failed")

    def handle(self, event, raw):
        if event not in EVENTS:
            raise ContractError("antigravity_unsupported_event")
        if event == "PreToolUse" and self.mode != "enforce":
            # observe 安装不接前置守卫；防止调用该入口暗中取得任务绑定。
            return {}
        project, conversation, mounted = self._common(raw)
        observed = self._observable(event, raw)
        if event in {"PreInvocation", "PostInvocation", "Stop"}:
            observed["capture_coverage"]["workspace_association"] = (
                "installed_workspace" if len(mounted) == 1 else "unknown_multi_workspace")
            if len(mounted) != 1:
                observed.pop("lastUserInput", None)
                observed.pop("finalModelOutput", None)
        # 诊断元数据不是事件身份；仅持久化不可逆摘要，正文仍经 clean。
        # 可选公开字段是捕获能力，不改变已存在的原生事件身份。升级或更换
        # modelName不能重放同一次Stop的fence/交付，也不回补旧回执的副作用。
        raw_digest = _native_digest(observed)
        r = self.runtime
        replayed_stop = False
        recover_stop = False
        with r.transaction():
            epoch = self._epoch()
            # 在缓存查找前处理计数回退，旧 PreInvocation 不能绕过轨迹失效。
            trajectory = self._trajectory(project, conversation, observed)
            if event in {"PreInvocation", "PostInvocation", "Stop"}:
                observed["capture_coverage"]["trajectory_identity"] = (
                    "ambiguous" if trajectory["ambiguous"] else "current")
                if trajectory["ambiguous"]:
                    observed.pop("lastUserInput", None)
                    observed.pop("finalModelOutput", None)
            # 0.14.0 的摘要先脱敏，无法可靠恢复原输入或忽略旧 modelName。
            # 同一代旧回执需要显式重置，不能把升级当作新事件而重放副作用。
            legacy = r.db.execute("SELECT 1 FROM antigravity_receipts WHERE project=? AND conversation=? "
                                  "AND generation>=? AND raw_digest NOT LIKE 'v2:%' LIMIT 1",
                                  (project, conversation, trajectory["reset_generation"])).fetchone()
            if legacy:
                if event == "PreInvocation":
                    return {"injectSteps": [{"ephemeralMessage": "Agent Learning: native event identity was upgraded. "
                        "A trusted antigravity-session-reset is required for this conversation before resuming plugin context."}]}
                return self._deny("native_identity_upgrade_requires_trusted_reset") if event == "PreToolUse" else (
                    {"decision": "stop"} if event == "Stop" else {})
            event_key = digest([epoch, trajectory["generation"], raw_digest])
            old = r.db.execute("SELECT * FROM antigravity_receipts WHERE project=? AND conversation=? AND event_key=?",
                               (project, conversation, event_key)).fetchone()
            prior_boundary = r.db.execute("SELECT host_epoch,generation FROM antigravity_receipts WHERE project=? AND conversation=? AND raw_digest=? "
                                          "ORDER BY created DESC LIMIT 1", (project, conversation, raw_digest)).fetchone()
            if (not old and prior_boundary and (prior_boundary["host_epoch"] != epoch or prior_boundary["generation"] != trajectory["generation"])
                    and trajectory["reset_generation"] <= prior_boundary["generation"]):
                return (self._deny("stale_native_event_requires_trusted_reset") if event == "PreToolUse" else
                        {"decision": "stop"} if event == "Stop" else {})
            if old:
                if old["host_epoch"] != epoch:
                    return (self._deny("stale_native_event") if event == "PreToolUse" else
                            {"decision": "stop"} if event == "Stop" else {})
                if event == "PreToolUse":
                    output = json.loads(old["output"])
                    if output.get("decision") == "deny":
                        return output
                    if old["state"] != "complete" or old["task_signature"] != self._task_signature(project, conversation):
                        return self._deny("native_event_replay_or_task_changed")
                    step = r.db.execute("SELECT closed,conflict FROM antigravity_steps WHERE project=? AND conversation=? "
                                        "AND generation=? AND step_index=?", (project, conversation, old["generation"], observed["stepIdx"])).fetchone()
                    if not step or step["closed"] or step["conflict"]:
                        return self._deny("native_operation_already_terminal_or_conflicted")
                    tool = observed["toolCall"]
                    if tool["name"] not in RECOVERY_TOOLS:
                        verdict = r.check_execution(project, conversation, r.task(project, conversation),
                            {"tool_name": tool["name"], "tool_input": tool["args"], "cwd": str(self.workspace)})
                        if verdict["decision"] == "denied":
                            return self._deny("native_replay_execution_denied: " + verdict["reason"])
                    # 宿主审批仍必须执行；缓存通过不能转换成自动 allow。
                    return {"decision": "ask", "reason": "Agent Learning: execution checks passed; retain host approval."}
                if old["state"] == "complete":
                    if event == "PreInvocation" and old["task_signature"] != self._receipt_signature(project, conversation, event):
                        return {}
                    if (event == "Stop" and observed["fullyIdle"] and not trajectory["ambiguous"]
                            and old["task_signature"] == self._task_signature(project, conversation)):
                        replayed_stop = True
                    else:
                        return json.loads(old["output"])
                # 不确定前次提交状态时不重投可能已消费通知，也不再次停止fence。
                if not replayed_stop and event in {"PreInvocation", "PostInvocation", "Stop"}:
                    recover_stop = (event == "Stop" and not trajectory["ambiguous"] and
                        self._pending_stop_recovery_reason(project, conversation, observed, event_key,
                                                           old, epoch, trajectory["generation"]) is None)
                    if not recover_stop:
                        return {"decision": "stop"} if event == "Stop" else {}
            task_signature = self._receipt_signature(project, conversation, event)
            phase_task_signature = self._task_signature(project, conversation)
            governance_epoch = r.host_epoch()
            if not old:
                r.db.execute("INSERT INTO antigravity_receipts(project,conversation,event_key,kind,host_epoch,generation,raw_digest,task_signature,created) "
                             "VALUES(?,?,?,?,?,?,?,?,?)", (project, conversation, event_key, event, epoch,
                                                           trajectory["generation"], raw_digest, task_signature, time.time()))
                if event in {"PreInvocation", "Stop"} and not trajectory["ambiguous"]:
                    try:
                        r.admin_observe_model(project, conversation, raw.get("modelName"), expected_epoch=epoch)
                    except ContractError:
                        r._metric(project, "admin_model_observation_rejected")
            operation, conflict, stale = (None, False, False)
            if event in {"PreToolUse", "PostToolUse"}:
                operation, conflict, stale = self._step(project, conversation, observed, trajectory, epoch)

        generation = trajectory["generation"]
        def phase_guard():
            self._assert_phase(project, conversation, epoch, generation, phase_task_signature)

        if replayed_stop:
            # 事件回执与后台派发不是一笔事务。允许失效前的同一阶段重试
            # 幂等派发；队列仍排除活跃/结果未知的模型调用，不重复记录/fence。
            self._schedule(project, governance_epoch, phase_guard)
            return json.loads(old["output"])
        if recover_stop:
            # 重新持锁核对；并发重投只有一个能落地。Stop、副作用和完成回执
            # 一起提交，崩溃会整体回滚；绝不恢复前置通知或工具执行。
            with r.transaction():
                self._assert_phase(project, conversation, epoch, generation)
                current = r.db.execute("SELECT * FROM antigravity_receipts WHERE project=? AND conversation=? AND event_key=?",
                                       (project, conversation, event_key)).fetchone()
                if current["state"] == "complete":
                    return json.loads(current["output"])
                phase_guard()
                if self._pending_stop_recovery_reason(project, conversation, observed, event_key,
                                                     current, epoch, generation) is not None:
                    return {"decision": "stop"}
                output, phase_task_signature = self._stop_locked(project, conversation, observed, event_key, phase_guard)
                r.db.execute("UPDATE antigravity_receipts SET state='complete',output=?,task_signature=? "
                             "WHERE project=? AND conversation=? AND event_key=?",
                             (encode(output), phase_task_signature, project, conversation, event_key))
                r._metric(project, "antigravity_stop_collision_recovered")
            if observed["fullyIdle"]:
                self._schedule(project, governance_epoch, phase_guard)
            return output
        if event in {"PreInvocation", "PostInvocation", "Stop"}:
            observed.update(host_epoch=epoch, generation=generation)
            if event != "Stop" and len(mounted) == 1 and not trajectory["ambiguous"]:
                self._transcript_public(project, conversation, observed, raw,
                                        epoch, generation, phase_guard)
        if event == "PreInvocation":
            if not trajectory["ambiguous"]:
                self._capture_prompt(project, conversation, observed, generation, epoch, phase_guard, mounted)
            access = r.register_context(project, conversation, expected_epoch=epoch, _phase_guard=phase_guard)
            fault = r.db.execute("SELECT signature,query FROM antigravity_faults WHERE project=? AND conversation=? AND host_epoch=? AND state='pending' "
                                 "ORDER BY created LIMIT 1", (project, conversation, epoch)).fetchone()
            outcome = {}
            text = r._context(project, conversation,
                              query=fault["query"] if fault else observed.get("lastUserInput", ""), context_handle=access["context_handle"],
                              expected_epoch=epoch, delivery="ephemeral", _phase_guard=phase_guard, _outcome=outcome,
                              workspace=str(self.workspace), raw=raw)
            if fault:
                with r.transaction():
                    phase_guard()
                    pending = outcome.get("retryable") or outcome.get("cooling") or outcome.get("delivery_pending")
                    r.db.execute("UPDATE antigravity_faults SET state=? WHERE project=? AND conversation=? AND signature=?",
                                 ("pending" if pending else "queried", project, conversation, fault["signature"]))
            output = {"injectSteps": [{"ephemeralMessage": text}]} if text else {}
        elif event == "Stop":
            output, phase_task_signature = self._stop(project, conversation, observed, event_key, phase_guard)
        elif event == "PostInvocation":
            output = self._public_response(project, conversation, observed, event_key, phase_guard)
        else:
            associated = self._tool_workspace(observed["toolCall"], mounted)
            if not associated or trajectory["ambiguous"] or stale:
                reason = ("workspace_association_unknown" if not associated else
                          "trajectory_identity_ambiguous" if trajectory["ambiguous"] else "stale_native_operation")
                self._record_unassociated(project, conversation, observed, event_key, reason, phase_guard)
                output = self._deny(reason) if event == "PreToolUse" else {}
            else:
                tool = observed["toolCall"]
                if event == "PreToolUse" and "context_handle" in tool["args"]:
                    try:
                        r.access_context(project, tool["args"]["context_handle"], conversation, epoch)
                    except ContractError:
                        output = self._deny("context_handle_native_conversation_mismatch")
                        with r.transaction():
                            phase_guard()
                            r.db.execute("UPDATE antigravity_receipts SET state='complete',output=? WHERE project=? AND conversation=? AND event_key=?",
                                         (encode(output), project, conversation, event_key))
                        return output
                if (event == "PreToolUse" and tool["name"] in {"mcp__agent_learning__" + name for name in ("task_update", "case_feedback", "knowledge_use")}
                        and "context_handle" not in tool["args"]):
                    output = self._deny("context_handle_required_for_native_write")
                    with r.transaction():
                        phase_guard()
                        r.db.execute("UPDATE antigravity_receipts SET state='complete',output=? WHERE project=? AND conversation=? AND event_key=?",
                                     (encode(output), project, conversation, event_key))
                    return output
                normalized = {"hook_event_name": event, "cwd": str(self.workspace), "session_id": conversation,
                              "tool_name": tool["name"], "tool_input": tool["args"], "tool_use_id": operation}
                if event == "PostToolUse":
                    # error 空只证明原生回调完成；没有退出码或后台结果，绝不宣称成功。
                    normalized["tool_response"] = {"isError": bool(observed["error"]), "error": observed["error"]}
                result = r.handle_hook(normalized, context_delivery=False, expected_epoch=epoch, _phase_guard=phase_guard,
                                       _adapter_event_key=event_key, _adapter_conflict=conflict)
                if event == "PreToolUse":
                    denial = result.get("hookSpecificOutput", {})
                    if conflict or denial.get("permissionDecision") == "deny":
                        output = self._deny("native_step_conflict") if conflict else self._deny(denial.get("permissionDecisionReason", "execution_denied"))
                    else:
                        output = {"decision": "ask", "reason": "Agent Learning: execution checks passed; retain host approval."}
                else:
                    if observed["error"]:
                        with r.transaction():
                            phase_guard()
                            # observe 的孤儿结果没有工具前置任务绑定。仅把当前任务
                            # 用作查询范围，不更改原事件或案例的 task_id / 证据归属。
                            operation_id = digest([conversation, operation])
                            binding = r.db.execute("SELECT task_id FROM operations WHERE project=? AND id=?", (project, operation_id)).fetchone()
                            if binding and binding["task_id"] is None:
                                self._orphan_fault(project, conversation, epoch, normalized)
                    output = {}
        with r.transaction():
            phase_guard()
            if event == "Stop":
                # 已完成 Stop 的可重派发边界是 fence 后的同一任务快照；
                # 只为本次完成写入，不猜测或回填升级前 complete 回执。
                r.db.execute("UPDATE antigravity_receipts SET state='complete',output=?,task_signature=? "
                             "WHERE project=? AND conversation=? AND event_key=?",
                             (encode(output), phase_task_signature, project, conversation, event_key))
            else:
                r.db.execute("UPDATE antigravity_receipts SET state='complete',output=? WHERE project=? AND conversation=? AND event_key=?",
                             (encode(output), project, conversation, event_key))
        if event == "Stop" and observed["fullyIdle"]:
            self._schedule(project, governance_epoch, phase_guard)
        if event == "PreInvocation" and r.config["software_updates"]["enabled"]:
            # Keep update prose out of the persisted native replay receipt. A new
            # safe event can deliver a newly cached version; replaying this event
            # repeats only its project knowledge output, never an old update ask.
            warning = r._schedule_software_update(str(self.workspace))
            if warning:
                output = {**output, "injectSteps": [*output.get("injectSteps", []), {"ephemeralMessage": warning}]}
            output = r._attach_software_update_notice(output, host="antigravity")
        return output


Adapter = AntigravityAdapter
