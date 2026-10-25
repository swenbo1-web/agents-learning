"""Model text is bounded; UI capabilities and full snapshots remain private."""
import copy
import json
import math
import unittest
from unittest.mock import patch

from agent_learning.panel_output import MAX_MODEL_CHARS, model_panel_result
from tests import test_panel_mcp as panel_fixture
from tests.test_panel_mcp import SUMMARY


class PanelOutputTests(unittest.TestCase):
    def large_result(self):
        usage = {"budget_mode": "tokens", "call_limit_enforced": False, "counted_attempts": 9,
                 "completed_reviews": 1, "failed_attempts": 8, "active_attempts": 0,
                 "reported_tokens": None, "usage_unknown_attempts": 8,
                 "remaining_calls": 91, "remaining_reported_tokens": None}
        summary = copy.deepcopy(SUMMARY)
        summary.update(project_usage=usage, global_usage={**usage, "counted_attempts": 18},
            admission={"admission_status": "available", "blocking_scope": None,
                       "project_usage": usage, "global_usage": usage, "huge": "x" * 20000},
            phase_review={"blocked_phase_count": 20, "waiting_phase_count": 0,
                          "recent_batches": [{"private_notes": "x" * 20000}]},
            project_budget_policy={"huge": "x" * 20000})
        return {"visible": True, "summary": summary, "capability": "secret-capability",
                "context_handle": "secret-context", "diagnostics": {"backups": {"entries": ["x" * 20000]}}}

    def test_large_snapshots_are_small_without_mutating_ui_state(self):
        original = self.large_result()
        frozen = copy.deepcopy(original)
        result = model_panel_result(original)
        self.assertEqual(original, frozen)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))), MAX_MODEL_CHARS)
        self.assertEqual(result["summary"]["project_usage"]["counted_attempts"], 9)
        self.assertEqual(result["summary"]["global_usage"]["counted_attempts"], 18)
        self.assertIsNone(result["summary"]["project_usage"]["reported_tokens"])
        self.assertFalse(result["summary"]["project_usage"]["call_limit_enforced"])
        self.assertEqual(result["summary"]["phase_review"]["blocked_phase_count"], 20)
        text = json.dumps(result)
        for forbidden in ("secret-capability", "secret-context", "expected", "recent_batches", "private_notes", "project_budget_policy", "entries"):
            self.assertNotIn(forbidden, text)
        self.assertNotIn("project_usage", result["summary"]["admission"])

    def test_action_receipts_are_preserved_without_archive_details(self):
        result = model_panel_result({"visible": True, "opened": "browser", "preset": "light",
            "history": {"job_id": "job", "state": "prepared", "archived_cases": 7,
                        "review_requested": False, "preparation": {"action": "continue_existing", "paths": ["private"]},
                        "changes": {"paths": ["private"]}}})
        self.assertEqual(result["opened"], "browser")
        self.assertEqual(result["preset"], "light")
        self.assertEqual(result["history"]["archived_cases"], 7)
        self.assertFalse(result["history"]["review_requested"])
        self.assertEqual(result["history"]["preparation"], {"action": "continue_existing"})
        self.assertNotIn("private", json.dumps(result))

    def test_absent_values_do_not_become_zero_and_nonfinite_values_stay_unknown(self):
        value = model_panel_result({"visible": False, "reason": "expired", "summary": {
            "project_usage": {"reported_tokens": math.nan, "remaining_calls": 10**10000},
            "global_usage": None}})
        self.assertIsNone(value["summary"]["project_usage"]["reported_tokens"])
        self.assertIsNone(value["summary"]["project_usage"]["remaining_calls"])
        self.assertNotIn("counted_attempts", value["summary"]["project_usage"])
        self.assertEqual(value["reason"], "expired")
        json.dumps(value, allow_nan=False)

    def test_extreme_optional_strings_and_collections_are_bounded(self):
        value = self.large_result()
        def flood(data):
            for key, item in data.items():
                if isinstance(item, dict): flood(item)
                elif isinstance(item, (str, int, float)): data[key] = "large " * 10000
        flood(value)
        result = model_panel_result(value)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))), MAX_MODEL_CHARS)
        self.assertTrue(result.get("details_omitted"))

    def test_debug_preserves_actual_runtime_match_field(self):
        result = model_panel_result({"visible": True, "diagnostics": {
            "version": 1, "installation": {"version": "1.4.25", "running_version": "1.4.24",
                "running_runtime_matches_installation": False, "backup_paths": ["private"]}}})
        self.assertFalse(result["diagnostics"]["installation"]["running_runtime_matches_installation"])
        self.assertNotIn("backup_paths", result["diagnostics"]["installation"])

    def test_json_escaping_cannot_exceed_response_budget(self):
        names = ("project_id", "project_name", "host_scope", "admin_admission_enabled",
                 "model_mode", "model", "admin_phase", "reason", "budget_scope")
        for character in ('"', "\\", "\x01", "\n", "\t"):
            with self.subTest(character=repr(character)):
                value = character * 200
                original = {"visible": True, "reason": value, "opened": value, "preset": value,
                    "summary": {**{name: value for name in names},
                        "admission": {"admission_status": value, "blocking_scope": value}}}
                result = model_panel_result(original)
                encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                self.assertLessEqual(len(encoded), MAX_MODEL_CHARS)
                self.assertEqual(json.loads(encoded)["visible"], True)
                self.assertEqual(original["reason"], value)


class PanelProtocolOutputTests(unittest.TestCase):
    def test_all_protocols_preserve_full_private_ui_and_compact_public_text(self):
        fixture = panel_fixture.PanelMCPTests(methodName="test_capability_is_only_hidden_metadata_even_on_old_protocol")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        result = PanelOutputTests().large_result()
        original = copy.deepcopy(result)
        with patch("tests.test_panel_mcp.FakePanel.bootstrap", return_value=result):
            for version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
                with self.subTest(version=version):
                    server, _ = fixture.ready(version=version)
                    response = fixture.tool(server, "learning_panel", {"context_handle": "current-handle"})["result"]
                    private = response["_meta"]["agentLearningPanel"]
                    self.assertEqual(private["summary"], original["summary"])
                    self.assertEqual(private["diagnostics"], original["diagnostics"])
                    self.assertEqual(private["capability"], "secret-capability")
                    self.assertEqual(private["context_handle"], "current-handle")
                    self.assertIn("expected", private["summary"])
                    text = response["content"][0]["text"]
                    self.assertLessEqual(len(text), MAX_MODEL_CHARS)
                    self.assertNotIn("secret", text)
                    self.assertNotIn("expected", text)
                    if "structuredContent" in response:
                        self.assertEqual(json.loads(text), response["structuredContent"])


if __name__ == "__main__":
    unittest.main()
