"""Stronger masking must not reinterpret unchanged old callbacks as conflicts."""
import json
import unittest
from unittest.mock import patch

from agent_learning.runtime import ContractError
from tests import test_module4_context as fixtures
from tests.test_redaction_efficiency import legacy_clean


class RedactionCaptureCompatibilityTests(unittest.TestCase):
    setUp = fixtures.Module4ContextTests.setUp
    hook = fixtures.Module4ContextTests.hook

    def fields(self, *, marker="SYNTHETIC_A", command="inspect"):
        return {"tool_use_id": "upgrade-call", "tool_name": "Bash",
                "tool_input": {"command": command + ' {"api_key":"' + marker + '"}'}}

    def old_hook(self, kind, **fields):
        with patch("agent_learning.runtime.clean", side_effect=legacy_clean):
            return self.hook(kind, **fields)

    def operation(self):
        return dict(self.r.db.execute("SELECT * FROM operations").fetchone())

    def events(self):
        return [tuple(row) for row in self.r.db.execute("SELECT * FROM events ORDER BY id")]

    def test_same_input_across_upgrade_keeps_success_and_old_evidence(self):
        fields = self.fields()
        self.old_hook("PreToolUse", **fields)
        prior = self.events()[0]
        self.hook("PostToolUse", **fields, tool_response={"exit_code": 0})
        self.assertEqual(self.operation()["state"], "succeeded")
        self.assertEqual(self.operation()["conflict"], 0)
        self.assertIn(prior, self.events())

    def test_truncated_pre_uses_full_legacy_signature_without_guessing_prefix(self):
        self.r.config["event_max_chars"] = 256
        fields = self.fields(command="inspect " + "padding " * 1000)
        self.old_hook("PreToolUse", **fields)
        row = self.r.db.execute("SELECT payload,tool_input_digest FROM events WHERE type='PreToolUse'").fetchone()
        self.assertTrue(json.loads(row["payload"])["truncated"])
        self.assertTrue(row["tool_input_digest"])
        self.hook("PostToolUse", **fields, tool_response={"exit_code": 0})
        self.assertEqual(self.operation()["state"], "succeeded")

    def test_nonsecret_change_still_marks_conflict(self):
        self.old_hook("PreToolUse", **self.fields())
        self.hook("PostToolUse", **self.fields(command="delete"), tool_response={"exit_code": 0})
        self.assertEqual(self.operation()["conflict"], 1)

    def test_newly_masked_secret_change_cannot_match_old_plaintext_signature(self):
        self.old_hook("PreToolUse", **self.fields(marker="SYNTHETIC_A"))
        self.hook("PostToolUse", **self.fields(marker="SYNTHETIC_B"), tool_response={"exit_code": 0})
        self.assertEqual(self.operation()["conflict"], 1)

    def test_observed_pre_replay_keeps_single_original_event(self):
        fields = self.fields()
        self.old_hook("PreToolUse", **fields)
        before = self.events()
        self.hook("PreToolUse", **fields)
        self.assertEqual(before, self.events())

    def test_enforced_pre_replay_keeps_permission_but_unknown_signature_does_not(self):
        self.r.config["enforce_task_policy"] = True
        fields = self.fields()
        with patch.object(self.r, "check_execution", return_value={"decision": "allowed", "reason": "fixture"}):
            self.old_hook("PreToolUse", **fields)
            before = self.events()
            result = self.hook("PreToolUse", **fields)
            self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
            self.assertEqual(before, self.events())
            self.r.db.execute("UPDATE events SET capture_digest=NULL WHERE type='PreToolUse'")
            result = self.hook("PreToolUse", **fields)
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("operation_payload_mismatch", result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_completed_post_replay_keeps_single_original_callback(self):
        fields = self.fields()
        response = {"exit_code": 0, "stdout": '{"api_key":"SYNTHETIC_A"}'}
        self.old_hook("PreToolUse", **fields)
        self.old_hook("PostToolUse", **fields, tool_response=response)
        before, operation = self.events(), self.operation()
        self.hook("PostToolUse", **fields, tool_response=response)
        self.assertEqual(before, self.events())
        self.assertEqual(operation, self.operation())

    def test_first_new_post_does_not_run_legacy_redaction_on_full_output(self):
        fields = self.fields()
        self.hook("PreToolUse", **fields)
        response = {"exit_code": 0, "stdout": "public output " * 10000}
        with patch("agent_learning.capture_compat.legacy_digest", side_effect=AssertionError("unexpected legacy scan")):
            self.hook("PostToolUse", **fields, tool_response=response)
        self.assertEqual(self.operation()["state"], "succeeded")

    def test_stop_and_prompt_replay_keep_original_source_and_memory(self):
        for kind, key in (("Stop", "last_assistant_message"), ("UserPromptSubmit", "prompt")):
            fields = {"turn_id": kind, key: 'retained result {"api_key":"SYNTHETIC_A"}'}
            self.old_hook(kind, **fields)
            before = self.events()
            memories = [tuple(row) for row in self.r.db.execute("SELECT * FROM work_memories ORDER BY id")]
            self.hook(kind, **fields)
            self.assertEqual(before, self.events())
            self.assertEqual(memories, [tuple(row) for row in self.r.db.execute("SELECT * FROM work_memories ORDER BY id")])

    def test_same_explicit_event_id_rejects_changed_legacy_private_or_public_value(self):
        original = {"detail": '{"api_key":"SYNTHETIC_A"}', "public": "keep"}
        with patch("agent_learning.runtime.clean", side_effect=legacy_clean):
            self.r._event(self.p, "chat", "Stop", original, event_id="stable")
        self.r._event(self.p, "chat", "Stop", original, event_id="stable")
        for changed in (dict(original, public="changed"), dict(original, detail='{"api_key":"SYNTHETIC_B"}')):
            with self.assertRaises(ContractError):
                self.r._event(self.p, "chat", "Stop", changed, event_id="stable")

    def test_missing_digest_does_not_authorize_changed_projection(self):
        original = {"detail": '{"api_key":"SYNTHETIC_A"}'}
        with patch("agent_learning.runtime.clean", side_effect=legacy_clean):
            self.r._event(self.p, "chat", "Stop", original, event_id="stable")
        self.r.db.execute("UPDATE events SET capture_digest=NULL WHERE type='Stop'")
        with self.assertRaises(ContractError):
            self.r._event(self.p, "chat", "Stop", original, event_id="stable")

    def test_stop_without_host_turn_identity_remains_two_distinct_events(self):
        fields = {"last_assistant_message": '{"api_key":"SYNTHETIC_A"}'}
        self.old_hook("Stop", **fields)
        self.hook("Stop", **fields)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='Stop'").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
