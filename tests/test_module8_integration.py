"""第八模块入口、迁移和派发的离线集成，不启动真实后台服务。"""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.cli import main
from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime


class Module8IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-module8-integrated-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "state"
        self.work = Path(self.temp.name) / "project"
        self.work.mkdir()
        self.r = Runtime(self.home)
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.work)
        self.task = self.r.task_update(self.p, "chat", "compiler guidance")
        self.handle = self.r.register_context(self.p, "chat")["context_handle"]

    def publish(self):
        problem = self.r.feedback(self.p, "chat", "compiler include path", category="operation")
        refs = [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="compiler guidance", content="Check the local include path", category="operation",
            applicability="same tool", limitations="reported only", evidence_refs=refs, validation_level="reported")
        return self.r.publish(self.p, candidate["candidate_id"])

    def use_args(self, knowledge):
        return dict(context_handle=self.handle, knowledge_id=knowledge["knowledge_id"],
                    knowledge_revision=knowledge["revision"], expected_task_id=self.task["id"],
                    expected_task_revision=self.task["revision"], expected_write_version=self.task["write_version"],
                    idempotency_key="actual-adoption")

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--home", str(self.home), "--workspace", str(self.work), *args])
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_new_defaults_do_not_enable_paid_or_scheduled_work(self):
        self.assertFalse(self.r.config["admin"]["enabled"])
        self.assertFalse(self.r.config["scheduler"]["enabled"])
        self.assertFalse(self.r.config["scheduler"]["auto_release"])
        self.assertFalse(self.r.config["scheduler"]["sync_enabled"])
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 25)

    def test_schema9_upgrade_and_reopen_preserve_knowledge(self):
        knowledge = self.publish()
        with self.r.transaction():
            for table in ("scheduler_worker", "scheduler_jobs", "admin_usage_observations", "admin_call_budgets",
                          "knowledge_adoptions", "learning_exposures"):
                self.r.db.execute("DROP TABLE " + table)
            self.r.db.execute("PRAGMA user_version=9")
        self.r.close()
        for _ in range(2):
            with contextlib.closing(Runtime(self.home)) as upgraded:
                self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 25)
                self.assertEqual(upgraded.read(self.p, knowledge["knowledge_id"])["revision"], knowledge["revision"])

    def test_module8_migration_failure_rolls_back_and_allows_retry(self):
        knowledge = self.publish()
        with self.r.transaction():
            for table in ("scheduler_worker", "scheduler_jobs", "admin_usage_observations", "admin_call_budgets",
                          "knowledge_adoptions", "learning_exposures"):
                self.r.db.execute("DROP TABLE " + table)
            self.r.db.execute("PRAGMA user_version=9")
        self.r.close()
        with patch("agent_learning.learning_evaluation.LearningEvaluationMixin._migrate_learning_evaluation",
                   side_effect=RuntimeError("interrupted migration")), self.assertRaises(RuntimeError):
            Runtime(self.home)
        with contextlib.closing(sqlite3.connect(self.home / "runtime.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_jobs'").fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='admin_call_budgets'").fetchone())
        with contextlib.closing(Runtime(self.home)) as upgraded:
            self.assertEqual(upgraded.read(self.p, knowledge["knowledge_id"])["revision"], 1)
            self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 25)

    def test_mcp_exposure_then_adoption_is_small_and_idempotent(self):
        knowledge = self.publish()
        server = Server(self.r, self.p, context_handle=self.handle)
        self.assertEqual(len(server.tools), 11)
        self.assertTrue({"candidate_reference_read", "candidate_reference_feedback"} <= set(server.tools))
        self.assertNotIn("candidate-reference-configure", server.tools)
        self.assertEqual(len(Server(self.r, self.p, role="admin").tools), 16)
        with self.assertRaises(ContractError):
            server.call("knowledge_use", self.use_args(knowledge))
        server.call("knowledge_read", {"knowledge_id": knowledge["knowledge_id"]})
        first = server.call("knowledge_use", self.use_args(knowledge))
        second = server.call("knowledge_use", self.use_args(knowledge))
        self.assertEqual(first, second)
        self.assertLess(len(json.dumps(first)), 6000)
        self.assertNotIn("verified", json.dumps(first))

    def test_mcp_cannot_use_receipt_from_old_account(self):
        server = Server(self.r, self.p, context_handle=self.handle)
        knowledge = self.publish()
        server.call("knowledge_read", {"knowledge_id": knowledge["knowledge_id"]})
        self.r.account_changed("actual-change")
        with self.assertRaises(ContractError):
            server.call("knowledge_use", self.use_args(knowledge))

    def test_status_includes_unsent_dirty_and_separate_budget_effects(self):
        self.publish()
        status = self.r.status(self.p)
        self.assertGreater(status["counts"]["knowledge_sync_dirty"], 0)
        self.assertIn("admin_budget", status)
        self.assertIn("scheduler", status)
        self.assertIn("learning_effects", status)
        self.assertEqual(status["counts"]["outbox"], 0)

    def test_cli_effects_and_scheduler_default_disabled(self):
        code, effects = self.cli("learning-effects", "--days", "7")
        self.assertEqual(code, 0)
        self.assertIsInstance(effects, dict)
        code, result = self.cli("scheduler-tick", "--max-jobs", "1", "--max-seconds", "1")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "disabled")

    def test_cli_adoption_uses_verified_context(self):
        knowledge = self.publish()
        Server(self.r, self.p, context_handle=self.handle).call("knowledge_read", {"knowledge_id": knowledge["knowledge_id"]})
        code, receipt = self.cli("knowledge-use", "--context-handle", self.handle,
            "--knowledge-id", knowledge["knowledge_id"], "--knowledge-revision", str(knowledge["revision"]),
            "--expected-task-id", self.task["id"], "--expected-task-revision", str(self.task["revision"]),
            "--expected-write-version", str(self.task["write_version"]), "--idempotency-key", "cli-use")
        self.assertEqual(code, 0)
        self.assertLess(len(json.dumps(receipt)), 6000)

    def test_session_start_wakes_scheduler_without_waiting_for_model(self):
        self.r.config["scheduler"]["enabled"] = True
        with patch.object(self.r, "scheduler_wake", return_value=True), \
             patch.object(self.r, "claim_scheduler_worker", return_value="reserved-worker"), \
             patch.object(self.r, "_launch_scheduler") as spawn:
            self.r.handle_hook({"hook_event_name": "SessionStart", "session_id": "chat", "cwd": str(self.work), "source": "resume"})
        spawn.assert_called_once_with(str(self.work), "reserved-worker")

    def test_stop_uses_scheduler_instead_of_legacy_dispatch(self):
        self.r.config["scheduler"]["enabled"] = True
        self.r.config["admin"]["enabled"] = True
        with patch.object(self.r, "scheduler_wake", return_value=True), \
             patch.object(self.r, "claim_scheduler_worker", return_value="reserved-worker"), \
             patch.object(self.r, "_launch_scheduler") as spawn, patch.object(self.r, "_launch_admin") as legacy:
            self.r.handle_hook({"hook_event_name": "Stop", "session_id": "chat", "cwd": str(self.work), "turn_id": "turn"})
        spawn.assert_called_once()
        legacy.assert_not_called()

    def test_schedule_start_failure_keeps_context_and_releases_worker(self):
        self.r.config["scheduler"]["enabled"] = True
        with patch.object(self.r, "scheduler_wake", return_value=True), \
             patch.object(self.r, "claim_scheduler_worker", return_value="reserved-worker"), \
             patch.object(self.r, "release_scheduler_worker") as release, \
             patch.object(self.r, "_launch_scheduler", side_effect=OSError("unavailable")):
            result = self.r.handle_hook({"hook_event_name": "SessionStart", "session_id": "chat", "cwd": str(self.work), "source": "resume"})
        release.assert_called_once_with("reserved-worker")
        self.assertIn("additionalContext", result["hookSpecificOutput"])
        self.assertIn("systemMessage", result)

    def test_log_rotation_failure_does_not_block_process_creation(self):
        log = self.home / "background.log"
        with log.open("wb") as stream:
            stream.truncate(1024 * 1024 + 1)
        with patch.object(Path, "replace", side_effect=PermissionError("active Windows writer")), \
             patch("subprocess.Popen") as spawn:
            self.r._launch_admin(self.work, "token")
        spawn.assert_called_once()
        self.assertEqual(self.r.status(self.p)["metrics"]["background_log_rotation_deferred"], 1)

    def test_failed_dispatch_cleanup_and_metrics_do_not_discard_context(self):
        self.r.config["scheduler"]["enabled"] = True
        with patch.object(self.r, "scheduler_wake", return_value=True), \
             patch.object(self.r, "claim_scheduler_worker", return_value="reserved-worker"), \
             patch.object(self.r, "release_scheduler_worker", side_effect=sqlite3.OperationalError("busy")), \
             patch.object(self.r, "_metric", side_effect=sqlite3.OperationalError("busy")), \
             patch.object(self.r, "_context", return_value="current task context"), \
             patch.object(self.r, "_launch_scheduler", side_effect=OSError("unavailable")):
            result = self.r.handle_hook({"hook_event_name": "SessionStart", "session_id": "chat", "cwd": str(self.work), "source": "resume"})
        self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "current task context")
        self.assertIn("systemMessage", result)

    def test_sync_deadline_boundary_returns_normally(self):
        self.r.config["hindsight"]["enabled"] = True
        with patch("agent_learning.knowledge_sync.time.monotonic", side_effect=[0, .9, 1.1, 1.2, 1.3]):
            result = self.r.sync_batch(self.p, max_seconds=1)
        self.assertTrue(result["budget_exhausted"])
        self.assertEqual(result["processed"], 0)

    def test_default_mode_session_start_wakes_ready_admin_queue(self):
        self.r.admin_configure(self.p, True)
        self.r.admin_observe_model(self.p, "chat", "model-v1")
        self.r.feedback(self.p, "chat", "Pending reviewable problem before resume")
        with patch.object(self.r, "claim_scheduler_worker") as sched_claim, \
             patch("agent_learning.admin_queue.dispatch_diagnostic", return_value={"reason": "ready"}), \
             patch("agent_learning.admin_queue.claim_dispatch", return_value="dispatch-tok-1"), \
             patch.object(self.r, "_launch_admin") as launch_admin:
            self.r.handle_hook({"hook_event_name": "SessionStart", "session_id": "chat", "cwd": str(self.work), "source": "resume"})
        sched_claim.assert_not_called()
        launch_admin.assert_called_once_with(str(self.work), "dispatch-tok-1")

    def test_multi_host_shared_database_scheduler_preserves_enabled_host_ready_state(self):
        import sys
        self.r.config["scheduler"]["enabled"] = True
        self.r.config["admin"].update(command=[sys.executable], max_calls_per_day=100)
        self.r.admin_configure(self.p, True, provider="host", model_mode="default")
        self.r.feedback(self.p, "chat", "Problem queued under codex host")
        self.assertTrue(self.r.scheduler_wake(self.p))
        state_codex = self.r.db.execute("SELECT state FROM scheduler_jobs WHERE project=? AND kind='admin'", (self.p,)).fetchone()[0]
        self.assertEqual(state_codex, "ready")

        other = Runtime(self.home, config={"scheduler": {"enabled": True}, "admin": {"command": [sys.executable], "max_calls_per_day": 100}}, host="dsh")
        self.addCleanup(other.close)
        # dsh 宿主尚未开启管理员，但其刷新调度时不能把 codex 宿主已就绪的 ready 覆盖成 disabled
        self.assertTrue(other.scheduler_wake(self.p))
        state_after_dsh = other.db.execute("SELECT state FROM scheduler_jobs WHERE project=? AND kind='admin'", (self.p,)).fetchone()[0]
        self.assertEqual(state_after_dsh, "ready")


if __name__ == "__main__":
    unittest.main()
