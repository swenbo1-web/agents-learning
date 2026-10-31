"""小预算卡和分页读取保留同checkout文件漂移提示；仅核对元数据。"""
import json
from pathlib import Path
import tempfile
import unittest

from agent_learning.mcp import Server
from agent_learning.runtime import Runtime


class KnowledgeFileStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.path = self.workspace / "service.py"
        self.path.write_text("first\n", encoding="utf-8")
        self.runtime = Runtime(Path(self.temp.name) / "home")
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.project(self.workspace)
        self.runtime.task_update(self.project, "source", "service startup config")
        shared = dict(session_id="source", cwd=str(self.workspace), tool_use_id="edit", tool_name="edit",
                      tool_input={"file_path": str(self.path)})
        self.runtime.handle_hook(dict(shared, hook_event_name="PreToolUse"))
        self.runtime.handle_hook(dict(shared, hook_event_name="PostToolUse", tool_response={"success": True}))
        event = self.runtime.db.execute("SELECT id FROM events WHERE type='PostToolUse'").fetchone()[0]
        problem = self.runtime.feedback(self.project, "source", "service startup config", category="operation", event_refs=[event])
        candidate = self.runtime.submit_candidate(self.project, problem_id=problem["id"], problem_revision=problem["revision"],
            title="service startup config", content="body " * 200, category="operation", applicability="condition " * 25,
            limitations="restriction " * 25, evidence_refs=[event], validation_level="tool_success")
        self.knowledge_id = self.runtime.publish(self.project, candidate["candidate_id"])["knowledge_id"]
        self.path.write_text("changed and longer\n", encoding="utf-8")

    def context_after_task_notice(self, conversation):
        handle = self.runtime.register_context(self.project, conversation)["context_handle"]
        # These tests measure the knowledge card's budget after the independent,
        # once-per-boundary task advice has been completely delivered.
        self.runtime.config["injection_max_chars"] = 2400
        notice = self.runtime._context(self.project, conversation, "",
            context_handle=handle, workspace=str(self.workspace))
        self.assertIn("task_update", notice)
        self.assertNotIn(self.knowledge_id, notice)
        return handle

    def test_identifier_card_keeps_file_change_flag_and_budget(self):
        for budget in (2400, 550, 450, 400):
            with self.subTest(budget=budget):
                conversation = f"read-{budget}"
                handle = self.context_after_task_notice(conversation)
                self.runtime.config["injection_max_chars"] = budget
                text = self.runtime._context(self.project, conversation, "service startup config",
                    context_handle=handle, workspace=str(self.workspace))
                cards = [json.loads(line) for line in text.splitlines() if line.startswith('{"')]
                self.assertEqual(len(cards), 1, text)
                self.assertTrue(cards[0]["source_files_changed"])
                self.assertEqual(cards[0]["knowledge_id"], self.knowledge_id)
                self.assertLessEqual(len(text), budget)
                if budget <= 550:
                    self.assertTrue(cards[0]["read_required"])

    def test_unfitting_identifier_card_is_not_recorded_as_exposed_and_can_return_later(self):
        for budget in (350, 128):
            with self.subTest(budget=budget):
                conversation = f"unfitting-{budget}"
                handle = self.context_after_task_notice(conversation)
                self.runtime.config["injection_max_chars"] = budget
                text = self.runtime._context(self.project, conversation, "service startup config",
                    context_handle=handle, workspace=str(self.workspace))
                self.assertLessEqual(len(text), budget)
                self.assertNotIn(self.knowledge_id, text)
                state = self.runtime.db.execute("SELECT shown,injected FROM contexts "
                    "WHERE project=? AND conversation=?", (self.project, conversation)).fetchone()
                self.assertEqual(json.loads(state["shown"]), [])
                self.assertEqual(json.loads(state["injected"]), [])
                self.runtime.config["injection_max_chars"] = 450
                restored = self.runtime._context(self.project, conversation, "service startup config",
                    context_handle=handle, workspace=str(self.workspace))
                cards = [json.loads(line) for line in restored.splitlines() if line.startswith('{"')]
                self.assertEqual(len(cards), 1, restored)
                self.assertEqual(cards[0]["knowledge_id"], self.knowledge_id)
                self.assertTrue(cards[0]["source_files_changed"])

    def test_initial_task_notice_defers_small_card_without_acknowledging_it(self):
        conversation = "first-task-notice"
        handle = self.runtime.register_context(self.project, conversation)["context_handle"]
        self.runtime.config["injection_max_chars"] = 400
        first = self.runtime._context(self.project, conversation, "service startup config",
            context_handle=handle, workspace=str(self.workspace))
        self.assertIn("task_update", first)
        self.assertNotIn(self.knowledge_id, first)
        self.assertLessEqual(len(first), 400)
        state = self.runtime.db.execute("SELECT shown,injected FROM contexts "
            "WHERE project=? AND conversation=?", (self.project, conversation)).fetchone()
        self.assertEqual(json.loads(state["shown"]), [])
        self.assertEqual(json.loads(state["injected"]), [])

        later = self.runtime._context(self.project, conversation, "service startup config",
            context_handle=handle, workspace=str(self.workspace))
        cards = [json.loads(line) for line in later.splitlines() if line.startswith('{"')]
        self.assertEqual(len(cards), 1, later)
        self.assertEqual(cards[0]["knowledge_id"], self.knowledge_id)
        self.assertTrue(cards[0]["source_files_changed"])
        self.assertLessEqual(len(later), 400)

    def test_mcp_read_and_search_report_same_drift(self):
        server = Server(self.runtime, self.project, workspace=str(self.workspace))
        self.addCleanup(server.close)
        page = server.call("knowledge_read", {"knowledge_id": self.knowledge_id, "max_chars": 1200})
        self.assertTrue(page["source_files_changed"])
        self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, sort_keys=True, separators=(",", ":"))), 1200)
        found = server.call("knowledge_search", {"query": "service startup config"})
        self.assertTrue(found["items"][0]["source_files_changed"])

    def test_other_checkout_does_not_compare_current_file_with_foreign_baseline(self):
        other = Path(self.temp.name) / "other-checkout"
        other.mkdir()
        (other / "service.py").write_text("unrelated", encoding="utf-8")
        server = Server(self.runtime, self.project, workspace=str(other))
        self.addCleanup(server.close)
        page = server.call("knowledge_read", {"knowledge_id": self.knowledge_id})
        self.assertNotIn("source_files_changed", page)
