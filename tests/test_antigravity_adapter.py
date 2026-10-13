"""原生 Antigravity 事件、身份与审批差异的隔离保护检查。"""
import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.antigravity import AntigravityAdapter, reset_session
from agent_learning.runtime import ContractError, Runtime


class AntigravityAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-antigravity-adapter-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.home = Path(self.temp.name) / "state"
        self.r = Runtime(self.home, config={}, host="antigravity")
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.c = self.r.host_conversation("native-chat")
        self.task = self.r.task_update(self.p, self.c, "compiler error", {"mode": "execute"})
        self.adapter = AntigravityAdapter(self.r, self.workspace)

    def event(self, **fields):
        return {"conversationId": "native-chat", "workspacePaths": [str(self.workspace)],
                "transcriptPath": "not-read-transcript.jsonl", "modelName": "test-model", **fields}

    def invocation(self, number=0, steps=0, **fields):
        return self.adapter.handle("PreInvocation", self.event(invocationNum=number, initialNumSteps=steps, **fields))

    def tool(self, kind, step=0, name="run_command", arguments=None, error="", **fields):
        raw = self.event(stepIdx=step, toolCall={"name": name, "args": arguments or {
            "CommandLine": "python -B check.py", "Cwd": str(self.workspace), "WaitMsBeforeAsync": 100}}, **fields)
        if kind == "PostToolUse":
            raw["error"] = error
        return self.adapter.handle(kind, raw)

    def stop(self, number=0, idle=True, reason="model_stop", **fields):
        return self.adapter.handle("Stop", self.event(executionNum=number, fullyIdle=idle,
                                                     terminationReason=reason, **fields))

    def enforce(self):
        self.adapter = AntigravityAdapter(self.r, self.workspace, "enforce")

    def approve(self):
        task = self.r.task(self.p, self.c)
        return self.r.approve_execution(self.p, self.c, task["id"], task["write_version"],
            {"mode": "execute", "allowed_tools": ["run_command"],
             "tool_rules": {"run_command": {"action": "execute", "path_fields": ["Cwd"]}},
             "allowed_paths": [str(self.workspace)]}, workspace=str(self.workspace))

    def publish(self):
        problem = self.r.feedback(self.p, self.c, "compiler error", "operation")
        packet = self.r.review_packet(self.p, problem["id"])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="compiler error", content="Check compiler paths", category="operation",
            applicability="The recorded compiler environment", limitations="Reported only", validation_level="reported",
            evidence_refs=[item["id"] for item in packet["events"]])
        return self.r.publish(self.p, candidate["candidate_id"])

    def metric(self, name):
        row = self.r.db.execute("SELECT value FROM metrics WHERE project=? AND name=?", (self.p, name)).fetchone()
        return row[0] if row else 0

    def test_pre_invocation_registers_native_identity_without_prompt_fence(self):
        before = self.r.task(self.p, self.c)["execution_epoch"]
        result = self.invocation()
        text = result["injectSteps"][0]["ephemeralMessage"]
        self.assertIn("context_handle=", text)
        self.assertEqual(self.r.task(self.p, self.c)["execution_epoch"], before)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type IN ('SessionStart','UserPromptSubmit')").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT conversation FROM context_access").fetchone()[0], self.c)

    def test_ignores_prompt_transcript_and_private_thought_fields(self):
        with patch.object(Path, "read_text", side_effect=AssertionError("transcript must not be read")):
            result = self.invocation(prompt="untrusted different problem", thoughts="private model material")
        self.assertIn("context_handle=", result["injectSteps"][0]["ephemeralMessage"])
        row = self.r.db.execute("SELECT last_query FROM contexts WHERE project=? AND conversation=?", (self.p, self.c)).fetchone()
        self.assertEqual(row[0], "compiler error")
        receipts = self.r.db.execute("SELECT output FROM antigravity_receipts").fetchone()[0]
        self.assertNotIn("private model material", receipts)

    def test_ephemeral_cards_are_repeated_without_repeating_search(self):
        item = self.publish()
        first = self.invocation()
        searches = self.metric("search_queries")
        injected = self.metric("knowledge_injected")
        second = self.invocation(1, 1)
        self.assertIn(item["knowledge_id"], first["injectSteps"][0]["ephemeralMessage"])
        self.assertIn(item["knowledge_id"], second["injectSteps"][0]["ephemeralMessage"])
        self.assertEqual(self.metric("search_queries"), searches)
        self.assertEqual(self.metric("knowledge_injected"), injected)
        self.assertLessEqual(len(second["injectSteps"][0]["ephemeralMessage"]), self.r.config["injection_max_chars"])

    def test_same_invocation_replay_is_durable_and_does_not_repeat_retrieval(self):
        first = self.invocation()
        searches = self.metric("search_queries")
        second_runtime = Runtime(self.home, config={}, host="antigravity")
        try:
            adapter = AntigravityAdapter(second_runtime, self.workspace)
            result = adapter.handle("PreInvocation", self.event(invocationNum=0, initialNumSteps=0))
        finally:
            second_runtime.close()
        self.assertEqual(result, first)
        self.assertEqual(self.metric("search_queries"), searches)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM antigravity_receipts").fetchone()[0], 1)

    def test_replayed_invocation_cannot_reinject_revoked_card(self):
        item = self.publish()
        self.invocation()
        self.r.revoke(self.p, item["knowledge_id"], item["revision"], reason="obsolete")
        self.assertEqual(self.invocation(), {})
        next_invocation = self.invocation(1, 1)
        text = next_invocation["injectSteps"][0]["ephemeralMessage"]
        self.assertNotIn('"content_summary"', text)

    def test_observe_has_no_pre_binding_and_post_does_not_guess_task(self):
        self.assertEqual(self.tool("PreToolUse"), {})
        self.tool("PostToolUse", error="compiler failure")
        event = self.r.db.execute("SELECT * FROM events WHERE type='PostToolUse'").fetchone()
        self.assertEqual(event["outcome"], "failed")
        self.assertIsNone(event["task_id"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='PreToolUse'").fetchone()[0], 0)

    def test_empty_error_means_call_completed_not_success(self):
        self.tool("PostToolUse", error="")
        event = self.r.db.execute("SELECT * FROM events WHERE type='PostToolUse'").fetchone()
        self.assertEqual(event["outcome"], "call_completed")
        self.assertNotIn("exit_code", event["payload"])
        self.assertEqual(self.r.db.execute("SELECT observation FROM operations").fetchone()[0], "call_completed")

    def test_missing_error_also_does_not_create_success_evidence(self):
        self.adapter.handle("PostToolUse", self.event(stepIdx=0, toolCall={"name": "view_file", "args": {"AbsolutePath": str(self.workspace / "file")}}))
        self.assertEqual(self.r.db.execute("SELECT outcome FROM events WHERE type='PostToolUse'").fetchone()[0], "call_completed")

    def test_explicit_error_is_failed_even_if_text_mentions_zero_exit(self):
        self.tool("PostToolUse", error="unexpected data: exit_code=0")
        self.assertEqual(self.r.db.execute("SELECT outcome FROM events WHERE type='PostToolUse'").fetchone()[0], "failed")

    def test_failure_followup_uses_next_pre_invocation(self):
        self.invocation()
        self.tool("PostToolUse", error="compiler error: missing include path")
        pending = self.r.db.execute("SELECT COUNT(*) FROM context_faults WHERE state='pending'").fetchone()[0]
        self.assertEqual(pending, 1)
        before = self.metric("search_queries")
        self.invocation(1, 1)
        self.assertGreater(self.metric("search_queries"), before)

    def test_failure_without_any_task_is_queried_without_task_fabrication(self):
        orphan = self.event(conversationId="without-task", stepIdx=0, toolCall={"name": "run_command", "args": {"Cwd": str(self.workspace)}},
                            error="compiler error")
        self.adapter.handle("PostToolUse", orphan)
        pending = self.r.db.execute("SELECT state FROM antigravity_faults").fetchone()
        self.assertEqual(pending[0], "pending")
        before = self.metric("search_queries")
        self.adapter.handle("PreInvocation", self.event(conversationId="without-task", invocationNum=0, initialNumSteps=1))
        self.assertGreater(self.metric("search_queries"), before)
        self.assertEqual(self.r.db.execute("SELECT state FROM antigravity_faults").fetchone()[0], "queried")
        self.assertIsNone(self.r.task(self.p, self.r.host_conversation("without-task")))
        self.assertIsNone(self.r.db.execute("SELECT task_id FROM events WHERE type='PostToolUse'").fetchone()[0])

    def test_enforce_without_approval_denies(self):
        self.enforce()
        result = self.tool("PreToolUse")
        self.assertEqual(result["decision"], "deny")
        self.assertIn("execution_approval_required", result["reason"])

    def test_passed_execution_guard_keeps_host_approval(self):
        self.enforce()
        self.approve()
        result = self.tool("PreToolUse")
        self.assertEqual(result["decision"], "ask")
        self.assertNotIn("permissionOverrides", result)
        self.assertNotIn("injectSteps", result)

    def test_pre_context_notifications_are_not_consumed(self):
        item = self.publish()
        self.invocation()
        self.r.revoke(self.p, item["knowledge_id"], item["revision"], reason="old")
        self.enforce()
        self.approve()
        self.tool("PreToolUse")
        result = self.invocation(1, 1)
        self.assertIn(item["knowledge_id"], result["injectSteps"][0]["ephemeralMessage"])

    def test_pre_and_post_bind_original_task_only_when_pre_installed(self):
        self.enforce()
        self.approve()
        self.tool("PreToolUse")
        self.r.task_update(self.p, self.c, "new goal", new_task=True)
        self.tool("PostToolUse", error="late failure")
        event = self.r.db.execute("SELECT * FROM events WHERE type='PostToolUse'").fetchone()
        self.assertEqual(event["task_id"], self.task["id"])

    def test_duplicate_post_callback_is_not_an_extra_case(self):
        self.tool("PostToolUse", error="compiler error")
        self.tool("PostToolUse", error="compiler error")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='PostToolUse'").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM problems").fetchone()[0], 1)

    def test_conflicting_input_cannot_support_success(self):
        self.tool("PostToolUse", error="failure")
        self.tool("PostToolUse", arguments={"CommandLine": "different", "Cwd": str(self.workspace)}, error="")
        self.assertEqual(self.r.db.execute("SELECT conflict FROM antigravity_steps").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT state FROM operations").fetchone()[0], "conflict")
        self.assertEqual(self.r.db.execute("SELECT outcome FROM events WHERE type='PostToolUse' ORDER BY created DESC LIMIT 1").fetchone()[0], "conflict")

    def test_completed_step_cannot_replay_a_cached_permission(self):
        self.enforce()
        self.approve()
        self.assertEqual(self.tool("PreToolUse")["decision"], "ask")
        self.tool("PostToolUse")
        self.assertEqual(self.tool("PreToolUse")["decision"], "deny")

    def test_changed_task_cannot_reuse_old_pre_receipt(self):
        self.enforce()
        self.approve()
        self.tool("PreToolUse")
        self.r.task_update(self.p, self.c, "different task", new_task=True)
        self.approve()
        self.assertEqual(self.tool("PreToolUse")["decision"], "deny")

    def test_pre_replay_rechecks_changed_runtime_tool_filter(self):
        self.enforce()
        self.approve()
        self.assertEqual(self.tool("PreToolUse")["decision"], "ask")
        self.r.config["allowed_tools"] = ["view_file"]
        result = self.tool("PreToolUse")
        self.assertEqual(result["decision"], "deny")
        self.assertIn("tool_disallowed_by_config", result["reason"])

    def test_account_change_blocks_old_raw_invocation_and_pre(self):
        self.enforce()
        self.approve()
        self.invocation()
        self.tool("PreToolUse")
        self.r.account_changed("native-account-change")
        self.assertEqual(self.invocation(), {})
        self.assertEqual(self.tool("PreToolUse")["decision"], "deny")

    def test_late_post_after_account_change_remains_unassociated(self):
        self.enforce()
        self.approve()
        self.tool("PreToolUse")
        self.r.account_changed("account-2")
        self.tool("PostToolUse", error="late error")
        event = self.r.db.execute("SELECT * FROM events WHERE type='AntigravityPostToolUse'").fetchone()
        self.assertIsNone(event["task_id"])
        self.assertEqual(event["outcome"], "unknown")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='PostToolUse'").fetchone()[0], 0)

    def test_trajectory_regression_latches_ambiguity_and_denies(self):
        self.enforce()
        self.approve()
        self.invocation(1, 10)
        self.tool("PreToolUse", 10)
        self.invocation(2, 11)
        self.invocation(0, 0)
        row = self.r.db.execute("SELECT generation,ambiguous FROM antigravity_trajectories").fetchone()
        self.assertEqual(tuple(row), (2, 1))
        self.assertEqual(self.tool("PreToolUse", 11)["decision"], "deny")
        self.tool("PostToolUse", 11, error="new error")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='AntigravityPostToolUse'").fetchone()[0], 1)

    def test_old_raw_invocation_replay_cannot_bypass_regression_detection(self):
        self.invocation(0, 0)
        self.invocation(1, 10)
        self.assertEqual(self.invocation(0, 0), {})
        row = self.r.db.execute("SELECT generation,ambiguous FROM antigravity_trajectories").fetchone()
        self.assertEqual(tuple(row), (2, 1))
        epoch = self.r.task(self.p, self.c)["execution_epoch"]
        self.invocation(0, 0)
        self.assertEqual(self.r.task(self.p, self.c)["execution_epoch"], epoch)

    def test_trusted_reset_rebinds_same_native_counters_and_pauses_execution(self):
        old = self.invocation(0, 0)["injectSteps"][0]["ephemeralMessage"]
        handle = self.r.db.execute("SELECT handle FROM context_access").fetchone()[0]
        self.invocation(1, 10)
        reset = reset_session(self.r, self.workspace, "native-chat")
        new = self.invocation(0, 0)["injectSteps"][0]["ephemeralMessage"]
        self.assertEqual(reset["generation"], 2)
        self.assertNotEqual(old, new)
        self.assertEqual(self.r.task(self.p, self.c)["status"], "paused")
        with self.assertRaises(ContractError):
            self.r.access_context(self.p, handle)

    def test_account_change_can_reuse_counters_only_after_trusted_reset(self):
        self.invocation()
        self.r.account_changed("switch")
        self.assertEqual(self.invocation(), {})
        reset_session(self.r, self.workspace, "native-chat")
        self.assertIn("injectSteps", self.invocation())

    def test_native_tool_cannot_present_other_conversation_handle(self):
        other_conversation = self.r.host_conversation("other-native-chat")
        other_handle = self.r.register_context(self.p, other_conversation)["context_handle"]
        self.enforce()
        output = self.tool("PreToolUse", name="mcp__agent_learning__task_update", arguments={"context_handle": other_handle, "goal": "wrong chat"})
        self.assertEqual(output["decision"], "deny")
        self.assertIn("native_conversation_mismatch", output["reason"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)

    def test_known_native_learning_write_requires_handle(self):
        self.enforce()
        output = self.tool("PreToolUse", name="mcp__agent_learning__task_update", arguments={"goal": "missing handle"})
        self.assertEqual(output["decision"], "deny")
        self.assertIn("handle_required", output["reason"])

    def test_known_native_learning_write_uses_own_verified_handle(self):
        handle = self.r.register_context(self.p, self.c)["context_handle"]
        self.enforce()
        output = self.tool("PreToolUse", name="mcp__agent_learning__task_update", arguments={"context_handle": handle, "goal": "right chat"})
        self.assertEqual(output["decision"], "ask")

    def test_stop_records_idle_without_claiming_delivery(self):
        before = self.r.task(self.p, self.c)["execution_epoch"]
        result = self.stop()
        self.assertEqual(result, {"decision": "stop"})
        self.assertEqual(self.r.task(self.p, self.c)["status"], "active")
        self.assertEqual(self.r.task(self.p, self.c)["execution_epoch"], before + 1)
        event = self.r.db.execute("SELECT * FROM events WHERE type='AntigravityStop'").fetchone()
        self.assertTrue(json.loads(event["payload"])["fullyIdle"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)

    def test_stop_replay_does_not_repeat_execution_fence(self):
        self.stop()
        after = self.r.task(self.p, self.c)["execution_epoch"]
        self.stop()
        self.assertEqual(self.r.task(self.p, self.c)["execution_epoch"], after)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='AntigravityStop'").fetchone()[0], 1)

    def test_non_idle_stop_does_not_launch_admin_or_claim_completion(self):
        self.r.config["admin"]["enabled"] = True
        self.r.feedback(self.p, self.c, "pending review")
        before = self.r.task(self.p, self.c)["execution_epoch"]
        with patch.object(self.r, "_launch_admin") as launch:
            self.stop(idle=False)
        launch.assert_not_called()
        self.assertEqual(self.r.task(self.p, self.c)["execution_epoch"], before)

    def test_stop_launch_failure_preserves_observation(self):
        self.r.config["admin"].update(enabled=True, antigravity_command=[sys.executable])
        self.r.feedback(self.p, self.c, "pending review")
        with patch.object(self.r, "_launch_admin", side_effect=OSError("private credential")):
            self.assertEqual(self.stop(), {"decision": "stop"})
        self.assertEqual(self.metric("antigravity_schedule_failed"), 1)
        self.assertNotIn("private credential", self.r.db.execute("SELECT payload FROM events WHERE type='AntigravityStop'").fetchone()[0])

    def other_runtime(self):
        other = Runtime(self.home, config={}, host="antigravity")
        self.addCleanup(other.close)
        return other

    def test_account_change_before_context_binding_does_not_create_new_epoch_handle(self):
        other, original = self.other_runtime(), self.r.register_context
        def switch_then_bind(*args, **kwargs):
            other.account_changed("before-binding")
            return original(*args, **kwargs)
        with patch.object(self.r, "register_context", side_effect=switch_then_bind):
            with self.assertRaisesRegex(ContractError, "reconnect_required"):
                self.invocation()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_access").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM contexts").fetchone()[0], 0)

    def test_trusted_reset_before_context_binding_rejects_old_generation(self):
        other, original = self.other_runtime(), self.r.register_context
        def reset_then_bind(*args, **kwargs):
            reset_session(other, self.workspace, "native-chat")
            return original(*args, **kwargs)
        with patch.object(self.r, "register_context", side_effect=reset_then_bind):
            with self.assertRaisesRegex(ContractError, "trajectory was reset"):
                self.invocation()
        self.assertEqual(self.r.task(self.p, self.c)["status"], "paused")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_access").fetchone()[0], 0)

    def test_reset_during_retrieval_cannot_commit_old_context(self):
        self.publish()
        other, original = self.other_runtime(), self.r.search
        def reset_then_search(*args, **kwargs):
            reset_session(other, self.workspace, "native-chat")
            return original(*args, **kwargs)
        with patch.object(self.r, "search", side_effect=reset_then_search):
            with self.assertRaisesRegex(ContractError, "trajectory was reset"):
                self.invocation()
        row = self.r.db.execute("SELECT injected,last_query FROM contexts WHERE conversation=?", (self.c,)).fetchone()
        self.assertEqual(tuple(row), ("[]", ""))
        self.assertEqual(self.metric("knowledge_injected"), 0)

    def test_old_stop_cannot_fence_task_created_between_phases(self):
        other, original, latest = self.other_runtime(), self.adapter._stop, {}
        def replace_then_stop(*args, **kwargs):
            latest.update(other.task_update(self.p, self.c, "new goal", new_task=True))
            return original(*args, **kwargs)
        with patch.object(self.adapter, "_stop", side_effect=replace_then_stop):
            with self.assertRaisesRegex(ContractError, "native task changed"):
                self.stop()
        self.assertEqual(self.r.task(self.p, self.c), latest)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='AntigravityStop'").fetchone()[0], 0)

    def test_old_stop_cannot_fence_task_after_account_change(self):
        other, original, latest = self.other_runtime(), self.adapter._stop, {}
        def switch_then_stop(*args, **kwargs):
            other.account_changed("before-stop")
            latest.update(other.task(self.p, self.c))
            return original(*args, **kwargs)
        with patch.object(self.adapter, "_stop", side_effect=switch_then_stop):
            with self.assertRaisesRegex(ContractError, "reconnect_required"):
                self.stop()
        self.assertEqual(self.r.task(self.p, self.c), latest)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='AntigravityStop'").fetchone()[0], 0)

    def test_account_change_before_core_capture_does_not_bind_new_epoch_operation(self):
        self.enforce()
        self.approve()
        other, original = self.other_runtime(), self.r.handle_hook
        def switch_then_capture(*args, **kwargs):
            other.account_changed("before-capture")
            return original(*args, **kwargs)
        with patch.object(self.r, "handle_hook", side_effect=switch_then_capture):
            with self.assertRaisesRegex(ContractError, "reconnect_required"):
                self.tool("PreToolUse")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='PreToolUse'").fetchone()[0], 0)

    def test_account_change_before_schedule_does_not_claim_new_epoch_administrator(self):
        self.r.config["admin"]["enabled"] = True
        self.r.feedback(self.p, self.c, "pending review")
        other, original = self.other_runtime(), self.adapter._schedule
        def switch_then_schedule(*args, **kwargs):
            other.account_changed("before-schedule")
            return original(*args, **kwargs)
        with patch.object(self.adapter, "_schedule", side_effect=switch_then_schedule), patch.object(self.r, "_launch_admin") as launch:
            self.assertEqual(self.stop(), {"decision": "stop"})
        launch.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_dispatch").fetchone()[0], 0)

    def test_fixed_workspace_must_be_explicitly_mounted(self):
        other = Path(self.temp.name) / "other"
        other.mkdir()
        with self.assertRaisesRegex(ContractError, "not_mounted"):
            self.invocation(workspacePaths=[str(other)])

    def test_multi_workspace_read_with_explicit_fixed_resource_is_associated(self):
        other = Path(self.temp.name) / "other"
        other.mkdir()
        self.tool("PostToolUse", name="view_file", arguments={"AbsolutePath": str(self.workspace / "file")},
                  workspacePaths=[str(other), str(self.workspace)])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='PostToolUse'").fetchone()[0], 1)

    def test_multi_workspace_tool_without_resource_is_unassociated(self):
        other = Path(self.temp.name) / "other"
        other.mkdir()
        self.tool("PostToolUse", name="search_web", arguments={"query": "example"},
                  workspacePaths=[str(other), str(self.workspace)])
        event = self.r.db.execute("SELECT * FROM events WHERE type='AntigravityPostToolUse'").fetchone()
        self.assertIsNone(event["task_id"])

    def test_resource_in_another_workspace_is_not_assigned_to_selected_project(self):
        other = Path(self.temp.name) / "other"
        other.mkdir()
        self.enforce()
        result = self.tool("PreToolUse", arguments={"CommandLine": "true", "Cwd": str(other)},
                           workspacePaths=[str(other), str(self.workspace)])
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)

    def test_invalid_native_types_do_not_create_receipts(self):
        invalid = [self.event(invocationNum=True, initialNumSteps=0), self.event(invocationNum=0, initialNumSteps=-1)]
        for raw in invalid:
            with self.assertRaises(ContractError):
                self.adapter.handle("PreInvocation", raw)
        with self.assertRaises(ContractError):
            self.adapter.handle("PostToolUse", self.event(toolCall={"name": "view_file", "args": {}}, error=""))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM antigravity_receipts").fetchone()[0], 0)


    def test_model_diagnostics_do_not_repeat_stop_or_invalidation(self):
        self.stop(modelName="model-A")
        self.r.task_update(self.p, self.c, "new explicit task", new_task=True)
        before = self.r.task(self.p, self.c)["execution_epoch"]
        self.assertEqual(self.stop(modelName="model-B"), {"decision": "stop"})
        self.assertEqual(self.r.task(self.p, self.c)["execution_epoch"], before)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='AntigravityStop'").fetchone()[0], 1)

    def test_model_diagnostics_do_not_duplicate_invocation_or_post(self):
        first = self.invocation(modelName="model-A")
        self.assertEqual(self.invocation(modelName="model-B"), first)
        self.tool("PostToolUse", error="compiler error", modelName="model-A")
        self.tool("PostToolUse", error="compiler error", modelName="model-B")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM antigravity_receipts").fetchone()[0], 2)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='PostToolUse'").fetchone()[0], 1)

    def test_actual_secret_input_change_denies_reused_pre_step(self):
        self.enforce()
        self.approve()
        def args(secret):
            return {"Cwd": str(self.workspace), "CommandLine": "check --password=" + secret}
        self.assertEqual(self.tool("PreToolUse", arguments=args("first-fixture-secret"))["decision"], "ask")
        self.assertEqual(self.tool("PreToolUse", arguments=args("second-fixture-secret"))["decision"], "deny")
        self.assertEqual(self.r.db.execute("SELECT conflict FROM antigravity_steps").fetchone()[0], 1)
        events = self.r.db.execute("SELECT outcome,payload FROM events WHERE type='PreToolUse' ORDER BY created").fetchall()
        self.assertEqual([row[0] for row in events], ["unknown", "denied"])
        self.assertNotIn("fixture-secret", str([row[1] for row in events]))

    def test_actual_secret_input_change_records_conflicting_post(self):
        for secret in ("first-fixture-secret", "second-fixture-secret"):
            self.tool("PostToolUse", arguments={"Cwd": str(self.workspace), "CommandLine": "check --password=" + secret})
        events = self.r.db.execute("SELECT outcome,payload FROM events WHERE type='PostToolUse' ORDER BY created").fetchall()
        self.assertEqual([row[0] for row in events], ["call_completed", "conflict"])
        self.assertEqual(self.r.db.execute("SELECT state FROM operations").fetchone()[0], "conflict")
        self.assertNotIn("fixture-secret", str([row[1] for row in events]))

    def test_legacy_native_identity_requires_reset_without_replaying_stop(self):
        item = self.publish()
        self.invocation()
        self.r.db.execute("UPDATE antigravity_receipts SET raw_digest=substr(raw_digest,4)")
        before = self.r.task(self.p, self.c)["execution_epoch"]
        self.enforce()
        self.assertEqual(self.tool("PreToolUse")["decision"], "deny")
        self.assertEqual(self.stop(), {"decision": "stop"})
        self.assertEqual(self.r.task(self.p, self.c)["execution_epoch"], before)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='AntigravityStop'").fetchone()[0], 0)
        self.assertIn("trusted antigravity-session-reset", json.dumps(self.invocation(1)))
        reset_session(self.r, self.workspace, "native-chat")
        self.assertIn("context_handle=", json.dumps(self.invocation()))
        self.assertEqual(self.r.read(self.p, item["knowledge_id"])["revision"], item["revision"])

    def test_native_reset_rejects_codex_runtime_before_any_write(self):
        other = Runtime(Path(self.temp.name) / "codex-home", config={})
        self.addCleanup(other.close)
        before = other.db.total_changes
        with self.assertRaisesRegex(ContractError, "antigravity_host_required"):
            reset_session(other, self.workspace, "codex-chat")
        self.assertEqual(other.db.total_changes, before)


class AntigravityDispatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "hooks" / "antigravity_dispatch.py"
        spec = importlib.util.spec_from_file_location("alp_antigravity_dispatch_test", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def dispatch(self, data, event="PreToolUse", mode="enforce", extra=None):
        with tempfile.TemporaryDirectory(prefix="alp-antigravity-dispatch-") as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            home = Path(temp) / "state"
            argv = ["--event", event, "--workspace", str(workspace), "--home", str(home), "--hook-mode", mode]
            output = io.StringIO()
            with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")), contextlib.redirect_stdout(output):
                self.assertEqual(self.module.main(argv + (extra or [])), 0)
            return json.loads(output.getvalue()), home.exists()

    def test_invalid_json_denies_fixed_enforce_entry_without_creating_database(self):
        output, created = self.dispatch(b"broken JSON")
        self.assertEqual(output["decision"], "deny")
        self.assertIn("input_invalid", output["reason"])
        self.assertFalse(created)

    def test_observe_invalid_json_is_native_empty_response(self):
        output, created = self.dispatch(b"broken JSON", "PostToolUse", "observe")
        self.assertEqual(output, {})
        self.assertFalse(created)

    def test_stop_failure_always_allows_stop_with_required_decision(self):
        output, created = self.dispatch(b"broken JSON", "Stop", "observe")
        self.assertEqual(output, {"decision": "stop"})
        self.assertFalse(created)

    def test_disabled_stop_still_returns_required_native_decision(self):
        with patch.dict("os.environ", {"ALP_DISABLED": "1"}):
            output, created = self.dispatch(b"broken JSON", "Stop", "observe")
        self.assertEqual(output, {"decision": "stop"})
        self.assertFalse(created)

    def test_size_limit_is_checked_before_runtime(self):
        with patch.object(self.module, "MAX_INPUT_BYTES", 10):
            output, created = self.dispatch(b"x" * 11)
        self.assertEqual(output["decision"], "deny")
        self.assertIn("input_too_large", output["reason"])
        self.assertFalse(created)

    def test_fixed_event_cannot_be_changed_by_input(self):
        output, created = self.dispatch(json.dumps({"hookEventName": "Stop"}).encode())
        self.assertEqual(output["decision"], "deny")
        self.assertIn("event_mismatch", output["reason"])
        self.assertFalse(created)

    def test_duplicate_arguments_deny_and_do_not_reconfigure_home(self):
        output, created = self.dispatch(b"{}", extra=["--home", "some-relative-other-home"])
        self.assertEqual(output["decision"], "deny")
        self.assertIn("arguments_invalid", output["reason"])
        self.assertFalse(created)

    def test_known_pre_entry_without_mode_fails_closed(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.module.main(["--event", "PreToolUse"])
        self.assertEqual(json.loads(output.getvalue())["decision"], "deny")

    def test_error_response_does_not_expose_original_input(self):
        output, _ = self.dispatch(b'{"conversationId":"secret-password"}')
        self.assertEqual(output["decision"], "deny")
        self.assertNotIn("secret-password", json.dumps(output))


class NativeFaultRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-native-fault-retry-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(Path(self.temp.name) / "home", config={}, host="antigravity")
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.c = self.r.host_conversation("taskless-chat")
        self.adapter = AntigravityAdapter(self.r, self.workspace)

    def raw(self, **fields):
        return {"conversationId": "taskless-chat", "workspacePaths": [str(self.workspace)], **fields}

    def fault(self, step, error):
        return self.adapter.handle("PostToolUse", self.raw(stepIdx=step, error=error,
            toolCall={"name": "run_command", "args": {"Cwd": str(self.workspace)}}))

    def invocation(self, number, steps=2):
        return self.adapter.handle("PreInvocation", self.raw(invocationNum=number, initialNumSteps=steps))

    def fault_states(self):
        return [row[0] for row in self.r.db.execute("SELECT state FROM antigravity_faults ORDER BY created")]

    def test_retryable_taskless_fault_retains_priority_and_cooling(self):
        self.fault(0, "fault-A")
        self.fault(1, "fault-B")
        queries = []
        def search(project, query, **kwargs):
            queries.append(query)
            return {"items": [], "retrieval": {"retryable": len(queries) == 1, "retry_after_seconds": 5}}
        with patch.object(self.r, "search", side_effect=search):
            self.invocation(0)
            self.assertEqual(self.fault_states(), ["pending", "pending"])
            self.invocation(1)
            self.assertEqual(len(queries), 1)
            self.assertEqual(self.fault_states(), ["pending", "pending"])
            signature = self.r.db.execute("SELECT retrieval_signature FROM contexts WHERE conversation=?", (self.c,)).fetchone()[0]
            deadline = float(signature.split(":", 2)[1])
            with patch("agent_learning.knowledge_context.time.time", return_value=deadline + 1):
                self.invocation(2)
                self.assertEqual(self.fault_states(), ["queried", "pending"])
                self.invocation(3)
        self.assertEqual(queries[0], queries[1])
        self.assertIn("fault-A", queries[1])
        self.assertIn("fault-B", queries[2])
        self.assertEqual(self.fault_states(), ["queried", "queried"])
        self.assertIsNone(self.r.task(self.p, self.c))

    def test_taskless_result_waits_for_budget_without_repeating_search(self):
        problem = self.r.feedback(self.p, self.c, "compiler error", "operation")
        packet = self.r.review_packet(self.p, problem["id"])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="compiler error", content="Check compiler paths", category="operation",
            applicability="Recorded compiler environment", limitations="Reported only", validation_level="reported",
            evidence_refs=[item["id"] for item in packet["events"]])
        published = self.r.publish(self.p, candidate["candidate_id"])
        item = self.r.read(self.p, published["knowledge_id"])
        item["match"] = {"auto_eligible": True, "matched_terms": ["compiler"]}
        self.r.config["injection_max_chars"] = 128
        self.fault(0, "compiler error")
        with patch.object(self.r, "search", return_value={"items": [item], "retrieval": {"retryable": False}}) as search:
            first = self.invocation(0, 1)
            self.assertNotIn(published["knowledge_id"], json.dumps(first))
            self.assertEqual(self.fault_states(), ["pending"])
            self.invocation(1, 1)
            self.assertEqual(search.call_count, 1)
            self.assertEqual(self.fault_states(), ["pending"])
            self.r.config["injection_max_chars"] = 2400
            delivered = self.invocation(2, 1)
        self.assertIn(published["knowledge_id"], delivered["injectSteps"][0]["ephemeralMessage"])
        self.assertEqual(self.fault_states(), ["queried"])
        self.assertIsNone(self.r.task(self.p, self.c))


if __name__ == "__main__":
    unittest.main()
