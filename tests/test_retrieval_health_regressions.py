"""Real SQLite regressions for useful retrieval without unrelated automatic cards."""
import unittest
from unittest.mock import patch

from agent_learning.knowledge_index import lexical_rows, normalized_query_plan
from tests import test_module4_context as context_fixture
from tests import test_hybrid_retrieval as hybrid_fixture


class RetrievalHealthTests(unittest.TestCase):
    setUp = context_fixture.Module4ContextTests.setUp
    hook = context_fixture.Module4ContextTests.hook
    publish = context_fixture.Module4ContextTests.publish
    text = context_fixture.Module4ContextTests.text

    def ids(self, query, automatic=True):
        return [item["knowledge_id"] for item in self.r.search(
            self.p, query, _automatic=automatic)["items"]]

    def test_chinese_generic_settings_phrase_cannot_override_wrong_error_code(self):
        unrelated = self.publish("管理员设置页面配色", content="只说明界面主题颜色和按钮样式，不涉及认证、签名或保存逻辑")
        query = "修复 E_AUTH_SIGNATURE_INVALID 管理员设置页面切换模型后无法保存选项的问题"
        self.assertIn(unrelated["knowledge_id"], self.ids(query, False))
        self.assertEqual(self.ids(query), [])
        self.assertNotIn(unrelated["knowledge_id"], self.text(self.hook(
            "UserPromptSubmit", prompt=query, turn_id="auth")))

    def test_generic_settings_phrase_alone_cannot_locate_different_chinese_issue(self):
        self.publish("管理员设置页面配色", content="只说明界面主题颜色和按钮样式")
        self.assertEqual(self.ids("修复管理员设置页面切换模型后无法保存选项的问题"), [])

    def test_long_english_exact_error_is_not_diluted_by_background(self):
        expected = self.publish("E_IMPORT", content="E_IMPORT resolution: repair installed dependency")
        query = ("Please analyze the project build pipeline because Python dependency import fails "
                 "during deployment with E_IMPORT on Windows after package changes")
        self.assertEqual(self.ids(query), [expected["knowledge_id"]])

    def test_background_count_versions_and_language_do_not_veto_error_identity(self):
        expected = self.publish("E_IMPORT", content="repair installed dependency")
        query = "After 3 retries in C++ and Python 3.10 or 3.12, investigate E_IMPORT and explain the cause"
        self.assertEqual(normalized_query_plan(query)["exact_anchors"], ["e_import"])
        self.assertEqual(self.ids(query), [expected["knowledge_id"]])

    def test_matching_error_components_do_not_equal_a_complete_error_code(self):
        self.publish("E_AUTH_EXPIRED", content="AUTH SIGNATURE INVALID troubleshooting")
        self.assertEqual(self.ids("E_AUTH_SIGNATURE_INVALID"), [])

    def test_identifier_only_in_limitations_does_not_grant_automatic_reference(self):
        item = self.publish("deployment reference", content="General installation steps",
                            limitations="Not applicable to E_IMPORT")
        self.assertIn(item["knowledge_id"], self.ids("E_IMPORT", False))
        self.assertEqual(self.ids("E_IMPORT"), [])

    def test_hyphenated_error_and_hresult_are_specific_in_long_questions(self):
        for code in ("AUTH-019", "0x80073D02"):
            with self.subTest(code=code):
                expected = self.publish(code, content="inspect the recorded service error")
                query = "Please inspect deployment service activation on Windows after package changes because " + code
                self.assertEqual(self.ids(query), [expected["knowledge_id"]])

    def test_case_folding_preserves_full_identifier(self):
        expected = self.publish("e_auth_signature_invalid", content="inspect signing key")
        self.assertEqual(self.ids("Please inspect E_AUTH_SIGNATURE_INVALID"), [expected["knowledge_id"]])

    def test_high_coverage_cannot_bypass_missing_error_in_local_fts(self):
        self.publish("release retained buffers", content="release retained memory")
        self.assertEqual(self.ids("release retained buffers E999"), [])

    def test_all_explicit_issue_and_file_identifiers_must_match(self):
        incomplete = self.publish("E_IMPORT", content="inspect dependency")
        expected = self.publish("src/auth.py E_IMPORT", content="repair import registration")
        query = "Please inspect deployment and resolve E_IMPORT inside src/auth.py after recent changes"
        self.assertEqual(self.ids(query), [expected["knowledge_id"]])
        self.assertIn(incomplete["knowledge_id"], self.ids(query, False))

    def test_full_file_path_is_an_anchor_even_in_long_question(self):
        wrong = self.publish("src/other.py", content="src auth file repair")
        expected = self.publish("src/auth.py", content="repair registration")
        query = "Please inspect the project deployment because requests stop after changes inside src/auth.py"
        self.assertEqual(self.ids(query), [expected["knowledge_id"]])
        self.assertNotIn(wrong["knowledge_id"], self.ids(query))

    def test_exact_identifier_after_many_words_survives_query_term_budget(self):
        expected = self.publish("E_IMPORT", content="repair dependency")
        query = " ".join("background" + chr(97 + i % 26) + chr(97 + i // 26) for i in range(150)) + " E_IMPORT"
        self.assertIn("e_import", normalized_query_plan(query)["terms"])
        self.assertEqual(self.ids(query), [expected["knowledge_id"]])

    def test_ordinary_short_question_and_technical_bilingual_concept_remain_useful(self):
        compiler = self.publish("compiler", content="inspect compilation")
        leak = self.publish("内存泄漏", content="定位分配与释放记录")
        self.assertEqual(self.ids("compiler"), [compiler["knowledge_id"]])
        self.assertEqual(self.ids("memory leak"), [leak["knowledge_id"]])

    def test_repeated_search_with_an_active_cursor_does_not_register_sqlite_function(self):
        expected = self.publish("E_IMPORT")
        self.publish("E_NETWORK")
        cursor = self.r.db.execute("SELECT body FROM knowledge ORDER BY id")
        self.addCleanup(cursor.close)
        self.assertIsNotNone(cursor.fetchone())
        self.r.db.execute("DELETE FROM cache")
        self.assertEqual(self.ids("E_IMPORT"), [expected["knowledge_id"]])
        self.r.db.execute("DELETE FROM cache")
        self.assertEqual(self.ids("E_IMPORT"), [expected["knowledge_id"]])
        self.assertIsNotNone(cursor.fetchone())

    def test_old_negative_cache_is_not_reused_for_new_match_policy(self):
        from agent_learning.runtime import digest
        expected = self.publish("E_IMPORT")
        plan = normalized_query_plan("E_IMPORT")
        legacy_plan = {key: value for key, value in plan.items()
                       if key not in {"match_policy_version", "exact_anchors"}}
        manifest = dict(self.r.db.execute("SELECT generation,revocation_epoch,source_epoch FROM projects WHERE id=?",
                                        (self.p,)).fetchone())
        source_epoch = manifest.pop("source_epoch")
        legacy_key = digest([self.p, legacy_plan, 5, {}, manifest, "local-fts", 4, True, source_epoch, None])
        self.r.db.execute("DELETE FROM cache")
        self.r.db.execute("INSERT INTO cache VALUES(?,'[]')", (legacy_key,))
        self.assertEqual(plan["match_policy_version"], 2)
        self.assertEqual(self.ids("E_IMPORT"), [expected["knowledge_id"]])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 2)
        self.assertEqual(self.r.db.execute("SELECT body FROM cache WHERE key=?", (legacy_key,)).fetchone()[0], "[]")

    def test_compaction_does_not_restore_an_unrelated_old_card(self):
        old = self.publish("unrelatedexcelcharts", content="Excel chart colors and cell formatting")
        self.assertIn(old["knowledge_id"], self.text(self.hook(
            "UserPromptSubmit", prompt="unrelatedexcelcharts", turn_id="old")))
        self.hook("UserPromptSubmit", prompt="postgresdeadlockprobe", turn_id="current")
        text = self.text(self.hook("SessionStart", source="compact"))
        self.assertNotIn(old["knowledge_id"], text)
        # Exposure remains tracked so revocation notifications can still reach the session.
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_exposures").fetchone()[0], 1)

    def test_compaction_restores_current_relevant_knowledge(self):
        expected = self.publish("compiler", content="inspect compilation")
        self.hook("UserPromptSubmit", prompt="compiler", turn_id="current")
        text = self.text(self.hook("SessionStart", source="compact"))
        self.assertIn(expected["knowledge_id"], text)

    def test_compaction_restores_current_query_while_old_fault_waits_for_tool(self):
        old = self.publish("E_IMPORT", content="repair dependency")
        current = self.publish("compiler", content="inspect compilation")
        self.hook("PreToolUse", tool_use_id="old-fault", tool_name="Bash")
        self.hook("PostToolUse", tool_use_id="old-fault", tool_name="Bash",
                  tool_response={"exit_code": 1, "stderr": "E_IMPORT"})
        self.hook("UserPromptSubmit", prompt="compiler", turn_id="current")
        text = self.text(self.hook("SessionStart", source="compact"))
        self.assertIn(current["knowledge_id"], text)
        self.assertNotIn(old["knowledge_id"], text)
        self.assertEqual(self.r.db.execute("SELECT state FROM context_faults").fetchone()[0], "pending")
        tool = self.text(self.hook("PreToolUse", tool_use_id="after-compact", tool_name="Read"))
        self.assertIn(old["knowledge_id"], tool)

    def test_compaction_does_not_use_old_fault_to_restore_unrelated_current_focus(self):
        old = self.publish("E_IMPORT", content="repair dependency")
        self.hook("UserPromptSubmit", prompt="E_IMPORT", turn_id="old")
        self.hook("PreToolUse", tool_use_id="old-fault", tool_name="Bash")
        self.hook("PostToolUse", tool_use_id="old-fault", tool_name="Bash",
                  tool_response={"exit_code": 1, "stderr": "E_IMPORT"})
        self.hook("UserPromptSubmit", prompt="unrelatedspreadsheet", turn_id="current")
        text = self.text(self.hook("SessionStart", source="compact"))
        self.assertNotIn(old["knowledge_id"], text)

    def test_compaction_never_resurrects_conflicted_or_revoked_source(self):
        expected = self.publish("compiler", success=True)
        self.hook("UserPromptSubmit", prompt="compiler", turn_id="current")
        self.hook("PostToolUse", tool_use_id="compiler", tool_name="Bash", tool_response={"exit_code": 1})
        text = self.text(self.hook("SessionStart", source="compact"))
        self.assertNotIn('"knowledge_id":"' + expected["knowledge_id"] + '"', text)
        self.assertIn("health=conflicted", text)

    def test_compaction_keeps_revocation_notice_without_restoring_old_card(self):
        expected = self.publish("compiler")
        self.hook("UserPromptSubmit", prompt="compiler", turn_id="current")
        self.r.revoke(self.p, expected["knowledge_id"], expected_revision=1)
        text = self.text(self.hook("SessionStart", source="compact"))
        self.assertNotIn('"knowledge_id":"' + expected["knowledge_id"] + '"', text)
        self.assertIn("已撤销", text)

    def test_missing_match_metadata_cannot_bypass_automatic_gate(self):
        expected = self.publish("compiler")
        item = self.r.read(self.p, expected["knowledge_id"])
        with patch.object(self.r, "search", return_value={"items": [item]}):
            text = self.text(self.hook("UserPromptSubmit", prompt="differenttopic", turn_id="missing-match"))
        self.assertNotIn(expected["knowledge_id"], text)

    def test_lexical_recall_is_bounded_and_has_separate_exact_lane(self):
        self.publish("E_IMPORT")
        traced = []
        self.r.db.set_trace_callback(traced.append)
        rows = lexical_rows(self.r.db, self.p, normalized_query_plan("pipeline build E_IMPORT"))
        self.assertLessEqual(len(rows), 128)
        reads = [query for query in traced if "knowledge_fts MATCH" in query]
        self.assertEqual(len(reads), 2)
        self.assertTrue(all("LIMIT 64" in query for query in reads))
        self.assertTrue(all("bm25(knowledge_fts)" in query for query in reads))

    def test_exact_lane_survives_a_small_recall_budget_with_many_generic_matches(self):
        for index in range(6):
            self.publish("deployment generic" + str(index), content="build pipeline project deployment Windows dependency package")
        expected = self.publish("E_IMPORT", content="repair registration")
        with patch("agent_learning.knowledge_index._LEXICAL_CANDIDATES", 4):
            found = self.ids("Please inspect build pipeline project deployment Windows dependency package E_IMPORT")
        self.assertEqual(found, [expected["knowledge_id"]])


class HybridRetrievalHealthTests(unittest.TestCase):
    setUp = hybrid_fixture.HybridRetrievalTests.setUp
    tearDown = hybrid_fixture.HybridRetrievalTests.tearDown
    candidate = hybrid_fixture.HybridRetrievalTests.candidate
    publish = hybrid_fixture.HybridRetrievalTests.publish

    def limited(self):
        self.serial += 1
        problem = self.r.feedback(self.p, "chat", "limited reference", category="operation")
        packet = self.r.review_packet(self.p, problem["id"])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"],
            problem_revision=problem["revision"], title="General deployment reference",
            content="General installation steps", applicability="same environment",
            limitations="Not applicable to E_IMPORT", category="operation", validation_level="reported",
            evidence_refs=[event["id"] for event in packet["events"]])
        return self.r.publish(self.p, candidate["candidate_id"])["knowledge_id"]

    def test_semantic_score_cannot_promote_a_limitations_only_identifier(self):
        self.provider.response = {"embeddings": [[1, 0]]}
        expected = self.limited()
        self.r.semantic_refresh(self.p)
        result = self.r.search(self.p, "E_IMPORT")
        self.assertEqual(result["items"][0]["knowledge_id"], expected)
        self.assertEqual(result["items"][0]["match"]["semantic_score"], 1)
        self.assertFalse(result["items"][0]["match"]["auto_eligible"])
        self.assertEqual(self.r.search(self.p, "E_IMPORT", _automatic=True)["items"], [])

    def test_degraded_semantic_does_not_promote_a_limitations_only_identifier(self):
        self.provider.response = {"embeddings": [[1, 0]]}
        self.limited()
        self.r.semantic_refresh(self.p)
        self.provider.status = 503
        result = self.r.search(self.p, "E_IMPORT", _automatic=True)
        self.assertEqual(result["retrieval"]["semantic_status"], "degraded")
        self.assertEqual(result["items"], [])

    def test_high_similarity_cannot_supply_an_absent_underscore_error_code(self):
        self.provider.response = {"embeddings": [[1, 0]]}
        expected = self.publish("管理员设置页面配色", content="theme colors")
        self.r.semantic_refresh(self.p)
        query = "修复 E_AUTH_SIGNATURE_INVALID 管理员设置页面保存问题"
        manual = self.r.search(self.p, query)["items"]
        self.assertEqual(manual[0]["knowledge_id"], expected)
        self.assertFalse(manual[0]["match"]["auto_eligible"])
        self.assertEqual(self.r.search(self.p, query, _automatic=True)["items"], [])

    def test_degraded_provider_uses_same_positive_and_negative_exact_gate(self):
        self.provider.response = {"embeddings": [[1, 0]]}
        self.r.config["retrieval"]["semantic"]["sync_batch_size"] = 1
        expected = self.publish("E_IMPORT", content="dependency repair")
        self.publish("release retained buffers")
        self.r.semantic_refresh(self.p)
        self.provider.status = 503
        query = "After 3 retries in C++ Python 3.10 or 3.12 investigate E_IMPORT in the deployment process"
        result = self.r.search(self.p, query, _automatic=True)
        self.assertEqual(result["retrieval"]["semantic_status"], "degraded")
        self.assertEqual([item["knowledge_id"] for item in result["items"]], [expected])
        self.assertEqual(self.r.search(self.p, "release retained buffers E_AUTH_MISSING", _automatic=True)["items"], [])


if __name__ == "__main__":
    unittest.main()
