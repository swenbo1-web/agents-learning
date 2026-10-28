"""Automatic history favors useful records without deleting original questions."""
from pathlib import Path
import tempfile
import unittest

from agent_learning.runtime import Runtime


class WorkQuestionRelevanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="work-question-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        (self.workspace / "memory.py").write_text("original = True\n", encoding="utf-8")
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": False},
            "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)

    def record(self, text, kind="UserPromptSubmit", conversation="past", *, file=False):
        payload = {"prompt" if kind == "UserPromptSubmit" else "last_assistant_message": text}
        if file:
            payload.update(tool_name="read_file", tool_input={"path": "memory.py"})
        with self.r.transaction():
            event, _ = self.r._event(self.p, conversation, kind, payload, outcome="reported")
            result = self.r._work_capture_event(self.p, event["id"], self.workspace, raw=payload)
        return result["memory_id"]

    def ids(self, query="", **kwargs):
        return [item["memory_id"] for item in self.r.work_context_items(
            self.p, "reader", query, workspace=self.workspace, **kwargs)]

    def manual_ids(self, query=""):
        return [item["memory_id"] for item in self.r.work_search(self.p, query, max_chars=10000)["items"]]

    def test_unanswered_old_questions_are_not_automatic_fallback(self):
        for text, query in (("知识模型注入是不是需要更多预算？", "知识模型注入"),
                            ("How does orbitguard memory injection work?", "orbitguard memory injection"),
                            ("orbitguard 的上下文怎么注入", "orbitguard")):
            with self.subTest(text=text):
                ident = self.record(text)
                self.assertIn(ident, self.manual_ids(query))
                self.assertNotIn(ident, self.ids(query))

    def test_previous_action_requests_without_results_are_not_answers(self):
        for text in ("请分析 orbitguard 上下文注入", "帮我继续检查 orbitguard", "Please investigate orbitguard", "对 orbitguard 深度分析"):
            with self.subTest(text=text):
                ident = self.record(text)
                self.assertIn(ident, self.manual_ids("orbitguard"))
                self.assertNotIn(ident, self.ids("orbitguard"))

    def test_explicit_request_history_and_user_words_remain_retrievable(self):
        question = self.record("orbitguard 的上下文怎么注入？")
        for query in ("查看我之前 orbitguard 的提问原话", "orbitguard 历史需求是什么", "What did I ask about orbitguard?",
                      "Find the original user request about orbitguard"):
            with self.subTest(query=query):
                self.assertIn(question, self.ids(query))

    def test_user_facts_constraints_and_corrections_keep_reported_level(self):
        cases = ("orbitguard 当前默认使用本地知识库。", "orbitguard must preserve tenant boundaries",
                 "orbitguard 保持旧接口兼容", "不对，orbitguard 仍未验证，不能认为已经成功。",
                 "orbitguard validation failed because the import omitted the model field.",
                 "We use orbitguard in offline mode.")
        for index, text in enumerate(cases):
            with self.subTest(text=text):
                conversation = "source-%d" % index
                ident = self.record(text, conversation=conversation)
                row = next(row for row in self.r._work_search_rows(self.p, "orbitguard", automatic=True,
                    conversation=conversation) if row["id"] == ident)
                item = self.r._work_item(self.p, row, workspace=self.workspace)
                self.assertEqual(item["validation_level"], "reported")
                self.assertFalse(item["execution_authority"])

    def test_question_about_requirement_is_not_itself_a_requirement(self):
        for text in ("orbitguard 是否必须调用模型？", "Must orbitguard use a paid model?", "Can orbitguard preserve old data?"):
            with self.subTest(text=text):
                ident = self.record(text)
                self.assertNotIn(ident, self.ids("orbitguard"))
                self.assertIn(ident, self.manual_ids("orbitguard"))

    def test_mixed_question_preserves_its_actual_constraint(self):
        ident = self.record("orbitguard 怎么实现？必须保留旧接口兼容。")
        self.assertIn(ident, self.ids("orbitguard"))

    def test_new_question_burst_cannot_hide_an_older_result(self):
        conclusion = self.record("orbitguard 已验证修复原因：旧回执缺少模型字段。", kind="Stop")
        for index in range(35):
            self.record("orbitguard 是怎样工作的？问题编号 %d" % index)
        self.assertEqual(self.ids("orbitguard", limit=1), [conclusion])

    def test_relevant_fact_cannot_outrank_equal_topic_public_result_by_repetition(self):
        conclusion = self.record("orbitguard：已验证回滚保护完整。", kind="Stop")
        self.record("orbitguard orbitguard orbitguard 当前版本为1.4.35。")
        self.assertEqual(self.ids("orbitguard", limit=1), [conclusion])

    def test_unfocused_current_conversation_does_not_inject_old_question(self):
        question = self.record("orbitguard 如何注入上下文？", conversation="reader")
        self.assertNotIn(question, self.ids())
        self.assertIn(question, self.manual_ids())

    def test_file_affinity_does_not_turn_an_old_question_into_an_answer(self):
        question = self.record("orbitguard memory.py 是怎样工作的？", file=True)
        self.assertNotIn(question, self.ids(file_only=True,
            raw={"tool_name": "read_file", "tool_input": {"path": "memory.py"}}))

    def test_explicit_history_never_bypasses_existing_topic_or_project_match(self):
        question = self.record("orbitguard 的上下文怎么注入？")
        self.assertNotIn(question, self.ids("查看之前 SQLite E_AUTH_SIGNATURE_INVALID 的提问原话"))
        other = self.root / "other"
        other.mkdir()
        project = self.r.project(other)
        self.assertEqual(self.r.work_context_items(project, "reader", "查看之前 orbitguard 的提问原话",
            workspace=other), [])

    def test_work_history_product_questions_are_not_request_history_intent(self):
        question = self.record("orbitguard 工作历史索引是否需要优化？")
        self.assertNotIn(question, self.ids("优化 orbitguard 工作历史索引"))
        self.assertNotIn(question, self.ids("优化 orbitguard 历史知识处理的问题"))
        self.assertIn(question, self.manual_ids("orbitguard 工作历史索引"))

    def test_history_framing_cannot_remove_a_path_identity(self):
        question = self.record("orbitguard D:/问题/config.json 是怎样工作的？")
        self.assertNotIn(question, self.ids("查看我之前 orbitguard D:/我的/config.json 的提问原话"))
        self.assertNotIn(question, self.ids("查看我之前 " + "无主题背景" * 2000 + " orbitguard 的提问原话"))

    def test_explicit_conclusion_preview_keeps_conditions_and_original_body(self):
        text = "背景：\n" + "orbitguard 提问和排查背景。" * 12 + "\n结论：\norbitguard 缺少模型字段。\n限制：\n仅在旧版本适用。\n细节：\n" + "其他细节。" * 30
        ident = self.record(text, kind="Stop")
        item = self.r.work_context_items(self.p, "reader", "orbitguard", workspace=self.workspace)[0]
        self.assertEqual(item["memory_id"], ident)
        self.assertIn("orbitguard 缺少模型字段", item["summary"])
        self.assertIn("仅在旧版本适用", item["summary"])
        self.assertNotIn("排查背景", item["summary"])
        self.assertTrue(item["read_required"])
        refreshed = self.r.work_validate_items(self.p, [item], workspace=self.workspace)
        self.assertEqual(refreshed[0]["summary"], item["summary"])
        content = self.r.work_read(self.p, ident, workspace=self.workspace, max_chars=6000)["content"]
        self.assertEqual(content, text)

    def test_long_condition_block_is_not_cut_into_an_unconditional_conclusion(self):
        ident = self.record("背景：\n" + "orbitguard background " * 10 + "\n结论：\norbitguard 已实现。\n限制：\n仅在" + "需要逐项验证的环境" * 15 + "适用。", kind="Stop")
        item = next(item for item in self.r.work_context_items(self.p, "reader", "orbitguard",
            workspace=self.workspace) if item["memory_id"] == ident)
        self.assertEqual(item["summary"], "")
        self.assertTrue(item["read_required"])


if __name__ == "__main__":
    unittest.main()
