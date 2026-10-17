"""Executor recovery, budget refresh and finite project runners without models."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_batch, admin_once
from agent_learning.admin_lifecycle import claim_call, finish_call, mark_launch_intent
from agent_learning.admin_queue import executor_backoff, record_executor_failure
from agent_learning.admin_usage import configure_project_budget, admin_project_budget_status
from agent_learning.runtime import Runtime
from agent_learning.scheduler import SCHEDULER_DEFAULTS


class SchedulerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-scheduler-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={
            "admin": {"enabled": True, "command": [sys.executable]},
            "scheduler": {**SCHEDULER_DEFAULTS, "enabled": True, "poll_seconds": 1}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.first = self.r.feedback(self.p, "chat", "First independent observation")

    def job(self):
        return dict(self.r.db.execute("SELECT * FROM scheduler_jobs WHERE project=? AND kind='admin'", (self.p,)).fetchone())

    def test_model_switch_ignores_old_executors_circuit(self):
        old_plan = self.r.resolve_admin_executor(self.p, "chat")
        def fail(runtime, project, **kwargs):
            for _ in range(3):
                record_executor_failure(runtime, project, old_plan, "executor_execution_failed")
            return {"status": "batch_complete", "stop_reason": "failed", "invocations": 1, "scanned_count": 1}
        with patch("agent_learning.admin.admin_batch", side_effect=fail):
            self.r.scheduler_tick(self.p)
        self.assertGreater(self.job()["next_due"], time.time() + 890)
        self.r.admin_configure(self.p, True, "host", "fixed", "healthy-model", expected_policy_version=0,
                               expected_epoch=self.r.session_epoch())
        self.assertIsNone(executor_backoff(self.r, self.p, self.r.resolve_admin_executor(self.p, "chat")))
        with patch("agent_learning.admin.admin_batch", return_value={"status": "batch_complete", "stop_reason": "idle"}) as batch:
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 1)
        batch.assert_called_once()
        self.assertIsNotNone(executor_backoff(self.r, self.p, old_plan))

    def test_budget_increase_after_last_slot_failure_rechecks_current_conditions(self):
        configure_project_budget(self.r, self.p, {"max_calls_per_day": 1}, expected_version=0)
        old_plan = self.r.resolve_admin_executor(self.p, "chat")
        def fail(runtime, project, **kwargs):
            with runtime.transaction():
                call = claim_call(runtime, project, self.first["id"], self.first["revision"], "controlled-failure", runtime.host_epoch(), 10)
            mark_launch_intent(runtime, call["call_id"], call["owner"], runtime.host_epoch())
            finish_call(runtime, call["call_id"], "failed")
            record_executor_failure(runtime, project, old_plan, "executor_execution_failed")
            runtime.feedback(project, "chat", "Another observation remains pending")
            return {"status": "batch_complete", "stop_reason": "failed", "invocations": 1, "scanned_count": 1}
        with patch("agent_learning.admin.admin_batch", side_effect=fail):
            self.r.scheduler_tick(self.p)
        self.assertEqual(self.job()["wait_reason"], "budget_exhausted")
        configure_project_budget(self.r, self.p, {"max_calls_per_day": 10}, expected_version=1)
        self.r.db.execute("UPDATE admin_executor_failures SET next_try=0")
        with patch("agent_learning.admin.admin_batch", return_value={"status": "batch_complete", "stop_reason": "idle"}) as batch:
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 1)
        batch.assert_called_once()
        self.assertEqual(admin_project_budget_status(self.r, self.p)["remaining_calls"], 9)

    def test_unattributed_scheduler_exception_uses_ordinary_short_retry(self):
        with patch("agent_learning.admin.admin_batch", side_effect=OSError("PRIVATE")) as batch:
            self.r.scheduler_tick(self.p)
            self.r.scheduler_tick(self.p)
        batch.assert_called_once()
        self.assertEqual(self.job()["wait_reason"], "retry_backoff")
        self.assertLess(self.job()["next_due"], time.time() + 31)

    def test_project_run_deadline_is_shared_across_ticks_and_can_only_shrink(self):
        configure_project_budget(self.r, self.p, {"max_run_seconds": 5}, expected_version=0)
        clock = {"value": 1000.0}
        deadlines = []
        def batch(runtime, project, **kwargs):
            deadlines.append(kwargs["_deadline"])
            clock["value"] += 2
            return {"status": "batch_complete", "stop_reason": "call_limit", "invocations": 1, "scanned_count": 1}
        def sleep(seconds):
            clock["value"] += seconds
        with patch("time.monotonic", side_effect=lambda: clock["value"]), \
                patch("time.time", side_effect=lambda: 1700000000 + clock["value"]), \
                patch("time.sleep", side_effect=sleep), patch("agent_learning.admin.admin_batch", side_effect=batch):
            result = self.r.scheduler_run(self.p, max_seconds=20)
        self.assertEqual(deadlines, [1005.0, 1005.0])
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["admin_run_exhausted_projects"], [self.p])
        self.assertEqual(self.job()["wait_reason"], "run_time_exhausted")
        # A later explicit runner gets a fresh finite allowance; the prior run
        # did not disable the project or discard the pending case.
        with patch("agent_learning.admin.admin_batch", return_value={"status": "batch_complete", "stop_reason": "idle"}) as batch:
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 1)
        batch.assert_called_once()

    def test_run_limit_shared_between_host_scopes_and_does_not_extend(self):
        other = Runtime(self.root / "home", host="antigravity")
        self.addCleanup(other.close)
        limits = {}
        configure_project_budget(self.r, self.p, {"max_run_seconds": 10}, expected_version=0)
        with patch("time.monotonic", return_value=100):
            first = self.r._scheduler_admin_run_deadline(self.p, "codex:default", limits)
        configure_project_budget(self.r, self.p, {"max_run_seconds": 20}, expected_version=1)
        with patch("time.monotonic", return_value=103):
            switched = self.r._scheduler_admin_run_deadline(self.p, "antigravity:default", limits)
        self.assertEqual((first, switched), (110, 110))
        configure_project_budget(self.r, self.p, {"max_run_seconds": 5}, expected_version=2)
        with patch("time.monotonic", return_value=104):
            self.assertEqual(self.r._scheduler_admin_run_deadline(self.p, "antigravity:default", limits), 105)

    def test_exhausted_admin_does_not_stop_release_or_other_projects(self):
        from tests.test_module8_scheduler import Module8SchedulerTests
        helper = Module8SchedulerTests()
        helper.r, helper.p = self.r, self.p
        helper.make_knowledge()
        self.r.config["scheduler"].update(auto_release=True, max_jobs_per_tick=1)
        other_workspace = self.root / "other"
        other_workspace.mkdir()
        other_project = self.r.project(other_workspace)
        self.r.feedback(other_project, "other-chat", "Other project observation")
        token = self.r.claim_scheduler_worker()
        self.r._scheduler_start_worker(token, 30)
        self.addCleanup(self.r.release_scheduler_worker, token)
        limits = {self.p: {"started": time.monotonic() - 20, "deadline": time.monotonic() - 10}}
        with patch("agent_learning.admin.admin_batch", return_value={"status": "batch_complete", "stop_reason": "idle"}) as batch, \
                patch.object(self.r, "daily_knowledge_release", return_value={"status": "released", "processed": 1}) as release:
            first = self.r._scheduler_tick(None, 3, time.monotonic() + 20, token, self.r.host_epoch(), _admin_run_limits=limits)
        self.assertTrue(release.called)
        self.assertTrue(any(call.args[1] == other_project for call in batch.call_args_list))
        self.assertFalse(any(call.args[1] == self.p for call in batch.call_args_list))
        self.assertIn(self.p, first["admin_run_exhausted_projects"])


class PrelaunchStateChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-prelaunch-change-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": True, "command": [sys.executable]}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.feedback(self.p, "chat", "Known local observation")

    def test_disable_before_launch_is_free_and_does_not_punish_executor(self):
        def changed(plan):
            self.r.config["admin"]["enabled"] = False
            return {"ready": True}
        with patch("agent_learning.admin_providers.executor_capability", side_effect=changed), \
                patch("agent_learning.admin.subprocess.Popen") as popen:
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "not_started")
        self.assertEqual(result["usage"]["execution"]["reason_code"], "review_context_changed")
        popen.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_executor_failures").fetchone()[0], 0)
        self.assertEqual(admin_project_budget_status(self.r, self.p)["used_calls"], 0)

    def test_account_transition_before_launch_keeps_known_not_started_free(self):
        def changed(plan):
            self.r.account_changed("prelaunch-state-test")
            return {"ready": True}
        with patch("agent_learning.admin_providers.executor_capability", side_effect=changed), \
                patch("agent_learning.admin.subprocess.Popen") as popen:
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "not_started")
        self.assertEqual(result["usage"]["execution"]["reason_code"], "review_context_changed")
        popen.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_executor_failures").fetchone()[0], 0)
        self.assertEqual(admin_project_budget_status(self.r, self.p)["used_calls"], 0)

    def test_direct_batch_deadline_respects_project_run_limit(self):
        configure_project_budget(self.r, self.p, {"max_batch_seconds": 60, "max_run_seconds": 2}, expected_version=0)
        started = time.monotonic()
        with patch("agent_learning.admin.admin_once", return_value={"status": "idle", "scanned_count": 0}) as once:
            admin_batch(self.r, self.p)
        self.assertLessEqual(once.call_args.kwargs["_deadline"], started + 2.1)

    def test_direct_once_timeout_respects_project_run_limit(self):
        from tests.test_module6_queue import fake_reviewer
        configure_project_budget(self.r, self.p, {"timeout_seconds": 30, "max_run_seconds": 2}, expected_version=0)
        observed = []
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer(callback=lambda packet, process: observed.append(process.timeout))):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "skip")
        self.assertEqual(observed, [2])

    def test_summary_disabled_between_reservation_and_launch_is_local_state_change(self):
        self.r.config["work_memory"]["summary_enabled"] = True
        self.r.db.execute("UPDATE problems SET status='reviewed' WHERE project=?", (self.p,))
        for index in range(2):
            self.r.handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "chat", "cwd": str(self.workspace),
                                "turn_id": "summary-source-" + str(index), "prompt": "Visible request " + str(index)})
        summary = self.r._work_queue_summary(self.p, "chat", self.workspace)
        self.assertEqual(summary["status"], "pending")
        def changed(plan):
            self.r.configure_work_summary(self.p, False)
            return {"ready": True}
        with patch("agent_learning.admin_providers.executor_capability", side_effect=changed), \
                patch("agent_learning.admin.subprocess.Popen") as popen:
            result = admin_once(self.r, self.p, summary["problem_id"])
        self.assertEqual(result["status"], "not_started")
        self.assertEqual(result["usage"]["execution"]["reason_code"], "review_context_changed")
        popen.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_executor_failures").fetchone()[0], 0)
        self.assertEqual(admin_project_budget_status(self.r, self.p)["used_calls"], 0)


if __name__ == "__main__":
    unittest.main()
