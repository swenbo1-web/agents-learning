"""Scoped recovery explanations do not grant retries or start paid work."""
import time
import unittest
from unittest.mock import patch

from agent_learning.admin_control import _control_queue
from agent_learning.admin_lifecycle import claim_call
from agent_learning.admin_queue import dispatch_diagnostic
from agent_learning.admin_recovery import blocked_review_summary, recovery_status
from agent_learning.runtime import encode
from tests import test_admin_recovery as recovery_fixtures
from tests import test_admin_response_store as response_fixtures


class RecoveryClassificationTests(unittest.TestCase):
    setUp = recovery_fixtures.AdminRecoveryTests.setUp

    def failure(self, state="failed", usage=None, *, host_scope=None, attributed=True):
        number = self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0]
        problem = self.r.feedback(self.p, "failed-" + str(number), "Independent failed review " + str(number))
        with self.r.transaction():
            call = claim_call(self.r, self.p, problem["id"], problem["revision"],
                              "independent-" + str(number), self.r.host_epoch(), 10)
            if attributed:
                plan = self.r.resolve_admin_executor(self.p, require_enabled=False)
                self.r.record_admin_execution(self.p, call["call_id"], plan, require_enabled=False)
                if host_scope:
                    self.r.db.execute("UPDATE admin_execution_plans SET host_scope=? WHERE call_id=?",
                                      (host_scope, call["call_id"]))
            self.r.db.execute("UPDATE admin_calls SET state=?,started=?,finished=?,lease_until=NULL,usage=? WHERE id=?",
                              (state, time.time(), time.time(), encode(usage or {}), call["call_id"]))
        return call["call_id"]

    def enable(self):
        self.r.admin_configure(self.p, enabled=True)

    def test_failed_and_unknown_are_separate_without_mutating_attempts(self):
        failed = self.failure()
        unknown = self.failure("timeout_unknown")
        rows = [tuple(r) for r in self.r.db.execute("SELECT * FROM admin_calls ORDER BY id")]
        before = self.r.db.total_changes
        with patch("subprocess.Popen") as launch:
            result = recovery_status(self.r, self.p, include_history=False)
        self.assertEqual(result["blocked_review_summary"]["failed"], 1)
        self.assertEqual(result["blocked_review_summary"]["result_unknown"], 1)
        self.assertEqual(result["blocked_review_summary"]["total"], 2)
        indexed = {item["id"]: item for item in result["blocked_reviews"]}
        self.assertEqual(indexed[failed]["failure_class"], "failed")
        self.assertEqual(indexed[unknown]["failure_class"], "result_unknown")
        self.assertEqual(indexed[failed]["suggested_recovery"], "explicit_retry")
        self.assertEqual([tuple(r) for r in self.r.db.execute("SELECT * FROM admin_calls ORDER BY id")], rows)
        self.assertEqual(before, self.r.db.total_changes)
        launch.assert_not_called()

    def test_failed_diagnostic_is_not_unknown_and_locks_are_preserved(self):
        identity = self.failure()
        self.enable()
        before = dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (identity,)).fetchone())
        detail = dispatch_diagnostic(self.r, self.p)
        self.assertEqual(detail["reason"], "failed_call_requires_review")
        self.assertEqual(detail["blocked_review_scope"], "host")
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (identity,)).fetchone()), before)
        self.assertEqual(before["retry_allowed"], 0)

    def test_unknown_diagnostic_retains_the_existing_reason(self):
        self.failure("orphaned_unknown")
        self.enable()
        self.assertEqual(dispatch_diagnostic(self.r, self.p)["reason"], "unknown_call_requires_review")

    def test_other_host_and_unattributed_attempts_do_not_become_host_recovery_actions(self):
        other = self.failure(host_scope="antigravity:default")
        legacy = self.failure("timeout_unknown", attributed=False)
        summary = blocked_review_summary(self.r, self.p)
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["unattributed"], {"failed": 0, "result_unknown": 1})
        self.assertEqual(recovery_status(self.r, self.p, include_history=False)["blocked_reviews"], [])
        self.enable()
        detail = dispatch_diagnostic(self.r, self.p)
        self.assertEqual(detail["reason"], "unknown_call_requires_review")
        self.assertEqual(detail["blocked_review_scope"], "project_unattributed")
        self.r.db.execute("UPDATE admin_calls SET retry_allowed=1 WHERE id=?", (legacy,))
        self.assertEqual(dispatch_diagnostic(self.r, self.p)["reason"], "ready")
        self.assertEqual(self.r.db.execute("SELECT retry_allowed FROM admin_calls WHERE id=?", (other,)).fetchone()[0], 0)

    def test_partial_usage_is_not_presented_as_known_full_consumption(self):
        partial = self.failure(usage={"token_totals": {"input_tokens": 10}, "completeness": "partial"})
        complete = self.failure(usage={"token_totals": {"input_tokens": 10, "output_tokens": 2}, "completeness": "complete"})
        result = {item["id"]: item for item in recovery_status(self.r, self.p, include_history=False)["blocked_reviews"]}
        self.assertFalse(result[partial]["usage_known"])
        self.assertEqual(result[partial]["usage_completeness"], "partial")
        self.assertTrue(result[complete]["usage_known"])

    def test_total_count_and_page_truncation_do_not_hide_older_failures(self):
        for _ in range(22):
            self.failure()
        result = recovery_status(self.r, self.p, include_history=False)
        self.assertEqual(len(result["blocked_reviews"]), 20)
        self.assertEqual(result["blocked_review_summary"]["total"], 22)
        self.assertTrue(result["blocked_reviews_truncated"])

    def test_queue_labels_selectable_units_without_claiming_model_admission(self):
        self.failure()
        result = _control_queue(self.r, self.p, include_history=False)
        self.assertEqual(result["eligible_unit"], "case")
        self.assertTrue(result["eligible_requires_preflight"])
        self.assertTrue(result["history_separate"])
        self.assertEqual(result["blocked_review_summary"]["failed"], 1)
        self.assertEqual(result["eligible"], 0)


class LocalRecoveryClassificationTests(unittest.TestCase):
    setUp = response_fixtures.AdminResponseStoreTests.setUp
    phase = response_fixtures.AdminResponseStoreTests.phase
    pending = response_fixtures.AdminResponseStoreTests.pending
    failed = response_fixtures.AdminResponseStoreTests.failed
    recover = response_fixtures.AdminResponseStoreTests.recover

    def test_saved_reply_suggests_local_recheck_without_starting_or_unlocking(self):
        identity = self.failed()
        before = dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (identity,)).fetchone())
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            item = next(x for x in recovery_status(self.r, self.p, include_history=False)["blocked_reviews"] if x["id"] == identity)
        launch.assert_not_called()
        self.assertEqual(item["suggested_recovery"], "local_recheck")
        self.assertTrue(item["saved_response"]["can_recheck"])
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (identity,)).fetchone()), before)
        queue = _control_queue(self.r, self.p, include_history=False)
        self.assertEqual(queue["eligible_unit"], "stage_batch")
        self.assertEqual(queue["eligible"], 0)


if __name__ == "__main__":
    unittest.main()
