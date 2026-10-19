"""Historical-response grants migrate atomically without changing paid records."""
import copy
import sqlite3
import unittest
from unittest.mock import patch

from agent_learning.runtime import ContractError, Runtime
from tests import test_history_response_recovery as fixtures


class Schema23BoundaryTests(unittest.TestCase):
    setUp = fixtures.HistoryResponseRecoveryTests.setUp
    failed = fixtures.HistoryResponseRecoveryTests.failed

    def old_database(self):
        job, call = self.failed()
        for name in ("history_response_artifacts", "history_response_accepts"):
            self.r.db.execute("DROP TABLE " + name)
        self.r.db.execute("PRAGMA user_version=22")
        return job, call

    def test_schema22_upgrade_preserves_sources_policies_and_paid_records(self):
        job, call = self.old_database()
        paid = tuple(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call,)).fetchone())
        batch = tuple(self.r.db.execute("SELECT * FROM history_jobs WHERE id=?", (job,)).fetchone())
        policy = self.r.admin_effective_config(self.p)
        routing = self.r.admin_policy(self.p)
        plan = self.r.admin_execution_plan(self.p, call)
        prior_epoch = self.r.session_epoch()
        with patch("subprocess.Popen", side_effect=AssertionError("Migration cannot run a model")):
            upgraded = Runtime(self.r.home, config=copy.deepcopy(self.r.config))
        self.addCleanup(upgraded.close)
        self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 25)
        self.assertEqual(tuple(upgraded.db.execute("SELECT * FROM admin_calls WHERE id=?", (call,)).fetchone()), paid)
        self.assertEqual(tuple(upgraded.db.execute("SELECT * FROM history_jobs WHERE id=?", (job,)).fetchone()), batch)
        current = upgraded.admin_effective_config(self.p)
        for key, value in policy.items():
            # The newly resolved executor has fresh epoch/signature values;
            # persisted operator policy and the original frozen plan do not.
            if key != "executor":
                self.assertEqual(current[key], value, key)
        self.assertEqual(upgraded.admin_policy(self.p), routing)
        self.assertEqual(upgraded.admin_execution_plan(self.p, call), plan)
        self.assertGreater(upgraded.session_epoch(), prior_epoch)
        for name in ("history_response_artifacts", "history_response_accepts"):
            self.assertEqual(upgraded.db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0], 0)
        reopened = Runtime(self.r.home, config=copy.deepcopy(self.r.config))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.session_epoch(), upgraded.session_epoch())

    def test_interrupted_install_rolls_back_grants_schema_and_epoch(self):
        self.old_database()
        from agent_learning import history_response_recovery as recovery
        install = recovery.install
        epoch = self.r.session_epoch()

        def interrupted(runtime):
            install(runtime)
            raise sqlite3.OperationalError("Simulated grant migration interruption")

        with patch.object(recovery, "install", side_effect=interrupted), self.assertRaises(sqlite3.OperationalError):
            Runtime(self.r.home, config=copy.deepcopy(self.r.config))
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 22)
        self.assertEqual(self.r.session_epoch(), epoch)
        for name in ("history_response_artifacts", "history_response_accepts"):
            self.assertIsNone(self.r.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone())

    def test_newer_unknown_schema_is_rejected_before_creating_tables(self):
        self.old_database()
        self.r.db.execute("PRAGMA user_version=26")
        with self.assertRaises(ContractError):
            Runtime(self.r.home, config=copy.deepcopy(self.r.config))
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 26)
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM sqlite_master WHERE name='history_response_accepts'").fetchone())


if __name__ == "__main__":
    unittest.main()
