#!/usr/bin/env python3
"""直接运行入口，无需pip安装。"""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from agent_learning.release import validate_entry

try:
    validate_entry(__file__)
except ValueError as exc:
    print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
    raise SystemExit(1)

from agent_learning.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
