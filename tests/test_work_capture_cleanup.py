"""Derived capture cleanup must not erase authoritative Hook observations."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.phase_review import configure, prepare
from agent_learning.runtime import Runtime
from agent_learning import work_recovery


class WorkCaptureCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="work-cleanup-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.home = root / "home"
        self.r = Runtime(self.home, config={"admin": {"enabled": False},
                                          "scheduler": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "chat", "Preserve source observations", {"mode": "analysis"})
        no_process = patch("subprocess.Popen", side_effect=AssertionError("cleanup cannot launch a model"))
        no_process.start()
        self.addCleanup(no_process.stop)

    def hook(self, kind, **fields):
        return self.r.handle_hook({"hook_event_name": kind, "session_id": "chat",
                                   "cwd": str(self.workspace), **fields})

    def tool_pre(self):
        return self.hook("PreToolUse", tool_name="Write", tool_use_id="cleanup-tool",
                         tool_input={"file_path": "src/compiler.py"})

    def tool_post(self):
        return self.hook("PostToolUse", tool_name="Write", tool_use_id="cleanup-tool",
                         tool_input={"file_path": "src/compiler.py"},
                         tool_response={"exit_code": 1, "output": "compiler failed"})

    def post_event(self):
        return self.r.db.execute("SELECT * FROM events WHERE type='PostToolUse'").fetchone()

    def repair(self):
        with self.r.transaction():
            self.r.db.execute("UPDATE work_capture_pending SET next_try=0 WHERE project=?", (self.p,))
            self.r._work_repair_safely(self.p)

    def test_cleanup_failure_preserves_tool_failure_and_retry_is_idempotent(self):
        self.tool_pre()
        with patch.object(work_recovery, "complete", side_effect=sqlite3.OperationalError("PRIVATE cleanup failure")):
            self.assertEqual(self.tool_post(), {})
        event = self.post_event()
        self.assertIsNotNone(event)
        self.assertEqual(event["outcome"], "failed")
        self.assertTrue(self.r.problems(self.p))
        self.assertEqual(self.r.db.execute("SELECT observation FROM operations").fetchone()[0], "failed")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories WHERE event_id=?",
                                           (event["id"],)).fetchone()[0], 0)
        pending = self.r.db.execute("SELECT * FROM work_capture_pending WHERE event_id=?", (event["id"],)).fetchone()
        self.assertEqual(pending["attempts"], 1)
        self.assertEqual(pending["last_error"], "OperationalError")
        self.assertNotIn("PRIVATE", json.dumps(dict(pending)))
        count = self.r.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        # A failure while restoring the derived projection keeps the same job,
        # rather than committing partial memory or replaying the business tool.
        with patch.object(work_recovery, "complete", side_effect=sqlite3.OperationalError("still unavailable")):
            self.repair()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories WHERE event_id=?",
                                           (event["id"],)).fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT attempts FROM work_capture_pending WHERE event_id=?",
                                           (event["id"],)).fetchone()[0], 2)
        self.repair()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories WHERE event_id=?",
                                           (event["id"],)).fetchone()[0], 1)
        with patch.object(self.r, "_work_capture_event", side_effect=AssertionError("completed capture must not replay")):
            self.repair()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], count)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_cleanup_failure_cannot_remove_an_execution_denial(self):
        self.r.config["enforce_task_policy"] = True
        with patch.object(work_recovery, "complete", side_effect=sqlite3.IntegrityError("cleanup unavailable")):
            response = self.tool_pre()
        self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.r.db.execute("SELECT decision FROM operations").fetchone()[0], "denied")
        self.assertEqual(self.r.db.execute("SELECT outcome FROM events WHERE type='PreToolUse'").fetchone()[0], "denied")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 1)

    def test_failed_delete_statement_rolls_back_projection_but_keeps_source(self):
        self.tool_pre()
        original = self.r._work_capture_event

        def seed_pending(*args, **kwargs):
            result = original(*args, **kwargs)
            project, event_id, workspace = args[:3]
            work_recovery.register(self.r, project, event_id, workspace, kwargs.get("raw"))
            return result

        with self.r.transaction():
            self.r.db.execute("CREATE TRIGGER fail_capture_cleanup BEFORE DELETE ON work_capture_pending "
                              "BEGIN SELECT RAISE(ABORT,'cleanup-only fault'); END")
        with patch.object(self.r, "_work_capture_event", side_effect=seed_pending):
            self.assertEqual(self.tool_post(), {})
        event = self.post_event()
        self.assertEqual(event["outcome"], "failed")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories WHERE event_id=?",
                                           (event["id"],)).fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT attempts FROM work_capture_pending WHERE event_id=?",
                                           (event["id"],)).fetchone()[0], 1)
        with self.r.transaction():
            self.r.db.execute("DROP TRIGGER fail_capture_cleanup")
        self.repair()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories WHERE event_id=?",
                                           (event["id"],)).fetchone()[0], 1)

    def test_delete_then_error_restores_an_existing_pending_capture(self):
        self.tool_pre()
        with patch.object(work_recovery, "complete", side_effect=sqlite3.OperationalError("initial cleanup unavailable")):
            self.tool_post()
        event = self.post_event()
        prior = dict(self.r.db.execute("SELECT * FROM work_capture_pending WHERE event_id=?", (event["id"],)).fetchone())
        complete = work_recovery.complete

        def interrupted(runtime, project, event_id):
            complete(runtime, project, event_id)
            raise sqlite3.OperationalError("after delete")

        with self.r.transaction(), patch.object(work_recovery, "complete", side_effect=interrupted):
            self.r._work_capture_safely(self.p, event["id"], self.workspace, raw=json.loads(event["payload"]))
        after = dict(self.r.db.execute("SELECT * FROM work_capture_pending WHERE event_id=?", (event["id"],)).fetchone())
        self.assertEqual(after["capture"], prior["capture"])
        self.assertEqual(after["capture_digest"], prior["capture_digest"])
        self.assertEqual(after["attempts"], prior["attempts"] + 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories WHERE event_id=?",
                                           (event["id"],)).fetchone()[0], 0)

    def test_retry_registration_failure_still_preserves_authoritative_event(self):
        self.tool_pre()
        register = work_recovery.register

        def unavailable(*args, **kwargs):
            if kwargs.get("pending", True):
                raise sqlite3.OperationalError("retry queue unavailable")
            return register(*args, **kwargs)

        with patch.object(work_recovery, "complete", side_effect=sqlite3.OperationalError("cleanup unavailable")), \
                patch.object(work_recovery, "register", side_effect=unavailable):
            self.assertEqual(self.tool_post(), {})
        event = self.post_event()
        self.assertEqual(event["outcome"], "failed")
        self.assertTrue(self.r.problems(self.p))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories WHERE event_id=?",
                                           (event["id"],)).fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT value FROM metrics WHERE name='work_capture_retry_registration_failed'").fetchone()[0], 1)

    def test_stop_cleanup_failure_retains_delivery_and_seals_original_once_after_recovery(self):
        # An available synthetic executor allows prepare() to build a batch,
        # while the patched dispatcher prevents any worker or model process.
        self.r.config["admin"].update(enabled=True, command=[sys.executable])
        self.r.admin_observe_model(self.p, "chat", "offline-cleanup-model")
        no_admin = patch.object(self.r, "_launch_admin")
        no_admin.start()
        self.addCleanup(no_admin.stop)
        configure(self.r, self.p, {"mode": "phase", "every_phases": 1}, 0)
        self.hook("UserPromptSubmit", turn_id="input", prompt="Preserve original phase source")
        task = self.r.task(self.p, "chat")
        self.r.task_update(self.p, "chat", task["goal"], status="delivered")
        text = "Original public constraint. " * 90 + "ORIGINAL_LONG_TAIL"
        with patch.object(work_recovery, "complete", side_effect=sqlite3.OperationalError("cleanup unavailable")):
            self.assertEqual(self.hook("Stop", turn_id="delivery", last_assistant_message=text), {})
        event = self.r.db.execute("SELECT * FROM events WHERE type='Stop'").fetchone()
        self.assertEqual(event["task_status"], "delivered")
        phase = self.r.db.execute("SELECT * FROM review_phases").fetchone()
        self.assertEqual(phase["state"], "awaiting_stop")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM phase_seal_requests").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories WHERE event_id=?",
                                           (event["id"],)).fetchone()[0], 0)
        with self.r.transaction():
            prepare(self.r, self.p, force=True)
        recovered = self.r.db.execute("SELECT * FROM review_phases WHERE id=?", (phase["id"],)).fetchone()
        self.assertEqual(recovered["event_id"], event["id"])
        self.assertEqual(recovered["task_id"], event["task_id"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM phase_seal_requests").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 0)
        packet = self.r.db.execute("SELECT packet FROM phase_review_batches").fetchone()
        self.assertIn("ORIGINAL_LONG_TAIL", packet[0])
        batch_count = self.r.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0]
        memory_count = self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0]
        with self.r.transaction():
            prepare(self.r, self.p, force=True)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0], batch_count)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], memory_count)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_unusable_database_is_not_reported_as_successful_capture(self):
        self.tool_pre()

        def unavailable(runtime, *_args):
            runtime.db.close()
            raise sqlite3.ProgrammingError("database unavailable")

        with patch.object(work_recovery, "complete", side_effect=unavailable):
            with self.assertRaises(sqlite3.Error):
                self.tool_post()
        with closing(sqlite3.connect(self.home / "runtime.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE type='PostToolUse'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
