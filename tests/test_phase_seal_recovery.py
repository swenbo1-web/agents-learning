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

    def test_each_host_defers_disabled_original_capture_through_fresh_stop_then_recovers_after_task_switch(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                r, p, send, c = self.start(host, suffix="-disabled-recovery")
                r.config["admin"]["max_input_chars"] = 64000
                marker = "ORIGINAL_MIDDLE_CONDITION_" + host
                text = "Original public detail. " * 180 + marker + " Further public detail. " * 180
                with patch.object(r, "_work_index", side_effect=sqlite3.OperationalError("Original projection failed")):
                    send("stop", turn=2, text=text)
                phase = dict(r.db.execute("SELECT * FROM review_phases").fetchone())
                request = dict(r.db.execute("SELECT * FROM phase_seal_requests").fetchone())
                self.assertNotIn(marker, r.db.execute("SELECT payload FROM events WHERE id=?", (request["event_id"],)).fetchone()[0])
                original_capture = r.db.execute("SELECT capture FROM work_capture_pending").fetchone()[0]
                original_memories = r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0]
                r.config["work_memory"]["enabled"] = False
                with patch("subprocess.Popen", side_effect=AssertionError("Local repair cannot start a model")):
                    # A later fresh Stop must not bypass the original capture's deferred result.
                    send("stop", turn=3, text="LATER_EXPLANATION_MUST_NOT_REPLACE_ORIGINAL")
                    with r.transaction():
                        prepare(r, p, force=True)
                    self.assertEqual(r.db.execute("SELECT state FROM review_phases WHERE id=?", (phase["id"],)).fetchone()[0], "awaiting_stop")
                    self.assertEqual(r.db.execute("SELECT capture FROM work_capture_pending").fetchone()[0], original_capture)
                    self.assertEqual(r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], original_memories)
                    self.assertEqual(r.db.execute("SELECT COUNT(*) FROM events WHERE type='PublicMessageSegment'").fetchone()[0], 0)
                    self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_seal_requests").fetchone()[0], 1)
                    r.task_update(p, c, "A different subsequent task", new_task=True)
                    r.config["work_memory"]["enabled"] = True
                    with r.transaction():
                        prepare(r, p, force=True)
                recovered = r.db.execute("SELECT * FROM review_phases WHERE id=?", (phase["id"],)).fetchone()
                self.assertEqual(recovered["event_id"], request["event_id"])
                self.assertEqual(recovered["task_id"], phase["task_id"])
                packet = r.db.execute("SELECT packet FROM phase_review_batches ORDER BY created DESC LIMIT 1").fetchone()[0]
                self.assertIn(marker, packet)
                self.assertNotIn("LATER_EXPLANATION_MUST_NOT_REPLACE_ORIGINAL", packet)
                self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_seal_requests").fetchone()[0], 0)
                self.assertEqual(r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 0)
                self.assertEqual(r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_empty_public_stops_are_not_deferred_and_native_antigravity_boundary_is_unchanged(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                r, p, send, _ = self.start(host, suffix="-no-public-reply")
                r.config["work_memory"]["enabled"] = False
                with patch("subprocess.Popen", side_effect=AssertionError("No model work is needed")):
                    send("stop", turn=2, text="")
                    with r.transaction():
                        prepare(r, p, force=True)
                state = r.db.execute("SELECT state FROM review_phases").fetchone()[0]
                if host == "antigravity":
                    # No public Stop exists, so keep the original native/public evidence boundary.
                    self.assertEqual(state, "awaiting_stop")
                else:
                    self.assertNotEqual(state, "awaiting_stop")
                self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_seal_requests").fetchone()[0], 0)
                self.assertEqual(r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 0)
                self.assertEqual(r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_disabled_channel_can_finish_a_phase_whose_original_work_is_already_captured(self):
        r, p, send, _ = self.start(suffix="-already-captured-disabled")
        phase, request = self.failed_stop(r, send, text="Original preserved public condition. " * 100)
        original = [tuple(row) for row in r.db.execute("SELECT id,payload FROM events WHERE type='PublicMessageSegment' ORDER BY id")]
        self.assertTrue(original)
        r.config["work_memory"]["enabled"] = False
        with patch.object(r, "_work_capture_event", side_effect=AssertionError("Existing captures must not replay")):
            with r.transaction():
                prepare(r, p, force=True)
        self.assertEqual(r.db.execute("SELECT event_id FROM review_phases WHERE id=?", (phase["id"],)).fetchone()[0], request["event_id"])
        self.assertEqual(original, [tuple(row) for row in r.db.execute("SELECT id,payload FROM events WHERE type='PublicMessageSegment' ORDER BY id")])
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_seal_requests").fetchone()[0], 0)

    def test_each_host_disabled_channel_seals_preserved_public_text_and_defers_only_missing_capture(self):
        for host in ("codex", "antigravity", "dsh"):
            for long_message in (False, True):
                with self.subTest(host=host, long_message=long_message):
                    r, p, send, _ = self.start(host, suffix="-disabled-fresh-" + str(long_message))
                    r.config["admin"]["max_input_chars"] = 64000
                    r.config["work_memory"]["enabled"] = False
                    marker = "FROZEN_PUBLIC_CONDITION_" + host
                    text = ("Original public detail. " * 180 + marker + " Further public detail. " * 180
                            if long_message else "A short complete public delivery: " + marker)
                    original_memories = r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0]
                    with patch("subprocess.Popen", side_effect=AssertionError("No model work is needed")):
                        send("stop", turn=2, text=text)
                        phase = r.db.execute("SELECT * FROM review_phases").fetchone()
                        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], original_memories)
                        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 0)
                        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM events WHERE type='PublicMessageSegment'").fetchone()[0], 0)
                        if long_message:
                            self.assertEqual(phase["state"], "awaiting_stop")
                            self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_seal_requests").fetchone()[0], 1)
                            r.config["work_memory"]["enabled"] = True
                            with r.transaction():
                                prepare(r, p, force=True)
                        else:
                            self.assertNotEqual(phase["state"], "awaiting_stop")
                    self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_seal_requests").fetchone()[0], 0)
                    packet = r.db.execute("SELECT packet FROM phase_review_batches ORDER BY created DESC LIMIT 1").fetchone()[0]
                    self.assertIn(marker, packet)
                    self.assertEqual(r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_deferred_captures_rotate_before_ready_seals_and_survive_restart(self):
        from agent_learning.phase_seal_recovery import recover
        for already_captured in (False, True):
            for force in (False, True):
                with self.subTest(already_captured=already_captured, force=force):
                    r = Runtime(self.root / f"rotation-{already_captured}-{force}", config={})
                    self.addCleanup(r.close)
                    p = r.project(self.workspace)
                    configure(r, p, {"mode": "phase", "every_phases": 20}, 0)
                    r.config["work_memory"]["enabled"] = False

                    def stop(conversation, text, *, fail_seal=False):
                        task = r.task_update(p, conversation, "Original delivery " + conversation)
                        with r.transaction():
                            r._event(p, conversation, "UserPromptSubmit", {"prompt": task["goal"]},
                                     outcome="reported", task_id=task["id"], task_revision=task["revision"])
                        r.task_update(p, conversation, task["goal"], status="delivered")
                        raw = {"last_assistant_message": text}
                        with r.transaction():
                            event, _ = r._event(p, conversation, "Stop", raw, outcome="reported",
                                                task_id=task["id"], task_revision=task["revision"])
                            if fail_seal:
                                with patch("agent_learning.phase_review.seal_phase", side_effect=sqlite3.OperationalError("Synthetic seal failure")):
                                    r._work_capture_safely(p, event["id"], self.workspace, raw=raw,
                                                          summarize=True, conversation=conversation)
                            else:
                                r._work_capture_safely(p, event["id"], self.workspace, raw=raw,
                                                      summarize=True, conversation=conversation)
                        return dict(r.db.execute("SELECT * FROM review_phases WHERE conversation=?", (conversation,)).fetchone())

                    with patch("subprocess.Popen", side_effect=AssertionError("Local recovery cannot launch a model")):
                        phases = [stop(f"long-{index}", "Original public detail. " * 180 +
                            f"ORIGINAL_MIDDLE_{index}" + " Further public detail. " * 180) for index in range(8)]
                        r.config["work_memory"]["enabled"] = already_captured
                        ready = stop("ready", "Complete public delivery", fail_seal=True)
                        r.config["work_memory"]["enabled"] = False
                        with r.transaction():
                            r.db.execute("UPDATE phase_seal_requests SET attempts=3,last_error='local_seal_failed' WHERE phase_id=?",
                                         (phases[0]["id"],))
                        frozen = [tuple(row) for row in r.db.execute(
                            "SELECT phase_id,event_id,workspace,capture,attempts,last_error,created "
                            "FROM phase_seal_requests ORDER BY created")]
                        self.assertEqual(len(frozen), 9)
                        fixed_now = 2000000000.0
                        with patch("agent_learning.phase_seal_recovery.time.time", return_value=fixed_now):
                            with r.transaction():
                                recover(r, p)
                            self.assertEqual(r.db.execute("SELECT state FROM review_phases WHERE id=?", (ready["id"],)).fetchone()[0], "awaiting_stop")
                            self.assertEqual(frozen, [tuple(row) for row in r.db.execute(
                                "SELECT phase_id,event_id,workspace,capture,attempts,last_error,created "
                                "FROM phase_seal_requests ORDER BY created")])
                            self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_seal_requests WHERE next_try>?", (fixed_now,)).fetchone()[0], 8)
                            with r.transaction():
                                recover(r, p, force=force)
                        self.assertEqual(r.db.execute("SELECT state FROM review_phases WHERE id=?", (ready["id"],)).fetchone()[0], "sealed")
                        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_seal_requests").fetchone()[0], 8)
                        r.config["work_memory"]["enabled"] = True
                        home, config = r.home, copy.deepcopy(r.config)
                        r.close()
                        reopened = Runtime(home, config=config)
                        self.addCleanup(reopened.close)
                        # Forced recovery must also consume future-dated deferred
                        # requests after restart and explicit channel re-enable.
                        with patch("agent_learning.phase_seal_recovery.time.time", return_value=fixed_now + 1):
                            with reopened.transaction():
                                recover(reopened, p, force=True)
                        self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM phase_seal_requests").fetchone()[0], 0)
                        self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM review_phases WHERE state='sealed'").fetchone()[0], 9)
                        for index, phase in enumerate(phases):
                            linked = reopened.db.execute("SELECT 1 FROM problems pr JOIN problem_events pe ON pe.problem=pr.id "
                                "JOIN events e ON e.id=pe.event WHERE pr.task_id=? AND e.type='PublicMessageSegment' "
                                "AND e.payload LIKE ?", (phase["task_id"], f"%ORIGINAL_MIDDLE_{index}%")).fetchone()
                            self.assertIsNotNone(linked)
                        self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
