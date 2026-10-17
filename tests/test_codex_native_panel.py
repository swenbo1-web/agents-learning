"""Optional real Codex app-server routing check; no model turns or real data."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from agent_learning.codex_connect import connect
from agent_learning.install import install
from agent_learning.runtime import Runtime


CODEX = os.environ.get("ALP_TEST_CODEX") or shutil.which("codex")


@unittest.skipUnless(CODEX, "Codex CLI is unavailable for the native UI routing check")
class CodexNativePanelTests(unittest.TestCase):
    def test_global_discovery_routes_to_each_installed_project_without_model_turns(self):
        with tempfile.TemporaryDirectory(prefix="cn-") as temporary:
            area = Path(temporary).resolve()
            home, codex_home = area / "knowledge", area / "codex"
            home.mkdir()
            config = home / "config.json"
            config.write_text("{}", encoding="utf-8")
            projects = {name: area / name for name in ("a", "b", "uninstalled")}
            for name, workspace in projects.items():
                workspace.mkdir()
                (workspace / ".git").mkdir()
                if name != "uninstalled":
                    target = area / (name + "-export")
                    install(target, runtime_store=area / "releases", panel_workspace=workspace)
                    connect(target, workspace, home=home, config=config, codex_home=codex_home)
            # Explicitly trust only these isolated fixture workspaces. No Hooks
            # or plugins are registered in this independent Codex home.
            with (codex_home / "config.toml").open("a", encoding="utf-8") as stream:
                for workspace in projects.values():
                    stream.write('\n[projects.' + json.dumps(str(workspace)) + ']\ntrust_level = "trusted"\n')
            environment = dict(os.environ, CODEX_HOME=str(codex_home), ALP_HOME=str(home), ALP_CONFIG=str(config),
                               ALP_RUNTIME_STORE=str(area / "releases"), PYTHONDONTWRITEBYTECODE="1", ALP_DISABLED="1")
            environment.pop("ALP_CONTEXT_HANDLE", None)
            replies, notifications = queue.Queue(), []
            with (area / "native.stderr.log").open("w", encoding="utf-8") as errors:
                process = subprocess.Popen([CODEX, "app-server"], cwd=area, env=environment,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                    text=True, encoding="utf-8", bufsize=1)
                def read():
                    for line in process.stdout:
                        try:
                            replies.put(json.loads(line))
                        except ValueError:
                            pass
                reader = threading.Thread(target=read, daemon=True)
                reader.start()
                serial = 0
                def request(method, params):
                    nonlocal serial
                    serial += 1
                    ident = serial
                    process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}) + "\n")
                    process.stdin.flush()
                    deadline = time.monotonic() + 40
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self.fail("Codex request timed out: " + method)
                        response = replies.get(timeout=remaining)
                        if response.get("id") == ident:
                            self.assertNotIn("error", response, (method, response.get("error")))
                            return response["result"]
                        notifications.append(response.get("method"))
                def tool(thread, name, arguments):
                    return request("mcpServer/tool/call", {"threadId": thread, "server": "agent_learning", "tool": name,
                        "arguments": arguments, "_meta": {"thread_id": thread, "threadId": thread}})
                try:
                    request("initialize", {"clientInfo": {"name": "alp_native_panel_test", "version": "1.0"},
                                           "capabilities": {"experimentalApi": True}})
                    process.stdin.write('{"jsonrpc":"2.0","method":"initialized"}\n')
                    process.stdin.flush()
                    catalog = request("mcpServerStatus/list", {"cursor": None, "limit": 100, "detail": "toolsAndAuthOnly"})
                    server = next(value for value in catalog["data"] if value["name"] == "agent_learning")
                    self.assertIsNone(server.get("toolsError"))
                    descriptor = next(t for t in server["tools"].values() if t["name"] == "learning_panel_open")
                    self.assertEqual(descriptor["_meta"]["openai/ui"]["entrypoints"], [{"type": "thread"}])
                    states = {}
                    for name, workspace in projects.items():
                        thread = request("thread/start", {"cwd": str(workspace), "approvalPolicy": "never",
                                                          "sandbox": "read-only", "ephemeral": True})["thread"]["id"]
                        opened = tool(thread, "learning_panel_open", {})
                        self.assertEqual(opened["content"], [])
                        self.assertNotIn("structuredContent", opened)
                        private = opened["_meta"]["agentLearningPanel"]
                        if name == "uninstalled":
                            self.assertFalse(private["visible"])
                            self.assertNotIn("summary", private)
                            continue
                        self.assertTrue(private["visible"])
                        self.assertEqual(Path(private["summary"]["workspace"]), workspace)
                        checked = tool(thread, "learning_panel_direct_action", {"action": "debug", "capability": private["capability"]})
                        self.assertTrue(checked["_meta"]["agentLearningPanel"]["visible"])
                        states[name] = (thread, private)
                    denied = tool(states["b"][0], "learning_panel_direct_action", {
                        "action": "debug", "capability": states["a"][1]["capability"]})
                    self.assertTrue(denied["isError"])
                    self.assertFalse(denied["_meta"]["agentLearningPanel"]["visible"])
                    self.assertNotIn("turn/started", notifications)
                finally:
                    process.stdin.close()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=8)
                    reader.join(timeout=2)
                    process.stdout.close()
            runtime = Runtime(home)
            try:
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM contexts").fetchone()[0], 0)
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
