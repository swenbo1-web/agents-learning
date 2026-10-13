#!/usr/bin/env python3
"""固定 DSH Hook 入口；核对发布副本后将事件交给 DshAdapter 与 Runtime 处理。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


MAX_INPUT_BYTES = 2 * 1024 * 1024
EVENTS = {
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "Stop", "Interrupt", "SubagentStart", "SubagentStop",
}
MODES = {"observe", "enforce"}
FAILURES = {
    "arguments_invalid": "the installed DSH hook arguments are invalid.",
    "input_too_large": "hook input exceeded the limit; this event was not recorded.",
    "input_invalid": "hook input was invalid; this event was not recorded.",
    "event_mismatch": "the event did not match the installed hook entry; this event was not recorded.",
    "release_unverified": "the installed runtime release could not be verified.",
    "runtime_unavailable": "runtime processing failed; event persistence and policy could not be confirmed.",
    "response_invalid": "runtime returned an invalid response; its result could not be confirmed.",
}


def _emit(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _failure(kind, mode, code):
    message = "Agent Learning [" + code + "]: " + FAILURES[code]
    if kind == "PreToolUse" and mode != "observe":
        _emit({
            "decision": "deny",
            "reason": message + " The configured execution guard could not verify this call.",
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": message + " The configured execution guard could not verify this call.",
            },
        })
    else:
        if mode is None:
            message += " Protection mode is unknown; execution policy was not confirmed."
        _emit({"systemMessage": message})


def _arguments(arguments):
    values, invalid = {}, False
    index = 0
    while index < len(arguments):
        flag = arguments[index]
        if flag not in {"--event", "--hook-mode", "--workspace", "--home"} or index + 1 >= len(arguments):
            invalid = True
            break
        value = arguments[index + 1]
        if flag in values:
            invalid = True
        elif flag == "--event" and value not in EVENTS or flag == "--hook-mode" and value not in MODES:
            invalid = True
        elif flag in {"--workspace", "--home"} and (not value.strip() or "\0" in value or not Path(value).is_absolute()):
            invalid = True
        else:
            values[flag] = value
        index += 2
    return values, invalid


def _mode(explicit, home_override=None):
    if explicit is not None:
        return explicit
    environment = os.environ.get("ALP_HOOK_MODE")
    if environment is not None:
        return environment if environment in MODES else None
    try:
        default_home = (Path(os.environ["LOCALAPPDATA"]) / "AgentLearning"
                        if os.environ.get("LOCALAPPDATA") else Path.home() / ".local/share/agent-learning")
        home = Path(home_override or os.environ.get("ALP_HOME") or default_home).resolve()
        config_path = Path(os.environ.get("ALP_CONFIG", home / "config.json"))
        supplied = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.is_file() else {}
        if not isinstance(supplied, dict):
            return None
        enabled = supplied.get("enforce_task_policy", False)
        return ("enforce" if enabled else "observe") if isinstance(enabled, bool) else None
    except (UnicodeError, ValueError, OSError, RecursionError):
        return None


def main(arguments=None):
    values, invalid = _arguments(sys.argv[1:] if arguments is None else arguments)
    kind = values.get("--event")
    explicit_mode = values.get("--hook-mode")
    mode = _mode(explicit_mode, values.get("--home"))
    environment_mode = os.environ.get("ALP_HOOK_MODE")
    if explicit_mode is None and environment_mode is not None and environment_mode not in MODES:
        invalid = True
    if invalid:
        _failure(kind, mode, "arguments_invalid")
        return 0
    if os.environ.get("ALP_DISABLED") == "1":
        _emit({})
        return 0
    try:
        raw_bytes = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw_bytes) > MAX_INPUT_BYTES:
            _failure(kind, mode, "input_too_large")
            return 0
        raw = json.loads(raw_bytes.decode("utf-8-sig"))
        if not isinstance(raw, dict):
            _failure(kind, mode, "input_invalid")
            return 0
    except (UnicodeError, ValueError, OSError, RecursionError):
        _failure(kind, mode, "input_invalid")
        return 0

    actual_kind = raw.get("hook_event_name") or raw.get("event")
    if kind is not None and actual_kind is not None and actual_kind != kind:
        _failure(kind, mode, "event_mismatch")
        return 0
    if kind is None:
        kind = actual_kind if isinstance(actual_kind, str) and actual_kind in EVENTS else None
    if kind is None:
        _failure(kind, mode, "input_invalid")
        return 0

    plugin_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(plugin_root))
    try:
        from agent_learning.release import validate_entry
        validate_entry(__file__)
    except Exception:
        _failure(kind, mode, "release_unverified")
        return 0
    try:
        from agent_learning.dsh import DshAdapter
        from agent_learning.runtime import Runtime
        runtime = Runtime(home=values.get("--home"), host="dsh")
        try:
            trusted_mode = explicit_mode or environment_mode or mode
            if trusted_mode not in MODES:
                _failure(kind, None, "runtime_unavailable")
                return 0
            output = DshAdapter(runtime, values.get("--workspace"), trusted_mode).handle(kind, raw)
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
