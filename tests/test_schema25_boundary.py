"""Recovery/provenance upgrade preserves original evidence and paid receipts."""
import copy
import sqlite3
import unittest
from unittest.mock import patch

from agent_learning.runtime import ContractError, Runtime
from tests import test_schema24_boundary as fixtures


class Schema25BoundaryTests(unittest.TestCase):
    setUp = fixtures.Schema24BoundaryTests.setUp
    failed = fixtures.Schema24BoundaryTests.failed
    phase = fixtures.Schema24BoundaryTests.phase
    snapshot = fixtures.Schema24BoundaryTests.snapshot
    open_runtime = fixtures.Schema24BoundaryTests.open_runtime

    def old_database(self):
        job, call = fixtures.Schema24BoundaryTests.old_database(self)
        for row in self.r.db.execute("SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND (name LIKE 'knowledge_lineage_%' OR name='phase_completed_source_change')").fetchall():
            self.r.db.execute('DROP TRIGGER "' + row[0] + '"')
        for table in ("phase_seal_requests", "phase_source_dirty", "phase_source_projection",
                      "dsh_bridge_bindings", "candidate_lineage_dependencies", "knowledge_lineage_projection_state"):
            self.r.db.execute("DROP TABLE IF EXISTS " + table)
        self.r.db.execute("PRAGMA user_version=24")
        self.r.db.execute("UPDATE runtime_storage_receipt SET signature=? WHERE id=1",
                          ("1.4.1:schema24:knowledge4:work1:startup9",))
        return job, call

    def test_upgrade_installs_recovery_and_lineage_without_rewriting_authority(self):
        self.old_database()
        before = self.snapshot()
        old_epoch = self.r.session_epoch()
        with patch("subprocess.Popen", side_effect=AssertionError("Upgrade cannot call a model")):
            upgraded = self.open_runtime()
        self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 25)
        self.assertEqual(self.snapshot(upgraded), before)
        self.assertGreater(upgraded.session_epoch(), old_epoch)
        for table in ("phase_seal_requests", "phase_source_dirty", "dsh_bridge_bindings",
                      "candidate_lineage_dependencies", "knowledge_lineage_projection_state"):
            self.assertIsNotNone(upgraded.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
        with self.assertRaises(ContractError):
            upgraded.access_context(self.p, self.handle)
        reopened = self.open_runtime()
        self.assertEqual(reopened.session_epoch(), upgraded.session_epoch())
        self.assertEqual(self.snapshot(reopened), before)

    def test_failed_migration_rolls_back_new_tables_and_governance_boundary(self):
        self.old_database()
        before = self.snapshot()
        old_epoch = self.r.session_epoch()
        from agent_learning import runtime_storage
        with patch.object(runtime_storage, "mark_storage_ready", side_effect=sqlite3.OperationalError("Simulated commit failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.open_runtime()
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 24)
        self.assertEqual(self.r.session_epoch(), old_epoch)
        self.assertEqual(self.snapshot(), before)
        for table in ("phase_seal_requests", "phase_source_dirty", "dsh_bridge_bindings", "candidate_lineage_dependencies"):
            self.assertIsNone(self.r.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone())

    def test_unknown_future_schema_is_rejected_without_migration(self):
        self.old_database()
        self.r.db.execute("PRAGMA user_version=26")
        before = self.snapshot()
        with patch.object(Runtime, "_initialize_storage") as initialize:
            with self.assertRaises(ContractError):
                self.open_runtime()
            self.assertEqual(initialize.call_count, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 26)


if __name__ == "__main__":
    unittest.main()
