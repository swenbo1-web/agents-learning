"""Complete file identities share recall and scoring; all evidence is synthetic."""
import json
import unittest
from unittest.mock import patch

from agent_learning.knowledge_index import INDEX_VERSION, match_terms, normalized_query_plan, terms
from agent_learning.query_language import canonical_text, matched_labels
from agent_learning.runtime import Runtime, digest
from tests import test_module4_context as fixture


class RetrievalIdentityTests(unittest.TestCase):
    setUp = fixture.Module4ContextTests.setUp
    publish = fixture.Module4ContextTests.publish

    def ids(self, query):
        return [item["knowledge_id"] for item in self.r.search(self.p, query, _automatic=True)["items"]]

    def test_unicode_paths_do_not_borrow_a_basename_or_another_directory(self):
        wrong = self.publish("other/auth.py", "auth.py legacy login repair")
        expected = self.publish("src/中文/auth.py", "registration repair")
        for query in ("src/中文/auth.py", r"src\中文\auth.py"):
            with self.subTest(query=query):
                self.assertEqual(normalized_query_plan(query)["exact_anchors"], ["src/中文/auth.py"])
                self.assertEqual(self.ids(query), [expected["knowledge_id"]])
                self.assertNotIn(wrong["knowledge_id"], self.ids(query))

    def test_windows_unicode_drive_remains_part_of_identity(self):
        self.publish("auth.py", "auth.py legacy setup")
        expected = self.publish(r"C:\项目\auth.py", "current setup")
        self.assertEqual(self.ids("c:/项目/auth.py"), [expected["knowledge_id"]])

    def test_non_chinese_unicode_path_segments_remain_complete(self):
        self.publish("auth.py", "auth.py legacy repair")
        for path in ("src/café/auth.py", "src/cafe\u0301/auth.py", "src/данные/auth.py", "src/データ/auth.py"):
            with self.subTest(path=path):
                self.assertEqual(normalized_query_plan(path)["exact_anchors"], [path])
                expected = self.publish(path, "current registration repair")
                self.assertEqual(self.ids(path), [expected["knowledge_id"]])

    def test_dotfiles_do_not_match_ordinary_words_or_another_parent(self):
        self.publish("env", "ordinary env command setup")
        self.publish("other/.env", "different environment file")
        expected = self.publish("src/.env", "current environment file")
        self.assertEqual(self.ids(".env"), [])
        self.assertEqual(self.ids("src/.env"), [expected["knowledge_id"]])
        bare = self.publish(".env", "root environment file")
        self.assertEqual(self.ids(".env"), [bare["knowledge_id"]])

    def test_chinese_dotfile_and_filename_are_not_translated(self):
        for path in (".管理员", "知识管理.py", "文档/管理员.md", "src/.知识"):
            with self.subTest(path=path):
                self.assertEqual(canonical_text(path), path)
                self.assertIn(path.lower(), terms(path))
                self.assertIn(path.lower(), match_terms(path))
                expected = self.publish(path, "preserve the complete file identity")
                self.assertEqual(self.ids(path), [expected["knowledge_id"]])

    def test_unicode_components_keep_natural_language_searchability(self):
        expected = self.publish("src/用户管理.py", "recorded file observation")
        self.assertIn("用户", terms("src/用户管理.py"))
        self.assertIn(expected["knowledge_id"], [item["knowledge_id"]
            for item in self.r.search(self.p, "用户管理")["items"]])

    def test_high_semantic_similarity_cannot_drop_the_unicode_parent(self):
        wrong = self.publish("auth.py", "auth.py legacy login repair")
        expected = self.publish("src/中文/auth.py", "current registration repair")
        row = self.r.db.execute("SELECT * FROM knowledge WHERE id=?", (wrong["knowledge_id"],)).fetchone()
        prepared = {"config": {"enabled": False, "automatic_min_similarity": 0},
                    "diagnostics": {}, "vector": [1, 0], "signature": self.r.semantic_signature(self.p)}
        matches = {wrong["knowledge_id"]: {"row": row, "score": 1, "rank": 1, "input_truncated": False}}
        with patch.object(self.r, "_semantic_prepare", return_value=prepared), \
                patch.object(self.r, "_semantic_candidates", return_value=matches):
            self.assertEqual(self.ids("src/中文/auth.py"), [expected["knowledge_id"]])

    def test_work_history_uses_the_same_complete_path_and_dotfile_gate(self):
        for number, text in enumerate(("auth.py legacy repair", "src/中文/auth.py current repair",
                                       "env command setup", ".env recorded settings")):
            self.r.handle_hook({"hook_event_name": "Stop", "cwd": str(self.workspace),
                "session_id": "previous", "turn_id": str(number), "last_assistant_message": text})
        for query in ("src/中文/auth.py", ".env"):
            rows = self.r._work_search_rows(self.p, query, automatic=True)
            self.assertTrue(rows)
            self.assertTrue(all(query in json.loads(row["body"])["summary"] for row in rows))

    def test_unicode_identity_overflow_and_long_input_remain_manual_only(self):
        query = " ".join(f"src/目录{number}/auth.py" for number in range(129))
        plan = normalized_query_plan(query)
        self.assertTrue(plan["query_diagnostics"]["identity_limit_exceeded"])
        self.assertFalse(plan["automatic_safe"])
        self.assertFalse(normalized_query_plan(".env " + " " * 65536)["automatic_safe"])
        with patch.object(self.r, "_semantic_prepare", side_effect=AssertionError("unsafe query called provider")):
            self.assertEqual(self.ids(query), [])

    def test_old_projection_rebuilds_without_rewriting_authority(self):
        expected = self.publish("src/中文/auth.py", "current registration repair")
        before = [tuple(row) for row in self.r.db.execute("SELECT id,revision,body FROM knowledge")]
        self.r.db.execute("UPDATE knowledge_fts SET text='auth py legacy'")
        self.r.db.execute("UPDATE knowledge_index_state SET version=? WHERE id=1", (INDEX_VERSION - 1,))
        self.r.close()
        self.r = Runtime(self.home)
        self.addCleanup(self.r.close)
        self.assertEqual(before, [tuple(row) for row in self.r.db.execute("SELECT id,revision,body FROM knowledge")])
        self.assertEqual(self.ids("src/中文/auth.py"), [expected["knowledge_id"]])

    def test_previous_match_policy_cache_cannot_hide_a_complete_unicode_identity(self):
        expected = self.publish("src/中文/auth.py", "current registration repair")
        previous_plan = dict(normalized_query_plan("src/中文/auth.py"), match_policy_version=3)
        manifest = dict(self.r.db.execute(
            "SELECT generation,revocation_epoch,source_epoch FROM projects WHERE id=?", (self.p,)).fetchone())
        source_epoch = manifest.pop("source_epoch")
        old_key = digest([self.p, previous_plan, 5, {}, manifest, "local-fts", INDEX_VERSION,
                          True, source_epoch, None])
        self.r.db.execute("INSERT OR REPLACE INTO cache VALUES(?,'[]')", (old_key,))
        self.assertEqual(self.ids("src/中文/auth.py"), [expected["knowledge_id"]])
        self.assertEqual(self.r.db.execute("SELECT body FROM cache WHERE key=?", (old_key,)).fetchone()[0], "[]")

    def test_context_reuses_one_plan_for_the_same_effective_question(self):
        self.r.config["work_memory"]["automatic_context"] = False
        handle = self.r.register_context(self.p, "chat")["context_handle"]
        with patch("agent_learning.knowledge_index.normalized_query_plan", wraps=normalized_query_plan) as plans, \
                patch.object(self.r, "search", return_value={"items": []}):
            self.r._context(self.p, "chat", query="src/中文/auth.py", context_handle=handle,
                            workspace=self.workspace)
        self.assertEqual(plans.call_count, 1)


class QueryLanguageBoundaryTests(unittest.TestCase):
    def test_long_alias_text_preserves_every_technical_identifier(self):
        source = '文档/管理员.md .管理员 project_admin 管理员：检查项目知识。'
        expected = '文档/管理员.md .管理员 project_admin  administrator ：检查 project  knowledge 。'
        self.assertEqual(canonical_text(source * 1000), expected * 1000)

    def test_alias_labels_do_not_translate_inside_paths_or_dotfiles(self):
        labels = matched_labels("文档/管理员.md .管理员 管理员", ["administrator"], terms)
        self.assertEqual(labels, ["管理", "理员"])


if __name__ == "__main__":
    unittest.main()
