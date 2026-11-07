"""Corrupt optional schedules cannot disable public reads or starve checks."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.mcp import Server
from agent_learning.runtime import Runtime


class WorkScheduleAuxiliaryTests(unittest.TestCase):
    def setUp(self):
        area = Path(__file__).resolve().parents[1] / "build/local/f64p/tests"
        area.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=area)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("tmp", "home", "codex", "runtime-releases", "project"):
            (self.root / name).mkdir()
        self.workspace = self.root / "project"
        (self.workspace / ".git").mkdir()
        environment = patch.dict(os.environ, {"ALP_HOME": str(self.root / "home"),
            "ALP_CONFIG": str(self.root / "config.json"), "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"),
            "CODEX_HOME": str(self.root / "codex"), "PYTHONDONTWRITEBYTECODE": "1",
            **{name: str(self.root / "tmp") for name in ("TMP", "TEMP", "TMPDIR")}})
        environment.start()
        self.addCleanup(environment.stop)
        self.config = {"admin": {"enabled": False}, "scheduler": {"enabled": False},
            "hindsight": {"enabled": False}, "injection_max_chars": 6000,
            "work_memory": {"max_items": 5, "max_context_chars": 1800}}
        self.runtime = Runtime(self.root / "home", config=self.config)
        self.addCleanup(lambda: self.runtime.close())
        self.project = self.runtime.project(self.workspace)
        self.runtime.task_update(self.project, "reader", "auxiliary schedule health", {"mode": "analysis"})
        self.handle = self.runtime.register_context(self.project, "reader")["context_handle"]
        self.server = Server(self.runtime, self.project, context_handle=self.handle, workspace=self.workspace)
        self.addCleanup(lambda: self.server.close())
        self.memories = []

    def seed(self, count, files=3):
        for index in range(count):
            paths = [self.workspace / ("item%d-%d.py" % (index, item)) for item in range(files)]
            for path in paths:
                path.write_text("original", encoding="utf-8")
            self.runtime.handle_hook({"hook_event_name": "PostToolUse", "session_id": "writer", "cwd": str(self.workspace),
                "tool_name": "apply_patch", "tool_use_id": "source%d" % index,
                "tool_input": "*** Begin Patch\n" + "".join("*** Update File: " + str(path) + "\n" for path in paths) + "*** End Patch",
                "tool_response": {"exit_code": 0, "output": "observed auxiliary source"}}, context_delivery=False)
            result = self.server.call("work_memory_search", {"file_path": str(paths[0]), "max_chars": 6000})
            self.assertEqual(len(result["items"]), 1)
            self.memories.append((result["items"][0]["memory_id"], paths))

    def authority(self):
        tables = ("events", "tasks", "task_snapshots", "operations", "knowledge", "active")
        return hashlib.sha256(json.dumps({table: [tuple(row) for row in self.runtime.db.execute(
            "SELECT * FROM " + table + " ORDER BY rowid")] for table in tables}, sort_keys=True).encode()).hexdigest()

    def updates(self):
        return self.runtime.work_exposure_updates(self.project, "reader", workspace=self.workspace)

    def reopen(self):
        self.server.close()
        self.runtime.close()
        self.runtime = Runtime(self.root / "home", config=self.config)
        self.server = Server(self.runtime, self.project, context_handle=self.handle, workspace=self.workspace)

    def test_half_json_delivery_receipt_recovers_via_bound_mcp_without_changing_authority(self):
        self.seed(1)
        memory, paths = self.memories[0]
        authority = self.authority()
        self.runtime.db.execute("UPDATE work_file_check_schedule SET file_states=?", ('{"half":',))
        found = self.server.call("work_memory_search", {"file_path": str(paths[0]), "max_chars": 6000})
        self.assertEqual(found["items"][0]["memory_id"], memory)
        repaired = json.loads(self.runtime.db.execute("SELECT file_states FROM work_file_check_schedule").fetchone()[0])
        self.assertEqual(len(repaired), 3)
        self.assertEqual(self.authority(), authority)
        self.assertEqual(self.updates(), [])

    def test_malformed_primary_and_partial_state_preserve_other_delivered_baselines(self):
        self.seed(1)
        memory, paths = self.memories[0]
        before = json.loads(self.runtime.db.execute("SELECT file_state FROM work_exposures").fetchone()[0])
        self.runtime.db.execute("UPDATE work_file_check_schedule SET file_states=?,primary_checkout=?", ('{"invalid":42}', 12))
        self.server.call("work_memory_search", {"file_path": str(paths[0]), "max_chars": 6000})
        after = json.loads(self.runtime.db.execute("SELECT file_state FROM work_exposures").fetchone()[0])
        self.assertEqual(after, before)
        self.assertEqual(len(after), 3)

    def test_partial_auxiliary_state_cannot_drop_hidden_delivered_file_baselines(self):
        self.seed(1, files=6)
        memory, paths = self.memories[0]
        for path in paths[3:]:
            self.server.call("work_memory_search", {"file_path": str(path), "max_chars": 6000})
        before = json.loads(self.runtime.db.execute("SELECT file_state FROM work_exposures").fetchone()[0])
        self.assertEqual(len(before), 6)
        saved = json.loads(self.runtime.db.execute("SELECT file_states FROM work_file_check_schedule").fetchone()[0])
        saved = dict(list(saved.items())[:1])
        self.runtime.db.execute("UPDATE work_file_check_schedule SET file_states=?", (json.dumps(saved),))
        self.server.call("work_memory_search", {"file_path": str(paths[0]), "max_chars": 6000})
        after = json.loads(self.runtime.db.execute("SELECT file_state FROM work_exposures").fetchone()[0])
        self.assertEqual(after, before)

    def test_delivery_write_failure_cannot_acknowledge_after_half_json_repair(self):
        self.seed(1)
        memory, paths = self.memories[0]
        self.runtime.db.execute("UPDATE work_file_check_schedule SET file_states=?", ('{"half":',))
        before = [tuple(row) for row in self.runtime.db.execute("SELECT * FROM work_exposures")]
        self.runtime.db.execute("CREATE TRIGGER fail_aux BEFORE UPDATE OF file_states ON work_file_check_schedule "
                                "BEGIN SELECT RAISE(FAIL,'synthetic'); END")
        paths[0].write_text("changed but the optional receipt storage is unavailable", encoding="utf-8")
        self.server.call("work_memory_search", {"file_path": str(paths[0]), "max_chars": 6000})
        self.assertEqual([tuple(row) for row in self.runtime.db.execute("SELECT * FROM work_exposures")], before)
        self.assertEqual(self.runtime.db.execute("SELECT file_states FROM work_file_check_schedule").fetchone()[0], '{"half":')

    def test_text_rank_does_not_break_public_hook_or_hide_changed_file(self):
        self.seed(2)
        memory, paths = self.memories[0]
        paths[0].write_text("changed under a nonnumeric schedule rank", encoding="utf-8")
        self.runtime.db.execute("UPDATE work_file_check_schedule SET last_checked='NaN' WHERE memory_id=?", (memory,))
        response = self.runtime.handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "reader", "cwd": str(self.workspace),
            "turn_id": "rank-health", "prompt": "continue auxiliary health"})
        self.assertIn(memory, json.dumps(response))

    def test_bad_ranks_are_fair_across_restart_with_256_file_callback_limit(self):
        self.seed(100)
        memory, paths = self.memories[0]  # Oldest row: tie order alone must not privilege it.
        before = self.authority()
        from agent_learning.work_memory import current_file_version
        for rank in ("NaN", -1e300, 1e300, float("inf")):
            with self.subTest(rank=rank):
                self.runtime.db.execute("UPDATE work_file_check_schedule SET last_checked=0")
                self.runtime.db.execute("UPDATE work_file_check_schedule SET last_checked=? WHERE memory_id=?", (rank, memory))
                paths[0].write_text("changed under malformed rank " + str(rank), encoding="utf-8")
                with patch("agent_learning.work_memory.current_file_version", wraps=current_file_version) as observed:
                    first = self.updates()
                    self.assertLessEqual(observed.call_count, 256)
                self.reopen()
                with patch("agent_learning.work_memory.current_file_version", wraps=current_file_version) as observed:
                    second = self.updates()
                    self.assertLessEqual(observed.call_count, 256)
                self.assertIn(memory, {item["memory_id"] for item in first + second})
        self.assertEqual(self.authority(), before)

    def test_schedule_select_excludes_ten_thousand_orphans_with_indexed_identity_probes(self):
        self.seed(2)
        self.runtime.db.executemany("INSERT INTO work_file_check_schedule(project,conversation,agent,memory_id) VALUES(?,?,?,?)",
            ((self.project, "reader", "", "orphan-%d" % index) for index in range(10_000)))
        statements, steps = [], [0]
        self.runtime.db.set_trace_callback(statements.append)
        self.runtime.db.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 100)
        self.updates()
        self.runtime.db.set_trace_callback(None)
        self.runtime.db.set_progress_handler(None, 0)
        selects = [query for query in statements if query.startswith("SELECT memory_id,last_checked")]
        self.assertEqual(len(selects), 1)
        self.assertIn("memory_id IN (", selects[0])
        self.assertLess(steps[0] * 100, 20_000)
        plan = [row[3] for row in self.runtime.db.execute("EXPLAIN QUERY PLAN " + selects[0])]
        self.assertTrue(any("memory_id=?" in detail for detail in plan), plan)

    def test_rebase_failure_preserves_core_and_advances_bounded_fallback(self):
        self.seed(3)
        self.runtime.db.execute("UPDATE work_file_check_schedule SET last_checked=1e300")
        before = self.authority()
        core = [tuple(row) for row in self.runtime.db.execute("SELECT * FROM work_exposures ORDER BY memory_id")]
        self.runtime.db.execute("CREATE TRIGGER fail_rank BEFORE UPDATE OF last_checked ON work_file_check_schedule "
                                "BEGIN SELECT RAISE(FAIL,'synthetic'); END")
        with patch("agent_learning.work_memory.WORK_EXPOSURE_CALLBACK_FILE_LIMIT", 3):
            self.updates()
            first = self.runtime._work_file_check_fallback[(self.project, "reader", "")]
            self.updates()
            second = self.runtime._work_file_check_fallback[(self.project, "reader", "")]
        self.assertGreater(second, first)
        self.assertEqual(self.authority(), before)
        self.assertEqual([tuple(row) for row in self.runtime.db.execute("SELECT * FROM work_exposures ORDER BY memory_id")], core)
        self.runtime.db.execute("DROP TRIGGER fail_rank")
        self.updates()
        self.assertNotIn((self.project, "reader", ""), self.runtime._work_file_schedule_failures)


if __name__ == "__main__":
    unittest.main()
