"""第四模块真实 SQLite/Hook 回归，不连接宿主或外部模型。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.runtime import ContractError, Runtime


class Module4ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.home = Path(self.temp.name) / "state"
        self.r = Runtime(self.home)
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "chat", "diagnostic task", {"mode": "analysis"})

    def hook(self, kind, **fields):
        return self.r.handle_hook(dict(hook_event_name=kind, session_id="chat", cwd=str(self.workspace), **fields))

    def publish(self, title, content=None, metadata=None, success=False,
                applicability="local tools", limitations="reported evidence only"):
        refs = []
        if success:
            self.hook("PreToolUse", tool_use_id=title, tool_name="Bash")
            self.hook("PostToolUse", tool_use_id=title, tool_name="Bash", tool_response={"exit_code": 0})
            refs = [self.r.db.execute("SELECT id FROM events WHERE type='PostToolUse' ORDER BY created DESC LIMIT 1").fetchone()[0]]
        problem = self.r.feedback(self.p, "chat", title + " evidence", category="operation", event_refs=refs)
        packet = self.r.review_packet(self.p, problem["id"])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title=title, content=content or title + " guidance", applicability=applicability, limitations=limitations,
            category="operation", validation_level="tool_success" if success else "reported",
            evidence_refs=[event["id"] for event in packet["events"]], metadata=metadata or {})
        return self.r.publish(self.p, candidate["candidate_id"])

    def text(self, result):
        return result.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_revocation_waits_for_complete_notification_under_small_budget(self):
        knowledge = self.publish("compiler")
        self.hook("UserPromptSubmit", prompt="compiler", turn_id="inject")
        self.r.revoke(self.p, knowledge["knowledge_id"], expected_revision=self.r.read(self.p, knowledge["knowledge_id"])["revision"])
        self.r.config["injection_max_chars"] = 128
        small = self.text(self.hook("UserPromptSubmit", prompt="继续", turn_id="small"))
        self.assertLessEqual(len(small), 128)
        # 句柄优先占位，通知整条保留到有空间的工具前置事件。
        self.assertNotIn(knowledge["knowledge_id"], small)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_exposures").fetchone()[0], 1)
        next_context = self.text(self.hook("PreToolUse", tool_use_id="next", tool_name="Read"))
        self.assertIn(knowledge["knowledge_id"] + "@1", next_context)
        self.assertIn("已撤销", next_context)
        self.assertEqual(self.hook("PreToolUse", tool_use_id="after", tool_name="Read"), {})

    def test_compact_puts_current_match_before_old_cards(self):
        old = []
        for name in ("historya", "historyb", "historyc"):
            old.append(self.publish(name))
            self.hook("UserPromptSubmit", prompt=name, turn_id=name)
        self.hook("UserPromptSubmit", prompt="newcriticaltopic", turn_id="new-query")
        new = self.publish("newcriticaltopic")
        text = self.text(self.hook("SessionStart", source="compact"))
        self.assertIn(new["knowledge_id"], text)
        cards = [json.loads(line) for line in text.splitlines() if line.startswith('{"')]
        self.assertEqual(cards[0]["knowledge_id"], new["knowledge_id"])
        self.assertLessEqual(len(cards), 3)

    def test_continuation_skips_unchanged_search_and_new_publication_invalidates(self):
        item = self.publish("compiler")
        self.hook("UserPromptSubmit", prompt="compiler", turn_id="initial")
        with patch.object(self.r, "search", side_effect=AssertionError("unchanged continuation queried")):
            for index in range(2):
                self.assertNotIn(item["knowledge_id"], self.text(self.hook("UserPromptSubmit", prompt="继续", turn_id=str(index))))
        new = self.publish("compiler recovery")
        self.assertIn(new["knowledge_id"], self.text(self.hook("UserPromptSubmit", prompt="继续", turn_id="changed")))

    def test_zero_match_plan_invalidates_after_publish(self):
        self.hook("UserPromptSubmit", prompt="newtopic", turn_id="missing")
        with patch.object(self.r, "search", side_effect=AssertionError("negative result queried")):
            self.hook("UserPromptSubmit", prompt="继续", turn_id="same")
        item = self.publish("newtopic")
        self.assertIn(item["knowledge_id"], self.text(self.hook("UserPromptSubmit", prompt="继续", turn_id="published")))

    def test_acknowledgements_reuse_focus_without_keyword_retrieval(self):
        self.hook("UserPromptSubmit", prompt="compiler", turn_id="focus")
        with patch.object(self.r, "search", wraps=self.r.search) as search:
            for index, word in enumerate(("确认", "确认。", "确定", "同意", "confirmed")):
                self.hook("UserPromptSubmit", prompt=word, turn_id="ack-" + str(index))
        self.assertTrue(all(call.args[1] == "compiler" for call in search.call_args_list))
        query = self.r.db.execute("SELECT last_query FROM contexts WHERE conversation='chat'").fetchone()[0]
        self.assertEqual(query, "compiler")

    def test_old_acknowledgement_focus_recovers_task_goal(self):
        self.hook("UserPromptSubmit", prompt="compiler", turn_id="initial")
        self.r.db.execute("UPDATE contexts SET last_query='确认' WHERE conversation='chat'")
        with patch.object(self.r, "search", wraps=self.r.search) as search:
            self.hook("UserPromptSubmit", prompt="确认", turn_id="recover")
        self.assertEqual(search.call_args.args[1], "diagnostic task")

    def test_confirmation_with_domain_text_remains_a_new_query(self):
        self.hook("UserPromptSubmit", prompt="compiler", turn_id="initial")
        with patch.object(self.r, "search", wraps=self.r.search) as search:
            self.hook("UserPromptSubmit", prompt="确认修复 signingcompat 签名", turn_id="new-focus")
        self.assertEqual(search.call_args.args[1], "确认修复 signingcompat 签名")

    def test_old_exposure_source_changes_are_tracked_beyond_eight(self):
        first = self.publish("oldcompiler", success=True)
        self.hook("UserPromptSubmit", prompt="oldcompiler", turn_id="old")
        for index in range(9):
            name = "topic" + str(index)
            self.publish(name)
            self.hook("UserPromptSubmit", prompt=name, turn_id=name)
        self.hook("PostToolUse", tool_use_id="oldcompiler", tool_name="Bash", tool_response={"exit_code": 1})
        warned = self.text(self.hook("PreToolUse", tool_use_id="next", tool_name="Read"))
        self.assertIn(first["knowledge_id"], warned)
        self.assertIn("health=conflicted", warned)

    def test_tool_failure_triggers_once_without_user_prompt(self):
        item = self.publish("Bash E_IMPORT", content="Bash E_IMPORT stderr importfailure guidance exit_code")
        self.hook("SessionStart")
        self.hook("PreToolUse", tool_use_id="failed", tool_name="Bash")
        self.hook("PostToolUse", tool_use_id="failed", tool_name="Bash", tool_response={"exit_code": 1, "stderr": "E_IMPORT"})
        next_context = self.text(self.hook("PreToolUse", tool_use_id="retry", tool_name="Read"))
        self.assertIn(item["knowledge_id"], next_context)
        with patch.object(self.r, "search", side_effect=AssertionError("fault retrieved twice")):
            self.hook("PreToolUse", tool_use_id="again", tool_name="Read")
        self.assertEqual(self.r.db.execute("SELECT value FROM metrics WHERE project=? AND name='failure_retrievals'", (self.p,)).fetchone()[0], 1)

    def test_late_failure_never_queries_new_task(self):
        self.hook("PreToolUse", tool_use_id="late", tool_name="Bash")
        self.r.task_update(self.p, "chat", "replacement", new_task=True)
        self.hook("PostToolUse", tool_use_id="late", tool_name="Bash", tool_response={"exit_code": 1, "stderr": "E_OLD"})
        with patch.object(self.r, "search", side_effect=AssertionError("old fault mixed into current task")):
            self.hook("PreToolUse", tool_use_id="new", tool_name="Read")

    def test_fault_waits_for_budget_without_repeating_search(self):
        item = self.publish("Bash E_IMPORT", content="Bash E_IMPORT stderr importfailure guidance exit_code")
        revoked = [self.publish("old" + str(index)) for index in range(2)]
        for index in range(2):
            self.hook("UserPromptSubmit", prompt="old" + str(index), turn_id="old" + str(index))
            self.r.revoke(self.p, revoked[index]["knowledge_id"], expected_revision=self.r.read(self.p, revoked[index]["knowledge_id"])["revision"])
        self.hook("PreToolUse", tool_use_id="failed", tool_name="Bash")
        self.hook("PostToolUse", tool_use_id="failed", tool_name="Bash", tool_response={"exit_code": 1, "stderr": "E_IMPORT"})
        self.r.config["injection_max_chars"] = 128
        small = self.text(self.hook("PreToolUse", tool_use_id="small", tool_name="Read"))
        self.assertNotIn(item["knowledge_id"], small)
        self.assertEqual(self.r.db.execute("SELECT state FROM context_faults").fetchone()[0], "ready")
        self.r.config["injection_max_chars"] = 2400
        with patch.object(self.r, "search", side_effect=AssertionError("ready fault queried twice")):
            text = self.text(self.hook("PreToolUse", tool_use_id="after", tool_name="Read"))
        self.assertIn(item["knowledge_id"], text)
        self.assertEqual(self.r.db.execute("SELECT state FROM context_faults").fetchone()[0], "done")

    def test_new_prompt_has_priority_and_fault_survives_execution_fence(self):
        item = self.publish("Bash E_IMPORT", content="Bash E_IMPORT stderr guidance exit_code")
        self.hook("PreToolUse", tool_use_id="failed", tool_name="Bash")
        self.hook("PostToolUse", tool_use_id="failed", tool_name="Bash", tool_response={"exit_code": 1, "stderr": "E_IMPORT"})
        text = self.text(self.hook("UserPromptSubmit", prompt="请定位刚才的错误", turn_id="after-failure"))
        self.assertNotIn(item["knowledge_id"], text)
        text = self.text(self.hook("PreToolUse", tool_use_id="after-prompt", tool_name="Read"))
        self.assertIn(item["knowledge_id"], text)
        self.assertEqual(self.r.db.execute("SELECT state FROM context_faults").fetchone()[0], "done")

    def test_conflict_after_query_prevents_stale_valid_injection(self):
        item = self.publish("compiler", success=True)
        original = self.r.search
        writer = Runtime(self.home)
        self.addCleanup(writer.close)
        def interleave(*args, **kwargs):
            result = original(*args, **kwargs)
            writer.db.execute("UPDATE operations SET conflict=1 WHERE project=?", (self.p,))
            return result
        with patch.object(self.r, "search", side_effect=interleave):
            text = self.text(self.hook("UserPromptSubmit", prompt="compiler", turn_id="race"))
        self.assertNotIn(item["knowledge_id"], text)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_exposures").fetchone()[0], 0)

    def test_query_runs_outside_write_transaction_and_account_race_is_rejected(self):
        self.publish("compiler")
        original = self.r.search
        writer = Runtime(self.home)
        self.addCleanup(writer.close)
        def interleave(*args, **kwargs):
            self.assertFalse(self.r.db.in_transaction)
            result = original(*args, **kwargs)
            writer.account_changed("switch")
            return result
        with patch.object(self.r, "search", side_effect=interleave):
            with self.assertRaisesRegex(ContractError, "reconnect_required"):
                self.hook("UserPromptSubmit", prompt="compiler", turn_id="race")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_exposures").fetchone()[0], 0)

    def test_task_change_during_query_cannot_commit_old_cards(self):
        self.publish("compiler")
        original = self.r.search
        writer = Runtime(self.home)
        self.addCleanup(writer.close)
        def interleave(*args, **kwargs):
            result = original(*args, **kwargs)
            writer.task_update(self.p, "chat", "replacement", new_task=True)
            return result
        with patch.object(self.r, "search", side_effect=interleave):
            with self.assertRaisesRegex(ContractError, "context_changed"):
                self.hook("UserPromptSubmit", prompt="compiler", turn_id="race")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_exposures").fetchone()[0], 0)

    def test_weak_single_character_overlap_does_not_auto_inject(self):
        item = self.publish("检查电子表格", content="核对Excel数据是否完整")
        text = self.text(self.hook("UserPromptSubmit", prompt="查询C++崩溃", turn_id="weak"))
        self.assertNotIn(item["knowledge_id"], text)

    def test_explicit_environment_filter_is_used_for_automatic_retrieval(self):
        a = self.publish("compiler", metadata={"environment": "A"})
        b = self.publish("compiler", metadata={"environment": "B"})
        self.r.task_update(self.p, "chat", "compiler", {"mode": "analysis", "knowledge_filters": {"environment": "B"}})
        text = self.text(self.hook("UserPromptSubmit", prompt="compiler", turn_id="environment"))
        self.assertNotIn(a["knowledge_id"], text)
        self.assertIn(b["knowledge_id"], text)

    def test_hook_keeps_middle_of_complete_conditions_when_budget_fits(self):
        applicability = "当前环境已核对。" * 32 + "必须预先生成验证过的备份。" + "确认回滚入口可用。" * 12
        limitations = "只适用于本地操作。" * 32 + "禁止覆盖共享原件。" + "继续核对执行结果。" * 12
        item = self.publish("compiler", applicability=applicability, limitations=limitations)
        text = self.text(self.hook("UserPromptSubmit", prompt="compiler", turn_id="full-conditions"))
        card = next(json.loads(line) for line in text.splitlines() if line.startswith('{"'))
        self.assertEqual(card["knowledge_id"], item["knowledge_id"])
        self.assertEqual(card["applicability"], applicability)
        self.assertEqual(card["limitations"], limitations)
        self.assertFalse(card.get("read_required", False))

    def test_hook_uses_read_required_when_complete_constraints_do_not_fit(self):
        item = self.publish("compiler", limitations="执行前必须验证备份与回滚。" * 300)
        text = self.text(self.hook("UserPromptSubmit", prompt="compiler", turn_id="read-conditions"))
        card = next(json.loads(line) for line in text.splitlines() if line.startswith('{"'))
        self.assertEqual(card["knowledge_id"], item["knowledge_id"])
        self.assertTrue(card["read_required"])
        self.assertNotIn("content_summary", card)
        self.assertLessEqual(len(text), self.r.config["injection_max_chars"])

    def test_automatic_retrieval_refills_after_five_conflicted_matches(self):
        for index in range(5):
            title = f"compiler recovery bad{index}"
            self.publish(title, success=True)
            self.hook("PostToolUse", tool_use_id=title, tool_name="Bash", tool_response={"exit_code": 1})
        valid = self.publish("diagnosis", content="compiler recovery")
        default = self.r.search(self.p, "compiler recovery")["items"]
        self.assertEqual(len(default), 5)
        self.assertTrue(all(item["source_health"] == "conflicted" for item in default))
        text = self.text(self.hook("UserPromptSubmit", prompt="compiler recovery", turn_id="refill"))
        self.assertIn(valid["knowledge_id"], text)
        self.assertNotIn('"source_health":"conflicted"', text)

    def test_query_plan_preserves_differences_before_last_four_thousand(self):
        item = self.publish("newneedle")
        common = " and " * 1100
        self.hook("UserPromptSubmit", prompt="absentmarker" + common, turn_id="long-missing")
        with patch.object(self.r, "search", wraps=self.r.search) as searched:
            text = self.text(self.hook("UserPromptSubmit", prompt="newneedle" + common, turn_id="long-found"))
        searched.assert_called_once()
        self.assertIn(item["knowledge_id"], text)
        saved = self.r.db.execute("SELECT last_query FROM contexts WHERE project=? AND conversation='chat'", (self.p,)).fetchone()[0]
        self.assertIn("newneedle", saved)
        with patch.object(self.r, "search", side_effect=AssertionError("same effective query should be reused")):
            self.hook("UserPromptSubmit", prompt="继续", turn_id="same-long-query")

    def test_automatic_cache_is_invalidated_by_source_health_change(self):
        item = self.publish("compiler", success=True)
        self.r.db.execute("UPDATE operations SET conflict=1 WHERE project=?", (self.p,))
        self.assertEqual(self.r.search(self.p, "compiler", _automatic=True)["items"], [])
        self.r.db.execute("UPDATE operations SET conflict=0 WHERE project=?", (self.p,))
        result = self.r.search(self.p, "compiler", _automatic=True)["items"]
        self.assertEqual([entry["knowledge_id"] for entry in result], [item["knowledge_id"]])
        self.assertTrue(result[0]["match"]["auto_eligible"])


if __name__ == "__main__":
    unittest.main()
