"""工作历史的实际 SQLite、身份、共享预算及双平台展示语义保护。"""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.cli import main, parser
from agent_learning.antigravity import AntigravityAdapter, reset_session
from agent_learning.mcp import Server, WORKER_TOOLS, validate
from agent_learning.runtime import ContractError, Runtime, encode


class WorkMemoryContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-work-context-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "checkout"
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src/parser.py").write_text("historicparserfence = True\n", encoding="utf-8")
        self.home = Path(self.temp.name) / "home"
        self.r = Runtime(self.home, config={"admin": {"enabled": False},
                                          "scheduler": {"enabled": False},
                                          "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "reader", "historicparserfence", {"mode": "analysis"})
        self.handle = self.r.register_context(self.p, "reader")["context_handle"]

    def observed(self, conversation="past-work", message=None, file_path="src/parser.py"):
        body = {"tool_name": "Write", "tool_input": {"file_path": file_path,
                "content": "historicparserfence implementation"},
                "tool_response": {"success": True, "output": message or
                    "historicparserfence tool succeeded; gameplay was not accepted by the user"}}
        with self.r.transaction():
            event, _ = self.r._event(self.p, conversation, "PostToolUse", body,
                                     outcome="succeeded")
            self.r._work_capture_event(self.p, event["id"], self.workspace, raw=body)
        found = self.r.work_search(self.p, file_path=file_path, workspace=self.workspace)
        self.assertTrue(found["items"], found)
        return found["items"][0]

    def context(self, **kwargs):
        return self.r._context(self.p, "reader", query="historicparserfence", context_handle=self.handle,
                               workspace=self.workspace, **kwargs)

    def published(self, title="historicparserfence"):
        problem = self.r.feedback(self.p, "reader", title + " reported evidence", category="operation")
        packet = self.r.review_packet(self.p, problem["id"])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title=title, content=title + " reported guidance", category="operation",
            applicability="Recorded environment only", limitations="User acceptance remains unknown",
            validation_level="reported", evidence_refs=[event["id"] for event in packet["events"]])
        return self.r.publish(self.p, candidate["candidate_id"])

    def test_worker_tools_are_read_only_and_cannot_select_workspace_or_role(self):
        contracts = {name: contract for name, _, contract in WORKER_TOOLS}
        self.assertIn("work_memory_search", contracts)
        self.assertIn("work_memory_read", contracts)
        for extra in ({"workspace": "outside"}, {"role": "admin"}, {"conversation_id": "past-work"}):
            with self.assertRaises(ContractError):
                validate(extra, contracts["work_memory_search"])
        for name in ("work-rebuild", "work_rebuild", "work_publish", "work_summary_accept"):
            self.assertNotIn(name, Server(self.r, self.p).tools)

    def test_unbound_search_returns_only_index_and_read_requires_identity(self):
        memory = self.observed()
        server = Server(self.r, self.p, workspace=self.workspace)
        found = server.call("work_memory_search", {"file_path": "src/parser.py", "max_chars": 1024})
        self.assertLessEqual(len(encode(found)), 1024)
        self.assertTrue(found["items"])
        for item in found["items"]:
            for field in ("evidence", "payload", "conversation", "task_id", "context_handle"):
                self.assertNotIn(field, item)
        with self.assertRaisesRegex(ContractError, "context_handle"):
            server.call("work_memory_read", {"memory_id": memory["memory_id"]})

    def test_invalid_handle_is_rejected_before_search_or_read(self):
        server = Server(self.r, self.p)
        for name, body in (("work_memory_search", {}), ("work_memory_read", {"memory_id": "missing"})):
            with patch.object(self.r, "work_search", side_effect=AssertionError("read before identity")), \
                 patch.object(self.r, "work_read", side_effect=AssertionError("read before identity")):
                with self.assertRaises(ContractError):
                    server.call(name, {**body, "context_handle": "untrusted"})

    def test_bound_search_records_only_actual_returned_indices(self):
        self.observed()
        server = Server(self.r, self.p, context_handle=self.handle, workspace=self.workspace)
        with patch.object(self.r, "work_record_exposure", wraps=self.r.work_record_exposure) as record:
            result = server.call("work_memory_search", {"file_path": "src/parser.py", "max_chars": 1024})
        self.assertEqual(record.call_count, 1)
        self.assertEqual(record.call_args.args[2], result["items"])
        self.assertEqual(record.call_args.kwargs["method"], "mcp")
        self.assertEqual(record.call_args.kwargs["context_handle"], self.handle)

    def test_read_budget_and_revision_are_preserved(self):
        memory = self.observed(message="historicparserfence " + "中文\\\"\n" * 900)
        server = Server(self.r, self.p, context_handle=self.handle)
        page = server.call("work_memory_read", {"memory_id": memory["memory_id"], "section": "evidence",
                                                "max_chars": 1024})
        self.assertLessEqual(len(encode(page)), 1024)
        self.assertEqual(json.loads(encode(page)), page)
        self.assertEqual(page["revision"], memory["revision"])
        with self.assertRaisesRegex(ContractError, "expected_revision"):
            server.call("work_memory_read", {"memory_id": memory["memory_id"], "offset": 1})
        with self.assertRaises(ContractError):
            server.call("work_memory_read", {"memory_id": memory["memory_id"],
                                             "expected_revision": memory["revision"] + 1})

    def test_account_change_during_read_prevents_exposure_return(self):
        memory = self.observed()
        server = Server(self.r, self.p, context_handle=self.handle)
        original = self.r.work_read
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.r.account_changed("work-memory-read-account-change")
            return result
        with patch.object(self.r, "work_read", side_effect=changed):
            with self.assertRaisesRegex(ContractError, "reconnect_required"):
                server.call("work_memory_read", {"memory_id": memory["memory_id"]})

    def test_other_project_cannot_read_work_memory(self):
        memory = self.observed()
        other = Path(self.temp.name) / "unrelated"
        other.mkdir()
        other_project = self.r.project(other)
        handle = self.r.register_context(other_project, "other")["context_handle"]
        with self.assertRaises(ContractError):
            Server(self.r, other_project, context_handle=handle).call(
                "work_memory_read", {"memory_id": memory["memory_id"]})

    def test_persistent_dedup_and_compaction_restore(self):
        memory = self.observed()
        first = self.context()
        self.assertIn(memory["memory_id"], first)
        self.assertIn("非正式解法", first)
        second = self.context()
        self.assertNotIn(memory["memory_id"], second)
        restored = self.context(compact=True)
        self.assertIn(memory["memory_id"], restored)
        self.assertNotIn(memory["memory_id"], self.context())

    def test_ephemeral_context_renders_indices_each_invocation(self):
        memory = self.observed()
        for _ in range(2):
            self.assertIn(memory["memory_id"], self.context(delivery="ephemeral"))

    def ag_file_context(self):
        memory = self.observed()
        ag = Runtime(self.home, host="antigravity", config={"admin": {"enabled": False}})
        self.addCleanup(ag.close)
        conversation = ag.host_conversation("ag-file-context")
        ag.task_update(self.p, conversation, "unrelatedgoalwithoutmemory", {"mode": "analysis"})
        adapter = AntigravityAdapter(ag, self.workspace)
        common = {"conversationId": "ag-file-context", "workspacePaths": [str(self.workspace)],
                  "modelName": "offline-model"}
        adapter.handle("PreInvocation", dict(common, invocationNum=0, initialNumSteps=0))
        adapter.handle("PostToolUse", dict(common, stepIdx=0, toolCall={"name": "view_file", "args": {
            "AbsolutePath": str(self.workspace / "src/parser.py")}}, error=""))
        post = ag.db.execute("SELECT task_id,outcome FROM events WHERE conversation=? AND type='PostToolUse'",
                             (conversation,)).fetchone()
        self.assertIsNone(post["task_id"])
        self.assertEqual(post["outcome"], "call_completed")
        return memory, ag, adapter, conversation, common

    def test_antigravity_file_history_uses_observed_target_with_unrelated_goal(self):
        memory, ag, adapter, conversation, common = self.ag_file_context()
        for invocation, steps in ((1, 1), (2, 1)):
            result = adapter.handle("PreInvocation", dict(common, invocationNum=invocation, initialNumSteps=steps))
            text = result["injectSteps"][0]["ephemeralMessage"]
            self.assertIn(memory["memory_id"], text)
            self.assertIn('"task_affinity":"context_only"', text)
            self.assertIn('"task_binding_known":false', text)
            self.assertLessEqual(len(text), ag.config["injection_max_chars"])
        ag.task_update(self.p, conversation, "newgoalwithoutmemory", new_task=True)
        result = adapter.handle("PreInvocation", dict(common, invocationNum=3, initialNumSteps=1))
        self.assertNotIn(memory["memory_id"], result["injectSteps"][0]["ephemeralMessage"])

    def test_antigravity_file_hint_expires_on_account_change(self):
        memory, ag, adapter, _, common = self.ag_file_context()
        ag.account_changed("file-hint-account-switch")
        result = adapter.handle("PreInvocation", dict(common, invocationNum=1, initialNumSteps=1))
        self.assertNotIn(memory["memory_id"], encode(result))

    def test_antigravity_file_hint_expires_on_native_reset(self):
        memory, ag, adapter, _, common = self.ag_file_context()
        reset_session(ag, self.workspace, "ag-file-context")
        result = adapter.handle("PreInvocation", dict(common, invocationNum=1, initialNumSteps=1))
        self.assertNotIn(memory["memory_id"], encode(result))

    def test_tool_file_trigger_works_without_a_fault(self):
        memory = self.observed()
        text = self.context(tool_only=True, raw={"tool_name": "Read", "tool_input": {"file_path": "src/parser.py"}})
        self.assertIn(memory["memory_id"], text)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_faults").fetchone()[0], 0)

    def test_arbitrary_tool_does_not_inject_last_work(self):
        memory = self.observed()
        text = self.context(tool_only=True, raw={"tool_name": "Bash", "tool_input": {"command": "git status"}})
        self.assertNotIn(memory["memory_id"], text)

    def test_shared_budget_does_not_confirm_undelivered_card(self):
        memory = self.observed()
        self.r.config["injection_max_chars"] = 128
        with patch.object(self.r, "work_record_exposure", wraps=self.r.work_record_exposure) as record:
            small = self.context()
        self.assertLessEqual(len(small), 128)
        self.assertNotIn(memory["memory_id"], small)
        self.assertEqual(record.call_count, 0)
        self.r.config["injection_max_chars"] = 2400
        self.assertIn(memory["memory_id"], self.context())

    def test_work_budget_remains_inside_injection_budget(self):
        self.observed()
        self.r.config["work_memory"]["max_context_chars"] = 350
        text = self.context()
        self.assertLessEqual(len(text), self.r.config["injection_max_chars"])
        work_lines = [line for line in text.splitlines() if line.startswith(("工作历史索引", "[工作历史]"))]
        self.assertLessEqual(len("\n".join(work_lines)) + bool(work_lines), 350)

    def test_file_version_change_survives_final_source_validation(self):
        memory = self.observed()
        (self.workspace / "src/parser.py").write_text("new implementation with different size\n", encoding="utf-8")
        text = self.context()
        self.assertIn(memory["memory_id"], text)
        self.assertIn('"file_version_changed":true', text)

    def test_file_restored_to_captured_stat_still_notifies_change_since_exposure(self):
        memory = self.observed()
        target = self.workspace / "src/parser.py"
        original_bytes, original_stat = target.read_bytes(), target.stat()
        self.assertIn(memory["memory_id"], self.context())
        target.write_text("different file version and different size\n", encoding="utf-8")
        changed = self.context()
        self.assertIn(memory["memory_id"], changed)
        target.write_bytes(original_bytes)
        os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        restored = self.context()
        self.assertIn(memory["memory_id"], restored)
        self.assertIn('"file_state_changed":true', restored)
        self.assertIn('"file_version_changed":true', restored)

    def test_source_change_waits_for_complete_shared_budget_notification(self):
        raw = {"session_id": "past-operation", "cwd": str(self.workspace), "tool_use_id": "file-op",
               "tool_name": "Write", "tool_input": {"file_path": "src/parser.py"}}
        self.r.handle_hook(dict(raw, hook_event_name="PreToolUse"))
        self.r.handle_hook(dict(raw, hook_event_name="PostToolUse", tool_response={"success": True,
                                                                                 "output": "historicparserfence"}))
        memory = self.r.work_search(self.p, file_path="src/parser.py", workspace=self.workspace)["items"][0]
        self.assertIn(memory["memory_id"], self.context())
        self.r.handle_hook(dict(raw, hook_event_name="PostToolUse", tool_response={"success": False,
                                                                                 "output": "historicparserfence conflict"}))
        self.r.config["injection_max_chars"] = 128
        small = self.context()
        self.assertLessEqual(len(small), 128)
        self.assertNotIn(memory["memory_id"], small)
        pending = self.r.work_exposure_updates(self.p, "reader")
        self.assertIn(memory["memory_id"], [item["memory_id"] for item in pending])
        self.r.config["injection_max_chars"] = 2400
        warned = self.context(tool_only=True, raw={"tool_name": "Bash", "tool_input": {"command": "git status"}})
        self.assertIn(memory["memory_id"], warned)
        self.assertIn('"source_changed":true', warned)
        self.assertIn('"state":"conflicted"', warned)
        self.assertNotIn(memory["memory_id"], [item["memory_id"] for item in self.r.work_exposure_updates(self.p, "reader")])

    def test_final_render_rechecks_source_changed_after_recall(self):
        memory = self.observed()
        original = self.r.work_context_items
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            source = memory["source_event_refs"][0]
            with self.r.transaction():
                row = self.r.db.execute("SELECT payload FROM events WHERE project=? AND id=?", (self.p, source)).fetchone()
                payload = json.loads(row[0])
                payload["tool_response"] = {"success": False, "output": "late evidence"}
                self.r.db.execute("UPDATE events SET payload=?,outcome='failed' WHERE project=? AND id=?",
                                  (encode(payload), self.p, source))
            return result
        with patch.object(self.r, "work_context_items", side_effect=changed):
            text = self.context()
        self.assertIn(memory["memory_id"], text)
        self.assertIn('"state":"failed"', text)

    def test_work_retrieval_failure_preserves_knowledge_and_revocation_notice(self):
        self.observed()
        current, old = self.published(), self.published("historicparserfence old")
        Server(self.r, self.p, context_handle=self.handle).call("knowledge_read", {"knowledge_id": old["knowledge_id"]})
        self.r.revoke(self.p, old["knowledge_id"], expected_revision=old["revision"])
        with patch.object(self.r, "work_context_items", side_effect=sqlite3.OperationalError("derived FTS is broken")):
            text = self.context()
        self.assertIn(current["knowledge_id"], text)
        self.assertIn(old["knowledge_id"] + "@1", text)
        self.assertIn("已撤销", text)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_exposures").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT value FROM metrics WHERE project=? AND name='work_context_failed'",
                                         (self.p,)).fetchone()[0], 1)

    def test_partial_work_exposure_failure_rolls_back_only_work_delivery(self):
        memory = self.observed()
        knowledge = self.published()
        original = self.r.work_record_exposure
        def fail_after_write(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError("work projection write failed")
        with patch.object(self.r, "work_record_exposure", side_effect=fail_after_write):
            text = self.context()
        self.assertIn(knowledge["knowledge_id"], text)
        self.assertNotIn(memory["memory_id"], text)
        self.assertNotIn("[工作历史]", text)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_exposures").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_exposures WHERE knowledge_id=?",
                                         (knowledge["knowledge_id"],)).fetchone()[0], 1)
        self.assertIn(memory["memory_id"], self.context())

    def test_read_only_history_does_not_resume_task_or_grant_execution(self):
        memory = self.observed()
        task = self.r.task(self.p, "reader")
        self.r.task_update(self.p, "reader", task["goal"], status="paused")
        before = self.r.task(self.p, "reader")
        Server(self.r, self.p, context_handle=self.handle).call("work_memory_read", {"memory_id": memory["memory_id"]})
        self.assertEqual(before, self.r.task(self.p, "reader"))
        self.assertFalse(self.r.execution_status(self.p, "reader")["ready"])

    def test_cli_rebuild_requires_explicit_workspace_before_runtime_open(self):
        output, error = io.StringIO(), io.StringIO()
        with patch("agent_learning.cli.Runtime", side_effect=AssertionError("created real runtime")), \
             redirect_stdout(output), redirect_stderr(error):
            code = main(["work-rebuild"])
        self.assertEqual(code, 1)
        self.assertIn("explicit --workspace", error.getvalue())

    def test_cli_commands_have_bounded_paging_contract(self):
        args = parser().parse_args(["--workspace", str(self.workspace), "work-read", "id", "--section", "evidence",
                                    "--offset", "100", "--expected-revision", "3", "--max-chars", "1024"])
        self.assertEqual((args.section, args.offset, args.expected_revision, args.max_chars), ("evidence", 100, 3, 1024))


if __name__ == "__main__":
    unittest.main()
