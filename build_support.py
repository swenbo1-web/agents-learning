"""将现有插件资源随 Python 包构建，源码只维护一份资源。"""
from pathlib import Path
import shutil
import stat

from setuptools.command.build_py import build_py


def _resource_target(build_lib):
    build_root = Path(build_lib).absolute()
    target = build_root / "agent_learning" / "_plugin_resources"
    for path in (build_root, target.parent, target):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise ValueError("plugin resource output must not contain links or junctions")
    if target.resolve() != build_root.resolve() / "agent_learning" / "_plugin_resources":
        raise ValueError("plugin resource output escaped the build directory")
    return target


class PluginBuildPy(build_py):
    def run(self):
        source = Path(__file__).resolve().parent
        # build_lib can be reused between wheel builds. Only this generated
        # resource tree belongs to us; merging would retain removed resources.
        # Reject links before removing it so a build cannot redirect cleanup.
        _resource_target(self.build_lib)
        super().run()
        target = _resource_target(self.build_lib)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        for name in ("scripts", "hooks", "skills", ".codex-plugin", ".agents", "host_resources"):
            shutil.copytree(source / name, target / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("README.md", "README.en.md", "LICENSE", "config.example.json", ".mcp.json"):
            shutil.copy2(source / name, target / name)
