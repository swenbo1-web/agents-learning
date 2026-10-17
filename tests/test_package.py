import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from agent_learning import __version__
from agent_learning import install as installer
from agent_learning.install import install
from agent_learning.release import ReleaseError, validate_entry, validate_release_root
from agent_learning.runtime import ContractError


class PackageTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("setuptools") and importlib.util.find_spec("wheel")
                         and importlib.util.find_spec("pip"), "Offline wheel build tools are optional")
    def test_wheel_includes_resources_and_supports_module_entry_and_export(self):
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="alp-wheel-package-") as temp:
            base = Path(temp)
            source = base / "build source"
            source.mkdir()
            for name in ("agent_learning", "scripts", "hooks", "skills", ".codex-plugin", ".agents", "host_resources"):
                shutil.copytree(source_root / name, source / name,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for name in ("pyproject.toml", "README.md", "README.en.md", "LICENSE", "config.example.json", ".mcp.json", "build_support.py", "MANIFEST.in"):
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
            with zipfile.ZipFile(wheels[0]) as wheel:
                for module in ("semantic_retrieval", "knowledge_browser", "task_evaluation", "install_update", "software_updates", "history_sources", "history_import",
                               "work_memory", "work_files", "work_summaries", "dsh",
                               "panel_control", "panel_ui", "panel_browser", "admin_diagnostics", "dsh_panel", "ui_install",
                               "codex_panel", "codex_actions"):
                    self.assertIn(f"agent_learning/{module}.py", wheel.namelist())
                self.assertIn("agent_learning/_plugin_resources/.codex-plugin/plugin.json", wheel.namelist())
                self.assertIn("agent_learning/_plugin_resources/README.en.md", wheel.namelist())
                self.assertIn("agent_learning/_plugin_resources/skills/learning-admin/SKILL.md", wheel.namelist())
                self.assertIn("agent_learning/_plugin_resources/skills/learning-history/SKILL.md", wheel.namelist())
                self.assertIn("agent_learning/_plugin_resources/skills/learning-history/agents/openai.yaml", wheel.namelist())
                for prefix in ("skills", "host_resources/antigravity/skills", "host_resources/dsh/skills"):
                    for skill in (("learning-review", "learning-debug", "learning-panel")
                                  if prefix != "host_resources/dsh/skills" else ("learning-review", "learning-debug")):
                        for relative in ("SKILL.md", "agents/openai.yaml"):
                            path = f"agent_learning/_plugin_resources/{prefix}/{skill}/{relative}"
                            self.assertIn(path, wheel.namelist())
                            self.assertEqual(wheel.read(path), (source_root / prefix / skill / relative).read_bytes())
                for relative in ("sidecar.json", "main.mjs", "index.html"):
                    path = "agent_learning/_plugin_resources/host_resources/antigravity/sidecars/control/" + relative
                    self.assertIn(path, wheel.namelist())
                    self.assertEqual(wheel.read(path), (source_root / "host_resources/antigravity/sidecars/control" / relative).read_bytes())
                self.assertIn("agent_learning/_plugin_resources/host_resources/antigravity/plugin.json", wheel.namelist())
                self.assertIn("agent_learning/_plugin_resources/host_resources/antigravity/rules/learning.md", wheel.namelist())
                self.assertIn("agent_learning/_plugin_resources/host_resources/antigravity/skills/learning-history/SKILL.md", wheel.namelist())
                self.assertIn("agent_learning/_plugin_resources/host_resources/dsh/plugin.json", wheel.namelist())
                self.assertIn("agent_learning/_plugin_resources/host_resources/dsh/package.json", wheel.namelist())
                self.assertIn("agent_learning/_plugin_resources/host_resources/dsh/host/index.mjs", wheel.namelist())
                reviewer = "host_resources/dsh/reviewer/runner.mjs"
                self.assertEqual(wheel.read("agent_learning/_plugin_resources/" + reviewer),
                                 (source_root / reviewer).read_bytes())
                self.assertIn("agent_learning/_plugin_resources/hooks/dsh_dispatch.py", wheel.namelist())
                self.assertEqual(wheel.read("agent_learning/_plugin_resources/LICENSE"), (source_root / "LICENSE").read_bytes())
                licenses = [name for name in wheel.namelist() if ".dist-info/" in name and name.endswith("/LICENSE")]
                self.assertTrue(licenses, "wheel must retain its distribution license")
                self.assertEqual(wheel.read(licenses[0]), (source_root / "LICENSE").read_bytes())
                self.assertFalse(any("share/agent-learning" in item for item in wheel.namelist()))
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
            # A directly installed wheel has its scripts under _plugin_resources.
            # Capture the child argv, then actually run its harmless cached read.
            background_probe = (
                "import contextlib,json,subprocess; from pathlib import Path; from unittest.mock import patch; "
                "from agent_learning.runtime import Runtime; "
                "r=Runtime(); "
                "probe=patch('subprocess.Popen'); child=probe.start(); "
                "r._launch_background(Path.cwd(),'update-status',[]); "
                "argv=child.call_args.args[0]; env=child.call_args.kwargs['env']; "
                "probe.stop(); r.close(); "
                "assert '_plugin_resources' in argv[2],argv; "
                "result=subprocess.run(argv,env=env,capture_output=True,timeout=10); "
                "assert result.returncode==0,result.stderr; "
                "print(json.dumps({'state':json.loads(result.stdout)['state'],'entry':argv[2]}))"
            )
            background = subprocess.run([sys.executable, "-B", "-c", background_probe],
                cwd=workspace, env=environment, capture_output=True, timeout=15)
            self.assertEqual(background.returncode, 0, background.stderr.decode("utf-8", "replace"))
            self.assertEqual(json.loads(background.stdout)["state"], "unchecked")
            exported = base / "wheel exported plugin"
            result = subprocess.run(
                [sys.executable, "-m", "agent_learning.cli", "install", str(exported),
                 "--runtime-store", str(base / "runtime releases")],
                cwd=workspace, env=environment, capture_output=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            self.assertEqual(json.loads(result.stdout.decode("utf-8"))["status"], "exported")
            for relative in ("hooks/dispatch.py", "scripts/learning.py", "skills/learning-worker/SKILL.md",
                             "skills/learning-admin/SKILL.md", "skills/learning-review/SKILL.md",
                             "skills/learning-review/agents/openai.yaml", "skills/learning-debug/SKILL.md",
                             "skills/learning-debug/agents/openai.yaml", ".codex-plugin/plugin.json",
                             ".agents/plugins/marketplace.json", "agent_learning/runtime.py", ".mcp.json", "LICENSE"):
                self.assertTrue((exported / relative).is_file(), relative)
            for module in ("semantic_retrieval", "knowledge_browser", "task_evaluation", "install_update", "software_updates", "history_sources", "history_import"):
                self.assertTrue((exported / "agent_learning" / f"{module}.py").is_file(), module)
            self.assertEqual((exported / "LICENSE").read_bytes(), (source_root / "LICENSE").read_bytes())
            manifest = json.loads((exported / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["version"], __version__)
            release = json.loads((exported / "runtime-release.json").read_text(encoding="utf-8"))
            self.assertEqual(validate_release_root(Path(release["runtime_directory"]))["release_id"], release["release_id"])
            self.assertTrue((Path(release["runtime_directory"]) / "skills/learning-review/SKILL.md").is_file())
            for relative in ("SKILL.md", "agents/openai.yaml"):
                self.assertEqual((Path(release["runtime_directory"]) / "skills/learning-debug" / relative).read_bytes(),
                                 (exported / "skills/learning-debug" / relative).read_bytes())
            native_export = base / "wheel exported Antigravity plugin"
            native_home = base / "Antigravity knowledge"
            native = subprocess.run(
                [sys.executable, "-B", "-m", "agent_learning.cli", "--workspace", str(workspace),
                 "--home", str(native_home), "install", str(native_export), "--host", "antigravity",
                 "--runtime-store", str(base / "runtime releases")],
                cwd=workspace, env=environment, capture_output=True, timeout=10,
            )
            self.assertEqual(native.returncode, 0, native.stderr.decode("utf-8", "replace"))
            native_receipt = json.loads(native.stdout.decode("utf-8"))
            self.assertEqual(native_receipt["target_host"], "antigravity")
            self.assertEqual(native_receipt["runtime_directory"], release["runtime_directory"])
            self.assertFalse(native_home.exists())
            self.assertFalse((native_export / ".codex-plugin").exists())
            for relative in ("plugin.json", "hooks.json", "mcp_config.json", "rules/learning.md",
                             "skills/learning-worker/SKILL.md", "skills/learning-review/SKILL.md",
                             "skills/learning-review/agents/openai.yaml", "skills/learning-debug/SKILL.md",
                             "skills/learning-debug/agents/openai.yaml", "hooks/antigravity_dispatch.py"):
                self.assertTrue((native_export / relative).is_file(), relative)

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is optional")
    def test_native_codex_catalog_and_install_in_isolated_home(self):
        with tempfile.TemporaryDirectory(prefix="alp-native-package-") as temp:
            root = Path(temp)
            exported = root / "exported plugin"
            install(exported, runtime_store=root / "runtime releases")
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

    def test_missing_resource_is_rejected_before_creating_directories(self):
        with tempfile.TemporaryDirectory(prefix="alp-invalid-resource-") as temp:
            base = Path(temp)
            source = base / "broken source"
            source.mkdir()
            target, store = base / "export", base / "releases"
            with patch.object(installer, "_resource_root", return_value=source):
                with self.assertRaisesRegex(ContractError, "resource is missing"):
                    install(target, runtime_store=store)
            self.assertFalse(target.exists())
            self.assertFalse(store.exists())

    def test_runtime_copy_failure_is_atomic_and_retry_succeeds(self):
        with tempfile.TemporaryDirectory(prefix="alp-atomic-release-") as temp:
            base = Path(temp)
            target, store = base / "export", base / "releases"
            def interrupted(source, package, staging):
                (staging / "partial").write_text("partial", encoding="utf-8")
                raise OSError("copy interrupted")
            with patch.object(installer, "_copy_payload", side_effect=interrupted):
                with self.assertRaises(ContractError):
                    install(target, runtime_store=store)
            self.assertFalse(target.exists())
            self.assertEqual(list(store.iterdir()), [])
            result = install(target, runtime_store=store)
            self.assertEqual(result["status"], "exported")
            self.assertEqual(validate_release_root(result["runtime_directory"])["version"], __version__)

    def test_export_copy_failure_leaves_only_complete_release_and_allows_retry(self):
        with tempfile.TemporaryDirectory(prefix="alp-atomic-export-") as temp:
            base = Path(temp)
            target, store = base / "export", base / "releases"
            original = installer._copy_payload
            calls = 0
            def interrupted(source, package, staging):
                nonlocal calls
                calls += 1
                original(source, package, staging)
                if calls == 2:
                    raise OSError("export interrupted")
            with patch.object(installer, "_copy_payload", side_effect=interrupted):
                with self.assertRaises(ContractError):
                    install(target, runtime_store=store)
            self.assertFalse(target.exists())
            self.assertFalse(list(base.glob(".alp-staging-*")))
            releases = list(store.iterdir())
            self.assertEqual(len(releases), 1)
            validate_release_root(releases[0])
            result = install(target, runtime_store=store)
            self.assertEqual(Path(result["runtime_directory"]).resolve(), releases[0].resolve())
            self.assertEqual(len(list(store.iterdir())), 1)

    def test_existing_target_is_preserved_without_creating_runtime_store(self):
        with tempfile.TemporaryDirectory(prefix="alp-preserve-export-") as temp:
            base = Path(temp)
            target, store = base / "export", base / "releases"
            target.mkdir()
            sentinel = target / "user file"
            sentinel.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "already exists"):
                install(target, runtime_store=store)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertFalse(store.exists())

    def test_stable_mcp_and_hook_work_after_export_moves_and_is_deleted(self):
        with tempfile.TemporaryDirectory(prefix="alp-stable-entries-") as temp:
            base = Path(temp)
            target, store = base / "export", base / "releases"
            result = install(target, runtime_store=store)
            release = Path(result["runtime_directory"])
            mcp = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["agent_learning"]
            handler = json.loads((target / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]["hooks"][0]
            self.assertEqual(Path(mcp["args"][0]).parents[1], release)
            command = handler["commandWindows"] if os.name == "nt" else handler["command"]
            moved = base / "moved export"
            target.rename(moved)
            shutil.rmtree(moved)
            workspace = base / "project"
            workspace.mkdir()
            environment = dict(os.environ, ALP_HOME=str(base / "data"), PYTHONDONTWRITEBYTECODE="1")
            for name in ("ALP_CONFIG", "ALP_DISABLED", "ALP_HOOK_MODE"):
                environment.pop(name, None)
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "package-check", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
            response = subprocess.run([mcp["command"], *mcp["args"]],
                input=("\n".join(json.dumps(item) for item in messages) + "\n").encode("utf-8"),
                cwd=workspace, env=environment, capture_output=True, timeout=5)
            self.assertEqual(response.returncode, 0, response.stderr.decode("utf-8", "replace"))
            replies = [json.loads(line) for line in response.stdout.decode("utf-8").splitlines()]
            names = {tool["name"] for tool in replies[-1]["result"]["tools"]}
            self.assertIn("knowledge_search", names)
            self.assertNotIn("submit_candidate", names)
            self.assertEqual(replies[0]["result"]["serverInfo"]["version"], __version__)
            event = {"hook_event_name": "SessionStart", "session_id": "stable-chat", "cwd": str(workspace), "source": "startup"}
            response = subprocess.run(command, shell=True, input=json.dumps(event).encode("utf-8"),
                cwd=workspace, env=dict(environment, PLUGIN_ROOT=str(base / "missing cache")), capture_output=True, timeout=10)
            self.assertEqual(response.returncode, 0, response.stderr.decode("utf-8", "replace"))
            output = json.loads(response.stdout.decode("utf-8"))
            self.assertIn("hookSpecificOutput", output)
            self.assertNotIn("systemMessage", output)
            self.assertEqual(validate_entry(release / "scripts/learning.py")["release_id"], result["release_id"])

    def test_changed_release_is_rejected_by_both_entries_and_missing_manifest_is_not_source(self):
        with tempfile.TemporaryDirectory(prefix="alp-release-integrity-") as temp:
            base = Path(temp)
            result = install(base / "export", runtime_store=base / "releases", hook_mode="enforce")
            release = Path(result["runtime_directory"])
            (release / "README.md").write_text("changed", encoding="utf-8")
            with self.assertRaises(ReleaseError):
                validate_entry(release / "scripts/learning.py")
            workspace = base / "project"
            workspace.mkdir()
            environment = dict(os.environ, ALP_HOME=str(base / "data"), PYTHONDONTWRITEBYTECODE="1")
            for name in ("ALP_CONFIG", "ALP_DISABLED", "ALP_HOOK_MODE"):
                environment.pop(name, None)
            response = subprocess.run([sys.executable, str(release / "scripts/learning.py"), "status"],
                cwd=workspace, env=environment, capture_output=True, timeout=5)
            self.assertEqual(response.returncode, 1)
            self.assertEqual(response.stdout, b"")
            self.assertEqual(json.loads(response.stderr.decode("utf-8"))["status"], "error")
            event = {"hook_event_name": "PreToolUse", "session_id": "guard-chat", "cwd": str(workspace), "tool_name": "Bash"}
            response = subprocess.run([sys.executable, str(release / "hooks/dispatch.py"), "--event", "PreToolUse", "--hook-mode", "enforce"],
                input=json.dumps(event).encode("utf-8"), cwd=workspace, env=environment, capture_output=True, timeout=5)
            self.assertEqual(response.returncode, 0)
            self.assertEqual(json.loads(response.stdout.decode("utf-8"))["hookSpecificOutput"]["permissionDecision"], "deny")
            (release / "release.json").unlink()
            with self.assertRaisesRegex(ReleaseError, "manifest is missing"):
                validate_entry(release / "scripts/learning.py")
            self.assertIsNone(validate_entry(Path(__file__).parents[1] / "scripts/learning.py"))

    def test_fixed_release_can_export_again_and_reuse_its_store(self):
        with tempfile.TemporaryDirectory(prefix="alp-release-reexport-") as temp:
            base = Path(temp)
            store = base / "releases"
            first = install(base / "first export", runtime_store=store)
            environment = dict(os.environ, ALP_DISABLED="1", PYTHONDONTWRITEBYTECODE="1")
            response = subprocess.run([sys.executable, str(Path(first["runtime_directory"]) / "scripts/learning.py"),
                "install", str(base / "second export"), "--runtime-store", str(store)],
                cwd=base, env=environment, capture_output=True, timeout=5)
            self.assertEqual(response.returncode, 0, response.stderr.decode("utf-8", "replace"))
            self.assertEqual(json.loads(response.stdout.decode("utf-8"))["release_id"], first["release_id"])
            self.assertEqual(len(list(store.iterdir())), 1)

    def test_default_runtime_store_ignores_knowledge_home_and_accepts_explicit_environment(self):
        with tempfile.TemporaryDirectory(prefix="alp-store-selection-") as temp:
            base = Path(temp)
            with patch.dict(os.environ, {"ALP_HOME": str(base / "knowledge")}, clear=False):
                os.environ.pop("ALP_RUNTIME_STORE", None)
                self.assertNotEqual(installer._default_store(), base / "knowledge")
            with patch.dict(os.environ, {"ALP_RUNTIME_STORE": str(base / "explicit")}, clear=False):
                self.assertEqual(installer._default_store(), base / "explicit")


if __name__ == "__main__":
    unittest.main()
