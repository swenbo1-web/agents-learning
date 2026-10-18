"""New review settings remain safe across old stores and concurrent clients."""
import copy
import unittest

from agent_learning.admin_control import apply_control, control_status, readonly_runtime
from agent_learning.phase_review import configure, policy
from agent_learning.runtime import ContractError
from tests import test_phase_review as fixtures


class PhaseCompatibilityTests(unittest.TestCase):
    setUp = fixtures.PhaseReviewTests.setUp

    def body(self, request_id):
        current = control_status(self.r, self.p, details=False)
        return {"request_id": request_id, "expected_policy_version": current["policy"]["policy_version"],
            "expected_settings_version": current["settings_version"], "expected_session_epoch": current["session_epoch"],
            "expected_governance_epoch": current["governance_epoch"],
            "expected_project_budget_version": current["project_budget_policy"]["version"],
            "enabled": True, "model_mode": "default", "model": "", "start": False, "project_limits": {},
            "phase_review": {"mode": "phase", "every_phases": 3},
            "expected_phase_version": current["phase_review"]["policy"]["version"]}

    def test_phase_only_edit_moves_settings_version_and_rejects_stale_client(self):
        first = self.body("first-save")
        apply_control(self.r, self.p, first, details=False)
        next_body = self.body("next-save")
        stale = copy.deepcopy(next_body)
        stale["request_id"] = "stale-save"
        next_body["phase_review"]["every_phases"] = 4
        saved = apply_control(self.r, self.p, next_body, details=False)
        self.assertGreater(saved["settings"]["settings_version"], stale["expected_settings_version"])
        with self.assertRaisesRegex(ContractError, "settings changed"):
            apply_control(self.r, self.p, stale, details=False)
        self.assertEqual(policy(self.r, self.p)["every_phases"], 4)

    def test_readonly_pre_v20_settings_do_not_query_missing_phase_tables_or_migrate(self):
        for table in ("phase_review_members", "phase_review_batches", "review_phases", "phase_review_policies"):
            self.r.db.execute("DROP TABLE " + table)
        self.r.db.execute("PRAGMA user_version=19")
        view = readonly_runtime(self.r.home / "runtime.sqlite3", self.r.db, "codex", "default")
        before = self.r.db.total_changes
        result = control_status(view, self.p)
        self.assertEqual(result["phase_review"]["policy"]["mode"], "legacy")
        self.assertEqual(view.db.total_changes, before)
        self.assertEqual(view.db.execute("PRAGMA user_version").fetchone()[0], 19)
        self.assertIsNone(view.db.execute("SELECT 1 FROM sqlite_master WHERE name='review_phases'").fetchone())

    def test_phase_configuration_rejected_atomically_while_call_is_running(self):
        from agent_learning.admin_lifecycle import claim_call
        problem = self.r.feedback(self.p, "active", "Sourced work")
        with self.r.transaction():
            claimed = claim_call(self.r, self.p, problem["id"], problem["revision"], "locked", self.r.host_epoch(), 30)
        self.assertEqual(claimed["status"], "reserved")
        before = policy(self.r, self.p)
        with self.assertRaisesRegex(ContractError, "current administrator call"):
            configure(self.r, self.p, {"every_phases": 6}, before["version"])
        self.assertEqual(policy(self.r, self.p), before)


if __name__ == "__main__":
    unittest.main()
