"""第四模块：真实 SQLite 验证查询质量和自动注入门槛。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.knowledge_index import terms
from agent_learning.runtime import Runtime


class Module4SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "state"
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.home)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "chat", "分析检索质量", {"mode": "analysis"})
        self.serial = 0

    def tearDown(self):
        self.r.close()
        self.temp.cleanup()

    def publish(self, *, title, content="guidance", applicability="same environment",
                limitations="reported only", project=None, conversation="chat", metadata=None):
        self.serial += 1
        project = project or self.p
        problem = self.r.feedback(project, conversation, f"case {self.serial}", category="operation")
        packet = self.r.review_packet(project, problem["id"])
        candidate = self.r.submit_candidate(
            project, problem_id=problem["id"], problem_revision=problem["revision"],
            title=title, content=content, category="operation", applicability=applicability,
            limitations=limitations, validation_level="reported", metadata=metadata or {},
            evidence_refs=[event["id"] for event in packet["events"]])["candidate_id"]
        return self.r.publish(project, candidate)["knowledge_id"]

    def test_long_chinese_request_keeps_problem_in_middle(self):
        expected = self.publish(title="内存泄漏定位", content="对照分配与释放记录，定位内存泄漏。")
        query = ("请先认真分析执行过程和运行环境，结合现有日志确定应用生命周期，"
                 "我们遇到持续增长的内存泄漏，需定位缓存引用和资源释放的关系，"
                 "最后给出清晰方法，详细说明判断依据和验证步骤。")
        self.assertGreater(len(terms(query)), 24)
        found = self.r.search(self.p, query)
        self.assertEqual(found["items"][0]["knowledge_id"], expected)
        self.assertIn("内存", found["items"][0]["match"]["matched_terms"])
        self.assertIn("泄漏", found["items"][0]["match"]["matched_terms"])
        self.assertTrue(found["items"][0]["match"]["auto_eligible"])

    def test_complete_problem_coverage_beats_repeated_generic_word(self):
        generic = self.publish(title="Python installation", content="Python install instructions",
                               applicability="Python", limitations="Python",
                               metadata={"tool": "Python", "environment": "Python"})
        expected = self.publish(title="diagnosis", content="Python memory leak")
        items = self.r.search(self.p, "Please carefully find Python memory leak")["items"]
        self.assertEqual([item["knowledge_id"] for item in items], [expected, generic])
        self.assertEqual(items[0]["match"]["coverage"], 1)
        self.assertTrue(items[0]["match"]["auto_eligible"])
        self.assertFalse(items[1]["match"]["auto_eligible"])

    def test_chinese_request_word_does_not_recall_unrelated_spreadsheet(self):
        self.publish(title="检查电子表格", content="Excel cell formatting")
        self.assertEqual(self.r.search(self.p, "查询C++崩溃")["items"], [])
        crash = self.publish(title="C++崩溃", content="读取崩溃栈与错误地址。")
        item = self.r.search(self.p, "查询C++崩溃")["items"][0]
        self.assertEqual(item["knowledge_id"], crash)
        self.assertTrue(item["match"]["auto_eligible"])

    def test_identifier_components_share_recall_and_ranking_tokens(self):
        expected = self.publish(title="python.exe", content="inspect interpreter path")
        for query in ("python", "python.exe", "exe"):
            with self.subTest(query=query):
                item = self.r.search(self.p, query)["items"][0]
                self.assertEqual(item["knowledge_id"], expected)
                self.assertGreater(item["match"]["score"], 0)
                self.assertTrue(item["match"]["auto_eligible"])
        self.assertNotIn("c", terms("C++ C#"))

    def test_single_character_queries_remain_manual_only(self):
        for title in ("C", "R", "帧预算"):
            expected = self.publish(title=title)
            query = title if title in {"C", "R"} else "帧"
            with self.subTest(query=query):
                item = self.r.search(self.p, query)["items"][0]
                self.assertEqual(item["knowledge_id"], expected)
                self.assertFalse(item["match"]["auto_eligible"])

    def test_other_project_and_cache_hit_preserve_matches_and_ranking(self):
        self.publish(title="alpha", content="partial")
        expected = self.publish(title="diagnosis", content="alpha beta")
        self.r.db.execute("DELETE FROM cache")
        initial = self.r.search(self.p, "alpha beta")
        other = self.r.project(Path(self.temp.name) / "other")
        self.r.task_update(other, "foreign", "other project", {"mode": "analysis"})
        self.publish(project=other, conversation="foreign", title="alpha " * 50, content="beta " * 50)
        with patch("agent_learning.knowledge_index._rank", side_effect=AssertionError("cached reranking")):
            cached = self.r.search(self.p, "alpha beta")
        self.assertEqual(cached["items"], initial["items"])
        missed = self.r.search(self.p, "alpha beta", limit=10)
        self.assertEqual(missed["items"], initial["items"])
        self.assertEqual(missed["items"][0]["knowledge_id"], expected)
        for row in self.r.db.execute("SELECT body FROM cache"):
            self.assertTrue(all(set(reference) == {"knowledge_id", "revision"}
                                for reference in json.loads(row[0])))

    def test_match_explanation_is_bounded_for_long_exact_query(self):
        words = [f"errorcode{index}" for index in range(160)]
        self.publish(title="diagnosis", content=" ".join(words))
        item = self.r.search(self.p, " ".join(words))["items"][0]
        self.assertEqual(item["match"]["coverage"], 1)
        self.assertLessEqual(len(item["match"]["matched_terms"]), 12)
        self.assertTrue(item["match"]["auto_eligible"])

    def test_cache_reference_to_unrelated_active_record_is_rejected(self):
        expected = self.publish(title="memory leak")
        unrelated = self.publish(title="spreadsheet formatting")
        self.r.db.execute("DELETE FROM cache")
        self.r.search(self.p, "memory leak")
        key = self.r.db.execute("SELECT key FROM cache").fetchone()[0]
        self.r.db.execute("UPDATE cache SET body=? WHERE key=?",
                          (json.dumps([{"knowledge_id": unrelated, "revision": 1}]), key))
        item = self.r.search(self.p, "memory leak")["items"][0]
        self.assertEqual(item["knowledge_id"], expected)
        self.assertGreater(item["match"]["score"], 0)


if __name__ == "__main__":
    unittest.main()
