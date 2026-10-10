#!/usr/bin/env python3
"""接收官方 Codex Hook JSON，记录与上下文处理统一交给 runtime。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any


MAX_INPUT_BYTES = 2 * 1024 * 1024
EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop",
          "Interrupt", "SubagentStart", "SubagentStop"}
MODES = {"observe", "enforce"}
FAILURES = {
    "arguments_invalid": "the installed hook arguments are invalid.",
    "input_too_large": "hook input exceeded the limit; this event was not recorded.",
    "input_invalid": "hook input was invalid; this event was not recorded.",
    "event_mismatch": "the event did not match the installed hook entry; this event was not recorded.",
    "release_unverified": "the installed runtime release could not be verified.",
    "runtime_unavailable": "runtime processing failed; event persistence and policy could not be confirmed.",
    "response_invalid": "runtime returned an invalid response; its result could not be confirmed.",
}


def _emit(output: dict[str, Any]) -> None:
    # ASCII 转义让 Windows 的终端编码不会影响 Hook 的 UTF-8 JSON 协议。
    sys.stdout.write(json.dumps(output, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _failure(kind: str | None, mode: str | None, code: str) -> None:
    # 只输出固定分类；原始输入、路径和异常可能带凭据，不能进入宿主反馈。
    message = "Agent Learning [" + code + "]: " + FAILURES[code]
    if kind == "PreToolUse" and mode == "enforce":
        _emit({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": message + " The configured execution guard could not verify this call.",
        }})
    else:
        if mode is None:
            message += " Protection mode is unknown; execution policy was not confirmed."
        _emit({"systemMessage": message})


def _arguments(arguments: list[str]) -> tuple[str | None, str | None, bool]:
    """固定事件名来自安装命令，坏 JSON 不能改变其 PreToolUse 故障策略。"""
    values: dict[str, str] = {}
    invalid = False
    index = 0
    while index < len(arguments):
        flag = arguments[index]
        if flag not in {"--event", "--hook-mode"} or index + 1 >= len(arguments):
            invalid = True
            break
        value = arguments[index + 1]
        allowed = EVENTS if flag == "--event" else MODES
        if value not in allowed or flag in values:
            invalid = True
        else:
            values[flag] = value
        index += 2
    return values.get("--event"), values.get("--hook-mode"), invalid


def _mode(explicit: str | None) -> str | None:
    if explicit is not None:
        return explicit
    environment = os.environ.get("ALP_HOOK_MODE")
    if environment is not None:
        return environment if environment in MODES else None
    # 不创建目录、不导入待验证代码。只读取能够确认的策略开关；坏配置不猜默认值。
    try:
        default_home = (Path(os.environ["LOCALAPPDATA"]) / "AgentLearning"
                        if os.environ.get("LOCALAPPDATA") else Path.home() / ".local/share/agent-learning")
        home = Path(os.environ.get("ALP_HOME") or default_home).resolve()
        config_path = Path(os.environ.get("ALP_CONFIG", home / "config.json"))
        supplied = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.is_file() else {}
        if not isinstance(supplied, dict):
            return None
        enabled = supplied.get("enforce_task_policy", False)
        return ("enforce" if enabled else "observe") if isinstance(enabled, bool) else None
    except (UnicodeError, ValueError, OSError, RecursionError):
        return None


def main(arguments: list[str] | None = None) -> int:
    kind, explicit_mode, invalid_arguments = _arguments(sys.argv[1:] if arguments is None else arguments)
    mode = _mode(explicit_mode)
    environment_mode = os.environ.get("ALP_HOOK_MODE")
    if explicit_mode is None and environment_mode is not None and environment_mode not in MODES:
        invalid_arguments = True
    if invalid_arguments:
        _failure(kind, mode, "arguments_invalid")
        return 0
    if os.environ.get("ALP_DISABLED") == "1":
        _emit({})
        return 0

    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            _failure(kind, mode, "input_too_large")
            return 0
        event = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(event, dict):
            _failure(kind, mode, "input_invalid")
            return 0
    except (UnicodeError, ValueError, OSError, RecursionError):
        _failure(kind, mode, "input_invalid")
        return 0

    actual_kind = event.get("hook_event_name")
    if kind is not None and actual_kind != kind:
        _failure(kind, mode, "event_mismatch")
        return 0
    if kind is None:
        kind = actual_kind if isinstance(actual_kind, str) and actual_kind in EVENTS else None
    if kind is None or any(not isinstance(event.get(field), str) or not event[field].strip()
                           for field in ("session_id", "cwd")):
        _failure(kind, mode, "input_invalid")
        return 0

    # 从脚本所属安装副本导入，避免依赖工作目录或另一个插件副本。
    plugin_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(plugin_root))
    try:
        from agent_learning.release import validate_entry

        validate_entry(__file__)
    except Exception:
        _failure(kind, mode, "release_unverified")
        return 0
    try:
        from agent_learning.runtime import Runtime

        # 持久目录由 Runtime 的 ALP_HOME/平台默认值统一决定。
        # PLUGIN_DATA 不参与选择，避免不同安装副本分裂记录。
        runtime = Runtime()
        try:
            # 固定入口／环境模式同时决定故障处理和正常守卫，避免两套策略互相矛盾。
            trusted_mode = explicit_mode or environment_mode
            mode = trusted_mode or ("enforce" if runtime.config["enforce_task_policy"] else "observe")
            runtime.config["enforce_task_policy"] = mode == "enforce"
            output = runtime.handle_hook(event)
        finally:
            runtime.close()
        if not isinstance(output, dict):
            _failure(kind, mode, "response_invalid")
            return 0
        _emit(output)
    except Exception:
        _failure(kind, mode, "runtime_unavailable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
