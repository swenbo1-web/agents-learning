"""Offline discovery and read-only capability reporting for the independent AG CLI."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_learning import admin_providers as providers
from agent_learning.runtime import ContractError
from tests.test_admin_routing import RoutingRuntime


class AntigravityExecutorDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-ag-executor-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = self.root / "local-app-data"
        self.official = self.local / "agy" / "bin" / "agy.exe"
        self.path_bin = self.root / "path-bin"
        self.path_bin.mkdir()
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.local), "PATH": str(self.path_bin)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.cache = patch.dict(providers._CAPABILITIES, {}, clear=True)
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not executed: trusted offline fixture", encoding="utf-8")
        return path

    def resolve(self, command=None):
        return providers.resolve_command(command or ["agy"], _windows=True)

    def windows_resolution(self):
        original = providers.resolve_command
        return patch.object(providers, "resolve_command", side_effect=lambda command: original(command, _windows=True))

    def test_official_install_fallback_does_not_start_process(self):
        self.executable(self.official)
        with patch("subprocess.Popen") as popen, patch("subprocess.run") as run:
            result = self.resolve()
        popen.assert_not_called()
        run.assert_not_called()
        self.assertEqual(result["args"], [str(self.official.resolve())])
        self.assertEqual(result["executable_source"], "official_install")

    def test_path_has_priority_over_official_fallback(self):
        self.executable(self.official)
        selected = self.executable(self.path_bin / "agy.exe")
        result = self.resolve()
        self.assertEqual(result["args"], [str(selected.resolve())])
        self.assertEqual(result["executable_source"], "path")

    def test_standard_exe_name_can_find_official_install(self):
        self.executable(self.official)
        self.assertEqual(self.resolve(["agy.exe"])["args"], [str(self.official.resolve())])

    def test_explicit_command_is_not_replaced(self):
        self.executable(self.official)
        explicit = self.executable(self.root / "custom" / "agy.exe")
        self.assertEqual(self.resolve([str(explicit)])["args"], [str(explicit.resolve())])
        with self.assertRaises(FileNotFoundError):
            self.resolve([str(self.root / "missing" / "agy.exe")])

    def test_extra_arguments_and_other_names_do_not_trigger_fallback(self):
        self.executable(self.official)
        with patch.object(providers.shutil, "which", return_value=None):
            for command in (["agy", "trusted-wrapper-mode"], ["other-reviewer"], ["agentapi"], ["antigravity"]):
                with self.subTest(command=command), self.assertRaises(FileNotFoundError):
                    self.resolve(command)

    def test_missing_cli_is_not_replaced_with_app_or_agentapi(self):
        self.executable(self.path_bin / "Antigravity.exe")
        self.executable(self.local / "agy" / "bin" / "language_server.exe")
        with self.assertRaises(FileNotFoundError):
            self.resolve()

    def test_relative_localappdata_and_directory_are_not_cli(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": "relative-install"}):
            with self.assertRaises(FileNotFoundError):
                self.resolve()
        self.official.mkdir(parents=True)
        with self.assertRaises(FileNotFoundError):
            self.resolve()

    def test_non_windows_does_not_use_windows_official_fallback(self):
        self.executable(self.official)
        with patch.object(providers.shutil, "which", return_value=None):
            with self.assertRaises(FileNotFoundError):
                providers.resolve_command(["agy"], _windows=False)

    def test_current_directory_and_relative_path_entries_are_skipped(self):
        self.executable(self.cwd / "agy.exe")
        selected = self.executable(self.path_bin / "agy.exe")
        entries = os.pathsep.join(("", ".", str(self.cwd), "relative-bin", str(self.path_bin)))
        with patch("agent_learning.admin_providers.Path.cwd", return_value=self.cwd), patch.dict(os.environ, {"PATH": entries}):
            result = self.resolve()
        self.assertEqual(result["args"], [str(selected.resolve())])

    def test_current_directory_shadow_does_not_hide_official_install(self):
        self.executable(self.cwd / "agy.exe")
        self.executable(self.official)
        with patch("agent_learning.admin_providers.Path.cwd", return_value=self.cwd), patch.dict(os.environ, {"PATH": str(self.cwd)}):
            self.assertEqual(self.resolve()["executable_source"], "official_install")

    def test_path_link_to_current_directory_shadow_is_skipped(self):
        shadow = self.executable(self.cwd / "agy.exe")
        link = self.path_bin / "agy.exe"
        try:
            link.symlink_to(shadow)
        except OSError:
            self.skipTest("symbolic links unavailable")
        self.executable(self.official)
        with patch("agent_learning.admin_providers.Path.cwd", return_value=self.cwd):
            self.assertEqual(self.resolve()["executable_source"], "official_install")

    def test_readonly_availability_distinguishes_launcher_and_unchecked_protocol(self):
        self.executable(self.official)
        with self.windows_resolution(), patch("subprocess.Popen") as popen, patch("subprocess.run") as run:
            result = providers.executor_availability({"provider": "antigravity", "command": ["agy"]})
        popen.assert_not_called()
        run.assert_not_called()
        self.assertTrue(result["cli_available"])
        self.assertEqual(result["executable_source"], "official_install")
        self.assertEqual(result["readiness_scope"], "launcher_only")
        self.assertFalse(result["capability_checked"])
        self.assertIsNone(result["capability_ready"])
        self.assertEqual(result["authentication"], "provider_cli_managed_not_checked")

    def test_missing_availability_keeps_capability_and_authentication_unknown(self):
        with self.windows_resolution(), patch("subprocess.run") as run:
            result = providers.executor_availability({"provider": "antigravity", "command": ["agy"]})
        run.assert_not_called()
        self.assertFalse(result["cli_available"])
        self.assertEqual(result["reason"], "cli_unavailable")
        self.assertEqual(result["readiness_scope"], "unavailable")
        self.assertIsNone(result["capability_ready"])
        self.assertEqual(result["authentication"], "provider_cli_managed_not_checked")

    def help_probe(self, *, supported):
        def probe(args, **kwargs):
            self.assertEqual(args[-1], "--help")
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            text = ("--input-format --output-format --json-schema --agent --print-timeout" if supported else "Usage: desktop launcher")
            kwargs["stdout"].write(text.encode("utf-8"))
            return Mock(returncode=0)
        return probe

    def test_status_reuses_help_cache_without_starting_probe(self):
        self.executable(self.official)
        plan = {"provider": "antigravity", "command": ["agy"]}
        with self.windows_resolution(), patch("subprocess.run", side_effect=self.help_probe(supported=True)) as run:
            self.assertTrue(providers.executor_capability(plan)["ready"])
            result = providers.executor_availability(plan)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(result["capability_checked"])
        self.assertTrue(result["capability_ready"])
        self.assertEqual(result["readiness_scope"], "protocol_checked")
        self.assertEqual(result["authentication"], "provider_cli_managed_not_checked")

    def test_unsupported_help_is_explicit_and_cache_is_invalidated_by_binary_change(self):
        self.executable(self.official)
        plan = {"provider": "antigravity", "command": ["agy"]}
        with self.windows_resolution(), patch("subprocess.run", side_effect=self.help_probe(supported=False)) as run:
            capability = providers.executor_capability(plan)
            result = providers.executor_availability(plan)
            self.official.write_text("changed binary identity: do not execute", encoding="utf-8")
            after = providers.executor_availability(plan)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(capability["ready"])
        self.assertEqual(capability["reason"], "cli_protocol_unsupported")
        self.assertFalse(result["capability_ready"])
        self.assertEqual(result["capability_reason"], "cli_protocol_unsupported")
        self.assertFalse(after["capability_checked"])

    def test_routing_freezes_ag_launcher_and_rejects_path_change(self):
        self.executable(self.official)
        runtime = RoutingRuntime(self.root / "routing.sqlite3", host="antigravity", config={"enabled": True})
        self.addCleanup(runtime.db.close)
        with self.windows_resolution():
            before = runtime.resolve_admin_executor("project-one")
            self.assertIn("launcher_signature", before)
            runtime.assert_admin_executor_snapshot("project-one", before)
            self.executable(self.path_bin / "agy.exe")
            with self.assertRaisesRegex(ContractError, "executor_changed"):
                runtime.assert_admin_executor_snapshot("project-one", before)

    def test_routing_freezes_ag_launcher_binary_identity(self):
        selected = self.executable(self.path_bin / "agy.exe")
        runtime = RoutingRuntime(self.root / "routing.sqlite3", host="antigravity", config={"enabled": True})
        self.addCleanup(runtime.db.close)
        with self.windows_resolution():
            before = runtime.resolve_admin_executor("project-one")
            selected.write_text("different binary contents and size; not executed", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "executor_changed"):
                runtime.assert_admin_executor_snapshot("project-one", before)

    def test_readonly_routing_does_not_enable_admin_or_run_help(self):
        self.executable(self.official)
        runtime = RoutingRuntime(self.root / "routing.sqlite3", host="antigravity")
        self.addCleanup(runtime.db.close)
        with self.windows_resolution(), patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
            result = runtime.admin_routing_status("project-one")
        run.assert_not_called()
        popen.assert_not_called()
        self.assertFalse(result["policy"]["enabled"])
        self.assertEqual(result["availability"]["readiness_scope"], "launcher_only")
        self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_host_policies").fetchone()[0], 0)

    def test_readonly_routing_reports_cached_unsupported_protocol(self):
        self.executable(self.official)
        runtime = RoutingRuntime(self.root / "routing.sqlite3", host="antigravity")
        self.addCleanup(runtime.db.close)
        with self.windows_resolution():
            executor = runtime.resolve_admin_executor("project-one", require_enabled=False)
            with patch("subprocess.run", side_effect=self.help_probe(supported=False)) as run:
                providers.executor_capability(executor)
                result = runtime.admin_routing_status("project-one")
        self.assertEqual(run.call_count, 1)
        self.assertTrue(result["availability"]["cli_available"])
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "cli_protocol_unsupported")


if __name__ == "__main__":
    unittest.main()
