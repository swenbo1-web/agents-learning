#!/usr/bin/env python3
"""直接运行入口，无需pip安装。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from agent_learning.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
