"""AG 可选公开消息到共享工作记录的真实 SQLite 回归；不读取会话或调用模型。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.antigravity import AntigravityAdapter, PUBLIC_TEXT_MAX_CHARS
from agent_learning.runtime import Runtime


class AntigravityCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-ag-public-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.r = Runtime(root / "home", config={}, host="antigravity")
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.c = self.r.host_conversation("chat")
        self.adapter = AntigravityAdapter(self.r, self.workspace)

    def raw(self, **fields):
        return {"conversationId": "chat", "workspacePaths": [str(self.workspace)], **fields}

    def invocation(self, number=0, steps=0, **fields):
        return self.adapter.handle("PreInvocation", self.raw(invocationNum=number, initialNumSteps=steps, **fields))

    def stop(self, number=0, idle=True, reason="model_stop", **fields):
        return self.adapter.handle("Stop", self.raw(executionNum=number, fullyIdle=idle,
                                                   terminationReason=reason, **fields))

    def events(self, kind):
        return self.r.db.execute("SELECT * FROM events WHERE type=? ORDER BY created,id", (kind,)).fetchall()

    def test_optional_prompt_records_user_event_and_queries_public_input_without_task(self):
        self.invocation(lastUserInput="项目选择postgres方案，请继续检查索引")
        event, = self.events("UserPromptSubmit")
        payload = json.loads(event["payload"])
        self.assertIn("postgres", payload["prompt"])
        self.assertEqual(payload["source"], "antigravity_public_hook")
        self.assertIsNone(event["task_id"])
        self.assertIsNone(self.r.task(self.p, self.c))
        self.assertIn("postgres", self.r.db.execute("SELECT last_query FROM contexts").fetchone()[0])

    def test_repeated_prompt_in_same_execution_is_not_another_user_message(self):
        self.invocation(lastUserInput="不对，我们选择postgres")
        self.invocation(1, 1, lastUserInput="不对，我们选择postgres")
        self.assertEqual(len(self.events("UserPromptSubmit")), 1)
        self.assertEqual(len(self.r.work_search(self.p, "postgres")["items"]), 1)

    def test_same_prompt_after_idle_boundary_is_preserved_as_new_input(self):
        self.invocation(lastUserInput="继续")
        self.stop()
        self.invocation(1, 1, lastUserInput="继续")
        self.assertEqual(len(self.events("UserPromptSubmit")), 2)

    def test_missing_or_non_string_fields_do_not_read_transcript_or_thoughts(self):
        for final in (None, {"text": "secret object"}, ["secret array"]):
            with patch.object(Path, "read_text", side_effect=AssertionError("must not read transcript")):
                self.invocation(0, 0, lastUserInput=final, prompt="untrusted prompt",
                                transcriptPath="never-read.jsonl", modelThinking="private")
                self.stop(finalModelOutput=final, modelThinking="private", message="untrusted final")
        self.assertEqual(self.events("UserPromptSubmit"), [])
        self.assertEqual(self.events("Stop"), [])
        native, = self.events("AntigravityStop")
        coverage = json.loads(native["payload"])["capture_coverage"]["finalModelOutput"]
        self.assertFalse(coverage["available"])
        self.assertNotIn("secret object", native["payload"])
        self.assertNotIn("private", native["payload"])

    def test_public_summary_searchable_without_task_or_delivery(self):
        self.stop(finalModelOutput="设计选择postgres事务 outbox，后续实现发送器。")
        rows = self.r.work_search(self.p, "outbox")["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["validation_level"], "reported")
        self.assertEqual(rows[0]["state"], "reported")
        event, = self.events("Stop")
        self.assertIsNone(event["task_id"])
        self.assertEqual(json.loads(event["payload"])["completion"], "complete")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_complete_output_can_reach_existing_delivery_capture(self):
        self.r.task_update(self.p, self.c, "选择存储方案")
        self.r.task_update(self.p, self.c, "选择存储方案", status="delivered")
        self.stop(finalModelOutput="已选择postgres；这是分析结论，仍需项目验证。")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 1)
        self.assertEqual(self.r.task(self.p, self.c)["status"], "delivered")

    def test_aborted_error_max_steps_or_non_idle_output_is_not_completed(self):
        for index, reason in enumerate(("aborted", "error", "max_steps", "model_stop")):
            with self.subTest(reason=reason):
                with patch.object(self.r, "_capture_delivery") as delivery, patch.object(self.r, "_work_queue_summary") as queue:
                    self.stop(index, idle=reason != "model_stop", reason=reason,
                              finalModelOutput="部分实现仍待完成 outbox")
                delivery.assert_not_called()
                queue.assert_not_called()
        self.assertTrue(all(json.loads(row["payload"])["completion"] == "partial" for row in self.events("Stop")))
        self.assertTrue(self.r.work_search(self.p, "outbox")["items"])

    def test_error_text_blocks_delivery_even_when_reason_says_model_stop(self):
        with patch.object(self.r, "_capture_delivery") as delivery, patch.object(self.r, "_work_queue_summary") as queue:
            self.stop(error="network failure", finalModelOutput="缓存中已有部分分析")
        delivery.assert_not_called()
        queue.assert_not_called()
        self.assertEqual(json.loads(self.events("Stop")[0]["payload"])["completion"], "partial")

    def test_public_fields_redacted_then_bounded_with_explicit_coverage(self):
        text = "password=fixture-private " + "设计说明" * PUBLIC_TEXT_MAX_CHARS + " 最终尾部"
        self.invocation(lastUserInput=text)
        self.stop(finalModelOutput=text)
        for kind, field, native in (("UserPromptSubmit", "prompt", "lastUserInput"),
                                    ("Stop", "last_assistant_message", "finalModelOutput")):
            payload = json.loads(self.events(kind)[0]["payload"])
            self.assertNotIn("fixture-private", json.dumps(payload))
            self.assertTrue(payload["truncated"])
            self.assertTrue(payload["capture_coverage"][native]["truncated"])
            self.assertFalse(payload["capture_coverage"][native]["full_original_available"])
            self.assertLessEqual(len(payload[field]), PUBLIC_TEXT_MAX_CHARS)

    def test_public_messages_not_guessed_across_multiple_mounted_workspaces(self):
        other = self.workspace.parent / "other"
        other.mkdir()
        mounts = [str(self.workspace), str(other)]
        self.invocation(lastUserInput="不对，这是另一个项目的决定", workspacePaths=mounts)
        self.stop(finalModelOutput="另一个项目已选Redis", workspacePaths=mounts)
        self.assertEqual(self.events("UserPromptSubmit"), [])
        self.assertEqual(self.events("Stop"), [])
        native, = self.events("AntigravityStop")
        self.assertEqual(json.loads(native["payload"])["capture_coverage"]["workspace_association"], "unknown_multi_workspace")

    def test_optional_capture_does_not_change_native_identity_or_repeat_fence(self):
        self.r.task_update(self.p, self.c, "current task")
        self.stop(finalModelOutput="原始公开报告")
        epoch = self.r.task(self.p, self.c)["execution_epoch"]
        self.stop(finalModelOutput="随后变化不能伪造另一条Stop", modelName="another-model")
        self.assertEqual(self.r.task(self.p, self.c)["execution_epoch"], epoch)
        self.assertEqual(len(self.events("AntigravityStop")), 1)
        self.assertEqual(len(self.events("Stop")), 1)
        self.assertEqual(json.loads(self.events("Stop")[0]["payload"])["last_assistant_message"], "原始公开报告")

    def test_complete_stop_replay_retries_dispatch_without_repeating_capture(self):
        with patch.object(self.adapter, "_schedule") as schedule:
            self.stop(finalModelOutput="阶段公开结论")
            self.stop(finalModelOutput="阶段公开结论")
        self.assertEqual(schedule.call_count, 2)
        self.assertEqual(len(self.events("Stop")), 1)
        self.assertEqual(len(self.events("AntigravityStop")), 1)

    def test_stop_replay_with_new_task_or_account_cannot_dispatch(self):
        self.r.task_update(self.p, self.c, "original task")
        self.stop(finalModelOutput="旧阶段")
        self.r.task_update(self.p, self.c, "new task", new_task=True)
        with patch.object(self.adapter, "_schedule") as schedule:
            self.stop(finalModelOutput="旧阶段")
        schedule.assert_not_called()
        self.r.account_changed("different-account")
        with patch.object(self.adapter, "_schedule") as schedule:
            self.stop(finalModelOutput="旧阶段")
        schedule.assert_not_called()

    def test_replayed_stop_can_retry_failed_background_launch_only_once_after_claim(self):
        self.r.config["admin"].update(enabled=True, antigravity_command=[sys.executable])
        self.r.feedback(self.p, self.c, "pending evidence")
        with patch.object(self.r, "_launch_admin", side_effect=OSError("fixture private path")) as first:
            self.stop()
        self.assertEqual(first.call_count, 1)
        with patch.object(self.r, "_launch_admin") as second:
            self.stop()
            self.stop()
        self.assertEqual(second.call_count, 1)
        self.assertEqual(len(self.events("AntigravityStop")), 1)

    def test_replayed_stop_does_not_restart_unknown_model_call(self):
        from agent_learning.admin_lifecycle import claim_call
        self.r.config["admin"].update(enabled=True, antigravity_command=[sys.executable])
        problem = self.r.feedback(self.p, self.c, "pending evidence")
        with patch.object(self.adapter, "_schedule"):
            self.stop()
        with self.r.transaction():
            call = claim_call(self.r, self.p, problem["id"], problem["revision"], "fixture",
                              self.r.host_epoch(), 120)
            self.r.db.execute("UPDATE admin_calls SET state='timeout_unknown' WHERE id=?", (call["call_id"],))
        with patch.object(self.r, "_launch_admin") as launch:
            self.stop()
        launch.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
