"""第三模块与真实 Hook、可选同步队列的交叉回归，不连接外部服务。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.runtime import Runtime


class Module3IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(Path(self.temp.name) / "state")
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "chat", "compiler analysis", {"mode": "analysis"})

    def hook(self, kind, **fields):
        return self.r.handle_hook(dict(hook_event_name=kind, session_id="chat", cwd=str(self.workspace), **fields))

    def publish(self, title="compiler", success=False):
        refs = []
        if success:
            self.hook("PreToolUse", tool_use_id="compile", tool_name="Bash")
            self.hook("PostToolUse", tool_use_id="compile", tool_name="Bash", tool_response={"exit_code": 0})
            refs = [self.r.db.execute("SELECT id FROM events WHERE type='PostToolUse' ORDER BY created DESC LIMIT 1").fetchone()[0]]
        problem = self.r.feedback(self.p, "chat", title + " evidence", category="operation", event_refs=refs)
        packet = self.r.review_packet(self.p, problem["id"])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title=title, content=title + " guidance", applicability="local tools", limitations="visible evidence only",
            category="operation", validation_level="tool_success" if success else "reported",
            evidence_refs=[event["id"] for event in packet["events"]])
        return self.r.publish(self.p, candidate["candidate_id"])

    def test_pre_tool_warns_source_conflict_without_new_prompt_or_revision(self):
        knowledge = self.publish(success=True)
        injected = self.hook("UserPromptSubmit", prompt="compiler", turn_id="inject")
        self.assertIn('"current_tool_success":true', injected["hookSpecificOutput"]["additionalContext"])
        self.hook("PostToolUse", tool_use_id="compile", tool_name="Bash", tool_response={"exit_code": 1})
        warned = self.hook("PreToolUse", tool_use_id="next", tool_name="Read")
        context = warned["hookSpecificOutput"]["additionalContext"]
        self.assertIn(knowledge["knowledge_id"], context)
        self.assertIn("health=conflicted", context)
        self.assertIn("tool_success=False", context)
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["revision"], 1)
        self.assertEqual(self.hook("PreToolUse", tool_use_id="after-warning", tool_name="Read"), {})

    def test_source_warning_survives_unrelated_query(self):
        knowledge = self.publish(success=True)
        self.hook("UserPromptSubmit", prompt="compiler", turn_id="inject")
        self.hook("PostToolUse", tool_use_id="compile", tool_name="Bash", tool_response={"exit_code": 1})
        result = self.hook("UserPromptSubmit", prompt="unrelatedsubject", turn_id="unrelated")
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn(knowledge["knowledge_id"], context)
        self.assertIn("health=conflicted", context)

    def test_small_warning_budget_retains_undelivered_changes(self):
        first = self.publish(title="compiler one", success=True)
        second = self.publish(title="compiler two", success=True)
        self.hook("UserPromptSubmit", prompt="compiler", turn_id="both")
        self.hook("PostToolUse", tool_use_id="compile", tool_name="Bash", tool_response={"exit_code": 1})
        self.r.config["injection_max_chars"] = 128
        one = self.hook("PreToolUse", tool_use_id="warning-1", tool_name="Read")["hookSpecificOutput"]["additionalContext"]
        two = self.hook("PreToolUse", tool_use_id="warning-2", tool_name="Read")["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(one), 128)
        self.assertLessEqual(len(two), 128)
        for knowledge in (first, second):
            self.assertEqual(int(knowledge["knowledge_id"] in one) + int(knowledge["knowledge_id"] in two), 1)
        self.assertEqual(self.hook("PreToolUse", tool_use_id="warning-3", tool_name="Read"), {})

    def test_source_tracking_keeps_recent_injection_order_and_bound(self):
        injected = []
        for index in range(10):
            knowledge = self.publish(title="topic" + str(index))
            self.hook("UserPromptSubmit", prompt="topic" + str(index), turn_id="topic" + str(index))
            injected.append(knowledge["knowledge_id"] + "@1")
        tracked = json.loads(self.r.db.execute("SELECT source_states FROM contexts WHERE project=? AND conversation='chat'", (self.p,)).fetchone()[0])
        self.assertEqual([identity for identity, state in tracked], injected[-8:])

    def test_restore_requeues_superseded_sync_that_was_never_sent(self):
        self.r.config["hindsight"]["enabled"] = True
        knowledge = self.publish()
        self.r.revoke(self.p, knowledge["knowledge_id"])
        with patch("agent_learning.hindsight.HindsightClient") as client:
            self.assertEqual(self.r.sync_once(self.p)["status"], "superseded")
            client.assert_not_called()
            self.r.restore(self.p, knowledge["knowledge_id"], 1)
            operation = self.r.db.execute("SELECT state,attempts FROM outbox").fetchone()
            self.assertEqual(tuple(operation), ("pending", 0))
            client.return_value.retain.return_value = {"success": True}
            self.assertEqual(self.r.sync_once(self.p)["status"], "synced")
            client.return_value.retain.assert_called_once()

    def test_restore_does_not_retry_unknown_or_attempted_sync(self):
        self.r.config["hindsight"]["enabled"] = True
        knowledge = self.publish()
        for state in ("blocked", "superseded", "synced"):
            with self.subTest(state=state):
                self.r.db.execute("UPDATE outbox SET state=?,attempts=1,error='unknown prior write'", (state,))
                self.r.revoke(self.p, knowledge["knowledge_id"])
                self.r.restore(self.p, knowledge["knowledge_id"], 1)
                self.assertEqual(tuple(self.r.db.execute("SELECT state,attempts FROM outbox").fetchone()), (state, 1))


if __name__ == "__main__":
    unittest.main()
