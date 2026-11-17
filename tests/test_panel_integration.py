"""Actual fixed-runtime MCP panel chain, with isolated state and no models/UI launch."""
from contextlib import closing
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import sys
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

    def client(self, workspace, *, ui=True, register_context=True):
        project = self.runtime.project(workspace)
        handle = self.runtime.register_context(project, "native-chat")["context_handle"] if register_context else None
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

    def test_same_installed_plugin_rejects_another_project_before_runtime(self):
        other = Path(self.area.name) / "uninstalled project"
        other.mkdir()
        (other / ".git").mkdir()
        unused_home = Path(self.area.name) / "uninstalled-home"
        environment = dict(os.environ, ALP_HOME=str(unused_home), ALP_CONFIG=str(self.config),
                           PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        environment.pop("ALP_CONTEXT_HANDLE", None)
        result = subprocess.run([self.mcp["command"], *self.mcp["args"]], cwd=other, env=environment,
                                input="", capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr)["reason"], "project_installation_invalid")
        self.assertFalse(unused_home.exists())

    def test_fixed_runtime_global_discovery_has_no_database_or_worker_tools(self):
        unused_home = Path(self.area.name) / "discovery-home"
        broken_config = Path(self.area.name) / "broken-config.json"
        broken_config.write_text("invalid configuration", encoding="utf-8")
        environment = dict(os.environ, ALP_HOME=str(unused_home), ALP_CONFIG=str(broken_config),
                           PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        environment.pop("ALP_CONTEXT_HANDLE", None)
        frames = [
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "isolated-discovery", "version": "1"}}},
            {"method": "notifications/initialized"},
            {"id": 2, "method": "tools/list"},
            {"id": 3, "method": "tools/call", "params": {"name": "learning_panel_open", "arguments": {}}},
            {"id": 4, "method": "tools/call", "params": {"name": "session_context", "arguments": {}}},
            {"id": 5, "method": "tools/call", "params": {"name": "task_update", "arguments": {"goal": "cached task"}}},
            {"id": 6, "method": "resources/list"},
        ]
        data = "".join(json.dumps({"jsonrpc": "2.0", **frame}) + "\n" for frame in frames)
        args = self.mcp["args"]
        script = next(value for value in args if value.endswith("learning.py"))
        result = subprocess.run([self.mcp["command"], "-B", script, "serve", "--panel-discovery"],
                                cwd=self.workspace, env=environment, input=data,
                                capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = {reply["id"]: reply for line in result.stdout.splitlines() if (reply := json.loads(line))}
        self.assertEqual([tool["name"] for tool in replies[2]["result"]["tools"]], ["learning_panel_open"])
        self.assertEqual(replies[3]["result"]["content"], [])
        self.assertFalse(replies[3]["result"]["_meta"]["agentLearningPanel"]["visible"])
        self.assertEqual(replies[4]["error"]["code"], -32602)
        self.assertEqual(replies[5]["error"]["code"], -32602)
        self.assertEqual(len(replies[6]["result"]["resources"]), 1)
        self.assertFalse(unused_home.exists())

    def test_first_subdirectory_connection_uses_installed_root_without_git(self):
        workspace = Path(self.area.name) / "non-git project"
        subdirectory = workspace / "src" / "feature"
        subdirectory.mkdir(parents=True)
        target = Path(self.area.name) / "non-git export"
        install(target, runtime_store=self.root / "releases", panel_workspace=workspace)
        mcp = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["agent_learning"]
        fresh_home = Path(self.area.name) / "fresh-subdirectory-home"
        environment = dict(os.environ, ALP_HOME=str(fresh_home), ALP_CONFIG=str(self.config),
                           PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        environment.pop("ALP_CONTEXT_HANDLE", None)
        frames = [
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "isolated-subdirectory", "version": "1"}}},
            {"method": "notifications/initialized"},
            {"id": 2, "method": "tools/call", "params": {"name": "learning_status", "arguments": {"view": "summary"}}},
        ]
        data = "".join(json.dumps({"jsonrpc": "2.0", **frame}) + "\n" for frame in frames)
        result = subprocess.run([mcp["command"], *mcp["args"]], cwd=subdirectory, env=environment,
                                input=data, capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = {reply["id"]: reply for line in result.stdout.splitlines() if (reply := json.loads(line))}
        self.assertFalse(replies[2]["result"]["isError"], replies[2])
        with closing(sqlite3.connect((fresh_home / "runtime.sqlite3").as_uri() + "?mode=ro", uri=True)) as database:
            database.execute("PRAGMA query_only=ON")
            self.assertEqual(database.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)
            paths = database.execute("SELECT path,project FROM project_paths").fetchall()
            self.assertEqual({row[0] for row in paths},
                             {os.path.normcase(str(workspace.resolve())), os.path.normcase(str(subdirectory.resolve()))})
            self.assertEqual(len({row[1] for row in paths}), 1)

    def test_subdirectory_alias_keeps_hook_and_worker_project_identity(self):
        workspace = Path(self.area.name) / "alias project"
        subdirectory = workspace / "child"
        subdirectory.mkdir(parents=True)
        target = workspace / ".agents/plugins/agent-learning"
        installed = install(target, runtime_store=self.root / "releases", panel_workspace=workspace)
        self.config.write_text(json.dumps({"project_aliases": {str(subdirectory): "child-alias"}}), encoding="utf-8")
        environment = dict(os.environ, ALP_HOME=str(self.home), ALP_CONFIG=str(self.config),
                           PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        for name in ("ALP_DISABLED", "ALP_CONTEXT_HANDLE"):
            environment.pop(name, None)
        hook = subprocess.run([sys.executable, "-B", str(Path(installed["runtime_directory"]) / "hooks/dispatch.py")],
            cwd=subdirectory, env=environment, input=json.dumps({"hook_event_name": "SessionStart",
                "source": "startup", "session_id": "aliased-chat", "cwd": str(subdirectory)}),
            capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(hook.returncode, 0, hook.stderr)
        context = json.loads(hook.stdout)["hookSpecificOutput"]["additionalContext"]
        handle = re.search(r"context_handle=([a-f0-9]{32})", context).group(1)
        mcp = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["agent_learning"]
        frames = [
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "isolated-alias", "version": "1"}}},
            {"method": "notifications/initialized"},
            {"id": 2, "method": "tools/call", "params": {"name": "session_context",
                "arguments": {"context_handle": handle}}},
        ]
        data = "".join(json.dumps({"jsonrpc": "2.0", **frame}) + "\n" for frame in frames)
        result = subprocess.run([mcp["command"], *mcp["args"]], cwd=subdirectory, env=environment,
                                input=data, capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = {reply["id"]: reply for line in result.stdout.splitlines() if (reply := json.loads(line))}
        self.assertFalse(replies[2]["result"]["isError"], replies[2])
        binding = replies[2]["result"]["structuredContent"]
        self.assertTrue(binding["context_bound"])
        self.assertEqual(binding["project_id"], "child-alias")

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

    def test_project_menu_bootstraps_without_creating_a_work_context_or_model_call(self):
        request, handle = self.client(self.workspace, ui=False, register_context=False)
        self.assertIsNone(handle)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM contexts").fetchone()[0], 0)
        tools = {tool["name"]: tool for tool in request("tools/list")["tools"]}
        self.assertEqual(tools["learning_panel_open"]["_meta"]["ui"]["visibility"], ["app"])
        resource_uri = tools["learning_panel_open"]["_meta"]["ui"]["resourceUri"]
        resource = request("resources/read", {"uri": resource_uri})["contents"][0]
        self.assertIn("const launchMode='direct'", resource["text"])
        missing = request("tools/call", {"name": "learning_panel_open", "arguments": {}})
        self.assertTrue(missing["isError"])
        self.assertEqual(missing["content"], [])
        self.assertNotIn("structuredContent", missing)
        meta = {"thread_id": "isolated-project-ui", "threadId": "isolated-project-ui"}
        opened = request("tools/call", {"name": "learning_panel_open", "arguments": {}, "_meta": meta})
        self.assertFalse(opened["isError"])
        self.assertEqual(opened["content"], [])
        self.assertNotIn("structuredContent", opened)
        private = opened["_meta"]["agentLearningPanel"]
        self.assertTrue(private["visible"])
        self.assertEqual(private["scope"], "project")
        self.assertNotIn("context_handle", private)
        args = {"capability": private["capability"], "action": "debug"}
        checked = request("tools/call", {"name": "learning_panel_direct_action", "arguments": args, "_meta": meta})
        self.assertFalse(checked["isError"])
        self.assertEqual(checked["content"], [])
        self.assertNotIn("structuredContent", checked)
        self.assertEqual(checked["_meta"]["agentLearningPanel"]["diagnostics"]["connection"]["hook"], "unknown")
        denied = request("tools/call", {"name": "learning_panel_direct_action", "arguments": args,
                                       "_meta": {"threadId": "different-ui-session"}})
        self.assertTrue(denied["isError"])
        self.assertEqual(denied["content"], [])
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM contexts").fetchone()[0], 0)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
