"""Public reply recovery uses SQLite and fake process output, never providers."""
import json
import time
import unittest
from unittest.mock import patch

from agent_learning.admin import ResponseRejected, admin_once
from agent_learning.admin_response_store import recheck, status
from agent_learning.runtime import ContractError, encode
from tests import test_phase_review as fixtures


class AdminResponseStoreTests(unittest.TestCase):
    setUp = fixtures.PhaseReviewTests.setUp
    phase = fixtures.PhaseReviewTests.phase

    def pending(self, *, decision="candidate", modify=None):
        self.phase("first", "First build convention")
        self.phase("second", "Second architecture constraint")
        base = fixtures.reviewer(decision)
        class Reviewer(base):
            def __init__(child, args, **kwargs):
                super().__init__(args, **kwargs)
                child.log = kwargs["stdout"]
            def communicate(child, *args, **kwargs):
                result = super().communicate(*args, **kwargs)
                if modify:
                    body = json.loads(child.output.read_text(encoding="utf-8"))
                    modify(body)
                    child.output.write_text(encode(body), encoding="utf-8")
                child.log.write((encode({"type": "turn.completed", "id": "fake-usage",
                    "usage": {"input_tokens": 600, "output_tokens": 120}}) + "\n").encode())
                return result
        return Reviewer

    def failed(self, *, modify=None):
        reviewer = self.pending(modify=modify)
        with patch("agent_learning.admin.subprocess.Popen", reviewer), patch(
                "agent_learning.admin._accept_response", side_effect=ResponseRejected("candidate_batch_ambiguous", "response.candidates")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)
        return result["call_id"]

    def recover(self, identity, **values):
        args = {"request_id": "explicit-local-request", "expected_version": status(self.r, self.p, identity)["version"],
                "expected_session_epoch": self.r.session_epoch(), "expected_governance_epoch": self.r.host_epoch(),
                "confirmed": True}
        args.update(values)
        return recheck(self.r, self.p, identity, **args)

    def test_multi_candidate_publish_proposal_matches_schema_and_receiver(self):
        def modify(body):
            for item in body["items"]:
                item["proposal"] = {"action": "publish", "knowledge_id": None, "expected_revision": None}
        with patch("agent_learning.admin.subprocess.Popen", self.pending(modify=modify)):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "candidate", result)
        count = self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        proposals = self.r.db.execute("SELECT COUNT(*) FROM governance_proposals").fetchone()[0]
        self.assertGreaterEqual(count, 4)
        self.assertEqual(proposals, count)

    def test_ambiguous_multi_candidate_target_rejected_atomically_with_public_code(self):
        def modify(body):
            body["items"][0]["proposal"] = {"action": "update", "knowledge_id": "some-target", "expected_revision": 1}
        with patch("agent_learning.admin.subprocess.Popen", self.pending(modify=modify)):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "failed", result)
        detail = status(self.r, self.p, result["call_id"])
        self.assertEqual(detail["rejection"], {"code": "candidate_batch_ambiguous", "path": "response.items[0].candidates"})
        self.assertTrue(detail["available"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)

    def test_reply_retained_then_local_recheck_never_launches_or_changes_paid_usage(self):
        identity = self.failed()
        state = status(self.r, self.p, identity)
        self.assertTrue(state["can_recheck"])
        from agent_learning.phase_review import status as phase_status
        self.assertEqual(phase_status(self.r, self.p)["blocked_phase_count"], 2)
        self.assertEqual(phase_status(self.r, self.p)["waiting_phase_count"], 0)
        row = dict(self.r.db.execute("SELECT * FROM admin_response_records WHERE call_id=?", (identity,)).fetchone())
        self.assertNotIn("provider_plan", json.loads(row["reserved"]))
        self.assertNotIn("instruction", json.loads(row["reserved"]))
        self.assertEqual(len(json.loads(row["response"])["items"]), 2)
        paid = dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (identity,)).fetchone())
        observations = [tuple(r) for r in self.r.db.execute("SELECT * FROM admin_usage_observations")]
        count = self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0]
        self.assertEqual(json.loads(paid["usage"])["token_totals"], {"input_tokens": 600, "output_tokens": 120})
        args = {"expected_version": state["version"]}
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            result = self.recover(identity, **args)
            replay = self.recover(identity, **args)
        launch.assert_not_called()
        self.assertEqual(result["status"], "accepted", result)
        self.assertTrue(result["result"]["local_recheck"])
        self.assertEqual(replay, result)
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (identity,)).fetchone()), paid)
        self.assertEqual([tuple(r) for r in self.r.db.execute("SELECT * FROM admin_usage_observations")], observations)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], count)
        self.assertGreaterEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 4)
        self.assertEqual(self.r.db.execute("SELECT state FROM phase_review_batches WHERE problem_id=?", (paid["problem"],)).fetchone()[0], "completed")
        self.assertEqual(phase_status(self.r, self.p)["blocked_phase_count"], 0)

    def test_unknown_and_nonterminal_success_output_cannot_recheck(self):
        identity = self.failed()
        for state, terminal, code in (("timeout_unknown", True, 0), ("failed", False, 0), ("failed", True, 1)):
            with self.subTest(state=state, terminal=terminal, code=code):
                self.r.db.execute("UPDATE admin_calls SET state=?,usage=? WHERE id=?", (state, encode({"execution": {
                    "terminal_observed": terminal, "returncode": code}}), identity))
                self.assertFalse(status(self.r, self.p, identity)["can_recheck"])
                with patch("agent_learning.admin.subprocess.Popen") as launch, self.assertRaises(ContractError):
                    self.recover(identity)
                launch.assert_not_called()

    def test_old_call_without_retained_public_response_has_no_implicit_retry(self):
        identity = self.failed()
        self.r.db.execute("DELETE FROM admin_response_records WHERE call_id=?", (identity,))
        self.assertFalse(status(self.r, self.p, identity)["available"])
        with patch("agent_learning.admin.subprocess.Popen") as launch, self.assertRaises(ContractError):
            self.recover(identity, expected_version=1)
        launch.assert_not_called()

    def test_changed_sources_prevent_recovery_and_leave_reply_for_inspection(self):
        identity = self.failed()
        member = self.r.db.execute("SELECT problem_id FROM phase_review_members LIMIT 1").fetchone()[0]
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (member,))
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            result = self.recover(identity)
        launch.assert_not_called()
        self.assertEqual(result["status"], "rejected", result)
        self.assertEqual(result["rejection"]["code"], "phase_source_changed")
        self.assertTrue(status(self.r, self.p, identity)["available"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)

    def test_old_epoch_and_other_host_cannot_recover(self):
        from agent_learning.runtime import Runtime
        identity = self.failed()
        other = Runtime(self.r.home, host="antigravity")
        self.addCleanup(other.close)
        self.assertFalse(status(other, self.p, identity)["available"])
        self.r.account_changed()
        self.assertFalse(status(self.r, self.p, identity)["can_recheck"])
        with self.assertRaises(ContractError):
            self.recover(identity)

    def test_changed_plan_or_policy_does_not_bypass_frozen_executor(self):
        identity = self.failed()
        self.r.admin_configure(self.p, True, "codex", "fixed", "different-model", expected_policy_version=0)
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            result = self.recover(identity)
        launch.assert_not_called()
        self.assertEqual(result["status"], "rejected", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)

    def test_immutable_execution_plan_fingerprint_cannot_be_replaced(self):
        identity = self.failed()
        body = json.loads(self.r.db.execute("SELECT body FROM admin_execution_plans WHERE call_id=?", (identity,)).fetchone()[0])
        body["model"] = "altered-immutable-model"
        self.r.db.execute("UPDATE admin_execution_plans SET body=? WHERE call_id=?", (encode(body), identity))
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            result = self.recover(identity)
        launch.assert_not_called()
        self.assertEqual(result["status"], "rejected", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)

    def test_frozen_response_and_reservation_fingerprints_are_checked(self):
        identity = self.failed()
        self.r.db.execute("UPDATE admin_response_records SET response=json_set(response,'$.reason','Changed reply') WHERE call_id=?", (identity,))
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            result = self.recover(identity)
        launch.assert_not_called()
        self.assertEqual(result["status"], "rejected", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)

    def test_concurrent_rechecks_and_request_identity_are_guarded(self):
        identity = self.failed()
        now = time.time()
        self.r.db.execute("INSERT INTO admin_response_rechecks VALUES(?,?,?,?,?,'validating','other-owner',?,NULL,?,?)",
            (self.p, self.r._session_scope(), "other-request", "other-digest", identity, now + 30, now, now))
        with self.assertRaises(ContractError):
            self.recover(identity)
        self.r.db.execute("UPDATE admin_response_rechecks SET lease_until=0")
        result = self.recover(identity)
        self.assertEqual(result["status"], "accepted", result)
        with self.assertRaises(ContractError):
            self.recover(identity)

    def test_unknown_fields_and_secrets_are_not_retained_as_private_reasoning(self):
        def modify(body):
            body["analysis"] = "NEVER-PERSIST-PRIVATE-REASONING"
            body["reason"] = "Bearer SECRET-CREDENTIAL password=credential"
        identity = self.failed(modify=modify)
        row = self.r.db.execute("SELECT response,state FROM admin_response_records WHERE call_id=?", (identity,)).fetchone()
        self.assertIsNone(row["response"])
        self.assertEqual(row["state"], "unavailable")
        self.assertFalse(status(self.r, self.p, identity)["can_recheck"])

    def test_clean_bounded_public_response_and_version_checked_for_confirmation(self):
        def modify(body):
            body["reason"] = "Bearer SECRET-CREDENTIAL password=credential"
        identity = self.failed(modify=modify)
        row = self.r.db.execute("SELECT response FROM admin_response_records WHERE call_id=?", (identity,)).fetchone()
        self.assertNotIn("SECRET-CREDENTIAL", row[0])
        self.assertNotIn("password=credential", row[0])
        with self.assertRaises(ContractError):
            self.recover(identity, expected_version=1)
        with self.assertRaises(ContractError):
            self.recover(identity, confirmed=False)

    def test_expired_independent_lease_rolls_back_every_candidate(self):
        identity = self.failed()
        from agent_learning.admin_response_store import validate_recheck
        def expire(runtime, reserved):
            runtime.db.execute("UPDATE admin_response_rechecks SET lease_until=0 WHERE call_id=?", (identity,))
            return validate_recheck(runtime, reserved)
        with patch("agent_learning.admin_response_store.validate_recheck", side_effect=expire):
            result = self.recover(identity)
        self.assertEqual(result["status"], "rejected", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
