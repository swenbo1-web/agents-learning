#!/usr/bin/env python3
"""固定 DSH 工具执行入口；核对发布副本后通过 MCP Server 执行单次工具调用。"""
from __future__ import annotations

import json
from pathlib import Path
import sys


MAX_INPUT_BYTES = 2 * 1024 * 1024


def _emit(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main():
    try:
        raw_bytes = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw_bytes) > MAX_INPUT_BYTES:
            _emit({"ok": False, "error": "tool input exceeded byte limit"})
            return 0
        payload = json.loads(raw_bytes.decode("utf-8-sig"))
        if not isinstance(payload, dict):
            _emit({"ok": False, "error": "tool input must be a JSON object"})
            return 0
    except (UnicodeError, ValueError, OSError, RecursionError) as exc:
        _emit({"ok": False, "error": f"invalid tool input: {exc}"})
        return 0

    plugin_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(plugin_root))
    try:
        from agent_learning.release import validate_entry
        validate_entry(__file__)
    except Exception as exc:
        _emit({"ok": False, "error": f"release verification failed: {exc}"})
        return 0

    runtime = None
    try:
        from agent_learning.mcp import Server
        from agent_learning.runtime import Runtime
        workspace = payload.get("workspace")
        tool_name = payload.get("tool")
        tool_args = payload.get("args")
        if not isinstance(workspace, str) or not workspace.strip():
            _emit({"ok": False, "error": "workspace is required"})
            return 0
        if not isinstance(tool_name, str) or not isinstance(tool_args, dict):
            _emit({"ok": False, "error": "tool name and object args are required"})
            return 0
        runtime = Runtime(home=payload.get("home"), host="dsh")
        project = runtime.project(workspace)
        native_session = payload.get("session_id")
        agent_id = payload.get("agent_id")
        if "context_handle" not in tool_args and isinstance(native_session, str) and native_session.strip():
            conversation = runtime.host_conversation(native_session.strip())
            access = runtime.register_context(project, conversation, agent_id=agent_id)
            tool_args = {**tool_args, "context_handle": access["context_handle"]}
        server = Server(runtime, project, "worker", workspace=workspace)
        result = server.call(tool_name, tool_args)
        _emit({"ok": True, "result": result})
    except Exception as exc:
        _emit({"ok": False, "error": str(exc)})
    finally:
        if runtime is not None:
            runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
