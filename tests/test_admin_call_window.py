"""Review admission must preserve a full call window without paid model calls."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_batch, admin_once
from agent_learning.admin_queue import claim_dispatch
from agent_learning.admin_usage import admin_project_budget_status, configure_project_budget
from agent_learning.runtime import ContractError, Runtime
from tests.test_module6_queue import fake_reviewer


class AdminCallWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-call-window-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={"admin": {
            "enabled": True, "command": [sys.executable], "timeout_seconds": 120,
            "max_calls_per_day": 100, "max_calls_per_batch": 5, "max_batch_seconds": 180}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.feedback(self.p, "chat", "A reusable observation")

    def test_four_second_tail_never_reserves_or_launches_in_any_host(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                r = Runtime(self.root / host, host=host, config={"admin": {
                    "enabled": True, "command": [sys.executable], "timeout_seconds": 120}})
                try:
                    p = r.project(self.workspace)
                    r.feedback(p, r.host_conversation("chat"), "An independent observation")
                    with patch("agent_learning.admin.subprocess.Popen") as launch:
                        result = admin_once(r, p, _deadline=time.monotonic() + 4)
                    self.assertEqual(result["status"], "batch_time_exhausted")
                    self.assertEqual(result["reason"], "insufficient_call_window")
                    self.assertEqual(result["required_seconds"], 120)
                    self.assertLessEqual(result["remaining_seconds"], 4)
                    self.assertEqual(r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
                    self.assertEqual(admin_project_budget_status(r, p)["used_calls"], 0)
                    launch.assert_not_called()
                finally:
                    r.close()

    def test_setup_consuming_the_window_is_free_and_later_resumes(self):
        clock = {"now": 0.0}
        def prepare(plan):
            clock["now"] = 10.0
            return {"ready": True}
        with patch("agent_learning.admin.time.monotonic", side_effect=lambda: clock["now"]), \
                patch("agent_learning.admin_providers.executor_capability", side_effect=prepare), \
                patch("agent_learning.admin.subprocess.Popen") as launch:
            result = admin_once(self.r, self.p, _deadline=125)
        launch.assert_not_called()
        self.assertEqual(result["status"], "batch_time_exhausted")
        row = self.r.db.execute("SELECT state,started,retry_allowed FROM admin_calls").fetchone()
        self.assertEqual(tuple(row), ("not_started", None, 0))
        self.assertFalse(result["usage"]["execution"]["input_submission_attempted"])
        self.assertEqual(admin_project_budget_status(self.r, self.p)["used_calls"], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_executor_failures").fetchone()[0], 0)
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer()):
            completed = admin_once(self.r, self.p, _deadline=time.monotonic() + 180)
        self.assertEqual(completed["status"], "skip")
        self.assertEqual(admin_project_budget_status(self.r, self.p)["used_calls"], 1)

    def test_batch_defers_its_tail_and_releases_dispatch(self):
        self.r.feedback(self.p, "chat", "Another independent observation")
        clock = {"now": 0.0}
        def advance(packet, process):
            self.assertGreater(process.timeout, 119)
            clock["now"] = 176.0
        with patch("agent_learning.admin.time.monotonic", side_effect=lambda: clock["now"]):
            token = claim_dispatch(self.r, self.p)
            with patch("agent_learning.admin.subprocess.Popen", fake_reviewer(callback=advance)):
                result = admin_batch(self.r, self.p, dispatch_token=token)
        self.assertEqual(result["invocations"], 1)
        self.assertEqual(result["stop_reason"], "batch_time_exhausted")
        self.assertEqual(result["reason"], "insufficient_call_window")
        self.assertEqual(result["remaining_seconds"], 4)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_dispatch").fetchone()[0], 0)
        self.assertEqual(admin_project_budget_status(self.r, self.p)["used_calls"], 1)

    def test_invalid_deadline_is_rejected_before_reservation(self):
        for value in (True, float("nan"), float("inf"), "later"):
            with self.subTest(value=value), self.assertRaises(ContractError):
                admin_once(self.r, self.p, _deadline=value)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_explicit_project_timeout_can_fit_a_short_window(self):
        configure_project_budget(self.r, self.p, {"timeout_seconds": 2}, expected_version=0)
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer()):
            result = admin_once(self.r, self.p, _deadline=time.monotonic() + 4)
        self.assertEqual(result["status"], "skip")
        self.assertEqual(admin_project_budget_status(self.r, self.p)["used_calls"], 1)

    def test_history_deferrals_preserve_allowance_before_and_after_preparation(self):
        (self.workspace / "README.md").write_text("Public project notes for a frozen history fixture.", encoding="utf-8")
        job = self.r.history_prepare(self.p, self.workspace)["job_id"]
        self.r.history_run(self.p, job, allow_review=False)
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            self.r.history_run(self.p, job, allow_review=True, max_calls=2, max_seconds=4)
        launch.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
        clock = {"now": 0.0}
        def prepare(plan):
            clock["now"] = 10.0
            return {"ready": True}
        with patch("agent_learning.admin.time.monotonic", side_effect=lambda: clock["now"]), \
                patch("agent_learning.admin_providers.executor_capability", side_effect=prepare), \
                patch("agent_learning.admin.subprocess.Popen") as launch:
            self.r.history_run(self.p, job, allow_review=True, max_calls=2, max_seconds=125)
        launch.assert_not_called()
        row = self.r.db.execute("SELECT state,remaining_calls,last_result FROM history_jobs WHERE id=?", (job,)).fetchone()
        self.assertEqual(row["state"], "paused")
        self.assertEqual(row["remaining_calls"], 2)
        self.assertEqual(json.loads(row["last_result"])["reason"], "batch_time_exhausted")
        self.assertEqual(admin_project_budget_status(self.r, self.p)["used_calls"], 0)

    def test_scheduler_defers_once_and_a_fresh_runner_can_resume(self):
        self.r.config["scheduler"].update(enabled=True, max_run_seconds=180, max_tick_seconds=180)
        with patch("agent_learning.admin.admin_batch", return_value={
                "status": "batch_complete", "scanned_count": 0, "invocations": 0,
                "stop_reason": "batch_time_exhausted", "reason": "insufficient_call_window",
                "required_seconds": 120, "remaining_seconds": 4}) as batch:
            result = self.r.scheduler_run(self.p)
        self.assertEqual(batch.call_count, 1)
        self.assertEqual(result["ticks"], 1)
        self.assertIn(self.p, result["admin_run_exhausted_projects"])
        row = self.r.db.execute("SELECT wait_reason,summary FROM scheduler_jobs WHERE project=? AND kind='admin'", (self.p,)).fetchone()
        self.assertEqual(row["wait_reason"], "insufficient_call_window")
        self.assertEqual(json.loads(row["summary"])["required_seconds"], 120)
        self.r.db.execute("UPDATE scheduler_jobs SET next_due=0")
        def finish(runtime, project, **kwargs):
            runtime.db.execute("UPDATE problems SET status='reviewed' WHERE project=?", (project,))
            return {"status": "batch_complete", "scanned_count": 1, "invocations": 1, "stop_reason": "idle"}
        with patch("agent_learning.admin.admin_batch", side_effect=finish) as resumed:
            next_run = self.r.scheduler_run(self.p)
        self.assertEqual(resumed.call_count, 1)
        self.assertEqual(next_run["processed"], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM scheduler_worker").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
