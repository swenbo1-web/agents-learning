"""Acknowledged/resumed follow-ups preserve retrieval focus, never authority."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from agent_learning import candidate_references
from agent_learning.knowledge_context import _continuation_query
from tests import test_context_action_continuation as fixtures


class ContinuationGrammar1444Tests(unittest.TestCase):
    def test_whole_acknowledged_progress_and_resumed_actions(self):
        for query in ("是的继续推进吧", "是的，继续推进吧。", "继续推进", "请继续进行一下",
                      "可以继续执行", "好的，接着完成吧", "没问题，按你说的继续优化",
                      "确认，继续检查", "同意，继续推进下去", "按原计划继续执行",
                      "已经重启，继续推进吧", "已重启继续", "重载完成，接着检查",
                      "已重新连接，继续验证", "好的，已经重启了，继续推进",
                      "是的，重载好了，继续优化", "yes, continue", "okay, continue testing",
                      "confirmed, reloaded, continue checking", "restarted, continue testing",
                      "reload completed continue checking", "是的", "没问题"):
            with self.subTest(query=query):
                self.assertTrue(_continuation_query(query))

    def test_subjects_versions_negation_and_standalone_reload_are_new_input(self):
        for query in ("是的，继续推进数据库迁移", "继续推进 v2.1", "可以继续执行新任务",
                      "是的继续推进 src/cache.py", "继续推进 E_LOCK_TIMEOUT",
                      "好的，继续分析 SQLite", "按原计划继续执行回滚",
                      "已重启 Redis，继续推进", "已重启，继续处理证书",
                      "没有重启，继续推进", "已重启，不要继续", "先不要继续推进",
                      "是的，停止继续推进", "是的，按你说的做好验收标准，继续推进",
                      "是的，开始新的任务", "好的，先修复其他问题", "已经重启", "重载完成",
                      "yes, continue testing certificate", "confirmed, deploy v2.1",
                      "restarted Redis, continue testing", "not restarted, continue checking",
                      "reloaded, do not continue", "restart completed", "安装", "部署",
                      "是的" * 2000 + "继续推进"):
            with self.subTest(query=query[:100]):
                self.assertFalse(_continuation_query(query))


class ContinuationFocus1444Tests(unittest.TestCase):
    def setUp(self):
        # Reuse the isolated Runtime fixture without importing a discoverable
        # TestCase class into this module or inheriting its unrelated tests.
        self.f = fixtures.ContextActionContinuationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.r, self.p = self.f.r, self.f.p
        self.goal = "ledgerfence rollback snapshot"

    def publish(self):
        problem = self.r.feedback(self.p, "evidence", self.goal, category="operation")
        packet = self.r.review_packet(self.p, problem["id"])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"],
            problem_revision=problem["revision"], title=self.goal,
            content="ledgerfence rollback snapshot: retain the verified rollback boundary.",
            applicability="ledgerfence rollback snapshot recovery", limitations="Reported reference only.",
            category="operation", validation_level="reported",
            evidence_refs=[event["id"] for event in packet["events"]])
        return self.r.publish(self.p, candidate["candidate_id"])["knowledge_id"]

    def generic_history(self):
        with self.r.transaction():
            raw = {"prompt": "是的继续推进吧"}
            event, _ = self.r._event(self.p, "past", "UserPromptSubmit", raw, outcome="reported")
            captured = self.r._work_capture_event(self.p, event["id"], self.f.workspace, raw=raw)
        return captured["memory_id"]

    def test_actual_runtime_recovers_task_knowledge_instead_of_old_acknowledgement(self):
        self.f.task(self.goal)
        knowledge = self.publish()
        old_acknowledgement = self.generic_history()
        with patch.object(self.r, "search", wraps=self.r.search) as formal:
            text = self.f.context("是的继续推进吧")
        self.assertEqual(formal.call_args.args[1], self.goal)
        self.assertEqual(self.f.saved_query(), self.goal)
        self.assertIn(knowledge, text)
        self.assertNotIn(old_acknowledgement, text)

    def test_polluted_action_focus_recovers_task_goal_for_every_retrieval_lane(self):
        self.f.task(self.goal)
        for old in ("是的继续推进吧", "已重启继续", "好的，继续执行"):
            with self.subTest(old=old):
                with self.r.transaction():
                    self.r.db.execute("UPDATE contexts SET last_query=?,retrieval_signature='' "
                        "WHERE project=? AND conversation=?", (old, self.p, self.f.conversation))
                with patch.object(self.r, "search", return_value={"items": []}) as formal, \
                        patch.object(self.r, "work_context_items", return_value=[]) as work, \
                        patch.object(candidate_references, "search", return_value={"items": []}) as candidate:
                    self.f.context("已经重启，继续推进吧")
                self.assertEqual(formal.call_args.args[1], self.goal)
                self.assertEqual(work.call_args.kwargs["query"], self.goal)
                self.assertEqual(candidate.call_args.args[2], self.goal)
                self.assertEqual(self.f.saved_query(), self.goal)

    def test_meaningful_saved_focus_is_retained_without_requery_or_reinjection(self):
        self.f.task("another explicit goal")
        knowledge = self.publish()
        self.assertIn(knowledge, self.f.context(self.goal))
        with patch.object(self.r, "search", side_effect=AssertionError("unchanged continuation queried")):
            for prompt in ("是的继续推进吧", "好的，已经重启了，继续检查", "yes, continue testing"):
                with self.subTest(prompt=prompt):
                    self.assertNotIn(knowledge, self.f.context(prompt))
                    self.assertEqual(self.f.saved_query(), self.goal)

    def test_compaction_retrieves_the_existing_business_focus(self):
        self.f.task(self.goal)
        knowledge = self.publish()
        self.f.context(self.goal)
        with patch.object(self.r, "search", wraps=self.r.search) as formal:
            text = self.f.context("已重启，继续推进吧", compact=True)
        self.assertEqual(formal.call_args.args[1], self.goal)
        self.assertIn(knowledge, text)

    def test_absent_focus_never_queries_recent_acknowledgements(self):
        old_acknowledgement = self.generic_history()
        with patch.object(self.r, "search", side_effect=AssertionError("unfocused formal query")), \
                patch.object(self.r, "work_context_items", side_effect=AssertionError("unfocused work query")), \
                patch.object(candidate_references, "search", side_effect=AssertionError("unfocused candidate query")), \
                patch.object(self.r, "work_exposure_updates", wraps=self.r.work_exposure_updates) as notices:
            text = self.f.context("是的继续推进吧")
        self.assertTrue(notices.called)
        self.assertEqual(self.f.saved_query(), "")
        self.assertNotIn(old_acknowledgement, text)

    def test_generic_task_goal_is_not_promoted_to_business_focus(self):
        self.f.task("是的继续推进吧")
        with patch.object(self.r, "search", side_effect=AssertionError("generic task searched")), \
                patch.object(self.r, "work_context_items", side_effect=AssertionError("generic task history")), \
                patch.object(candidate_references, "search", side_effect=AssertionError("generic task candidate")):
            self.f.context("已重启继续")
        self.assertEqual(self.f.saved_query(), "")

    def test_new_subject_replaces_existing_focus_in_every_lane(self):
        self.f.task(self.goal)
        query = "是的，继续推进 certificate v2.1"
        with patch.object(self.r, "search", return_value={"items": []}) as formal, \
                patch.object(self.r, "work_context_items", return_value=[]) as work, \
                patch.object(candidate_references, "search", return_value={"items": []}) as candidate:
            self.f.context(query)
        self.assertEqual(formal.call_args.args[1], query)
        self.assertEqual(work.call_args.kwargs["query"], query)
        self.assertEqual(candidate.call_args.args[2], query)
        self.assertEqual(self.f.saved_query(), query)

    def test_followup_does_not_reopen_task_or_change_execution_permissions(self):
        self.f.task(self.goal)
        self.r.task_update(self.p, self.f.conversation, self.goal, status="delivered")
        before = self.r.task(self.p, self.f.conversation)
        self.f.context("是的，已重启，继续执行吧")
        after = self.r.task(self.p, self.f.conversation)
        for field in ("id", "revision", "write_version", "execution_epoch", "status", "scope"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(after["status"], "delivered")


if __name__ == "__main__":
    unittest.main()
