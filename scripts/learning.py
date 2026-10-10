#!/usr/bin/env python3
"""直接运行入口，无需pip安装。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from agent_learning.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
