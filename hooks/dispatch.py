#!/usr/bin/env python3
"""接收官方 Codex Hook JSON，记录与上下文处理统一交给 runtime。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any


MAX_INPUT_BYTES = 2 * 1024 * 1024


def _emit(output: dict[str, Any]) -> None:
    # ASCII 转义让 Windows 的终端编码不会影响 Hook 的 UTF-8 JSON 协议。
    sys.stdout.write(json.dumps(output, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _warning(message: str) -> None:
    # 不返回 block/continue:false，也不打印原始事件、凭据或异常详情。
    _emit({"systemMessage": "Agent Learning: " + message})


def main() -> int:
    if os.environ.get("ALP_DISABLED") == "1":
        _emit({})
        return 0

    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            _warning("hook input exceeded the limit; this event was not recorded.")
            return 0
        event = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(event, dict):
            _warning("hook input must be a JSON object; this event was not recorded.")
            return 0
    except (UnicodeError, ValueError, OSError):
        _warning("hook input was invalid; this event was not recorded.")
        return 0

    # 从脚本所属安装副本导入，避免依赖工作目录或另一个插件副本。
    plugin_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(plugin_root))
    try:
        from agent_learning.runtime import Runtime

        # 持久目录由 Runtime 的 ALP_HOME/平台默认值统一决定。
        # PLUGIN_DATA 不参与选择，避免不同安装副本分裂记录。
        runtime = Runtime()
        try:
            output = runtime.handle_hook(event)
        finally:
            runtime.close()
        if not isinstance(output, dict):
            _warning("runtime returned an invalid response; recording was not confirmed.")
            return 0
        _emit(output)
    except Exception:
        _warning("runtime could not confirm this event; check the local service status.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
