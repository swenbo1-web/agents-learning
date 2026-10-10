import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from agent_learning.install import install


class PackageTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("setuptools") and importlib.util.find_spec("wheel")
                         and importlib.util.find_spec("pip"), "Offline wheel build tools are optional")
    def test_wheel_includes_resources_and_supports_module_entry_and_export(self):
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="alp-wheel-package-") as temp:
            base = Path(temp)
            source = base / "build source"
            source.mkdir()
            for name in ("agent_learning", "scripts", "hooks", "skills", ".codex-plugin", ".agents"):
                shutil.copytree(source_root / name, source / name,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for name in ("pyproject.toml", "README.md", "config.example.json"):
                shutil.copy2(source_root / name, source / name)
            wheel_directory = base / "wheels"
            wheel_directory.mkdir()
            environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", ALP_DISABLED="1")
            environment.pop("ALP_CONFIG", None)
            build = subprocess.run(
                [sys.executable, "-c", "import sys; from setuptools.build_meta import build_wheel; build_wheel(sys.argv[1])",
                 str(wheel_directory)], cwd=source, env=environment, capture_output=True, timeout=30,
            )
            self.assertEqual(build.returncode, 0, build.stderr.decode("utf-8", "replace"))
            installed = base / "installed"
            wheels = list(wheel_directory.glob("*.whl"))
            self.assertEqual(len(wheels), 1)
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-deps", "--no-index", "--no-cache-dir",
                 "--target", str(installed), str(wheels[0])],
                cwd=base, env=environment, capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            environment.update(PYTHONPATH=str(installed), ALP_HOME=str(base / "home"))
            workspace = base / "workspace"
            workspace.mkdir()
            status = subprocess.run(
                [sys.executable, "-m", "agent_learning.cli", "status"],
                cwd=workspace, env=environment, capture_output=True, timeout=5,
            )
            self.assertEqual(status.returncode, 0, status.stderr.decode("utf-8", "replace"))
            self.assertIsInstance(json.loads(status.stdout.decode("utf-8"))["project_id"], str)
            exported = base / "wheel exported plugin"
            result = subprocess.run(
                [sys.executable, "-m", "agent_learning.cli", "install", str(exported)],
                cwd=workspace, env=environment, capture_output=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            self.assertEqual(json.loads(result.stdout.decode("utf-8"))["status"], "exported")
            for relative in ("hooks/dispatch.py", "scripts/learning.py", "skills/learning-worker/SKILL.md",
                             "skills/learning-admin/SKILL.md", ".codex-plugin/plugin.json",
                             ".agents/plugins/marketplace.json", "agent_learning/runtime.py", ".mcp.json"):
                self.assertTrue((exported / relative).is_file(), relative)
            manifest = json.loads((exported / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["version"], "0.2.0")

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is optional")
    def test_native_codex_catalog_and_install_in_isolated_home(self):
        with tempfile.TemporaryDirectory(prefix="alp-native-package-") as temp:
            root = Path(temp)
            exported = root / "exported plugin"
            install(exported)
            (root / "isolated-codex-home").mkdir()
            # 仅检查原生加载器，绝不改当前用户配置或启动模型。
            env = dict(os.environ, CODEX_HOME=str(root / "isolated-codex-home"), ALP_DISABLED="1")
            codex = shutil.which("codex")
            for args in (
                ["plugin", "marketplace", "add", "--json", str(exported)],
                ["plugin", "add", "--json", "agent-learning@agent-learning-local"],
            ):
                result = subprocess.run([codex, *args], cwd=root, env=env,
                                        capture_output=True, encoding="utf-8", timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertIsInstance(json.loads(result.stdout), dict)


if __name__ == "__main__":
    unittest.main()
