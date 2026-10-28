"""Publication-source observations never imply independent semantic verification."""
import unittest
from agent_learning.source_caveats import context_caveat, summarize_sources


class SourceCaveatTests(unittest.TestCase):
    def event(self, outcome, sequence, *, health="valid", kind="PostToolUse"):
        return {"event_id": str(sequence), "sequence": sequence, "outcome": outcome, "health": health,
                "type": kind, "current_tool_success": health == "valid" and kind == "PostToolUse" and outcome == "succeeded"}

    def source(self, name, events, health="valid"):
        return {"candidate_id": name, "health": health, "evidence": events}

    def test_single_source_recovery_uses_order_not_any_success(self):
        for outcomes, state in ((("failed", "succeeded"), "none"), (("succeeded", "failed"), "unresolved_source_failures")):
            with self.subTest(outcomes=outcomes):
                result = summarize_sources([self.source("a", [self.event(x, n + 1) for n, x in enumerate(outcomes)])])
                self.assertEqual(result["state"], state)

    def test_independent_success_cannot_resolve_another_source_failure(self):
        result = summarize_sources([self.source("a", [self.event("failed", 1)]),
                                    self.source("b", [self.event("succeeded", 20)])])
        self.assertEqual(result, {"state": "mixed_support", "unresolved_source_count": 1,
                                  "supporting_source_count": 1, "source_count": 2})

    def test_repeated_source_and_event_do_not_increase_counts(self):
        record = self.source("a", [self.event("failed", 1), self.event("failed", 1)])
        result = summarize_sources([record, record])
        self.assertEqual(result["unresolved_source_count"], 1)
        self.assertEqual(result["source_count"], 1)

    def test_unknown_and_protocol_completion_are_not_failures_or_recovery(self):
        for outcome in ("reported", "unknown", "call_completed"):
            with self.subTest(outcome=outcome):
                self.assertEqual(summarize_sources([self.source("a", [self.event(outcome, 1)])])["state"], "none")
                result = summarize_sources([self.source("a", [self.event("failed", 1), self.event(outcome, 2)])])
                self.assertEqual(result["state"], "unresolved_source_failures")

    def test_conflicted_or_denied_success_does_not_recover_or_support(self):
        for health in ("conflicted", "denied", "missing"):
            with self.subTest(health=health):
                result = summarize_sources([self.source("a", [self.event("failed", 1), self.event("succeeded", 2, health=health)], health)])
                self.assertEqual(result["unresolved_source_count"], 1)
                self.assertEqual(result["supporting_source_count"], 0)

    def test_unordered_legacy_failure_is_not_guessed_to_be_recovered(self):
        failed = self.event("failed", 1)
        failed.pop("sequence")
        self.assertEqual(summarize_sources([self.source("a", [failed, self.event("succeeded", 2)])])["state"], "unresolved_source_failures")

    def test_clear_caveats_take_no_context_space(self):
        self.assertIsNone(context_caveat({}))
        self.assertIsNone(context_caveat({"source_caveat": summarize_sources([self.source("a", [self.event("reported", 1)])])}))
        self.assertIsNone(context_caveat({"source_caveat": {"state": "mixed_support", "unresolved_source_count": False}}))
