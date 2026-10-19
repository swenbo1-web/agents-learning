"""The local-deferral protocol fences v23 executors without rewriting evidence."""
import copy
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning import candidate_references as references
from agent_learning.admin_usage import configure_project_budget
from agent_learning.phase_review import configure
from agent_learning.runtime import ContractError, Runtime
from tests import test_history_response_recovery as history_fixtures
from tests import test_phase_review as phase_fixtures


class Schema24BoundaryTests(unittest.TestCase):
    failed = history_fixtures.HistoryResponseRecoveryTests.failed
    phase = phase_fixtures.PhaseReviewTests.phase

    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "build" / "local" / "r80-schema"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="boundary-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        (self.workspace / "README.md").write_text(
            "RenderingPipeline ShaderCache project architecture.", encoding="utf-8")
        self.r = Runtime(self.root / "home", config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.config["admin"].update(command=[sys.executable], timeout_seconds=10)

    def old_database(self):
        self.r.db.execute("INSERT INTO admin_control_state(project,host_scope,enabled,version,updated,auto_publish,publication_mode) "
            "VALUES(?,?,0,1,?,1,'tiered')", (self.p, self.r._session_scope(), time.time()))
        job, call = self.failed(auto_reference=True)
        references.grant_history(self.r, self.p, job, True, 0)
        configure_project_budget(self.r, self.p, {"max_reported_tokens_per_day": 123456}, expected_version=0)
        self.r.admin_configure(self.p, True, model_mode="fixed", model="operator-chosen-model")
        configure(self.r, self.p, {"mode": "phase", "every_phases": 2}, 0)
        self.phase("first-stage", "A sourced rendering convention")
        self.phase("second-stage", "An independent shader-cache design conclusion")
        self.phase("third-stage", "Another real project architecture result")
        self.handle = self.r.register_context(self.p, "first-stage")["context_handle"]
        self.r.db.execute("PRAGMA user_version=23")
        self.r.db.execute("UPDATE runtime_storage_receipt SET signature=? WHERE id=1",
                          ("1.4.0:schema23:knowledge4:work1:startup8",))
        return job, call

    def snapshot(self, runtime=None):
        runtime = runtime or self.r
        tables = (
            "tasks", "task_snapshots", "events", "problems", "problem_events",
            "review_phases", "phase_scan_progress", "phase_review_batches", "phase_review_members",
            "admin_calls", "admin_call_budgets", "admin_usage_observations", "admin_execution_plans",
            "admin_host_policies", "admin_project_budget_policies", "phase_review_policies", "admin_control_state",
            "history_jobs", "history_job_sources", "history_materials", "history_units", "history_case_links",
            "candidate_reference_history", "history_response_accepts", "history_response_artifacts",
        )
        return {table: [tuple(row) for row in runtime.db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                for table in tables}

    def open_runtime(self):
        runtime = Runtime(self.r.home, config=copy.deepcopy(self.r.config))
        self.addCleanup(runtime.close)
        return runtime

    def test_v23_upgrade_preserves_stage_records_ledgers_policy_and_history_authority(self):
        job, call = self.old_database()
        before = self.snapshot()
        epoch = self.r.session_epoch()
        with patch("subprocess.Popen", side_effect=AssertionError("Migration cannot launch a model")):
            upgraded = self.open_runtime()
        self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 25)
        self.assertEqual(self.snapshot(upgraded), before)
        self.assertGreater(upgraded.session_epoch(), epoch)
        self.assertEqual(upgraded.session_epoch(), upgraded.host_epoch())
        with self.assertRaises(ContractError):
            upgraded.access_context(self.p, self.handle)
        # A protocol fence preserves the grant record, but cannot re-authorize
        # a historical batch under a newly created governance boundary.
        self.assertFalse(references.history_grant(upgraded, self.p, job)["enabled"])
        paid = upgraded.db.execute("SELECT state,retry_allowed,json_extract(usage,'$.reported_turns') "
                                   "FROM admin_calls WHERE id=?", (call,)).fetchone()
        self.assertEqual(tuple(paid), ("failed", 0, 1))
        with patch("subprocess.Popen", side_effect=AssertionError("Reopening cannot launch a model")):
            reopened = self.open_runtime()
        self.assertEqual(reopened.session_epoch(), upgraded.session_epoch(), "fence is applied exactly once")
        self.assertEqual(self.snapshot(reopened), before)

    def test_interruption_after_fence_rolls_back_schema_epochs_and_authority(self):
        self.old_database()
        before = self.snapshot()
        epoch = self.r.session_epoch()
        receipt = tuple(self.r.db.execute("SELECT * FROM runtime_storage_receipt").fetchone())
        from agent_learning import runtime_storage
        mark = runtime_storage.mark_storage_ready

        def interrupted(runtime):
            self.assertEqual(runtime.db.execute("PRAGMA user_version").fetchone()[0], 25)
            self.assertGreater(runtime.session_epoch(), epoch)
            mark(runtime)
            raise sqlite3.OperationalError("Simulated protocol-24 commit interruption")

        with patch.object(runtime_storage, "mark_storage_ready", side_effect=interrupted), patch(
                "subprocess.Popen", side_effect=AssertionError("Migration cannot launch a model")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "commit interruption"):
                self.open_runtime()
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 23)
        self.assertEqual(self.r.session_epoch(), epoch)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(tuple(self.r.db.execute("SELECT * FROM runtime_storage_receipt").fetchone()), receipt)
        self.assertTrue(self.r.access_context(self.p, self.handle))

    def test_v26_is_rejected_before_any_initializer_or_authority_write(self):
        self.old_database()
        self.r.db.execute("PRAGMA user_version=26")
        before = self.snapshot()
        epoch = self.r.session_epoch()
        with patch.object(Runtime, "_initialize_storage") as initialize, patch("subprocess.Popen") as launch:
            with self.assertRaisesRegex(ContractError, "unsupported runtime database version"):
                self.open_runtime()
            initialize.assert_not_called()
            launch.assert_not_called()
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 26)
        self.assertEqual(self.r.session_epoch(), epoch)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
