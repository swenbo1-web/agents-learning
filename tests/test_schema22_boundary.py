"""Upgrade new derived state without changing knowledge, policy or paid records."""
import copy
import sqlite3
import unittest
from unittest.mock import patch

from agent_learning import candidate_references as refs
from agent_learning.runtime import ContractError, Runtime
from tests import test_candidate_references as fixtures


class Schema22BoundaryTests(unittest.TestCase):
    setUp = fixtures.CandidateReferenceTests.setUp
    candidate = fixtures.CandidateReferenceTests.candidate
    enable = fixtures.CandidateReferenceTests.enable

    def old_database(self):
        identity, _, _, _, _ = self.candidate()
        self.enable()
        refs.record_exposure(self.r, self.p, self.handle, refs.assess(self.r, self.p, identity))
        for column in ("environment_state", "pending_notice"):
            self.r.db.execute("ALTER TABLE candidate_reference_exposures DROP COLUMN " + column)
        self.r.db.execute("ALTER TABLE phase_scan_progress DROP COLUMN last_checked")
        self.r.db.execute("PRAGMA user_version=21")
        return identity

    def test_schema21_upgrade_preserves_candidates_policies_and_fences_old_handles_once(self):
        identity = self.old_database()
        original = tuple(self.r.db.execute("SELECT * FROM candidates WHERE id=?", (identity,)).fetchone())
        policy = refs.policy(self.r, self.p)
        epoch = self.r.session_epoch()
        with patch("subprocess.Popen", side_effect=AssertionError("upgrade cannot start a model")):
            upgraded = Runtime(self.r.home, config=copy.deepcopy(self.r.config))
        self.addCleanup(upgraded.close)
        self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 24)
        self.assertEqual(tuple(upgraded.db.execute("SELECT * FROM candidates WHERE id=?", (identity,)).fetchone()), original)
        self.assertEqual(refs.policy(upgraded, self.p), policy)
        self.assertGreater(upgraded.session_epoch(), epoch)
        with self.assertRaises(ContractError):
            upgraded.access_context(self.p, self.handle)
        self.assertEqual(upgraded.db.execute("SELECT COUNT(*) FROM candidate_reference_exposures").fetchone()[0], 1)
        self.assertEqual(upgraded.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
        reopened = Runtime(self.r.home, config=copy.deepcopy(self.r.config))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.session_epoch(), upgraded.session_epoch())

    def test_failed_upgrade_rolls_back_new_columns_and_epoch(self):
        self.old_database()
        epoch = self.r.session_epoch()
        install = refs.install

        def interrupted(runtime):
            install(runtime)
            raise sqlite3.OperationalError("simulated migration interruption")

        with patch.object(refs, "install", side_effect=interrupted), self.assertRaises(sqlite3.OperationalError):
            Runtime(self.r.home, config=copy.deepcopy(self.r.config))
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 21)
        self.assertEqual(self.r.session_epoch(), epoch)
        self.assertNotIn("pending_notice", {row[1] for row in self.r.db.execute("PRAGMA table_info(candidate_reference_exposures)")})
        self.assertNotIn("last_checked", {row[1] for row in self.r.db.execute("PRAGMA table_info(phase_scan_progress)")})
        self.assertTrue(self.r.access_context(self.p, self.handle))
