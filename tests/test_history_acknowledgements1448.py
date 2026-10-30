"""Old navigation remains searchable without becoming automatic project memory."""
from pathlib import Path
import tempfile
import unittest

from agent_learning.knowledge_context import _continuation_query
from agent_learning.runtime import Runtime
from agent_learning.work_memory import _request_has_context


NOISE = ("是的继续推进吧", "已经重启，继续推进", "是的，按你说的做好验收标准，继续推进")
QUERY = "继续推进，箱体改善了就行，如果优化不到10%也没事"


class HistoryAcknowledgementGrammar1448Tests(unittest.TestCase):
    def test_navigation_is_not_a_project_fact(self):
        for text in (*NOISE, "好的，继续检查", "yes, continue testing",
                     "confirmed, reloaded, continue checking"):
            with self.subTest(text=text):
                self.assertFalse(_request_has_context(text))

    def test_real_facts_constraints_and_acceptance_survive_confirmation(self):
        for text in ("已经重启", "已重启 Redis，继续推进", "没有重启，继续推进",
                     "已重启，不要继续", "是的，SQLite 默认超时为30秒",
                     "是的，必须保留旧接口，继续推进", "是的，不对，回滚仍失败",
                     "是的，当前版本为1.4.47", "验收标准是回滚后两张表计数均为0",
                     "是的，优化不到10%也没事", "是的，" + QUERY,
                     "是的，优化不到10%，也没事",
                     "是的，继续推进，SQLite 默认超时为30秒",
                     "确认修复结果是发布前置条件。", "好的分析是基于实际证据。",
                     "Yes, SQLite uses a 30 second timeout."):
            with self.subTest(text=text):
                self.assertTrue(_request_has_context(text))

    def test_confirmation_does_not_turn_a_question_into_a_fact(self):
        for text in ("是的，SQLite 是否必须调用模型？", "是的，怎么继续推进？",
                     "Yes, must SQLite use a paid model?",
                     "orbitguard 默认使用本地库，还是远端服务？",
                     "orbitguard 的默认配置是什么，是否需要调用模型？",
                     "SQLite is local, or is it remote?",
                     "orbitguard 必须调用模型吗 ",
                     "Please investigate orbitguard, does it use a paid model"):
            with self.subTest(text=text):
                self.assertFalse(_request_has_context(text))

    def test_substantive_current_queries_keep_their_subject(self):
        self.assertFalse(_continuation_query(QUERY))
        self.assertFalse(_continuation_query(NOISE[2]))
        self.assertFalse(_continuation_query("已经重启"))
        self.assertTrue(_continuation_query(NOISE[0]))
        self.assertTrue(_continuation_query(NOISE[1]))


class HistoryAcknowledgementRuntime1448Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="history-ack1448-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        (self.workspace / "memory.py").write_text("original = True\n", encoding="utf-8")

    def runtime(self, host="codex"):
        runtime = Runtime(self.root / host, host=host, config={
            "admin": {"enabled": False}, "scheduler": {"enabled": False},
            "hindsight": {"enabled": False}})
        self.addCleanup(runtime.close)
        return runtime, runtime.project(self.workspace)

    def record(self, runtime, project, text, *, conversation="past", file=False, kind="UserPromptSubmit"):
        raw = {"prompt" if kind == "UserPromptSubmit" else "last_assistant_message": text}
        if file:
            raw.update(tool_name="read_file", tool_input={"path": "memory.py"})
        with runtime.transaction():
            event, _ = runtime._event(project, runtime.host_conversation(conversation), kind,
                                      raw, outcome="reported")
            item = runtime._work_capture_event(project, event["id"], self.workspace, raw=raw)
        return item["memory_id"]

    def test_concrete_chinese_prompt_never_recalls_navigation_on_any_host(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                runtime, project = self.runtime(host)
                old = {self.record(runtime, project, text) for text in NOISE}
                output = runtime.handle_hook({"hook_event_name": "UserPromptSubmit",
                    "cwd": str(self.workspace), "session_id": runtime.host_conversation("reader"),
                    "turn_id": "concrete-business", "prompt": QUERY})
                text = output.get("hookSpecificOutput", {}).get("additionalContext", "")
                for identity in old:
                    self.assertNotIn(identity, text)
                # Automatic suppression never deletes the original public source.
                for identity in old:
                    self.assertIsNotNone(runtime.db.execute(
                        "SELECT id FROM work_memories WHERE project=? AND id=?", (project, identity)).fetchone())
                original = runtime.work_read(project, next(iter(old)), workspace=self.workspace)
                self.assertIn(original["content"], NOISE)
                self.assertEqual(runtime.db.execute(
                    "SELECT COUNT(*) FROM admin_calls WHERE project=?", (project,)).fetchone()[0], 0)

    def test_empty_query_and_file_affinity_cannot_bypass_history_filter(self):
        runtime, project = self.runtime()
        reader = runtime.host_conversation("reader")
        old = {self.record(runtime, project, text, conversation="reader", file=True) for text in NOISE}
        for kwargs in ({}, {"file_only": True, "raw": {
                "tool_name": "read_file", "tool_input": {"path": "memory.py"}}}, {"compact": True}):
            with self.subTest(kwargs=kwargs):
                items = runtime.work_context_items(project, reader, workspace=self.workspace, **kwargs)
                self.assertFalse(old.intersection(item["memory_id"] for item in items))

    def test_manual_and_explicit_user_history_recall_remain_available(self):
        runtime, project = self.runtime()
        old = self.record(runtime, project, NOISE[0])
        manual = runtime.work_search(project, "继续推进", max_chars=10000)["items"]
        self.assertIn(old, [item["memory_id"] for item in manual])
        explicit = runtime.work_context_items(project, "reader", "查看之前用户继续推进的原话",
                                              workspace=self.workspace, limit=10)
        self.assertIn(old, [item["memory_id"] for item in explicit])

    def test_useful_reported_fact_is_not_lost_in_the_same_chinese_topic(self):
        runtime, project = self.runtime()
        old = {self.record(runtime, project, text) for text in NOISE}
        useful = self.record(runtime, project, "是的，箱体默认使用独立时限，继续推进需保留单调时钟。")
        items = runtime.work_context_items(project, "reader", "继续推进 箱体 独立时限 单调时钟",
                                           workspace=self.workspace, limit=10)
        self.assertIn(useful, [item["memory_id"] for item in items])
        self.assertFalse(old.intersection(item["memory_id"] for item in items))
        item = next(item for item in items if item["memory_id"] == useful)
        self.assertEqual(item["validation_level"], "reported")
        self.assertFalse(item["execution_authority"])


if __name__ == "__main__":
    unittest.main()
