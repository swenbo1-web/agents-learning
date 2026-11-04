"""Bad local metric records remain unknown without breaking other batch views."""
import json
import unittest
from unittest.mock import patch

from agent_learning.admin_control import control_status
from agent_learning.admin_lifecycle import finish_call
from agent_learning.phase_metrics import _number, _object, batch_metrics
from agent_learning.phase_review import status
from tests import test_phase_metrics as fixtures


class PhaseMetricValueTests(unittest.TestCase):
    def test_numbers_reject_nonfinite_boolean_and_unrecordable_integers(self):
        for value in (None, True, -1, float("inf"), float("nan"), 10 ** 400, 2 ** 63):
            with self.subTest(value_type=type(value).__name__):
                self.assertFalse(_number(value))
        for value in (0, 1.5, 2 ** 63 - 1):
            self.assertTrue(_number(value))

    def test_deep_or_invalid_json_is_unavailable(self):
        for value in ('{"nested":' + '[' * 1500 + '0' + ']' * 1500 + '}',
                      '{"token_totals":{"input_tokens":' + '9' * 5000 + '}}',
                      '{broken', 'null', '[]'):
            with self.subTest(chars=len(value)):
                self.assertIsNone(_object(value, 24000))
        self.assertEqual(_object('{"input_tokens":5}', 24000), {"input_tokens": 5})

    def test_flat_container_walk_has_a_node_limit(self):
        over = '{"items":[' + ','.join('0' for _ in range(100_001)) + ']}'
        self.assertIsNone(_object(over, 1_000_000))
        within = '{"items":[' + ','.join('0' for _ in range(100)) + ']}'
        self.assertEqual(len(_object(within, 1_000_000)["items"]), 100)


