"""Opt-in tail batches use existing admission, evidence and executor guards."""
import time
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.phase_review import _batch_trigger, configure, policy, status
from agent_learning.runtime import ContractError
from tests import test_phase_review as fixtures


class PhaseWaitLimitTests(unittest.TestCase):
    setUp = fixtures.PhaseReviewTests.setUp
    phase = fixtures.PhaseReviewTests.phase

    def settings(self, **values):
        return configure(self.r, self.p, values, policy(self.r, self.p)["version"])

    def age(self, seconds=120):
        stamp = time.time() - seconds
        self.r.db.execute("UPDATE review_phases SET created=?", (stamp,))

    def later(self, seconds=120):
        # Advance only the scheduler's clock; source-window timestamps must
        # remain an honest ordered history, including the original Stop.
        stamp = time.time() + seconds
        return patch("agent_learning.phase_review.time", SimpleNamespace(time=lambda: stamp))

    def test_default_keeps_the_existing_count_only_contract(self):
        self.assertEqual(policy(self.r, self.p)["max_wait_seconds"], 0)
        self.phase("tail", "New sourced project convention")
        self.age(864000)
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            admin_once(self.r, self.p)
        launch.assert_not_called()
        self.assertEqual(status(self.r, self.p)["phases"]["sealed"], 1)

    def test_opt_in_tail_flushes_only_after_its_delivery_wait(self):
        self.settings(max_wait_seconds=60)
        self.phase("tail", "New sourced project convention")
        self.age(120)
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            admin_once(self.r, self.p)
        launch.assert_not_called()  # An old task is not an old delivered source.
        with self.later(), patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "skip", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        self.assertEqual(status(self.r, self.p)["phases"]["completed"], 1)
        body = json.loads(self.r.db.execute("SELECT packet FROM phase_review_batches").fetchone()[0])
        self.assertEqual(body["_local_admission"]["trigger_reason"], "max_wait")
        from agent_learning.phase_review import instruction
        without = {key: value for key, value in body.items() if key != "_local_admission"}
        self.assertEqual(instruction(body), instruction(without))

    def test_deadline_never_resumes_an_unknown_call(self):
        import subprocess
        self.settings(max_wait_seconds=60)
        self.phase("tail", "Independent sourced project convention")
        class Timeout(fixtures.reviewer("skip")):
            def communicate(self, *args, **kwargs):
                raise subprocess.TimeoutExpired("controlled", 5)
        with self.later(), patch("agent_learning.admin.subprocess.Popen", Timeout):
            first = admin_once(self.r, self.p)
        self.assertIn(first["status"], {"timeout_unknown", "failed"}, first)
        with self.later(), patch("agent_learning.admin.subprocess.Popen") as launch:
            admin_once(self.r, self.p)
        launch.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_reading_a_due_tail_never_dispatches_or_writes(self):
        self.settings(max_wait_seconds=60)
        self.phase("tail", "Sourced project convention")
        before = self.r.db.total_changes
        with self.later(), patch("agent_learning.admin.subprocess.Popen") as launch:
            for _ in range(3):
                status(self.r, self.p)
        launch.assert_not_called()
        self.assertEqual(self.r.db.total_changes, before)

    def test_wait_setting_is_strict_and_old_clients_preserve_it(self):
        for value in (-1, 86401, True, 1.5, "60", None):
            with self.subTest(value=value), self.assertRaises(ContractError):
                self.settings(max_wait_seconds=value)
        self.settings(max_wait_seconds=60)
        self.settings(every_phases=3)  # Omission by an old client is not disable.
        self.assertEqual(policy(self.r, self.p)["max_wait_seconds"], 60)


class BatchTriggerTests(unittest.TestCase):
    def row(self, identity="a", state="sealed", stamp=10):
        return {"id": identity, "state": state, "created": stamp, "ready_since": stamp}

    def test_routine_only_never_wakes_at_any_age(self):
        self.assertIsNone(_batch_trigger([self.row(state="routine_deferred")],
                                        {"every_phases": 1, "max_wait_seconds": 1}, {"a"}, 100))

    def test_a_recent_business_stage_cannot_borrow_an_old_status_deadline(self):
        rows = [self.row("status", "routine_deferred", 0), self.row("business", stamp=99)]
        self.assertIsNone(_batch_trigger(rows, {"every_phases": 3, "max_wait_seconds": 60}, {"status"}, 100))

    def test_due_tail_is_not_hidden_by_a_continuation(self):
        rows = [self.row("old", "continuing"), self.row("tail")]
        self.assertEqual(_batch_trigger(rows, {"every_phases": 3, "max_wait_seconds": 60}, set(), 100), "max_wait")

    def test_count_trigger_still_takes_precedence_when_it_is_met(self):
        self.assertEqual(_batch_trigger([self.row()], {"every_phases": 1, "max_wait_seconds": 1}, set(), 100), "phase_count")


if __name__ == "__main__":
    unittest.main()
