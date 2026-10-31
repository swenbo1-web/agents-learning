"""Independent connections preserve a late stage after an old wake is acknowledged."""
import threading
import unittest
from unittest.mock import patch

from agent_learning.runtime import Runtime
from agent_learning.scheduler_wakeup import pending
from tests import test_scheduler_wakeup1452 as fixtures


class WakeupConcurrencyTests(unittest.TestCase):
    setUp = fixtures.SchedulerWakeup1452Tests.setUp
    runtime = fixtures.SchedulerWakeup1452Tests.runtime
    host_events = fixtures.SchedulerWakeup1452Tests.host_events
    deliver = fixtures.SchedulerWakeup1452Tests.deliver
    start = fixtures.SchedulerWakeup1452Tests.start
    intent = fixtures.SchedulerWakeup1452Tests.intent
    interrupt_after_capture = fixtures.SchedulerWakeup1452Tests.interrupt_after_capture

    def test_late_stop_survives_another_connections_refresh_commit(self):
        r, project, send, conversation, _ = self.start()
        self.interrupt_after_capture(r, send)
        old_event = self.intent(r, project)["event_id"]
        ready, begin_write, writing = threading.Event(), threading.Event(), threading.Event()
        failures = []

        def writer():
            other = None
            try:
                other = Runtime(r.home, config=r.config)
                ready.set()
                if not begin_write.wait(5):
                    raise AssertionError("refresh did not release the late writer")
                current = other.task(project, conversation)
                writing.set()
                task = other.task_update(project, conversation, "A distinct late delivered stage",
                    new_task=True, expected_task_id=current["id"],
                    expected_write_version=current["write_version"])
                other.task_update(project, conversation, task["goal"], status="delivered",
                    expected_task_id=task["id"], expected_write_version=task["write_version"])
                with patch.object(other, "_launch_scheduler"):
                    other.handle_hook({"hook_event_name": "Stop", "session_id": conversation,
                        "cwd": str(self.workspace), "turn_id": "late-stage-stop",
                        "last_assistant_message": "Delivered a new independently sourced constraint"})
            except BaseException as exc:
                failures.append(exc)
                ready.set()
                writing.set()
            finally:
                if other is not None:
                    other.close()

        worker = threading.Thread(target=writer, daemon=True)
        worker.start()
        try:
            self.assertTrue(ready.wait(5))
            self.assertEqual(failures, [])
            with r.transaction():
                r._scheduler_refresh(project, r._scheduler_config(), acknowledge_wakeups=True)
                self.assertIsNone(self.intent(r, project))
                begin_write.set()
                self.assertTrue(writing.wait(5))
                # The second connection cannot write inside this refresh's
                # BEGIN IMMEDIATE. Its new Stop commits after this ack.
                self.assertIsNone(self.intent(r, project))
        finally:
            begin_write.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertNotEqual(self.intent(r, project)["event_id"], old_event)
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM review_phases").fetchone()[0], 2)
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_paused_hint_remains_recoverable_under_the_current_control(self):
        r, project, send, _, launch = self.start()
        self.interrupt_after_capture(r, send)
        original = dict(self.intent(r, project))
        r.admin_configure(project, False)
        send("prompt", turn=3)
        self.assertIsNone(pending(r, project))
        self.assertEqual(r.scheduler_status(project)["pending_wakeups"][0]["state"], "paused")
        self.assertEqual(dict(self.intent(r, project)), original)
        launch.assert_not_called()
        r.admin_configure(project, True)
        send("prompt", turn=4)
        launch.assert_called_once()
        self.assertEqual(self.intent(r, project)["event_id"], original["event_id"])
        self.assertEqual(self.intent(r, project)["attempts"], 1)
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
