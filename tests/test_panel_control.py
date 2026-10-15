"""Project visibility and human control contracts; isolated SQLite, no model calls."""
import json
from pathlib import Path
import tempfile
import sys
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from agent_learning.install import install
from agent_learning.panel_control import PanelControl, same_workspace
from agent_learning.runtime import Runtime, ContractError


class PanelControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.area = tempfile.TemporaryDirectory(prefix="alp-panel-control-")
        cls.base = Path(cls.area.name)
        cls.workspace = cls.base / "installed project"
        cls.workspace.mkdir()
        (cls.workspace / ".git").mkdir()
        (cls.workspace / "README.md").write_text("# Existing project notes\nUse the local project knowledge.\n", encoding="utf-8")
        cls.target = cls.base / "export"
        install(cls.target, runtime_store=cls.base / "releases", panel_workspace=cls.workspace)

    @classmethod
    def tearDownClass(cls):
        cls.area.cleanup()

    def setUp(self):
        self.data = tempfile.TemporaryDirectory(prefix="alp-panel-state-")
        self.addCleanup(self.data.cleanup)
        self.runtime = Runtime(Path(self.data.name) / "home", config={})
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.project(self.workspace)
        self.handle = self.runtime.register_context(self.project, "current-chat")["context_handle"]
        self.panel = PanelControl(self.runtime, self.project, self.workspace, self.target)
        self.addCleanup(self.panel.close)

    def bootstrap(self):
        result = self.panel.bootstrap(context_handle=self.handle)
        self.assertTrue(result["visible"], result)
        return result

    def action(self, opened, action, **kwargs):
        return self.panel.action(opened["capability"], action, context_handle=self.handle, **kwargs)

    def test_known_project_without_installation_or_current_handle_has_no_buttons(self):
        missing = PanelControl(self.runtime, self.project, self.workspace, None)
        self.assertFalse(missing.eligible())
        self.assertFalse(missing.bootstrap(context_handle=self.handle)["visible"])
        self.assertFalse(self.panel.bootstrap()["visible"])
        self.assertEqual(self.panel.leases, {})

    def test_bootstrap_status_does_not_enable_or_call_model_or_return_knowledge(self):
        before = self.runtime.db.total_changes
        with patch.object(self.runtime, "_launch_admin") as launch:
            result = self.bootstrap()
            status = self.action(result, "status")
        launch.assert_not_called()
        self.assertEqual(self.runtime.db.total_changes, before)
        self.assertFalse(status["summary"]["admin_admission_enabled"])
        self.assertEqual(status["summary"]["workspace"], str(self.workspace.resolve()))
        self.assertNotIn("capability", status)
        self.assertNotIn("history_import", status["summary"])

    def test_other_registered_project_and_nested_checkout_are_not_installed(self):
        other = Path(self.data.name) / "another project"
        other.mkdir()
        project = self.runtime.project(other)
        wrong = PanelControl(self.runtime, project, other, self.target)
        self.assertFalse(wrong.eligible())
        self.assertFalse(self.panel.bootstrap(context_handle=self.handle, workspace=str(other))["visible"])
        nested = self.workspace / "nested repository"
        nested.mkdir(exist_ok=True)
        (nested / ".git").mkdir(exist_ok=True)
        self.assertFalse(same_workspace(nested, self.workspace))
        self.assertTrue(same_workspace(self.workspace / ".git", self.workspace))

    def test_candidate_and_deferred_items_are_counted_as_pending_review(self):
        self.runtime.task_update(self.project, "current-chat", "knowledge task")
        problem = self.runtime.feedback(self.project, "current-chat", "A reusable observation")
        packet = self.runtime.review_packet(self.project, problem["id"])
        self.runtime.submit_candidate(self.project, problem_id=problem["id"],
            problem_revision=packet["problem"]["revision"], title="Project guidance",
            content="Inspect the current project before changing it.", category="operation",
            applicability="This project.", limitations="Reported observation.",
            evidence_refs=[packet["events"][0]["id"]], validation_level="reported")
        self.assertEqual(self.bootstrap()["summary"]["counts"]["candidates"], 1)
        self.runtime.db.execute("UPDATE candidates SET status='deferred' WHERE project=?", (self.project,))
        self.assertEqual(self.bootstrap()["summary"]["counts"]["candidates"], 1)

    def test_parent_prefix_and_missing_paths_cannot_claim_project(self):
        sibling = self.base / "installed project backup"
        sibling.mkdir(exist_ok=True)
        self.assertFalse(same_workspace(sibling, self.workspace))
        self.assertFalse(same_workspace(self.base / "missing", self.workspace))
        self.assertFalse(same_workspace("relative/path", self.workspace))

    def test_subdirectory_alias_is_a_different_project_even_under_installation(self):
        subdirectory = self.workspace / "aliased child"
        subdirectory.mkdir(exist_ok=True)
        self.runtime.config["project_aliases"] = {str(subdirectory): "other-logical-project"}
        project = self.runtime.project(subdirectory)
        self.assertNotEqual(project, self.project)
        wrong = PanelControl(self.runtime, project, subdirectory, self.target)
        self.assertFalse(wrong.eligible())

    def test_capability_is_bound_to_context_and_expires_on_account_switch(self):
        opened = self.bootstrap()
        second = self.runtime.register_context(self.project, "other-chat")["context_handle"]
        with self.assertRaises(ContractError):
            self.panel.action(opened["capability"], "status", context_handle=second)
        self.runtime.account_changed()
        with self.assertRaises(ContractError):
            self.action(opened, "status")
        self.assertFalse(self.panel.bootstrap(context_handle=self.handle)["visible"])

    def test_uninstall_and_expiration_disable_existing_buttons(self):
        opened = self.bootstrap()
        self.panel.leases[opened["capability"]]["expires"] = 0
        with self.assertRaises(ContractError):
            self.action(opened, "status")
        opened = self.bootstrap()
        with patch("agent_learning.install_update.inspect_installation", side_effect=ContractError("removed")):
            with self.assertRaises(ContractError):
                self.action(opened, "open_review", request_id="open")

    def test_browser_open_retries_are_idempotent_and_do_not_return_tokens(self):
        opened = self.bootstrap()
        with patch.object(self.panel, "_open_browser") as launch:
            first = self.action(opened, "open_review", request_id="single-click")
            repeated = self.action(opened, "open_review", request_id="single-click")
        launch.assert_called_once_with(True)
        self.assertEqual(first, repeated)
        self.assertEqual(first["opened"], "review")
        self.assertNotIn("url", first)
        with self.assertRaises(ContractError):
            self.action(opened, "open_browser", request_id="single-click")

    def test_revoked_browser_is_replaced_after_a_fresh_panel_open(self):
        opened = self.bootstrap()
        lifetime = {"valid": True}
        with patch("webbrowser.open", return_value=True), patch(
                "agent_learning.panel_browser.make_panel_guard",
                side_effect=[lambda: lifetime["valid"], lambda: True]):
            self.action(opened, "open_browser", request_id="first-browser")
            old_server, old_thread = self.panel.browsers["browser"]
            with urllib.request.urlopen(old_server.browser_url, timeout=5) as response:
                self.assertEqual(response.status, 200)
            lifetime["valid"] = False
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(old_server.browser_url, timeout=5)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
            reopened = self.bootstrap()
            self.action(reopened, "open_browser", request_id="replacement-browser")
            server, _ = self.panel.browsers["browser"]
            self.assertIsNot(server, old_server)
            self.assertFalse(old_thread.is_alive())
            with urllib.request.urlopen(server.browser_url, timeout=5) as response:
                self.assertEqual(response.status, 200)

    def test_changed_installation_requires_a_new_controller(self):
        opened = self.bootstrap()
        replacement = dict(self.panel.binding, release_id="replacement-release")
        with patch.object(self.panel, "_installation", return_value=replacement):
            self.assertFalse(self.panel.bootstrap(context_handle=self.handle)["visible"])
            with self.assertRaises(ContractError):
                self.action(opened, "status")
            fresh = PanelControl(self.runtime, self.project, self.workspace, self.target)
            self.addCleanup(fresh.close)
            with patch.object(fresh, "_installation", return_value=replacement):
                self.assertTrue(fresh.bootstrap(context_handle=self.handle)["visible"])

    def test_presets_require_confirmation_and_displayed_versions(self):
        opened = self.bootstrap()
        expected = opened["summary"]["expected"]
        before = self.runtime.db.total_changes
        for extra in ({"confirm": False, "expected": expected}, {"confirm": True, "expected": {}}):
            with self.assertRaises(ContractError):
                self.action(opened, "set_admin_preset", preset="light", request_id="not-confirmed", **extra)
        self.assertEqual(self.runtime.db.total_changes, before)

    def test_preset_uses_shared_budget_and_exact_replay_launches_once(self):
        self.runtime.config["admin"]["command"] = [sys.executable]
        self.runtime.admin_observe_model(self.project, "current-chat", "observed-model")
        self.runtime.task_update(self.project, "current-chat", "project work")
        self.runtime.feedback(self.project, "current-chat", "A reusable project observation")
        opened = self.bootstrap()
        args = {"preset": "light", "request_id": "enable-once", "confirm": True,
                "expected": opened["summary"]["expected"]}
        with patch.object(self.runtime, "_launch_admin") as launch:
            first = self.action(opened, "set_admin_preset", **args)
            repeated = self.action(opened, "set_admin_preset", **args)
        launch.assert_called_once()
        self.assertEqual(first, repeated)
        self.assertTrue(first["summary"]["admin_admission_enabled"])
        self.assertEqual(first["summary"]["max_calls_per_day"], 15)
        self.assertEqual(first["summary"]["budget_scope"], "ALP_HOME")
        with self.assertRaises(ContractError):
            self.action(opened, "set_admin_preset", **dict(args, preset="active"))

    def test_request_budget_is_per_panel_and_expired_panels_are_cleaned(self):
        opened = self.bootstrap()
        with patch("agent_learning.panel_control.MAX_REQUESTS", 1), patch.object(self.panel, "_open_browser"):
            self.action(opened, "open_browser", request_id="first")
            with self.assertRaises(ContractError):
                self.action(opened, "open_browser", request_id="second")
            other = self.bootstrap()
            self.action(other, "open_browser", request_id="new-panel")
            self.panel.leases[opened["capability"]]["expires"] = 0
            self.bootstrap()
        self.assertFalse(any(key[0] == opened["capability"] for key in self.panel.requests))

    def test_stale_display_does_not_overwrite_other_panels_settings(self):
        first, second = self.bootstrap(), self.bootstrap()
        with patch.object(self.runtime, "_launch_admin"):
            self.action(first, "set_admin_preset", preset="light", confirm=True,
                        request_id="first-change", expected=first["summary"]["expected"])
            with self.assertRaises(ContractError):
                self.action(second, "set_admin_preset", preset="active", confirm=True,
                            request_id="stale-change", expected=second["summary"]["expected"])
        self.assertEqual(self.action(first, "status")["summary"]["max_calls_per_day"], 15)

    def test_history_button_archives_only_and_same_click_does_not_repeat(self):
        opened = self.bootstrap()
        with patch.object(self.runtime, "_launch_admin") as launch, patch("agent_learning.admin.subprocess.Popen") as model:
            result = self.action(opened, "archive_history", confirm=True, request_id="archive-once")
            repeated = self.action(opened, "archive_history", confirm=True, request_id="archive-once")
        launch.assert_not_called()
        model.assert_not_called()
        self.assertEqual(result, repeated)
        self.assertFalse(result["history"]["review_requested"])
        self.assertGreater(result["history"]["archived_cases"], 0)
        self.assertEqual(result["history"]["published_knowledge"], 0)

    def test_native_sidecar_requires_current_workspace_and_conversation(self):
        panel = PanelControl(self.runtime, self.project, self.workspace, self.target, require_context=False)
        self.addCleanup(panel.close)
        self.assertFalse(panel.bootstrap(workspace=str(self.workspace))["visible"])
        opened = panel.bootstrap(workspace=self.workspace.as_uri(), conversation_id="native-current")
        self.assertTrue(opened["visible"])
        with self.assertRaises(ContractError):
            panel.action(opened["capability"], "status", workspace=str(self.workspace), conversation_id="native-other")
        self.assertTrue(panel.action(opened["capability"], "status", workspace=str(self.workspace),
                                     conversation_id="native-current")["visible"])
