"""导出独立安装副本；不更改用户现有Codex配置或Hook信任记录。"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import shlex
import shutil
import sys
from .runtime import ContractError


def install(destination):
    source = Path(__file__).resolve().parents[1]
    target = Path(destination).resolve()
    if target == source or source in target.parents or target in source.parents:
        raise ContractError("installation must use a separate directory outside the source repository")
    if target.exists():
        raise ContractError("installation destination already exists; choose a new directory")
    target.mkdir(parents=True)
    for name in ("agent_learning", "scripts", "hooks", "skills", ".codex-plugin", ".agents"):
        shutil.copytree(source / name, target / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("README.md", "config.example.json"):
        shutil.copy2(source / name, target / name)
    mcp = {"mcpServers": {"agent_learning": {"command": sys.executable,
            "args": [str(target / "scripts/learning.py"), "serve", "--role", "worker"]}}}
    (target / ".mcp.json").write_text(json.dumps(mcp, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    hook_file = target / "hooks/hooks.json"
    hooks = json.loads(hook_file.read_text(encoding="utf-8"))
    arguments = [sys.executable, str(target / "hooks/dispatch.py")]
    for groups in hooks["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                handler["command"] = shlex.join(arguments)
                script = "& " + " ".join("'" + argument.replace("'", "''") + "'" for argument in arguments)
                encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
                handler.pop("command_windows", None)
                handler["commandWindows"] = "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded
    hook_file.write_text(json.dumps(hooks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 同时提供可追加的marketplace条目，不覆盖用户已有catalog。
    return {"status": "exported", "plugin_directory": str(target), "python": sys.executable,
            "next_step": "将此目录加入本地插件源并安装；在Codex界面审阅并信任Hooks。"}
