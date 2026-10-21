"""Automatic identifier recall stays complete without scanning unrelated prose."""
import json
import time
import unittest
from unittest.mock import patch

from agent_learning.knowledge_index import INDEX_VERSION, normalized_query_plan
from agent_learning.runtime import digest
from tests import test_module4_context as fixture
from tests import test_retrieval_extreme_recovery as recovery_fixture


class AnchoredRecallEfficiencyTests(unittest.TestCase):
    setUp = fixture.Module4ContextTests.setUp
    hook = fixture.Module4ContextTests.hook
    publish = fixture.Module4ContextTests.publish
    text = fixture.Module4ContextTests.text
    crowded = recovery_fixture.RetrievalExtremeRecoveryTests.crowded

    def search(self, query, automatic=True, **kwargs):
        return self.r.search(self.p, query, _automatic=automatic, **kwargs)

    def clone_generic(self, count):
        seed = self.publish("transaction lock recovery", "transaction lock recovery guidance")
        row = self.r.db.execute("SELECT * FROM knowledge WHERE id=?", (seed["knowledge_id"],)).fetchone()
        body = json.loads(row["body"])
        with self.r.transaction():
            for index in range(count):
                identity = "generic-" + str(index).zfill(5)
                self.r.db.execute("INSERT INTO knowledge(project,id,revision,body,created) VALUES(?,?,?,?,?)",
                                  (self.p, identity, 1, row["body"], time.time()))
                self.r.db.execute("INSERT INTO active(project,id,revision) VALUES(?,?,1)", (self.p, identity))
                self.r._index_knowledge(self.p, identity, 1, body)
        self.r.db.execute("DELETE FROM cache")
        return seed

    def test_unique_identifier_with_common_prose_avoids_generic_pages_and_caches(self):
        self.clone_generic(1000)
        expected = self.publish("E_PERF0111", "transaction lock recovery guidance")
        self.r.db.execute("DELETE FROM cache")
        traced = []
        self.r.db.set_trace_callback(traced.append)
        with patch.object(self.r, "_public", wraps=self.r._public) as expand:
            first = self.search("E_PERF0111 transaction lock recovery")
        self.r.db.set_trace_callback(None)
        self.assertEqual([item["knowledge_id"] for item in first["items"]], [expected["knowledge_id"]])
        self.assertNotIn("recall_diagnostics", first)
        self.assertEqual(expand.call_count, 1)
        reads = [query for query in traced if "knowledge_fts MATCH" in query]
        self.assertEqual(len(reads), 1)
        self.assertIn('MATCH \'"e_perf0111"\'', reads[0])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 1)
        with patch("agent_learning.knowledge_index.lexical_rows", side_effect=AssertionError("cache missed")):
            again = self.search("E_PERF0111 transaction lock recovery")
        self.assertEqual(again["items"], first["items"])

    def test_manual_search_keeps_broad_candidates_and_both_lanes(self):
        generic = self.publish("transaction lock recovery", "transaction lock recovery guidance")
        expected = self.publish("E_PERF0111", "transaction lock recovery guidance")
        traced = []
        self.r.db.set_trace_callback(traced.append)
        result = self.search("E_PERF0111 transaction lock recovery", False)
        self.r.db.set_trace_callback(None)
        self.assertEqual({item["knowledge_id"] for item in result["items"]},
                         {generic["knowledge_id"], expected["knowledge_id"]})
        self.assertEqual(len([query for query in traced if "knowledge_fts MATCH" in query]), 2)
        by_id = {item["knowledge_id"]: item for item in result["items"]}
        self.assertFalse(by_id[generic["knowledge_id"]]["match"]["auto_eligible"])

    def test_anchor_lane_refills_after_more_than_two_pages_of_unhealthy_sources(self):
        expected = self.crowded(140)
        self.r.db.execute("DELETE FROM cache")
        result = self.search("E_CROWDED transaction lock recovery")
        self.assertEqual([item["knowledge_id"] for item in result["items"]], [expected["knowledge_id"]])
        self.assertEqual(result["recall_diagnostics"]["pages"], 3)
        self.assertEqual(result["recall_diagnostics"]["examined"], 141)
        self.assertFalse(result["recall_diagnostics"]["scan_limit_reached"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 1)

    def test_true_anchor_limit_stays_bounded_and_does_not_cache_incomplete_absence(self):
        self.crowded(300)
        self.r.db.execute("DELETE FROM cache")
        result = self.search("E_CROWDED transaction lock recovery")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["recall_diagnostics"]["pages"], 4)
        self.assertEqual(result["recall_diagnostics"]["examined"], 256)
        self.assertTrue(result["recall_diagnostics"]["scan_limit_reached"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 0)

    def test_full_paths_error_identifiers_and_language_tokens_keep_the_same_gate(self):
        for identifier in (r"src\auth.py", "AUTH-019", "0x80073D02", "C++", "C#"):
            with self.subTest(identifier=identifier):
                expected = self.publish(identifier, "transaction lock recovery guidance")
                result = self.search(identifier + " transaction lock recovery")
                self.assertEqual([item["knowledge_id"] for item in result["items"]],
                                 [expected["knowledge_id"]])
                self.assertTrue(result["items"][0]["match"]["auto_eligible"])

    def test_all_identifiers_are_required_and_limitations_only_stay_ineligible(self):
        self.publish("E_IMPORT", "transaction lock recovery")
        self.publish("reference", "transaction lock recovery", limitations="Not applicable to E_IMPORT src/auth.py")
        expected = self.publish("E_IMPORT src/auth.py", "transaction lock recovery")
        result = self.search("E_IMPORT src/auth.py transaction lock recovery")
        self.assertEqual([item["knowledge_id"] for item in result["items"]], [expected["knowledge_id"]])

    def test_complete_negative_result_can_cache_but_publication_invalidates_it(self):
        self.clone_generic(300)
        query = "E_ABSENT transaction lock recovery"
        first = self.search(query)
        self.assertEqual(first["items"], [])
        self.assertNotIn("recall_diagnostics", first)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 1)
        with patch("agent_learning.knowledge_index.lexical_rows", side_effect=AssertionError("cache missed")):
            self.assertEqual(self.search(query)["items"], [])
        expected = self.publish("E_ABSENT", "transaction lock recovery")
        self.assertEqual(self.search(query)["items"][0]["knowledge_id"], expected["knowledge_id"])

    def test_old_recall_cache_is_not_reused_without_rebuilding_the_index(self):
        expected = self.publish("E_IMPORT", "transaction lock recovery")
        query = "E_IMPORT transaction lock recovery"
        manifest = dict(self.r.db.execute("SELECT generation,revocation_epoch,source_epoch FROM projects WHERE id=?",
                                        (self.p,)).fetchone())
        source_epoch = manifest.pop("source_epoch")
        legacy_plan = {key: value for key, value in normalized_query_plan(query).items()
                       if key != "recall_policy_version"}
        old_key = digest([self.p, legacy_plan, 5, {}, manifest,
                          "local-fts", INDEX_VERSION, True, source_epoch, None])
        self.r.db.execute("DELETE FROM cache")
        self.r.db.execute("INSERT INTO cache VALUES(?,'[]')", (old_key,))
        result = self.search(query)
        self.assertEqual(result["items"][0]["knowledge_id"], expected["knowledge_id"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 2)
        self.assertEqual(self.r.db.execute("SELECT version FROM knowledge_index_state").fetchone()[0], INDEX_VERSION)

    @staticmethod
    def legacy_plan(query):
        return {key: value for key, value in normalized_query_plan(query).items()
                if key != "recall_policy_version"}

    def test_legacy_context_negative_result_requeries_once_after_recall_upgrade(self):
        expected = self.publish("E_CONTEXT", "transaction lock recovery")
        with patch("agent_learning.knowledge_index.normalized_query_plan", side_effect=self.legacy_plan), \
                patch.object(self.r, "search", return_value={"items": []}):
            self.hook("UserPromptSubmit", prompt="E_CONTEXT transaction lock recovery", turn_id="legacy")
        self.assertEqual(self.r.db.execute("SELECT retrieval_refs FROM contexts").fetchone()[0], "[]")
        with patch.object(self.r, "search", wraps=self.r.search) as search:
            text = self.text(self.hook("UserPromptSubmit", prompt="继续", turn_id="upgrade"))
        self.assertEqual(search.call_count, 1)
        self.assertIn(expected["knowledge_id"], text)
        with patch.object(self.r, "search", side_effect=AssertionError("unchanged plan queried twice")):
            self.hook("UserPromptSubmit", prompt="继续", turn_id="unchanged")

    def test_legacy_ready_fault_requeries_once_and_retains_narrow_budget_delivery(self):
        expected = self.publish("Bash E_IMPORT", "Bash E_IMPORT stderr importfailure guidance exit_code")
        self.hook("PreToolUse", tool_use_id="failed", tool_name="Bash")
        self.hook("PostToolUse", tool_use_id="failed", tool_name="Bash",
                  tool_response={"exit_code": 1, "stderr": "E_IMPORT"})
        self.r.config["injection_max_chars"] = 128
        with patch("agent_learning.knowledge_index.normalized_query_plan", side_effect=self.legacy_plan):
            self.hook("PreToolUse", tool_use_id="legacy-small", tool_name="Read")
        fault = self.r.db.execute("SELECT * FROM context_faults").fetchone()
        self.assertEqual(fault["state"], "ready")
        self.assertTrue(json.loads(fault["results"]))
        with patch.object(self.r, "search", wraps=self.r.search) as search:
            self.hook("PreToolUse", tool_use_id="upgrade-small", tool_name="Read")
        self.assertEqual(search.call_count, 1)
        current = self.r.db.execute("SELECT * FROM context_faults").fetchone()
        self.assertEqual(current["state"], "ready")
        self.assertNotEqual(json.loads(current["results"])[0]["retrieval_signature"],
                            json.loads(fault["results"])[0]["retrieval_signature"])
        with patch.object(self.r, "search", side_effect=AssertionError("unchanged ready result queried twice")):
            self.hook("PreToolUse", tool_use_id="same-small", tool_name="Read")
            self.r.config["injection_max_chars"] = 2400
            text = self.text(self.hook("PreToolUse", tool_use_id="deliver", tool_name="Read"))
        self.assertIn(expected["knowledge_id"], text)
        self.assertEqual(self.r.db.execute("SELECT state FROM context_faults").fetchone()[0], "done")

    def test_hybrid_candidates_still_merge_and_obey_positive_identifiers(self):
        expected = self.publish("E_HYBRID", "transaction lock recovery")
        unrelated = self.publish("transaction lock recovery", "common guidance")
        rows = {row["id"]: row for row in self.r.db.execute("SELECT * FROM knowledge")}
        prepared = {"config": {"enabled": False, "automatic_min_similarity": 0.85}, "diagnostics": {},
                    "vector": [1, 0], "signature": self.r.semantic_signature(self.p)}
        matches = {item["knowledge_id"]: {"row": rows[item["knowledge_id"]], "score": 1,
                   "rank": index + 1, "input_truncated": False} for index, item in enumerate((unrelated, expected))}
        with patch.object(self.r, "_semantic_prepare", return_value=prepared), \
                patch.object(self.r, "_semantic_candidates", return_value=matches), \
                patch("agent_learning.knowledge_index.lexical_rows", return_value=[]):
            result = self.search("E_HYBRID transaction lock recovery")
        self.assertEqual([item["knowledge_id"] for item in result["items"]], [expected["knowledge_id"]])
        self.assertEqual(result["items"][0]["match"]["method"], "hybrid")

    def test_degraded_semantic_falls_back_to_anchor_recall_without_caching_failure(self):
        expected = self.publish("E_FALLBACK", "transaction lock recovery")
        self.clone_generic(300)
        prepared = {"config": {"enabled": True}, "diagnostics": {"semantic_status": "degraded", "retryable": True},
                    "vector": None, "signature": self.r.semantic_signature(self.p)}
        with patch.object(self.r, "_semantic_prepare", return_value=prepared):
            result = self.search("E_FALLBACK transaction lock recovery")
        self.assertEqual([item["knowledge_id"] for item in result["items"]], [expected["knowledge_id"]])
        self.assertEqual(result["retrieval"]["semantic_status"], "degraded")
        self.assertNotIn("recall_diagnostics", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
