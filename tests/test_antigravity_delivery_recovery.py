"""Terminal public delivery needs fresh native state and the same proven window."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.antigravity import AntigravityAdapter, reset_session
from agent_learning.antigravity_health import capture_status
from agent_learning.runtime import Runtime


class AntigravityDeliveryRecoveryTests(unittest.TestCase):
    def setUp(self):
        area = tempfile.TemporaryDirectory(prefix="alp-ag-delivery-")
        self.addCleanup(area.cleanup)
        self.root = Path(area.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.artifact = self.root / "brain" / "chat"
        logs = self.artifact / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        self.path = logs / "transcript_full.jsonl"
        self.rows = []
        self.r = Runtime(self.root / "home", host="antigravity", config={
            "admin": {"enabled": False}, "scheduler": {"enabled": False},
            "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.c = self.r.host_conversation("chat")
        self.adapter = AntigravityAdapter(self.r, self.workspace)

    def raw(self, **extra):
        return {"conversationId": "chat", "workspacePaths": [str(self.workspace)],
                "transcriptPath": str(self.path), "artifactDirectoryPath": str(self.artifact), **extra}

    def write(self):
        self.path.write_text("".join(json.dumps(row) + "\n" for row in self.rows), encoding="utf-8")

    def add(self, kind, content=None, **extra):
        row = {"step_index": len(self.rows), "type": kind, "status": "DONE",
               "source": "USER_EXPLICIT" if kind == "USER_INPUT" else "MODEL", **extra}
        if content is not None:
            row["content"] = content
        self.rows.append(row)
        self.write()

    def invoke(self, number=0):
        self.pre = {"invocationNum": number, "initialNumSteps": len(self.rows)}
        return self.adapter.handle("PreInvocation", self.raw(**self.pre))

    def post(self):
        return self.adapter.handle("PostInvocation", self.raw(**self.pre))

    def pair(self, request="current public request", response="current public response", number=0):
        self.add("USER_INPUT", request)
        self.invoke(number)
        self.add("PLANNER_RESPONSE", response)
        self.post()

    def stop(self, number=0, **extra):
        return self.adapter.handle("Stop", self.raw(executionNum=number, fullyIdle=True,
            terminationReason="model_stop", **extra))

    def events(self, kind):
        return self.r.db.execute("SELECT * FROM events WHERE type=? ORDER BY created,id", (kind,)).fetchall()

    def reason(self):
        return capture_status(self.r, self.p).get("delivery_recovery_reason")

    def test_fresh_terminal_stop_reads_public_file_and_is_idempotent(self):
        self.pair()
        self.assertEqual(capture_status(self.r, self.p)["public_delivery"], "unknown")
        self.stop()
        self.assertEqual(capture_status(self.r, self.p)["public_delivery"], "complete")
        self.assertEqual(self.reason(), "stop_public_output_recovered")
        self.assertEqual(len(self.events("Stop")), 1)
        self.assertEqual(json.loads(self.events("Stop")[0]["payload"])["proven_public_response_event"],
                         self.events("AntigravityPublicResponse")[0]["id"])
        before = [tuple(row) for row in self.events("Stop")]
        with patch("agent_learning.antigravity_transcript.read_public", side_effect=AssertionError("replay read")):
            self.stop()
        self.assertEqual([tuple(row) for row in self.events("Stop")], before)

    def test_post_alone_remains_partial_and_cannot_complete_a_phase(self):
        self.pair()
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_phases").fetchone()[0], 0)
        self.assertTrue(all(json.loads(row["payload"])["completion"] == "partial"
                            for row in self.events("PublicMessageSegment")))

    def test_explicit_delivered_task_can_capture_its_complete_public_delivery(self):
        self.r.task_update(self.p, self.c, "current explicit task", new_task=True)
        self.r.task_update(self.p, self.c, "current explicit task", status="delivered")
        self.pair()
        self.stop()
        self.assertEqual(len(self.events("Stop")), 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_unknown_or_tool_tail_prevents_terminal_recovery(self):
        self.pair()
        self.add("GENERIC", "must not become public delivery")
        self.stop()
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(self.reason(), "stop_transcript_not_terminal")

    def test_tool_call_tail_is_not_a_complete_public_delivery(self):
        self.pair()
        self.add("TOOL_CALL", "not public")
        self.stop()
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(self.reason(), "stop_transcript_not_terminal")

    def test_tool_result_tail_is_not_a_complete_public_delivery(self):
        self.pair()
        self.add("TOOL_RESULT", "not public")
        self.stop()
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(self.reason(), "stop_transcript_not_terminal")

    def test_truncated_post_cannot_become_complete_at_stop(self):
        self.pair(response="long public content " * 1000)
        with patch("agent_learning.antigravity_transcript.read_public", side_effect=AssertionError("partial read")):
            self.stop()
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(self.reason(), "stop_public_response_unverified")

    def test_only_done_sdk_ephemeral_tail_is_permitted(self):
        self.pair()
        self.add("EPHEMERAL_MESSAGE", "not retained", source="SYSTEM_SDK")
        self.stop()
        self.assertEqual(len(self.events("Stop")), 1)
        self.assertNotIn("not retained", self.events("Stop")[0]["payload"])

    def test_non_done_system_tail_is_not_terminal(self):
        self.pair()
        self.add("EPHEMERAL_MESSAGE", "not done", source="SYSTEM_SDK", status="RUNNING")
        self.stop()
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(self.reason(), "stop_transcript_not_terminal")

    def test_rewritten_response_does_not_borrow_saved_post_metadata(self):
        self.pair()
        self.rows[-1]["content"] = "changed public response"
        self.write()
        self.stop()
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(self.reason(), "stop_public_response_unverified")

    def test_new_user_without_stop_boundary_cannot_supply_delayed_delivery(self):
        self.pair()
        self.pair("another request", "another response", 1)
        with patch("agent_learning.antigravity_transcript.read_public", side_effect=AssertionError("unbound read")):
            self.stop()
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(self.reason(), "stop_boundary_changed")

    def test_another_stop_cannot_reuse_a_previously_completed_window(self):
        self.pair()
        self.stop()
        self.stop(1)
        self.assertEqual(len(self.events("Stop")), 1)
        self.assertEqual(self.reason(), "stop_boundary_changed")

    def test_new_execution_can_complete_a_new_public_window(self):
        self.pair()
        self.stop()
        self.pair("second request", "second response", 1)
        self.stop(1)
        self.assertEqual(len(self.events("Stop")), 2)
        self.assertIn("second response", self.events("Stop")[-1]["payload"])

    def test_multiple_invocations_keep_one_user_execution_boundary(self):
        self.pair()
        self.add("GENERIC", "old tool detail")
        self.invoke(1)
        self.add("PLANNER_RESPONSE", "final invocation response")
        self.post()
        self.stop()
        self.assertEqual(len(self.events("Stop")), 1)
        self.assertIn("final invocation response", self.events("Stop")[0]["payload"])
        self.assertNotIn("old tool detail", self.events("Stop")[0]["payload"])

    def test_input_archive_is_required_even_when_window_metadata_exists(self):
        self.pair()
        self.r.db.execute("DELETE FROM events WHERE type='UserPromptSubmit'")
        self.stop()
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(self.reason(), "stop_input_unverified")

    def test_task_change_cannot_inherit_prior_post_text(self):
        self.pair()
        self.r.task_update(self.p, self.c, "new task", new_task=True)
        self.stop()
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(self.reason(), "stop_boundary_changed")

    def test_legacy_post_missing_digest_is_not_retroactively_promoted(self):
        self.pair()
        row = self.events("AntigravityPublicResponse")[0]
        payload = json.loads(row["payload"])
        payload.pop("public_response_digest")
        self.r.db.execute("UPDATE events SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
        self.stop()
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(self.reason(), "stop_public_response_unverified")

    def test_old_complete_missing_stop_is_never_backfilled(self):
        self.pair()
        self.stop(finalModelOutput="")
        with patch("agent_learning.antigravity_transcript.read_public", side_effect=AssertionError("old Stop read")):
            self.stop()
        self.assertEqual(len(self.events("Stop")), 0)

    def test_file_change_between_recovery_and_commit_drops_recovered_text(self):
        self.pair()
        original = self.adapter._stop
        def changed(*args, **kwargs):
            self.add("USER_INPUT", "arrived before commit")
            return original(*args, **kwargs)
        with patch.object(self.adapter, "_stop", side_effect=changed):
            self.stop()
        self.assertEqual(len(self.events("Stop")), 0)
        self.assertEqual(self.reason(), "stop_transcript_changed")

    def test_explicit_native_output_has_priority_and_needs_no_transcript_read(self):
        self.pair()
        with patch("agent_learning.antigravity_transcript.read_public", side_effect=AssertionError("native priority")):
            self.stop(finalModelOutput="native final public response")
        self.assertEqual(len(self.events("Stop")), 1)
        self.assertNotIn("delivery_recovery_reason", capture_status(self.r, self.p))

    def test_prior_stop_from_old_generation_does_not_predict_new_counter(self):
        self.pair()
        self.stop(40)
        reset_session(self.r, self.workspace, "chat")
        self.pair("new generation request", "new generation response", 0)
        self.stop(0)
        self.assertEqual(len(self.events("Stop")), 2)
        self.assertEqual(self.reason(), "stop_public_output_recovered")

    def test_prior_stop_from_old_account_epoch_does_not_predict_new_counter(self):
        self.pair()
        self.stop(40)
        self.r.account_changed("fixture-account-transition")
        self.pair("new account request", "new account response", 1)
        self.stop(0)
        self.assertEqual(len(self.events("Stop")), 2)
        self.assertEqual(self.reason(), "stop_public_output_recovered")

    def test_delivery_metadata_contains_no_paths_or_unrecognized_reason_text(self):
        self.pair()
        self.stop()
        view = capture_status(self.r, self.p)
        self.assertNotIn(str(self.path), json.dumps(view))
        before = self.r.db.total_changes
        self.assertEqual(capture_status(self.r, self.p), view)
        self.assertEqual(self.r.db.total_changes, before)
        row = self.events("AntigravityStop")[0]
        payload = json.loads(row["payload"])
        payload["delivery_recovery_reason"] = "arbitrary-secret-diagnostic"
        self.r.db.execute("UPDATE events SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
        self.assertNotIn("arbitrary-secret", json.dumps(capture_status(self.r, self.p)))
