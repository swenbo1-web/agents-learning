"""Synthetic tool mentions cannot substitute for the current business problem."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning import candidate_references as refs
from agent_learning.knowledge_index import _match_info, normalized_query_plan
from agent_learning.runtime import Runtime


class ToolBusinessRelevanceTests(unittest.TestCase):
    QUERY = "exec_command SQLite rollback"
    RIGHT = "exec_command SQLite rollback preserves the transaction boundary."
    WRONG = "exec_command image render chooses a four panel composition."

    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "build/local/fix1438/tool"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="business-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": False},
            "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.serial = 0

    def candidate(self, content):
        self.serial += 1
        conversation = "source-" + str(self.serial)
        task = self.r.task_update(self.p, conversation, content, new_task=True)
        with self.r.transaction():
            event, _ = self.r._event(self.p, conversation, "Stop", {"last_assistant_message": content},
                task_id=task["id"], task_revision=task["revision"], outcome="reported")
            problem = self.r._problem(self.p, conversation, content, "operation", [event["id"]],
                                      task_binding=task)
        return self.r.submit_candidate(self.p, problem_id=problem["id"],
            problem_revision=problem["revision"], title=content, content=content,
            category="operation", applicability="local environment", limitations="reported only",
            validation_level="reported", evidence_refs=[event["id"]])["candidate_id"]

    def publish(self, content):
        return self.r.publish(self.p, self.candidate(content))["knowledge_id"]

    def automatic_ids(self, query):
        return [item["knowledge_id"] for item in self.r.search(self.p, query, _automatic=True)["items"]]

    def match(self, query, content, *, limitations=""):
        plan = normalized_query_plan(query)
        weights = {word: 1 for word in plan["terms"]}
        return _match_info(json.dumps({"title": content, "content": content,
            "limitations": limitations}), weights, sum(weights.values()), plan["meaningful"],
            _anchors=plan["exact_anchors"])

    def test_shared_tool_is_not_a_business_anchor_or_coverage_shortcut(self):
        for tool in ("exec_command", "exec", "command", "functions.exec_command", "apply_patch",
                     "read_file", "run_command", "Bash"):
            for topic in ("SQLite rollback", "authentication refresh"):
                with self.subTest(tool=tool, topic=topic):
                    self.assertFalse(self.match(tool + " " + topic,
                        tool + " image render chooses a four panel composition.")["auto_eligible"])

    def test_high_tool_component_coverage_still_needs_a_business_hit(self):
        # exec_command contributes three matching tokens out of four.
        info = self.match("exec_command SQLite", self.WRONG)
        self.assertGreaterEqual(info["coverage"], 0.65)
        self.assertFalse(info["auto_eligible"])

    def test_limitations_cannot_supply_the_only_business_match(self):
        self.assertFalse(self.match(self.QUERY, self.WRONG,
            limitations="Not applicable to SQLite rollback.")["auto_eligible"])

    def test_automatic_formal_recall_keeps_the_correct_business_card(self):
        wrong = self.publish(self.WRONG)
        right = self.publish(self.RIGHT)
        self.assertEqual(self.automatic_ids(self.QUERY), [right])
        self.assertNotEqual(wrong, right)

    def test_manual_search_and_read_preserve_the_unrelated_card_and_authority(self):
        wrong = self.publish(self.WRONG)
        before = tuple(self.r.db.execute("SELECT body,revision FROM knowledge WHERE id=?", (wrong,)).fetchone())
        self.automatic_ids(self.QUERY)
        item = next(item for item in self.r.search(self.p, self.QUERY)["items"] if item["knowledge_id"] == wrong)
        self.assertFalse(item["match"]["auto_eligible"])
        self.assertEqual(self.r.read(self.p, wrong)["content"], self.WRONG)
        self.assertEqual(tuple(self.r.db.execute("SELECT body,revision FROM knowledge WHERE id=?", (wrong,)).fetchone()), before)

    def test_pending_candidates_share_the_business_gate(self):
        wrong = self.candidate(self.WRONG)
        right = self.candidate(self.RIGHT)
        refs.configure(self.r, self.p, True, 0)
        self.assertEqual([item["candidate_id"] for item in refs.search(self.r, self.p, self.QUERY, limit=3)], [right])
        self.assertTrue(refs.assess(self.r, self.p, wrong)["eligible"])

    def test_work_history_shares_the_gate_and_manual_history_remains_readable(self):
        for number, content in enumerate((self.WRONG, self.RIGHT)):
            self.r.handle_hook({"hook_event_name": "Stop", "cwd": str(self.workspace),
                "session_id": "past-" + str(number), "last_assistant_message": content})
        rows = self.r._work_search_rows(self.p, self.QUERY, automatic=True)
        self.assertEqual([json.loads(row["body"])["summary"] for row in rows], [self.RIGHT])
        manual = self.r.work_search(self.p, self.QUERY, max_chars=10000)["items"]
        self.assertTrue(any(item["summary"] == self.WRONG for item in manual))
        wrong = next(item for item in manual if item["summary"] == self.WRONG)
        self.assertEqual(self.r.work_read(self.p, wrong["memory_id"])["content"], self.WRONG)

    def test_semantic_similarity_cannot_revive_tool_only_business_mismatch(self):
        wrong = self.publish(self.WRONG)
        right = self.publish(self.RIGHT)
        row = self.r.db.execute("SELECT * FROM knowledge WHERE id=?", (wrong,)).fetchone()
        prepared = {"config": {"enabled": False, "automatic_min_similarity": 0},
            "diagnostics": {}, "vector": [1, 0], "signature": self.r.semantic_signature(self.p)}
        matches = {wrong: {"row": row, "score": 1, "rank": 1, "input_truncated": False}}
        with patch.object(self.r, "_semantic_prepare", return_value=prepared), \
                patch.object(self.r, "_semantic_candidates", return_value=matches):
            for cached in (False, True):
                with self.subTest(cached=cached):
                    self.assertEqual(self.automatic_ids(self.QUERY), [right])
            manual = self.r.search(self.p, self.QUERY)["items"]
            self.assertFalse(next(item for item in manual if item["knowledge_id"] == wrong)["match"]["auto_eligible"])

    def test_tool_itself_and_explicit_fault_remain_eligible(self):
        cases = [("exec_command", self.WRONG),
            ("how to use exec_command", "Use exec_command to invoke the shell."),
            ("what is exec_command tool usage", "exec_command invokes the shell."),
            ("exec_command 工具调用用法", "exec_command 调用的参数应按工具说明填写。"),
            ("如何使用 exec_command", "exec_command 调用的参数应按工具说明填写。"),
            ("exec_command timeout", "exec_command timeout requires bounded output."),
            ("apply_patch failed", "apply_patch failed because the patch context changed."),
            ("read_file permission denied", "read_file permission denied requires checking file access.")]
        for query, content in cases:
            with self.subTest(query=query):
                self.assertTrue(self.match(query, content)["auto_eligible"])

    def test_business_errors_and_files_keep_sparse_exact_identity_recall(self):
        for identity in ("E_SQLITE_ROLLBACK", "E1042", "src/事务.py", ".env"):
            with self.subTest(identity=identity):
                query = "exec_command " + identity + " inspect transaction recovery safety details"
                self.assertTrue(self.match(query, "exec_command " + identity + " recovery.")["auto_eligible"])
                self.assertFalse(self.match(query, self.WRONG)["auto_eligible"])

    def test_complete_tool_identity_remains_required_alongside_business_identity(self):
        query = "exec_command E_SQLITE_ROLLBACK"
        self.assertEqual(normalized_query_plan(query)["exact_anchors"], ["exec_command", "e_sqlite_rollback"])
        self.assertFalse(self.match(query, "E_SQLITE_ROLLBACK transaction recovery.")["auto_eligible"])
        self.assertTrue(self.match(query, "exec_command E_SQLITE_ROLLBACK transaction recovery.")["auto_eligible"])

    def test_chinese_issue_phrase_and_long_identified_question_stay_eligible(self):
        query = "请认真检查 exec_command 当前项目数据库事务恢复应该如何推进以及风险"
        self.assertTrue(self.match(query, "exec_command 数据库事务恢复已保留边界。")["auto_eligible"])
        query = "exec_command E_SQLITE_ROLLBACK " + "background context " * 500
        right = self.publish("exec_command E_SQLITE_ROLLBACK preserves the transaction.")
        self.assertEqual(self.automatic_ids(query), [right])


if __name__ == "__main__":
    unittest.main()
