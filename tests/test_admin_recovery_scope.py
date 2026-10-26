"""Retained attempts are not a global host stop; current locks remain strict."""
import json
import time
import unittest
from unittest.mock import patch

from agent_learning.admin_lifecycle import claim_call
from agent_learning.admin_queue import (dispatch_diagnostic, eligible_query, review_protection_status,
                                        scan_signatures)
from agent_learning.admin_recovery import blocked_review_summary
from agent_learning.phase_review import configure, policy, prepare
from agent_learning.runtime import encode
from tests import test_admin_recovery as recovery_fixtures
from tests import test_phase_review as phase_fixtures


class ProtectionFixtures:
    def failure(self, *, problem=None, state="timeout_unknown", host=None, attributed=True):
        number = self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0]
        problem = problem or self.r.feedback(self.p, "source-" + str(number), "Independent fixture " + str(number))
        with self.r.transaction():
            call = claim_call(self.r, self.p, problem["id"], problem["revision"],
                              "failure-" + str(number), self.r.host_epoch(), 10)
            if attributed:
                executor = self.r.resolve_admin_executor(self.p, require_enabled=False)
                self.r.record_admin_execution(self.p, call["call_id"], executor, require_enabled=False)
                if host:
                    self.r.db.execute("UPDATE admin_execution_plans SET host_scope=? WHERE call_id=?",
                                      (host, call["call_id"]))
            self.r.db.execute("UPDATE admin_calls SET state=?,started=?,finished=?,lease_until=NULL,usage=? WHERE id=?",
                (state, time.time(), time.time(), encode({"reported_turns": 0, "completeness": "unknown"}), call["call_id"]))
        return call["call_id"], problem

    def protection(self):
        return review_protection_status(self.r, self.p, *scan_signatures(self.r, self.p))

    def call(self, identity):
        return dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (identity,)).fetchone())


