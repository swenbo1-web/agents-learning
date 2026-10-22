"""跨聊天原生身份和旧回执兼容；仅使用隔离 SQLite，不运行宿主模型。"""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.antigravity import AntigravityAdapter, reset_session
from agent_learning.runtime import ContractError, Runtime, digest


class AntigravityEventIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-ag-event-identity-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.r = Runtime(root / "home", config={}, host="antigravity")
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.adapter = AntigravityAdapter(self.r, self.workspace)

    def conversation(self, native):
        return self.r.host_conversation(native)

    def raw(self, native="one", **fields):
        return {"conversationId": native, "workspacePaths": [str(self.workspace)], **fields}

    def stop(self, native="one", text="阶段公开结论", **fields):
        return self.adapter.handle("Stop", self.raw(native, executionNum=0, fullyIdle=True,
            terminationReason="model_stop", finalModelOutput=text, **fields))

    def invocation(self, native="one", number=0, text="相同公开用户输入"):
        return self.adapter.handle("PreInvocation", self.raw(native, invocationNum=number,
                                                           initialNumSteps=number, lastUserInput=text))

    def counts(self):
        return {table: self.r.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in ("events", "deliveries", "delivery_rounds", "work_memories", "review_phases")}

    def events(self, kind, native=None):
        if native is None:
            return self.r.db.execute("SELECT * FROM events WHERE type=? ORDER BY created,id", (kind,)).fetchall()
        return self.r.db.execute("SELECT * FROM events WHERE type=? AND conversation=? ORDER BY created,id",
                                (kind, self.conversation(native))).fetchall()

    @contextmanager
    def legacy_event_ids(self):
        """复现1.4.8写入的真实事件和receipt形状，保留其他事务逻辑。"""
        original = self.r._event
        stop_signatures = {}
        def legacy(project, conversation, kind, payload, event_id=None, **kwargs):
            if (isinstance(event_id, list) and len(event_id) >= 3
                    and event_id[0] in {"antigravity", "antigravity-public-stop", "antigravity-public-prompt"}
                    and event_id[1] == conversation):
                event_id = [event_id[0], *event_id[2:]]
            if kind == "AntigravityStop" and isinstance(event_id, list):
                stop_signatures[(project, conversation, event_id[-1])] = self.adapter._task_signature(project, conversation)
            return original(project, conversation, kind, payload, event_id=event_id, **kwargs)
        try:
            with patch.object(self.r, "_event", side_effect=legacy):
                yield
        finally:
            # 1.4.8 的完成回执保存 fence 前签名；新版不得猜测回填它。
            for (project, conversation, key), signature in stop_signatures.items():
                self.r.db.execute("UPDATE antigravity_receipts SET task_signature=? WHERE project=? "
                    "AND conversation=? AND event_key=? AND state='complete'",
                    (signature, project, conversation, key))

    def old_collision(self, *, task=False, legacy_text="阶段公开结论"):
        if task:
            for native in ("one", "two"):
                self.r.task_update(self.p, self.conversation(native), "明确完成项目阶段")
                self.r.task_update(self.p, self.conversation(native), "明确完成项目阶段", status="delivered")
        with self.legacy_event_ids():
            self.stop("one", text=legacy_text)
            with self.assertRaisesRegex(ContractError, "event id reused"):
                self.stop("two", text="第二个聊天的阶段交付")
        return self.receipt("two")

    def receipt(self, native):
        return self.r.db.execute("SELECT * FROM antigravity_receipts WHERE conversation=? ORDER BY created DESC LIMIT 1",
                                 (self.conversation(native),)).fetchone()

    def recovery_reason(self, native="two"):
        old = self.receipt(native)
        raw = self.raw(native, executionNum=0, fullyIdle=True, terminationReason="model_stop")
        return self.adapter._pending_stop_recovery_reason(self.p, self.conversation(native),
            self.adapter._observable("Stop", raw), old["event_key"], old,
            self.r.session_epoch(), old["generation"])

    def test_same_stop_counter_in_two_chats_with_different_public_text_is_independent(self):
        for native, text in (("one", "第一聊天交付"), ("two", "第二聊天交付")):
            self.r.task_update(self.p, self.conversation(native), "阶段工作")
            self.r.task_update(self.p, self.conversation(native), "阶段工作", status="delivered")
            self.assertEqual(self.stop(native, text), {"decision": "stop"})
        self.assertEqual(len(self.events("AntigravityStop")), 2)
        self.assertEqual(len(self.events("Stop")), 2)
        self.assertEqual(self.counts()["deliveries"], 2)
        self.assertEqual({row["conversation"] for row in self.events("Stop")},
                         {self.conversation("one"), self.conversation("two")})
        before = self.counts()
        self.stop("one", "第一聊天交付")
        self.stop("two", "第二聊天交付")
        self.assertEqual(self.counts(), before)

    def test_same_stop_counter_and_same_public_text_are_still_two_sources(self):
        self.stop("one")
        self.stop("two")
        self.assertEqual(len(self.events("AntigravityStop")), 2)
        self.assertEqual(len(self.events("Stop")), 2)
        self.assertEqual(self.counts()["work_memories"], 2)

    def test_same_first_prompt_in_two_chats_is_independent(self):
        self.invocation("one")
        self.invocation("two")
        self.assertEqual(len(self.events("UserPromptSubmit")), 2)
        self.invocation("one", 1)
        self.invocation("two", 1)
        self.assertEqual(len(self.events("UserPromptSubmit")), 2)

    def test_legacy_prompt_reuses_same_conversation_event_in_new_invocation(self):
        with self.legacy_event_ids():
            self.invocation()
        before = self.counts()
        self.invocation(number=1)
        self.assertEqual(self.counts(), {**before, "events": before["events"] + 1})
        self.assertEqual(len(self.events("AntigravityPublicInput", "one")), 2)
        self.assertEqual(len(self.events("UserPromptSubmit", "one")), 1)
        self.invocation("two")
        self.assertEqual(len(self.events("UserPromptSubmit")), 2)

    def test_legacy_complete_stop_does_not_replay_fence_delivery_or_new_public_text(self):
        self.r.task_update(self.p, self.conversation("one"), "明确交付")
        self.r.task_update(self.p, self.conversation("one"), "明确交付", status="delivered")
        with self.legacy_event_ids():
            self.stop()
        before, task = self.counts(), self.r.task(self.p, self.conversation("one"))
        signature = self.receipt("one")["task_signature"]
        self.assertNotEqual(signature, self.adapter._task_signature(self.p, self.conversation("one")))
        with patch.object(self.adapter, "_schedule") as schedule:
            self.stop(text="不能变成新交付", modelName="different-model")
        schedule.assert_not_called()
        self.assertEqual(self.receipt("one")["task_signature"], signature)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.r.task(self.p, self.conversation("one")), task)
        self.assertNotIn("不能变成新交付", str([row["payload"] for row in self.events("Stop")]))

    def delivered_task(self):
        self.r.task_update(self.p, self.conversation("one"), "明确交付项目规范")
        return self.r.task_update(self.p, self.conversation("one"), "明确交付项目规范", status="delivered")

    def test_completed_task_stop_stores_post_fence_signature_and_retries_missing_dispatch(self):
        self.delivered_task()
        with patch.object(self.adapter, "_schedule") as schedule:
            self.stop()
            before = self.counts()
            task = self.r.task(self.p, self.conversation("one"))
            self.assertEqual(self.receipt("one")["task_signature"],
                             self.adapter._task_signature(self.p, self.conversation("one")))
            self.stop()
        self.assertEqual(schedule.call_count, 2)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.r.task(self.p, self.conversation("one")), task)

    def test_completed_task_stop_retries_launch_failure_without_repeating_delivery(self):
        self.delivered_task()
        self.r.config["admin"].update(enabled=True, antigravity_command=[sys.executable])
        with patch.object(self.r, "_launch_admin", side_effect=OSError("fixture launch failed")) as first:
            self.stop()
        self.assertEqual(first.call_count, 1)
        before, task = self.counts(), self.r.task(self.p, self.conversation("one"))
        with patch.object(self.r, "_launch_admin") as launch:
            self.stop()
            self.stop()
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.r.task(self.p, self.conversation("one")), task)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_recovered_completed_task_stop_retries_dispatch_without_repeating_effects(self):
        self.old_collision(task=True)
        with patch.object(self.adapter, "_schedule") as schedule:
            self.stop("two", text="第二个聊天的阶段交付")
            before = self.counts()
            self.assertEqual(self.receipt("two")["task_signature"],
                             self.adapter._task_signature(self.p, self.conversation("two")))
            self.stop("two", text="第二个聊天的阶段交付")
        self.assertEqual(schedule.call_count, 2)
        self.assertEqual(self.counts(), before)

    def test_completed_task_stop_cannot_dispatch_for_replaced_task(self):
        self.delivered_task()
        with patch.object(self.adapter, "_schedule"):
            self.stop()
        self.r.task_update(self.p, self.conversation("one"), "不同的新任务", new_task=True)
        before = self.counts()
        with patch.object(self.adapter, "_schedule") as schedule:
            self.stop()
        schedule.assert_not_called()
        self.assertEqual(self.counts(), before)

    def test_completed_task_stop_cannot_dispatch_after_account_change(self):
        self.delivered_task()
        with patch.object(self.adapter, "_schedule"):
            self.stop()
        self.r.account_changed("fixture-new-account")
        before = self.counts()
        with patch.object(self.adapter, "_schedule") as schedule:
            self.stop()
        schedule.assert_not_called()
        self.assertEqual(self.counts(), before)

    def test_reset_between_completed_replay_and_dispatch_blocks_old_generation(self):
        self.delivered_task()
        with patch.object(self.adapter, "_schedule"):
            self.stop()
        self.r.config["admin"].update(enabled=True, antigravity_command=[sys.executable])
        original = self.adapter._schedule
        def reset_before_schedule(*args, **kwargs):
            reset_session(self.r, self.workspace, "one")
            return original(*args, **kwargs)
        with patch.object(self.adapter, "_schedule", side_effect=reset_before_schedule), \
                patch.object(self.r, "_launch_admin") as launch:
            self.stop()
        launch.assert_not_called()
        self.assertEqual(len(self.events("AntigravityStop", "one")), 1)
        self.assertEqual(len(self.events("Stop", "one")), 1)

    def test_completed_task_replay_does_not_restart_unknown_paid_call(self):
        from agent_learning.admin_lifecycle import claim_call
        self.delivered_task()
        with patch.object(self.adapter, "_schedule"):
            self.stop()
        problem = self.r.db.execute("SELECT * FROM problems WHERE project=?", (self.p,)).fetchone()
        self.r.config["admin"].update(enabled=True, antigravity_command=[sys.executable])
        with self.r.transaction():
            call = claim_call(self.r, self.p, problem["id"], problem["revision"], "fixture",
                              self.r.host_epoch(), 120)
            self.r.db.execute("UPDATE admin_calls SET state='timeout_unknown' WHERE id=?", (call["call_id"],))
        with patch.object(self.r, "_launch_admin") as launch:
            self.stop()
        launch.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_collision_pending_stop_recovers_once_without_model(self):
        old = self.old_collision(task=True)
        self.assertEqual(old["state"], "pending")
        self.assertIsNone(self.recovery_reason())
        before = self.r.task(self.p, self.conversation("two"))["execution_epoch"]
        with patch.object(self.r, "_launch_admin", side_effect=AssertionError("no real model")):
            self.stop("two", text="第二个聊天的阶段交付")
            after = self.counts()
            self.stop("two", text="不能再次写入")
        self.assertEqual(self.receipt("two")["state"], "complete")
        self.assertEqual(self.counts(), after)
        self.assertEqual(self.r.task(self.p, self.conversation("two"))["execution_epoch"], before + 1)
        self.assertEqual(len(self.events("Stop", "two")), 1)
        self.assertEqual(self.counts()["deliveries"], 2)
        self.assertEqual(self.r.db.execute("SELECT value FROM metrics WHERE project=? AND name=?",
            (self.p, "antigravity_stop_collision_recovered")).fetchone()[0], 1)

    def test_recovery_failure_rolls_back_stop_fence_and_receipt_then_can_retry(self):
        self.old_collision(task=True)
        before, task = self.counts(), self.r.task(self.p, self.conversation("two"))
        with patch.object(self.r, "_capture_delivery", side_effect=ContractError("fixture failure after native event")):
            with self.assertRaisesRegex(ContractError, "fixture failure"):
                self.stop("two", text="第二个聊天的阶段交付")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.r.task(self.p, self.conversation("two")), task)
        self.assertEqual(self.receipt("two")["state"], "pending")
        self.stop("two", text="第二个聊天的阶段交付")
        self.assertEqual(self.receipt("two")["state"], "complete")

    def test_interleaved_pending_replays_commit_effects_only_once(self):
        self.old_collision(task=True)
        original_transaction = self.r.transaction
        original_reason = self.adapter._pending_stop_recovery_reason
        state = {"ready": False, "interleaved": False}
        def reason(*args, **kwargs):
            result = original_reason(*args, **kwargs)
            if result is None:
                state["ready"] = True
            return result
        @contextmanager
        def transaction():
            with original_transaction():
                yield
            if state["ready"] and not state["interleaved"]:
                state["interleaved"] = True
                self.stop("two", text="第二个聊天的阶段交付")
        before = self.r.task(self.p, self.conversation("two"))["execution_epoch"]
        with patch.object(self.adapter, "_pending_stop_recovery_reason", side_effect=reason), \
                patch.object(self.r, "transaction", side_effect=transaction):
            self.stop("two", text="第二个聊天的阶段交付")
        self.assertTrue(state["interleaved"])
        self.assertEqual(self.r.task(self.p, self.conversation("two"))["execution_epoch"], before + 1)
        self.assertEqual(len(self.events("AntigravityStop", "two")), 1)
        self.assertEqual(len(self.events("Stop", "two")), 1)
        self.assertEqual(self.receipt("two")["state"], "complete")

    def test_pending_stop_without_proven_collision_stays_pending(self):
        with patch.object(self.adapter, "_stop", side_effect=ContractError("before stop")):
            with self.assertRaisesRegex(ContractError, "before stop"):
                self.stop()
        self.assertEqual(self.recovery_reason("one"), "legacy_cross_conversation_collision_unproven")
        before = self.counts()
        self.stop()
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.receipt("one")["state"], "pending")

    def test_pending_stop_with_committed_legacy_effects_stays_pending(self):
        with self.legacy_event_ids():
            self.stop()
        self.r.db.execute("UPDATE antigravity_receipts SET state='pending' WHERE conversation=?", (self.conversation("one"),))
        before = self.counts()
        self.stop()
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.receipt("one")["state"], "pending")

    def test_pending_collision_rejects_new_native_or_old_and_new_public_stop(self):
        self.old_collision(legacy_text="")
        for prefix, scoped, kind in (("antigravity", True, "AntigravityStop"),
                                     ("antigravity-public-stop", True, "Stop"),
                                     ("antigravity-public-stop", False, "Stop")):
            with self.subTest(prefix=prefix, scoped=scoped):
                old = self.receipt("two")
                identity = [prefix, self.conversation("two"), old["event_key"]] if scoped else [prefix, old["event_key"]]
                inserted, _ = self.r._event(self.p, self.conversation("two"), kind, {"uncertain": True}, event_id=identity)
                self.assertEqual(self.recovery_reason(), "stop_effect_already_recorded")
                before = self.counts()
                self.stop("two")
                self.assertEqual(self.counts(), before)
                self.assertEqual(self.receipt("two")["state"], "pending")
                self.r.db.execute("DELETE FROM events WHERE id=?", (inserted["id"],))

    def test_pending_collision_with_later_event_is_not_blindly_retried(self):
        self.old_collision()
        self.r._event(self.p, self.conversation("two"), "ExecutionFence", {"uncertain": True})
        self.assertEqual(self.recovery_reason(), "later_conversation_effects_present")
        before = self.counts()
        self.stop("two")
        self.assertEqual(self.counts(), before)

    def test_pending_collision_with_orphan_work_projection_is_not_retried(self):
        old = self.old_collision()
        self.r.db.execute("UPDATE work_memories SET conversation=?,created=? WHERE project=?",
                          (self.conversation("two"), old["created"] + 1, self.p))
        self.assertEqual(self.recovery_reason(), "later_work_projection_present")
        before = self.counts()
        self.stop("two")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.receipt("two")["state"], "pending")

    def test_pending_collision_with_orphan_delivery_is_not_retried(self):
        old = self.old_collision(task=True)
        task = self.r.task(self.p, self.conversation("two"))
        self.r.db.execute("UPDATE delivery_rounds SET task_id=?,revision=?,write_version=?,created=? WHERE project=?",
                          (task["id"], task["revision"], task["write_version"], old["created"] + 1, self.p))
        self.assertEqual(self.recovery_reason(), "later_delivery_present")
        before = self.counts()
        self.stop("two")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.receipt("two")["state"], "pending")

    def test_pending_collision_with_mismatched_legacy_native_identity_is_not_retried(self):
        old = self.old_collision()
        identity = digest([self.p, ["antigravity", old["event_key"]]])
        self.r.db.execute("UPDATE events SET payload=? WHERE id=?", ('{"kind":"different event"}', identity))
        self.assertEqual(self.recovery_reason(), "legacy_native_identity_mismatch")
        before = self.counts()
        self.stop("two")
        self.assertEqual(self.counts(), before)

    def test_pending_collision_cannot_borrow_new_task(self):
        self.old_collision(task=True)
        self.r.task_update(self.p, self.conversation("two"), "完全不同的新任务", new_task=True)
        task, before = self.r.task(self.p, self.conversation("two")), self.counts()
        self.assertEqual(self.recovery_reason(), "receipt_task_changed")
        self.stop("two")
        self.assertEqual(self.r.task(self.p, self.conversation("two")), task)
        self.assertEqual(self.counts(), before)

    def test_pending_collision_cannot_cross_account_and_reset_is_explicit(self):
        self.old_collision(task=True)
        self.r.account_changed("fixture-account")
        before = self.counts()
        self.stop("two")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.receipt("two")["state"], "pending")
        reset_session(self.r, self.workspace, "two")
        self.stop("two")
        self.assertEqual(len(self.events("AntigravityStop", "two")), 1)
        self.assertEqual(self.receipt("two")["generation"], 2)

    def test_old_014_receipt_still_requires_trusted_reset(self):
        with self.legacy_event_ids():
            self.invocation()
        self.r.db.execute("UPDATE antigravity_receipts SET raw_digest=substr(raw_digest,4)")
        before = self.counts()
        self.stop()
        self.assertEqual(self.counts(), before)
        self.assertIn("trusted antigravity-session-reset", json.dumps(self.invocation(number=1)))

    def test_unassociated_tool_events_do_not_collide_across_chats(self):
        for native in ("one", "two"):
            raw = self.raw(native, stepIdx=0, toolCall={"name": "view_file", "args": {"AbsolutePath": str(self.workspace.parent / "elsewhere")}}, error="")
            self.adapter.handle("PostToolUse", raw)
            self.adapter.handle("PostToolUse", raw)
        self.assertEqual(len(self.events("AntigravityPostToolUse")), 2)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)

    def test_legacy_pending_unassociated_event_reuses_source_without_duplicate(self):
        raw = self.raw(stepIdx=0, toolCall={"name": "view_file", "args": {"AbsolutePath": str(self.workspace.parent / "elsewhere")}}, error="")
        with self.legacy_event_ids():
            self.adapter.handle("PostToolUse", raw)
        self.r.db.execute("UPDATE antigravity_receipts SET state='pending'")
        before = self.counts()
        self.adapter.handle("PostToolUse", raw)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.receipt("one")["state"], "complete")


if __name__ == "__main__":
    unittest.main()
