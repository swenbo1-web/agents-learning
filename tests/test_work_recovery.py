"""Derived projection recovery never borrows a later task or checkout."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.runtime import Runtime
from agent_learning.work_memory import WORK_INDEX_VERSION


class WorkRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-work-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "checkout"
        self.workspace.mkdir()
        (self.workspace / ".git").mkdir()
        self.home = self.root / "home"
        self.r = Runtime(self.home, config={})
        self.addCleanup(lambda: self.r.close())
        self.p = self.r.project(self.workspace)

    def hook(self, **fields):
        return self.r.handle_hook(dict(hook_event_name="UserPromptSubmit", session_id="chat",
            cwd=str(self.workspace), turn_id="stable-turn", prompt="compiler boundary", **fields))

    def fail(self, **fields):
        with patch.object(self.r, "_work_index", side_effect=sqlite3.OperationalError("synthetic interruption")):
            self.hook(**fields)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], 0)
        job = self.r.db.execute("SELECT * FROM work_capture_pending").fetchone()
        self.assertIsNotNone(job)
        self.assertEqual(job["last_error"], "OperationalError")
        return dict(job)

    def due(self):
        with self.r.transaction():
            self.r.db.execute("UPDATE work_capture_pending SET next_try=0")

    def test_exact_event_replay_repairs_once_after_restart(self):
        job = self.fail()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_event_checkouts").fetchone()[0], 1)
        self.r.close()
        self.r = Runtime(self.home, config={})
        self.due()
        self.hook()
        self.hook()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT event_id FROM work_memories").fetchone()[0], job["event_id"])

    def test_retry_is_bounded_and_backoff_prevents_hot_loop(self):
        job = self.fail()
        with patch.object(self.r, "_work_capture_event", side_effect=AssertionError("must not retry before due")):
            self.hook()
        self.due()
        with patch.object(self.r, "_work_capture_event", side_effect=ValueError("bad projection")):
            self.hook()
        updated = dict(self.r.db.execute("SELECT * FROM work_capture_pending").fetchone())
        self.assertEqual(updated["attempts"], job["attempts"] + 1)
        self.assertGreater(updated["next_try"], 0)

    def test_disabled_capture_keeps_pending_without_replaying(self):
        self.fail()
        self.due()
        self.r.config["work_memory"]["enabled"] = False
        with patch.object(self.r, "_work_capture_event", side_effect=AssertionError("capture disabled")):
            self.hook()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 1)

    def test_source_digest_change_keeps_original_pending_and_no_projection(self):
        self.fail()
        self.due()
        with self.r.transaction():
            self.r.db.execute("UPDATE events SET capture_digest='changed'")
            self.r._work_repair_safely(self.p)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT last_error FROM work_capture_pending").fetchone()[0], "ContractError")

    def test_repairs_are_limited_and_do_not_accept_another_checkout(self):
        with patch.object(self.r, "_work_index", side_effect=ValueError("projection interrupted")):
            for index in range(4):
                self.r.handle_hook(dict(hook_event_name="UserPromptSubmit", session_id="chat",
                    cwd=str(self.workspace), turn_id=f"turn-{index}", prompt=f"request {index}"))
        self.due()
        with self.r.transaction():
            self.r._work_repair_safely(self.p)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 2)
        other = self.root / "another checkout"
        other.mkdir()
        with self.r.transaction():
            self.r.db.execute("UPDATE work_capture_pending SET workspace=?", (str(other),))
            self.r._work_repair_safely(self.p)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 2)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], 2)

    def test_public_segments_and_original_task_survive_failed_projection(self):
        old = self.r.task_update(self.p, "chat", "original boundary", {"mode": "analysis"})
        text = "compiler boundary " * 350
        # A raw capture can be larger than its authoritative bounded event preview.
        with patch.object(self.r, "_work_index", side_effect=ValueError("projection interrupted")):
            self.r.handle_hook(dict(hook_event_name="Stop", session_id="chat", cwd=str(self.workspace),
                turn_id="public", last_assistant_message=text))
        self.r.task_update(self.p, "chat", "next task", {"mode": "analysis"}, new_task=True)
        self.due()
        with self.r.transaction():
            self.r._work_repair_safely(self.p)
        rows = self.r.db.execute("SELECT m.*,e.type FROM work_memories m JOIN events e ON e.id=m.event_id").fetchall()
        segments = [json.loads(row["body"])["summary"] for row in rows if row["type"] == "PublicMessageSegment"]
        self.assertEqual("".join(segments), text)
        self.assertTrue(all(row["task_id"] == old["id"] for row in rows))
        original_time = self.r.db.execute("SELECT created FROM events WHERE type='Stop'").fetchone()[0]
        self.assertTrue(all(row["created"] == original_time for row in rows))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 0)

    def test_subdirectory_cwd_is_preserved_for_relative_file_links(self):
        sub = self.workspace / "src"
        sub.mkdir()
        (sub / "a.py").write_text("fixture", encoding="utf-8")
        raw = dict(tool_name="write_file", tool_input={"path": "a.py"}, tool_response={"exit_code": 0})
        with self.r.transaction():
            event, _ = self.r._event(self.p, "chat", "PostToolUse", raw, outcome="succeeded")
            with patch.object(self.r, "_work_index", side_effect=ValueError("projection interrupted")):
                self.r._work_capture_safely(self.p, event["id"], sub, raw=raw)
        self.due()
        with self.r.transaction():
            self.r._work_repair_safely(self.p)
        self.assertEqual(self.r.db.execute("SELECT relative_path FROM work_file_links").fetchone()[0], "src/a.py")

    def test_lexical_upgrade_backfills_in_batches_without_changing_evidence(self):
        self.hook()
        before = [tuple(row) for row in self.r.db.execute("SELECT id,body,source_hash FROM work_memories")]
        with self.r.transaction():
            self.r.db.execute("UPDATE work_index_state SET version=?", (WORK_INDEX_VERSION - 1,))
            self.r.db.execute("UPDATE work_memory_fts SET text='obsolete'")
        self.r.close()
        self.r = Runtime(self.home, config={})
        self.assertEqual(self.r.work_memory_status(self.p)["index_rebuild_pending"], 1)
        self.hook()
        self.assertEqual(self.r.work_memory_status(self.p)["index_rebuild_pending"], 0)
        self.assertEqual(before, [tuple(row) for row in self.r.db.execute("SELECT id,body,source_hash FROM work_memories")])
        self.assertNotEqual(self.r.db.execute("SELECT text FROM work_memory_fts").fetchone()[0], "obsolete")


if __name__ == "__main__":
    unittest.main()