class PhaseMetricsCorruptionTests(unittest.TestCase):
    setUp = fixtures.PhaseMetricsTests.setUp
    phase = fixtures.PhaseMetricsTests.phase
    review = fixtures.PhaseMetricsTests.review
    usage = fixtures.PhaseMetricsTests.usage

    def clone_call(self, batch, usage):
        row = dict(self.r.db.execute("SELECT * FROM admin_calls WHERE project=? AND problem=?",
                                   (self.p, batch)).fetchone())
        row.update(id=row["id"] + "-extra", usage=json.dumps(usage))
        self.r.db.execute("INSERT INTO admin_calls(" + ",".join(row) + ") VALUES(" +
                          ",".join("?" for _ in row) + ")", tuple(row.values()))

    def test_oversized_token_record_is_unknown_but_valid_call_time_survives(self):
        batch, _ = self.review()
        self.usage(batch, {"token_totals": {"input_tokens": 10 ** 400, "output_tokens": 20},
                           "completeness": "complete", "elapsed_seconds": 2.5})
        before = self.r.db.total_changes
        metric = batch_metrics(self.r, self.p, batch)
        self.assertIsNone(metric["input_tokens"])
        self.assertIsNone(metric["output_tokens"])
        self.assertIsNone(metric["total_tokens"])
        self.assertEqual(metric["usage_completeness"], "unknown")
        self.assertEqual(metric["call_elapsed_seconds"], 2.5)
        self.assertEqual(metric["call_count"], 1)
        self.assertEqual(self.r.db.total_changes, before)

    def test_corrupt_call_does_not_erase_other_call_receipts_or_invent_complete_usage(self):
        batch, _ = self.review("candidate")
        self.usage(batch, {"token_totals": {"input_tokens": 100, "output_tokens": 20},
                           "completeness": "complete", "elapsed_seconds": 1.0})
        self.clone_call(batch, {"token_totals": {"input_tokens": 10 ** 400, "output_tokens": 10},
                               "completeness": "complete", "elapsed_seconds": 2.0})
        before = self.r.db.total_changes
        metric = batch_metrics(self.r, self.p, batch)
        self.assertEqual(metric["usage_completeness"], "partial")
        self.assertIsNone(metric["total_tokens"])
        self.assertGreaterEqual(metric["candidate_count"], 4)
        self.assertEqual(metric["call_count"], 2)
        self.assertEqual(metric["call_elapsed_seconds"], 3.0)
        self.assertEqual(self.r.db.total_changes, before)

    def test_corrupt_packet_result_and_usage_are_separate_unknown_fields(self):
        batch, _ = self.review()
        deep = '{"value":' + '[' * 1500 + '0' + ']' * 1500 + '}'
        self.r.db.execute("UPDATE phase_review_batches SET packet=?,result=? WHERE problem_id=?", (deep, deep, batch))
        self.r.db.execute("UPDATE admin_calls SET usage=? WHERE problem=?", (deep, batch))
        before = self.r.db.total_changes
        metric = batch_metrics(self.r, self.p, batch)
        self.assertFalse(metric["material_counts_complete"])
        self.assertFalse(metric["result_counts_complete"])
        self.assertIsNone(metric["submitted_cases"])
        self.assertIsNone(metric["result_cases"])
        self.assertIsNone(metric["total_tokens"])
        self.assertEqual(metric["usage_completeness"], "unknown")
        self.assertEqual(self.r.db.total_changes, before)

    def test_bad_batch_timestamp_does_not_break_valid_token_totals(self):
        batch, _ = self.review()
        self.usage(batch, {"token_totals": {"input_tokens": 100, "output_tokens": 20},
                           "completeness": "complete", "elapsed_seconds": 2.0})
        self.r.db.execute("UPDATE phase_review_batches SET created='invalid' WHERE problem_id=?", (batch,))
        metric = batch_metrics(self.r, self.p, batch)
        self.assertIsNone(metric["batch_elapsed_seconds"])
        self.assertEqual(metric["total_tokens"], 120)
        self.assertEqual(metric["call_elapsed_seconds"], 2.0)

    def test_aggregate_overflow_is_unknown_without_float_conversion_or_invalid_json(self):
        batch, _ = self.review()
        usage = {"token_totals": {"input_tokens": 2 ** 62, "output_tokens": 20},
                 "completeness": "complete", "elapsed_seconds": 1e308}
        self.usage(batch, usage)
        self.clone_call(batch, usage)
        metric = batch_metrics(self.r, self.p, batch)
        self.assertIsNone(metric["input_tokens"])
        self.assertIsNone(metric["total_tokens"])
        self.assertEqual(metric["usage_completeness"], "partial")
        self.assertIsNone(metric["call_elapsed_seconds"])
        json.dumps(metric, allow_nan=False)

    def test_corrupt_batch_does_not_break_settings_and_other_batch_status(self):
        batch, _ = self.review()
        self.usage(batch, {"token_totals": {"input_tokens": 10 ** 400, "output_tokens": 1}, "completeness": "complete"})
        original = dict(self.r.db.execute("SELECT * FROM phase_review_batches WHERE problem_id=?", (batch,)).fetchone())
        original.update(problem_id=batch + "-other", result=None)
        self.r.db.execute("INSERT INTO phase_review_batches(" + ",".join(original) + ") VALUES(" +
                          ",".join("?" for _ in original) + ")", tuple(original.values()))
        before = self.r.db.total_changes
        statements = []
        self.r.db.set_trace_callback(statements.append)
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            view = status(self.r, self.p)
            settings = control_status(self.r, self.p, details=False)
            launch.assert_not_called()
        self.r.db.set_trace_callback(None)
        self.assertEqual(len(view["recent_batches"]), 2)
        self.assertEqual(len(settings["phase_review"]["recent_batches"]), 2)
        self.assertEqual(self.r.db.total_changes, before)
        self.assertFalse(any(value.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for value in statements))

    def test_deep_result_settings_projection_and_metric_reads_remain_bounded(self):
        batch, _ = self.review()
        deep = '{"value":' + '[' * 1500 + '0' + ']' * 1500 + '}'
        self.r.db.execute("UPDATE phase_review_batches SET result=? WHERE problem_id=?", (deep, batch))
        before = self.r.db.total_changes
        statements = []
        self.r.db.set_trace_callback(statements.append)
        metric = batch_metrics(self.r, self.p, batch)
        self.r.db.set_trace_callback(None)
        self.assertEqual(len(statements), 6)
        self.assertIsNone(metric["result_cases"])
        self.assertFalse(metric["result_counts_complete"])
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            view = status(self.r, self.p)
            settings = control_status(self.r, self.p, details=False)
            launch.assert_not_called()
        self.assertEqual(view["recent_batches"][0]["items"], [])
        self.assertEqual(len(settings["phase_review"]["recent_batches"]), 1)
        self.assertEqual(self.r.db.total_changes, before)

    def test_finish_call_conflict_is_visible_without_a_false_cache_subset(self):
        batch, _ = self.review()
        call = self.r.db.execute("SELECT id FROM admin_calls WHERE project=? AND problem=?", (self.p, batch)).fetchone()[0]
        saved = finish_call(self.r, call, "skip", {"token_totals": {
            "input_tokens": 100, "cached_input_tokens": 150, "output_tokens": 20}, "completeness": "complete"})
        self.assertEqual(json.loads(saved["usage"])["token_totals"]["cached_input_tokens"], 150)
        before = self.r.db.total_changes
        metric = batch_metrics(self.r, self.p, batch)
        self.assertTrue(metric["usage_conflict"])
        self.assertIn("cached_input_exceeds_input", metric["usage_consistency_errors"])
        self.assertIsNone(metric["cached_input_tokens"])
        self.assertEqual((metric["input_tokens"], metric["output_tokens"], metric["total_tokens"]), (100, 20, 120))
        self.assertEqual(metric["usage_completeness"], "partial")
        self.assertEqual(status(self.r, self.p)["recent_batches"][0]["metrics"], metric)
        self.assertEqual(self.r.db.execute("SELECT usage FROM admin_calls WHERE id=?", (call,)).fetchone()[0], saved["usage"])
        self.assertEqual(self.r.db.execute("SELECT reported_tokens FROM admin_call_budgets WHERE call_id=?", (call,)).fetchone()[0], 120)
        self.assertEqual(self.r.db.total_changes, before)

    def test_bad_turn_cache_is_hidden_even_when_the_aggregate_is_a_valid_subset(self):
        batch, _ = self.review()
        self.usage(batch, {"token_usage": [{"input_tokens": 2, "cached_input_tokens": 10, "output_tokens": 1},
                                         {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 1}]})
        metric = batch_metrics(self.r, self.p, batch)
        self.assertTrue(metric["usage_conflict"])
        self.assertIn("turn_cached_input_exceeds_input", metric["usage_consistency_errors"])
        self.assertIsNone(metric["cached_input_tokens"])
        self.assertEqual((metric["input_tokens"], metric["output_tokens"], metric["total_tokens"]), (102, 2, 104))
        self.assertEqual(metric["usage_completeness"], "partial")

    def test_missing_cache_is_unknown_without_a_false_conflict(self):
        batch, _ = self.review()
        self.usage(batch, {"token_totals": {"input_tokens": 100, "output_tokens": 20}, "completeness": "complete"})
        metric = batch_metrics(self.r, self.p, batch)
        self.assertFalse(metric["usage_conflict"])
        self.assertEqual(metric["usage_consistency_errors"], [])
        self.assertIsNone(metric["cached_input_tokens"])
        self.assertEqual(metric["usage_completeness"], "complete")


if __name__ == "__main__":
    unittest.main()
