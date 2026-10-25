"""Focused retrieval keeps audit receipts searchable without treating them as answers."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.runtime import Runtime
from agent_learning.knowledge_index import normalized_query_plan
from agent_learning.work_memory import _metadata_only_write, _write_receipt_query


class RetrievalNoiseRankingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-rank-noise-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        (self.workspace / "journal.py").write_text("# fixture\n", encoding="utf-8")
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": False},
            "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.serial = 0

    def publish(self, title, content, applicability="same environment", limitations="reported only"):
        self.serial += 1
        problem = self.r.feedback(self.p, "sources", "fixture " + str(self.serial))
        packet = self.r.review_packet(self.p, problem["id"])
        result = self.r.submit_candidate(self.p, problem_id=problem["id"],
            problem_revision=problem["revision"], title=title, content=content,
            category="operation", applicability=applicability, limitations=limitations,
            validation_level="reported", evidence_refs=[e["id"] for e in packet["events"]])
        return self.r.publish(self.p, result["candidate_id"])["knowledge_id"]

    def work(self, text, *, receipt=False, path="journal.py", outcome="reported"):
        self.serial += 1
        payload = {"tool_name": "apply_patch" if receipt else "write_file",
                   "tool_input": {"path": path}}
        payload.update({"tool_response": text} if receipt else {"last_assistant_message": text})
        with self.r.transaction():
            event, _ = self.r._event(self.p, "sources", "PostToolUse" if receipt else "Stop",
                                    payload, outcome=outcome)
            result = self.r._work_capture_event(self.p, event["id"], self.workspace)
        return result["memory_id"]

    def search_work(self, query="", *, limit=5):
        return self.r.work_search(self.p, query, file_path="journal.py", workspace=self.workspace,
                                  limit=limit, max_chars=10000)["items"]

    def test_positive_topic_coverage_beats_keywords_only_in_conditions(self):
        generic = self.publish("platform overview", "alpha support", applicability="alpha beta gamma")
        focused = self.publish("beta troubleshooting", "alpha beta gamma diagnosis")
        items = self.r.search(self.p, "alpha beta gamma")["items"]
        self.assertEqual([i["knowledge_id"] for i in items], [focused, generic])
        self.assertEqual(items[0]["match"]["topic_coverage"], 1)
        self.assertLess(items[1]["match"]["topic_coverage"], 1)
        # Ranking changes do not silently tighten or loosen automatic gates.
        self.assertTrue(items[1]["match"]["auto_eligible"])

    def test_focused_explanation_wins_equal_coverage_against_broad_catalogue(self):
        generic = self.publish("alpha beta gamma platform introduction", "alpha beta gamma " +
            "installation languages editors settings accounts guides interfaces examples " * 5)
        focused = self.publish("alpha beta", "gamma diagnosis")
        self.assertEqual(self.r.search(self.p, "alpha beta gamma")["items"][0]["knowledge_id"], focused)
        self.assertEqual(len(self.r.search(self.p, "alpha beta gamma")["items"]), 2)
        self.assertNotEqual(focused, generic)

    def test_old_version_text_is_not_assumed_invalid_and_installation_remains_searchable(self):
        overview = self.publish("Widget v1.0.0 installation overview", "Install Widget prerequisites")
        self.publish("Current renderer", "Widget rendering pipeline")
        item = self.r.search(self.p, "installation overview")["items"][0]
        self.assertEqual(item["knowledge_id"], overview)
        self.assertEqual(item["source_health"], "valid")

    def test_exact_error_identifier_stays_above_short_generic_text(self):
        self.publish("alpha", "alpha diagnosis")
        expected = self.publish("E_LAUNCH_FAILED", "alpha " + "explanation " * 30)
        item = self.r.search(self.p, "E_LAUNCH_FAILED alpha")["items"][0]
        self.assertEqual(item["knowledge_id"], expected)
        self.assertTrue(item["match"]["auto_eligible"])

    def test_rank_version_invalidates_prior_order_without_rebuilding_index(self):
        generic = self.publish("alpha overview", "alpha extra unrelated words")
        focused = self.publish("alpha", "alpha")
        self.r.db.execute("DELETE FROM cache")
        with patch("agent_learning.knowledge_index._RANK_POLICY_VERSION", 0):
            self.r.search(self.p, "alpha")
        key = self.r.db.execute("SELECT key FROM cache").fetchone()[0]
        wrong = json.dumps([{"knowledge_id": generic, "revision": 1}, {"knowledge_id": focused, "revision": 1}])
        self.r.db.execute("UPDATE cache SET body=? WHERE key=?", (wrong, key))
        with patch.object(self.r, "_rebuild_knowledge_index", side_effect=AssertionError("query rebuild")):
            items = self.r.search(self.p, "alpha")["items"]
        self.assertEqual(items[0]["knowledge_id"], focused)
        self.assertEqual(self.r.db.execute("SELECT body FROM cache WHERE key=?", (key,)).fetchone()[0], wrong)

    def test_known_patch_acknowledgement_is_recognized_as_json_string_or_plain_text(self):
        raw = "Success. Updated the following files:\nM journal.py\nA tests/check.py\n"
        for output in (raw, json.dumps(raw), json.dumps("Exit code: 0\nWall time: 0 seconds\nOutput:\n" + raw)):
            with self.subTest(output=output):
                self.assertTrue(_metadata_only_write({"type": "file_change", "summary": output}))

    def test_patch_output_with_warning_error_or_truncation_is_not_discarded(self):
        raw = "Success. Updated the following files:\nM journal.py"
        for output in (raw + "\nWARNING: journal recovery remains incomplete", "Exit code: 1\n" + raw,
                       raw + "\nFailure: E_LAUNCH_FAILED", "Meaningful result before\n" + raw):
            self.assertFalse(_metadata_only_write({"type": "file_change", "summary": json.dumps(output)}))
        self.assertFalse(_metadata_only_write({"type": "file_change", "summary": json.dumps(raw),
                                              "coverage": {"truncated": True}}))
        for coverage in (None, "unknown", {"full_original_available": False}):
            self.assertFalse(_metadata_only_write({"type": "file_change", "summary": json.dumps(raw),
                                                  "coverage": coverage}))

    def test_manual_file_receipt_burst_does_not_hide_older_public_explanation(self):
        expected = self.work("backend recovery: preserve the launcher identity across worker startup.")
        receipts = {self.work("Success. Updated the following files:\nM journal.py\n", receipt=True)
                    for _ in range(16)}
        items = self.search_work("backend recovery", limit=1)
        self.assertEqual([i["memory_id"] for i in items], [expected])
        self.assertFalse(receipts & {i["memory_id"] for i in items})
        self.assertEqual(self.search_work(limit=1)[0]["memory_id"], expected)

    def test_manual_acknowledgements_remain_available_for_explicit_patch_query(self):
        self.work("backend recovery: preserve launcher identity.")
        expected = self.work("Success. Updated the following files:\nM journal.py\n", receipt=True)
        self.assertEqual(self.search_work("apply_patch", limit=1)[0]["memory_id"], expected)
        self.assertEqual(self.search_work("补丁", limit=1)[0]["memory_id"], expected)

    def matching_work_pair(self, query):
        # Both records contain the whole query and share the same file scope.
        # The acknowledgement can acquire topic words from changed filenames;
        # that must not make it a better fault explanation than public analysis.
        explanation = self.work(query + "\nThe result needs a root-cause explanation and verification.")
        filename = query if query.endswith((".py", ".md")) else query + ".txt"
        receipt = self.work("Success. Updated the following files:\nM journal.py\nM " + filename,
                            receipt=True)
        return explanation, receipt

    def test_failure_queries_do_not_prefer_success_acknowledgements(self):
        for query in ("SQLite 写入失败", "apply_patch 崩溃原因", "补丁导致崩溃", "数据库写入错误",
                      "why patch crashed"):
            with self.subTest(query=query):
                self.assertFalse(_write_receipt_query(normalized_query_plan(query)))
                explanation, receipt = self.matching_work_pair(query)
                items = self.search_work(query, limit=2)
                self.assertEqual(items[0]["memory_id"], explanation)
                self.assertEqual(self.r.work_read(self.p, receipt)["memory_id"], receipt)

    def test_explicit_receipt_requests_keep_priority_even_with_failure_context(self):
        for query in ("查失败操作前的成功回执", "patch failed receipt", "补丁成功输出",
                      "apply_patch successful output", "apply_patch", "补丁"):
            with self.subTest(query=query):
                self.assertTrue(_write_receipt_query(normalized_query_plan(query)))
                _, receipt = self.matching_work_pair(query)
                self.assertEqual(self.search_work(query, limit=1)[0]["memory_id"], receipt)

    def test_receipt_word_inside_a_file_identity_does_not_imply_receipt_intent(self):
        for query in ("receipt_notes.py", "archive/回执.md"):
            with self.subTest(query=query):
                self.assertFalse(_write_receipt_query(normalized_query_plan(query)))
                explanation, _ = self.matching_work_pair(query)
                self.assertEqual(self.search_work(query, limit=1)[0]["memory_id"], explanation)

    def test_meaningful_file_output_and_failures_remain_searchable(self):
        expected = self.work({"exit_code": 1, "stderr": "E_LAUNCH_FAILED diagnostic"}, receipt=True,
                             outcome="failed")
        item = self.search_work("E_LAUNCH_FAILED", limit=1)[0]
        self.assertEqual(item["memory_id"], expected)
        self.assertEqual(item["type"], "tool_failure")

    def test_metadata_recognition_does_not_change_stored_observation_or_source(self):
        expected = self.work("Success. Updated the following files:\nM journal.py\n", receipt=True)
        before = self.r.db.execute("SELECT body,source_hash FROM work_memories WHERE id=?", (expected,)).fetchone()
        self.search_work("backend recovery")
        after = self.r.db.execute("SELECT body,source_hash FROM work_memories WHERE id=?", (expected,)).fetchone()
        self.assertEqual(tuple(before), tuple(after))


if __name__ == "__main__":
    unittest.main()
