"""双宿主导出和覆盖更新的隔离保护回归，不加载用户插件或知识库。"""
import base64
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.cli import main
from agent_learning.install import install, _hook_commands
from agent_learning.install_update import INSTALL_MANIFEST, inspect_installation, snapshot
from agent_learning.release import validate_release_root
from agent_learning.runtime import ContractError


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class AntigravityInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-antigravity-install-")
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self._cleanup_root)
        self.workspace = self.root / "project's 中文 workspace"
        self.workspace.mkdir()
        self.home = self.root / "shared knowledge"
        self.target = self.root / "antigravity plugin"
        self.store = self.root / "fixed releases"

    def _cleanup_root(self):
        import shutil
        from agent_learning.release import filesystem_path
        if self.root.exists() and self.root.name.startswith("alp-antigravity-install-"):
            shutil.rmtree(filesystem_path(self.root), ignore_errors=True)
        self.temp.cleanup()

    def initial(self, mode=None):
        return install(self.target, runtime_store=self.store, hook_mode=mode,
                       host="antigravity", workspace=self.workspace, home=self.home)

    def test_native_export_pins_identity_paths_and_omits_observe_permission_hook(self):
        result = self.initial()
        self.assertEqual(result["target_host"], "antigravity")
        self.assertEqual(result["hook_mode"], "observe")
        self.assertFalse(self.home.exists(), "export must not open or create a knowledge database")
        self.assertFalse((self.target / ".codex-plugin").exists())
        self.assertFalse((self.target / ".mcp.json").exists())
        self.assertFalse((self.target / ".agents").exists())
        plugin = read_json(self.target / "plugin.json")
        self.assertEqual(set(plugin), {"name", "description"})
        self.assertEqual(plugin["name"], "agent-learning")
        hooks = read_json(self.target / "hooks.json")["agent-learning"]
        self.assertEqual(set(hooks), {"PreInvocation", "PostToolUse", "Stop"})
        self.assertNotIn("permissionOverrides", json.dumps(hooks))
        runtime = Path(result["runtime_directory"])
        mcp = read_json(self.target / "mcp_config.json")["mcpServers"]["agent_learning"]
        self.assertEqual(mcp["command"], result["python"])
        self.assertEqual(mcp["args"], ["-B", str(runtime / "scripts/learning.py"), "--workspace", str(self.workspace),
                         "--home", str(self.home), "serve", "--role", "worker", "--host", "antigravity"])
        self.assertTrue((self.target / "rules/learning.md").is_file())
        self.assertTrue((self.target / "skills/learning-worker/SKILL.md").is_file())
        self.assertTrue((self.target / "skills/learning-review/SKILL.md").is_file())
        self.assertTrue((self.target / "skills/learning-review/agents/openai.yaml").is_file())
        self.assertTrue((self.target / "skills/learning-debug/SKILL.md").is_file())
        self.assertTrue((self.target / "skills/learning-debug/agents/openai.yaml").is_file())
        self.assertEqual(read_json(self.target / INSTALL_MANIFEST)["target_host"], "antigravity")
        self.assertEqual(inspect_installation(self.target, _hook_commands)["target_host"], "antigravity")
        validate_release_root(runtime)
        self.assertTrue((runtime / ".codex-plugin/plugin.json").is_file())
        self.assertTrue((runtime / "host_resources/antigravity/plugin.json").is_file())
        self.assertEqual((runtime / "host_resources/antigravity/skills/learning-review/SKILL.md").read_bytes(),
                         (self.target / "skills/learning-review/SKILL.md").read_bytes())
        for relative in ("SKILL.md", "agents/openai.yaml"):
            self.assertEqual((runtime / "host_resources/antigravity/skills/learning-debug" / relative).read_bytes(),
                             (self.target / "skills/learning-debug" / relative).read_bytes())

    def test_enforce_hooks_use_pinned_dispatcher_with_native_command_field_only(self):
        result = self.initial("enforce")
        runtime = Path(result["runtime_directory"])
        hooks = read_json(self.target / "hooks.json")["agent-learning"]
        self.assertIn("PreToolUse", hooks)
        for event, handlers in hooks.items():
            command, windows = _hook_commands(result["python"], runtime / "hooks/antigravity_dispatch.py",
                event, "enforce", host="antigravity", workspace=str(self.workspace), home=str(self.home))
            for entry in handlers:
                for handler in entry["hooks"] if "hooks" in entry else [entry]:
                    self.assertEqual(handler["command"], windows if os.name == "nt" else command)
                    self.assertEqual(set(handler), {"type", "command", "timeout"})
                    if os.name == "nt":
                        decoded = base64.b64decode(handler["command"].split()[-1]).decode("utf-16-le")
                        self.assertIn("'-B'", decoded)
                        self.assertIn(str(self.workspace).replace("'", "''"), decoded)
                        self.assertIn(str(self.home), decoded)
                        self.assertIn("'--event' '" + event + "'", decoded)

    def test_same_release_is_shared_by_two_separate_host_exports(self):
        native = self.initial()
        codex = install(self.root / "codex plugin", runtime_store=self.store)
        self.assertEqual(native["runtime_directory"], codex["runtime_directory"])
        self.assertEqual(codex["target_host"], "codex")
        self.assertFalse((self.root / "codex plugin/plugin.json").exists())
        self.assertTrue((self.root / "codex plugin/.codex-plugin/plugin.json").exists())

    def test_cross_host_upgrade_is_rejected_before_any_switch(self):
        native = self.initial()
        before = snapshot(self.target)
        with self.assertRaisesRegex(ContractError, "different target host"):
            install(self.target, upgrade=True, host="codex")
        self.assertEqual(snapshot(self.target), before)
        codex_target = self.root / "codex plugin"
        install(codex_target, runtime_store=self.store)
        with self.assertRaisesRegex(ContractError, "different target host"):
            install(codex_target, upgrade=True, host="antigravity", workspace=self.workspace, home=self.home)
        self.assertTrue(Path(native["runtime_directory"]).is_dir())
        self.assertFalse(list(self.root.glob(".alp-backup-*")))

    def test_upgrade_retains_bindings_policy_extra_files_and_external_data(self):
        first = self.initial("enforce")
        self.home.mkdir()
        (self.home / "runtime.sqlite3").write_bytes(b"external database is never opened by installer")
        (self.home / "config.json").write_bytes(b"external user settings")
        old_data = snapshot(self.home)
        (self.target / "personal.txt").write_bytes(b"preserve user file")
        with patch.dict(os.environ, {"ALP_HOME": str(self.root / "other account's home")}):
            updated = install(self.target, upgrade=True, host="antigravity", expected_release_id=first["release_id"])
        self.assertEqual(updated["workspace"], str(self.workspace))
        self.assertEqual(updated["home"], str(self.home))
        self.assertEqual(updated["hook_mode"], "enforce")
        self.assertEqual(snapshot(self.home), old_data)
        self.assertEqual((self.target / "personal.txt").read_bytes(), b"preserve user file")
        self.assertNotIn("personal.txt", read_json(self.target / INSTALL_MANIFEST)["files"])
        self.assertEqual(updated["status"], "unchanged")
        self.assertNotIn("backup_directory", updated)
        inspect_installation(self.target, _hook_commands, target_host="antigravity")

    def test_explicit_rebinding_and_hook_policy_update_switches_both_native_entries(self):
        self.initial("enforce")
        second_project = self.root / "second project"
        second_project.mkdir()
        second_home = self.root / "second knowledge"
        result = install(self.target, upgrade=True, host="antigravity", workspace=second_project,
                         home=second_home, hook_mode="observe")
        self.assertEqual(result["workspace"], str(second_project))
        self.assertFalse(second_home.exists())
        mcp = read_json(self.target / "mcp_config.json")["mcpServers"]["agent_learning"]["args"]
        self.assertEqual(mcp[mcp.index("--workspace") + 1], str(second_project))
        self.assertEqual(mcp[mcp.index("--home") + 1], str(second_home))
        self.assertNotIn("PreToolUse", read_json(self.target / "hooks.json")["agent-learning"])
        inspect_installation(self.target, _hook_commands)

    def test_modified_native_config_or_host_manifest_is_preserved_and_rejected(self):
        self.initial()
        mcp_path = self.target / "mcp_config.json"
        original = mcp_path.read_bytes()
        value = read_json(mcp_path)
        value["mcpServers"]["agent_learning"]["args"][-1] = "codex"
        write_json(mcp_path, value)
        changed = snapshot(self.target)
        with self.assertRaisesRegex(ContractError, "MCP configuration"):
            install(self.target, upgrade=True, host="antigravity")
        self.assertEqual(snapshot(self.target), changed)
        mcp_path.write_bytes(original)
        manifest = read_json(self.target / INSTALL_MANIFEST)
        manifest["target_host"] = "codex"
        write_json(self.target / INSTALL_MANIFEST, manifest)
        with self.assertRaisesRegex(ContractError, "file manifest"):
            install(self.target, upgrade=True, host="antigravity")

    def test_schema1_codex_installation_without_host_identity_can_upgrade(self):
        codex = install(self.target, runtime_store=self.store)
        receipt_path = self.target / "runtime-release.json"
        receipt = read_json(receipt_path)
        receipt.pop("target_host")
        write_json(receipt_path, receipt)
        manifest = read_json(self.target / INSTALL_MANIFEST)
        manifest["schema"] = 1
        manifest.pop("target_host")
        manifest["files"]["runtime-release.json"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        write_json(self.target / INSTALL_MANIFEST, manifest)
        self.assertEqual(inspect_installation(self.target, _hook_commands)["target_host"], "codex")
        updated = install(self.target, upgrade=True, expected_release_id=codex["release_id"])
        self.assertEqual(updated["target_host"], "codex")
        self.assertEqual(read_json(self.target / INSTALL_MANIFEST)["schema"], 2)

    def test_project_level_export_and_read_only_default_home_selection(self):
        target = self.workspace / ".agents/plugins/agent-learning"
        default = self.root / "env knowledge"
        with patch.dict(os.environ, {"ALP_HOME": str(default)}):
            result = install(target, runtime_store=self.store, host="antigravity", workspace=self.workspace)
        self.assertEqual(result["home"], str(default))
        self.assertFalse(default.exists())
        self.assertTrue((target / "plugin.json").is_file())

    def test_default_cli_layout_exports_without_creating_knowledge_or_config(self):
        profile = self.root / "isolated user"
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("ALP_") and key != "LOCALAPPDATA"}
        if os.name == "nt":
            environment["LOCALAPPDATA"] = str(self.root / "local app data")
            home = self.root / "local app data/AgentLearning"
        else:
            home = profile / ".local/share/agent-learning"
        target = self.workspace / ".agents/plugins/agent-learning"
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), patch.object(Path, "home", return_value=profile), \
                redirect_stdout(output):
            status = main(["--workspace", str(self.workspace), "install", str(target),
                           "--host", "antigravity", "--hook-mode", "observe"])
        self.assertEqual(status, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["home"], str(home))
        self.assertEqual(Path(result["runtime_directory"]).parent, home / "runtime-releases")
        self.assertEqual(inspect_installation(target, _hook_commands)["target_host"], "antigravity")
        self.assertFalse((home / "runtime.sqlite3").exists())
        self.assertFalse((home / "config.json").exists())
        arguments = read_json(target / "mcp_config.json")["mcpServers"]["agent_learning"]["args"]
        self.assertEqual(arguments[arguments.index("--home") + 1], str(home))

    def test_reserved_runtime_directory_preserves_existing_data_during_export_and_upgrade(self):
        self.home.mkdir()
        database = self.home / "runtime.sqlite3"
        settings = self.home / "config.json"
        database.write_bytes(b"opaque existing database; must not be opened")
        settings.write_bytes(b"opaque user configuration; must not be parsed")
        first = install(self.target, runtime_store=self.home / "runtime-releases", host="antigravity",
                        workspace=self.workspace, home=self.home)
        updated = install(self.target, host="antigravity", upgrade=True)
        self.assertEqual(updated["home"], str(self.home))
        self.assertEqual(updated["runtime_directory"], first["runtime_directory"])
        self.assertEqual(database.read_bytes(), b"opaque existing database; must not be opened")
        self.assertEqual(settings.read_bytes(), b"opaque user configuration; must not be parsed")

    def test_pinned_default_release_can_reexport_and_migrate_store_without_rebinding_data(self):
        store = self.home / "runtime-releases"
        first = install(self.target, runtime_store=store, host="antigravity",
                        workspace=self.workspace, home=self.home)
        runtime = Path(first["runtime_directory"])
        environment = {key: value for key, value in os.environ.items() if not key.startswith("ALP_")}
        environment.update(PYTHONDONTWRITEBYTECODE="1", CODEX_HOME=str(self.root / "isolated codex"))
        for destination, next_store in ((self.root / "second export", store),
                                        (self.root / "migrated export", self.store)):
            command = [sys.executable, "-B", str(runtime / "scripts/learning.py"),
                       "--workspace", str(self.workspace), "--home", str(self.home),
                       "install", str(destination), "--host", "antigravity",
                       "--runtime-store", str(next_store)]
            response = subprocess.run(command, cwd=self.root, env=environment,
                                      capture_output=True, timeout=15)
            self.assertEqual(response.returncode, 0, response.stderr.decode("utf-8", "replace"))
            result = json.loads(response.stdout.decode("utf-8"))
            self.assertEqual(result["release_id"], first["release_id"])
            self.assertEqual(result["home"], str(self.home))
            self.assertEqual(Path(result["runtime_directory"]).parent, next_store)
        self.assertEqual(len(list(store.iterdir())), 1)
        for invalid_home in (runtime, runtime / "knowledge"):
            destination = self.root / "rejected export"
            response = subprocess.run([sys.executable, "-B", str(runtime / "scripts/learning.py"),
                "--workspace", str(self.workspace), "--home", str(invalid_home), "install", str(destination),
                "--host", "antigravity", "--runtime-store", str(store)], cwd=self.root,
                env=environment, capture_output=True, timeout=15)
            self.assertEqual(response.returncode, 1)
            self.assertFalse(destination.exists())
        self.assertFalse((self.home / "runtime.sqlite3").exists())
        self.assertFalse((self.home / "config.json").exists())
        validate_release_root(runtime)

    def test_default_layout_exception_does_not_allow_other_overlapping_paths(self):
        cases = [(self.home, self.home),
                 (self.home / "runtime-releases/knowledge", self.home / "runtime-releases"),
                 (self.home, self.home / "other runtime directory"),
                 (self.root, self.root / "runtime-releases"),
                 (self.target / "knowledge", self.store),
                 (Path(__file__).resolve().parents[1], self.store)]
        for home, store in cases:
            with self.subTest(home=home, store=store), self.assertRaises(ContractError):
                install(self.target, runtime_store=store, host="antigravity",
                        workspace=self.workspace, home=home)
            self.assertFalse(self.target.exists())
            self.assertFalse(store.exists())
        self.assertFalse(list(self.root.glob(".alp-backup-*")))

    def test_invalid_bindings_do_not_publish_a_plugin_or_create_knowledge_data(self):
        cases = [dict(host="other"), dict(host="antigravity"),
                 dict(host="antigravity", workspace=self.root / "missing", home=self.home),
                 dict(host="antigravity", workspace=self.workspace, home=self.target / "data"),
                 dict(host="antigravity", workspace=self.workspace, home=self.store / "data"),
                 dict(host="codex", workspace=self.workspace, home=self.home)]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ContractError):
                install(self.target, runtime_store=self.store, **kwargs)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.home.exists())
        self.assertFalse(self.store.exists())

    def test_file_home_or_parent_rejected_before_initial_export(self):
        blocker = self.root / "existing data file"
        blocker.write_bytes(b"preserve user file")
        for home in (blocker, blocker / "new knowledge", blocker / "nested/new knowledge"):
            # POSIX 路径核验可能先遇到 ENOTDIR；契约是拒绝并保留文件，
            # 具体诊断文本不属于跨平台安装 API。
            with self.subTest(home=home), self.assertRaises(ContractError):
                install(self.target, runtime_store=self.store, host="antigravity",
                        workspace=self.workspace, home=home)
            self.assertEqual(blocker.read_bytes(), b"preserve user file")
            self.assertFalse(self.target.exists())
            self.assertFalse(self.store.exists())
            self.assertFalse(self.home.exists())

    def test_file_home_or_parent_rebind_preserves_existing_installation(self):
        first = self.initial()
        before_target, before_store = snapshot(self.target), snapshot(self.store)
        blocker = self.root / "existing data file"
        blocker.write_bytes(b"preserve user file")
        for home in (blocker, blocker / "new knowledge", blocker / "nested/new knowledge"):
            with self.subTest(home=home), self.assertRaises(ContractError):
                install(self.target, upgrade=True, host="antigravity", home=home)
            self.assertEqual(snapshot(self.target), before_target)
            self.assertEqual(snapshot(self.store), before_store)
            self.assertEqual(blocker.read_bytes(), b"preserve user file")
            self.assertEqual(read_json(self.target / "runtime-release.json")["release_id"], first["release_id"])
            self.assertFalse(list(self.root.glob(".alp-backup-*")))
            self.assertFalse(self.home.exists())

    def test_rebind_to_project_inside_export_preserves_installation_and_project(self):
        self.initial()
        nested = self.target / "personal project"
        nested.mkdir()
        source = nested / "source.txt"
        source.write_bytes(b"preserve project file")
        before_target, before_store = snapshot(self.target), snapshot(self.store)
        with self.assertRaisesRegex(ContractError, "outside export and runtime"):
            install(self.target, upgrade=True, host="antigravity", workspace=nested)
        self.assertEqual(snapshot(self.target), before_target)
        self.assertEqual(snapshot(self.store), before_store)
        self.assertEqual(source.read_bytes(), b"preserve project file")
        self.assertFalse(list(self.root.glob(".alp-backup-*")))
        self.assertFalse(self.home.exists())

    def test_exported_native_hook_and_mcp_share_verified_utf8_session_and_fail_closed(self):
        """执行实际导出入口，覆盖Windows命令包装、中文stdio和默认审批边界。"""
        self.initial()
        observe = read_json(self.target / "hooks.json")["agent-learning"]
        self.assertNotIn("PreToolUse", observe)
        # 参数已固定，不能让测试继承真实数据目录、个人配置或停用开关。
        environment = {key: value for key, value in os.environ.items() if not key.startswith("ALP_")}
        environment.update(ALP_HOME=str(self.home), ALP_CONFIG=str(self.home / "config.json"),
                           PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="cp936")
        self.home.mkdir()
        write_json(self.home / "config.json", {})
        event = {"conversationId": "原生中文会话", "workspacePaths": [str(self.workspace)],
                 "modelName": "离线测试模型", "invocationNum": 0, "initialNumSteps": 0}
        invoked = subprocess.run(shlex.split(observe["PreInvocation"][0]["command"]),
            input=json.dumps(event, ensure_ascii=False).encode("utf-8"), cwd=self.root,
            env=environment, capture_output=True, timeout=15)
        self.assertEqual(invoked.returncode, 0, invoked.stderr.decode("utf-8", "replace"))
        injection = json.loads(invoked.stdout.decode("utf-8"))
        text = injection["injectSteps"][0]["ephemeralMessage"]
        handle_match = re.search(r"context_handle=([^;\s]+)", text)
        self.assertIsNotNone(handle_match, "the exported hook must deliver a usable session identity")
        handle = handle_match.group(1)
        mcp = read_json(self.target / "mcp_config.json")["mcpServers"]["agent_learning"]
        goal = "检查中文编码与隔离会话"
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "offline-native-export-check", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "session_context", "arguments": {"context_handle": handle}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "task_update", "arguments": {"context_handle": handle, "goal": goal}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "session_context", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
                "name": "session_context", "arguments": {"context_handle": handle}}},
        ]
        served = subprocess.run([mcp["command"], *mcp["args"]],
            input=("\n".join(json.dumps(item, ensure_ascii=False) for item in messages) + "\n").encode("utf-8"),
            cwd=self.root, env=environment, capture_output=True, timeout=15)
        self.assertEqual(served.returncode, 0, served.stderr.decode("utf-8", "replace"))
        responses = [json.loads(line) for line in served.stdout.decode("utf-8").splitlines()]
        self.assertEqual([item["id"] for item in responses], [1, 2, 3, 4, 5])
        bound = responses[1]["result"]["structuredContent"]
        self.assertFalse(responses[1]["result"]["isError"])
        self.assertTrue(bound["context_bound"])
        self.assertEqual(bound["context_handle"], handle)
        self.assertEqual(bound["conversation_id"], "antigravity:default:原生中文会话")
        self.assertFalse(responses[3]["result"]["structuredContent"]["context_bound"],
                         "a shared native MCP process must not inherit a previous request's handle")
        self.assertEqual(responses[4]["result"]["structuredContent"]["task"]["goal"], goal)
        self.assertTrue((self.home / "runtime.sqlite3").is_file())
        install(self.target, upgrade=True, host="antigravity", hook_mode="enforce")
        enforced = read_json(self.target / "hooks.json")["agent-learning"]
        broken = subprocess.run(shlex.split(enforced["PreToolUse"][0]["hooks"][0]["command"]),
            input=b'{"toolCall":', cwd=self.root, env=environment, capture_output=True, timeout=15)
        self.assertEqual(broken.returncode, 0, broken.stderr.decode("utf-8", "replace"))
        denial = json.loads(broken.stdout.decode("utf-8"))
        self.assertEqual(denial["decision"], "deny")
        self.assertIn("input_invalid", denial["reason"])
        self.assertNotIn("permissionOverrides", denial)


if __name__ == "__main__":
    unittest.main()
