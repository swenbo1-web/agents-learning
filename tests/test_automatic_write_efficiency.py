"""Automatic observation selection uses recorded evidence and no model calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.runtime import Runtime


class AutomaticWriteEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)

    def capture(self, kind, payload, *, conversation="chat", operation=None, agent=None, outcome=None):
        with self.r.transaction():
            event, _ = self.r._event(self.p, conversation, kind, payload,
                operation_id=operation, agent_id=agent,
                outcome=outcome or ("succeeded" if kind == "PostToolUse" else "reported"))
            memory = self.r._work_capture_event(self.p, event["id"], self.workspace, raw=payload)
        return event, memory

    def test_missing_post_input_uses_matching_observed_pre_input_for_each_host(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host), patch("subprocess.Popen") as model:
                self.r.host = host
                self.r.host_scope = host + ":default"
                conversation, operation = host + ":chat", host + ":test"
                pre, _ = self.capture("PreToolUse", {"tool_name":"exec_command",
                    "tool_input":{"cmd":"python -B -m unittest"}}, conversation=conversation, operation=operation)
                post, memory = self.capture("PostToolUse", {"tool_name":"exec_command",
                    "tool_response":{"exit_code":0, "output":"All regression tests passed"}},
                    conversation=conversation, operation=operation)
                self.assertIsNotNone(memory, "successful verification was lost")
                record = json.loads(self.r.db.execute("SELECT body FROM work_memories WHERE event_id=?", (post["id"],)).fetchone()[0])
                self.assertEqual(record["type"], "verification")
                self.assertEqual(record["classification_source_event"], pre["id"])
                self.assertIn("unittest", record["tool_input_preview"])
                self.assertEqual(record["validation_level"], "tool_observed")
                self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
                model.assert_not_called()

    def test_pre_input_cannot_cross_conversation_agent_or_tool(self):
        for identity, changes in (("conversation", {"conversation":"other"}),
                                   ("agent", {"agent":"child"}),
                                   ("tool", {"tool":"read_tool"})):
            with self.subTest(identity=identity):
                operation = "isolation:" + identity
                self.capture("PreToolUse", {"tool_name":"exec_command", "tool_input":{"cmd":"pytest"}}, operation=operation)
                _, memory = self.capture("PostToolUse", {"tool_name":changes.get("tool", "exec_command"),
                    "tool_response":{"exit_code":0}}, operation=operation,
                    conversation=changes.get("conversation", "chat"), agent=changes.get("agent"))
                self.assertIsNone(memory)

    def test_explicit_empty_post_input_is_not_replaced_and_ambiguous_pre_is_not_guessed(self):
        self.capture("PreToolUse", {"tool_name":"exec_command", "tool_input":{"cmd":"pytest"}}, operation="explicit")
        _, memory = self.capture("PostToolUse", {"tool_name":"exec_command", "tool_input":{},
            "tool_response":{"exit_code":0}}, operation="explicit")
        self.assertIsNone(memory)
        for command in ("pytest", "echo different operation input"):
            self.capture("PreToolUse", {"tool_name":"exec_command", "tool_input":{"cmd":command}}, operation="ambiguous")
        _, memory = self.capture("PostToolUse", {"tool_name":"exec_command", "tool_response":{"exit_code":0}}, operation="ambiguous")
        self.assertIsNone(memory)

    def test_recorded_input_classification_does_not_upgrade_unknown_result(self):
        self.capture("PreToolUse", {"tool_name":"exec_command", "tool_input":{"cmd":"pytest"}}, operation="unknown")
        event, memory = self.capture("PostToolUse", {"tool_name":"exec_command", "toolCall":None,
            "tool_response":{"output":"No definitive exit status"}}, operation="unknown", outcome="unknown")
        self.assertIsNotNone(memory)
        self.assertEqual(self.r.work_source_snapshot(self.p, event["id"])["state"], "unknown")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_classification_is_fenced_by_task_assignment_and_execution_revision(self):
        self.capture("PreToolUse", {"tool_name":"exec_command", "tool_input":{"cmd":"pytest"}}, operation="scoped")
        event, memory = self.capture("PostToolUse", {"tool_name":"exec_command", "tool_response":{"exit_code":0}}, operation="scoped")
        self.assertIsNotNone(memory)
        for field, value in (("task_id","another-task"), ("task_revision",99),
                             ("task_write_version",99), ("execution_epoch",99), ("assignment_id","another-assignment")):
            with self.subTest(field=field):
                changed = dict(event, **{field:value})
                self.assertIsNone(self.r._work_verification_source(self.p, changed, {}, {}, "exec_command"))

    def test_account_epoch_cannot_supply_prior_input(self):
        self.capture("PreToolUse", {"tool_name":"exec_command", "tool_input":{"cmd":"pytest"}, "host_epoch":1}, operation="epoch")
        _, memory = self.capture("PostToolUse", {"tool_name":"exec_command", "tool_response":{"exit_code":0},
                                                  "host_epoch":2}, operation="epoch")
        self.assertIsNone(memory)

    def test_codex_real_hook_path_captures_omitted_input_verification(self):
        base = {"session_id":"native-hook", "cwd":str(self.workspace)}
        self.r.handle_hook(dict(base, hook_event_name="PreToolUse", tool_use_id="test", tool_name="exec_command",
                               tool_input={"cmd":"python -B -m unittest"}))
        self.r.handle_hook(dict(base, hook_event_name="PostToolUse", tool_use_id="test", tool_name="exec_command",
                               tool_response={"exit_code":0, "output":"Regression complete"}))
        memories = self.r.db.execute("SELECT body FROM work_memories WHERE project=? AND conversation=?", (self.p, base["session_id"])).fetchall()
        self.assertEqual([json.loads(row[0])["type"] for row in memories], ["verification"])

    def test_different_observed_test_commands_are_not_deduplicated_by_identical_results(self):
        self.r.config["admin"].update(enabled=True)
        self.r.config["work_memory"].update(summary_min_events=2, summary_max_events=4)
        self.r.configure_work_summary(self.p, True)
        for operation, command in (("unit","pytest unit"), ("integration","pytest integration")):
            self.capture("PreToolUse", {"tool_name":"exec_command", "tool_input":{"cmd":command}}, operation=operation)
            self.capture("PostToolUse", {"tool_name":"exec_command", "tool_response":{"exit_code":0}}, operation=operation)
        queued = self.r._work_queue_summary(self.p, "chat", self.workspace)
        self.assertEqual(queued["status"], "pending", queued)
        self.assertEqual(queued["source_count"], 2)

    def test_identical_observed_test_inputs_can_still_deduplicate_without_losing_records(self):
        self.r.config["admin"].update(enabled=True)
        self.r.config["work_memory"].update(summary_min_events=1, summary_max_events=4)
        self.r.configure_work_summary(self.p, True)
        for operation in ("first","repeated"):
            self.capture("PreToolUse", {"tool_name":"exec_command", "tool_input":{"cmd":"pytest unit"}}, operation=operation)
            self.capture("PostToolUse", {"tool_name":"exec_command", "tool_response":{"exit_code":0}}, operation=operation)
        queued = self.r._work_queue_summary(self.p, "chat", self.workspace)
        self.assertEqual(queued["source_count"], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], 2)

    def long_message(self):
        self.r.config["admin"].update(enabled=True)
        self.r.config["work_memory"].update(summary_min_events=2, summary_max_events=4,
                                            summary_strategy="all_windows")
        self.r.configure_work_summary(self.p, True)
        event, _ = self.capture("Stop", {"last_assistant_message":"Optical calibration results remain reported, pending hardware validation. " * 45})
        return event

    def test_complete_public_segments_do_not_send_their_parent_preview_twice(self):
        parent = self.long_message()
        with patch("subprocess.Popen") as model:
            result = self.r._work_queue_summary(self.p, "chat", self.workspace)
        self.assertEqual(result["status"], "pending", result)
        snapshots = json.loads(self.r.db.execute("SELECT source_snapshots FROM work_summary_links WHERE problem_id=?",
                                                 (result["problem_id"],)).fetchone()[0])
        self.assertNotIn(parent["id"], {item["event_id"] for item in snapshots})
        self.assertTrue(all("segment" in item["projection"]["coverage"] for item in snapshots))
        self.assertTrue(self.r.db.execute("SELECT 1 FROM work_memories WHERE event_id=?", (parent["id"],)).fetchone())
        model.assert_not_called()

    def test_incomplete_segment_set_retains_parent_preview(self):
        parent = self.long_message()
        self.r.db.execute("DELETE FROM work_memories WHERE event_id IN (SELECT id FROM events "
            "WHERE type='PublicMessageSegment' ORDER BY created DESC LIMIT 1)")
        result = self.r._work_queue_summary(self.p, "chat", self.workspace)
        snapshots = json.loads(self.r.db.execute("SELECT source_snapshots FROM work_summary_links WHERE problem_id=?",
                                                 (result["problem_id"],)).fetchone()[0])
        self.assertIn(parent["id"], {item["event_id"] for item in snapshots})


if __name__ == "__main__":
    unittest.main()
