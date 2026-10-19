"""Per-batch measurements use existing receipts without new model calls."""
import json
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.phase_metrics import batch_metrics
from agent_learning.phase_review import status
from tests import test_phase_review as fixtures


class PhaseMetricsTests(unittest.TestCase):
    setUp = fixtures.PhaseReviewTests.setUp
    phase = fixtures.PhaseReviewTests.phase

    def review(self, decision="skip"):
        self.phase("first", "First sourced build convention")
        self.phase("second", "Second independent sourced constraint")
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer(decision)):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], decision)
        row = self.r.db.execute("SELECT problem_id,packet FROM phase_review_batches WHERE state='completed'").fetchone()
        return row[0], json.loads(row[1])

    def usage(self, batch, value):
        self.r.db.execute("UPDATE admin_calls SET usage=? WHERE project=? AND problem=?",
                          (json.dumps(value), self.p, batch))

    def test_one_call_many_cases_and_receipts_counted_once(self):
        batch, packet = self.review()
        self.usage(batch, {"token_totals":{"input_tokens":11465,"output_tokens":203},
                           "completeness":"complete","elapsed_seconds":13.132})
        before = self.r.db.total_changes
        metric = batch_metrics(self.r, self.p, batch)
        self.assertEqual(metric["call_count"], 1)
        self.assertEqual(metric["submitted_cases"], len(packet["items"]))
        self.assertGreaterEqual(metric["submitted_cases"], 2)
        self.assertEqual(metric["evidence_records"], len({e["id"] for item in packet["items"] for e in item["events"]}))
        self.assertEqual(metric["candidate_count"], 0)
        self.assertEqual(metric["published_count"], 0)
        self.assertEqual(metric["total_tokens"], 11668)
        self.assertEqual(metric["call_elapsed_seconds"], 13.132)
        self.assertEqual(metric["usage_completeness"], "complete")
        self.assertEqual(self.r.db.total_changes, before)
        self.assertEqual(status(self.r, self.p)["recent_batches"][0]["metrics"], metric)

    def test_missing_and_partial_usage_are_not_zero(self):
        batch, _ = self.review()
        self.usage(batch, {})
        metric = batch_metrics(self.r, self.p, batch)
        self.assertIsNone(metric["total_tokens"])
        self.assertIsNone(metric["call_elapsed_seconds"])
        self.assertEqual(metric["usage_completeness"], "unknown")
        self.usage(batch, {"token_totals":{"input_tokens":100},"completeness":"partial"})
        metric = batch_metrics(self.r, self.p, batch)
        self.assertEqual(metric["input_tokens"], 100)
        self.assertIsNone(metric["output_tokens"])
        self.assertIsNone(metric["total_tokens"])
        self.assertEqual(metric["usage_completeness"], "partial")

    def test_candidates_are_not_publications(self):
        batch, _ = self.review("candidate")
        metric = batch_metrics(self.r, self.p, batch)
        self.assertGreaterEqual(metric["candidate_count"], 4)
        self.assertEqual(metric["published_count"], 0)
        self.assertEqual(metric["call_count"], 1)

    def test_foreign_host_and_project_cannot_read_batch_metrics(self):
        batch, _ = self.review()
        self.assertIsNone(batch_metrics(self.r, "unknown-project", batch))
        self.r.host_scope = "antigravity:default"
        self.r.host = "antigravity"
        self.assertIsNone(batch_metrics(self.r, self.p, batch))

    def test_bounded_invalid_packet_and_usage_do_not_break_status(self):
        batch, _ = self.review()
        self.r.db.execute("UPDATE phase_review_batches SET packet=? WHERE problem_id=?", ('{'+' '*1000001, batch))
        self.usage(batch, {"token_totals":{"input_tokens":True}})
        metric = batch_metrics(self.r, self.p, batch)
        self.assertIsNone(metric["submitted_cases"])
        self.assertIsNone(metric["total_tokens"])
        self.assertFalse(metric["material_counts_complete"])


if __name__ == "__main__":
    unittest.main()
