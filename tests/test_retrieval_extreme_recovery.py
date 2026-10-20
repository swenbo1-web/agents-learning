"""Bounded query identities and refill recovery with real isolated SQLite."""
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

from agent_learning.knowledge_index import INDEX_VERSION, normalized_query_plan, terms
from tests import test_module4_context as fixture


class RetrievalExtremeRecoveryTests(unittest.TestCase):
    setUp = fixture.Module4ContextTests.setUp
    hook = fixture.Module4ContextTests.hook
    publish = fixture.Module4ContextTests.publish
    text = fixture.Module4ContextTests.text

    def search(self, query, automatic=True):
        return self.r.search(self.p, query, _automatic=automatic)

    def test_error_before_character_cutoff_still_controls_automatic_recall(self):
        expected = self.publish("E_CRITICAL", "E_CRITICAL repair transaction safety")
        wrong = self.publish("管理员设置页面配色", "管理员设置页面配色主题颜色按钮样式")
        query = "E_CRITICAL" + " filler" * 1500 + " 管理员设置页面配色"
        result = self.search(query)
        self.assertEqual([item["knowledge_id"] for item in result["items"]], [expected["knowledge_id"]])
        self.assertTrue(result["query_diagnostics"]["automatic_safe"])
        self.assertTrue(result["query_diagnostics"]["input_truncated"])
        self.assertNotIn(wrong["knowledge_id"], self.text(self.hook(
            "UserPromptSubmit", prompt=query, turn_id="long")))
        self.assertIn("E_CRITICAL", self.r.db.execute("SELECT last_query FROM contexts").fetchone()[0])
        compact = self.text(self.hook("SessionStart", source="compact"))
        self.assertIn(expected["knowledge_id"], compact)
        self.assertNotIn(wrong["knowledge_id"], compact)

    def test_identity_overflow_is_manual_only_across_continuation_and_compaction(self):
        files = " ".join("path" + str(index) + ".py" for index in range(128))
        wrong = self.publish("many files", files)
        query = files + " E_CRITICAL"
        plan = normalized_query_plan(query)
        self.assertIn("e_critical", plan["exact_anchors"])
        self.assertFalse(plan["automatic_safe"])
        with patch.object(self.r, "_semantic_prepare", side_effect=AssertionError("unsafe query called provider")):
            result = self.search(query)
        self.assertEqual(result["items"], [])
        self.assertTrue(result["query_diagnostics"]["identity_limit_exceeded"])
        manual = self.search(query, False)
        self.assertIn(wrong["knowledge_id"], [item["knowledge_id"] for item in manual["items"]])
        self.assertFalse(manual["items"][0]["match"]["auto_eligible"])
        self.hook("UserPromptSubmit", prompt=query, turn_id="overflow")
        self.publish("new background", "background release")  # invalidate the previous empty result
        for kind, fields in (("UserPromptSubmit", {"prompt": "继续", "turn_id": "continue"}),
                             ("SessionStart", {"source": "compact"})):
            self.assertNotIn(wrong["knowledge_id"], self.text(self.hook(kind, **fields)))

    def test_full_scan_limit_stays_unsafe_after_persisting_a_bounded_query(self):
        wrong = self.publish("compiler", "compiler configuration")
        query = "compiler " + " " * 65536 + " E_CRITICAL"
        result = self.search(query)
        self.assertFalse(result["query_diagnostics"]["identity_scan_complete"])
        self.assertEqual(result["items"], [])
        self.hook("UserPromptSubmit", prompt=query, turn_id="huge")
        saved = self.r.db.execute("SELECT last_query FROM contexts").fetchone()[0]
        self.assertEqual(len(saved), 65537)
        self.assertFalse(normalized_query_plan(saved)["automatic_safe"])
        self.assertNotIn(wrong["knowledge_id"], self.text(self.hook("SessionStart", source="compact")))

    def test_long_prose_without_complete_identity_is_manual_only(self):
        wrong = self.publish("管理员设置页面配色", "管理员设置页面配色主题颜色按钮样式")
        result = self.search("未明确定位的其他问题" * 1200 + " 管理员设置页面配色")
        self.assertEqual(result["items"], [])
        self.assertFalse(result["query_diagnostics"]["automatic_safe"])
        manual = self.search("未明确定位的其他问题" * 1200 + " 管理员设置页面配色", False)
        self.assertIn(wrong["knowledge_id"], [item["knowledge_id"] for item in manual["items"]])

    def test_normal_queries_do_not_add_redundant_diagnostic_tokens(self):
        self.publish("compiler")
        result = self.search("compiler")
        self.assertNotIn("query_diagnostics", result)
        self.assertNotIn("recall_diagnostics", result)

    def test_long_chinese_prose_retains_complete_problem_phrase(self):
        expected = self.publish("数据库死锁定位", "数据库死锁定位，观察事务锁等待。")
        # Diverse background exceeds the scoring-term budget, but the complete
        # natural-language question is still available for phrase qualification.
        background = "".join(chr(0x4e00 + index) for index in range(180))
        query = background + "请分析数据库死锁定位的问题"
        result = self.search(query)
        self.assertTrue(result["query_diagnostics"]["automatic_safe"])
        self.assertTrue(result["query_diagnostics"]["terms_truncated"])
        self.assertEqual([item["knowledge_id"] for item in result["items"]], [expected["knowledge_id"]])

    def test_path_identity_is_equivalent_across_windows_and_posix_not_basename(self):
        wrong = self.publish("auth.py", "auth.py legacy login")
        expected = self.publish("src/auth.py", "src/auth.py registration")
        for query in (r"src\auth.py", "src/auth.py"):
            with self.subTest(query=query):
                self.assertEqual([item["knowledge_id"] for item in self.search(query)["items"]],
                                 [expected["knowledge_id"]])
        self.assertNotIn(wrong["knowledge_id"], [item["knowledge_id"] for item in self.search(r"src\auth.py")["items"]])
        self.assertIn("c:/src/auth.py", terms(r"C:\src\auth.py"))

    def test_upgrade_rebuilds_old_windows_projection_without_changing_authority(self):
        expected = self.publish(r"src\auth.py", "registration")
        before = list(self.r.db.execute("SELECT id,revision,body FROM knowledge"))
        self.r.db.execute("UPDATE knowledge_index_state SET version=4")
        self.r.db.execute("DELETE FROM knowledge_fts")
        self.r.close()
        from agent_learning.runtime import Runtime
        self.r = Runtime(self.home)
        self.addCleanup(self.r.close)
        self.assertEqual(self.r.db.execute("SELECT version FROM knowledge_index_state").fetchone()[0], INDEX_VERSION)
        self.assertEqual([tuple(row) for row in before],
                         [tuple(row) for row in self.r.db.execute("SELECT id,revision,body FROM knowledge")])
        self.assertEqual(self.search("src/auth.py")["items"][0]["knowledge_id"], expected["knowledge_id"])

    def crowded(self, count):
        expected = self.publish("E_CROWDED", "E_CROWDED repair dependency")
        row = self.r.db.execute("SELECT * FROM knowledge WHERE project=? AND id=?",
                                (self.p, expected["knowledge_id"])).fetchone()
        body = json.loads(row["body"])
        with self.r.transaction():
            for index in range(count):
                identity = "000000-" + str(index).zfill(4)
                invalid = dict(body, title="E_CROWDED", content="E_CROWDED", evidence_refs=["missing-" + str(index)])
                self.r.db.execute("INSERT INTO knowledge(project,id,revision,body,created) VALUES(?,?,?,?,?)",
                                  (self.p, identity, 1, json.dumps(invalid), time.time()))
                self.r.db.execute("INSERT INTO active(project,id,revision) VALUES(?,?,?)", (self.p, identity, 1))
                self.r._index_knowledge(self.p, identity, 1, invalid)
        return expected

    def test_missing_sources_cannot_crowd_out_healthy_result_after_initial_recall(self):
        expected = self.crowded(140)
        result = self.search("E_CROWDED")
        self.assertEqual([item["knowledge_id"] for item in result["items"]], [expected["knowledge_id"]])
        self.assertGreater(result["recall_diagnostics"]["pages"], 1)
        self.assertFalse(result["recall_diagnostics"]["scan_limit_reached"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 141)
        self.assertTrue(all(item["source_health"] == "missing" for item in self.search("E_CROWDED", False)["items"]))

    def test_excessive_unhealthy_recall_is_bounded_and_explained_without_negative_cache(self):
        self.crowded(300)
        result = self.search("E_CROWDED")
        self.assertEqual(result["items"], [])
        diagnostics = result["recall_diagnostics"]
        self.assertTrue(diagnostics["scan_limit_reached"])
        self.assertLessEqual(diagnostics["pages"], 4)
        self.assertLessEqual(diagnostics["examined"], 512)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 0)
        self.assertEqual(self.search("E_CROWDED")["recall_diagnostics"], diagnostics)
        self.assertFalse(self.r.db.in_transaction)

    def test_refill_advances_across_a_page_containing_only_seen_lane_results(self):
        expected = self.crowded(1)
        rows = self.r.db.execute("SELECT * FROM knowledge ORDER BY id").fetchall()
        bad = next(row for row in rows if row["id"] != expected["knowledge_id"])
        good = next(row for row in rows if row["id"] == expected["knowledge_id"])
        def pages(db, project, plan, filters=None, *, page=0, diagnostics=None):
            diagnostics["page_may_have_more"] = page < 2
            return [bad] if page < 2 else [good]
        with patch("agent_learning.knowledge_index.lexical_rows", side_effect=pages) as recall:
            result = self.search("E_CROWDED")
        self.assertEqual([item["knowledge_id"] for item in result["items"]], [expected["knowledge_id"]])
        self.assertEqual(recall.call_count, 3)
        self.assertEqual(result["recall_diagnostics"]["examined"], 2)

    def test_malformed_source_json_is_skipped_without_hiding_explicit_read_failure(self):
        bad = self.publish("E_JSON", "E_JSON")
        good = self.publish("E_JSON healthy", "E_JSON repair dependency")
        self.r.db.execute("UPDATE knowledge_sources SET evidence_refs='{' WHERE knowledge_id=?", (bad["knowledge_id"],))
        result = self.search("E_JSON")
        self.assertEqual([item["knowledge_id"] for item in result["items"]], [good["knowledge_id"]])
        self.assertEqual(result["recall_diagnostics"]["unreadable_records"], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 0)
        with self.assertRaises(json.JSONDecodeError):
            self.r.read(self.p, bad["knowledge_id"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 2)

    def test_malformed_authority_json_does_not_abort_automatic_refill_or_filtered_query(self):
        bad = self.publish("E_BODY", "E_BODY")
        good = self.publish("E_BODY healthy", "E_BODY repair dependency")
        self.r.db.execute("UPDATE knowledge SET body='{' WHERE id=?", (bad["knowledge_id"],))
        result = self.search("E_BODY")
        self.assertEqual([item["knowledge_id"] for item in result["items"]], [good["knowledge_id"]])
        self.assertEqual(result["recall_diagnostics"]["unreadable_records"], 1)
        filtered = self.r.search(self.p, "E_BODY", _automatic=True, filters={"category": "operation"})
        self.assertEqual([item["knowledge_id"] for item in filtered["items"]], [good["knowledge_id"]])
        self.assertFalse(self.r.db.in_transaction)

    def test_database_interruption_is_not_misclassified_as_one_unreadable_record(self):
        self.publish("compiler")
        with patch.object(self.r, "_public", side_effect=sqlite3.OperationalError("interrupted")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "interrupted"):
                self.search("compiler")
        self.assertFalse(self.r.db.in_transaction)

    def test_high_semantic_similarity_cannot_reparse_a_known_unreadable_body(self):
        bad = self.publish("E_BODY", "E_BODY")
        good = self.publish("E_BODY healthy", "E_BODY repair dependency")
        self.r.db.execute("UPDATE knowledge SET body='{' WHERE id=?", (bad["knowledge_id"],))
        row = self.r.db.execute("SELECT * FROM knowledge WHERE id=?", (bad["knowledge_id"],)).fetchone()
        prepared = {"config": {"enabled": False, "automatic_min_similarity": 0},
                    "diagnostics": {}, "vector": [1, 0], "signature": self.r.semantic_signature(self.p)}
        matches = {bad["knowledge_id"]: {"row": row, "score": 1, "rank": 1, "input_truncated": False}}
        with patch.object(self.r, "_semantic_prepare", return_value=prepared), \
                patch.object(self.r, "_semantic_candidates", return_value=matches):
            result = self.search("E_BODY")
        self.assertEqual([item["knowledge_id"] for item in result["items"]], [good["knowledge_id"]])
        self.assertEqual(result["recall_diagnostics"]["unreadable_records"], 1)

    def test_work_history_uses_complete_error_and_path_identity(self):
        for turn, content in (("wrong-error", "管理员设置页面配色主题颜色按钮样式"),
                              ("wrong-file", "auth.py legacy login"),
                              ("correct-error", "E_CRITICAL repair transaction safety"),
                              ("correct-file", "src/auth.py registration")):
            self.r.handle_hook({"hook_event_name": "Stop", "cwd": str(self.workspace), "session_id": "old",
                                "last_assistant_message": content, "turn_id": turn})
        query = "E_CRITICAL" + " filler" * 1500 + " 管理员设置页面配色"
        rows = self.r._work_search_rows(self.p, query, automatic=True)
        summaries = [json.loads(row["body"])["summary"] for row in rows]
        self.assertTrue(summaries)
        self.assertTrue(all("E_CRITICAL" in summary for summary in summaries))
        rows = self.r._work_search_rows(self.p, r"src\auth.py", automatic=True)
        self.assertTrue(rows)
        self.assertTrue(all("src/auth.py" in json.loads(row["body"])["summary"] for row in rows))


if __name__ == "__main__":
    unittest.main()
