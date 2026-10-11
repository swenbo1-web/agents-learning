"""检索修订一致性、项目独立排名和可重建投影的真实 SQLite 回归。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.knowledge_index import INDEX_VERSION, terms
from agent_learning.runtime import ContractError, Runtime


class KnowledgeIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "state"
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.home)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "chat", "检查知识检索", {"mode": "analysis"})
        self.serial = 0

    def tearDown(self):
        self.r.close()
        self.temp.cleanup()

    def candidate(self, *, project=None, conversation="chat", title="record", content="guidance",
                  applicability="same setup", limitations="reported only", category="operation",
                  kind="solution", metadata=None):
        self.serial += 1
        project = project or self.p
        problem = self.r.feedback(project, conversation, f"case {self.serial}", category=category)
        packet = self.r.review_packet(project, problem["id"])
        return self.r.submit_candidate(project, problem_id=problem["id"], problem_revision=problem["revision"],
                                       title=title, content=content, category=category, knowledge_kind=kind,
                                       applicability=applicability, limitations=limitations,
                                       evidence_refs=[event["id"] for event in packet["events"]],
                                       validation_level="reported", metadata=metadata or {})["candidate_id"]

    def publish(self, **body):
        project = body.get("project") or self.p
        return self.r.publish(project, self.candidate(**body))

    def concurrent_update(self, prime_cache):
        published = self.publish(title="compiler", content="alpha", metadata={"environment": "A"})
        updated = self.candidate(title="graphics", content="beta", metadata={"environment": "B"})
        if prime_cache:
            self.r.search(self.p, "compiler", filters={"environment": "A"})
        writer = Runtime(self.home)
        original_public = self.r._public
        calls = []

        def interleave(project, row):
            if not calls:
                calls.append(writer.publish(self.p, updated, knowledge_id=published["knowledge_id"], expected_revision=1))
            return original_public(project, row)

        try:
            with patch.object(self.r, "_public", side_effect=interleave):
                found = self.r.search(self.p, "compiler", filters={"environment": "A"})
            self.assertEqual(calls[0]["revision"], 2)
            self.assertEqual(found["generation"], 1)
            self.assertEqual(found["items"][0]["revision"], 1)
            self.assertEqual(found["items"][0]["metadata"]["environment"], "A")
            self.assertEqual(found["items"][0]["title"], "compiler")
            self.assertEqual(self.r.read(self.p, published["knowledge_id"])["revision"], 2)
            after = self.r.search(self.p, "compiler", filters={"environment": "A"})
            self.assertEqual(after["generation"], 2)
            self.assertEqual(after["items"], [])
            self.assertFalse(self.r.db.in_transaction)
        finally:
            writer.close()

    def test_query_materializes_original_revision_during_concurrent_publish(self):
        self.concurrent_update(prime_cache=False)

    def test_cache_materializes_original_revision_during_concurrent_publish(self):
        self.concurrent_update(prime_cache=True)

    def test_cache_lock_after_snapshot_does_not_fail_completed_search(self):
        published = self.publish(title="compiler", content="alpha", metadata={"environment": "A"})
        updated = self.candidate(title="graphics", content="beta", metadata={"environment": "B"})
        writer = Runtime(self.home)
        self.r.db.execute("PRAGMA busy_timeout=1")
        original_public = self.r._public
        calls = []

        def interleave(project, row):
            if not calls:
                calls.append(writer.publish(self.p, updated, knowledge_id=published["knowledge_id"], expected_revision=1))
                writer.db.execute("BEGIN IMMEDIATE")
            return original_public(project, row)

        try:
            with patch.object(self.r, "_public", side_effect=interleave):
                found = self.r.search(self.p, "compiler", filters={"environment": "A"})
            self.assertEqual(found["generation"], 1)
            self.assertEqual(found["items"][0]["revision"], 1)
            self.assertEqual(found["items"][0]["metadata"]["environment"], "A")
            self.assertFalse(self.r.db.in_transaction)
            self.assertTrue(writer.db.in_transaction)
        finally:
            if writer.db.in_transaction:
                writer.db.execute("ROLLBACK")
            writer.close()

    def test_cache_binds_revisions_and_rechecks_active_and_filters(self):
        published = self.publish(title="compiler", metadata={"environment": "A"})
        self.r.db.execute("DELETE FROM cache")
        self.r.search(self.p, "compiler", filters={"environment": "A"})
        cache = self.r.db.execute("SELECT key,body FROM cache").fetchone()
        self.assertEqual(json.loads(cache["body"]), [{"knowledge_id": published["knowledge_id"], "revision": 1}])
        wrong = self.publish(title="graphics", metadata={"environment": "B"})
        self.r.db.execute("DELETE FROM cache")
        self.r.search(self.p, "compiler", filters={"environment": "A"})
        key = self.r.db.execute("SELECT key FROM cache").fetchone()[0]
        self.r.db.execute("UPDATE cache SET body=? WHERE key=?",
                          (json.dumps([{"knowledge_id": wrong["knowledge_id"], "revision": 1}]), key))
        found = self.r.search(self.p, "compiler", filters={"environment": "A"})
        self.assertEqual(found["items"][0]["knowledge_id"], published["knowledge_id"])
        self.r.db.execute("UPDATE cache SET body=? WHERE key=?",
                          (json.dumps([{"knowledge_id": published["knowledge_id"], "revision": 99}]), key))
        self.assertEqual(self.r.search(self.p, "compiler", filters={"environment": "A"})["items"][0]["revision"], 1)

    def test_other_projects_do_not_change_ranking_or_cache_behavior(self):
        self.publish(title="record alpha", content="diagnosis one")
        self.publish(title="record beta", content="diagnosis two")
        self.r.db.execute("DELETE FROM cache")
        initial = self.r.search(self.p, "alpha beta", limit=5)
        expected = [item["knowledge_id"] for item in initial["items"]]
        other = self.r.project(Path(self.temp.name) / "other")
        self.r.task_update(other, "other-chat", "foreign task", {"mode": "analysis"})
        self.publish(project=other, conversation="other-chat", title="alpha " * 100, content="foreign text")
        with patch("agent_learning.knowledge_index._rank", side_effect=AssertionError("cached query must not rerank")):
            cached = self.r.search(self.p, "alpha beta", limit=5)
        missed = self.r.search(self.p, "alpha beta", limit=10)
        self.assertEqual(cached["generation"], initial["generation"])
        self.assertEqual([item["knowledge_id"] for item in cached["items"]], expected)
        self.assertEqual([item["knowledge_id"] for item in missed["items"]], expected)
        self.assertEqual(self.r.search(self.p, "alpha beta", limit=1)["items"][0]["knowledge_id"], expected[0])

    def test_technical_identifiers_remain_distinct_and_single_chinese_is_searchable(self):
        ids = {}
        for language in ("C++", "C#", "C", "R"):
            ids[language] = self.publish(title=language, content="language-specific guidance")["knowledge_id"]
        for language, knowledge_id in ids.items():
            with self.subTest(language=language):
                found = self.r.search(self.p, language)
                self.assertEqual([item["knowledge_id"] for item in found["items"]], [knowledge_id])
        chinese = self.publish(title="帧预算", content="检查时间分配")["knowledge_id"]
        self.assertEqual(self.r.search(self.p, "帧")["items"][0]["knowledge_id"], chinese)
        self.assertNotIn("c", terms("C++ C#"))
        self.assertEqual(terms("C++20"), ["cplusplus", "cplusplus20"])

    def test_limitations_are_indexed_and_classification_filters_are_exact(self):
        solution = self.publish(title="compiler", limitations="only Ubuntu22", kind="solution", category="operation")
        self.publish(title="compiler", limitations="other environment", kind="pattern", category="professional")
        self.assertEqual(self.r.search(self.p, "Ubuntu22")["items"][0]["knowledge_id"], solution["knowledge_id"])
        filtered = self.r.search(self.p, "compiler", filters={"knowledge_kind": "solution", "category": "operation"})
        self.assertEqual([item["knowledge_id"] for item in filtered["items"]], [solution["knowledge_id"]])
        with self.assertRaises(ContractError):
            self.r.search(self.p, "compiler", filters={"unsupported": "value"})
        with self.assertRaises(ContractError):
            self.r.search(self.p, "compiler", filters={"knowledge_kind": "invented"})

    def test_search_inside_existing_write_transaction_keeps_ownership(self):
        published = self.publish(title="compiler")
        before = self.r.db.execute("SELECT value FROM metrics WHERE project=? AND name='search_queries'", (self.p,)).fetchone()[0]
        try:
            with self.r.transaction():
                found = self.r.search(self.p, "compiler")
                self.assertEqual(found["items"][0]["knowledge_id"], published["knowledge_id"])
                self.assertTrue(self.r.db.in_transaction)
                raise RuntimeError("rollback surrounding hook")
        except RuntimeError:
            pass
        after = self.r.db.execute("SELECT value FROM metrics WHERE project=? AND name='search_queries'", (self.p,)).fetchone()[0]
        self.assertEqual(after, before)
        hooked = self.r.handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "chat",
                                    "cwd": str(self.workspace), "prompt": "compiler", "turn_id": "nested"})
        self.assertIn(published["knowledge_id"], hooked["hookSpecificOutput"]["additionalContext"])

    def test_rebuild_removes_history_and_stale_sensitive_projection(self):
        published = self.publish(title="compiler", content="safe guidance")
        updated = self.candidate(title="graphics", content="correct guidance")
        self.r.publish(self.p, updated, knowledge_id=published["knowledge_id"], expected_revision=1)
        self.r.db.execute("INSERT INTO knowledge_fts VALUES(?,?,?,?)",
                          (self.p, published["knowledge_id"], 1, "SYNTHETIC_OLD_TOKEN"))
        self.r.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?)", ("old-cache", "[]"))
        rebuilt = self.r.rebuild_index()
        self.assertEqual(rebuilt, {"status": "rebuilt", "indexed": 1, "index_version": INDEX_VERSION})
        indexed = self.r.db.execute("SELECT revision,text FROM knowledge_fts").fetchall()
        self.assertEqual(len(indexed), 1)
        self.assertEqual(int(indexed[0]["revision"]), 2)
        self.assertNotIn("SYNTHETIC_OLD_TOKEN", indexed[0]["text"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 0)
        self.assertEqual(self.r.search(self.p, "SYNTHETIC_OLD_TOKEN")["items"], [])
        self.assertEqual(self.r.search(self.p, "graphics")["items"][0]["revision"], 2)
        self.assertEqual(self.r.db.execute("SELECT version FROM knowledge_index_state WHERE id=1").fetchone()[0], INDEX_VERSION)

    def test_publish_and_revoke_keep_only_active_projection(self):
        published = self.publish(title="compiler")
        update = self.candidate(title="graphics", content="replacement")
        self.r.publish(self.p, update, knowledge_id=published["knowledge_id"], expected_revision=1)
        rows = self.r.db.execute("SELECT revision FROM knowledge_fts WHERE project=? AND id=?",
                                 (self.p, published["knowledge_id"])).fetchall()
        self.assertEqual([int(row[0]) for row in rows], [2])
        self.assertEqual(self.r.search(self.p, "compiler")["items"], [])
        self.r.revoke(self.p, published["knowledge_id"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM knowledge_fts WHERE project=?", (self.p,)).fetchone()[0], 0)
        self.assertEqual(self.r.search(self.p, "graphics")["items"], [])


if __name__ == "__main__":
    unittest.main()
