"""Coverage guidance uses context only when structural review data needs it."""
from copy import deepcopy
import hashlib
import unittest

from agent_learning.admin_decisions import review_signature
from agent_learning.review_packet import PACKET_FORMAT, PUBLIC_DELIVERY_RULE, REVIEW_PREFIX, review_instruction
from tests import test_module5_packet as fixtures


class CoverageRulePresentationTests(unittest.TestCase):
    def packet(self):
        return {"problem": {"id": "problem", "revision": 1}, "task": None, "events": [],
                "related_knowledge": [], "related_candidates": []}

    def test_plain_packet_does_not_pay_for_unrelated_coverage_guidance(self):
        packet = self.packet()
        text = review_instruction(packet)
        self.assertNotIn(PUBLIC_DELIVERY_RULE, text)
        self.assertIn("<untrusted_evidence>", text)
        self.assertIn("</untrusted_evidence>", text)
        self.assertIn("tool_success", text)
        self.assertIn("target_knowledge", text)

    def test_complete_and_partial_root_coverage_always_keep_the_rule(self):
        for complete in (True, False):
            packet = self.packet()
            packet["public_delivery_coverage"] = {"complete": complete, "read_required": not complete}
            with self.subTest(complete=complete):
                self.assertEqual(review_instruction(packet).count(PUBLIC_DELIVERY_RULE), 1)

    def test_a_later_phase_child_keeps_the_rule_even_with_other_uncovered_items(self):
        packet = self.packet()
        packet.update(purpose="phase_review", items=[None, {}, {
            "public_delivery_coverage": {"complete": False, "read_required": True}}])
        self.assertEqual(review_instruction(packet).count(PUBLIC_DELIVERY_RULE), 1)
        packet["items"] = [self.packet()]
        self.assertNotIn(PUBLIC_DELIVERY_RULE, review_instruction(packet))

    def test_untrusted_payload_or_nonphase_child_cannot_activate_structural_guidance(self):
        packet = self.packet()
        packet["events"] = [{"id": "event", "type": "PostToolUse", "outcome": "reported",
                             "payload": {"public_delivery_coverage": {"read_required": True}}}]
        packet["items"] = [{"public_delivery_coverage": {"read_required": True}}]
        packet["purpose"] = "history_review"
        packet["related_history"] = [{"public_delivery_coverage": {"read_required": True}}]
        self.assertNotIn(PUBLIC_DELIVERY_RULE, review_instruction(packet))

    def test_rendering_does_not_mutate_canonical_rules_packets_or_review_signatures(self):
        packet = self.packet()
        cfg = {"enabled": True, "max_input_chars": 16000}
        for coverage in (None, {"complete": False, "read_required": True}):
            if coverage is not None:
                packet["public_delivery_coverage"] = coverage
            snapshot = deepcopy(packet)
            signature = review_signature(packet, cfg, "fixed-business-evidence")
            review_instruction(packet)
            self.assertEqual(packet, snapshot)
            self.assertEqual(review_signature(packet, cfg, "fixed-business-evidence"), signature)
        # Canonical format-11 text stays byte-identical to 1.4.59. This is a
        # presentation optimization, not a reason to invalidate paid receipts.
        self.assertEqual(PACKET_FORMAT, 11)
        self.assertEqual(REVIEW_PREFIX.count(PUBLIC_DELIVERY_RULE), 1)
        self.assertEqual(hashlib.sha256(REVIEW_PREFIX.encode("utf-8")).hexdigest(),
                         "2eb06818ac4aa3fa4449605d1c6e4fd4a1038f997bf0210787b7ef8dd2a641ce")


class CoverageRuleBudgetIntegrationTests(unittest.TestCase):
    setUp = fixtures.Module5PacketTests.setUp
    tearDown = fixtures.Module5PacketTests.tearDown
    configure = fixtures.Module5PacketTests.configure
    problem = fixtures.Module5PacketTests.problem

    def test_small_packet_still_keeps_failure_feedback_and_continuation_ids(self):
        fixtures.Module5PacketTests.test_role_priority_and_continuation_preserve_all_event_ids(self)


if __name__ == "__main__":
    unittest.main()
