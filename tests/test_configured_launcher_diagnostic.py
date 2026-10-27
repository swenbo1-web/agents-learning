"""A missing pinned launcher is diagnosable without fallback or model work."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning import admin_providers as providers
from agent_learning.admin_control import control_summary
from agent_learning.admin_queue import dispatch_diagnostic
from agent_learning.runtime import Runtime


class ConfiguredLauncherDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.missing = self.root / "build" / "local" / "opt1432" / "missing-pinned-launcher" / "codex.exe"
        self.assertFalse(self.missing.exists())
        for target in ("agent_learning.admin_providers.subprocess.Popen",
                       "agent_learning.admin_providers.subprocess.run"):
            mocked = patch(target, side_effect=AssertionError("status must not start a process"))
            mocked.start()
            self.addCleanup(mocked.stop)
        cache = patch.dict(providers._CAPABILITIES, {}, clear=True)
        cache.start()
        self.addCleanup(cache.stop)

    def test_default_codex_still_uses_the_verified_host_launcher(self):
        with patch("agent_learning.host_cli.codex_host_launcher",
                   return_value=(sys.executable, "host_ancestor")) as host, \
                patch("agent_learning.admin_providers.shutil.which") as lookup:
            result = providers.resolve_command(["codex"], _windows=True)
        host.assert_called_once_with(windows=True)
        lookup.assert_not_called()
        self.assertEqual(result["args"], [str(Path(sys.executable).resolve())])
        self.assertEqual(result["executable_source"], "host_ancestor")

    def test_existing_absolute_launcher_keeps_its_arguments(self):
        with patch("agent_learning.host_cli.codex_host_launcher") as host, \
                patch("agent_learning.admin_providers.shutil.which") as lookup:
            result = providers.resolve_command([sys.executable, "-B"])
        host.assert_not_called()
        lookup.assert_not_called()
        self.assertEqual(result["args"], [str(Path(sys.executable).resolve()), "-B"])

    def test_missing_absolute_launcher_never_discovers_or_substitutes(self):
        with patch("agent_learning.host_cli.codex_host_launcher") as host, \
                patch("agent_learning.admin_providers.shutil.which") as lookup:
            result = providers.executor_availability({"provider": "codex", "command": [str(self.missing)]})
        host.assert_not_called()
        lookup.assert_not_called()
        self.assertFalse(result["cli_available"])
        self.assertEqual(result["reason"], "configured_launcher_missing")
        self.assertFalse(result["capability_checked"])
        self.assertIsNone(result["capability_ready"])
        self.assertEqual(result["readiness_scope"], "unavailable")

    def test_default_missing_path_command_keeps_generic_unavailability(self):
        with patch("agent_learning.host_cli.codex_host_launcher", return_value=(None, None)), \
                patch("agent_learning.admin_providers.shutil.which", return_value=None):
            result = providers.executor_availability({"provider": "codex", "command": ["codex"]})
        self.assertEqual(result["reason"], "cli_unavailable")

    def test_permission_denied_is_never_claimed_to_be_missing(self):
        with patch.object(Path, "stat", side_effect=PermissionError("PRIVATE_PATH_OR_CREDENTIAL")):
            result = providers.executor_availability({"provider": "codex", "command": [str(self.missing)]})
        self.assertEqual(result["reason"], "cli_permission_denied")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_other_filesystem_errors_do_not_claim_absence(self):
        with patch.object(Path, "stat", side_effect=OSError("PRIVATE_IO_FAILURE")):
            result = providers.executor_availability({"provider": "codex", "command": [str(self.missing)]})
        self.assertEqual(result["reason"], "cli_unavailable")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_directory_is_unavailable_without_fallback_not_missing(self):
        with patch("agent_learning.admin_providers.shutil.which") as lookup:
            result = providers.executor_availability({"provider": "codex", "command": [str(self.root)]})
        lookup.assert_not_called()
        self.assertEqual(result["reason"], "cli_unavailable")

    def test_existing_script_still_requires_an_explicit_interpreter(self):
        with self.assertRaises(ValueError) as caught:
            providers.resolve_command([str(Path(__file__).resolve())], _windows=True)
        self.assertEqual(providers.launch_error(caught.exception)["reason"], "cli_shim_unsupported")

    def test_launch_diagnostic_never_copies_path_or_exception_text(self):
        with self.assertRaises(FileNotFoundError) as caught:
            providers.resolve_command([str(self.missing)])
        diagnostic = providers.launch_error(caught.exception)
        self.assertEqual(diagnostic["reason"], "configured_launcher_missing")
        self.assertNotIn(str(self.missing), json.dumps(diagnostic))
        self.assertLessEqual(set(diagnostic), {"reason", "error_type", "errno", "winerror"})

    def test_all_visual_surfaces_translate_the_fixed_reason_and_explain_explicit_repair(self):
        paths = ("agent_learning/browser_ui.py", "agent_learning/panel_ui.py",
                 "host_resources/dsh/client/index.js", "host_resources/antigravity/sidecars/control/index.html")
        for relative in paths:
            with self.subTest(surface=relative):
                text = (self.root / relative).read_text(encoding="utf-8")
                self.assertIn("configured_launcher_missing", text)
                self.assertIn("cli_permission_denied", text)
                self.assertIn("admin-configure", text)
                self.assertIn("No launcher was substituted.", text)
                self.assertIn("插件不会自动替换", text)

    def runtime(self, *, pending):
        temporary = tempfile.TemporaryDirectory(prefix="launcher-diagnostic-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        workspace = root / "workspace"
        workspace.mkdir()
        runtime = Runtime(root / "home", config={"admin": {"enabled": True}})
        self.addCleanup(runtime.close)
        project = runtime.project(workspace)
        runtime.admin_configure(project, True, provider="codex", model_mode="fixed",
                                model="offline-fixed-model", command=[str(self.missing)])
        runtime.task_update(project, "offline-chat", "Review the observed project operation")
        if pending:
            runtime.feedback(project, "offline-chat", "An observed project operation needs review", "operation")
        return runtime, project

    def assert_readonly_diagnostic(self, runtime, project, expected_dispatch):
        policy = runtime.admin_policy(project)
        changes = runtime.db.total_changes
        routing = runtime.admin_routing_status(project)
        dispatch = dispatch_diagnostic(runtime, project)
        summary = control_summary(runtime, project)
        self.assertEqual(routing["reason"], "configured_launcher_missing")
        self.assertEqual(routing["availability"]["reason"], "configured_launcher_missing")
        self.assertFalse(routing["ready"])
        self.assertEqual(dispatch["reason"], expected_dispatch)
        self.assertEqual(summary["routing"]["reason"], "configured_launcher_missing")
        self.assertEqual(summary["diagnostic"]["reason"], expected_dispatch)
        self.assertEqual(summary["runner"]["reason"], expected_dispatch)
        self.assertFalse(summary["runner"]["running"])
        self.assertEqual(runtime.admin_policy(project), policy)
        self.assertEqual(runtime.db.total_changes, changes)
        for table in ("admin_calls", "admin_dispatch", "admin_execution_plans", "admin_call_budgets"):
            self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)

    def test_pending_legacy_case_preserves_missing_launcher_reason_through_status_and_dispatch(self):
        runtime, project = self.runtime(pending=True)
        self.assert_readonly_diagnostic(runtime, project, "configured_launcher_missing")

    def test_idle_and_disabled_dispatch_reasons_keep_precedence_over_launcher_diagnosis(self):
        runtime, project = self.runtime(pending=False)
        self.assert_readonly_diagnostic(runtime, project, "no_increment")
        runtime.feedback(project, "offline-chat", "An observed project operation needs review", "operation")
        policy = runtime.admin_policy(project)
        runtime.admin_configure(project, False, provider=policy["provider"], model_mode=policy["model_mode"],
                                model=policy["model"], expected_policy_version=policy["policy_version"])
        self.assert_readonly_diagnostic(runtime, project, "disabled")


if __name__ == "__main__":
    unittest.main()
