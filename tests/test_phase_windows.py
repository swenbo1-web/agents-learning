"""Finite phase-review windows with no paid calls or real host settings."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_drain
from agent_learning.admin_presets import PRESETS
from agent_learning.phase_review import configure
from agent_learning.runtime import Runtime
from agent_learning.scheduler import phase_batch_rollover
from tests import test_phase_review


class PhaseWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="phase-windows-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={
            "admin": {"enabled": True, "command": [sys.executable], "timeout_seconds": 120,
                      "max_calls_per_day": 100, "max_batch_seconds": 180, "max_run_seconds": 600},
            "scheduler": {"enabled": True, "max_run_seconds": 300, "max_tick_seconds": 180,
                          "poll_seconds": 1}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        configure(self.r, self.p, {"mode": "phase", "every_phases": 1}, 0)
        test_phase_review.PhaseReviewTests.phase(self, "phase-chat", "A sourced stage convention")

    @staticmethod
    def short_batch(scanned=1):
        return {"status": "batch_complete", "stop_reason": "batch_time_exhausted",
                "reason": "insufficient_call_window", "required_seconds": 120,
                "remaining_seconds": 15, "scanned_count": scanned, "invocations": int(scanned > 0)}

    def test_all_presets_fit_default_call_timeout_without_reducing_it(self):
        for name, preset in PRESETS.items():
            with self.subTest(name=name):
                self.assertGreaterEqual(preset["max_batch_seconds"], 120)
                self.assertGreaterEqual(preset["max_run_seconds"], preset["max_batch_seconds"])
        self.assertEqual(self.r.admin_effective_config(self.p)["timeout_seconds"], 120)

    def test_rollover_is_phase_only_and_requires_progress_and_full_outer_window(self):
        with patch("time.monotonic", return_value=100):
            self.assertTrue(phase_batch_rollover(self.r, self.p, self.short_batch(), 240))
            self.assertFalse(phase_batch_rollover(self.r, self.p, self.short_batch(0), 240))
            self.assertFalse(phase_batch_rollover(self.r, self.p, self.short_batch(), 219))
            self.assertFalse(phase_batch_rollover(self.r, self.p, self.short_batch(), 240, fresh_seconds=90))
            for reason in ("failed", "timeout_unknown", "review_required", "needs_evidence", "token_budget_exhausted"):
                self.assertFalse(phase_batch_rollover(self.r, self.p,
                    {**self.short_batch(), "stop_reason": reason}, 240))
        configure(self.r, self.p, {"mode": "legacy"}, 1)
        with patch("time.monotonic", return_value=100):
            self.assertFalse(phase_batch_rollover(self.r, self.p, self.short_batch(), 240))

    def run_clocked(self, first_result, *, max_seconds=300):
        clock = {"now": 1000.0}
        deadlines = []
        def batch(runtime, project, **kwargs):
            deadlines.append(kwargs["_deadline"])
            if len(deadlines) == 1:
                clock["now"] += 165
                return first_result
            clock["now"] += 5
            runtime.db.execute("UPDATE phase_review_batches SET state='completed' WHERE project=?", (project,))
            runtime.db.execute("UPDATE problems SET status='reviewed' WHERE project=?", (project,))
            return {"status": "batch_complete", "stop_reason": "idle", "scanned_count": 1, "invocations": 1}
        def sleep(seconds):
            clock["now"] += seconds
        with patch("time.monotonic", side_effect=lambda: clock["now"]), \
                patch("time.time", side_effect=lambda: 1700000000 + clock["now"]), \
                patch("time.sleep", side_effect=sleep), \
                patch("agent_learning.admin.admin_batch", side_effect=batch):
            result = self.r.scheduler_run(self.p, max_seconds=max_seconds)
        return result, deadlines

    def test_child_tail_rolls_over_to_new_tick_within_original_run_limit(self):
        result, deadlines = self.run_clocked(self.short_batch())
        self.assertEqual(result["processed"], 2)
        self.assertEqual(deadlines, [1180.0, 1300.0])
        self.assertEqual(result["admin_run_exhausted_projects"], [])
        self.assertLess(result["elapsed_seconds"], 300)
        self.assertIsNone(self.r.scheduler_status()["worker"])

    def test_no_progress_stops_without_retry_loop(self):
        result, deadlines = self.run_clocked(self.short_batch(0))
        self.assertEqual(len(deadlines), 1)
        self.assertEqual(result["admin_run_exhausted_projects"], [self.p])

    def test_outer_window_cannot_be_extended_by_phase_rollover(self):
        result, deadlines = self.run_clocked(self.short_batch(), max_seconds=240)
        self.assertEqual(len(deadlines), 1)
        self.assertEqual(result["admin_run_exhausted_projects"], [self.p])
        self.assertLess(result["elapsed_seconds"], 240)

    def test_short_global_tick_never_launches_or_reserves_a_call(self):
        self.r.config["scheduler"]["max_tick_seconds"] = 60
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            result = self.r.scheduler_run(self.p, max_seconds=240)
        launch.assert_not_called()
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["admin_run_exhausted_projects"], [self.p])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
        row = self.r.db.execute("SELECT wait_reason FROM scheduler_jobs WHERE project=? AND kind='admin'", (self.p,)).fetchone()
        self.assertEqual(row[0], "insufficient_call_window")

    def run_drain_clocked(self, first_result, *, max_seconds=300, lock_unknown=False):
        clock = {"now": 1000.0}
        seen = []
        def batch(runtime, project, **kwargs):
            self.assertFalse(kwargs["_release_dispatch"])
            lease = runtime.db.execute("SELECT token,lease_until FROM admin_dispatch WHERE project=?", (project,)).fetchone()
            self.assertIsNotNone(lease)
            self.assertEqual(lease["token"], kwargs["dispatch_token"])
            self.assertGreater(lease["lease_until"], 1700000000 + clock["now"])
            seen.append((kwargs["dispatch_token"], kwargs["_deadline"]))
            if len(seen) == 1:
                clock["now"] += 165
                if lock_unknown:
                    from agent_learning.admin_lifecycle import claim_call, finish_call, mark_launch_intent
                    parent = runtime.db.execute("SELECT p.id,p.revision FROM phase_review_batches b "
                        "JOIN problems p ON p.id=b.problem_id WHERE b.project=? AND b.state='pending'", (project,)).fetchone()
                    with runtime.transaction():
                        call = claim_call(runtime, project, parent["id"], parent["revision"],
                                          "controlled-unknown", runtime.host_epoch(), 120, reserved_tokens=16000)
                    mark_launch_intent(runtime, call["call_id"], call["owner"], runtime.host_epoch())
                    finish_call(runtime, call["call_id"], "timeout_unknown")
                return first_result
            clock["now"] += 5
            return {"status": "batch_complete", "stop_reason": "idle", "scanned_count": 1, "invocations": 1}
        with patch("time.monotonic", side_effect=lambda: clock["now"]), \
                patch("time.time", side_effect=lambda: 1700000000 + clock["now"]), \
                patch("agent_learning.admin.admin_batch", side_effect=batch):
            result = admin_drain(self.r, self.p, max_seconds=max_seconds)
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM admin_dispatch WHERE project=?", (self.p,)).fetchone())
        return result, seen

    def test_drain_renews_child_window_under_one_original_dispatch_lease(self):
        result, seen = self.run_drain_clocked(self.short_batch())
        self.assertEqual(result["batches"], 2)
        self.assertEqual(result["invocations"], 2)
        self.assertEqual(result["stop_reason"], "idle")
        self.assertEqual(len({token for token, _ in seen}), 1)
        self.assertEqual([deadline for _, deadline in seen], [1300.0, 1300.0])
        self.assertEqual(result["elapsed_seconds"], 170)

    def test_drain_does_not_renew_after_empty_scan(self):
        result, seen = self.run_drain_clocked(self.short_batch(0))
        self.assertEqual(len(seen), 1)
        self.assertEqual(result["batches"], 1)
        self.assertEqual(result["invocations"], 0)
        self.assertEqual(result["stop_reason"], "batch_time_exhausted")

    def test_drain_unknown_result_stops_and_preserves_its_lock_and_token_reservation(self):
        result, seen = self.run_drain_clocked({"status": "batch_complete", "stop_reason": "timeout_unknown",
            "scanned_count": 1, "invocations": 1}, lock_unknown=True)
        self.assertEqual(len(seen), 1)
        self.assertEqual(result["stop_reason"], "timeout_unknown")
        call = self.r.db.execute("SELECT state,retry_allowed FROM admin_calls").fetchone()
        self.assertEqual(tuple(call), ("timeout_unknown", 0))
        self.assertEqual(self.r.db.execute("SELECT reserved_tokens FROM admin_call_budgets").fetchone()[0], 16000)

    def test_drain_stops_when_original_outer_allowance_cannot_fit_next_call(self):
        result, seen = self.run_drain_clocked(self.short_batch(), max_seconds=240)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], 1240)
        self.assertEqual(result["stop_reason"], "batch_time_exhausted")
        self.assertLess(result["elapsed_seconds"], 240)

    def test_legacy_drain_does_not_gain_phase_rollover_behavior(self):
        configure(self.r, self.p, {"mode": "legacy"}, 1)
        result, seen = self.run_drain_clocked(self.short_batch())
        self.assertEqual(len(seen), 1)
        self.assertEqual(result["stop_reason"], "batch_time_exhausted")


if __name__ == "__main__":
    unittest.main()
