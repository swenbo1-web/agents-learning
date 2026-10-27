"""Bounded native panel reads without runtime startup or discarded detail scans."""
from contextlib import closing
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from agent_learning.admin_control import control_status, control_summary
from agent_learning.dsh_panel import read_status, snapshot, status_payload
from agent_learning.runtime import ContractError, Runtime


class DshPanelReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dsh-panel-read-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.home = self.root / "home"
        self.config = {"admin": {"command": [sys.executable]}, "scheduler": {"enabled": False}}
        self.runtime = Runtime(self.home, config=self.config, host="dsh")
        self.addCleanup(self.runtime.close)
        (self.home / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        environment = patch.dict(os.environ, {"ALP_CONFIG": str(self.home / "config.json")})
        environment.start()
        self.addCleanup(environment.stop)
        self.project = self.runtime.project(self.workspace)

    def read(self, **kwargs):
        return read_status(self.home, self.workspace, self.workspace, **kwargs)

    def test_status_projection_preserves_native_fields_and_same_control_snapshot(self):
        original = self.runtime.status(self.project)
        control = control_status(self.runtime, self.project)
        original_snapshot = snapshot(self.runtime, self.project)
        observed = self.read()["data"]
        for name in ("project_id", "home", "host_scope", "admin_admission_enabled", "project_usage",
                     "global_usage", "project_budget_policy", "global_budget_policy", "admission"):
            self.assertEqual(observed[name], original[name], name)
        for name, value in observed["counts"].items():
            self.assertEqual(value, original["counts"][name], name)
        for name, value in observed["work_memory"].items():
            self.assertEqual(value, original["work_memory"][name], name)
        self.assertEqual(observed["panel_snapshot"], original_snapshot)
        self.assertEqual(observed["panel_snapshot"]["diagnostic"], control["diagnostic"])
        self.assertEqual(observed["history_import"]["onboarding"]["state"], "unoffered")

    def test_native_read_never_runs_runtime_startup_or_unused_detail_scans(self):
        with patch.object(Runtime, "__init__", side_effect=AssertionError("runtime startup")), \
             patch.object(Runtime, "status", side_effect=AssertionError("full status")), \
             patch.object(Runtime, "history_status", side_effect=AssertionError("history detail")), \
             patch("agent_learning.admin_control._control_queue", side_effect=AssertionError("queue detail")), \
             patch("agent_learning.admin_recovery.recovery_status", side_effect=AssertionError("recovery detail")), \
             patch("agent_learning.admin_review_flow.review_activity", side_effect=AssertionError("activity detail")), \
             patch("subprocess.Popen", side_effect=AssertionError("process launch")):
            self.assertEqual(self.read()["data"]["project_id"], self.project)

    def test_common_summary_and_existing_light_view_do_not_expand_health(self):
        import agent_learning.admin_control as module
        with patch.object(module, "phase_status", wraps=module.phase_status) as phases, \
             patch("agent_learning.admin_queue.dispatch_diagnostic", wraps=__import__(
                 "agent_learning.admin_queue", fromlist=["dispatch_diagnostic"]).dispatch_diagnostic) as diagnostic:
            light = control_status(self.runtime, self.project, details=False)
        self.assertTrue(light["details_separate"])
        self.assertNotIn("queue", light)
        self.assertEqual(phases.call_args.kwargs["include_health"], False)
        self.assertEqual(diagnostic.call_args.kwargs["check_queue"], False)
        full_core = control_summary(self.runtime, self.project)
        self.assertNotIn("queue", full_core)
        self.assertNotIn("recovery", full_core)
        self.assertNotIn("review_activity", full_core)

    def test_panel_core_is_read_once(self):
        with patch("agent_learning.dsh_panel.control_summary", wraps=control_summary) as common:
            status_payload(self.runtime, self.project)
        common.assert_called_once_with(self.runtime, self.project)

    def test_missing_home_database_are_not_created(self):
        absent = self.root / "absent-home"
        self.assertEqual(read_status(absent, self.workspace, self.workspace), {"registered": False})
        self.assertFalse(absent.exists())
        empty = self.root / "empty-home"
        empty.mkdir()
        self.assertEqual(read_status(empty, self.workspace, self.workspace), {"registered": False})
        self.assertEqual(list(empty.iterdir()), [])

    def test_unregistered_project_is_not_registered_by_get(self):
        other = self.root / "unregistered"
        other.mkdir()
        before = self.runtime.db.total_changes
        self.assertEqual(read_status(self.home, other, other), {"registered": False})
        self.assertEqual(self.runtime.db.total_changes, before)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM project_paths").fetchone()[0], 1)

    def test_subdirectory_allowed_but_nested_checkout_and_alias_are_rejected(self):
        child = self.workspace / "src"
        child.mkdir()
        self.assertEqual(read_status(self.home, child, self.workspace)["data"]["project_id"], self.project)
        (child / ".git").mkdir()
        with self.assertRaisesRegex(ContractError, "not bound"):
            read_status(self.home, child, self.workspace)
        (child / ".git").rmdir()
        self.config["project_aliases"] = {str(child): "other-project"}
        (self.home / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "different logical project"):
            read_status(self.home, child, self.workspace)

    def test_explicit_alias_to_registered_project_remains_supported(self):
        other = self.root / "alias"
        other.mkdir()
        self.config["project_aliases"] = {str(other): self.project}
        (self.home / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        self.assertEqual(read_status(self.home, other, other)["data"]["project_id"], self.project)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM project_paths").fetchone()[0], 1)

    def test_deleted_path_mapping_is_not_recreated_from_existing_project_hash(self):
        self.runtime.db.execute("DELETE FROM project_paths")
        self.assertEqual(self.read(), {"registered": False})
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM project_paths").fetchone()[0], 0)

    def test_uninitialized_dsh_scope_is_not_created_by_read(self):
        self.runtime.db.execute("DELETE FROM host_scopes WHERE scope='dsh:default'")
        with self.assertRaisesRegex(ContractError, "host_scope_uninitialized"):
            self.read()
        self.assertFalse(self.runtime.db.execute("SELECT 1 FROM host_scopes WHERE scope='dsh:default'").fetchone())

    def test_wrong_expected_project_fails_closed(self):
        with self.assertRaisesRegex(ContractError, "project changed"):
            self.read(expected_project="other-project")

    def test_old_schema_requires_explicit_upgrade_without_migration(self):
        self.runtime.db.execute("PRAGMA user_version=24")
        with patch.object(Runtime, "__init__", side_effect=AssertionError("migration")), \
             self.assertRaisesRegex(ContractError, "explicit upgrade"):
            self.read()
        self.assertEqual(self.runtime.db.execute("PRAGMA user_version").fetchone()[0], 24)

    def test_read_snapshot_keeps_epoch_and_counts_consistent_during_other_writer(self):
        conversation = self.runtime.host_conversation("snapshot")
        self.runtime.task_update(self.project, conversation, "Check consistent panel projection")
        problem = self.runtime.feedback(self.project, conversation, "A pending observation for the panel")

        def interleave(runtime, project):
            state = control_summary(runtime, project)
            with closing(sqlite3.connect(self.home / "runtime.sqlite3", isolation_level=None)) as writer:
                writer.execute("UPDATE host_scopes SET epoch=epoch+1 WHERE scope='dsh:default'")
                writer.execute("UPDATE problems SET status='resolved' WHERE project=? AND id=?",
                               (project, problem["id"]))
            return state

        with patch("agent_learning.dsh_panel.control_summary", side_effect=interleave):
            data = self.read()["data"]
        old_epoch = data["panel_snapshot"]["expected"]["expected_session_epoch"]
        current = self.read()["data"]
        self.assertEqual(current["panel_snapshot"]["expected"]["expected_session_epoch"], old_epoch + 1)
        self.assertEqual(data["counts"]["pending_problems"], 1)
        self.assertEqual(current["counts"]["pending_problems"], 0)

    def test_sqlite_query_only_rejects_accidental_write(self):
        def attempt_write(runtime, project):
            runtime.db.execute("DELETE FROM projects")
        with patch("agent_learning.dsh_panel.status_payload", side_effect=attempt_write), \
             self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
            self.read()
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)

    def test_query_budget_interrupts_excessive_projection(self):
        def costly(runtime, project):
            runtime.db.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) "
                               "SELECT SUM(x) FROM n").fetchone()
        with patch("agent_learning.dsh_panel.status_payload", side_effect=costly), \
             self.assertRaisesRegex(sqlite3.OperationalError, "interrupted"):
            self.read()

    def test_hook_status_uses_readonly_branch_before_runtime_constructor(self):
        entry = Path(__file__).resolve().parents[1] / "hooks/dsh_tool.py"
        spec = importlib.util.spec_from_file_location("dsh_readonly_hook", entry)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = {"home": str(self.home), "workspace": str(self.workspace),
                   "panel_bound_workspace": str(self.workspace), "panel_status": True}
        output = io.StringIO()
        with patch.object(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))), \
             patch.object(sys, "stdout", output), \
             patch.object(Runtime, "__init__", side_effect=AssertionError("runtime startup")), \
             patch("subprocess.Popen", side_effect=AssertionError("model launch")):
            self.assertEqual(module.main(), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["data"]["project_id"], self.project)

    def test_hook_panel_read_errors_have_fixed_reasons_without_sql_details(self):
        entry = Path(__file__).resolve().parents[1] / "hooks/dsh_tool.py"
        spec = importlib.util.spec_from_file_location("dsh_read_error_hook", entry)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = {"home": str(self.home), "workspace": str(self.workspace),
                   "panel_bound_workspace": str(self.workspace), "panel_status": True}
        coded = sqlite3.OperationalError("PRIVATE details from SQLite")
        coded.sqlite_errorcode = 9
        for error, reason in ((coded, "query_budget_exceeded"),
                              (sqlite3.OperationalError("interrupted"), "query_budget_exceeded"),
                              (sqlite3.OperationalError("PRIVATE SQL: locked"), "database_read_unavailable"),
                              (ContractError("host_scope_uninitialized"), "host_scope_uninitialized")):
            with self.subTest(reason=reason), patch.dict(sqlite3.__dict__):
                # Python 3.10 does not expose all SQLite result constants.
                sqlite3.__dict__.pop("SQLITE_INTERRUPT", None)
                output = io.StringIO()
                with patch.object(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))), \
                     patch.object(sys, "stdout", output), \
                     patch("agent_learning.dsh_panel.read_status", side_effect=error), \
                     patch.object(Runtime, "__init__", side_effect=AssertionError("runtime startup")):
                    self.assertEqual(module.main(), 0)
                result = json.loads(output.getvalue())
                self.assertFalse(result["ok"])
                self.assertEqual(result["reason"], reason)
                self.assertNotIn("PRIVATE", result["error"])


if __name__ == "__main__":
    unittest.main()
