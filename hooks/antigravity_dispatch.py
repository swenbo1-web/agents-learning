#!/usr/bin/env python3
"""固定 Antigravity Hook 入口；安装参数选择事件、项目、数据目录和故障模式。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


MAX_INPUT_BYTES = 2 * 1024 * 1024
EVENTS = {"PreInvocation", "PreToolUse", "PostToolUse", "Stop"}
MODES = {"observe", "enforce"}


def _emit(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _failure(event, mode, code):
    # 固定故障分类，不回传异常、JSON、路径或潜在凭据。
    if event == "PreToolUse" and mode != "observe":
        _emit({"decision": "deny", "reason": "Agent Learning [" + code + "]: execution checks could not be confirmed."})
    else:
        _emit({"decision": "stop"} if event == "Stop" else {})


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
    return values, invalid or set(values) != {"--event", "--hook-mode", "--workspace", "--home"}


def main(arguments=None):
    values, invalid = _arguments(sys.argv[1:] if arguments is None else arguments)
    event, mode = values.get("--event"), values.get("--hook-mode")
    if invalid:
        _failure(event, mode, "arguments_invalid")
        return 0
    if os.environ.get("ALP_DISABLED") == "1":
        _emit({"decision": "stop"} if event == "Stop" else {})
        return 0
    try:
        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            _failure(event, mode, "input_too_large")
            return 0
        raw = json.loads(data.decode("utf-8-sig"))
        if not isinstance(raw, dict):
            _failure(event, mode, "input_invalid")
            return 0
    except (ValueError, UnicodeError, OSError, RecursionError):
        _failure(event, mode, "input_invalid")
        return 0
    # Native payload不含事件名；若外部显式附加名称，不允许与固定入口矛盾。
    if any(field in raw and raw[field] != event for field in ("hookEventName", "hook_event_name")):
        _failure(event, mode, "event_mismatch")
        return 0
    plugin_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(plugin_root))
    try:
        from agent_learning.release import validate_entry
        validate_entry(__file__)
    except Exception:
        _failure(event, mode, "release_unverified")
        return 0
    try:
        from agent_learning.antigravity import AntigravityAdapter
        from agent_learning.runtime import Runtime
        runtime = Runtime(home=values["--home"], host="antigravity")
        try:
            output = AntigravityAdapter(runtime, values["--workspace"], mode).handle(event, raw)
        finally:
            runtime.close()
        if not isinstance(output, dict):
            _failure(event, mode, "response_invalid")
        else:
            _emit(output)
    except Exception:
        _failure(event, mode, "runtime_unavailable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
