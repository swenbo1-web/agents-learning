"""Actual fixed-runtime MCP panel chain, with isolated state and no models/UI launch."""
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import unittest

from agent_learning.install import install
from agent_learning.runtime import Runtime


class PanelIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.area = tempfile.TemporaryDirectory(prefix="alp-panel-mcp-entry-")
        cls.root = Path(cls.area.name)
        cls.workspace = cls.root / "installed project"
        cls.workspace.mkdir()
        (cls.workspace / ".git").mkdir()
        cls.target = cls.root / "export"
        install(cls.target, runtime_store=cls.root / "releases", panel_workspace=cls.workspace)
        cls.mcp = json.loads((cls.target / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["agent_learning"]

    @classmethod
    def tearDownClass(cls):
        from agent_learning.release import filesystem_path
        import shutil
        shutil.rmtree(filesystem_path(cls.root), ignore_errors=True)
        cls.area.cleanup()

    def setUp(self):
        self.area = tempfile.TemporaryDirectory(prefix="alp-panel-mcp-data-")
        self.addCleanup(self.area.cleanup)
        self.home = Path(self.area.name) / "home"
        self.config = Path(self.area.name) / "config.json"
        self.config.write_text("{}", encoding="utf-8")
        self.runtime = Runtime(self.home, config={})
        self.addCleanup(self.runtime.close)

    def client(self, workspace, *, ui=True):
        project = self.runtime.project(workspace)
        handle = self.runtime.register_context(project, "native-chat")["context_handle"]
        env = {**os.environ, "ALP_HOME": str(self.home), "ALP_CONFIG": str(self.config),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
        env.pop("ALP_DISABLED", None)
        env.pop("ALP_CONTEXT_HANDLE", None)
        process = subprocess.Popen([self.mcp["command"], *self.mcp["args"]], cwd=workspace,
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", text=True)
        replies = queue.Queue()
        def read():
            for line in process.stdout:
                try:
                    replies.put(json.loads(line))
                except ValueError:
                    replies.put({"invalid": True})
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        def close():
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=2)
            process.stdout.close()
            process.stderr.close()
        self.addCleanup(close)
        serial = [0]
        def request(method, params=None):
            serial[0] += 1
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": serial[0], "method": method,
                                            "params": params or {}}) + "\n")
            process.stdin.flush()
            result = replies.get(timeout=20)
            self.assertEqual(result.get("id"), serial[0], result)
            self.assertNotIn("error", result, result)
            return result["result"]
        capabilities = {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}} if ui else {}
        request("initialize", {"protocolVersion": "2025-11-25", "clientInfo": {"name": "isolated-ui", "version": "1"},
            "capabilities": capabilities})
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        process.stdin.flush()
        return request, handle

    def test_fixed_runtime_resource_bootstrap_and_confirmed_action_without_model(self):
        request, handle = self.client(self.workspace)
        names = {tool["name"] for tool in request("tools/list")["tools"]}
        self.assertIn("learning_panel", names)
        self.assertIn("learning_panel_action", names)
        resource = request("resources/read", {"uri": "ui://agent-learning/panel-v1.html"})["contents"][0]
        self.assertEqual(resource["mimeType"], "text/html;profile=mcp-app")
        opened = request("tools/call", {"name": "learning_panel", "arguments": {"context_handle": handle}})
        private = opened["_meta"]["agentLearningPanel"]
        self.assertTrue(private["visible"])
        public = json.loads(opened["content"][0]["text"])
        self.assertNotIn("capability", public)
        self.assertNotIn(private["capability"], json.dumps(opened["structuredContent"]))
        args = {"context_handle": handle, "capability": private["capability"], "action": "set_admin_preset",
                "preset": "off", "confirm": True, "expected": private["summary"]["expected"], "request_id": "isolated-human-click"}
        changed = request("tools/call", {"name": "learning_panel_action", "arguments": args})
        replay = request("tools/call", {"name": "learning_panel_action", "arguments": args})
        self.assertFalse(changed["isError"])
        self.assertEqual(changed, replay)
        self.assertFalse(changed["structuredContent"]["summary"]["admin_admission_enabled"])
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_same_installed_plugin_exposes_no_panel_in_another_project(self):
        other = Path(self.area.name) / "uninstalled project"
        other.mkdir()
        (other / ".git").mkdir()
        request, _ = self.client(other)
        names = {tool["name"] for tool in request("tools/list")["tools"]}
        self.assertNotIn("learning_panel", names)
        self.assertNotIn("learning_panel_action", names)
        self.assertIn("knowledge_search", names)

    def test_fixed_runtime_without_ui_returns_only_verified_browser_launch_information(self):
        request, handle = self.client(self.workspace, ui=False)
        names = {tool["name"] for tool in request("tools/list")["tools"]}
        self.assertNotIn("learning_panel_action", names)
        opened = request("tools/call", {"name": "learning_panel", "arguments": {"context_handle": handle}})
        self.assertNotIn("_meta", opened)
        body = opened["structuredContent"]
        self.assertEqual(body["reason"], "panel_ui_unavailable")
        self.assertEqual(body["fallback_skill"], "learning-browser")
        fallback = body["fallback"]
        self.assertEqual(Path(fallback["installation"]), self.target.resolve())
        self.assertEqual(Path(fallback["workspace"]), self.workspace.resolve())
        self.assertEqual(Path(fallback["home"]), self.home.resolve())
        self.assertEqual((fallback["host"], fallback["host_instance"], fallback["mode"]),
                         ("codex", "default", "read_only"))
        self.assertNotIn(handle, json.dumps(opened))
        self.assertNotIn("capability", json.dumps(opened))
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
