"""会话归属、换号边界与记录/整理分离的真实临时数据库回归。"""
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.runtime import ContractError, Runtime


class SessionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.home = Path(self.temp.name) / "state"
        self.r = Runtime(self.home)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "chat", "诊断模块加载", {"mode": "analysis"})

    def tearDown(self):
        self.r.close()
        self.temp.cleanup()

    def hook(self, kind, **fields):
        return self.r.handle_hook({"hook_event_name": kind, "session_id": "chat", "cwd": str(self.workspace), **fields})

    def handle(self, kind="SessionStart", **fields):
        text = self.hook(kind, **fields)["hookSpecificOutput"]["additionalContext"]
        return re.search(r"context_handle=([a-f0-9]{32})", text).group(1)

    def published(self):
        problem = self.r.feedback(self.p, "chat", "模块加载错误", "operation")
        packet = self.r.review_packet(self.p, problem["id"])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="模块加载诊断", content="核对模块路径与当前解释器", category="operation",
            applicability="模块路径问题", limitations="报告证据", validation_level="reported",
            evidence_refs=[e["id"] for e in packet["events"]])
        return self.r.publish(self.p, candidate["candidate_id"])

    def test_failed_update_does_not_poison_session_binding(self):
        with self.assertRaises(ContractError):
            self.r.task_update(self.p, "new", "目标", expected_revision=4)
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM session_projects WHERE conversation='new'").fetchone())
        other = self.r.project(Path(self.temp.name) / "other")
        self.assertEqual(self.r.task_update(other, "new", "正确项目")["goal"], "正确项目")

    def test_handle_scope_and_compression_recovery(self):
        handle = self.handle()
        self.assertEqual(handle, self.handle("SessionStart", source="compact"))
        self.assertEqual(handle, self.handle("UserPromptSubmit", prompt="继续"))
        context = self.r.session_context(self.p, handle)
        self.assertEqual(context["task"]["revision"], 1)
        self.assertEqual(context["conversation_id"], "chat")
        self.assertNotIn("context_handle", self.r.session_context(self.p))
        with self.assertRaisesRegex(ContractError, "context_mismatch"):
            self.r.access_context(self.p, handle, "another")
        other = self.r.project(Path(self.temp.name) / "other")
        with self.assertRaisesRegex(ContractError, "context_mismatch"):
            self.r.access_context(other, handle)

    def test_identity_survives_small_injection_budget(self):
        self.r.config["injection_max_chars"] = 128
        self.assertEqual(len(self.handle()), 32)

    def test_account_change_between_registration_and_injection_rejects_stale_identity(self):
        access = self.r.register_context(self.p, "chat")
        self.r.account_changed()
        with self.assertRaisesRegex(ContractError, "reconnect_required"):
            self.r._context(self.p, "chat", context_handle=access["context_handle"], expected_epoch=access["host_epoch"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM contexts").fetchone()[0], 0)

    def test_account_change_keeps_knowledge_and_invalidates_old_handle(self):
        item, handle = self.published(), self.handle()
        first = self.r.account_changed("login-event-1")
        self.assertTrue(first["changed"])
        self.assertFalse(self.r.account_changed("login-event-1")["changed"])
        self.assertEqual(self.r.host_epoch(), 1)
        self.assertEqual(self.r.project(self.workspace), self.p)
        self.assertEqual(self.r.read(self.p, item["knowledge_id"])["revision"], 1)
        with self.assertRaisesRegex(ContractError, "reconnect_required"):
            self.r.task_update(self.p, "chat", "错误回写", context_handle=handle, expected_epoch=0)
        self.assertEqual(self.r.task(self.p, "chat")["goal"], "诊断模块加载")
        replacement = self.handle("UserPromptSubmit", prompt="模块加载")
        self.assertNotEqual(replacement, handle)
        self.assertEqual(self.r.session_context(self.p, replacement)["host_epoch"], 1)

    def test_account_change_does_not_promote_late_tool_result(self):
        self.hook("PreToolUse", tool_use_id="running", tool_name="Bash")
        self.r.account_changed()
        self.hook("PostToolUse", tool_use_id="running", tool_name="Bash", tool_response={"exit_code": 0})
        self.assertEqual(self.r.db.execute("SELECT state FROM operations").fetchone()[0], "unknown")
        self.assertEqual(self.r.db.execute("SELECT outcome FROM events WHERE type='PostToolUse'").fetchone()[0], "unknown")

    def test_subagents_get_independent_injection_and_parent_task_protection(self):
        item = self.published()
        self.handle("UserPromptSubmit", prompt="模块加载")
        one = self.hook("SubagentStart", agent_id="child-1")
        two = self.hook("SubagentStart", agent_id="child-2")
        for result in (one, two):
            self.assertIn(item["knowledge_id"], result["hookSpecificOutput"]["additionalContext"])
        handle = re.search(r"context_handle=([a-f0-9]{32})", one["hookSpecificOutput"]["additionalContext"]).group(1)
        self.assertEqual(self.r.session_context(self.p, handle)["task_access"], "parent_read_only")
        with self.assertRaisesRegex(ContractError, "parent_task_read_only"):
            self.r.task_update(self.p, "chat", "子任务不能覆盖父任务", context_handle=handle)
        unknown = self.hook("SubagentStart")
        self.assertNotIn("context_handle=", unknown["hookSpecificOutput"]["additionalContext"])

    def test_subagent_cannot_attach_another_agents_evidence(self):
        handle = self.handle("SubagentStart", agent_id="child-1")
        self.handle("SubagentStart", agent_id="child-2")
        self.hook("PostToolUse", agent_id="child-2", tool_name="Bash", tool_response={"exit_code": 0})
        event = self.r.db.execute("SELECT id FROM events WHERE agent='child-2' AND type='PostToolUse'").fetchone()[0]
        with self.assertRaisesRegex(ContractError, "current subagent"):
            self.r.feedback(self.p, "chat", "错误引用", event_refs=[event], context_handle=handle)

    def test_tool_call_completion_is_not_business_success(self):
        self.hook("PostToolUse", tool_use_id="mcp-result", tool_name="mcp__example__invoke",
                  tool_response={"isError": False, "content": [{"type": "text", "text": "queued"}]})
        self.assertEqual(self.r.db.execute("SELECT state FROM operations").fetchone()[0], "call_completed")
        self.assertEqual(Runtime.outcome({"isError": False}), "call_completed")

    def test_stop_with_late_message_preserves_delivery_and_deduplicates_it(self):
        self.r.task_update(self.p, "chat", "诊断模块加载", status="delivered")
        self.hook("Stop", turn_id="turn", last_assistant_message=None)
        self.hook("Stop", turn_id="turn", last_assistant_message="结果已经报告")
        self.hook("Stop", turn_id="turn", last_assistant_message="结果已经报告")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='Stop'").fetchone()[0], 2)

    def test_admin_launch_failure_does_not_claim_event_loss(self):
        self.r.feedback(self.p, "chat", "待审核错误")
        self.r.config["admin"]["enabled"] = True
        with patch.object(self.r, "_launch_admin", side_effect=OSError("private details")):
            result = self.hook("Stop")
        self.assertIn("event_recorded; admin_schedule_failed", result["systemMessage"])
        self.assertNotIn("private details", result["systemMessage"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='Stop'").fetchone()[0], 1)

    def test_admin_does_not_spawn_without_pending_material(self):
        self.r.config["admin"]["enabled"] = True
        with patch.object(self.r, "_launch_admin") as launch:
            self.hook("Stop")
        launch.assert_not_called()

    def test_inflight_admin_account_change_keeps_budget_and_rejects_response(self):
        self.r.feedback(self.p, "chat", "需要审核模块错误")
        script = Path(self.temp.name) / "fake_reviewer.py"
        root = str(Path(__file__).resolve().parents[1])
        script.write_text("import json, os, pathlib, sys\n"
            + f"sys.path.insert(0, {root!r})\n"
            + "from agent_learning.runtime import Runtime\n"
            + "sys.stdin.buffer.read()\n"
            + "r=Runtime(os.environ['ALP_HOME']); r.account_changed('test-review-switch'); r.close()\n"
            + "output=pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1])\n"
            + "output.write_text(json.dumps({'decision':'skip','reason':'fake controlled response','candidate':None}), encoding='utf-8')\n",
            encoding="utf-8")
        self.r.config["admin"].update(enabled=True, command=[sys.executable, str(script)], max_calls_per_day=1)
        result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "account_changed_unknown")
        call = self.r.db.execute("SELECT state,started,host_epoch FROM admin_calls").fetchone()
        self.assertEqual(call["state"], "account_changed_unknown")
        self.assertIsNotNone(call["started"])
        self.assertEqual(call["host_epoch"], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)
        self.r.feedback(self.p, "chat", "不同的新材料")
        self.assertEqual(admin_once(self.r, self.p)["status"], "budget_exhausted")


if __name__ == "__main__":
    unittest.main()
