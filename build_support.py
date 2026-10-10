"""将现有插件资源随 Python 包构建，源码只维护一份资源。"""
from pathlib import Path
import shutil

from setuptools.command.build_py import build_py


class PluginBuildPy(build_py):
    def run(self):
        super().run()
        source = Path(__file__).resolve().parent
        target = Path(self.build_lib) / "agent_learning" / "_plugin_resources"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("scripts", "hooks", "skills", ".codex-plugin", ".agents"):
            shutil.copytree(source / name, target / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("README.md", "config.example.json", ".mcp.json"):
            shutil.copy2(source / name, target / name)
