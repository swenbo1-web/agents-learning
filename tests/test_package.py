import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from agent_learning.install import install


class PackageTests(unittest.TestCase):
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