class AdminRecoveryScopeTests(ProtectionFixtures, unittest.TestCase):
    setUp = recovery_fixtures.AdminRecoveryTests.setUp

    def test_current_case_lock_is_still_a_real_queue_block(self):
        identity, _ = self.failure()
        before = self.call(identity)
        result = self.protection()
        self.assertTrue(result["queue_blocked_by_protected_calls"])
        self.assertEqual(result["reason"], "unknown_call_requires_review")
        self.assertEqual(result["blocked_review_summary"]["result_unknown"], 1)
        self.assertEqual(self.call(identity), before)

    def test_completed_problem_retains_protection_without_claiming_current_block(self):
        identity, problem = self.failure()
        self.r.db.execute("UPDATE problems SET status='reviewed' WHERE id=?", (problem["id"],))
        before = self.call(identity)
        self.r.admin_configure(self.p, enabled=True)
        result = dispatch_diagnostic(self.r, self.p)
        self.assertFalse(result["queue_blocked_by_protected_calls"])
        self.assertEqual(result["reason"], "protected_history_only")
        self.assertEqual(result["blocked_review_summary"]["total"], 0)
        self.assertEqual(result["protected_review_summary"]["result_unknown"], 1)
        self.assertNotIn("blocked_review_scope", result)
        self.assertEqual(self.call(identity), before)

    def test_superseded_revision_does_not_block_current_revision(self):
        identity, problem = self.failure()
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (problem["id"],))
        result = self.protection()
        self.assertFalse(result["queue_blocked_by_protected_calls"])
        self.assertEqual(result["reason"], "protected_history_only")
        self.assertEqual(self.call(identity)["retry_allowed"], 0)
        query, args = eligible_query(*scan_signatures(self.r, self.p)[:1])
        self.assertTrue(self.r.db.execute(query + "LIMIT 1", (self.p, *args)).fetchone())

    def test_other_host_history_does_not_pollute_current_host(self):
        self.failure(host="antigravity:default")
        result = self.protection()
        self.assertFalse(result["queue_blocked_by_protected_calls"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["protected_review_summary"]["total"], 0)

    def test_unattributed_current_lock_is_project_protection_not_guessed_host(self):
        self.failure(attributed=False)
        result = self.protection()
        self.assertTrue(result["queue_blocked_by_protected_calls"])
        self.assertEqual(result["blocked_review_scope"], "project_unattributed")
        self.assertEqual(result["blocked_review_summary"]["total"], 0)
        self.assertEqual(result["blocked_review_summary"]["unattributed"]["result_unknown"], 1)

    def test_local_scan_cache_cannot_hide_a_real_current_call_lock(self):
        identity, problem = self.failure(state="failed")
        signature, _ = scan_signatures(self.r, self.p)
        self.r.db.execute("INSERT OR REPLACE INTO admin_review_checks VALUES(?,?,?,?,?,?,?)",
            (self.p, problem["id"], problem["revision"], signature, None, time.time(), identity))
        result = self.protection()
        self.assertTrue(result["queue_blocked_by_protected_calls"])
        self.assertEqual(result["reason"], "failed_call_requires_review")

    def test_running_tool_keeps_its_own_wait_reason(self):
        self.r.task_update(self.p, "source-running", "Wait for the source tool")
        problem = self.r.feedback(self.p, "source-running", "Observe current source before review")
        identity, _ = self.failure(problem=problem)
        self.r.handle_hook({"hook_event_name": "PreToolUse", "session_id": "source-running",
            "cwd": str(self.workspace), "tool_use_id": "running-fixture", "tool_name": "Bash",
            "tool_input": {"command": "synthetic only"}})
        self.r.admin_configure(self.p, enabled=True)
        before = self.call(identity)
        self.assertEqual(dispatch_diagnostic(self.r, self.p)["reason"], "awaiting_tool_completion")
        self.assertEqual(blocked_review_summary(self.r, self.p)["result_unknown"], 1)
        self.assertEqual(self.call(identity), before)

    def test_current_preflight_requires_current_revision_and_signature(self):
        problem = self.r.feedback(self.p, "preflight", "Current evidence still needs confirmation")
        self.r.admin_configure(self.p, enabled=True)
        signature, preflight = scan_signatures(self.r, self.p)
        self.r.db.execute("INSERT INTO admin_preflights VALUES(?,?,?,?,?,?,?)",
            (self.p, problem["id"], problem["revision"], preflight, None, "awaiting_evidence", time.time()))
        result = self.protection()
        self.assertTrue(result["awaiting_evidence"])
        self.assertEqual(result["reason"], "awaiting_evidence")
        self.r.db.execute("UPDATE admin_preflights SET signature='old-configuration'")
        self.assertFalse(self.protection()["awaiting_evidence"])
        self.r.db.execute("UPDATE admin_preflights SET signature=?,revision=revision+1", (preflight,))
        self.assertFalse(self.protection()["awaiting_evidence"])

    def test_read_only_projection_never_starts_models_or_changes_attempt_usage(self):
        identity, _ = self.failure()
        before, changes = self.call(identity), self.r.db.total_changes
        with patch("subprocess.Popen") as model:
            self.protection()
        model.assert_not_called()
        self.assertEqual(self.call(identity), before)
        self.assertEqual(self.r.db.total_changes, changes)

    def test_thousands_of_historical_attempts_use_bounded_sql_work(self):
        identity, problem = self.failure()
        self.r.db.execute("UPDATE problems SET status='reviewed' WHERE id=?", (problem["id"],))
        call = self.call(identity)
        plan = dict(self.r.db.execute("SELECT * FROM admin_execution_plans WHERE call_id=?", (identity,)).fetchone())
        with self.r.transaction():
            for table, row, key in (("admin_calls", call, "id"), ("admin_execution_plans", plan, "call_id")):
                columns = list(row)
                values = [[("historical-" + str(i)) if name == key else row[name] for name in columns]
                          for i in range(3000)]
                self.r.db.executemany("INSERT INTO " + table + "(" + ",".join(columns) + ") VALUES("
                                      + ",".join("?" for _ in columns) + ")", values)
        steps = 0
        def progress():
            nonlocal steps
            steps += 1000
            return steps > 5000000
        changes = self.r.db.total_changes
        self.r.db.set_progress_handler(progress, 1000)
        try:
            result = self.protection()
        finally:
            self.r.db.set_progress_handler(None, 0)
        self.assertFalse(result["queue_blocked_by_protected_calls"])
        self.assertEqual(result["protected_review_summary"]["result_unknown"], 3001)
        self.assertLess(steps, 5000000)
        self.assertEqual(self.r.db.total_changes, changes)


class PhaseRecoveryScopeTests(ProtectionFixtures, unittest.TestCase):
    setUp = phase_fixtures.PhaseReviewTests.setUp
    phase = phase_fixtures.PhaseReviewTests.phase

    def pending(self):
        self.phase("one")
        self.phase("two", "Another independent sourced convention")
        with self.r.transaction():
            prepare(self.r, self.p)
        row = self.r.db.execute("SELECT p.* FROM problems p JOIN phase_review_batches b ON b.problem_id=p.id "
                                "WHERE b.state='pending' LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def test_legacy_calls_are_only_historical_protection_in_phase_mode(self):
        identity, _ = self.failure()
        self.assertEqual(policy(self.r, self.p)["mode"], "phase")
        result = self.protection()
        self.assertFalse(result["queue_blocked_by_protected_calls"])
        self.assertEqual(result["reason"], "protected_history_only")
        self.assertEqual(self.call(identity)["retry_allowed"], 0)

    def test_legacy_preflights_and_review_states_do_not_make_phase_mode_wait(self):
        _, problem = self.failure()
        _, preflight = scan_signatures(self.r, self.p)
        self.r.db.execute("INSERT INTO admin_preflights VALUES(?,?,?,?,?,?,?)",
            (self.p, problem["id"], problem["revision"], preflight, None, "awaiting_evidence", time.time()))
        self.r.db.execute("INSERT INTO review_states VALUES(?,?,?,?,?,?,?)",
            (self.p, problem["id"], problem["revision"], "old-review", "awaiting_evidence", "fixture", time.time()))
        result = dispatch_diagnostic(self.r, self.p)
        self.assertEqual(result["reason"], "protected_history_only")
        self.assertFalse(result["awaiting_evidence"])

    def test_legacy_running_tools_do_not_make_phase_queue_wait(self):
        self.r.task_update(self.p, "legacy-tool", "Old work is still running")
        self.r.feedback(self.p, "legacy-tool", "An unrelated operation source")
        self.r.handle_hook({"hook_event_name": "PreToolUse", "session_id": "legacy-tool",
            "cwd": str(self.workspace), "tool_use_id": "old-running", "tool_name": "Bash",
            "tool_input": {"command": "synthetic only"}})
        result = dispatch_diagnostic(self.r, self.p)
        self.assertEqual(result["reason"], "no_increment")
        self.assertFalse(result["awaiting_tool_completion"])

    def test_current_pending_phase_batch_keeps_unknown_review_requirement(self):
        identity, _ = self.failure(problem=self.pending())
        before = self.call(identity)
        result = self.protection()
        self.assertTrue(result["queue_blocked_by_protected_calls"])
        self.assertEqual(result["reason"], "unknown_call_requires_review")
        self.assertEqual(self.call(identity), before)

    def test_old_batch_state_or_epoch_does_not_claim_current_queue_block(self):
        _, problem = self.failure(problem=self.pending())
        for column, value in (("state", "failed"), ("session_epoch", self.r.session_epoch() + 1),
                              ("governance_epoch", self.r.host_epoch() + 1)):
            with self.subTest(column=column):
                row = dict(self.r.db.execute("SELECT * FROM phase_review_batches WHERE problem_id=?", (problem["id"],)).fetchone())
                self.r.db.execute("UPDATE phase_review_batches SET " + column + "=? WHERE problem_id=?", (value, problem["id"]))
                self.assertFalse(self.protection()["queue_blocked_by_protected_calls"])
                self.r.db.execute("UPDATE phase_review_batches SET " + column + "=? WHERE problem_id=?", (row[column], problem["id"]))


if __name__ == "__main__":
    unittest.main()
