"""Derived projection recovery never borrows a later task or checkout."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.runtime import Runtime
from agent_learning.work_memory import WORK_INDEX_VERSION
from agent_learning.work_files import checkout_key, knowledge_file_state


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

    def failed_tool(self, raw):
        with self.r.transaction():
            event, _ = self.r._event(self.p, "tool-chat", "PostToolUse", raw, outcome="succeeded")
            with patch.object(self.r, "_work_index", side_effect=sqlite3.OperationalError("synthetic index failure")):
                self.r._work_capture_safely(self.p, event["id"], self.workspace, raw=raw)
        self.due()
        return event

    def test_delayed_retry_preserves_original_file_metadata_and_source_change(self):
        file = self.workspace / "engine.py"
        file.write_text("first", encoding="utf-8")
        event = self.failed_tool({"tool_name": "write_file", "tool_input": {"path": "engine.py"},
                                  "tool_response": {"exit_code": 0}})
        job = self.r.db.execute("SELECT capture FROM work_capture_pending").fetchone()
        self.assertEqual(json.loads(job[0])["file_projection"]["links"][0]["version"]["size"], 5)
        file.write_text("changed since the first observation", encoding="utf-8")
        with self.r.transaction():
            self.r._work_repair_safely(self.p)
        stored = json.loads(self.r.db.execute("SELECT version FROM work_file_links").fetchone()[0])
        self.assertEqual(stored["size"], 5)
        row = self.r.db.execute("SELECT * FROM work_memories").fetchone()
        self.assertTrue(self.r._work_item(self.p, row, workspace=self.workspace)["files"][0]["version_changed"])
        self.assertTrue(knowledge_file_state(self.r, self.p, {"evidence": [{"event_id": event["id"]}]},
                                            self.workspace)["source_files_changed"])

    def test_long_patch_retry_preserves_all_original_bounded_file_targets(self):
        text = "*** Begin Patch\n" + "".join(
            f"*** Update File: part{i}.py\n@@\n-" + "old " * 600 + "\n+" + "new " * 600 + "\n"
            for i in range(4)) + "*** End Patch"
        event = self.failed_tool({"tool_name": "apply_patch", "tool_input": {"patch": text},
                                  "tool_response": {"exit_code": 0}})
        self.assertTrue(json.loads(event["payload"])["truncated"])
        serialized = self.r.db.execute("SELECT capture FROM work_capture_pending").fetchone()[0]
        self.assertNotIn("old old old", serialized)
        with self.r.transaction():
            self.r._work_repair_safely(self.p)
        self.assertEqual([row[0] for row in self.r.db.execute("SELECT relative_path FROM work_file_links ORDER BY relative_path")],
                         [f"part{i}.py" for i in range(4)])
        self.assertEqual(self.r.work_memory_status(self.p)["capture_repair_pending"], 0)

    def test_legacy_tool_retry_marks_missing_original_metadata_unknown(self):
        file = self.workspace / "engine.py"
        file.write_text("first", encoding="utf-8")
        self.failed_tool({"tool_name": "write_file", "tool_input": {"path": "engine.py"},
                          "tool_response": {"exit_code": 0}})
        with self.r.transaction():
            self.r.db.execute("UPDATE work_capture_pending SET capture='{}'")
        file.write_text("a later file", encoding="utf-8")
        with self.r.transaction():
            self.r._work_repair_safely(self.p)
        stored = json.loads(self.r.db.execute("SELECT version FROM work_file_links").fetchone()[0])
        self.assertEqual(stored["status"], "unknown")
        row = self.r.db.execute("SELECT * FROM work_memories").fetchone()
        item = self.r._work_item(self.p, row, workspace=self.workspace)
        self.assertTrue(item["file_projection_incomplete"])
        self.assertFalse(item["automatic_eligible"])

    def test_retry_cannot_apply_a_projection_bound_to_different_evidence(self):
        self.failed_tool({"tool_name": "write_file", "tool_input": {"path": "engine.py"},
                          "tool_response": {"exit_code": 0}})
        with self.r.transaction():
            capture = json.loads(self.r.db.execute("SELECT capture FROM work_capture_pending").fetchone()[0])
            capture["file_projection"]["capture_digest"] = "different-source"
            self.r.db.execute("UPDATE work_capture_pending SET capture=?", (json.dumps(capture),))
            self.r._work_repair_safely(self.p)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], 0)
        self.assertEqual(self.r.work_memory_status(self.p)["capture_repair_pending"], 1)

    def test_explicit_rebuild_uses_pending_original_observation_and_reindexes_without_sampling(self):
        file = self.workspace / "engine.py"
        file.write_text("first", encoding="utf-8")
        self.failed_tool({"tool_name": "write_file", "tool_input": {"path": "engine.py"},
                          "tool_response": {"exit_code": 0}})
        file.write_text("changed later", encoding="utf-8")
        self.r.work_rebuild(self.p, self.workspace)
        original = self.r.db.execute("SELECT version FROM work_file_links").fetchone()[0]
        self.assertEqual(json.loads(original)["size"], 5)
        self.assertEqual(self.r.work_memory_status(self.p)["capture_repair_pending"], 0)
        with self.r.transaction():
            self.r.db.execute("DELETE FROM work_memory_fts")
        with patch("agent_learning.work_memory.current_file_version", side_effect=AssertionError("Reindex must not resample")):
            result = self.r.work_rebuild(self.p, self.workspace)
        self.assertEqual(result["reindexed"], 1)
        self.assertEqual(self.r.db.execute("SELECT version FROM work_file_links").fetchone()[0], original)

    def test_large_escaped_public_capture_keeps_plan_and_marks_truncation(self):
        from agent_learning.work_recovery import register
        text = "\x01" * 65536
        with self.r.transaction():
            event, _ = self.r._event(self.p, "chat", "Stop", {"last_assistant_message": text}, outcome="reported")
            register(self.r, self.p, event["id"], self.workspace, {"last_assistant_message": text})
        serialized = self.r.db.execute("SELECT capture FROM work_capture_pending").fetchone()[0]
        self.assertLessEqual(len(serialized), 150000)
        capture = json.loads(serialized)
        self.assertTrue(capture["file_projection"]["complete"])
        self.assertTrue(capture["last_assistant_message_truncated"])
        self.assertLess(len(capture["last_assistant_message"]), len(text))

    def test_restore_distinguishes_disabled_pending_existing_and_empty_public_events(self):
        from agent_learning.work_recovery import restore
        job = self.fail()
        capture = job["capture"]
        self.r.config["work_memory"]["enabled"] = False
        with patch.object(self.r, "_work_capture_event", side_effect=AssertionError("Disabled capture must stay private")):
            with self.r.transaction():
                result = restore(self.r, self.p, job["event_id"], self.workspace, reindex=True)
            self.assertEqual(result["status"], "deferred")
            self.assertFalse(result["indexed"])
            self.assertFalse(result["reindexed"])
            self.assertEqual(self.r.db.execute("SELECT capture FROM work_capture_pending").fetchone()[0], capture)
            self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], 0)
            page = self.r.work_rebuild(self.p, self.workspace)
            self.assertEqual((page["indexed"], page["reindexed"]), (0, 0))
            with self.r.transaction():
                empty, _ = self.r._event(self.p, "chat", "Stop", {})
                result = restore(self.r, self.p, empty["id"], self.workspace)
            self.assertEqual(result["status"], "not_applicable")
        self.r.config["work_memory"]["enabled"] = True
        with self.r.transaction():
            result = restore(self.r, self.p, job["event_id"], self.workspace)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["indexed"])
        before = tuple(self.r.db.execute("SELECT body,source_hash,workspace_key FROM work_memories").fetchone())
        self.r.config["work_memory"]["enabled"] = False
        with patch.object(self.r, "_work_capture_event", side_effect=AssertionError("Existing capture must not replay")):
            with self.r.transaction():
                result = restore(self.r, self.p, job["event_id"], self.workspace, reindex=True)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["already_captured"])
        self.assertTrue(result["reindexed"])
        self.assertEqual(before, tuple(self.r.db.execute("SELECT body,source_hash,workspace_key FROM work_memories").fetchone()))

    def test_not_applicable_pending_is_retired_without_counting_a_projection_repair(self):
        from agent_learning.work_recovery import register, recover
        with self.r.transaction():
            event, _ = self.r._event(self.p, "chat", "Stop", {})
            register(self.r, self.p, event["id"], self.workspace)
            repaired = recover(self.r, self.p)
        self.assertEqual(repaired, 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_capture_pending").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], 0)

    def test_disabled_uncaptured_public_text_requires_complete_matching_original_evidence(self):
        from agent_learning.work_recovery import restore
        self.r.config["work_memory"]["enabled"] = False
        variants = [({}, None, "not_applicable"), ({"truncated": True}, None, "deferred"),
                    ({"last_assistant_message_truncated": True}, None, "deferred"),
                    ({"capture_coverage": {"finalModelOutput": {"full_original_available": False}}}, None, "deferred"),
                    ({}, {"last_assistant_message": "A longer frozen public capture"}, "deferred")]
        for fields, raw, expected in variants:
            with self.subTest(fields=fields, raw=raw):
                with self.r.transaction():
                    event, _ = self.r._event(self.p, "chat", "Stop", {"last_assistant_message": "Available public text", **fields})
                    result = restore(self.r, self.p, event["id"], self.workspace, raw=raw)
                self.assertEqual(result["status"], expected)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories").fetchone()[0], 0)

    def test_root_rebuild_reuses_subdirectory_pre_links_and_post_cannot_inherit_a_guessed_path(self):
        sub = self.workspace / "src"
        sub.mkdir()
        (sub / "a.py").write_text("Original tool target", encoding="utf-8")
        (self.workspace / "a.py").write_text("Unrelated root file", encoding="utf-8")
        raw = {"tool_name": "write_file", "tool_input": {"path": "a.py"}}
        with self.r.transaction():
            pre, _ = self.r._event(self.p, "chat", "PreToolUse", raw, operation_id="subdirectory-operation")
            self.r._work_capture_safely(self.p, pre["id"], sub, raw=raw)
        original = [tuple(row) for row in self.r.db.execute("SELECT * FROM work_file_links")]
        (sub / "a.py").write_text("A later file version must not replace the original observation", encoding="utf-8")
        with patch.object(self.r, "_work_file_projection", side_effect=AssertionError("Reuse original file paths and metadata")):
            result = self.r.work_rebuild(self.p, self.workspace)
        self.assertEqual(result["indexed"], 0)
        self.assertEqual(original, [tuple(row) for row in self.r.db.execute("SELECT * FROM work_file_links")])
        raw = {"tool_name": "write_file", "tool_response": {"exit_code": 0,
               "output": "A meaningful update of the actual source target was observed."}}
        with self.r.transaction():
            post, _ = self.r._event(self.p, "chat", "PostToolUse", raw, outcome="succeeded",
                                    operation_id="subdirectory-operation")
            self.r._work_capture_safely(self.p, post["id"], sub, raw=raw)
        self.assertEqual([row[0] for row in self.r.db.execute("SELECT relative_path FROM work_file_links WHERE event_id=?",
                                                           (post["id"],))], ["src/a.py"])
        self.assertEqual(self.r.work_search(self.p, file_path="a.py", workspace=self.workspace)["items"], [])
        self.assertEqual(len(self.r.work_search(self.p, file_path="src/a.py", workspace=self.workspace)["items"]), 1)

    def test_rebuild_legacy_tool_requires_original_cwd_before_deriving_relative_targets(self):
        sub = self.workspace / "src"
        sub.mkdir()
        with self.r.transaction():
            unknown, _ = self.r._event(self.p, "chat", "PostToolUse", {
                "tool_name": "write_file", "tool_input": {"path": "a.py"}, "tool_response": {"exit_code": 0}},
                outcome="succeeded")
            self.r.db.execute("INSERT INTO work_event_checkouts VALUES(?,?,?)", (self.p, unknown["id"],
                checkout_key(self.workspace)))
            known, _ = self.r._event(self.p, "chat", "PostToolUse", {"cwd": str(sub),
                "tool_name": "write_file", "tool_input": {"path": "a.py"}, "tool_response": {"exit_code": 0}},
                outcome="succeeded")
        result = self.r.work_rebuild(self.p, self.workspace)
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_file_links WHERE event_id=?",
                                          (unknown["id"],)).fetchone()[0], 0)
        link = self.r.db.execute("SELECT relative_path,version FROM work_file_links WHERE event_id=?",
                                 (known["id"],)).fetchone()
        self.assertEqual(link["relative_path"], "src/a.py")
        self.assertEqual(json.loads(link["version"])["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
