"""Frozen phase admission and bounded selection agree without starting a model."""
import json
import unittest
from unittest.mock import patch

from agent_learning.admin import _select_review, admin_once
from agent_learning.admin_lifecycle import finish_call, mark_launch_intent
from agent_learning.admin_queue import (claim_dispatch, release_dispatch, record_executor_failure,
    clear_executor_failure, dispatch_diagnostic)
from agent_learning.admin_usage import (admin_admission_status, configure_project_budget,
    project_budget_policy, estimate_reservation)
from agent_learning.phase_review import configure, policy, seal, prepare, instruction
from agent_learning.scheduler import SCHEDULER_DEFAULTS
from tests import test_phase_review as fixtures


class PhaseBudgetFitTests(unittest.TestCase):
    setUp = fixtures.PhaseReviewTests.setUp

    def phase(self, conversation, extra=0):
        self.r.admin_observe_model(self.p, conversation, "model-" + conversation)
        task = self.r.task_update(self.p, conversation, "Independent contract " + conversation, new_task=True)
        self.r._event(self.p, conversation, "UserPromptSubmit", {"prompt": task["goal"]}, outcome="reported",
                      task_id=task["id"], task_revision=1)
        for number in range(extra):
            self.r.feedback(self.p, conversation, "Independent contract " + conversation + " item " + str(number), category="project")
        self.r.task_update(self.p, conversation, task["goal"], status="delivered", expected_task_id=task["id"],
                           expected_write_version=task["write_version"])
        event, _ = self.r._event(self.p, conversation, "Stop", {"last_assistant_message": "Delivered " + conversation},
                                 outcome="reported", task_id=task["id"], task_revision=1)
        with self.r.transaction():
            seal(self.r, self.p, conversation, event["id"])

    def backlog(self, count=1):
        self.r.config["admin"]["max_input_chars"] = 64000
        self.r.admin_configure(self.p, True, model_mode="current", expected_policy_version=0)
        configure(self.r, self.p, {"every_phases": 1}, policy(self.r, self.p)["version"])
        plans = []
        for number in range(count):
            conversation = "large-" + str(number)
            self.phase(conversation, extra=4)
            plans.append(self.r.resolve_admin_executor(self.p, conversation))
            record_executor_failure(self.r, self.p, plans[-1], "configuration_change_required")
        self.phase("small")
        for plan in plans:
            clear_executor_failure(self.r, self.p, plan)
        with self.r.transaction():
            prepare(self.r, self.p)
        rows = []
        for row in self.r.db.execute("SELECT problem_id,packet FROM phase_review_batches WHERE state='pending' ORDER BY created,problem_id"):
            body = json.loads(row["packet"])
            rows.append((row["problem_id"], body["executor_conversation"],
                estimate_reservation(self.r, self.p, instruction(body), phase=True)["reserved_tokens"]))
        self.assertEqual(len(rows), count + 1)
        small = next(row for row in rows if row[1] == "small")
        self.assertTrue(all(row[2] > small[2] + 100 for row in rows if row[1] != "small"))
        self.cap(small[2] + 100)
        return rows, small

    def cap(self, value):
        configure_project_budget(self.r, self.p, {"max_reported_tokens_per_day": value},
            expected_version=project_budget_policy(self.r, self.p)["version"])

    def select(self, limit=10):
        with self.r.transaction():
            return _select_review(self.r, self.p, None, limit, 5, self.r.host_epoch())

    def test_same_window_admission_diagnostic_dispatch_and_atomic_claim_choose_a_fit_phase(self):
        _, small = self.backlog()
        self.r.config["scheduler"].update(SCHEDULER_DEFAULTS, enabled=True, poll_seconds=1)
        self.assertEqual(admin_admission_status(self.r, self.p)["admission_status"], "available")
        self.assertEqual(dispatch_diagnostic(self.r, self.p)["reason"], "ready")
        self.assertEqual(self.r._scheduler_admin_state(self.p, self.r._scheduler_config())[1], "ready")
        token = claim_dispatch(self.r, self.p)
        self.assertIsNotNone(token)
        release_dispatch(self.r, self.p, token)
        result = self.select()
        self.assertEqual((result["status"], result["selected"]["id"]), ("reserved", small[0]))
        self.assertEqual(self.r.db.execute("SELECT reserved_tokens FROM admin_call_budgets").fetchone()[0], small[2])

    def test_direct_selection_rotates_past_more_than_one_bounded_page(self):
        _, small = self.backlog(count=11)
        results = []
        for _ in range(5):
            result = self.select(limit=3)
            results.append(result)
            self.assertLessEqual(result["scanned_count"], 3)
            if result["status"] == "reserved":
                break
        self.assertEqual(results[-1]["status"], "reserved")
        self.assertEqual(results[-1]["selected"]["id"], small[0])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_dispatch_advances_beyond_forty_large_pending_requests_without_a_model(self):
        _, small = self.backlog(count=41)
        self.assertEqual(dispatch_diagnostic(self.r, self.p)["reason"], "executor_scan_pending")
        with patch("subprocess.Popen") as launch:
            token = claim_dispatch(self.r, self.p)
        launch.assert_not_called()
        self.assertIsNotNone(token)
        release_dispatch(self.r, self.p, token)
        self.assertEqual(dispatch_diagnostic(self.r, self.p)["reason"], "ready")
        result = self.select()
        self.assertEqual((result["status"], result["selected"]["id"]), ("reserved", small[0]))

    def test_unknown_small_call_stays_locked_and_budget_increase_reaches_large_phase(self):
        rows, small = self.backlog()
        result = self.select()
        mark_launch_intent(self.r, result["call_id"], result["owner"], self.r.host_epoch())
        finish_call(self.r, result["call_id"], "timeout_unknown")
        with patch("subprocess.Popen") as launch:
            self.assertNotEqual(self.select()["status"], "reserved")
        launch.assert_not_called()
        self.cap(100000)
        next_call = self.select()
        self.assertEqual(next_call["status"], "reserved")
        self.assertNotEqual(next_call["selected"]["id"], small[0])
        old = self.r.db.execute("SELECT state,retry_allowed FROM admin_calls WHERE id=?", (result["call_id"],)).fetchone()
        self.assertEqual(tuple(old), ("timeout_unknown", 0))
