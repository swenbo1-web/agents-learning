"""Action-only followups keep a scoped business focus without generic recall."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning import candidate_references
from agent_learning.runtime import Runtime


class ContinuationFocusTests(unittest.TestCase):
    def setUp(self):
        area = Path(__file__).resolve().parents[1] / "build" / "local" / "fix1423" / "continuation"
        area.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="case-", dir=area)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("tmp", "home", "codex", "runtime-releases", "checkout"):
            (self.root / name).mkdir()
        config = self.root / "config.json"
        config.write_text("{}", encoding="utf-8")
        environment = {"PYTHONDONTWRITEBYTECODE": "1", "ALP_HOME": str(self.root / "home"),
            "ALP_CONFIG": str(config), "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"),
            "CODEX_HOME": str(self.root / "codex")}
        environment.update({name: str(self.root / "tmp") for name in ("TMP", "TEMP", "TMPDIR")})
        isolated = patch.dict(os.environ, environment)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.workspace = self.root / "checkout"
        self.r = Runtime(self.root / "home", config={
            "admin": {"enabled": False}, "scheduler": {"enabled": False},
            "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.goal = "定位 1.4.22 管理员 exit1"
        self.r.task_update(self.p, "reader", self.goal,
                           {"mode": "analysis", "knowledge_filters": {"environment": "offline"}})
        self.handle = self.r.register_context(self.p, "reader")["context_handle"]

    def context(self, query, *, conversation="reader", handle=None, **kwargs):
        return self.r._context(self.p, conversation, query=query,
            context_handle=handle or self.handle, workspace=self.workspace, **kwargs)

    def hook(self, prompt, turn):
        result = self.r.handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "reader",
            "cwd": str(self.workspace), "prompt": prompt, "turn_id": turn})
        return result.get("hookSpecificOutput", {}).get("additionalContext", "")

    def generic_history(self):
        problem = self.r.feedback(self.p, "past", "继续保留原始证据，验证旧来源规则", category="operation")
        packet = self.r.review_packet(self.p, problem["id"])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"],
            problem_revision=problem["revision"], title="旧版来源数量事实",
            content="旧版已取消64条来源阈值；继续保留原始证据，最终验证历史规则。",
            applicability="本地历史规则", limitations="仅公开陈述，未证明用户验收", category="operation",
            validation_level="reported", metadata={"environment": "offline"},
            evidence_refs=[event["id"] for event in packet["events"]])
        knowledge = self.r.publish(self.p, candidate["candidate_id"])["knowledge_id"]
        raw = {"last_assistant_message": "旧 Hook 审阅说明：继续保留审阅意见，最终验证旧安装。"}
        with self.r.transaction():
            event, _ = self.r._event(self.p, "past", "Stop", raw, outcome="reported")
            memory = self.r._work_capture_event(self.p, event["id"], self.workspace, raw=raw)
        return knowledge, memory["memory_id"]

    def test_delivered_followups_keep_focus_and_do_not_inject_generic_history(self):
        knowledge, memory = self.generic_history()
        focus = "定位 src/admin.py 的 E_QUEUE_EXIT1"
        self.hook(focus, "focus")
        self.r.task_update(self.p, "reader", self.goal, status="delivered")
        with patch.object(self.r, "search", wraps=self.r.search) as formal, \
                patch.object(self.r, "work_context_items", wraps=self.r.work_context_items) as work, \
                patch.object(candidate_references, "search", return_value={"items": []}) as candidate:
            for index, prompt in enumerate(("继续验证", "继续检查", "继续测试", "确认修复",
                    "continue testing", "please continue checking", "继续验证一下", "continue to verify",
                    "继续检查并验证", "继续", "确认。")):
                with self.subTest(prompt=prompt):
                    text = self.hook(prompt, f"followup-{index}")
                    self.assertNotIn(knowledge, text)
                    self.assertNotIn(memory, text)
                    saved = self.r.db.execute("SELECT last_query FROM contexts WHERE conversation='reader'").fetchone()[0]
                    self.assertEqual(saved, focus)
        self.assertTrue(formal.called)
        self.assertTrue(all(call.args[1] == focus for call in formal.call_args_list))
        self.assertTrue(all(call.kwargs["filters"] == {"environment": "offline"} for call in formal.call_args_list))
        self.assertEqual(len(work.call_args_list), 11)
        self.assertTrue(all(call.kwargs["query"] == focus for call in work.call_args_list))
        self.assertEqual(len(candidate.call_args_list), 11)
        self.assertTrue(all(call.args[2] == focus for call in candidate.call_args_list))
        self.assertEqual(self.r.task(self.p, "reader")["status"], "delivered")

    def test_old_action_only_focus_recovers_delivered_task_goal_for_all_channels(self):
        self.context("initial business focus")
        self.r.task_update(self.p, "reader", self.goal, status="delivered")
        for old in ("继续验证", "确认修复", "continue testing", "验证"):
            with self.subTest(old=old):
                self.r.db.execute("UPDATE contexts SET last_query=?,retrieval_signature='' WHERE conversation='reader'", (old,))
                with patch.object(self.r, "search", return_value={"items": []}) as formal, \
                        patch.object(self.r, "work_context_items", return_value=[]) as work, \
                        patch.object(candidate_references, "search", return_value={"items": []}) as candidate:
                    self.context("继续检查")
                self.assertEqual(formal.call_args.args[1], self.goal)
                self.assertEqual(work.call_args.kwargs["query"], self.goal)
                self.assertEqual(candidate.call_args.args[2], self.goal)
                saved = self.r.db.execute("SELECT last_query FROM contexts WHERE conversation='reader'").fetchone()[0]
                self.assertEqual(saved, self.goal)
        self.assertEqual(self.r.task(self.p, "reader")["status"], "delivered")

    def test_missing_business_focus_does_not_fall_back_to_unrelated_recent_history(self):
        knowledge, memory = self.generic_history()
        for conversation, goal in (("no-task", None), ("generic-task", "继续检查")):
            with self.subTest(conversation=conversation):
                if goal:
                    self.r.task_update(self.p, conversation, goal, {"mode": "analysis"})
                handle = self.r.register_context(self.p, conversation)["context_handle"]
                with patch.object(self.r, "search", side_effect=AssertionError("generic formal query")), \
                        patch.object(self.r, "work_context_items", side_effect=AssertionError("blank recent-history fallback")), \
                        patch.object(candidate_references, "search", side_effect=AssertionError("generic candidate query")), \
                        patch.object(self.r, "work_exposure_updates", wraps=self.r.work_exposure_updates) as notices:
                    text = self.context("继续验证", conversation=conversation, handle=handle)
                self.assertTrue(notices.called)
                self.assertNotIn(knowledge, text)
                self.assertNotIn(memory, text)
                saved = self.r.db.execute("SELECT last_query FROM contexts WHERE conversation=?", (conversation,)).fetchone()[0]
                self.assertEqual(saved, "")

    def test_subject_bearing_prompts_remain_new_business_queries(self):
        self.context("old business focus")
        for query in ("继续验证数据库锁", "验证证书", "确认修复数据库锁", "continue testing database locks",
                      "verify certificate", "continue checking src/admin.py E_QUEUE_EXIT1", "确认修复 signingcompat 签名"):
            with self.subTest(query=query):
                with patch.object(self.r, "search", return_value={"items": []}) as formal, \
                        patch.object(self.r, "work_context_items", return_value=[]) as work, \
                        patch.object(candidate_references, "search", return_value={"items": []}) as candidate:
                    self.context(query)
                self.assertEqual(formal.call_args.args[1], query)
                self.assertEqual(work.call_args.kwargs["query"], query)
                self.assertEqual(candidate.call_args.args[2], query)

    def test_empty_startup_keeps_existing_work_recovery_path(self):
        conversation = "new-session"
        handle = self.r.register_context(self.p, conversation)["context_handle"]
        with patch.object(self.r, "work_context_items", return_value=[]) as work:
            self.context("", conversation=conversation, handle=handle)
        self.assertEqual(work.call_args.kwargs["query"], "")


if __name__ == "__main__":
    unittest.main()
