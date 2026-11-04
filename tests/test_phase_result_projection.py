"""Corrupt stored batch results degrade only their bounded display projection."""
import json
import unittest
from unittest.mock import patch

from agent_learning.admin_control import control_status
from agent_learning.phase_review import _result, status
from tests import test_phase_metrics as fixtures


class PhaseResultProjectionTests(unittest.TestCase):
    def test_deep_and_invalid_json_do_not_escape_the_view(self):
        for value in ("[" * 1500 + "0" + "]" * 1500, "{", "null", "[]", "1", 10):
            with self.subTest(value_type=type(value).__name__):
                self.assertEqual(_result(value), {})

    def test_bad_optional_shapes_are_unknown_not_truthy_or_nested(self):
        body = {"coverage_complete": "true", "items": [None, 5, {
            "problem_id": 4, "status": {"unexpected": 1}, "reason": 9,
            "candidate_ids": [1, {}, "good", "x" * 201]}]}
        self.assertEqual(_result(json.dumps(body)), {"coverage_complete": None, "items": [{
            "problem_id": None, "status": None, "reason": "", "candidate_ids": ["good"]}]})

    def test_valid_old_single_candidate_and_current_list_remain_readable(self):
        value = {"coverage_complete": True, "items": [
            {"problem_id": "p", "status": "candidate", "reason": "Accepted source", "candidate_id": "old"},
            {"problem_id": "q", "status": "candidate", "reason": "Second source", "candidate_ids": ["new1", "new2"]},
            {"problem_id": "r", "status": "candidate", "reason": "Legacy preview", "candidate_ids": [], "candidate_id": "fallback"}]}
        result = _result(json.dumps(value))
        self.assertIs(result["coverage_complete"], True)
        self.assertEqual([item["candidate_ids"] for item in result["items"]], [["old"], ["new1", "new2"], ["fallback"]])

    def test_projection_has_fixed_limits_and_preserves_plain_untrusted_text(self):
        raw = {"problem_id": "p", "status": "skip", "reason": "<script>" + "x" * 500,
               "candidate_ids": ["candidate-" + str(i) for i in range(20)]}
        result = _result(json.dumps({"items": [raw] * 50}))
        self.assertEqual(len(result["items"]), 8)
        self.assertEqual(len(result["items"][0]["candidate_ids"]), 5)
        self.assertEqual(len(result["items"][0]["reason"]), 250)
        self.assertTrue(result["items"][0]["reason"].startswith("<script>"))


class CorruptResultSettingsTests(unittest.TestCase):
    setUp = fixtures.PhaseMetricsTests.setUp
    phase = fixtures.PhaseMetricsTests.phase
    review = fixtures.PhaseMetricsTests.review

    def test_deep_result_does_not_break_status_or_settings_and_does_not_write(self):
        batch, _ = self.review()
        damaged = '{"items":[],"padding":' + "[" * 1500 + "0" + "]" * 1500 + "}"
        self.r.db.execute("UPDATE phase_review_batches SET result=? WHERE problem_id=?", (damaged, batch))
        before = self.r.db.total_changes
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            view = status(self.r, self.p)
            settings = control_status(self.r, self.p, details=False)
        launch.assert_not_called()
        self.assertEqual(view["recent_batches"][0]["items"], [])
        self.assertIsNone(view["recent_batches"][0]["coverage_complete"])
        self.assertEqual(settings["phase_review"]["recent_batches"][0]["items"], [])
        self.assertEqual(self.r.db.total_changes, before)

    def test_bad_result_item_does_not_hide_other_valid_visible_items(self):
        batch, _ = self.review()
        damaged = {"coverage_complete": False, "items": [{"problem_id": "good", "status": "skip", "reason": "Observed"},
                    {"problem_id": "bad", "status": ["skip"], "reason": 42, "candidate_ids": {"wrong": 1}}]}
        self.r.db.execute("UPDATE phase_review_batches SET result=? WHERE problem_id=?", (json.dumps(damaged), batch))
        before = self.r.db.total_changes
        view = status(self.r, self.p)
        self.assertEqual(view["recent_batches"][0]["items"][0]["reason"], "Observed")
        self.assertEqual(view["recent_batches"][0]["items"][1]["reason"], "")
        self.assertEqual(self.r.db.total_changes, before)


if __name__ == "__main__":
    unittest.main()
