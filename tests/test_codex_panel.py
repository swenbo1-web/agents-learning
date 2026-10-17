"""Direct project controls: isolated installations/HTTP only, no model or host."""
from contextlib import redirect_stdout
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from agent_learning.codex_panel import make_panel_server, open_panel
from agent_learning.install import install
from agent_learning.panel_control import PanelControl
from agent_learning.runtime import ContractError, Runtime


class CodexPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.area = tempfile.TemporaryDirectory(prefix="cp-")
        cls.root = Path(cls.area.name).resolve()
        cls.workspace = cls.root / "project"
        cls.workspace.mkdir()
        (cls.workspace / ".git").mkdir()
        cls.target = cls.root / "export"
        install(cls.target, runtime_store=cls.root / "releases", panel_workspace=cls.workspace)

    @classmethod
    def tearDownClass(cls):
        from agent_learning.release import filesystem_path
        import shutil
        shutil.rmtree(filesystem_path(cls.root), ignore_errors=True)
        cls.area.cleanup()

    def setUp(self):
        self.data = tempfile.TemporaryDirectory(prefix="cd-")
        self.addCleanup(self.data.cleanup)
        self.home = Path(self.data.name) / "home"
        self.runtime = Runtime(self.home, config={})
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.project(self.workspace)

    def panel(self):
        panel = PanelControl(self.runtime, self.project, self.workspace, self.target, project_ui=True)
        self.addCleanup(panel.close)
        return panel

    def server(self):
        with patch("pathlib.Path.cwd", return_value=self.workspace):
            server = make_panel_server(self.target, workspace=self.workspace, home=self.home)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
        self.addCleanup(close)
        return server

    def request(self, server, method="GET", path="/", body=None, headers=None):
        headers = dict(headers or {})
        if body is not None:
            if not isinstance(body, (str, bytes)):
                body = json.dumps(body)
            headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def authenticate(self, server):
        status, headers, _ = self.request(server, path=urlsplit(server.browser_url).path)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        return {"Cookie": headers["Set-Cookie"].split(";", 1)[0], "Origin": server.origin}

    def bootstrap_http(self, server, headers, session="window-1"):
        status, _, body = self.request(server, "POST", "/api/bootstrap", {"ui_session_id": session}, headers)
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertTrue(result["visible"], result)
        self.assertEqual(result["scope"], "project")
        return result

    def test_project_scope_opens_and_debugs_without_creating_a_conversation(self):
        panel = self.panel()
        before = self.runtime.db.total_changes
        with patch.object(self.runtime, "access_context", side_effect=AssertionError("No context lookup")), \
                patch.object(self.runtime, "session_context", side_effect=AssertionError("No context lookup")), \
                patch.object(self.runtime, "register_context", side_effect=AssertionError("No artificial context")), \
                patch.object(self.runtime, "_launch_admin") as launch:
            opened = panel.bootstrap(workspace=self.workspace, ui_session_id="window-1")
            result = panel.action(opened["capability"], "debug", workspace=self.workspace, ui_session_id="window-1")
        self.assertTrue(opened["visible"])
        self.assertEqual(result["scope"], "project")
        self.assertEqual(result["diagnostics"]["connection"], {"ui_bridge": "responding", "mcp": "unknown", "hook": "unknown", "context_bound": None})
        self.assertEqual(self.runtime.db.total_changes, before)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM context_access").fetchone()[0], 0)
        launch.assert_not_called()

    def test_project_scope_rejects_missing_or_fake_conversation_authorization(self):
        panel = self.panel()
        for values in ({}, {"ui_session_id": ""}, {"ui_session_id": "x" * 501}, {"ui_session_id": "\nwindow"},
                       {"ui_session_id": "window", "context_handle": "pretend-hook"},
                       {"ui_session_id": "window", "conversation_id": "pretend-conversation"}):
            with self.subTest(values=list(values)):
                self.assertFalse(panel.bootstrap(workspace=self.workspace, **values)["visible"])
        self.assertFalse(panel.bootstrap(ui_session_id="window")["visible"])
        other = Path(self.data.name)
        self.assertFalse(panel.bootstrap(workspace=other, ui_session_id="window")["visible"])

    def test_ui_capability_is_bound_to_the_window_scope_and_epoch(self):
        panel = self.panel()
        opened = panel.bootstrap(workspace=self.workspace, ui_session_id="window-1")
        for values in ({"ui_session_id": "window-2"}, {"ui_session_id": "window-1", "context_handle": "anything"},
                       {"ui_session_id": "window-1", "conversation_id": "anything"}, {}):
            with self.subTest(values=list(values)), self.assertRaises(ContractError):
                panel.action(opened["capability"], "status", workspace=self.workspace, **values)
        legacy = PanelControl(self.runtime, self.project, self.workspace, self.target)
        self.addCleanup(legacy.close)
        with self.assertRaises(ContractError):
            legacy.action(opened["capability"], "status", workspace=self.workspace, ui_session_id="window-1")
        self.runtime.account_changed()
        with self.assertRaises(ContractError):
            panel.action(opened["capability"], "status", workspace=self.workspace, ui_session_id="window-1")

    def test_legacy_and_other_hosts_cannot_adopt_project_ui_scope(self):
        legacy = PanelControl(self.runtime, self.project, self.workspace, self.target)
        self.addCleanup(legacy.close)
        self.assertFalse(legacy.bootstrap(workspace=self.workspace, ui_session_id="window")["visible"])
        native = PanelControl(self.runtime, self.project, self.workspace, self.target, require_context=False)
        self.addCleanup(native.close)
        self.assertFalse(native.bootstrap(workspace=self.workspace, conversation_id="native", ui_session_id="window")["visible"])
        other = Runtime(Path(self.data.name) / "ag-home", config={}, host="antigravity")
        self.addCleanup(other.close)
        with self.assertRaises(ContractError):
            PanelControl(other, self.project, self.workspace, self.target, project_ui=True)

    def test_launch_validates_cwd_and_installation_before_opening_runtime(self):
        absent_home = Path(self.data.name) / "must-not-create"
        child = self.workspace / "subdirectory"
        child.mkdir(exist_ok=True)
        with patch("agent_learning.codex_panel.Runtime", side_effect=AssertionError("Wrong project must not create a DB")):
            for current in (Path(self.data.name), child):
                with self.subTest(current=current), patch("pathlib.Path.cwd", return_value=current), self.assertRaises(ContractError):
                    make_panel_server(self.target, workspace=self.workspace, home=absent_home)
            with patch("pathlib.Path.cwd", return_value=self.workspace), self.assertRaises(ContractError):
                make_panel_server(self.target, workspace=child, home=absent_home)
            with self.assertRaises(ContractError):
                make_panel_server(self.target, port=True, home=absent_home)
        self.assertFalse(absent_home.exists())

    def test_launch_does_not_create_a_database_in_managed_payload(self):
        receipt = json.loads((self.target / "runtime-release.json").read_text(encoding="utf-8"))
        source = Path(__file__).resolve().parents[1]
        protected = [self.target, self.target / "personal", Path(receipt["runtime_directory"]), source / "bad-home"]
        with patch("agent_learning.codex_panel.Runtime", side_effect=AssertionError("Managed files must be preserved")), \
                patch("pathlib.Path.cwd", return_value=self.workspace):
            for home in protected:
                with self.subTest(home=home), self.assertRaises(ContractError):
                    make_panel_server(self.target, workspace=self.workspace, home=home)
                self.assertFalse((home / "runtime.sqlite3").exists())

    def test_old_fixed_action_cannot_follow_a_new_installation_runtime(self):
        old = Path(self.data.name) / "old-runtime"
        old.mkdir()
        (old / "release.json").write_text("{}", encoding="utf-8")
        with patch("agent_learning.codex_panel.__file__", str(old / "agent_learning/codex_panel.py")), \
                patch("agent_learning.codex_panel.Runtime", side_effect=AssertionError("Refresh before opening any DB")), \
                patch("pathlib.Path.cwd", return_value=self.workspace), self.assertRaisesRegex(ContractError, "refresh"):
            make_panel_server(self.target, workspace=self.workspace, home=self.home)

    def test_http_cookie_is_required_and_access_code_is_one_time(self):
        server = self.server()
        self.assertEqual(server.server_address[0], "127.0.0.1")
        self.assertEqual(self.request(server)[0], 401)
        entry = urlsplit(server.browser_url).path
        headers = self.authenticate(server)
        status, page_headers, html = self.request(server, headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("script-src 'sha256-", page_headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", page_headers["Content-Security-Policy"])
        self.assertNotIn(server.cookie_value.encode(), html)
        self.assertNotIn(entry.encode(), html)
        self.assertEqual(self.request(server, path=entry)[0], 403)
        self.assertEqual(self.request(server, path="/runtime.sqlite3", headers=headers)[0], 404)
        self.assertEqual(self.request(server, path="/", headers={**headers, "Host": "evil.example"})[0], 403)

    def test_http_bootstrap_and_debug_need_no_model_or_hook(self):
        server = self.server()
        headers = self.authenticate(server)
        before = server.runtime.db.total_changes
        with patch.object(server.runtime, "_launch_admin") as launch:
            opened = self.bootstrap_http(server, headers)
            args = {"ui_session_id": "window-1", "capability": opened["capability"], "action": "debug"}
            status, _, raw = self.request(server, "POST", "/api/action", args, headers)
        self.assertEqual(status, 200, raw)
        result = json.loads(raw)
        self.assertEqual(result["diagnostics"]["project"]["home"], str(self.home.resolve()))
        self.assertIsNone(result["diagnostics"]["connection"]["context_bound"])
        self.assertNotIn("context_handle", raw.decode())
        self.assertEqual(server.runtime.db.total_changes, before)
        launch.assert_not_called()

    def test_http_rejects_csrf_unbounded_and_arbitrary_identity_fields(self):
        server = self.server()
        headers = self.authenticate(server)
        body = {"ui_session_id": "window-1"}
        for bad in ({}, {"Cookie": headers["Cookie"]}, {**headers, "Origin": "https://evil.example"},
                    {**headers, "Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=list(bad)):
                self.assertEqual(self.request(server, "POST", "/api/bootstrap", body, bad)[0], 403)
        for value in ({**body, "workspace": str(self.workspace)}, {**body, "home": str(self.home)},
                      {**body, "context_handle": "fake"}, {**body, "conversation_id": "fake"},
                      {"ui_session_id": ""}, "{bad-json", '{"ui_session_id":"one","ui_session_id":"two"}', "x" * 32769):
            with self.subTest(value=type(value).__name__):
                self.assertEqual(self.request(server, "POST", "/api/bootstrap", value, headers)[0], 400)
        self.assertEqual(self.request(server, "POST", "/api/bootstrap", body, {**headers, "Transfer-Encoding": "chunked"})[0], 400)

    def test_http_mutations_keep_confirmation_versions_and_idempotency(self):
        server = self.server()
        headers = self.authenticate(server)
        opened = self.bootstrap_http(server, headers)
        args = {"ui_session_id": "window-1", "capability": opened["capability"], "action": "set_admin_preset",
                "preset": "off", "request_id": "pause-once", "expected": opened["summary"]["expected"]}
        with patch.object(server.runtime, "_launch_admin") as launch:
            self.assertEqual(self.request(server, "POST", "/api/action", args, headers)[0], 400)
            args["confirm"] = True
            status, _, first = self.request(server, "POST", "/api/action", args, headers)
            self.assertEqual(status, 200, first)
            changes = server.runtime.db.total_changes
            status, _, second = self.request(server, "POST", "/api/action", args, headers)
            self.assertEqual(status, 200, second)
            self.assertEqual(first, second)
            self.assertEqual(server.runtime.db.total_changes, changes)
        launch.assert_not_called()

    def test_http_guard_revokes_window_on_account_change(self):
        server = self.server()
        headers = self.authenticate(server)
        opened = self.bootstrap_http(server, headers)
        with server.lock:
            server.runtime.account_changed()
        args = {"ui_session_id": "window-1", "capability": opened["capability"], "action": "status"}
        self.assertEqual(self.request(server, "POST", "/api/action", args, headers)[0], 403)
        self.assertEqual(self.request(server, headers=headers)[0], 403)

    def test_open_panel_does_not_print_access_code_and_closes_on_terminal_interrupt(self):
        server = Mock()
        server.browser_url = "http://127.0.0.1:1234/access/private-one-time-code"
        server.controller.project = "project-1"
        server.serve_forever.side_effect = KeyboardInterrupt()
        output = io.StringIO()
        with patch("agent_learning.codex_panel.make_panel_server", return_value=server), \
                patch("agent_learning.codex_panel.webbrowser.open", return_value=True) as browser, redirect_stdout(output):
            result = open_panel(self.target, workspace=self.workspace, home=self.home)
        self.assertEqual(result, {"status": "stopped", "project_id": "project-1"})
        self.assertNotIn("private-one-time-code", output.getvalue())
        browser.assert_called_once_with(server.browser_url)
        server.server_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
