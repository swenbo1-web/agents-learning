"""工作历史扩展的故障隔离、升级与双平台恢复契约。仅使用临时数据。"""
import contextlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.antigravity import AntigravityAdapter
from agent_learning.runtime import Runtime


class WorkMemoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-work-integration-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "checkout"
        self.workspace.mkdir()
        self.home = Path(self.temp.name) / "home"
        self.r = Runtime(self.home, config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.task = self.r.task_update(self.p, "chat", "compiler boundary", {"mode": "analysis"})

    def hook(self, kind, **fields):
        return self.r.handle_hook(dict(hook_event_name=kind, session_id="chat",
                                      cwd=str(self.workspace), **fields))

    def test_default_projection_does_not_enable_ai_or_publication(self):
        self.assertTrue(self.r.config["work_memory"]["enabled"])
        self.assertFalse(self.r.config["work_memory"]["summary_enabled"])
        for section in ("admin", "scheduler", "hindsight"):
            self.assertFalse(self.r.config[section]["enabled"])
        self.assertFalse(self.r.config["admin"]["auto_publish"])
        with contextlib.closing(Runtime(Path(self.temp.name) / "partial", config={
                "work_memory": {"automatic_context": False}})) as other:
            self.assertFalse(other.config["work_memory"]["automatic_context"])
            self.assertFalse(other.config["work_memory"]["summary_enabled"])

    def test_projection_error_keeps_authoritative_failure_and_case(self):
        self.hook("PreToolUse", tool_name="Write", tool_use_id="failure",
                  tool_input={"file_path": "src/compiler.py"})
        original = self.r._work_capture_event
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError("derived index interrupted")
        with patch.object(self.r, "_work_capture_event", side_effect=interrupted):
            self.assertEqual(self.hook("PostToolUse", tool_name="Write", tool_use_id="failure",
                tool_input={"file_path": "src/compiler.py"}, tool_response={"exit_code": 1, "output": "compiler failed"}), {})
        event = self.r.db.execute("SELECT id,outcome FROM events WHERE type='PostToolUse'").fetchone()
        self.assertEqual(event["outcome"], "failed")
        self.assertTrue(self.r.problems(self.p))
        self.assertEqual(self.r.db.execute("SELECT observation FROM operations").fetchone()[0], "failed")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories WHERE event_id=?", (event["id"],)).fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT value FROM metrics WHERE name='work_memory_capture_failed'").fetchone()[0], 1)

    def test_projection_error_cannot_remove_execution_denial(self):
        self.r.config["enforce_task_policy"] = True
        with patch.object(self.r, "_work_capture_event", side_effect=ValueError("bad derived target")):
            result = self.hook("PreToolUse", tool_name="Write", tool_use_id="denied")
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.r.db.execute("SELECT decision FROM operations").fetchone()[0], "denied")
        self.assertEqual(self.r.db.execute("SELECT outcome FROM events WHERE type='PreToolUse'").fetchone()[0], "denied")

    def test_summary_queue_error_keeps_delivery_record(self):
        with patch.object(self.r, "_work_queue_summary", side_effect=sqlite3.OperationalError("queue interrupted")):
            self.assertEqual(self.hook("Stop", turn_id="delivery", last_assistant_message="compiler checked, user acceptance pending"), {})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='Stop'").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories m JOIN events e ON e.id=m.event_id "
                                          "WHERE e.type='Stop'").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_summary_links").fetchone()[0], 0)
        self.assertEqual(self.r.status(self.p)["metrics"].get("work_summary_queue_failed"), 1)

    def test_work_reads_remain_recovery_tools_without_execution_grant_on_both_hosts(self):
        self.r.config["enforce_task_policy"] = True
        for name in ("work_memory_search", "work_memory_read"):
            result = self.hook("PreToolUse", tool_name="mcp__agent_learning__" + name, tool_use_id=name)
            self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertFalse(self.r.execution_status(self.p, "chat")["ready"])
        with contextlib.closing(Runtime(self.home, config={}, host="antigravity")) as ag:
            conversation = ag.host_conversation("native")
            ag.task_update(self.p, conversation, "compiler boundary", {"mode": "analysis"})
            adapter = AntigravityAdapter(ag, self.workspace, "enforce")
            common = dict(conversationId="native", workspacePaths=[str(self.workspace)], modelName="fake-model")
            adapter.handle("PreInvocation", dict(common, invocationNum=0, initialNumSteps=0))
            for index, name in enumerate(("work_memory_search", "work_memory_read")):
                result = adapter.handle("PreToolUse", dict(common, stepIdx=index, toolCall={
                    "name": "mcp__agent_learning__" + name, "args": {}}))
                self.assertNotEqual(result.get("permissionDecision"), "deny", result)
            self.assertFalse(ag.execution_status(self.p, conversation)["ready"])

    def test_schema14_upgrade_failure_rolls_back_new_tables_and_preserves_core(self):
        problem = self.r.feedback(self.p, "chat", "compiler boundary")
        packet = self.r.review_packet(self.p, problem["id"])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="compiler boundary", content="Check compiler paths", category="operation",
            applicability="Recorded environment", limitations="Reported only", validation_level="reported",
            evidence_refs=[event["id"] for event in packet["events"]])
        knowledge = self.r.publish(self.p, candidate["candidate_id"])
        with self.r.transaction():
            tables = [row[0] for row in self.r.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'work_%'")
                      if not row[0].startswith("work_memory_fts_")]
            for table in sorted(tables, key=lambda value: value != "work_summary_links"):
                self.r.db.execute("DROP TABLE " + table)
            self.r.db.execute("PRAGMA user_version=14")
        self.r.close()
        with patch("agent_learning.runtime.install_work_summaries", side_effect=RuntimeError("migration interruption")), \
                self.assertRaisesRegex(RuntimeError, "migration interruption"):
            Runtime(self.home, config={})
        with contextlib.closing(sqlite3.connect(self.home / "runtime.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 14)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='work_memories'").fetchone())
        for _ in range(2):
            with contextlib.closing(Runtime(self.home, config={})) as upgraded:
                self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 17)
                self.assertEqual(upgraded.read(self.p, knowledge["knowledge_id"])["revision"], knowledge["revision"])
                self.assertEqual(upgraded.task(self.p, "chat")["id"], self.task["id"])
                self.assertEqual(upgraded.review_packet(self.p, problem["id"])["problem"]["revision"], problem["revision"])
                self.assertEqual(upgraded.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
