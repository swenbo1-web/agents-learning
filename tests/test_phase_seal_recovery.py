"""Original Stop compensation survives failures without borrowing later work."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.phase_review import configure, prepare
from agent_learning.runtime import Runtime
from tests import test_phase_host_integration as fixtures


class PhaseSealRecoveryTests(unittest.TestCase):
    runtime = fixtures.PhaseHostIntegrationTests.runtime
    host_events = fixtures.PhaseHostIntegrationTests.host_events
    deliver = fixtures.PhaseHostIntegrationTests.deliver

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="seal-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"ALP_DSH_REVIEW_PROFILE": ""}).start()

    def start(self, host="codex", suffix="", *, text="Original project constraint"):
        r, p = self.runtime(host, host + suffix)
        configure(r, p, {"mode": "phase", "every_phases": 1}, 0)
        send, c = self.host_events(r, native="seal-native" + suffix)
        send("start", turn=0)
        r.task_update(p, c, "Preserve original project constraint", new_task=True)
        send("prompt", turn=1, text=text)
        self.deliver(r, p, c)
        return r, p, send, c

    def failed_stop(self, r, send, *, text="ORIGINAL_PUBLIC_CONSTRAINT"):
        with patch("agent_learning.phase_review.seal_phase", side_effect=sqlite3.OperationalError("Simulated seal failure")):
            send("stop", turn=2, text=text)
        phase = r.db.execute("SELECT * FROM review_phases ORDER BY created LIMIT 1").fetchone()
        self.assertEqual(phase["state"], "awaiting_stop")
        request = r.db.execute("SELECT * FROM phase_seal_requests WHERE phase_id=?", (phase["id"],)).fetchone()
        self.assertIsNotNone(request, "the Stop binding must outlive the optional savepoint rollback")
        return phase, request

    def test_each_host_recovers_original_stop_after_task_switch_exactly_once(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                r, p, send, c = self.start(host)
                phase, request = self.failed_stop(r, send)
                original_time = r.db.execute("SELECT created FROM events WHERE id=?", (request["event_id"],)).fetchone()[0]
                r.task_update(p, c, "A different later task", new_task=True)
                send("prompt", turn=3, text="LATER_REQUEST_MUST_NOT_JOIN")
                send("stop", turn=4, text="LATER_REPLY_MUST_NOT_JOIN")
                with patch("subprocess.Popen", side_effect=AssertionError("Local compensation cannot start a model")):
                    with r.transaction():
                        prepare(r, p, force=True)
                recovered = r.db.execute("SELECT * FROM review_phases WHERE id=?", (phase["id"],)).fetchone()
                self.assertEqual(recovered["event_id"], request["event_id"])
                self.assertEqual(recovered["task_id"], phase["task_id"])
                packet = r.db.execute("SELECT packet FROM phase_review_batches WHERE project=? ORDER BY created DESC LIMIT 1", (p,)).fetchone()
                self.assertIsNotNone(packet)
                body = json.loads(packet[0])
                visible = json.dumps(body)
                self.assertIn("ORIGINAL_PUBLIC_CONSTRAINT", visible)
                self.assertNotIn("LATER_REQUEST_MUST_NOT_JOIN", visible)
                self.assertNotIn("LATER_REPLY_MUST_NOT_JOIN", visible)
                self.assertTrue(all(e["created"] <= original_time for item in body["items"] for e in item["events"]))
                self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_seal_requests WHERE phase_id=?", (phase["id"],)).fetchone()[0], 0)
                batches = r.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0]
                with r.transaction():
                    prepare(r, p, force=True)
                self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0], batches)
                self.assertEqual(r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_reopen_recovers_original_long_public_segments(self):
        r, p, send, c = self.start(suffix="-long")
        text = "Original sourced constraint. " * 90 + "ORIGINAL_LONG_TAIL"
        with patch.object(r, "_work_capture_event", side_effect=sqlite3.OperationalError("Capture unavailable")):
            send("stop", turn=2, text=text)
        phase = r.db.execute("SELECT * FROM review_phases").fetchone()
        self.assertEqual(phase["state"], "awaiting_stop")
        reopened = Runtime(r.home, host=r.host, config=copy.deepcopy(r.config))
        self.addCleanup(reopened.close)
        with patch("subprocess.Popen", side_effect=AssertionError("Recovery cannot start a model")):
            with reopened.transaction():
                prepare(reopened, p, force=True)
        refs = reopened.db.execute("SELECT e.payload FROM problems p JOIN problem_events pe ON pe.problem=p.id "
            "JOIN events e ON e.id=pe.event WHERE p.project=? AND p.task_id=? AND e.type='PublicMessageSegment'",
            (p, phase["task_id"])).fetchall()
        self.assertTrue(any("ORIGINAL_LONG_TAIL" in row[0] for row in refs))
        self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM phase_seal_requests").fetchone()[0], 0)
        self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 0)

    def test_phase_recovery_completes_both_queues_without_replaying_work(self):
        r, p, send, c = self.start(suffix="-work-confirm")
        text = "Original sourced constraint. " * 90 + "ORIGINAL_LONG_TAIL"
        with patch.object(r, "_work_index", side_effect=sqlite3.OperationalError("Projection interrupted")):
            send("stop", turn=2, text=text)
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_seal_requests").fetchone()[0], 1)
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 1)
        with patch("subprocess.Popen", side_effect=AssertionError("Recovery cannot start a model")):
            with r.transaction():
                prepare(r, p, force=True)
            self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_seal_requests").fetchone()[0], 0)
            self.assertEqual(r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 0)
            with patch.object(r, "_work_capture_event", side_effect=AssertionError("Completed work must not replay")):
                with r.transaction():
                    r._work_repair_safely(p)
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_repeated_fault_retains_request_and_never_creates_call(self):
        r, p, send, c = self.start(suffix="-fault")
        phase, request = self.failed_stop(r, send)
        for _ in range(2):
            with patch("agent_learning.phase_review.seal_phase", side_effect=sqlite3.OperationalError("Still unavailable")):
                with r.transaction():
                    prepare(r, p, force=True)
        current = r.db.execute("SELECT * FROM phase_seal_requests WHERE phase_id=?", (phase["id"],)).fetchone()
        self.assertGreaterEqual(current["attempts"], 2)
        self.assertEqual(current["event_id"], request["event_id"])
        self.assertEqual(current["last_error"], "local_seal_failed")
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0], 0)
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_already_captured_oversized_reply_keeps_original_segment_metadata(self):
        r, p, send, c = self.start(suffix="-oversized")
        text = "Original visible project evidence. " * 2200
        self.assertGreater(len(text), 65536)
        phase, request = self.failed_stop(r, send, text=text)
        segments = [tuple(row) for row in r.db.execute("SELECT id,payload FROM events WHERE type='PublicMessageSegment' ORDER BY id")]
        self.assertTrue(segments)
        with r.transaction():
            prepare(r, p, force=True)
        self.assertEqual(r.db.execute("SELECT event_id FROM review_phases WHERE id=?", (phase["id"],)).fetchone()[0], request["event_id"])
        self.assertEqual([tuple(row) for row in r.db.execute("SELECT id,payload FROM events WHERE type='PublicMessageSegment' ORDER BY id")], segments)
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_seal_requests").fetchone()[0], 0)

    def test_later_stop_never_replaces_first_pinned_stop(self):
        r, p, send, c = self.start(suffix="-later")
        phase, request = self.failed_stop(r, send)
        send("stop", turn=9, text="LATER_EXPLANATION_IS_OUTSIDE_ORIGINAL_STAGE")
        current = r.db.execute("SELECT event_id FROM review_phases WHERE id=?", (phase["id"],)).fetchone()
        self.assertEqual(current[0], request["event_id"])
        packet = r.db.execute("SELECT packet FROM phase_review_batches ORDER BY created DESC LIMIT 1").fetchone()
        self.assertIsNotNone(packet)
        self.assertNotIn("LATER_EXPLANATION_IS_OUTSIDE_ORIGINAL_STAGE", packet[0])

    def test_account_change_retains_old_request_outside_current_queue(self):
        r, p, send, c = self.start(suffix="-epoch")
        phase, request = self.failed_stop(r, send)
        r.account_changed(notification_id="explicit-account-boundary")
        with r.transaction():
            prepare(r, p, force=True)
        self.assertEqual(r.db.execute("SELECT state FROM review_phases WHERE id=?", (phase["id"],)).fetchone()[0], "awaiting_stop")
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_seal_requests").fetchone()[0], 1)
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
