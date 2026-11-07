"""Sparse conversation diagnostics remain indexed without a host filter."""
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_learning.runtime import Runtime
from agent_learning.runtime_storage import _signature, storage_ready
from agent_learning.work_file_diagnostics import install, summary


class FileDiagnosticScaleTests(unittest.TestCase):
    def test_sparse_conversation_without_host_uses_bounded_index_and_stays_read_only(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        install(SimpleNamespace(db=db))
        db.executemany("INSERT INTO work_file_observations VALUES(?,?,?,?,?,?,?,?,?)", (
            ("p", ("codex", "dsh")[i % 2], "rare" if i == 0 else "common", str(i), i,
             1, 0, 1, '{"outside_workspace":1}') for i in range(100_000)))
        db.commit()
        before = db.total_changes
        db.execute("PRAGMA query_only=ON")
        statements = []
        db.set_trace_callback(statements.append)
        steps = [0]
        db.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 100)
        result = summary(SimpleNamespace(db=db), "p", conversation="rare")
        db.set_progress_handler(None, 0)
        db.set_trace_callback(None)
        queries = [query for query in statements if query.startswith("SELECT")]
        self.assertEqual(len(queries), 1)
        plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + queries[0])]
        self.assertTrue(any("work_file_observation_conversation" in detail for detail in plan), plan)
        self.assertFalse(any("TEMP B-TREE" in detail for detail in plan), plan)
        self.assertLess(steps[0] * 100, 1_000)
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["reasons"], {"outside_workspace": 1})
        self.assertEqual(summary(SimpleNamespace(db=db), "p", conversation="rare", host="dsh")["events"], 0)
        self.assertEqual(summary(SimpleNamespace(db=db), "foreign", conversation="rare")["events"], 0)
        self.assertEqual(db.total_changes, before)

    def test_new_index_install_is_idempotent_and_keeps_derived_record_contract(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        view = SimpleNamespace(db=db)
        install(view)
        columns = [row[1] for row in db.execute("PRAGMA table_info(work_file_observations)")]
        db.execute("INSERT INTO work_file_observations VALUES(?,?,?,?,?,?,?,?,?)",
                   ("p", "codex", "c", "event", 1, 1, 0, 1, '{}'))
        install(view)
        self.assertEqual([row[1] for row in db.execute("PRAGMA table_info(work_file_observations)")], columns)
        self.assertEqual(db.execute("SELECT count(*) FROM work_file_observations").fetchone()[0], 1)

    def test_schema25_version62_receipt_gets_new_index_on_version63_open(self):
        base = Path(__file__).resolve().parents[1] / "build/local/r63scale"
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        environment = patch.dict(os.environ, {
            "ALP_HOME": str(folder / "h"), "ALP_CONFIG": str(folder / "config.json"),
            "ALP_RUNTIME_STORE": str(folder / "r"), "CODEX_HOME": str(folder / "c"),
            "TMP": str(base), "TEMP": str(base), "TMPDIR": str(base), "PYTHONDONTWRITEBYTECODE": "1"})
        with environment:
            primary = folder / "p"
            primary.mkdir()
            runtime = Runtime(folder / "h", config={})
            project = runtime.project(primary)
            paths = [tuple(row) for row in runtime.db.execute("SELECT * FROM project_paths ORDER BY path")]
            runtime.db.execute("DROP INDEX work_file_observation_conversation")
            old_signature = "1.4.62:" + _signature().split(":", 1)[1]
            runtime.db.execute("UPDATE runtime_storage_receipt SET signature=?,schema_cookie=? WHERE id=1",
                               (old_signature, runtime.db.execute("PRAGMA schema_version").fetchone()[0]))
            with patch("agent_learning.runtime_storage._signature", return_value=old_signature):
                self.assertTrue(storage_ready(runtime))
            runtime.close()
            runtime = Runtime(folder / "h", config={})
            try:
                self.assertTrue(runtime.db.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='work_file_observation_conversation'").fetchone())
                self.assertEqual(runtime.db.execute("PRAGMA user_version").fetchone()[0], 25)
                self.assertEqual(runtime.project(primary), project)
                self.assertEqual([tuple(row) for row in runtime.db.execute("SELECT * FROM project_paths ORDER BY path")], paths)
                self.assertEqual(runtime.db.execute("SELECT signature FROM runtime_storage_receipt").fetchone()[0], _signature())
                self.assertEqual(runtime.db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
