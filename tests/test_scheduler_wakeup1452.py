"""Stop handoff survives the gap before a finite local scheduler starts."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.phase_review import configure
from agent_learning.runtime import Runtime
from tests import test_phase_host_integration as fixtures


class SchedulerWakeup1452Tests(unittest.TestCase):
    runtime = fixtures.PhaseHostIntegrationTests.runtime
    host_events = fixtures.PhaseHostIntegrationTests.host_events
    deliver = fixtures.PhaseHostIntegrationTests.deliver

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wake1452-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"ALP_DSH_REVIEW_PROFILE": ""}).start()
        self.launches = []

    def start(self, host="codex"):
        r, p = self.runtime(host)
        configure(r, p, {"mode": "phase", "every_phases": 1}, 0)
        send, c = self.host_events(r)
        send("start", turn=0)
        r.task_update(p, c, "Preserve the sourced reusable project constraint", new_task=True)
        send("prompt", turn=1, text="Use explicit reusable project conventions")
        self.deliver(r, p, c)
        r.config["scheduler"]["enabled"] = True
        spawn = patch.object(r, "_launch_scheduler").start()
        return r, p, send, c, spawn

    def interrupt_after_capture(self, r, send):
        with patch.object(r, "scheduler_wake", side_effect=KeyboardInterrupt("before wake transaction")):
            with self.assertRaises(KeyboardInterrupt):
                send("stop", turn=2, text="Delivered a reusable project constraint")

    def intent(self, r, p):
        return r.db.execute("SELECT * FROM scheduler_wake_requests WHERE project=? AND host_scope=?",
                            (p, r._session_scope())).fetchone()

    def test_stop_commits_stage_without_preparing_inside_sync_hook(self):
        r, p, send, c, spawn = self.start()
        with patch("agent_learning.phase_review.prepare", side_effect=AssertionError("expensive synchronous scan")):
            self.interrupt_after_capture(r, send)
        phase = r.db.execute("SELECT state,event_id FROM review_phases WHERE project=?", (p,)).fetchone()
        self.assertEqual(phase["state"], "sealed")
        self.assertIsNotNone(phase["event_id"])
        self.assertIsNotNone(self.intent(r, p))
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0], 0)
        spawn.assert_not_called()

    def test_reopen_prompt_recovers_once_without_sync_queue_scan(self):
        r, p, send, c, spawn = self.start()
        self.interrupt_after_capture(r, send)
        home, config = r.home, r.config
        r.close()
        reopened = Runtime(home, config=config)
        self.addCleanup(reopened.close)
        with patch.object(reopened, "_launch_scheduler") as launch, \
             patch.object(reopened, "_scheduler_refresh", side_effect=AssertionError("sync scan")), \
             patch.object(reopened, "_scheduler_recover", side_effect=AssertionError("sync global recovery")):
            raw = {"hook_event_name": "UserPromptSubmit", "session_id": c, "cwd": str(self.workspace),
                   "prompt": "Continue the next project stage", "turn_id": "3"}
            reopened.handle_hook(raw)
            reopened.handle_hook(raw)
        launch.assert_called_once()
        self.assertEqual(self.intent(reopened, p)["attempts"], 1)
        self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_launch_failure_retains_hint_with_bounded_retry(self):
        r, p, send, c, spawn = self.start()
        spawn.side_effect = OSError("offline launcher unavailable")
        send("stop", turn=2, text="Delivered a sourced project convention")
        self.assertEqual(self.intent(r, p)["attempts"], 1)
        self.assertIsNone(r.db.execute("SELECT 1 FROM scheduler_worker").fetchone())
        for turn in range(3, 10):
            send("prompt", turn=turn, text="Continue the project stage")
        self.assertEqual(spawn.call_count, 1)
        r.db.execute("UPDATE scheduler_wake_requests SET next_try=0")
        spawn.side_effect = None
        send("prompt", turn=11, text="Continue with explicit project boundaries")
        self.assertEqual(spawn.call_count, 2)
        self.assertEqual(self.intent(r, p)["attempts"], 2)

    def test_worker_reservation_cannot_ack_or_duplicate_a_launch(self):
        r, p, send, c, spawn = self.start()
        self.interrupt_after_capture(r, send)
        first = r.claim_scheduler_worker()
        before = self.intent(r, p)["next_try"]
        send("prompt", turn=3)
        spawn.assert_not_called()
        self.assertIsNotNone(self.intent(r, p))
        self.assertEqual(self.intent(r, p)["attempts"], 0)
        self.assertEqual(self.intent(r, p)["next_try"], before)
        r.db.execute("UPDATE scheduler_worker SET lease_until=0")
        r.db.execute("UPDATE scheduler_wake_requests SET next_try=0")
        send("prompt", turn=4)
        spawn.assert_called_once()
        self.assertNotEqual(first, r.db.execute("SELECT token FROM scheduler_worker").fetchone()[0])
        self.assertIsNotNone(self.intent(r, p))

    def test_refresh_acknowledgement_rolls_back_with_derived_jobs(self):
        r, p, send, c, spawn = self.start()
        self.interrupt_after_capture(r, send)
        with patch.object(r, "_scheduler_release_state", side_effect=sqlite3.OperationalError("local refresh failure")):
            with self.assertRaises(sqlite3.OperationalError), r.transaction():
                r._scheduler_refresh(p, r._scheduler_config(), acknowledge_wakeups=True)
        self.assertIsNotNone(self.intent(r, p))
        with r.transaction():
            r._scheduler_refresh(p, r._scheduler_config(), acknowledge_wakeups=True)
        self.assertIsNone(self.intent(r, p))
        self.assertIsNotNone(r.db.execute("SELECT 1 FROM phase_review_batches").fetchone())
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_native_hosts_recover_next_prompt_in_their_own_scope(self):
        for host in ("dsh", "antigravity"):
            with self.subTest(host=host):
                r, p, send, c, spawn = self.start(host)
                # Adapter handling catches local wake failures, while keeping
                # its original public Stop/trajectory receipt complete.
                with patch.object(r, "scheduler_wake", side_effect=OSError("local wake interrupted")):
                    send("stop", turn=2, text="Delivered a sourced project convention")
                self.assertIsNotNone(self.intent(r, p))
                with patch.object(r, "_scheduler_refresh", side_effect=AssertionError("sync scan")):
                    send("prompt", turn=3)
                spawn.assert_called_once()
                self.assertEqual(r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_disabled_configuration_and_old_account_never_consume_hint(self):
        from agent_learning.scheduler_wakeup import pending
        r, p, send, c, spawn = self.start()
        self.interrupt_after_capture(r, send)
        r.config["scheduler"]["enabled"] = False
        send("prompt", turn=3)
        self.assertIsNone(pending(r, p))
        r.config["scheduler"]["enabled"] = True
        r.admin_configure(p, False)
        send("prompt", turn=4)
        self.assertIsNone(pending(r, p))
        self.assertEqual(r.scheduler_status(p)["pending_wakeups"][0]["state"], "paused")
        r.admin_configure(p, True)
        r.scope_account_changed("wake-account-changed")
        send("prompt", turn=5)
        self.assertIsNone(pending(r, p))
        self.assertEqual(r.scheduler_status(p)["pending_wakeups"], [])
        spawn.assert_not_called()

    def test_foreign_host_cannot_borrow_intent_and_status_is_read_only(self):
        from agent_learning.scheduler_wakeup import pending
        r, p, send, c, spawn = self.start()
        self.interrupt_after_capture(r, send)
        other = Runtime(r.home, host="dsh", config=r.config)
        self.addCleanup(other.close)
        self.assertIsNone(pending(other, p))
        changes = r.db.total_changes
        with patch.object(r, "admin_effective_config", side_effect=AssertionError("no executor resolution")):
            state = r.scheduler_status(p)
        self.assertEqual(r.db.total_changes, changes)
        self.assertEqual(len(state["pending_wakeups"]), 1)
        self.assertEqual(state["pending_wakeups"][0]["state"], "wake_pending")
        self.assertIsNone(state["worker"])

    def test_protected_model_failure_is_not_unlocked_by_local_resume(self):
        r, p, send, c, spawn = self.start()
        self.interrupt_after_capture(r, send)
        r.db.execute("INSERT INTO admin_calls(id,project,problem,revision,day,state,created,retry_allowed) "
                     "VALUES('protected',?,'old-problem',1,'2026-01-01','failed',1,0)", (p,))
        send("prompt", turn=3)
        row = r.db.execute("SELECT state,retry_allowed FROM admin_calls WHERE id='protected'").fetchone()
        self.assertEqual(tuple(row), ("failed", 0))
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_hint_registration_failure_preserves_original_stop_and_public_text(self):
        r, p, send, c, spawn = self.start()
        with patch("agent_learning.scheduler_wakeup.request", side_effect=sqlite3.OperationalError("derived hint unavailable")), \
             patch.object(r, "scheduler_wake", side_effect=OSError("no local worker")):
            send("stop", turn=2, text="A complete public delivery remains available")
        event = r.db.execute("SELECT payload FROM events WHERE project=? AND conversation=? AND type='Stop'",
                             (p, c)).fetchone()
        self.assertIn("A complete public delivery remains available", event[0])
        self.assertEqual(r.db.execute("SELECT state FROM review_phases").fetchone()[0], "sealed")
        self.assertIsNone(self.intent(r, p))
        self.assertIsNotNone(r.db.execute("SELECT 1 FROM work_memories WHERE project=? AND conversation=? "
            "AND json_extract(body,'$.type')='public_summary'", (p, c)).fetchone())

    def test_schema25_reopen_installs_missing_derived_wake_table(self):
        r, p, send, c, spawn = self.start()
        self.assertEqual(r.db.execute("PRAGMA user_version").fetchone()[0], 25)
        r.db.execute("DROP TABLE scheduler_wake_requests")
        # Previous-version receipts must not claim new derived storage exists.
        r.db.execute("UPDATE runtime_storage_receipt SET signature='1.4.51:schema25:old-startup'")
        home, config = r.home, r.config
        r.close()
        reopened = Runtime(home, config=config)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.db.execute("PRAGMA user_version").fetchone()[0], 25)
        self.assertIsNotNone(reopened.db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_wake_requests'").fetchone())
        self.assertIsNotNone(reopened.task(p, c))

    def test_foreign_sync_refresh_cannot_ack_hint_behind_unstarted_worker(self):
        r, p, send, c, spawn = self.start("antigravity")
        r.admin_configure(p, True, provider="host", model_mode="first")
        with patch.object(r, "scheduler_wake", side_effect=OSError("wake interrupted after capture")):
            send("stop", turn=2, text="Delivered a reusable sourced project convention")
        self.assertIsNotNone(self.intent(r, p))
        other = Runtime(r.home, host="codex", config=r.config)
        self.addCleanup(other.close)
        other.admin_configure(p, False)
        # Codex has no hint of its own. Its synchronous refresh still visits
        # the enabled AG queue, but cannot acknowledge AG's handoff.
        token = other.claim_scheduler_worker()
        self.assertIsNotNone(token)
        other.scheduler_wake(p)
        self.assertIsNotNone(self.intent(r, p))
        self.assertIsNotNone(r.db.execute("SELECT 1 FROM phase_review_batches").fetchone())
        self.assertEqual(r.db.execute("SELECT state FROM scheduler_worker").fetchone()[0], "reserved")
        send("prompt", turn=3)
        spawn.assert_not_called()
        self.assertIsNotNone(self.intent(r, p))
        r.db.execute("UPDATE scheduler_worker SET lease_until=0")
        send("prompt", turn=4)
        spawn.assert_called_once()
        self.assertIsNotNone(self.intent(r, p))
        self.assertNotEqual(token, r.db.execute("SELECT token FROM scheduler_worker").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
