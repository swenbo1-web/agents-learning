"""Independent read projections do not scan paths or change runtime policy."""
from pathlib import Path
import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_learning.admin_control import control_detail, control_status, readonly_runtime
from agent_learning.knowledge_browser import make_browser_server
from agent_learning.runtime import Runtime


class AdminFileDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "build" / "local" / "r62ui" / "tests"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="detail-", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict("os.environ", {
            "ALP_HOME": str(self.root / "home"), "ALP_CONFIG": str(self.root / "config.json"),
            "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"),
            "CODEX_HOME": str(self.root / "codex"), "PYTHONDONTWRITEBYTECODE": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.runtime = Runtime(self.root / "home", config={})
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.project(self.workspace)

    def test_basic_settings_do_not_read_link_or_capture_details(self):
        before = self.runtime.db.total_changes
        with patch("agent_learning.workspace_links.workspace_links_status") as links, \
                patch("agent_learning.work_file_diagnostics.summary") as capture:
            result = control_status(self.runtime, self.project, details=False)
        links.assert_not_called()
        capture.assert_not_called()
        self.assertTrue(result["details_separate"])
        self.assertNotIn("queue", result)
        self.assertNotIn("recovery", result)
        self.assertEqual(self.runtime.db.total_changes, before)
        self.assertEqual(result["phase_review"]["policy"]["mode"], "legacy")
        self.assertFalse(result["phase_review"]["policy"]["configured"])

    def test_queue_link_status_has_no_inferred_checkout_and_keeps_version_binding(self):
        status = {"roots": [], "linked_workspaces": [str(self.root / "chosen")],
                  "workspace_verified": False, "execution_permission": False,
                  "reload_required": None, "config_version": "fixture"}
        before = self.runtime.db.total_changes
        with patch("agent_learning.workspace_links.workspace_links_status", return_value=status) as links, \
                patch("agent_learning.work_file_diagnostics.summary") as capture:
            result = control_detail(self.runtime, self.project, "queue")
        links.assert_called_once_with(self.runtime, self.project, None)
        capture.assert_not_called()
        self.assertEqual(result["data"]["workspace_links"], status)
        self.assertEqual(result["project_id"], self.project)
        self.assertEqual(result["host_scope"], self.runtime._session_scope())
        self.assertEqual(result["section"], "queue")
        self.assertEqual(len(result["versions"]), 5)
        self.assertEqual(self.runtime.db.total_changes, before)

    def test_capture_summary_loads_only_in_recovery_with_fixed_recent_bound(self):
        status = {"events": 2, "target_count": 3, "accepted_count": 1,
                  "filtered_count": 2, "reasons": {"outside_workspace": 2}, "truncated": False}
        before = self.runtime.db.total_changes
        with patch("agent_learning.work_file_diagnostics.summary", return_value=status) as capture, \
                patch("agent_learning.workspace_links.workspace_links_status") as links:
            result = control_detail(self.runtime, self.project, "recovery")
        capture.assert_called_once_with(self.runtime, self.project, host="codex", limit=40)
        links.assert_not_called()
        self.assertEqual(result["data"]["file_capture_diagnostics"], status)
        self.assertEqual(result["section"], "recovery")
        self.assertEqual(self.runtime.db.total_changes, before)

    def test_checkout_unverified_projection_and_empty_capture_are_still_read_only(self):
        before = self.runtime.db.total_changes
        queue = control_detail(self.runtime, self.project, "queue")["data"]["workspace_links"]
        recovery = control_detail(self.runtime, self.project, "recovery")["data"]["file_capture_diagnostics"]
        self.assertEqual(queue["roots"], [])
        self.assertFalse(queue["workspace_verified"])
        self.assertFalse(queue["execution_permission"])
        self.assertEqual(recovery["accepted_count"], 0)
        self.assertEqual(recovery["filtered_count"], 0)
        self.assertEqual(self.runtime.db.total_changes, before)

    def test_readonly_view_lazily_loads_links_without_claiming_host_reload(self):
        config = self.root / "browser-config.json"
        chosen = self.root / "chosen-not-observed"
        config.write_text(json.dumps({"linked_workspaces": {self.project: [str(chosen)]}}), encoding="utf-8")
        with patch.dict("os.environ", {"ALP_CONFIG": str(config)}):
            view = readonly_runtime(self.runtime.home / "runtime.sqlite3", self.runtime.db, "codex", "default")
            self.assertFalse(hasattr(view, "_workspace_links_loaded"))
            with patch("agent_learning.workspace_links._checked_root", side_effect=AssertionError("observed chosen root")):
                status = control_detail(view, self.project, "queue")["data"]["workspace_links"]
        self.assertEqual(status["linked_workspaces"], [str(chosen)])
        self.assertEqual(status["roots"], [])
        self.assertIsNone(status["reload_required"])
        self.assertFalse(status["workspace_verified"])
        self.assertFalse(chosen.exists())

    def test_invalid_link_shape_is_confined_to_queue_detail(self):
        config = self.root / "browser-config.json"
        config.write_text(json.dumps({"linked_workspaces": []}), encoding="utf-8")
        with patch.dict("os.environ", {"ALP_CONFIG": str(config)}):
            view = readonly_runtime(self.runtime.home / "runtime.sqlite3", self.runtime.db, "codex", "default")
            result = control_status(view, self.project, details=False)
            self.assertTrue(result["details_separate"])
            with self.assertRaises(ValueError):
                control_detail(view, self.project, "queue")
            control_detail(view, self.project, "recovery")

    def test_http_reads_new_metadata_without_constructing_or_migrating_runtime(self):
        server = make_browser_server(self.runtime.home / "runtime.sqlite3", self.project, admin_control=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        before = self.runtime.db.total_changes
        try:
            with patch("agent_learning.runtime.Runtime.__init__", side_effect=AssertionError("GET constructed Runtime")), \
                    patch("subprocess.Popen", side_effect=AssertionError("GET launched process")):
                for section, key in (("queue", "workspace_links"), ("recovery", "file_capture_diagnostics")):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                    try:
                        connection.request("GET", "/api/admin-control/details?section=" + section,
                                           headers={"Authorization": "Bearer " + server.access_token})
                        response = connection.getresponse()
                        result = json.loads(response.read())
                        self.assertEqual(response.status, 200, result)
                        self.assertEqual(result["project_id"], self.project)
                        self.assertIn(key, result["data"])
                        if section == "queue":
                            self.assertIsNone(result["data"][key]["reload_required"])
                    finally:
                        connection.close()
            self.assertEqual(self.runtime.db.total_changes, before)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
