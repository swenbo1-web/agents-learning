"""Actual HTTP checks for panel-opened browser capability revocation."""
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_learning.install import install
from agent_learning.knowledge_browser import make_browser_server
from agent_learning.panel_browser import make_panel_guard
from agent_learning.runtime import Runtime, encode


class PanelBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.area = tempfile.TemporaryDirectory(prefix="alp-panel-browser-install-")
        cls.base = Path(cls.area.name).resolve()
        cls.workspace = cls.base / "project"
        cls.workspace.mkdir()
        (cls.workspace / ".git").mkdir()
        cls.target = cls.base / "export"
        install(cls.target, runtime_store=cls.base / "releases", panel_workspace=cls.workspace)
        receipt = json.loads((cls.target / "runtime-release.json").read_text(encoding="utf-8"))
        cls.binding = {"installation": str(cls.target), "workspace": str(cls.workspace),
                       "release_id": receipt["release_id"],
                       "receipt_digest": hashlib.sha256(encode(receipt).encode("utf-8")).hexdigest()}

    @classmethod
    def tearDownClass(cls):
        from agent_learning.release import filesystem_path
        if cls.base.exists() and cls.base.name.startswith("alp-panel-browser-install-"):
            shutil.rmtree(filesystem_path(cls.base), ignore_errors=True)
        cls.area.cleanup()

    def setUp(self):
        self.area = tempfile.TemporaryDirectory(prefix="alp-panel-browser-state-")
        self.addCleanup(self.area.cleanup)
        self.home = Path(self.area.name) / "home"
        self.runtime = Runtime(self.home, config={})
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.project(self.workspace)
        self.database = self.home / "runtime.sqlite3"
        self.guard = make_panel_guard(self.target, self.binding, self.database,
            self.runtime._session_scope(), self.runtime.session_epoch(), self.runtime.host_epoch())
        self.server = make_browser_server(self.database, self.project, review=True,
                                         admin_control=True, access_guard=self.guard)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, path="/api/session", *, body=None):
        headers = {"Authorization": "Bearer " + self.server.access_token,
                   "X-Review-Token": self.server.review_token,
                   "X-Admin-Token": self.server.admin_token,
                   "Origin": self.server.origin,
                   "Content-Type": "application/json"}
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.request("POST" if body is not None else "GET", path,
                               json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def temporary_identity_change(self, name, operation):
        path = self.target / name
        original = path.read_bytes()
        self.addCleanup(path.write_bytes, original)
        operation(path)
        return path, original

    def assert_expired(self):
        self.assertEqual(self.request()[0], 403)
        self.assertEqual(self.request("/")[0], 403)
        self.assertEqual(self.request("/api/review/prepare", body={"candidate_ids": ["untrusted"], "action": "approve"})[0], 403)
        self.assertEqual(self.request("/api/admin-control/configure", body={})[0], 403)

    def test_http_guard_reads_its_own_connection_without_writing_or_launching_models(self):
        before = self.runtime.db.total_changes
        with patch("agent_learning.runtime.Runtime.__init__", side_effect=AssertionError("guard must not create Runtime")), \
                patch("agent_learning.admin.subprocess.Popen") as model:
            self.assertEqual(self.request()[0], 200)
            self.assertEqual(self.request("/api/admin-control")[0], 200)
        self.assertEqual(self.runtime.db.total_changes, before)
        model.assert_not_called()

    def test_same_host_epoch_change_revokes_existing_browser_before_read_or_write(self):
        self.assertEqual(self.request()[0], 200)
        self.runtime.db.execute("UPDATE host_scopes SET epoch=epoch+1 WHERE scope=?", (self.runtime._session_scope(),))
        with patch("agent_learning.runtime.Runtime.__init__", side_effect=AssertionError("revoked requests must not create Runtime")):
            self.assert_expired()

    def test_governance_epoch_change_alone_also_revokes_browser(self):
        self.assertEqual(self.request()[0], 200)
        self.runtime.db.execute("UPDATE host_state SET epoch=epoch+1 WHERE id=1")
        self.assert_expired()

    def test_real_account_switch_revokes_an_existing_approval_preview(self):
        self.runtime.task_update(self.project, "chat", "Review historical fact")
        problem = self.runtime.feedback(self.project, "chat", "Observed project rule", category="project")
        refs = [row[0] for row in self.runtime.db.execute("SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        candidate = self.runtime.submit_candidate(self.project, problem_id=problem["id"], problem_revision=problem["revision"],
            title="Reported rule", content="Only the reported project fact", category="project",
            applicability="This project", limitations="Reported evidence only", evidence_refs=refs,
            validation_level="reported")["candidate_id"]
        code, raw = self.request("/api/review/prepare", body={"candidate_ids": [candidate], "action": "approve"})
        self.assertEqual(code, 200, raw)
        preview = json.loads(raw)
        self.runtime.account_changed()
        before = self.runtime.db.total_changes
        with patch("agent_learning.admin.subprocess.Popen") as model:
            code, _ = self.request("/api/review/commit", body={"plan_id": preview["plan_id"], "request_id": "old-preview"})
        self.assertEqual(code, 403)
        self.assertEqual(self.runtime.db.total_changes, before)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM active WHERE project=?", (self.project,)).fetchone()[0], 0)
        model.assert_not_called()

    def test_receipt_replacement_revokes_browser_permanently(self):
        self.assertEqual(self.request()[0], 200)
        path, original = self.temporary_identity_change("runtime-release.json", lambda item: item.write_bytes(item.read_bytes() + b"\n"))
        self.assert_expired()
        path.write_bytes(original)
        self.assertEqual(self.request()[0], 403, "restoring an installation must not revive an old capability")

    def test_plugin_manifest_change_revokes_existing_browser(self):
        self.assertEqual(self.request()[0], 200)
        self.temporary_identity_change(".codex-plugin/plugin.json", lambda item: item.write_bytes(item.read_bytes() + b"\n"))
        self.assert_expired()

    def test_removing_installation_manifest_revokes_existing_browser(self):
        self.assertEqual(self.request()[0], 200)
        self.temporary_identity_change("installation-manifest.json", lambda item: item.unlink())
        self.assert_expired()

    def test_removing_the_export_directory_revokes_existing_browser(self):
        self.assertEqual(self.request()[0], 200)
        moved = self.target.with_name("temporarily removed export")
        self.target.rename(moved)
        self.addCleanup(moved.rename, self.target)
        self.assert_expired()

    def test_database_file_replacement_is_rejected_even_with_identical_epochs(self):
        self.assertEqual(self.request()[0], 200)
        self.runtime.close()
        previous = self.home / "original.sqlite3"
        self.database.rename(previous)
        shutil.copy2(previous, self.database)
        self.assert_expired()

    def test_a_missing_database_is_not_recreated_by_lifetime_check(self):
        self.runtime.close()
        self.database.unlink()
        self.assert_expired()
        self.assertFalse(self.database.exists())

    def test_guard_exception_is_a_denial_without_browser_data_or_model_calls(self):
        self.server.access_guard = lambda: (_ for _ in ()).throw(RuntimeError("private detail"))
        before = self.runtime.db.total_changes
        self.assert_expired()
        self.assertEqual(self.runtime.db.total_changes, before)
        self.assertNotIn(b"private detail", self.request()[1])

    def test_existing_browser_without_guard_keeps_its_independent_capability(self):
        independent = make_browser_server(self.database, self.project)
        thread = threading.Thread(target=independent.serve_forever, daemon=True)
        thread.start()
        self.runtime.account_changed()
        connection = http.client.HTTPConnection("127.0.0.1", independent.server_address[1], timeout=5)
        try:
            connection.request("GET", "/api/session", headers={"Authorization": "Bearer " + independent.access_token})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
        finally:
            connection.close()
            independent.shutdown()
            independent.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()
