"""Synthetic rollback exports keep explicit user anchors and native boundaries."""
import json
import unittest
from unittest.mock import patch

from agent_learning.antigravity_transcript import read_public
from tests import test_antigravity_transcript as fixture


class AntigravityBranchTests(unittest.TestCase):
    TRANSCRIPT_FILENAME = "transcript.jsonl"
    setUp = fixture.AntigravityTranscriptTests.setUp
    raw = fixture.AntigravityTranscriptTests.raw
    write = fixture.AntigravityTranscriptTests.write
    invoke = fixture.AntigravityTranscriptTests.invoke
    post = fixture.AntigravityTranscriptTests.post
    stop = fixture.AntigravityTranscriptTests.stop
    events = fixture.AntigravityTranscriptTests.events
    coverage = fixture.AntigravityTranscriptTests.coverage
    input_metadata = fixture.AntigravityTranscriptTests.input_metadata

    def append(self, index, kind, text=None, **extra):
        record = {"step_index": index, "type": kind, "status": "DONE",
                  "source": "USER_EXPLICIT" if kind == "USER_INPUT" else "MODEL"}
        if text is not None:
            record["content"] = text
        record.update(extra)
        self.rows.append(record)
        self.write()

    def prior(self, prompt="current explicit request"):
        self.append(34, "USER_INPUT", "old abandoned request")
        self.append(37, "USER_INPUT", prompt)
        self.append(39, "PLANNER_RESPONSE", "abandoned response", thinking="PRIVATE-OLD")

    def branch(self, prompt="current explicit request", **extra):
        self.append(37, "USER_INPUT", prompt, **extra)

    def test_reader_returns_only_latest_complete_explicit_branch(self):
        self.prior()
        self.branch()
        result = read_public(self.raw())
        self.assertTrue(result["available"], result)
        self.assertEqual([r["step_index"] for r in result["records"]], [37])
        self.assertTrue(result["branch_identity"])
        self.assertNotIn("abandoned", json.dumps(result))
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_unanchored_rollback_remains_unbound_at_adapter(self):
        self.prior()
        self.branch()
        self.invoke(initialNumSteps=40)
        self.assertFalse(self.input_metadata()["public_window"]["available"])
        self.assertEqual(len(self.events("AntigravityPublicWindow")), 0)

    def test_current_native_prompt_can_bind_branch_and_capture_only_new_reply(self):
        self.prior()
        self.branch()
        self.invoke(initialNumSteps=40, lastUserInput="current explicit request")
        self.assertTrue(self.input_metadata()["public_window"]["available"])
        self.append(40, "PLANNER_RESPONSE", "fresh public reply", thinking="PRIVATE-NEW")
        self.post()
        self.assertTrue(self.coverage()["available"], self.coverage())
        self.assertEqual(self.coverage()["step_index"], 40)
        self.stop()
        public = json.loads(self.events("Stop")[-1]["payload"])
        self.assertEqual(public["last_assistant_message"], "fresh public reply")
        self.assertNotIn("PRIVATE", str([row["payload"] for row in self.events("PublicMessageSegment")]))

    def test_new_pre_without_native_prompt_rebinds_prior_verified_native_user(self):
        self.append(37, "USER_INPUT", "current explicit request")
        self.invoke(initialNumSteps=38, lastUserInput="current explicit request")
        self.append(39, "PLANNER_RESPONSE", "abandoned response")
        self.branch()
        self.post()
        self.assertFalse(self.coverage()["available"])
        self.invoke(1, initialNumSteps=40)
        self.assertTrue(self.input_metadata()["public_window"]["available"])
        self.append(40, "PLANNER_RESPONSE", "fresh public reply")
        self.post()
        self.assertTrue(self.coverage()["available"], self.coverage())
        self.stop()
        self.assertEqual(len(self.events("Stop")), 1)

    def test_failed_first_window_can_use_exact_native_input_on_later_pre(self):
        self.append(37, "USER_INPUT", "old export spelling")
        self.invoke(initialNumSteps=38, lastUserInput="new native request")
        self.assertEqual(len(self.events("AntigravityPublicWindow")), 0)
        self.append(39, "PLANNER_RESPONSE", "abandoned reply")
        self.branch("new native request")
        self.invoke(1, initialNumSteps=40)
        self.assertTrue(self.input_metadata()["public_window"]["available"])
        self.append(40, "PLANNER_RESPONSE", "new exact native reply")
        self.post()
        self.assertTrue(self.coverage()["available"], self.coverage())

    def test_different_user_digest_cannot_borrow_prior_native_anchor(self):
        self.append(37, "USER_INPUT", "native original")
        self.invoke(initialNumSteps=38, lastUserInput="native original")
        self.append(39, "PLANNER_RESPONSE", "abandoned reply")
        self.branch("different request")
        self.invoke(1, initialNumSteps=40)
        self.assertFalse(self.input_metadata()["public_window"]["available"])

    def test_later_same_text_user_step_cannot_borrow_prior_native_anchor(self):
        self.append(37, "USER_INPUT", "same request")
        self.invoke(initialNumSteps=38, lastUserInput="same request")
        self.append(45, "PLANNER_RESPONSE", "abandoned reply")
        self.append(42, "USER_INPUT", "same request")
        self.invoke(1, initialNumSteps=46)
        self.assertFalse(self.input_metadata()["public_window"]["available"])

    def test_native_anchor_cannot_cross_a_stop(self):
        self.append(37, "USER_INPUT", "same request")
        self.invoke(initialNumSteps=38, lastUserInput="same request")
        self.append(38, "PLANNER_RESPONSE", "completed reply")
        self.post()
        self.stop()
        self.branch("same request")
        self.invoke(1, initialNumSteps=40)
        self.assertFalse(self.input_metadata()["public_window"]["available"])

    def test_post_cannot_use_branch_appended_after_its_pre_even_with_same_prompt(self):
        self.append(37, "USER_INPUT", "same request")
        self.invoke(initialNumSteps=38, lastUserInput="same request")
        self.append(38, "PLANNER_RESPONSE", "abandoned reply")
        self.branch("same request")
        self.append(39, "PLANNER_RESPONSE", "later reply")
        self.post()
        self.assertFalse(self.coverage()["available"])
        with patch.object(self.r, "_capture_delivery") as capture:
            self.stop()
        capture.assert_not_called()
        self.assertEqual(len(self.events("Stop")), 0)

    def test_invalid_reset_records_keep_conflict(self):
        for record in (
            {"type":"PLANNER_RESPONSE", "source":"MODEL", "status":"DONE", "content":"wrong"},
            {"type":"USER_INPUT", "source":"MODEL", "status":"DONE", "content":"wrong"},
            {"type":"USER_INPUT", "source":"USER_EXPLICIT", "status":"RUNNING", "content":"partial"},
            {"type":"USER_INPUT", "source":"USER_EXPLICIT", "status":"DONE", "content":""},
            {"type":"USER_INPUT", "source":"USER_EXPLICIT", "status":"DONE", "content":"partial", "truncated_fields":["content"]},
        ):
            with self.subTest(record=record):
                self.rows=[]
                self.prior()
                self.rows.append(dict(record, step_index=37))
                self.write()
                self.assertFalse(read_public(self.raw())["available"])

    def test_adjacent_equal_user_index_keeps_conflict(self):
        self.append(37, "USER_INPUT", "same request")
        self.branch("same request")
        self.assertEqual(read_public(self.raw())["reason"], "transcript_step_conflict")

    def test_branch_identity_changes_when_another_rollback_is_appended(self):
        self.prior()
        self.branch()
        first=read_public(self.raw())
        self.append(40, "PLANNER_RESPONSE", "public reply")
        self.branch()
        second=read_public(self.raw())
        self.assertTrue(first["available"], first)
        self.assertTrue(second["available"], second)
        self.assertNotEqual(first["branch_identity"], second["branch_identity"])

    def test_transcript_only_input_cannot_authorize_rollback(self):
        self.append(37, "USER_INPUT", "same request")
        self.invoke(initialNumSteps=38)
        self.append(39, "PLANNER_RESPONSE", "abandoned reply")
        self.branch("same request")
        self.invoke(1, initialNumSteps=40)
        self.assertFalse(self.input_metadata()["public_window"]["available"])

    def test_newer_different_native_input_blocks_older_matching_anchor(self):
        self.append(37, "USER_INPUT", "older native request")
        self.invoke(initialNumSteps=38, lastUserInput="older native request")
        self.invoke(1, initialNumSteps=40, lastUserInput="newer native request")
        self.append(41, "PLANNER_RESPONSE", "abandoned reply")
        self.branch("older native request")
        self.invoke(2, initialNumSteps=42)
        self.assertFalse(self.input_metadata()["public_window"]["available"])

    def test_explicit_empty_native_input_blocks_older_matching_anchor(self):
        self.append(37, "USER_INPUT", "same request")
        self.invoke(initialNumSteps=38, lastUserInput="same request")
        self.invoke(1, initialNumSteps=40, lastUserInput="")
        self.append(41, "PLANNER_RESPONSE", "abandoned reply")
        self.branch("same request")
        self.invoke(2, initialNumSteps=42)
        self.assertFalse(self.input_metadata()["public_window"]["available"])

    def test_invocation_must_advance_to_rebind_a_native_anchor(self):
        self.append(37, "USER_INPUT", "same request")
        self.invoke(initialNumSteps=38, lastUserInput="same request")
        self.append(39, "PLANNER_RESPONSE", "abandoned reply")
        self.branch("same request")
        self.invoke(0, initialNumSteps=40)
        self.assertFalse(self.input_metadata()["public_window"]["available"])

    def test_unbound_native_anchor_cannot_guess_missing_user_position(self):
        self.append(37, "USER_INPUT", "old export")
        self.invoke(initialNumSteps=38, lastUserInput="native current request")
        self.append(39, "PLANNER_RESPONSE", "abandoned reply")
        self.append(34, "USER_INPUT", "native current request")
        self.invoke(1, initialNumSteps=40)
        self.assertFalse(self.input_metadata()["public_window"]["available"])

    def test_incomplete_native_anchor_is_not_reused(self):
        self.append(37, "USER_INPUT", "same request")
        self.invoke(initialNumSteps=38, lastUserInput="same request")
        self.r.db.execute("UPDATE events SET payload=json_set(payload,'$.capture_coverage.lastUserInput.truncated',json('true')) WHERE type='AntigravityPublicInput'")
        self.append(39, "PLANNER_RESPONSE", "abandoned reply")
        self.branch("same request")
        self.invoke(1, initialNumSteps=40)
        self.assertFalse(self.input_metadata()["public_window"]["available"])

    def test_rebound_different_user_does_not_clear_unfinished_execution_fence(self):
        self.append(37, "USER_INPUT", "old request")
        self.invoke(initialNumSteps=38, lastUserInput="old request")
        self.append(39, "PLANNER_RESPONSE", "old reply")
        self.branch("new request")
        self.invoke(1, initialNumSteps=40, lastUserInput="new request")
        self.append(40, "PLANNER_RESPONSE", "new reply")
        self.post()
        self.assertTrue(self.coverage()["available"])
        with patch.object(self.r, "_capture_delivery") as capture:
            self.stop()
        capture.assert_not_called()
        self.assertEqual(len(self.events("Stop")), 0)

    def test_failed_native_window_allows_only_contiguous_sdk_positions(self):
        self.append(37, "USER_INPUT", "old export")
        self.invoke(initialNumSteps=40, lastUserInput="current native request")
        self.append(41, "PLANNER_RESPONSE", "abandoned reply")
        self.branch("current native request")
        self.append(38, "EPHEMERAL_MESSAGE", "PRIVATE-SYSTEM", source="SYSTEM_SDK")
        self.append(39, "EPHEMERAL_MESSAGE", "PRIVATE-SYSTEM", source="SYSTEM_SDK")
        self.invoke(1, initialNumSteps=42)
        self.assertTrue(self.input_metadata()["public_window"]["available"])

    def test_recovery_after_two_unbound_native_users_does_not_invent_execution_start(self):
        self.append(37, "USER_INPUT", "lagged export")
        self.invoke(initialNumSteps=38, lastUserInput="first native request")
        self.invoke(1, initialNumSteps=38, lastUserInput="second native request")
        self.assertEqual(len(self.events("AntigravityPublicWindow")), 0)
        self.append(39, "PLANNER_RESPONSE", "abandoned reply")
        self.branch("second native request")
        self.invoke(2, initialNumSteps=40)
        self.assertTrue(self.input_metadata()["public_window"]["available"])
        self.append(40, "PLANNER_RESPONSE", "second native response")
        self.post()
        self.assertTrue(self.coverage()["available"])
        with patch.object(self.r, "_capture_delivery") as capture:
            self.stop()
        capture.assert_not_called()
        self.assertEqual(len(self.events("Stop")), 0)

    def test_tail_window_shift_preserves_the_active_branch_identity(self):
        for index in range(34):
            self.append(index, "GENERIC", "PRIVATE-FILLER")
        self.prior()
        self.branch()
        suffix="".join(json.dumps(row)+"\n" for row in self.rows[-3:]).encode()
        with patch("agent_learning.antigravity_transcript.MAX_SCAN_BYTES", len(suffix)+160):
            before=read_public(self.raw())
            self.append(40, "PLANNER_RESPONSE", "fresh response"*5)
            after=read_public(self.raw())
        self.assertTrue(before["available"], before)
        self.assertTrue(after["available"], after)
        self.assertTrue(before["head_omitted"])
        self.assertTrue(after["head_omitted"])
        self.assertTrue(before["branch_recovered"])
        self.assertTrue(after["branch_recovered"])
        self.assertEqual(before["branch_identity"],after["branch_identity"])

    def test_marker_can_leave_tail_without_losing_the_current_user_identity(self):
        for index in range(34):
            self.append(index, "GENERIC", "PRIVATE-FILLER")
        self.prior()
        self.branch()
        self.append(41, "USER_INPUT", "later current request")
        with patch("agent_learning.antigravity_transcript.MAX_SCAN_BYTES", 1024):
            before=read_public(self.raw())
            self.invoke(initialNumSteps=42, lastUserInput="later current request")
            self.append(42, "PLANNER_RESPONSE", "reply"*150)
            after=read_public(self.raw())
            self.post()
        self.assertTrue(before["branch_recovered"])
        self.assertTrue(before["head_omitted"])
        self.assertTrue(after["available"], after)
        self.assertFalse(after["branch_recovered"])
        self.assertTrue(after["head_omitted"])
        self.assertEqual(before["branch_identity"],after["branch_identity"])
        self.assertTrue(self.coverage()["available"],self.coverage())

    def test_same_user_reappend_cannot_hide_behind_an_evicted_rollback_marker(self):
        for index in range(34):
            self.append(index, "GENERIC", "PRIVATE-FILLER")
        self.append(41, "USER_INPUT", "current request")
        with patch("agent_learning.antigravity_transcript.MAX_SCAN_BYTES", 1024):
            self.invoke(initialNumSteps=42, lastUserInput="current request")
            self.append(42, "PLANNER_RESPONSE", "abandoned")
            self.append(41, "USER_INPUT", "current request")
            self.append(43, "PLANNER_RESPONSE", "reply"*150)
            snapshot=read_public(self.raw())
            self.post()
        self.assertTrue(snapshot["available"],snapshot)
        self.assertFalse(snapshot["branch_recovered"])
        self.assertTrue(snapshot["head_omitted"])
        self.assertFalse(self.coverage()["available"])

    def test_old_window_without_identity_only_retains_normal_full_scan_compatibility(self):
        self.append(37, "USER_INPUT", "current request")
        self.invoke(initialNumSteps=38, lastUserInput="current request")
        self.r.db.execute("UPDATE events SET payload=json_remove(payload,'$.branch_identity') WHERE type='AntigravityPublicWindow'")
        self.append(38, "PLANNER_RESPONSE", "current reply")
        self.post()
        self.assertTrue(self.coverage()["available"],self.coverage())

    def test_evicted_marker_does_not_hide_two_unclosed_native_inputs(self):
        self.append(37, "USER_INPUT", "lagged export")
        self.invoke(initialNumSteps=38, lastUserInput="first native request")
        self.invoke(1, initialNumSteps=38, lastUserInput="second native request")
        self.append(39, "PLANNER_RESPONSE", "abandoned"*250)
        self.branch("second native request")
        for index in range(38,43):
            self.append(index,"EPHEMERAL_MESSAGE","PRIVATE-SDK",source="SYSTEM_SDK")
        active="".join(json.dumps(row)+"\n" for row in self.rows[-6:]).encode()
        with patch("agent_learning.antigravity_transcript.MAX_SCAN_BYTES",len(active)+400):
            snapshot=read_public(self.raw())
            self.assertTrue(snapshot["head_omitted"])
            self.assertFalse(snapshot["branch_recovered"])
            self.invoke(2,initialNumSteps=43)
            self.assertTrue(self.input_metadata()["public_window"]["available"])
            self.append(43,"PLANNER_RESPONSE","second public reply")
            self.post()
            self.assertTrue(self.coverage()["available"])
            with patch.object(self.r,"_capture_delivery") as capture:
                self.stop()
        capture.assert_not_called()
        self.assertEqual(len(self.events("Stop")),0)

    def test_equal_native_text_at_later_user_position_is_not_execution_start(self):
        self.append(37,"USER_INPUT","lagged export")
        self.invoke(initialNumSteps=38,lastUserInput="continue")
        self.invoke(1,initialNumSteps=43,lastUserInput="continue")
        self.assertEqual(len(self.events("AntigravityPublicWindow")),0)
        self.append(44,"PLANNER_RESPONSE","abandoned")
        self.append(42,"USER_INPUT","continue")
        self.invoke(2,initialNumSteps=45)
        self.assertTrue(self.input_metadata()["public_window"]["available"])
        self.append(45,"PLANNER_RESPONSE","new public reply")
        self.post()
        self.assertTrue(self.coverage()["available"])
        with patch.object(self.r,"_capture_delivery") as capture:
            self.stop()
        capture.assert_not_called()
        self.assertEqual(len(self.events("Stop")),0)


if __name__ == "__main__":
    unittest.main()
