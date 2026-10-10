"""导出独立安装副本；不更改用户现有Codex配置或Hook信任记录。"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import shlex
import shutil
import sys
import sysconfig
from .runtime import ContractError


RESOURCE_DIRS = ("scripts", "hooks", "skills", ".codex-plugin", ".agents")
RESOURCE_FILES = ("README.md", "config.example.json")


def _resource_root():
    package_parent = Path(__file__).resolve().parents[1]
    # 源码导出优先；标准wheel资源放在解释器数据目录，--target安装位于包旁。
    for root in (package_parent, package_parent / "share/agent-learning",
                 Path(sysconfig.get_path("data")) / "share/agent-learning"):
        if (all((root / name).is_dir() for name in RESOURCE_DIRS)
                and all((root / name).is_file() for name in RESOURCE_FILES)):
            return root
    raise ContractError("plugin export resources are missing; use the complete source checkout or reinstall the wheel with bundled resources")


def install(destination):
    source = _resource_root()
    package = Path(__file__).resolve().parent
    target = Path(destination).resolve()
    for protected in (source, package):
        if target == protected or protected in target.parents or target in protected.parents:
            raise ContractError("installation must use a separate directory outside the source repository and installed package")
    if target.exists():
        raise ContractError("installation destination already exists; choose a new directory")
    target.mkdir(parents=True)
    shutil.copytree(package, target / "agent_learning", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in RESOURCE_DIRS:
        shutil.copytree(source / name, target / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in RESOURCE_FILES:
        shutil.copy2(source / name, target / name)
    mcp = {"mcpServers": {"agent_learning": {"command": sys.executable,
            "args": [str(target / "scripts/learning.py"), "serve", "--role", "worker"]}}}
    (target / ".mcp.json").write_text(json.dumps(mcp, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    hook_file = target / "hooks/hooks.json"
    hooks = json.loads(hook_file.read_text(encoding="utf-8"))
    for groups in hooks["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                handler["command"] = shlex.quote(sys.executable) + ' "${PLUGIN_ROOT}/hooks/dispatch.py"'
                # Hook环境变量由宿主提供，指向安装缓存；不用导出源路径执行代码。
                python = "'" + sys.executable.replace("'", "''") + "'"
                script = "& " + python + " ([System.IO.Path]::Combine($env:PLUGIN_ROOT, 'hooks', 'dispatch.py'))"
                encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
                handler.pop("command_windows", None)
                handler["commandWindows"] = "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded
    hook_file.write_text(json.dumps(hooks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 同时提供可追加的marketplace条目，不覆盖用户已有catalog。
    return {"status": "exported", "plugin_directory": str(target), "python": sys.executable,
            "next_step": "将此目录加入本地插件源并安装；在Codex界面审阅并信任Hooks。"}
