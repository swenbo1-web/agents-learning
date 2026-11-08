"""Final risk validation reserves the shared budget without duplicating reads."""
import unittest
from unittest.mock import patch

from tests import test_work_source_risk_priority1465 as fixtures


class WorkRiskBudgetTests(unittest.TestCase):
    def setUp(self):
        self.source = fixtures.WorkSourceRiskPriorityTests()
        self.source.setUp()
        self.addCleanup(self.source.doCleanups)

    def test_fresh_validation_once_then_candidate_resumes_after_risk_ack(self):
        source = self.source
        f = source.f
        candidate = source.candidate()
        f.r.config["work_memory"]["max_context_chars"] = 600
        source.conflict()
        with patch.object(f.r, "work_validate_items", wraps=f.r.work_validate_items) as validate:
            text, cards = source.context_with_candidates()
        self.assertEqual(validate.call_count, 1)
        self.assertTrue(cards)
        self.assertEqual(cards[0]["memory_id"], source.risk_id)
        self.assertEqual(cards[0]["state"], "conflicted")
        self.assertNotIn("summary", cards[0])
        self.assertNotIn(candidate, text)
        with patch.object(f.r, "work_validate_items", wraps=f.r.work_validate_items) as validate:
            following, next_cards = source.context_with_candidates()
        # The risk is acknowledged and recall has no further work items.
        self.assertEqual(validate.call_count, 0)
        self.assertIn(candidate, following)
        self.assertNotIn(source.risk_id, [card["memory_id"] for card in next_cards])

    def test_total_128_budget_keeps_source_and_new_candidate_unacknowledged(self):
        source = self.source
        f = source.f
        candidate = source.candidate()
        source.conflict()
        before = source.shown()
        f.r.config["work_memory"]["max_context_chars"] = 600
        f.r.config["injection_max_chars"] = 128
        text, cards = source.context_with_candidates()
        self.assertLessEqual(len(text), 128)
        self.assertEqual(cards, [])
        self.assertNotIn(candidate, text)
        self.assertEqual(source.shown(), before)
        f.r.config["injection_max_chars"] = 2400
        restored, risk_cards = source.context_with_candidates()
        self.assertNotIn(candidate, restored)
        self.assertEqual(risk_cards[0]["memory_id"], source.risk_id)
        self.assertNotEqual(source.shown(), before)
        self.assertIn(candidate, source.context_with_candidates()[0])
