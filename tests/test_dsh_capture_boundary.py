"""Native capture receipts and frozen attribution; no models or real databases."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from agent_learning.dsh import DshAdapter, reset_session
from agent_learning.runtime import Runtime


class DshCaptureBoundaryTests(unittest.TestCase):
    def setUp(self):
        local = Path(os.environ.get("ALP_TEST_ROOT", Path(__file__).resolve().parents[1] / "build/local/opt1431/dsh"))
        (local / "tmp").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="capture-boundary-", dir=local / "tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.env = patch.dict(os.environ, {"ALP_DSH_REVIEW_PROFILE": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.r = Runtime(self.root / "home", config={}, host="dsh")
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.c = self.r.host_conversation("capture-chat")
        self.a = DshAdapter(self.r, self.workspace)
        self.instance = uuid.uuid4().hex
        self.start = self.a.handle("SessionStart", self.raw(source="resume"))
        self.trajectory = self.start["dsh_trajectory"]

    def raw(self, **values):
        result = {"sessionId": "capture-chat", "dsh_bridge_instance": self.instance}
        if hasattr(self, "trajectory"):
            result["dsh_trajectory"] = dict(self.trajectory)
        result.update(values)
        return result

    def boundary(self):
        return self.a.handle("PreToolUse", self.raw(callId=str(uuid.uuid4()), name="read", arguments={}))[
            "dsh_capture_boundary"]

    def stop(self, expected=None, **values):
        result = self.raw(turn=1, last_assistant_message="Completed public result.",
                          dsh_capture_id=str(uuid.uuid4()), **values)
        if expected is not None:
            result["dsh_capture_expected"] = expected
        return result

    def count(self, kind):
        return self.r.db.execute("SELECT COUNT(*) FROM events WHERE project=? AND type=?", (self.p, kind)).fetchone()[0]

    def new_task(self, goal="repair the implementation"):
        current = self.r.task(self.p, self.c)
        return self.r.task_update(self.p, self.c, goal, new_task=True,
                                 expected_task_id=current["id"] if current else None,
                                 expected_write_version=current["write_version"] if current else None)

    def test_completed_receipt_returns_original_ack_after_task_changed(self):
        self.new_task()
        raw = self.stop(self.boundary())
        original = self.a.handle("Stop", raw)
        self.new_task("a different task")
        before = self.r.db.total_changes
        replay = self.a.handle("Stop", {**raw, "dsh_capture_replay": True})
        self.assertEqual(replay, original)
        self.assertEqual(self.count("Stop"), 1)
        self.assertEqual(self.r.db.total_changes, before, "a complete receipt cannot repeat capture writes")

    def test_failed_before_event_write_retries_against_same_boundary(self):
        self.new_task()
        raw = self.stop(self.boundary())
        with patch.object(self.r, "handle_hook", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(OSError):
                self.a.handle("Stop", raw)
        self.assertEqual(self.count("Stop"), 0)
        result = self.a.handle("Stop", {**raw, "dsh_capture_replay": True})
        self.assertEqual(result["dsh_capture_ack"], raw["dsh_capture_id"])
        self.assertEqual(self.count("Stop"), 1)

    def test_failed_before_receipt_exists_can_retry_verified_boundary(self):
        raw = self.stop(self.boundary())
        result = self.a.handle("Stop", {**raw, "dsh_capture_replay": True})
        self.assertEqual(result["dsh_capture_ack"], raw["dsh_capture_id"])
        self.assertEqual(self.count("Stop"), 1)

    def test_old_stop_cannot_borrow_later_task_boundary(self):
        self.new_task()
        raw = self.stop(self.boundary())
        with patch.object(self.r, "handle_hook", side_effect=OSError("not saved")):
            with self.assertRaises(OSError):
                self.a.handle("Stop", raw)
        self.new_task("later task")
        for candidate in (raw, {**raw, "dsh_capture_replay": True}):
            result = self.a.handle("Stop", candidate)
            self.assertTrue(result["dsh_capture_rejected"])
            self.assertEqual(self.count("Stop"), 0)

    def test_replayed_body_or_expected_proof_cannot_change(self):
        raw = self.stop(self.boundary())
        self.a.handle("Stop", raw)
        for changes in ({"last_assistant_message": "changed"}, {"dsh_capture_expected": "0" * 64}):
            result = self.a.handle("Stop", {**raw, **changes, "dsh_capture_replay": True})
            self.assertTrue(result["dsh_capture_rejected"])
            self.assertIn("identity_conflict", result["systemMessage"])
        self.assertEqual(self.count("Stop"), 1)

    def test_first_task_update_post_is_not_rejected_by_expected_old_boundary(self):
        active = self.new_task()
        handle = self.start["hookSpecificOutput"]["additionalContext"].split("context_handle=")[1].split(";")[0]
        arguments = {"context_handle": handle}
        pre = self.a.handle("PreToolUse", self.raw(callId="task-update", name="task_update", arguments=arguments))
        self.r.task_update(self.p, self.c, active["goal"], status="delivered",
                           expected_task_id=active["id"], expected_write_version=active["write_version"])
        raw = self.raw(callId="task-update", name="task_update", arguments=arguments, result={"isError": False},
                       dsh_capture_id=str(uuid.uuid4()), dsh_capture_expected=pre["dsh_capture_boundary"])
        result = self.a.handle("PostToolUse", raw)
        self.assertEqual(result["dsh_capture_ack"], raw["dsh_capture_id"])
        self.assertNotEqual(result["dsh_capture_boundary"], pre["dsh_capture_boundary"])

    def test_late_post_uses_exact_pre_operation_after_new_task(self):
        original = self.new_task()
        pre = self.a.handle("PreToolUse", self.raw(callId="verify", name="read", arguments={}))
        raw = self.raw(callId="verify", name="read", arguments={}, result={"success": True},
                       dsh_capture_id=str(uuid.uuid4()), dsh_capture_expected=pre["dsh_capture_boundary"])
        self.new_task("later task")
        result = self.a.handle("PostToolUse", {**raw, "dsh_capture_replay": True})
        self.assertEqual(result["dsh_capture_ack"], raw["dsh_capture_id"])
        event = self.r.db.execute("SELECT task_id FROM events WHERE type='PostToolUse'").fetchone()
        self.assertEqual(event["task_id"], original["id"])

    def test_unbound_post_cannot_use_old_task_proof(self):
        self.new_task()
        old = self.boundary()
        self.new_task("later task")
        result = self.a.handle("PostToolUse", self.raw(callId="never-started", name="read", result={"success": True},
            dsh_capture_id=str(uuid.uuid4()), dsh_capture_expected=old, dsh_capture_replay=True))
        self.assertTrue(result["dsh_capture_rejected"])
        self.assertEqual(self.count("PostToolUse"), 0)

    def test_unknown_receipt_without_proof_cannot_be_replayed(self):
        result = self.a.handle("Stop", {**self.stop(), "dsh_capture_replay": True})
        self.assertTrue(result["dsh_capture_rejected"])
        self.assertEqual(self.count("Stop"), 0)

    def test_child_stop_cannot_borrow_a_reassigned_child_boundary(self):
        self.new_task()
        child_instance = uuid.uuid4().hex
        child = self.a.handle("SubagentStart", {"sessionId": "capture-chat", "agentId": "child",
            "dsh_bridge_instance": child_instance, "prompt": "delegated work"})
        old = child["dsh_capture_boundary"]
        with self.r.transaction():
            self.r._bind_assignment(self.p, self.c, "child", uuid.uuid4().hex)
        raw = {"sessionId": "capture-chat", "agentId": "child", "dsh_bridge_instance": child_instance,
            "dsh_trajectory": child["dsh_trajectory"], "turn": 1, "last_assistant_message": "child result",
            "dsh_capture_id": str(uuid.uuid4()), "dsh_capture_expected": old, "dsh_capture_replay": True}
        result = self.a.handle("SubagentStop", raw)
        self.assertTrue(result["dsh_capture_rejected"])
        self.assertEqual(self.count("SubagentStop"), 0)

    def test_completed_receipt_cannot_cross_reset_or_host_epoch(self):
        raw = self.stop(self.boundary())
        self.a.handle("Stop", raw)
        reset_session(self.r, self.workspace, "capture-chat")
        result = self.a.handle("Stop", {**raw, "dsh_capture_replay": True})
        self.assertTrue(result["dsh_reconnect_required"])
        self.assertEqual(self.count("Stop"), 1)

    def test_legacy_producers_keep_original_empty_stop_response(self):
        result = self.a.handle("Stop", {"sessionId": "legacy", "turn": 1})
        self.assertEqual(result, {})

    def test_proof_and_receipt_fields_never_enter_public_evidence(self):
        raw = self.stop(self.boundary())
        self.a.handle("Stop", raw)
        payload = self.r.db.execute("SELECT payload FROM events WHERE type='Stop'").fetchone()[0]
        self.assertNotIn("dsh_capture", payload)
        self.assertNotIn(raw["dsh_capture_expected"], payload)
        self.assertNotIn(raw["dsh_capture_id"], payload)

    def test_malformed_protocol_fields_are_rejected_without_capture(self):
        for changes in ({"dsh_capture_id": 3}, {"dsh_capture_id": "bad"},
                        {"dsh_capture_replay": 1}, {"dsh_capture_expected": {}},
                        {"dsh_capture_expected": "not-a-proof"}):
            result = self.a.handle("Stop", {**self.stop(), **changes})
            self.assertTrue(result["dsh_capture_rejected"])
        self.assertEqual(self.count("Stop"), 0)


if __name__ == "__main__":
    unittest.main()
