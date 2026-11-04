"""Frozen selection diagnostics are read-only and do not invent dispatch causes."""
import json
import unittest
from unittest.mock import patch

from agent_learning.phase_metrics import _selection_diagnostics, batch_metrics
from agent_learning.phase_review import policy, configure, instruction, status
from tests import test_phase_metrics as fixtures


class SelectionProjectionTests(unittest.TestCase):
    def test_counts_describe_frozen_material_not_live_readiness(self):
        packet = {"phase_ids": ["p1", "p2"], "items": [
            {"related_knowledge": [{"knowledge_id": "k"}], "related_candidates": []},
            {"related_knowledge": [{"knowledge_id": "k"}], "related_candidates": [{"candidate_id": "c"}]}],
            "reused_problem_count": 80, "reused_problem_ids": ["r"],
            "omitted_problem_count": 90, "omitted_problem_ids": ["o"]}
        result = _selection_diagnostics(packet, 1234)
        self.assertEqual(result, {"scope": "frozen_batch_selection", "trigger_reason": "not_recorded",
            "policy_at_selection": None, "selection_reason": "visible_material_selected", "packet_chars": 1234,
            "phase_count": 2, "selected_cases": 2, "reused_cases": 80, "uncovered_cases": 90,
            "related_knowledge_refs": 2, "related_candidate_refs": 1})

    def test_local_reuse_does_not_claim_a_new_model_call(self):
        packet = {"items": [], "phase_ids": ["p"], "reused_problem_count": 3, "omitted_problem_count": 0}
        result = _selection_diagnostics(packet, 80)
        self.assertEqual(result["selection_reason"], "local_reuse_only")
        self.assertNotIn("call_count", result)
        packet["reused_problem_count"] = 0
        packet["omitted_problem_count"] = 2
        self.assertEqual(_selection_diagnostics(packet, 80)["selection_reason"], "uncovered_material")

    def test_old_or_invalid_totals_are_unknown_not_preview_lengths(self):
        for value in ({}, {"reused_problem_count": True, "omitted_problem_count": -1}):
            packet = {"items": [{}], "reused_problem_ids": ["r"], "omitted_problem_ids": ["o"], **value}
            result = _selection_diagnostics(packet, 70)
            self.assertIsNone(result["reused_cases"])
            self.assertIsNone(result["uncovered_cases"])
            self.assertIsNone(result["phase_count"])
            self.assertIsNone(result["related_knowledge_refs"])
            self.assertIsNone(result["related_candidate_refs"])
        self.assertIsNone(_selection_diagnostics(None, None))
        self.assertIsNone(_selection_diagnostics({"items": [None]}, 25))

    def test_frozen_admission_requires_valid_version_reason_and_policy(self):
        snapshot = {"version": 1, "trigger_reason": "max_wait", "every_phases": 3, "max_wait_seconds": 300}
        packet = {"items": [], "_local_admission": snapshot}
        result = _selection_diagnostics(packet, 100)
        self.assertEqual(result["trigger_reason"], "max_wait")
        self.assertEqual(result["policy_at_selection"], {"every_phases": 3, "max_wait_seconds": 300})
        for change in ({"version": True}, {"version": 2}, {"trigger_reason": "model_said_so"},
                       {"trigger_reason": {}}, {"every_phases": True}, {"every_phases": 21},
                       {"max_wait_seconds": -1}, {"max_wait_seconds": 86401}, {"max_wait_seconds": 0}):
            with self.subTest(change=change):
                result = _selection_diagnostics({"items": [], "_local_admission": {**snapshot, **change}}, 100)
                self.assertEqual(result["trigger_reason"], "not_recorded")
                self.assertIsNone(result["policy_at_selection"])
        for reason in ("phase_count", "continuation", "explicit_request"):
            result = _selection_diagnostics({"items": [], "_local_admission":
                {**snapshot, "trigger_reason": reason, "max_wait_seconds": 0}}, 100)
            self.assertEqual(result["trigger_reason"], reason)


class SelectionMetricsIntegrationTests(unittest.TestCase):
    setUp = fixtures.PhaseMetricsTests.setUp
    phase = fixtures.PhaseMetricsTests.phase
    review = fixtures.PhaseMetricsTests.review
    usage = fixtures.PhaseMetricsTests.usage

    def test_projection_does_not_dispatch_write_or_reinterpret_historical_frequency(self):
        batch, packet = self.review()
        self.usage(batch, {"token_totals": {"input_tokens": 100, "cached_input_tokens": 80,
                                            "output_tokens": 20}, "completeness": "complete"})
        configure(self.r, self.p, {"every_phases": 20}, policy(self.r, self.p)["version"])
        before_changes = self.r.db.total_changes
        before_calls = self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0]
        frozen = self.r.db.execute("SELECT packet FROM phase_review_batches WHERE problem_id=?", (batch,)).fetchone()[0]
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            metric = batch_metrics(self.r, self.p, batch)
            view = status(self.r, self.p)
            launch.assert_not_called()
        d = metric["selection_diagnostics"]
        self.assertEqual(d["selected_cases"], len(packet["items"]))
        self.assertEqual(d["packet_chars"], len(frozen))
        self.assertEqual(d["trigger_reason"], "phase_count")
        self.assertEqual(d["policy_at_selection"], {"every_phases": 2, "max_wait_seconds": 0})
        self.assertEqual(metric["cached_input_tokens"], 80)
        self.assertEqual(metric["total_tokens"], 120)  # Cache is a subset, not added again.
        self.assertEqual(view["recent_batches"][0]["metrics"], metric)
        self.assertEqual(self.r.db.total_changes, before_changes)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], before_calls)

    def test_skip_is_not_failure_or_zero_cost_and_unreported_cache_is_unknown(self):
        batch, _ = self.review("skip")
        self.usage(batch, {"token_totals": {"input_tokens": 100, "output_tokens": 10}, "completeness": "complete"})
        result = batch_metrics(self.r, self.p, batch)
        self.assertEqual(result["selection_diagnostics"]["selection_reason"], "visible_material_selected")
        self.assertEqual(result["total_tokens"], 110)
        self.assertEqual(result["call_count"], 1)
        self.assertIsNone(result["cached_input_tokens"])
        self.assertEqual(result["candidate_count"], 0)

    def test_local_admission_does_not_enter_prompt_or_evidence_fingerprint(self):
        from agent_learning.admin import _fingerprint
        batch, packet = self.review()
        self.assertEqual(packet["_local_admission"]["trigger_reason"], "phase_count")
        plain = {key: value for key, value in packet.items() if key != "_local_admission"}
        self.assertEqual(instruction(packet), instruction(plain))
        self.assertNotIn("_local_admission", instruction(packet))
        self.assertEqual(_fingerprint(packet), _fingerprint(plain))
        self.assertIn("_local_admission", packet)  # Projection does not mutate the frozen input.
        self.r.db.execute("UPDATE phase_review_batches SET packet=? WHERE problem_id=?", (json.dumps(plain), batch))
        diagnostic = batch_metrics(self.r, self.p, batch)["selection_diagnostics"]
        self.assertEqual(diagnostic["trigger_reason"], "not_recorded")
        self.assertIsNone(diagnostic["policy_at_selection"])


if __name__ == "__main__":
    unittest.main()
