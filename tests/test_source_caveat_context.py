"""Mixed support is discoverable without replaying an unresolved claim as advice."""
import json
import unittest

from agent_learning.knowledge_output import search_preview
from tests import test_source_recovery_consistency as fixtures


class SourceCaveatContextTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SourceRecoveryConsistencyTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.r, self.p = self.f.r, self.f.p
        self.r.task_update(self.p, "caveat-reader", "allocator recovery", {"mode": "analysis"})
        self.handle = self.r.register_context(self.p, "caveat-reader")["context_handle"]

    def source(self, outcomes, name):
        candidate, *_ = self.f.recovered(outcomes, conversation=name, validation="reported")
        return self.r.publish(self.p, candidate)

    def context(self):
        return self.r._context(self.p, "caveat-reader", query="allocator recovery",
                               context_handle=self.handle, workspace=self.f.workspace)

    @staticmethod
    def cards(text):
        return [json.loads(line) for line in text.splitlines() if line.startswith('{"')]

    def mixed(self):
        record = self.source(("failed",), "unrecovered")
        self.source(("succeeded",), "independent-success")
        return record

    def test_mixed_sources_get_a_risk_index_not_an_automatic_conclusion(self):
        record = self.mixed()
        output = self.context()
        card = next(c for c in self.cards(output) if c["knowledge_id"] == record["knowledge_id"])
        self.assertEqual(card["source_caveat"]["state"], "mixed_support")
        self.assertTrue(card["read_required"])
        self.assertFalse(card.get("content_summary"))
        self.assertEqual(card["validation_level"], "reported")
        self.assertEqual(card["source_health"], "valid")
        self.assertEqual(self.cards(self.context()), [])
        self.assertLessEqual(len(output), self.r.config["injection_max_chars"])

    def test_a_single_unresolved_source_uses_the_same_safe_index(self):
        record = self.source(("failed",), "only-failure")
        card = next(c for c in self.cards(self.context()) if c["knowledge_id"] == record["knowledge_id"])
        self.assertEqual(card["source_caveat"]["unresolved_source_count"], 1)
        self.assertTrue(card["read_required"])
        self.assertFalse(card.get("content_summary"))

    def test_recovered_source_still_delivers_a_whole_conclusion(self):
        record = self.source(("failed", "succeeded"), "recovered")
        card = next(c for c in self.cards(self.context()) if c["knowledge_id"] == record["knowledge_id"])
        self.assertNotIn("source_caveat", card)
        self.assertIn("verify allocator recovery", card["content_summary"])
        self.assertEqual(self.cards(self.context()), [])

    def test_new_failed_source_invalidates_old_body_coverage_and_notifies_once(self):
        record = self.source(("succeeded",), "healthy-first")
        self.assertIn("verify allocator recovery", self.context())
        self.source(("failed",), "failed-later-source")
        changed = self.context()
        card = next(c for c in self.cards(changed) if c["knowledge_id"] == record["knowledge_id"])
        self.assertTrue(card["source_caveat"]["unresolved_source_count"])
        self.assertFalse(card.get("content_summary"))
        self.assertIn("未恢复失败", changed)
        again = self.context()
        self.assertEqual(self.cards(again), [])
        self.assertNotIn("未恢复失败", again)

    def test_manual_read_and_bounded_search_keep_the_warning_and_original_body(self):
        record = self.mixed()
        item = self.r.read(self.p, record["knowledge_id"])
        self.assertIn("verify allocator recovery", item["content"])
        self.assertEqual(item["source_caveat"]["state"], "mixed_support")
        result = search_preview({"items": [item]}, max_chars=1400)
        self.assertEqual(result["items"][0]["source_caveat"]["state"], "mixed_support")
