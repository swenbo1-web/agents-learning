"""Native public transcript recovery with isolated, synthetic JSONL fixtures."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.antigravity import AntigravityAdapter, reset_session
from agent_learning.antigravity_transcript import read_public
from agent_learning.runtime import ContractError, Runtime


class AntigravityTranscriptTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "build" / "local" / "ag1412" / "transcript-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.artifact = self.root / "brain" / "chat"
        self.path = self.artifact / ".system_generated" / "logs" / "transcript.jsonl"
        self.path.parent.mkdir(parents=True)
        self.rows = []
        self.r = Runtime(self.root / "home", config={}, host="antigravity")
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.c = self.r.host_conversation("chat")
        self.adapter = AntigravityAdapter(self.r, self.workspace)

    def raw(self, **fields):
        return {"conversationId": "chat", "workspacePaths": [str(self.workspace)],
                "transcriptPath": str(self.path), "artifactDirectoryPath": str(self.artifact), **fields}

    def write(self):
        self.path.write_text("".join(json.dumps(row) + "\n" for row in self.rows), encoding="utf-8")

    def add(self, kind, content=None, **extra):
        row = {"step_index": len(self.rows), "type": kind,
               "source": "USER_EXPLICIT" if kind == "USER_INPUT" else "MODEL", "status": "DONE"}
        if content is not None:
            row["content"] = content
        row.update(extra)
        self.rows.append(row)
        self.write()
        return row

    def invoke(self, number=0, **fields):
        self.last_pre = {"invocationNum": number, "initialNumSteps": len(self.rows), **fields}
        return self.adapter.handle("PreInvocation", self.raw(**self.last_pre))

    def post(self, **fields):
        return self.adapter.handle("PostInvocation", self.raw(**{**self.last_pre, **fields}))

    def stop(self, number=0, **fields):
        return self.adapter.handle("Stop", self.raw(**{
            "executionNum": number, "fullyIdle": True, "terminationReason": "model_stop", **fields}))

    def events(self, kind):
        return self.r.db.execute("SELECT * FROM events WHERE type=? ORDER BY created,id", (kind,)).fetchall()

    def coverage(self):
        return json.loads(self.events("AntigravityPublicResponse")[-1]["payload"])["capture_coverage"]["finalModelOutput"]

    def normal(self):
        self.add("USER_INPUT", "Choose sqlite outbox design")
        self.invoke()
        self.add("PLANNER_RESPONSE", "Project uses sqlite outbox; validation remains pending.")
        self.post()
        self.stop()

    def test_public_prompt_final_and_progressive_work_memory(self):
        self.normal()
        self.assertEqual(len(self.events("UserPromptSubmit")), 1)
        self.assertEqual(len(self.events("PublicMessageSegment")), 1)
        self.assertTrue(self.coverage()["available"])
        self.assertEqual(self.coverage()["source"], "antigravity_transcript")
        self.assertTrue(self.r.work_search(self.p, "outbox")["items"])
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 25)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_reader_excludes_thoughts_tools_and_system_text(self):
        self.add("USER_INPUT", "public user", thinking="PRIVATE-USER")
        self.add("EPHEMERAL_MESSAGE", "PRIVATE-SYSTEM", source="SYSTEM_SDK")
        self.add("GENERIC", "PRIVATE-TOOL", tool_calls=[{"args": "PRIVATE-ARGS"}])
        self.add("PLANNER_RESPONSE", "public final", thinking="PRIVATE-THOUGHT", tool_calls=[])
        result = read_public(self.raw())
        text = json.dumps(result)
        self.assertNotIn("PRIVATE", text)
        self.assertNotIn("thinking", text)
        self.assertNotIn("tool_calls", text)

    def test_same_user_loop_refreshes_snapshot_without_duplicate_message(self):
        self.add("USER_INPUT", "public user")
        self.invoke()
        self.add("PLANNER_RESPONSE", thinking="PRIVATE")
        self.invoke(1)
        self.add("PLANNER_RESPONSE", "public final")
        self.post()
        self.assertEqual(len(self.events("UserPromptSubmit")), 1)
        self.assertEqual(len(self.events("PublicMessageSegment")), 1)
        self.assertEqual(len(self.events("AntigravityPublicWindow")), 2)

    def test_same_text_new_user_after_stop_preserved(self):
        self.normal()
        self.add("USER_INPUT", "Choose sqlite outbox design")
        self.invoke(1)
        self.add("PLANNER_RESPONSE", "Second public delivery")
        self.post()
        self.stop(0)
        self.assertEqual(len(self.events("UserPromptSubmit")), 2)
        self.assertEqual(len(self.events("PublicMessageSegment")), 2)

    def test_late_post_cannot_borrow_a_new_invocation(self):
        self.add("USER_INPUT", "first request")
        self.invoke()
        first = dict(self.last_pre)
        self.add("PLANNER_RESPONSE", "first public reply")
        self.add("USER_INPUT", "second request")
        self.invoke(1)
        self.add("PLANNER_RESPONSE", "second public reply")
        self.post(**first)
        self.assertEqual(len(self.events("PublicMessageSegment")), 0)
        self.assertEqual(self.coverage()["reason"], "transcript_invocation_superseded")
        self.post()
        self.assertEqual(len(self.events("PublicMessageSegment")), 1)
        self.assertIn("second public", self.events("PublicMessageSegment")[0]["payload"])

    def test_resumed_conversation_uses_native_pair_without_execution_counter_guess(self):
        self.add("USER_INPUT", "old request")
        self.add("PLANNER_RESPONSE", "old reply")
        self.add("USER_INPUT", "resumed request")
        self.invoke(5)
        self.add("PLANNER_RESPONSE", "bound resumed reply")
        self.post()
        self.stop(0)
        self.add("USER_INPUT", "fresh request")
        self.invoke(6)
        self.add("PLANNER_RESPONSE", "fresh bound reply")
        self.post()
        self.stop(0)
        self.assertEqual(len(self.events("PublicMessageSegment")), 2)
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_phases").fetchone()[0], 0)

    def test_completed_post_replay_does_not_read_or_backfill(self):
        self.normal()
        with patch("agent_learning.antigravity_transcript.read_public", side_effect=AssertionError("no replay I/O")):
            self.post()
            self.stop()
            self.invoke(initialNumSteps=1)
        self.assertEqual(len(self.events("PublicMessageSegment")), 1)

    def test_post_without_window_does_not_backfill_on_replay(self):
        self.add("USER_INPUT", "new request")
        self.add("PLANNER_RESPONSE", "reply without original pre")
        self.last_pre = {"invocationNum": 0, "initialNumSteps": 1}
        self.post()
        self.assertEqual(len(self.events("PublicMessageSegment")), 0)
        with patch("agent_learning.antigravity_transcript.read_public", side_effect=AssertionError("no replay I/O")):
            self.post()
        self.assertEqual(self.coverage()["reason"], "transcript_window_unbound")

    def test_task_changed_after_last_pre_blocks_capture(self):
        self.add("USER_INPUT", "current request")
        self.invoke()
        self.r.task_update(self.p, self.c, "new task")
        self.add("PLANNER_RESPONSE", "must not belong to new task")
        self.post()
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(self.coverage()["reason"], "transcript_task_changed")

    def test_task_update_followed_by_native_pre_refreshes_snapshot(self):
        self.add("USER_INPUT", "current request")
        self.invoke()
        self.r.task_update(self.p, self.c, "current task")
        self.add("GENERIC", "observable tool result")
        self.invoke(1)
        self.add("PLANNER_RESPONSE", "bound public reply")
        self.post()
        self.assertEqual(len(self.events("PublicMessageSegment")), 1)

    def test_new_explicit_user_before_stop_cannot_borrow_old_window(self):
        self.add("USER_INPUT", "first request")
        self.invoke()
        self.add("USER_INPUT", "second request")
        self.add("PLANNER_RESPONSE", "second reply")
        self.post()
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(self.coverage()["reason"], "transcript_user_boundary_changed")

    def test_stop_does_not_read_transcript_or_promote_public_response(self):
        self.add("USER_INPUT", "request")
        self.invoke()
        self.add("PLANNER_RESPONSE", "public response with execution status unverified")
        self.post()
        with patch("agent_learning.antigravity_transcript.read_public", side_effect=AssertionError("no Stop I/O")):
            self.stop(0, terminationReason="aborted")
            self.stop(0, fullyIdle=False)
            self.stop(0, error="failed")
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(len(self.events("PublicMessageSegment")), 1)
        self.assertEqual(json.loads(self.events("PublicMessageSegment")[0]["payload"])["completion"], "partial")

    def test_reset_and_account_change_do_not_reuse_window(self):
        self.add("USER_INPUT", "old request")
        self.invoke()
        reset_session(self.r, self.workspace, "chat")
        self.add("PLANNER_RESPONSE", "old callback text")
        self.post()
        self.assertEqual(len(self.events("PublicMessageSegment")), 0)
        self.r.account_changed("another-account")
        self.post()
        self.assertEqual(len(self.events("PublicMessageSegment")), 0)

    def test_content_truncation_preserved_reasoning_truncation_ignored(self):
        self.add("USER_INPUT", "request")
        self.invoke()
        self.add("PLANNER_RESPONSE", "public partial", truncated_fields=["content"])
        self.post()
        self.assertTrue(self.coverage()["truncated"])
        self.add("USER_INPUT", "next request")
        self.invoke(1)
        self.add("PLANNER_RESPONSE", "complete public reply", truncated_fields=["thinking"], thinking="PRIVATE")
        self.post()
        self.assertFalse(self.coverage()["truncated"])
        self.assertTrue(self.coverage()["full_original_available"])
        self.assertEqual(json.loads(self.events("PublicMessageSegment")[-1]["payload"])["completion"], "partial")

    def test_multiple_model_responses_cannot_be_guessed_into_one_invocation(self):
        self.add("USER_INPUT", "request")
        self.invoke()
        self.add("PLANNER_RESPONSE", "first response")
        self.add("PLANNER_RESPONSE", "second response")
        self.post()
        self.assertFalse(self.coverage()["available"])
        self.assertEqual(self.coverage()["reason"], "transcript_response_window_ambiguous")
        self.assertEqual(len(self.events("PublicMessageSegment")), 0)

    def test_tool_only_model_invocation_is_not_public_text(self):
        self.add("USER_INPUT", "request")
        self.invoke()
        self.add("PLANNER_RESPONSE", thinking="PRIVATE", tool_calls=[{"name": "read"}])
        self.post()
        self.assertEqual(self.coverage()["reason"], "transcript_final_public_missing")
        self.assertEqual(len(self.events("PublicMessageSegment")), 0)

    def test_multimount_and_ambiguous_trajectory_do_not_read(self):
        other = self.root / "other"
        other.mkdir()
        with patch("agent_learning.antigravity_transcript.read_public", side_effect=AssertionError("isolated mount")):
            self.invoke(workspacePaths=[str(self.workspace), str(other)])

    def test_explicit_native_fields_prefer_original_source(self):
        self.add("USER_INPUT", "transcript request")
        self.invoke(lastUserInput="native request")
        self.assertEqual(len(self.events("AntigravityPublicWindow")), 0)
        with patch("agent_learning.antigravity_transcript.read_public", side_effect=AssertionError("no Stop I/O")):
            self.stop(finalModelOutput="native public reply")
        self.assertIn("native public reply", self.events("Stop")[0]["payload"])

    def test_native_prompt_and_transcript_response_can_share_verified_window(self):
        self.add("USER_INPUT", "native request")
        self.invoke(lastUserInput="native request")
        prompt = json.loads(self.events("UserPromptSubmit")[0]["payload"])
        self.assertEqual(prompt["capture_coverage"]["lastUserInput"]["source"], "native_hook")
        self.add("PLANNER_RESPONSE", "transcript public response")
        self.post()
        self.assertEqual(len(self.events("PublicMessageSegment")), 1)
        self.assertTrue(self.coverage()["available"])

    def test_unsupported_explicit_native_prompt_is_not_replaced(self):
        self.add("USER_INPUT", "transcript user text")
        with patch("agent_learning.antigravity_transcript.read_public", side_effect=AssertionError("invalid native field")):
            self.invoke(lastUserInput={"text": "unsupported"})
        self.assertEqual(len(self.events("UserPromptSubmit")), 0)
        self.assertEqual(len(self.events("AntigravityPublicWindow")), 0)

    def test_empty_direct_public_fields_are_not_reported_as_available(self):
        self.add("USER_INPUT", "transcript user text")
        self.invoke(lastUserInput="  \n\t")
        self.assertEqual(len(self.events("UserPromptSubmit")), 0)
        self.assertEqual(len(self.events("AntigravityPublicWindow")), 0)
        self.stop(finalModelOutput="  \n\t")
        body = json.loads(self.events("AntigravityStop")[0]["payload"])
        self.assertFalse(body["capture_coverage"]["finalModelOutput"]["available"])
        self.assertEqual(body["capture_coverage"]["finalModelOutput"]["reason"], "empty")
        self.assertFalse(body["capture_coverage"]["finalModelOutput"]["full_original_available"])
        self.assertEqual(len(self.events("Stop")), 0)

    def test_path_missing_wrong_conversation_and_unrelated_file(self):
        self.add("USER_INPUT", "request")
        for update, reason in (({"artifactDirectoryPath": ""}, "transcript_path_missing"),
                               ({"conversationId": "other"}, "transcript_conversation_mismatch"),
                               ({"transcriptPath": str(self.artifact / "notes.txt")}, "transcript_path_invalid")):
            with self.subTest(update=update):
                self.assertEqual(read_public(self.raw(**update))["reason"], reason)

    def test_directory_is_not_a_regular_transcript(self):
        self.path.mkdir()
        self.assertEqual(read_public(self.raw())["reason"], "transcript_not_regular")

    def test_symlink_to_another_file_rejected(self):
        other = self.root / "other.jsonl"
        other.write_text('{}\n', encoding="utf-8")
        try:
            self.path.symlink_to(other)
        except OSError:
            self.skipTest("symlink privilege unavailable")
        self.assertEqual(read_public(self.raw())["reason"], "transcript_path_redirected")

    def test_invalid_incomplete_oversized_and_unknown_schema(self):
        for content, reason in ((b'{broken}\n', "transcript_invalid_json"),
                                (b'{}', "transcript_incomplete_line"),
                                (b'{}\n', "transcript_schema_unknown"),
                                (b'x' * (256 * 1024 + 1) + b'\n', "transcript_line_budget")):
            with self.subTest(reason=reason):
                self.path.write_bytes(content)
                self.assertEqual(read_public(self.raw())["reason"], reason)

    def test_conflicting_step_order_rejected(self):
        self.add("USER_INPUT", "one")
        self.add("PLANNER_RESPONSE", "two", step_index=0)
        self.assertEqual(read_public(self.raw())["reason"], "transcript_step_conflict")

    def test_deadline_is_an_explicit_capture_gap(self):
        self.add("USER_INPUT", "request")
        with patch("agent_learning.antigravity_transcript.READ_SECONDS", -1):
            self.assertEqual(read_public(self.raw())["reason"], "transcript_read_deadline")

    def test_tail_budget_does_not_turn_old_history_into_current_public_input(self):
        self.add("USER_INPUT", "request")
        self.add("GENERIC", "unrelated tool bytes " * 20)
        with patch("agent_learning.antigravity_transcript.MAX_SCAN_BYTES", 128):
            self.invoke()
        self.assertEqual(len(self.events("UserPromptSubmit")), 0)
        self.assertEqual(len(self.events("AntigravityPublicWindow")), 0)

    def test_unknown_public_unicode_does_not_break_native_pipeline(self):
        self.add("USER_INPUT", "request")
        self.invoke()
        self.add("PLANNER_RESPONSE", "invalid-public-\ud800")
        self.assertEqual(self.post(), {})
        self.assertFalse(self.coverage()["available"])
        self.assertEqual(len(self.events("PublicMessageSegment")), 0)

    def test_old_four_event_installation_contract_stays_readable(self):
        from agent_learning.install_update import antigravity_hooks
        old = {"agent-learning": {"PreInvocation": [{"type": "command", "command": "old"}],
            "Stop": [{"type": "command", "command": "old"}],
            "PreToolUse": [{"hooks": [{"type": "command", "command": "old"}]}],
            "PostToolUse": [{"hooks": [{"type": "command", "command": "old"}]}]}}
        with patch("agent_learning.install_update._json", return_value=old):
            result = antigravity_hooks(self.root, "python", "observe", lambda *a, **k: ("new", "new"),
                                       str(self.workspace), str(self.root / "home"))
        self.assertEqual(set(result["agent-learning"]), {"PreInvocation", "PostToolUse", "Stop"})

    def test_reader_failure_leaves_native_stop_complete_and_no_models(self):
        self.add("USER_INPUT", "request")
        self.invoke()
        self.path.write_bytes(b'{broken}\n')
        self.assertEqual(self.post(), {})
        self.assertEqual(self.coverage()["reason"], "transcript_invalid_json")
        self.assertEqual(self.r.db.execute("SELECT state FROM antigravity_receipts").fetchone()[0], "complete")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_task_change_during_read_cannot_commit_public_text(self):
        self.add("USER_INPUT", "request")
        self.invoke()
        self.add("PLANNER_RESPONSE", "must not move into a replacement task")
        original = read_public
        def change_then_return(raw):
            result = original(raw)
            self.r.task_update(self.p, self.c, "replacement task")
            return result
        with patch("agent_learning.antigravity_transcript.read_public", side_effect=change_then_return):
            with self.assertRaisesRegex(ContractError, "native task changed"):
                self.post()
        self.assertEqual(len(self.events("PublicMessageSegment")), 0)

    def test_pending_post_is_not_replayed(self):
        self.normal()
        self.r.db.execute("UPDATE antigravity_receipts SET state='pending' WHERE kind='PostInvocation'")
        with patch("agent_learning.antigravity_transcript.read_public", side_effect=AssertionError("pending I/O")):
            self.post()
        self.assertEqual(len(self.events("PublicMessageSegment")), 1)

    def test_long_public_reply_keeps_progressive_segments_and_no_private_fields(self):
        self.add("USER_INPUT", "request")
        self.invoke()
        self.add("PLANNER_RESPONSE", "Outbox delivery report " * 200, thinking="PRIVATE-THOUGHT", password="PRIVATE-PASSWORD")
        self.post()
        rows = self.events("PublicMessageSegment")
        self.assertGreater(len(rows), 1)
        self.assertNotIn("PRIVATE", str([row["payload"] for row in self.events("AntigravityPublicResponse") + rows]))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_native_no_tool_call_alias_only_with_explicit_complete_public_text(self):
        self.stop(0, terminationReason="NO_TOOL_CALL", finalModelOutput="Explicit public delivery")
        self.assertEqual(json.loads(self.events("Stop")[0]["payload"])["completion"], "complete")

    def test_public_reader_requires_matching_completed_pre_before_file_io(self):
        self.last_pre = {"invocationNum": 0, "initialNumSteps": 1}
        with patch("agent_learning.antigravity_transcript.read_public", side_effect=AssertionError("orphan I/O")):
            self.post()
        self.assertEqual(self.coverage()["reason"], "transcript_window_unbound")


if __name__ == "__main__":
    unittest.main()
