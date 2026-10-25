"""Protocol events, usable replies and old unknowns remain distinct in read views."""
import json
import unittest

from agent_learning.admin_observations import execution_observations, provider_failure_diagnostic, review_failure_reason
from agent_learning.admin_queue import record_dispatch_diagnostic, dispatch_diagnostic
from agent_learning.admin_recovery import recovery_status
from tests import test_admin_response_store as fixtures


class ObservationProjectionTests(unittest.TestCase):
    def test_old_terminal_flag_is_not_new_protocol_or_response_evidence(self):
        for flag in (True, False):
            view = execution_observations({"execution": {"terminal_observed": flag, "returncode": 0}})
            self.assertIsNone(view["protocol_terminal_observed"])
            self.assertIsNone(view["response_collected"])
            self.assertEqual(view["protocol_terminal_status"], "unknown")
            self.assertEqual(view["returncode"], 0)

    def test_failed_protocol_is_distinct_from_missing_usable_reply(self):
        view = execution_observations({"execution": {"protocol_terminal_observed": True,
            "protocol_terminal_status": "failed", "response_collected": False, "returncode": 1,
            "secret": "DO_NOT_EXPOSE"}})
        self.assertTrue(view["protocol_terminal_observed"])
        self.assertEqual(view["protocol_terminal_status"], "failed")
        self.assertFalse(view["response_collected"])
        self.assertNotIn("DO_NOT_EXPOSE", json.dumps(view))

    def test_malformed_or_contradictory_observations_stay_unknown(self):
        for usage in (None, [], {"execution": []}, {"execution": {"protocol_terminal_observed": 1,
                "protocol_terminal_status": [], "response_collected": "yes", "returncode": True}},
                {"execution": {"protocol_terminal_observed": False, "protocol_terminal_status": "completed"}}):
            view = execution_observations(usage)
            self.assertIsNone(view["protocol_terminal_observed"])
            self.assertEqual(view["protocol_terminal_status"], "unknown")

    def test_later_local_rejection_is_not_blamed_on_an_earlier_provider_error(self):
        from agent_learning.admin_diagnostics import codex_failure
        diagnostic = codex_failure({"type": "error", "error": {"code": "rate_limit_exceeded"}})
        for phase in ("response_validation", "decision_commit"):
            usage = {"provider_diagnostic": diagnostic, "execution": {
                "phase": phase, "reason_code": "executor_response_invalid", "returncode": 0}}
            self.assertIsNone(provider_failure_diagnostic(usage))
            self.assertEqual(review_failure_reason(usage), "executor_response_invalid")
        for malformed in ([], {}, None, 1):
            self.assertIsNone(provider_failure_diagnostic({"execution": {"phase": malformed}}))


class ObservationRecoveryTests(unittest.TestCase):
    setUp = fixtures.AdminResponseStoreTests.setUp
    phase = fixtures.AdminResponseStoreTests.phase
    pending = fixtures.AdminResponseStoreTests.pending
    failed = fixtures.AdminResponseStoreTests.failed

    def test_failed_attempt_view_is_read_only_and_preserves_recovery_authority(self):
        call = self.failed()
        before = dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call,)).fetchone())
        changes = self.r.db.total_changes
        item = next(row for row in recovery_status(self.r, self.p, include_history=False)["blocked_reviews"]
                    if row["id"] == call)
        self.assertEqual(self.r.db.total_changes, changes)
        observations = item["execution_observations"]
        self.assertTrue(observations["protocol_terminal_observed"])
        self.assertEqual(observations["protocol_terminal_status"], "completed")
        self.assertTrue(observations["response_collected"])
        self.assertEqual(item["state"], "failed")
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call,)).fetchone()), before)

    def test_dispatch_view_filters_execution_metadata(self):
        record_dispatch_diagnostic(self.r, self.p, "executor_nonzero_exit", {
            "protocol_terminal_observed": True, "protocol_terminal_status": "failed",
            "response_collected": False, "returncode": 1, "secret": "DO_NOT_EXPOSE"})
        details = dispatch_diagnostic(self.r, self.p)["last_observation"]["details"]
        self.assertEqual(details["protocol_terminal_status"], "failed")
        self.assertTrue(details["protocol_terminal_observed"])
        self.assertFalse(details["response_collected"])
        self.assertNotIn("DO_NOT_EXPOSE", json.dumps(details))


if __name__ == "__main__":
    unittest.main()
