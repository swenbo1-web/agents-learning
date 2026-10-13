"""持久调度与派发边界；真实 SQLite、内存任务，无模型或网络调用。"""
from pathlib import Path
import tempfile
import sys
import time
import unittest
from unittest.mock import patch

from agent_learning.runtime import ContractError, Runtime
from agent_learning.scheduler import SchedulerMixin, SCHEDULER_DEFAULTS, validate_scheduler_config


SchedulerRuntime = Runtime if issubclass(Runtime, SchedulerMixin) else type(
    "SchedulerRuntime", (SchedulerMixin, Runtime), {})


class Module8SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-module8-scheduler-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = SchedulerRuntime(Path(self.temp.name) / "state")
        self.addCleanup(self.r.close)
        with self.r.transaction():
            self.r._migrate_scheduler_storage()
        self.p = self.r.project(self.workspace)
        self.r.config["scheduler"] = {**SCHEDULER_DEFAULTS, "enabled": True, "poll_seconds": 1}
        self.r.config["admin"].update(enabled=True, command=[sys.executable], max_calls_per_day=100)
        self.r.task_update(self.p, "chat", "scheduler test")

    def problem(self, summary="independent compiler error", project=None):
        return self.r.feedback(project or self.p, "chat" if project is None or project == self.p else "other-chat", summary, category="operation")

    def job(self, kind="admin", project=None):
        return dict(self.r.db.execute("SELECT * FROM scheduler_jobs WHERE project=? AND kind=?", (project or self.p, kind)).fetchone())

    def finish_problems(self, runtime, project, **kwargs):
        self.assertFalse(runtime.db.in_transaction)
        self.assertGreater(kwargs["_deadline"], time.monotonic())
        runtime.db.execute("UPDATE problems SET status='resolved' WHERE project=?", (project,))
        return {"status": "batch_complete", "scanned_count": 1, "invocations": 1, "stop_reason": "idle", "items": [{"secret": "not persisted"}]}

    def make_knowledge(self):
        problem = self.problem()
        refs = [r[0] for r in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="Compiler environment", content="Check compiler environment", category="operation",
            applicability="Same compiler", limitations="Reported observation", evidence_refs=refs, validation_level="reported")
        return self.r.publish(self.p, candidate["candidate_id"])

    def test_installation_requires_transaction_and_is_idempotent(self):
        with self.assertRaises(ContractError):
            self.r._migrate_scheduler_storage()
        version = self.r.db.execute("PRAGMA user_version").fetchone()[0]
        with self.r.transaction():
            self.r._migrate_scheduler_storage()
        self.assertEqual(version, self.r.db.execute("PRAGMA user_version").fetchone()[0])

    def test_default_disabled_never_starts_work(self):
        self.problem()
        self.r.config["scheduler"]["enabled"] = False
        with patch("agent_learning.admin.admin_batch") as work:
            self.assertFalse(self.r.scheduler_wake(self.p))
            self.assertEqual(self.r.scheduler_tick()["status"], "disabled")
            self.assertEqual(self.r.scheduler_run()["status"], "disabled")
            self.assertIsNone(self.r.claim_scheduler_worker())
        work.assert_not_called()

    def test_config_rejects_bool_nan_unknown_and_unbounded_values(self):
        for values in ({"max_jobs_per_tick": True}, {"max_sync_seconds": float("nan")},
                       {"enabled": "yes"}, {"max_run_seconds": 3601}, {"unexpected": 1},
                       {"max_release_items": 5001}):
            with self.subTest(values=values), self.assertRaises(ContractError):
                validate_scheduler_config(values)

    def test_api_rejects_unknown_project_and_invalid_time(self):
        for value in (0, -1, True, float("nan"), float("inf"), 601):
            with self.subTest(value=value), self.assertRaises(ContractError):
                self.r.scheduler_tick(max_seconds=value)
        with self.assertRaises(ContractError):
            self.r.scheduler_status("missing")
        with self.assertRaises(ContractError):
            self.r.scheduler_tick(max_jobs=True)

    def test_wake_merges_without_model_invocation(self):
        self.problem()
        with patch("agent_learning.admin.admin_batch") as work:
            self.assertTrue(self.r.scheduler_wake(self.p))
            self.assertTrue(self.r.scheduler_wake(self.p))
        work.assert_not_called()
        self.assertEqual(self.r.scheduler_status(self.p)["total_jobs"], 3)
        self.assertEqual(self.job()["state"], "ready")

    def test_global_worker_claim_and_release_are_owner_bound(self):
        token = self.r.claim_scheduler_worker()
        self.assertTrue(token)
        self.assertIsNone(self.r.claim_scheduler_worker())
        self.r.release_scheduler_worker("foreign")
        self.assertIsNone(self.r.claim_scheduler_worker())
        self.r.release_scheduler_worker(token)
        self.assertTrue(self.r.claim_scheduler_worker())

    def test_duplicate_runner_cannot_release_active_owner(self):
        token = self.r.claim_scheduler_worker()
        self.r._scheduler_start_worker(token, 30)
        with self.assertRaises(ContractError):
            self.r.scheduler_run(worker_token=token)
        self.assertIsNotNone(self.r.db.execute("SELECT token FROM scheduler_worker WHERE token=?", (token,)).fetchone())

    def test_disabled_late_runner_does_not_release_active_owner(self):
        token = self.r.claim_scheduler_worker()
        self.r._scheduler_start_worker(token, 30)
        self.r.config["scheduler"]["enabled"] = False
        self.assertEqual(self.r.scheduler_run(worker_token=token)["status"], "disabled")
        self.assertIsNotNone(self.r.db.execute("SELECT 1 FROM scheduler_worker WHERE token=? AND state='running'", (token,)).fetchone())

    def test_disabled_late_runner_releases_only_its_reserved_token(self):
        token = self.r.claim_scheduler_worker()
        self.r.config["scheduler"]["enabled"] = False
        self.r.scheduler_run(worker_token="foreign")
        self.assertIsNotNone(self.r.db.execute("SELECT 1 FROM scheduler_worker WHERE token=?", (token,)).fetchone())
        self.assertEqual(self.r.scheduler_run(worker_token=token)["status"], "disabled")
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM scheduler_worker WHERE token=?", (token,)).fetchone())

    def test_expired_worker_recovered_without_old_release_deleting_new(self):
        token = self.r.claim_scheduler_worker()
        self.r.db.execute("UPDATE scheduler_worker SET lease_until=0")
        replacement = self.r.claim_scheduler_worker()
        self.assertNotEqual(token, replacement)
        self.r.release_scheduler_worker(token)
        self.assertIsNotNone(self.r.db.execute("SELECT 1 FROM scheduler_worker WHERE token=?", (replacement,)).fetchone())

    def test_tick_runs_model_work_outside_transaction_and_bounds_summary(self):
        self.problem()
        with patch("agent_learning.admin.admin_batch", side_effect=self.finish_problems) as work:
            result = self.r.scheduler_tick(self.p)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(work.call_count, 1)
        self.assertEqual(self.job()["state"], "idle")
        self.assertNotIn("secret", self.job()["summary"])
        self.assertNotIn("items", self.job()["summary"])
        self.assertIsNone(self.r.scheduler_status()["worker"])

    def test_pending_increment_survives_batch_limit_and_second_tick(self):
        first, second = self.problem("first error"), self.problem("second error")
        def partial(runtime, project, **kwargs):
            runtime.db.execute("UPDATE problems SET status='resolved' WHERE id=?", (first["id"],))
            return {"status": "batch_complete", "invocations": 1, "stop_reason": "call_limit"}
        with patch("agent_learning.admin.admin_batch", side_effect=partial):
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 1)
        self.assertEqual(self.job()["state"], "ready")
        self.assertGreater(self.job()["next_due"], time.time())
        with patch("agent_learning.admin.admin_batch") as early:
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 0)
        early.assert_not_called()
        self.r.db.execute("UPDATE scheduler_jobs SET next_due=0 WHERE kind='admin'")
        # 增量检查可以重新发现到期的剩余任务。
        with patch("agent_learning.admin.admin_batch", side_effect=self.finish_problems):
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 1)
        self.assertEqual(self.r.db.execute("SELECT status FROM problems WHERE id=?", (second["id"],)).fetchone()[0], "resolved")

    def test_global_daily_wait_records_reset_time_without_call(self):
        self.problem()
        self.r.config["admin"]["max_calls_per_day"] = 0
        self.assertFalse(self.r.scheduler_wake(self.p))
        self.assertEqual(self.job()["wait_reason"], "budget_exhausted")
        self.assertGreater(self.job()["next_due"], time.time())
        with patch("agent_learning.admin.admin_batch") as work:
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 0)
        work.assert_not_called()

    def test_sufficient_new_budget_wakes_without_case_change(self):
        self.problem()
        self.r.config["admin"]["max_calls_per_day"] = 0
        self.r.scheduler_wake(self.p)
        self.r.config["admin"]["max_calls_per_day"] = 100
        with patch("agent_learning.admin.admin_batch", side_effect=self.finish_problems):
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 1)

    def test_failed_launch_is_backed_off_then_retried(self):
        self.problem()
        with patch("agent_learning.admin.admin_batch", side_effect=OSError("private text")) as work:
            self.r.scheduler_tick(self.p)
            self.r.scheduler_tick(self.p)
        self.assertEqual(work.call_count, 1)
        self.assertEqual(self.job()["wait_reason"], "retry_backoff")
        self.assertNotIn("private text", self.job()["summary"])
        self.r.db.execute("UPDATE scheduler_jobs SET next_due=0 WHERE kind='admin'")
        with patch("agent_learning.admin.admin_batch", side_effect=self.finish_problems):
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 1)

    def test_new_evidence_bypasses_old_backoff(self):
        self.problem()
        with patch("agent_learning.admin.admin_batch", side_effect=OSError):
            self.r.scheduler_tick(self.p)
        self.problem("changed material")
        with patch("agent_learning.admin.admin_batch", side_effect=self.finish_problems):
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 1)

    def test_unknown_call_does_not_retry_without_explicit_eligibility(self):
        problem = self.problem()
        self.r.db.execute("INSERT INTO admin_calls(id,project,problem,revision,day,state,created) "
            "VALUES('unknown',?,?,?,'2026-10-11','timeout_unknown',?)", (self.p, problem["id"], problem["revision"], time.time()))
        with patch("agent_learning.admin.admin_batch") as work:
            self.r.scheduler_wake(self.p)
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 0)
        work.assert_not_called()
        self.assertEqual(self.job()["wait_reason"], "review_required")
        self.r.db.execute("UPDATE admin_calls SET retry_allowed=1 WHERE id='unknown'")
        self.assertTrue(self.r.scheduler_wake(self.p))

    def test_account_change_invalidates_worker_and_old_epoch(self):
        self.problem()
        token = self.r.claim_scheduler_worker()
        self.r.account_changed("scheduler-account-change")
        with self.assertRaises(ContractError):
            self.r.scheduler_run(worker_token=token)
        self.assertTrue(self.r.claim_scheduler_worker())
        with self.assertRaises(ContractError):
            self.r.scheduler_tick(expected_epoch=0)

    def test_two_projects_rotate_after_single_job_ticks(self):
        self.problem()
        other_workspace = Path(self.temp.name) / "other"
        other_workspace.mkdir()
        other = self.r.project(other_workspace)
        self.r.task_update(other, "other-chat", "other task")
        self.problem("other compiler error", project=other)
        with patch("agent_learning.admin.admin_batch", return_value={"status": "batch_complete", "stop_reason": "call_limit"}):
            first = self.r.scheduler_tick(max_jobs=1)
            second = self.r.scheduler_tick(max_jobs=1)
        self.assertNotEqual(first["items"][0]["project_id"], second["items"][0]["project_id"])

    def test_release_requires_explicit_scheduler_flag_and_does_not_govern(self):
        self.make_knowledge()
        self.r.config["admin"]["enabled"] = False
        with patch.object(self.r, "daily_knowledge_release", wraps=self.r.daily_knowledge_release) as publish:
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 0)
            self.r.config["scheduler"]["auto_release"] = True
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 1)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(self.job("release")["wait_reason"], "no_release_increment")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active WHERE project=?", (self.p,)).fetchone()[0], 1)
        self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 0)

    def test_release_interval_defers_new_knowledge(self):
        knowledge = self.make_knowledge()
        self.r.config["admin"]["enabled"] = False
        self.r.config["scheduler"]["auto_release"] = True
        self.r.scheduler_tick(self.p)
        self.r.revoke(self.p, knowledge["knowledge_id"], expected_revision=knowledge["revision"], reason="test revoke")
        self.r.scheduler_wake(self.p)
        self.assertEqual(self.job("release")["wait_reason"], "release_interval")
        self.assertGreater(self.job("release")["next_due"], time.time())

    def test_sync_has_two_enable_gates_and_reports_dirty_count(self):
        self.make_knowledge()
        self.r.config["admin"]["enabled"] = False
        self.assertGreater(self.r.scheduler_status()["sync_dirty_count"], 0)
        self.r.config["scheduler"]["sync_enabled"] = True
        with patch.object(self.r, "sync_batch") as sync:
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 0)
        sync.assert_not_called()
        self.r.config["hindsight"]["enabled"] = True
        with patch.object(self.r, "sync_batch", return_value={"status": "completed", "processed": 1}) as sync:
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 1)
        self.assertEqual(sync.call_count, 1)

    def test_blocked_unknown_sync_waits_for_explicit_reconciliation(self):
        self.r.config["admin"]["enabled"] = False
        self.r.config["hindsight"]["enabled"] = True
        self.r.config["scheduler"]["sync_enabled"] = True
        self.r.db.execute("INSERT INTO outbox(id,project,kind,payload,state,write_outcome,attempts) "
                          "VALUES('unknown-sync',?,'published_sync','{}','blocked','unknown',1)", (self.p,))
        with patch.object(self.r, "sync_batch") as sync:
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 0)
        sync.assert_not_called()
        self.assertEqual(self.job("sync")["wait_reason"], "sync_review_required")

    def test_run_reuses_reserved_worker_and_exits_with_future_wait_persisted(self):
        self.problem()
        self.r.config["admin"]["max_calls_per_day"] = 0
        token = self.r.claim_scheduler_worker()
        started = time.monotonic()
        result = self.r.scheduler_run(self.p, max_seconds=1, worker_token=token)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(result["processed"], 0)
        self.assertGreater(self.job()["next_due"], time.time())
        self.assertIsNone(self.r.scheduler_status()["worker"])

    def test_run_continues_pending_batch_without_new_hook(self):
        first = self.problem("first runner case")
        self.problem("second runner case")
        calls = []
        def review(runtime, project, **kwargs):
            calls.append(project)
            if len(calls) == 1:
                runtime.db.execute("UPDATE problems SET status='resolved' WHERE id=?", (first["id"],))
                return {"status": "batch_complete", "invocations": 1, "stop_reason": "call_limit"}
            return self.finish_problems(runtime, project, **kwargs)
        with patch("agent_learning.admin.admin_batch", side_effect=review):
            result = self.r.scheduler_run(self.p, max_seconds=3)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["processed"], 2)

    def test_real_admin_queue_preflight_avoids_repeated_unchanged_review(self):
        from tests.test_module6_queue import fake_reviewer
        self.problem("real deterministic queue case")
        self.r.config["admin"]["max_calls_per_batch"] = 1
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("skip")):
            first = self.r.scheduler_tick(self.p)
        self.assertEqual(first["processed"], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls WHERE state='skip'").fetchone()[0], 1)
        with patch("agent_learning.admin.subprocess.Popen") as reviewer:
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 0)
        reviewer.assert_not_called()
        self.assertEqual(self.job()["wait_reason"], "no_review_increment")

    def test_real_three_case_two_call_batch_continues_without_new_hook(self):
        from tests.test_module6_queue import fake_reviewer
        for summary in ("compiler alpha error", "filesystem beta permission", "network gamma timeout"):
            self.problem(summary)
        self.r.config["admin"]["max_calls_per_batch"] = 2
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("skip")):
            result = self.r.scheduler_run(self.p, max_seconds=4)
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["ticks"], 2)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls WHERE state='skip'").fetchone()[0], 3)
        self.assertEqual(self.job()["state"], "idle")

    def test_real_admin_needs_evidence_stays_waiting_until_material_changes(self):
        from tests.test_module6_queue import fake_reviewer
        self.problem("real missing evidence case")
        self.r.config["admin"]["max_calls_per_batch"] = 1
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("needs_evidence")):
            self.r.scheduler_tick(self.p)
        self.assertEqual(self.job()["state"], "waiting")
        self.assertEqual(self.job()["wait_reason"], "awaiting_evidence")
        self.assertIsNone(self.job()["next_due"])
        with patch("agent_learning.admin.subprocess.Popen") as reviewer:
            self.assertEqual(self.r.scheduler_tick(self.p)["processed"], 0)
        reviewer.assert_not_called()

    def test_program_release_precedes_paid_admin_for_same_project(self):
        self.make_knowledge()
        self.problem("still waiting for administrator")
        self.r.config["scheduler"]["auto_release"] = True
        with patch("agent_learning.admin.admin_batch") as work:
            result = self.r.scheduler_tick(self.p, max_jobs=1)
        work.assert_not_called()
        self.assertEqual(result["items"][0]["kind"], "release")

    def test_reserved_worker_start_failure_releases_only_reserved_token(self):
        token = self.r.claim_scheduler_worker()
        with patch.object(self.r, "_scheduler_start_worker", side_effect=ContractError("controlled startup failure")):
            with self.assertRaises(ContractError):
                self.r.scheduler_run(worker_token=token)
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM scheduler_worker WHERE token=?", (token,)).fetchone())

    def test_account_change_during_work_rejects_late_scheduler_receipt(self):
        self.problem()
        def switch(runtime, project, **kwargs):
            runtime.account_changed("during-work")
            return {"status": "batch_complete", "invocations": 1}
        with patch("agent_learning.admin.admin_batch", side_effect=switch), self.assertRaises(ContractError):
            self.r.scheduler_tick(self.p)
        self.assertEqual(self.job()["summary"], "{}")
        self.r.config["admin"]["enabled"] = False
        self.r.scheduler_wake(self.p)
        self.assertEqual(self.job()["state"], "disabled")


if __name__ == "__main__":
    unittest.main()
