"""Protocol completion can defer exact status tools, never certify their success."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.antigravity import AntigravityAdapter
from agent_learning.dsh import DshAdapter
from tests import test_phase_local_deferral as fixtures


class PhaseStatusCompletionTests(unittest.TestCase):
    runtime = fixtures.PhaseLocalDeferralTests.runtime
    host_events = fixtures.PhaseLocalDeferralTests.host_events
    deliver = fixtures.PhaseLocalDeferralTests.deliver
    setup_host = fixtures.PhaseLocalDeferralTests.setup_host
    begin = fixtures.PhaseLocalDeferralTests.begin
    finish = fixtures.PhaseLocalDeferralTests.finish
    pending_packet = fixtures.PhaseLocalDeferralTests.pending_packet

    def setUp(self):
        base = Path(os.environ.get("ALP_TEST_ROOT", Path(__file__).resolve().parents[1]
                                  / "build" / "local" / "phase-status-completion-tests"))
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="status-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"ALP_DSH_REVIEW_PROFILE": ""}).start()

    def stage(self, host="codex", suffix=""):
        runtime, project, send, conversation = self.setup_host(host, suffix)
        self.begin(runtime, project, send, conversation)
        return runtime, project, send, conversation

    def tool(self, runtime, *, name=None, response=None, pre=True, post=True, number=1):
        suffix = runtime.home.name.removesuffix("-home").removeprefix(runtime.host)
        native = "routine" + suffix
        name = name or ("learning_status" if runtime.host == "dsh"
                        else "mcp__agent_learning__learning_status")
        response = response if response is not None else {"isError": False}
        if runtime.host == "antigravity":
            # Only enforce installations connect a native pre-tool callback;
            # observe-only completions must stay unbound and non-routine.
            adapter = AntigravityAdapter(runtime, self.workspace, "enforce" if pre else "observe")
            event = {"conversationId": native, "workspacePaths": [str(self.workspace)],
                     "stepIdx": number, "toolCall": {"name": name, "args": {}},
                     "error": "failed" if response.get("isError") is True else ""}
            if pre:
                adapter.handle("PreToolUse", event)
            if post:
                adapter.handle("PostToolUse", event)
        elif runtime.host == "dsh":
            adapter = DshAdapter(runtime, self.workspace)
            event = {"sessionId": native, "turn": 1, "callId": "status-" + str(number),
                     "tool_name": name, "tool_input": {}, "tool_response": response}
            if pre:
                adapter.handle("PreToolUse", event)
            if post:
                adapter.handle("PostToolUse", event)
        else:
            event = {"cwd": str(self.workspace), "session_id": native, "turn_id": "status-turn",
                     "tool_use_id": "status-" + str(number), "tool_name": name,
                     "tool_input": {}, "tool_response": response}
            if pre:
                runtime.handle_hook({**event, "hook_event_name": "PreToolUse"})
            if post:
                runtime.handle_hook({**event, "hook_event_name": "PostToolUse"})

    def test_native_completion_defers_exact_status_without_rewriting_observation(self):
        for host in ("codex", "dsh", "antigravity"):
            with self.subTest(host=host):
                runtime, project, send, conversation = self.stage(host)
                self.tool(runtime)
                observed = runtime.db.execute("SELECT outcome FROM events WHERE conversation=? "
                                              "AND type='PostToolUse'", (conversation,)).fetchone()
                self.assertEqual(observed[0], "call_completed")
                phase = self.finish(runtime, project, send, conversation)
                self.assertEqual(phase["state"], "routine_deferred")
                self.assertEqual(runtime.db.execute("SELECT observation FROM operations").fetchone()[0],
                                 "call_completed", "deferral is not business success or evidence promotion")
                with patch("agent_learning.admin.subprocess.Popen") as model:
                    admin_once(runtime, project)
                model.assert_not_called()
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM review_receipts").fetchone()[0], 0)

    def test_native_context_status_and_dsh_exact_aliases_are_supported(self):
        variants = (("codex", "mcp__agent_learning__session_context"),
                    ("antigravity", "mcp__agent_learning__session_context"),
                    ("dsh", "session_context"), ("dsh", "agent_learning_session_context"),
                    ("dsh", "agent_learning_learning_status"))
        for index, (host, name) in enumerate(variants):
            with self.subTest(host=host, name=name):
                runtime, project, send, conversation = self.stage(host, "-alias-" + str(index))
                self.tool(runtime, name=name)
                self.assertEqual(self.finish(runtime, project, send, conversation)["state"], "routine_deferred")

    def test_existing_explicit_success_still_defers(self):
        runtime, project, send, conversation = self.stage()
        self.tool(runtime, response={"success": True})
        self.assertEqual(self.finish(runtime, project, send, conversation)["state"], "routine_deferred")

    def test_custom_names_wrappers_and_write_tools_keep_normal_review(self):
        variants = (("codex", "learning_status"), ("codex", "mcp__other__learning_status"),
                    ("dsh", "other_learning_status"), ("dsh", "agent_learning_task_update"),
                    ("antigravity", "call_mcp_tool"), ("antigravity", "evil_mcp__agent_learning__session_context"))
        for index, (host, name) in enumerate(variants):
            with self.subTest(host=host, name=name):
                runtime, project, send, conversation = self.stage(host, "-custom-" + str(index))
                self.tool(runtime, name=name)
                self.assertNotEqual(self.finish(runtime, project, send, conversation)["state"], "routine_deferred")

    def test_failed_unknown_running_and_partial_callbacks_keep_normal_review(self):
        variants = ({"isError": True}, {"output": "unknown result"},
                    {"session_id": "running"}, {"isError": False, "session_id": "running"},
                    {"isError": False, "truncated": True})
        for index, response in enumerate(variants):
            with self.subTest(response=response):
                runtime, project, send, conversation = self.stage(suffix="-terminal-" + str(index))
                self.tool(runtime, response=response)
                self.assertNotEqual(self.finish(runtime, project, send, conversation)["state"], "routine_deferred")

    def test_post_without_pre_remains_unreviewed_normal_material(self):
        for host in ("codex", "dsh", "antigravity"):
            with self.subTest(host=host):
                runtime, project, send, conversation = self.stage(host, "-orphan")
                self.tool(runtime, pre=False)
                self.assertNotEqual(self.finish(runtime, project, send, conversation)["state"], "routine_deferred")

    def test_incomplete_pair_running_operation_or_wrong_identity_keeps_normal_review(self):
        variants = ("missing_pre", "state", "decision", "task_id", "tool_name", "host_epoch", "conflict")
        for number, variant in enumerate(variants):
            with self.subTest(variant=variant):
                runtime, project, send, conversation = self.stage(suffix="-identity-" + str(number))
                self.tool(runtime)
                if variant == "missing_pre":
                    runtime.db.execute("DELETE FROM events WHERE type='PreToolUse'")
                else:
                    values = {"state": "running", "decision": "unobserved", "task_id": "another-task",
                              "tool_name": "another-tool", "host_epoch": -1, "conflict": 1}
                    runtime.db.execute("UPDATE operations SET " + variant + "=?", (values[variant],))
                self.assertNotEqual(self.finish(runtime, project, send, conversation)["state"], "routine_deferred")

    def test_completed_status_call_cannot_hide_new_fact_in_reply(self):
        runtime, project, send, conversation = self.stage()
        self.tool(runtime)
        phase = self.finish(runtime, project, send, conversation,
                            reply="Agent Learning 连接正常。项目发布需要保留固定知识目录。")
        self.assertNotEqual(phase["state"], "routine_deferred")


if __name__ == "__main__":
    unittest.main()
