"""Token-sized batches and finite continuation preserve unreviewed material."""
import json
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_usage import admin_project_budget_status, estimate_reservation
from agent_learning.phase_review import configure, instruction, policy, status
from tests import test_phase_review as fixtures


class PhaseBudgetCoverageTests(unittest.TestCase):
    setUp = fixtures.PhaseReviewTests.setUp
    phase = fixtures.PhaseReviewTests.phase

    def test_small_token_input_never_creates_an_unlaunchable_queued_batch(self):
        configure(self.r, self.p, {"max_input_tokens": 16000}, policy(self.r, self.p)["version"])
        self.phase("long-one", "项目设计资料" * 600)
        self.phase("long-two", "另一份阶段资料" * 600)
        with patch("agent_learning.admin.subprocess.Popen") as model:
            result = admin_once(self.r, self.p)
        self.assertEqual(model.call_count, 0)
        self.assertNotEqual(result["status"], "token_budget_exhausted", result)
        self.assertEqual(status(self.r, self.p)["phases"].get("partial"), 2)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM phase_review_batches WHERE state='pending'").fetchone()[0], 0)

    def test_large_member_cannot_displace_small_ready_sibling(self):
        configure(self.r, self.p, {"max_input_tokens": 20000}, policy(self.r, self.p)["version"])
        self.phase("long", "工程限制和需求" * 600)
        self.phase("short", "Small sourced project fact")
        row = self.r.db.execute("SELECT packet FROM phase_review_batches WHERE state='pending'").fetchone()
        self.assertIsNotNone(row)
        body = json.loads(row[0])
        self.assertFalse(body["coverage_complete"])
        self.assertTrue(estimate_reservation(self.r, self.p, instruction(body), phase=True)["admitted"])
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "skip", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        self.assertGreater(status(self.r, self.p)["phases"].get("partial", 0), 0)

    def test_large_duplicate_set_continues_without_another_delivery_or_paid_call(self):
        event = self.phase("one")
        task = self.r.task(self.p, "one")
        for number in range(110):
            self.r._problem(self.p, "one", "阶段公开记录：A sourced project fact [" + format(number, "032x") + "] part 1",
                            "mixed", [event["id"]], task_binding=task)
        self.phase("two", "Other independent convention")
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip")):
            first = admin_once(self.r, self.p)
        self.assertEqual(first["status"], "skip", first)
        # All 110 duplicate rows advance locally using the accepted review. A
        # bounded first page must neither lose the tail nor require a new stage.
        self.assertEqual(status(self.r, self.p)["phases"].get("completed"), 2)
        usage = admin_project_budget_status(self.r, self.p)
        self.assertEqual(usage["counted_attempts"], 1)
        self.assertEqual(usage["completed_reviews"], 1)
        self.assertEqual(usage["active_attempts"], 0)

    def duplicate_batch(self):
        event = self.phase("one")
        task = self.r.task(self.p, "one")
        for number in range(2):
            self.r._problem(self.p, "one", "阶段公开记录：A sourced project fact [" + format(number, "032x") + "] part 1",
                            "mixed", [event["id"]], task_binding=task)
        self.phase("two", "Other independent convention")
        return self.r.db.execute("SELECT problem_id FROM phase_review_members WHERE state='duplicate_pending' LIMIT 1").fetchone()[0]

    def change_comparison(self, identity):
        self.r.db.execute("INSERT INTO admin_case_epochs(project,problem,revision) VALUES(?,?,1) "
            "ON CONFLICT(project,problem) DO UPDATE SET revision=revision+1", (self.p, identity))

    def test_duplicate_comparison_changes_while_model_runs_roll_back_every_result(self):
        duplicate = self.duplicate_batch()
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer(callback=lambda packet: self.change_comparison(duplicate))):
            result = admin_once(self.r, self.p)
        self.assertNotEqual(result["status"], "candidate", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)

    def test_duplicate_comparison_change_before_launch_replans_once(self):
        duplicate = self.duplicate_batch()
        self.change_comparison(duplicate)
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "skip", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_old_account_stage_counts_are_separate_from_current_queue(self):
        self.phase("old-account")
        self.r.scope_account_changed()
        state = status(self.r, self.p)
        self.assertEqual(state["phases"], {})
        self.assertEqual(state["historical_boundary_phases"], 1)


if __name__ == "__main__":
    unittest.main()
