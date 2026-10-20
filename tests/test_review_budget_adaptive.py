"""Actual request envelopes govern admission, not imaginary padding or source language."""
import json
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_usage import estimate_reservation, admin_project_budget_status
from agent_learning.phase_review import configure, instruction, policy, response_schema, status
from agent_learning.runtime import encode
from tests import test_phase_review as fixtures


class AdaptiveReviewBudgetTests(unittest.TestCase):
    setUp = fixtures.PhaseReviewTests.setUp
    phase = fixtures.PhaseReviewTests.phase

    def one_phase(self, text="A sourced reusable project fact."):
        configure(self.r, self.p, {"every_phases": 1}, policy(self.r, self.p)["version"])
        self.phase("one", text)
        row = self.r.db.execute("SELECT packet FROM phase_review_batches WHERE state='pending'").fetchone()
        return json.loads(row[0]) if row else None

    def test_four_thousand_character_limit_can_review_a_complete_small_case(self):
        self.r.config["admin"]["max_input_chars"] = 4000
        packet = self.one_phase()
        self.assertIsNotNone(packet)
        self.assertEqual(len(packet["items"]), 1)
        self.assertFalse(packet["items"][0]["incomplete"])
        self.assertLessEqual(len(instruction(packet)), 4000)
        self.assertTrue(estimate_reservation(self.r, self.p, instruction(packet), phase=True)["admitted"])
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "skip", result)
        self.assertEqual(status(self.r, self.p)["phases"], {"completed": 1})
        with patch("agent_learning.admin.subprocess.Popen") as model:
            admin_once(self.r, self.p)
        model.assert_not_called()

    def test_estimate_accounts_for_actual_schema_and_utf8_without_changing_policy(self):
        settings = policy(self.r, self.p)
        schemas = len(encode(response_schema()).encode("utf-8"))
        for text in ("a" * 800, "中" * 800, "中\\\"\n" * 100):
            with self.subTest(text=text[:10]):
                estimate = estimate_reservation(self.r, self.p, text, phase=True)
                expected = 12000 + (len(text.encode("utf-8")) + schemas + 1) // 2
                self.assertEqual(estimate["estimated_input_tokens"], expected)
                self.assertEqual(estimate["schema_bytes"], schemas)
                self.assertEqual(estimate["reserved_tokens"], expected + settings["max_output_tokens"])
                self.assertEqual(estimate["instruction_chars"], len(text))
                self.assertTrue(estimate["estimate_only"])
        self.assertEqual(policy(self.r, self.p), settings)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_impossibly_small_character_budget_retains_unreviewed_sources_without_calling(self):
        self.r.config["admin"]["max_input_chars"] = 1000
        with patch("agent_learning.admin.subprocess.Popen") as model:
            packet = self.one_phase("A real sourced project convention that must not be silently discarded.")
            for _ in range(3):
                admin_once(self.r, self.p)
        model.assert_not_called()
        self.assertIsNone(packet)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
        self.assertNotEqual(status(self.r, self.p)["phases"], {"completed": 1})
        self.assertGreater(self.r.db.execute("SELECT COUNT(*) FROM problem_events").fetchone()[0], 0)

    def test_large_unicode_member_does_not_queue_above_token_limit(self):
        self.r.config["admin"]["max_input_chars"] = 64000
        configure(self.r, self.p, {"max_input_tokens": 16000}, policy(self.r, self.p)["version"])
        with patch("agent_learning.admin.subprocess.Popen") as model:
            packet = self.one_phase("工程实际约束和接口定义" * 1000)
        model.assert_not_called()
        self.assertIsNone(packet)
        self.assertEqual(admin_project_budget_status(self.r, self.p)["token_reservations"], 0)

    def test_legacy_reservation_remains_fixed_and_no_usage_is_invented(self):
        estimate = estimate_reservation(self.r, self.p, "large" * 10000, phase=False)
        self.assertEqual(estimate, {"admitted": True, "reserved_tokens": 4000})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_call_budgets").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
