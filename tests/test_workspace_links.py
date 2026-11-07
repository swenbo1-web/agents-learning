"""Explicit roots never change project identity, knowledge or execution scope."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_learning.cli import main
from agent_learning.runtime import ContractError, Runtime
from agent_learning.workspace_links import (MAX_LINKED_ROOTS, _config_lock, file_workspace_roots,
                                             open_workspace_link_view, workspace_project,
                                             set_workspace_link, workspace_links_status)


class WorkspaceLinkTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "build/local/r62policy"
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home = self.base / "home"
        self.config = self.base / "config.json"
        self.workspace = self.base / "project"
        self.other = self.base / "other"
        for root in (self.workspace, self.other):
            root.mkdir()
            (root / ".git").mkdir()
        self.environment = mock.patch.dict(os.environ, {
            "ALP_HOME": str(self.home), "ALP_CONFIG": str(self.config),
            "ALP_RUNTIME_STORE": str(self.base / "runtime-releases"),
            "CODEX_HOME": str(self.base / "codex-home"), "PYTHONDONTWRITEBYTECODE": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.runtime = Runtime(self.home)
        self.project = self.runtime.project(self.workspace)
        self.addCleanup(lambda: self.runtime.close())

    def write_config(self, value):
        self.config.write_text(json.dumps(value), encoding="utf-8")

    def reload(self, **kwargs):
        self.runtime.close()
        self.runtime = Runtime(self.home, **kwargs)

    def state(self, workspace=True):
        return workspace_links_status(self.runtime, self.project, self.workspace if workspace else None)

    def link(self, root=None, **kwargs):
        version = kwargs.pop("expected_version") if "expected_version" in kwargs else self.state()["config_version"]
        return set_workspace_link(self.runtime, self.project, self.workspace, str(root or self.other),
            expected_version=version,
            confirm=kwargs.pop("confirm", True), **kwargs)

    def configure(self):
        self.write_config({"linked_workspaces": {self.project: [str(self.other)]}})
        self.reload()

    def test_default_is_primary_only_and_does_not_write_config(self):
        self.assertEqual([item["path"] for item in self.state()["roots"]], [str(self.workspace)])
        self.assertFalse(self.config.exists())
        self.assertFalse(self.state()["execution_permission"])

    def test_explicit_link_keeps_identity_knowledge_and_permissions_separate(self):
        foreign = self.runtime.project(self.other)
        paths = [tuple(row) for row in self.runtime.db.execute("SELECT * FROM project_paths ORDER BY path")]
        self.assertNotEqual(foreign, self.project)
        result = self.link()
        self.assertEqual(result["status"], "linked")
        self.assertFalse(result["execution_permission"])
        self.assertEqual([tuple(row) for row in self.runtime.db.execute("SELECT * FROM project_paths ORDER BY path")], paths)
        self.assertEqual(self.runtime.project(self.other), foreign)
        self.assertFalse(self.runtime.config["enforce_task_policy"])
        self.reload()
        roots = self.state()["roots"]
        self.assertEqual([item["path"] for item in roots], [str(self.workspace), str(self.other)])
        self.assertEqual([item["primary"] for item in roots], [True, False])
        self.assertNotEqual(roots[0]["checkout_key"], roots[1]["checkout_key"])
        self.assertEqual(len(file_workspace_roots(self.runtime, foreign, self.other)), 1)

    def test_supplied_config_is_a_distinct_trusted_source(self):
        self.reload(config={"linked_workspaces": {self.project: [str(self.other)]}})
        self.assertEqual(len(self.state()["roots"]), 2)
        self.assertFalse(self.state()["reload_required"])
        with self.assertRaisesRegex(ContractError, "file-backed"):
            self.link(remove=True)

    def test_runtime_reloads_required_before_new_root_is_used(self):
        self.link()
        self.assertTrue(self.state()["reload_required"])
        self.assertEqual(len(self.state()["roots"]), 1)
        self.reload()
        self.assertEqual(len(self.state()["roots"]), 2)

    def test_revocation_does_not_touch_former_root(self):
        self.configure()
        self.assertEqual(len(self.state()["roots"]), 2)
        self.write_config({"linked_workspaces": {}})
        with mock.patch("agent_learning.workspace_links._checked_root", side_effect=AssertionError("revoked path touched")):
            result = self.state()
        self.assertTrue(result["reload_required"])
        self.assertEqual(len(result["roots"]), 1)

    def test_no_workspace_shows_configuration_without_observing_roots(self):
        self.configure()
        with mock.patch("agent_learning.workspace_links._checked_root", side_effect=AssertionError("path observed")):
            result = self.state(workspace=False)
        self.assertEqual(result["roots"], [])
        self.assertEqual(result["linked_workspaces"], [str(self.other)])
        self.assertFalse(result["workspace_verified"])

    def test_read_view_cannot_claim_host_snapshot_or_observe_linked_roots(self):
        self.configure()
        self.runtime._workspace_links_snapshot_verified = False
        with mock.patch("agent_learning.workspace_links._checked_root", side_effect=AssertionError("path observed")):
            for workspace in (True, False):
                result = self.state(workspace=workspace)
                self.assertIsNone(result["reload_required"])
                self.assertFalse(result["workspace_verified"])
                self.assertEqual(len(result["roots"]), 1 if workspace else 0)
                self.assertEqual(result["linked_workspaces"], [str(self.other)])
        with self.assertRaisesRegex(ContractError, "file-backed"):
            self.link(remove=True)

    def test_other_project_or_admin_change_does_not_invalidate_roots(self):
        self.configure()
        self.state()
        self.write_config({"linked_workspaces": {self.project: [str(self.other)], "elsewhere": []},
                           "admin": {"enabled": False, "max_calls_per_day": 13}})
        self.assertFalse(self.state()["reload_required"])
        self.assertEqual(len(self.state()["roots"]), 2)

    def test_unchanged_config_is_parsed_only_once(self):
        self.configure()
        from agent_learning import workspace_links
        with mock.patch.object(workspace_links, "_read_config", wraps=workspace_links._read_config) as read:
            for _ in range(4):
                self.assertEqual(len(self.state()["roots"]), 2)
        self.assertEqual(read.call_count, 1)

    def test_stale_project_version_rejects_without_losing_first_change(self):
        version = self.state()["config_version"]
        self.link(expected_version=version)
        third = self.base / "third"
        third.mkdir()
        with self.assertRaisesRegex(ContractError, "changed"):
            self.link(third, expected_version=version)
        self.assertEqual(json.loads(self.config.read_text())["linked_workspaces"][self.project], [str(self.other)])

    def test_project_cas_preserves_unrelated_current_settings(self):
        version = self.state()["config_version"]
        self.write_config({"admin": {"enabled": False, "max_calls_per_day": 17}, "custom": {"keep": True},
                           "linked_workspaces": {"other-project": [str(self.other)]}})
        self.link(expected_version=version)
        supplied = json.loads(self.config.read_text())
        self.assertEqual(supplied["admin"]["max_calls_per_day"], 17)
        self.assertEqual(supplied["custom"], {"keep": True})
        self.assertEqual(supplied["linked_workspaces"]["other-project"], [str(self.other)])

    def test_changed_alias_identity_rejects_mutation(self):
        self.write_config({"project_aliases": {str(self.workspace): "different-project"}})
        with self.assertRaisesRegex(ContractError, "identity"):
            self.link()
        self.assertNotIn("linked_workspaces", json.loads(self.config.read_text()))

    def test_confirmation_and_matching_known_current_project_required(self):
        with self.assertRaisesRegex(ContractError, "confirm"):
            self.link(confirm=False)
        with self.assertRaisesRegex(ContractError, "identity"):
            set_workspace_link(self.runtime, "unknown", self.workspace, str(self.other),
                               expected_version=self.state()["config_version"], confirm=True)
        self.assertFalse(self.config.exists())

    def test_unknown_missing_and_nested_checkout_roots_are_rejected(self):
        nested = self.other / "src"
        nested.mkdir()
        for root in (self.base / "missing", nested, self.workspace, self.workspace.parent):
            with self.subTest(root=root), self.assertRaises((ValueError, OSError)):
                self.link(root)
        self.assertFalse(self.config.exists())

    def test_relative_drive_sensitive_and_private_roots_are_rejected(self):
        sensitive = self.base / "secrets"
        sensitive.mkdir()
        for root in ("relative", str(Path(self.workspace.anchor)), str(sensitive), str(self.home)):
            with self.subTest(root=root), self.assertRaises((ValueError, OSError)):
                self.link(root)

    def test_traversal_and_url_are_rejected(self):
        for root in (str(self.other / ".." / "other"), "https://example.test/project"):
            with self.subTest(root=root), self.assertRaises(ContractError):
                self.link(root)

    def test_overlapping_linked_roots_are_rejected(self):
        self.link()
        nested = self.other / "nested-checkout"
        nested.mkdir()
        (nested / ".git").mkdir()
        with self.assertRaisesRegex(ContractError, "overlap"):
            self.link(nested)

    def test_reparse_root_rejected_before_resolve(self):
        from agent_learning import workspace_links
        original = Path.lstat

        def flagged(path, *args, **kwargs):
            if path == self.other:
                return type("Reparse", (), {"st_mode": 0, "st_file_attributes": 0x400})()
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "lstat", flagged), self.assertRaisesRegex(ContractError, "reparse"):
            self.link()

    def test_missing_or_replaced_linked_root_is_ignored(self):
        self.configure()
        (self.other / ".git").rmdir()
        self.other.rmdir()
        self.assertEqual(len(self.state()["roots"]), 1)
        self.assertEqual(self.state()["rejected_roots"], [{"reason": "invalid_linked_root"}])

    def test_unlink_missing_or_replaced_root_does_not_validate_it(self):
        self.configure()
        version = self.state()["config_version"]
        with mock.patch("agent_learning.workspace_links._checked_root", side_effect=AssertionError("path touched")):
            result = self.link(remove=True, expected_version=version)
        self.assertEqual(result["status"], "unlinked")
        self.assertEqual(json.loads(self.config.read_text())["linked_workspaces"], {})

    def test_duplicate_link_and_unlink_are_idempotent(self):
        self.link()
        self.assertEqual(self.link()["status"], "unchanged")
        self.assertEqual(self.link(remove=True)["status"], "unlinked")
        self.assertEqual(self.link(remove=True)["status"], "unchanged")

    def test_invalid_configuration_shapes_and_budget_fail_closed(self):
        bad = (None, [], {self.project: str(self.other)}, {"": []},
               {self.project: [str(self.other)] * 2}, {self.project: ["relative"]},
               {self.project: [str(self.base / str(index)) for index in range(MAX_LINKED_ROOTS + 1)]})
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ContractError):
                Runtime(self.base / "bad-home", config={"linked_workspaces": value})

    def test_invalid_changed_disk_config_stops_former_root_checks(self):
        self.configure()
        self.state()
        self.config.write_text("{invalid", encoding="utf-8")
        with mock.patch("agent_learning.workspace_links._checked_root", side_effect=AssertionError("path touched")):
            self.assertTrue(self.state()["reload_required"])

    def test_config_too_large_is_rejected_before_mutation(self):
        version = self.state()["config_version"]
        self.config.write_text('{"padding":"' + "x" * (1024 * 1024) + '"}', encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "byte limit"):
            self.link(expected_version=version)

    def test_lock_and_atomic_failure_leave_original_configuration(self):
        self.write_config({"custom": "preserve"})
        version = self.state()["config_version"]
        lock = self.config.with_name(self.config.name + ".workspace-links.lock")
        with _config_lock(lock), self.assertRaisesRegex(ContractError, "being edited"):
            self.link(expected_version=version)
        before = self.config.read_bytes()
        with mock.patch("agent_learning.workspace_links.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                self.link(expected_version=version)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_three_hosts_share_explicit_roots_without_changing_identity(self):
        self.configure()
        for host in ("codex", "antigravity", "dsh"):
            self.reload(host=host)
            self.assertEqual(self.runtime.project(self.workspace), self.project)
            self.assertEqual(len(self.state()["roots"]), 2)

    def test_git_worktree_file_retains_its_own_checkout(self):
        (self.other / ".git").rmdir()
        (self.other / ".git").write_text("gitdir: ../project/.git/worktrees/other", encoding="utf-8")
        self.link()
        self.reload()
        roots = self.state()["roots"]
        self.assertEqual(len(roots), 2)
        self.assertNotEqual(roots[0]["checkout_key"], roots[1]["checkout_key"])

    def test_config_paths_are_normalized_once_without_permanent_reload_loop(self):
        self.write_config({"linked_workspaces": {self.project: [self.other.as_posix()]}})
        self.reload()
        self.assertFalse(self.state()["reload_required"])
        self.assertEqual(len(self.state()["roots"]), 2)

    def test_configuration_race_before_atomic_save_is_rejected(self):
        from agent_learning import workspace_links
        original = workspace_links._read_config
        version = self.state()["config_version"]

        def concurrent_change(path):
            supplied = original(path)
            self.write_config({"concurrent": "preserve"})
            return supplied

        with mock.patch.object(workspace_links, "_read_config", side_effect=concurrent_change):
            with self.assertRaisesRegex(ContractError, "changed while reading"):
                self.link(expected_version=version)
        self.assertEqual(json.loads(self.config.read_text()), {"concurrent": "preserve"})

    def cli(self, *args, workspace=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        command = ["--home", str(self.home)]
        if workspace:
            command += ["--workspace", str(self.workspace)]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(command + list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_list_link_unlink_round_trip(self):
        code, stdout, _ = self.cli("workspace-list")
        self.assertEqual(code, 0)
        listed = json.loads(stdout)
        self.assertEqual(listed["project"], self.project)
        command = [str(self.other), "--project", self.project, "--expected-version", listed["config_version"], "--confirm"]
        code, stdout, stderr = self.cli("workspace-link", *command)
        self.assertEqual(code, 0, stderr)
        linked = json.loads(stdout)
        command[-2] = linked["config_version"]
        code, stdout, stderr = self.cli("workspace-unlink", *command)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "unlinked")

    def test_cli_requires_explicit_workspace_and_confirmation(self):
        code, _, error = self.cli("workspace-list", workspace=False)
        self.assertEqual(code, 1)
        self.assertIn("explicit absolute", error)
        code, _, error = self.cli("workspace-link", str(self.other), "--project", self.project,
                                  "--expected-version", self.state()["config_version"])
        self.assertEqual(code, 1)
        self.assertIn("confirm", error)
        self.assertFalse(self.config.exists())

    def test_cli_list_and_mutation_never_initialize_runtime(self):
        with mock.patch("agent_learning.cli.Runtime", side_effect=AssertionError("initialized")):
            code, stdout, stderr = self.cli("workspace-list")
            self.assertEqual(code, 0, stderr)
            listed = json.loads(stdout)
            self.assertEqual(listed["project_id"], self.project)
            code, _, stderr = self.cli("workspace-link", str(self.other), "--project", self.project,
                "--expected-version", listed["config_version"], "--confirm")
            self.assertEqual(code, 0, stderr)

    def test_cli_unknown_project_is_not_registered_as_a_side_effect(self):
        before = [tuple(row) for row in self.runtime.db.execute("SELECT * FROM project_paths ORDER BY path")]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--home", str(self.home), "--workspace", str(self.other), "workspace-list"])
        self.assertEqual(code, 1)
        self.assertEqual([tuple(row) for row in self.runtime.db.execute("SELECT * FROM project_paths ORDER BY path")], before)

    def test_cli_missing_home_never_creates_a_database_or_directory(self):
        missing = self.base / "not-initialized"
        with mock.patch.dict(os.environ, {"ALP_HOME": str(missing)}):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(["--workspace", str(self.workspace), "workspace-list"])
        self.assertEqual(code, 1)
        self.assertIn("not registered", stderr.getvalue())
        self.assertFalse(missing.exists())

    def test_config_edit_view_is_query_only_and_reports_no_database_changes(self):
        with open_workspace_link_view(self.home) as view:
            self.assertEqual(workspace_project(view, self.workspace), self.project)
            version = workspace_links_status(view, self.project, self.workspace)["config_version"]
            set_workspace_link(view, self.project, self.workspace, str(self.other),
                               expected_version=version, confirm=True)
            self.assertEqual(view.db.total_changes, 0)
            self.assertEqual(view.db.execute("PRAGMA query_only").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
