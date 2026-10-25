"""Missing native input is not an empty, action-only continuation."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.runtime import ContractError, Runtime


class ContextQueryAvailabilityTests(unittest.TestCase):
    def setUp(self):
        area = Path(__file__).resolve().parents[1] / "build" / "local" / "opt1426" / "query-availability"
        area.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=area)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("tmp", "home", "codex", "runtime-releases", "checkout"):
            (self.root / name).mkdir()
        self.workspace = self.root / "checkout"
        config = self.root / "config.json"
        config.write_text("{}", encoding="utf-8")
        environment = {"ALP_HOME": str(self.root / "home"), "ALP_CONFIG": str(config),
            "CODEX_HOME": str(self.root / "codex"), "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"),
            "PYTHONDONTWRITEBYTECODE": "1"}
        environment.update({name: str(self.root / "tmp") for name in ("TMP", "TEMP", "TMPDIR")})
        isolated = patch.dict(os.environ, environment)
        isolated.start()
        self.addCleanup(isolated.stop)

    def runtime(self, host="antigravity"):
        runtime = Runtime(self.root / "home" / host, host=host, config={
            "admin": {"enabled": False}, "scheduler": {"enabled": False},
            "hindsight": {"enabled": False}})
        self.addCleanup(runtime.close)
        project = runtime.project(self.workspace)
        conversation = runtime.host_conversation("reader")
        handle = runtime.register_context(project, conversation)["context_handle"]
        return runtime, project, conversation, handle

    def record(self, runtime, project, conversation="past", *, file=False):
        if file:
            payload = {"tool_name": "Write", "tool_input": {"file_path": "parser.py"},
                "tool_response": {"success": True, "output": "orbitguard keeps the parser observation"}}
            kind, outcome = "PostToolUse", "succeeded"
        else:
            payload = {"last_assistant_message": "orbitguard: retain the public launch evidence."}
            kind, outcome = "Stop", "reported"
        with runtime.transaction():
            event, _ = runtime._event(project, conversation, kind, payload, outcome=outcome)
            runtime._work_capture_event(project, event["id"], self.workspace, raw=payload)
        return runtime.db.execute("SELECT id FROM work_memories WHERE project=? AND event_id=?",
                                  (project, event["id"])).fetchone()[0]

    def context(self, runtime, project, conversation, handle, query="", **kwargs):
        return runtime._context(project, conversation, query=query, context_handle=handle,
            workspace=self.workspace, delivery="ephemeral", **kwargs)

    def test_empty_new_session_does_not_take_other_conversation_recent_history(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                r, p, c, handle = self.runtime(host)
                memory = self.record(r, p)
                text = self.context(r, p, c, handle)
                self.assertNotIn(memory, text)
                self.assertTrue(r.work_search(p, "orbitguard")["items"])
                self.assertIn(memory, self.context(r, p, c, handle, "orbitguard"))

    def test_known_empty_startup_can_restore_same_conversation_history(self):
        r, p, c, handle = self.runtime()
        memory = self.record(r, p, c)
        self.assertIn(memory, self.context(r, p, c, handle))

    def test_missing_query_does_not_reuse_or_erase_saved_focus(self):
        r, p, c, handle = self.runtime()
        memory = self.record(r, p, c)
        self.assertIn(memory, self.context(r, p, c, handle, "orbitguard"))
        with patch.object(r, "search", side_effect=AssertionError("stale focus reused")):
            text = self.context(r, p, c, handle, _query_available=False)
        self.assertNotIn(memory, text)
        saved = r.db.execute("SELECT last_query FROM contexts WHERE project=? AND conversation=?", (p, c)).fetchone()[0]
        self.assertEqual(saved, "orbitguard")
        self.assertIn(memory, self.context(r, p, c, handle, "continue"))

    def test_missing_query_can_use_explicit_current_task_goal(self):
        r, p, c, handle = self.runtime()
        r.task_update(p, c, "orbitguard constraints", {"mode": "analysis"})
        with patch.object(r, "search", return_value={"items": []}) as search:
            text = self.context(r, p, c, handle, _query_available=False)
        self.assertEqual(search.call_args.args[1], "orbitguard constraints")
        self.assertIn("task=", text)

    def test_missing_query_preserves_task_and_execution_notices_without_topic(self):
        r, p, c, handle = self.runtime()
        r.config["enforce_task_policy"] = True
        r.task_update(p, c, "继续检查", {"mode": "analysis"})
        text = self.context(r, p, c, handle, _query_available=False)
        self.assertIn("task=", text)
        self.assertIn("执行保护：", text)

    def test_missing_query_does_not_resume_completed_task_goal(self):
        r, p, c, handle = self.runtime()
        r.task_update(p, c, "orbitguard constraints", {"mode": "analysis"})
        r.task_update(p, c, "orbitguard constraints", status="delivered")
        with patch.object(r, "search", side_effect=AssertionError("completed goal reused")):
            text = self.context(r, p, c, handle, _query_available=False)
        self.assertIn("status=delivered", text)

    def test_missing_query_keeps_real_fault_retrieval(self):
        r, p, c, handle = self.runtime()
        task = r.task_update(p, c, "orbitguard constraints", {"mode": "analysis"})
        with r.transaction():
            r._queue_fault(p, c, None, task, r.session_epoch(),
                {"tool_name": "Compiler", "tool_response": {"exit_code": 1, "error": "E_ORBIT"}})
        with patch.object(r, "search", return_value={"items": []}) as search:
            self.context(r, p, c, handle, _query_available=False)
        self.assertIn("E_ORBIT", search.call_args.args[1])
        self.assertEqual(r.db.execute("SELECT value FROM metrics WHERE project=? AND name='failure_retrievals'",
                                     (p,)).fetchone()[0], 1)

    def test_missing_query_retains_file_notices_and_explicit_file_context(self):
        r, p, c, handle = self.runtime()
        path = self.workspace / "parser.py"
        path.write_text("original", encoding="utf-8")
        memory = self.record(r, p, file=True)
        self.assertIn(memory, self.context(r, p, c, handle, "orbitguard"))
        path.write_text("changed file size", encoding="utf-8")
        text = self.context(r, p, c, handle, _query_available=False)
        self.assertIn(memory, text)
        self.assertIn('"file_state_changed":true', text)
        self.assertIn(memory, self.context(r, p, c, handle, _query_available=False,
            tool_only=True, raw={"tool_name": "Read", "tool_input": {"file_path": "parser.py"}}))

    def test_unavailable_query_contract_rejects_nonempty_and_nonboolean_flags(self):
        r, p, c, handle = self.runtime()
        with self.assertRaisesRegex(ContractError, "must be empty"):
            self.context(r, p, c, handle, "orbitguard", _query_available=False)
        with self.assertRaisesRegex(ContractError, "must be boolean"):
            self.context(r, p, c, handle, _query_available=0)


if __name__ == "__main__":
    unittest.main()
