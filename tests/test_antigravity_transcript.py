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
    TRANSCRIPT_FILENAME = "transcript.jsonl"

    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "build" / "local" / "fix1427" / "transcript-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.artifact = self.root / "brain" / "chat"
        self.path = self.artifact / ".system_generated" / "logs" / self.TRANSCRIPT_FILENAME
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

    def input_metadata(self):
        return json.loads(self.events("AntigravityPublicInput")[-1]["payload"])

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

    def test_native_1_4_7_windows_allow_only_completed_system_ephemeral_steps(self):
        self.add("USER_INPUT", "public task")
        initials = []
        for invocation in range(3):
            initial = len(self.rows)
            initials.append(initial)
            self.add("EPHEMERAL_MESSAGE", "EXCLUDED-SYSTEM-TEXT", source="SYSTEM_SDK")
            self.invoke(invocation, initialNumSteps=initial)
            self.assertEqual(self.input_metadata()["public_window"],
                             {"available": True, "reason": "bound", "trailing_system_steps": 1})
            self.add("PLANNER_RESPONSE", "public result" if invocation == 2 else None, thinking="EXCLUDED-THOUGHT")
            self.post()
            if invocation != 2:
                self.add("GENERIC", "EXCLUDED-TOOL-TEXT")
        self.assertEqual(initials, [1, 4, 7])
        self.assertEqual(len(self.events("AntigravityPublicInput")), 3)
        self.assertEqual(len(self.events("AntigravityPublicWindow")), 3)
        self.assertEqual(len(self.events("UserPromptSubmit")), 1)
        self.assertEqual(len(self.events("PublicMessageSegment")), 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_phases").fetchone()[0], 0)
        metadata = str([row["payload"] for row in self.events("AntigravityPublicInput")])
        self.assertNotIn("public task", metadata)
        self.assertNotIn("EXCLUDED", metadata)
        self.assertNotIn(str(self.path), metadata)

    def test_multiple_trailing_system_steps_remain_bounded_public_window(self):
        self.add("USER_INPUT", "public task")
        self.add("EPHEMERAL_MESSAGE", "EXCLUDED-ONE", source="SYSTEM_SDK")
        self.add("EPHEMERAL_MESSAGE", "EXCLUDED-TWO", source="SYSTEM_SDK")
        self.invoke(initialNumSteps=1)
        self.assertEqual(self.input_metadata()["public_window"]["trailing_system_steps"], 2)
        self.add("PLANNER_RESPONSE", "public response")
        self.post()
        self.assertTrue(self.coverage()["available"])

    def test_ahead_user_model_tool_unknown_or_non_done_system_steps_are_rejected(self):
        variants = [("USER_INPUT", "USER_EXPLICIT", "DONE"), ("PLANNER_RESPONSE", "MODEL", "DONE"),
                    ("GENERIC", "MODEL", "DONE"), ("UNKNOWN", "SYSTEM_SDK", "DONE"),
                    ("EPHEMERAL_MESSAGE", "MODEL", "DONE"),
                    ("EPHEMERAL_MESSAGE", "SYSTEM_SDK", "RUNNING")]
        self.add("USER_INPUT", "original public task")
        for number, (kind, source, status) in enumerate(variants):
            with self.subTest(kind=kind, source=source, status=status):
                self.rows = self.rows[:1]
                self.add(kind, "must not be borrowed", source=source, status=status)
                self.invoke(number, initialNumSteps=1)
                self.assertEqual(self.input_metadata()["public_window"],
                    {"available": False, "reason": "transcript_ahead_of_invocation"})
        self.assertEqual(len(self.events("AntigravityPublicWindow")), 0)
        self.assertEqual(len(self.events("UserPromptSubmit")), 0)

    def test_pre_failure_diagnostics_keep_native_prompt_priority_and_no_text(self):
        self.add("USER_INPUT", "TRANSCRIPT-PUBLIC-INPUT")
        self.invoke(lastUserInput="NATIVE-DIFFERENT-INPUT")
        metadata = self.input_metadata()
        self.assertTrue(metadata["capture_coverage"]["lastUserInput"]["available"])
        self.assertEqual(metadata["public_window"]["reason"], "transcript_native_prompt_mismatch")
        self.assertNotIn("INPUT", json.dumps(metadata))
        self.invoke(1, lastUserInput="  ")
        self.assertEqual(self.input_metadata()["public_window"]["reason"], "native_prompt_empty")
        self.invoke(2, lastUserInput={"text": "UNSUPPORTED-INPUT"})
        self.assertEqual(self.input_metadata()["public_window"]["reason"], "native_prompt_unsupported_type")
        self.invoke(3, artifactDirectoryPath="")
        self.assertEqual(self.input_metadata()["public_window"]["reason"], "transcript_path_missing")
        with patch("agent_learning.antigravity_transcript.read_public",
                   return_value={"available": False, "reason": "transcript_read_deadline"}):
            self.invoke(4)
        self.assertEqual(self.input_metadata()["public_window"]["reason"], "transcript_read_deadline")
        self.assertEqual(len(self.events("UserPromptSubmit")), 1)

    def test_old_complete_and_pending_pre_receipts_do_not_backfill_input_diagnostics(self):
        self.add("USER_INPUT", "public task")
        self.invoke()
        self.r.db.execute("DELETE FROM events WHERE type='AntigravityPublicInput'")
        with patch("agent_learning.antigravity_transcript.read_public", side_effect=AssertionError("no replay I/O")), \
             patch.object(self.adapter, "_capture_prompt") as capture, patch.object(self.r, "_context") as context:
            self.invoke()
            self.r.db.execute("UPDATE antigravity_receipts SET state='pending' WHERE kind='PreInvocation'")
            self.invoke()
        capture.assert_not_called()
        context.assert_not_called()
        self.assertEqual(len(self.events("AntigravityPublicInput")), 0)

    def test_multimount_pre_reports_isolation_without_private_payload(self):
        other = self.root / "other"
        other.mkdir()
        self.invoke(workspacePaths=[str(self.workspace), str(other)])
        metadata = self.input_metadata()
        self.assertEqual(metadata["public_window"]["reason"], "workspace_association_unknown")
        self.assertNotIn(str(other), json.dumps(metadata))

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

    def test_every_invocation_excludes_its_single_saved_prompt(self):
        self.add("USER_INPUT", "saturnringdiagnostic")
        with patch.object(self.r, "_context", wraps=self.r._context) as context:
            for number in range(11):
                with self.subTest(invocation=number):
                    output = self.invoke(number)
                    source, = self.events("UserPromptSubmit")
                    memory, = self.r.db.execute(
                        "SELECT id,body FROM work_memories WHERE project=? AND event_id=?",
                        (self.p, source["id"])).fetchall()
                    self.assertEqual(json.loads(memory["body"])["type"], "user_request")
                    self.assertEqual(context.call_args.kwargs["_source_event_id"], source["id"])
                    self.assertNotIn(memory["id"], json.dumps(output))
                    self.assertNotIn("[工作历史]", json.dumps(output, ensure_ascii=False))
                    self.assertTrue(self.input_metadata()["public_window"]["available"])
                    self.add("PLANNER_RESPONSE", tool_calls=[{"name": "synthetic_read_only"}])
                    tool_raw = self.raw(stepIdx=len(self.rows), toolCall={"name": "view_file",
                        "args": {"AbsolutePath": str(self.workspace / "synthetic-read-only.txt")}})
                    self.adapter.handle("PreToolUse", tool_raw)
                    self.adapter.handle("PostToolUse", {**tool_raw, "error": ""})
                    self.add("GENERIC", "synthetic read-only tool result")
                    self.post()
                    self.assertEqual(self.coverage()["reason"], "transcript_final_public_missing")
        self.assertEqual(len(self.events("AntigravityPublicInput")), 11)
        self.assertEqual(len(self.events("AntigravityPublicResponse")), 11)
        self.assertEqual(len(self.events("PostToolUse")), 11)
        self.assertTrue(all(row["outcome"] == "call_completed" for row in self.events("PostToolUse")))
        self.assertTrue(self.r.work_search(self.p, "saturnringdiagnostic")["items"])

    def test_current_prompt_exclusion_keeps_relevant_history(self):
        with self.r.transaction():
            prior, _ = self.r._event(self.p, self.r.host_conversation("past"), "Stop",
                {"last_assistant_message": "orbitguard: retain launch evidence for orbitguard."},
                outcome="reported")
            self.r._work_capture_event(self.p, prior["id"], self.workspace)
        self.add("USER_INPUT", "orbitguard")
        with patch.object(self.r, "_context", wraps=self.r._context) as context:
            output = self.invoke()
        source, = self.events("UserPromptSubmit")
        self.assertEqual(context.call_args.kwargs["_source_event_id"], source["id"])
        text = output["injectSteps"][0]["ephemeralMessage"]
        cards = [json.loads(line.removeprefix("[工作历史] ")) for line in text.splitlines()
                 if line.startswith("[工作历史] ")]
        self.assertTrue(cards)
        for card in cards:
            event_id = self.r.db.execute("SELECT event_id FROM work_memories WHERE project=? AND id=?",
                (self.p, card["memory_id"])).fetchone()[0]
            self.assertEqual(event_id, prior["id"])

    def test_long_prompt_segments_are_excluded_with_real_fault_query(self):
        self.add("USER_INPUT", "不对，segmentfence 应保留原始证据。" * 150)
        self.r.db.execute("INSERT INTO antigravity_faults VALUES(?,?,?,?,?,'pending',?)",
            (self.p, self.c, "controlled-segment-fault", self.r.session_epoch(), "segmentfence", 1.0))
        with patch.object(self.r, "_context", wraps=self.r._context) as context:
            output = self.invoke()
        source, = self.events("UserPromptSubmit")
        own = self.r.db.execute(
            "SELECT id FROM work_memories WHERE project=? AND (event_id=? OR "
            "json_extract(body,'$.public_message_id')=?)",
            (self.p, source["id"], source["id"])).fetchall()
        self.assertGreater(len(own), 2)
        self.assertEqual(context.call_args.kwargs["_source_event_id"], source["id"])
        self.assertEqual(context.call_args.kwargs["query"], "segmentfence")
        self.assertIs(context.call_args.kwargs["_query_available"], True)
        for memory in own:
            self.assertNotIn(memory["id"], json.dumps(output))
        self.assertNotIn("[工作历史]", json.dumps(output, ensure_ascii=False))
        self.assertTrue(self.r.work_search(self.p, "segmentfence")["items"])

    def test_same_correction_after_stop_keeps_prior_source_and_excludes_current(self):
        text = "不对，signingcompat 应保留已发布应用签名。"
        self.add("USER_INPUT", text)
        self.invoke()
        first, = self.events("UserPromptSubmit")
        self.stop()
        self.add("USER_INPUT", text)
        with patch.object(self.r, "_context", wraps=self.r._context) as context:
            output = self.invoke(1)
        sources = self.events("UserPromptSubmit")
        self.assertEqual(len(sources), 2)
        current = sources[-1]
        self.assertNotEqual(current["id"], first["id"])
        self.assertEqual(context.call_args.kwargs["_source_event_id"], current["id"])
        memories = {row["event_id"]: row["id"] for row in self.r.db.execute(
            "SELECT event_id,id FROM work_memories WHERE project=?", (self.p,))}
        self.assertIn(memories[first["id"]], json.dumps(output))
        self.assertNotIn(memories[current["id"]], json.dumps(output))

    def test_legacy_native_prompt_reuse_returns_its_canonical_source(self):
        original = self.r._event
        def legacy(project, conversation, kind, payload, event_id=None, **kwargs):
            if (isinstance(event_id, list) and event_id[0] == "antigravity-public-prompt"
                    and event_id[1] == conversation):
                event_id = [event_id[0], *event_id[2:]]
            return original(project, conversation, kind, payload, event_id=event_id, **kwargs)
        with patch.object(self.r, "_event", side_effect=legacy):
            self.invoke(lastUserInput="legacyfence")
        source, = self.events("UserPromptSubmit")
        with patch.object(self.r, "_context", wraps=self.r._context) as context:
            output = self.invoke(1, lastUserInput="legacyfence")
        self.assertEqual(len(self.events("UserPromptSubmit")), 1)
        self.assertEqual(context.call_args.kwargs["_source_event_id"], source["id"])
        memory = self.r.db.execute("SELECT id FROM work_memories WHERE project=? AND event_id=?",
                                  (self.p, source["id"])).fetchone()[0]
        self.assertNotIn(memory, json.dumps(output))

    def test_same_native_prompt_in_another_chat_keeps_its_own_source(self):
        self.invoke(lastUserInput="samechatfence")
        first, = self.events("UserPromptSubmit")
        with patch.object(self.r, "_context", wraps=self.r._context) as context:
            output = self.adapter.handle("PreInvocation", self.raw(conversationId="another-chat",
                invocationNum=0, initialNumSteps=0, lastUserInput="samechatfence"))
        first, current = self.events("UserPromptSubmit")
        self.assertNotEqual(first["conversation"], current["conversation"])
        self.assertEqual(context.call_args.kwargs["_source_event_id"], current["id"])
        memories = {row["event_id"]: row["id"] for row in self.r.db.execute(
            "SELECT event_id,id FROM work_memories WHERE project=?", (self.p,))}
        self.assertIn(memories[first["id"]], json.dumps(output))
        self.assertNotIn(memories[current["id"]], json.dumps(output))

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
        source, = self.events("UserPromptSubmit")
        self.r.task_update(self.p, self.c, "current task")
        self.add("GENERIC", "observable tool result")
        with patch.object(self.r, "_context", wraps=self.r._context) as context:
            self.invoke(1)
        self.assertEqual(context.call_args.kwargs["_source_event_id"], source["id"])
        current_source, = self.events("UserPromptSubmit")
        self.assertEqual(dict(current_source), dict(source), "a reused input must not be rebound to a new task")
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

    def sibling(self):
        name = "transcript_full.jsonl" if self.TRANSCRIPT_FILENAME == "transcript.jsonl" else "transcript.jsonl"
        return self.path.with_name(name)

    def test_only_explicit_supported_path_is_read_without_sibling_fallback(self):
        self.add("USER_INPUT", "public request")
        other = self.sibling()
        other.write_bytes(self.path.read_bytes())
        for name in ("transcript_private.jsonl", "transcript_full.jsonl.bak", "other.jsonl"):
            with self.subTest(name=name), patch.object(Path, "open", side_effect=AssertionError("unrelated I/O")):
                result = read_public(self.raw(transcriptPath=str(self.path.with_name(name))))
            self.assertEqual(result["reason"], "transcript_path_invalid")
        self.path.unlink()
        self.assertEqual(read_public(self.raw())["reason"], "transcript_unavailable")

    def test_uri_unc_and_parent_traversal_are_rejected_before_io(self):
        self.add("USER_INPUT", "public request")
        for fields in ({"transcriptPath": self.path.as_uri()},
                       {"artifactDirectoryPath": self.artifact.as_uri()},
                       {"transcriptPath": r"\\server\share\brain\chat\.system_generated\logs" + "\\" + self.path.name},
                       {"transcriptPath": "//server/share/brain/chat/.system_generated/logs/" + self.path.name},
                       {"transcriptPath": str(self.path.parent / ".." / "logs" / self.path.name)}):
            with self.subTest(fields=fields), patch.object(Path, "open", side_effect=AssertionError("invalid I/O")):
                result = read_public(self.raw(**fields))
            self.assertEqual(result["reason"], "transcript_path_invalid")

    def test_parent_reparse_point_is_rejected(self):
        from types import SimpleNamespace

        self.add("USER_INPUT", "public request")
        original = Path.stat
        def metadata(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path == self.path.parent:
                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
            return value
        with patch.object(Path, "stat", metadata):
            self.assertEqual(read_public(self.raw())["reason"], "transcript_path_redirected")

    def test_pre_post_cannot_switch_between_compact_and_full_files(self):
        self.add("USER_INPUT", "public request")
        self.invoke()
        self.add("PLANNER_RESPONSE", "public response")
        other = self.sibling()
        other.write_bytes(self.path.read_bytes())
        self.post(transcriptPath=str(other))
        self.assertEqual(self.coverage()["reason"], "transcript_file_changed")
        self.assertEqual(len(self.events("PublicMessageSegment")), 0)

    def test_replaced_transcript_cannot_borrow_pre_file_identity(self):
        self.add("USER_INPUT", "public request")
        self.invoke()
        self.add("PLANNER_RESPONSE", "public response")
        replacement = self.path.with_suffix(".replacement")
        replacement.write_bytes(self.path.read_bytes())
        replacement.replace(self.path)
        self.post()
        self.assertEqual(self.coverage()["reason"], "transcript_file_changed")
        self.assertEqual(len(self.events("PublicMessageSegment")), 0)

    def test_record_budget_remains_a_capture_gap(self):
        self.add("USER_INPUT", "public request")
        self.add("PLANNER_RESPONSE", "public response")
        with patch("agent_learning.antigravity_transcript.MAX_RECORDS", 1):
            self.assertEqual(read_public(self.raw())["reason"], "transcript_scan_budget")

    def test_missing_stop_body_keeps_input_and_response_without_completing_delivery(self):
        from agent_learning.antigravity_health import capture_status

        self.normal()
        status = capture_status(self.r, self.p)
        self.assertEqual(status["input_window"]["public_input"], "available")
        self.assertTrue(status["input_window"]["available"])
        self.assertEqual(status["public_response"], "available")
        self.assertEqual(status["public_delivery"], "unavailable")
        self.assertEqual(status["reason"], "public_output_missing")
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(json.loads(self.events("PublicMessageSegment")[0]["payload"])["completion"], "partial")
        for table in ("deliveries", "review_phases", "admin_calls"):
            self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)

    def test_delayed_stop_after_another_invocation_never_promotes_its_response(self):
        self.add("USER_INPUT", "first public request")
        self.invoke()
        self.add("PLANNER_RESPONSE", "first public response")
        self.post()
        self.add("USER_INPUT", "second public request")
        self.invoke(1)
        self.add("PLANNER_RESPONSE", "second public response")
        self.post()
        with patch("agent_learning.antigravity_transcript.read_public", side_effect=AssertionError("no Stop I/O")), \
             patch.object(self.r, "_capture_delivery") as capture:
            self.stop(0)
            self.stop(0)
        capture.assert_not_called()
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(len(self.events("AntigravityStop")), 1)
        self.assertEqual(len(self.events("PublicMessageSegment")), 2)
        self.assertTrue(all(json.loads(row["payload"])["completion"] == "partial"
                            for row in self.events("PublicMessageSegment")))

    def test_missing_public_prompt_does_not_supply_context_query(self):
        with patch.object(self.r, "_context", return_value="") as context:
            self.invoke()
        self.assertEqual(context.call_args.kwargs["query"], "")
        self.assertIs(context.call_args.kwargs["_query_available"], False)
        self.assertIsNone(context.call_args.kwargs["_source_event_id"])
        self.assertEqual(len(self.events("UserPromptSubmit")), 0)

    def test_partial_continue_is_retained_but_cannot_reuse_prior_context_query(self):
        self.invoke(lastUserInput="current sqlite outbox business question")
        self.add("USER_INPUT", "继续", truncated_fields=["content"])
        with patch.object(self.r, "_context", wraps=self.r._context) as context:
            self.invoke(1)
        self.assertEqual(context.call_args.kwargs["query"], "")
        self.assertIs(context.call_args.kwargs["_query_available"], False)
        source = self.events("UserPromptSubmit")[-1]
        self.assertEqual(context.call_args.kwargs["_source_event_id"], source["id"])
        payload = json.loads(source["payload"])
        self.assertEqual(payload["prompt"], "继续")
        self.assertTrue(payload["capture_coverage"]["lastUserInput"]["truncated"])

    def test_complete_native_prompt_remains_query_when_transcript_window_unbound(self):
        self.add("USER_INPUT", "a different transcript request")
        with patch.object(self.r, "_context", return_value="") as context:
            self.invoke(lastUserInput="native sqlite outbox request")
        self.assertEqual(self.input_metadata()["public_window"]["reason"], "transcript_native_prompt_mismatch")
        self.assertEqual(context.call_args.kwargs["query"], "native sqlite outbox request")
        self.assertIs(context.call_args.kwargs["_query_available"], True)

    def test_complete_transcript_prompt_supplies_context_query(self):
        self.add("USER_INPUT", "current sqlite outbox request")
        with patch.object(self.r, "_context", return_value="") as context:
            self.invoke()
        self.assertEqual(context.call_args.kwargs["query"], "current sqlite outbox request")
        self.assertIs(context.call_args.kwargs["_query_available"], True)

    def test_real_fault_query_has_priority_when_public_prompt_missing_or_partial(self):
        for number, partial in enumerate((False, True)):
            with self.subTest(partial=partial):
                if partial:
                    self.add("USER_INPUT", "继续", truncated_fields=["content"])
                self.r.db.execute("INSERT INTO antigravity_faults VALUES(?,?,?,?,?,'pending',?)",
                    (self.p, self.c, "controlled-fault-" + str(number), self.r.session_epoch(),
                     "sqlite database is locked", 1.0))
                with patch.object(self.r, "_context", return_value="") as context:
                    self.invoke(number)
                self.assertEqual(context.call_args.kwargs["query"], "sqlite database is locked")
                self.assertIs(context.call_args.kwargs["_query_available"], True)
                source = self.events("UserPromptSubmit")[-1] if partial else None
                self.assertEqual(context.call_args.kwargs["_source_event_id"], source["id"] if source else None)
                coverage = self.input_metadata()["capture_coverage"]["lastUserInput"]
                self.assertTrue(coverage["truncated"] if partial else not coverage["available"])

    def test_bounded_native_prompt_is_retained_without_context_query(self):
        from agent_learning.antigravity import PUBLIC_TEXT_MAX_CHARS

        with patch.object(self.r, "_context", return_value="") as context:
            self.invoke(lastUserInput="continue sqlite outbox " * PUBLIC_TEXT_MAX_CHARS)
        self.assertEqual(context.call_args.kwargs["query"], "")
        self.assertIs(context.call_args.kwargs["_query_available"], False)
        payload = json.loads(self.events("UserPromptSubmit")[0]["payload"])
        self.assertTrue(payload["capture_coverage"]["lastUserInput"]["truncated"])
        self.assertLessEqual(len(payload["prompt"]), PUBLIC_TEXT_MAX_CHARS)

    def test_unassociated_native_prompt_does_not_keep_query_available(self):
        other = self.root / "other-workspace"
        other.mkdir()
        with patch.object(self.r, "_context", return_value="") as context:
            self.invoke(lastUserInput="native public request",
                        workspacePaths=[str(self.workspace), str(other)])
        self.assertEqual(context.call_args.kwargs["query"], "")
        self.assertIs(context.call_args.kwargs["_query_available"], False)
        self.assertIsNone(context.call_args.kwargs["_source_event_id"])
        self.r.db.execute("UPDATE antigravity_trajectories SET ambiguous=1 WHERE project=? AND conversation=?",
                          (self.p, self.c))
        with patch.object(self.r, "_context", return_value="") as context:
            self.invoke(1, lastUserInput="native public request")
        self.assertEqual(context.call_args.kwargs["query"], "")
        self.assertIs(context.call_args.kwargs["_query_available"], False)
        self.assertIsNone(context.call_args.kwargs["_source_event_id"])


class AntigravityFullTranscriptTests(AntigravityTranscriptTests):
    """The same public-field, identity, budget and replay contract applies to full JSONL."""

    TRANSCRIPT_FILENAME = "transcript_full.jsonl"


if __name__ == "__main__":
    unittest.main()
