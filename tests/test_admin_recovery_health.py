"""Recovered decisions have a distinct outcome from immutable paid attempts."""
import time
import unittest
from unittest.mock import patch

from agent_learning.admin import ResponseRejected, admin_once
from agent_learning.admin_lifecycle import claim_call, retry_call
from agent_learning.admin_queue import executor_backoff, executor_signature
from agent_learning.admin_response_store import recheck, status
from agent_learning.admin_review_flow import review_activity
from agent_learning.phase_metrics import batch_metrics
from agent_learning.runtime import ContractError, encode
from tests import test_admin_response_store as fixtures
from tests import test_history_candidates as history_fixtures
from tests.test_module6_queue import fake_reviewer


class AdminRecoveryHealthTests(unittest.TestCase):
    setUp = fixtures.AdminResponseStoreTests.setUp
    phase = fixtures.AdminResponseStoreTests.phase
    pending = fixtures.AdminResponseStoreTests.pending
    failed = fixtures.AdminResponseStoreTests.failed
    recover = fixtures.AdminResponseStoreTests.recover

    def test_local_success_clears_stale_failure_state_and_exposes_child_candidates(self):
        identity = self.failed()
        paid = dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (identity,)).fetchone())
        plan = self.r.admin_execution_plan(self.p, identity)["executor"]
        self.assertIsNotNone(executor_backoff(self.r, self.p, plan))
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            result = self.recover(identity)
        launch.assert_not_called()
        self.assertEqual(result["status"], "accepted", result)
        self.assertIsNone(executor_backoff(self.r, self.p, plan))
        cfg = self.r._scheduler_config()
        cfg["enabled"] = True
        scheduler = self.r._scheduler_admin_state(self.p, cfg)
        self.assertNotEqual(scheduler[3], "review_required", scheduler)
        before = self.r.db.total_changes
        item = review_activity(self.r, self.p)["recent"][0]
        self.assertEqual(self.r.db.total_changes, before)
        self.assertEqual(item["state"], "candidate")
        self.assertEqual(item["execution_state"], "failed")
        self.assertTrue(item["local_recheck"])
        self.assertEqual(item["purpose"], "phase_review")
        self.assertEqual(item["candidate_count"], 4)
        self.assertEqual(len(item["candidates"]), 4)
        self.assertTrue(all(value["status"] != "unavailable" for value in item["candidates"]))
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (identity,)).fetchone()), paid)
        metric = batch_metrics(self.r, self.p, paid["problem"])
        self.assertEqual(metric["candidate_count"], 4)
        self.assertEqual(metric["local_recheck_count"], 1)
        self.assertEqual(metric["input_tokens"], 600)
        self.assertEqual(metric["output_tokens"], 120)

    def test_effective_review_counters_require_receipts_and_keep_project_token_ledger(self):
        from agent_learning.admin_usage import admin_budget_status, admin_project_budget_status
        identity = self.failed()
        before = admin_project_budget_status(self.r, self.p)
        self.assertEqual(before["completed_reviews"], 0)
        self.assertEqual(before["locally_recovered_reviews"], 0)
        self.assertEqual(before["effective_completed_reviews"], 0)
        self.assertEqual(before["failed_attempts"], 1)
        result = self.recover(identity)
        self.assertEqual(result["status"], "accepted", result)
        after = admin_project_budget_status(self.r, self.p)
        self.assertEqual(after["completed_reviews"], 0)
        self.assertEqual(after["failed_attempts"], 1)
        self.assertEqual(after["locally_recovered_reviews"], 1)
        self.assertEqual(after["effective_completed_reviews"], 1)
        for key in ("counted_attempts", "started_attempts", "reported_tokens", "token_reservations", "accounted_tokens"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(admin_budget_status(self.r)["locally_recovered_reviews"], 1)
        other = self.workspace.parent / "other-project"
        other.mkdir()
        other_project = self.r.project(other)
        self.assertEqual(admin_project_budget_status(self.r, other_project)["effective_completed_reviews"], 0)
        problem = self.r.db.execute("SELECT problem FROM admin_calls WHERE id=?", (identity,)).fetchone()[0]
        self.r.db.execute("DELETE FROM review_receipts WHERE project=? AND problem=? AND call_id=?", (self.p, problem, identity))
        self.assertEqual(admin_project_budget_status(self.r, self.p)["locally_recovered_reviews"], 0)
        self.assertEqual(admin_budget_status(self.r)["locally_recovered_reviews"], 0)

    def test_recovery_never_clears_a_newer_provider_failure(self):
        identity = self.failed()
        plan = self.r.admin_execution_plan(self.p, identity)["executor"]
        later = time.time() + 2
        self.r.db.execute("UPDATE admin_executor_failures SET reason='provider_quota_exceeded',updated=?,next_try=? "
            "WHERE project=? AND host_scope=? AND signature=?",
            (later, later + 900, self.p, self.r._session_scope(), executor_signature(plan)))
        result = self.recover(identity)
        self.assertEqual(result["status"], "accepted", result)
        backoff = executor_backoff(self.r, self.p, plan)
        self.assertEqual(backoff["reason"], "provider_quota_exceeded")

    def test_recovered_phase_publication_and_candidates_are_summarized_once(self):
        self.r.db.execute("INSERT INTO admin_control_state(project,host_scope,enabled,version,updated,auto_publish,publication_mode) "
            "VALUES(?,?,1,1,?,1,'tiered')", (self.p, self.r._session_scope(), time.time()))
        identity = self.failed()
        result = self.recover(identity)
        self.assertEqual(result["status"], "accepted", result)
        item = review_activity(self.r, self.p)["recent"][0]
        self.assertEqual(item["candidate_count"], 4)
        self.assertEqual(item["publication"], {"status": "published", "candidate_count": 4, "published_count": 4})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 4)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_explicit_retry_rejects_already_recovered_legacy_call(self):
        from agent_learning.phase_review import configure, policy
        configure(self.r, self.p, {"mode": "legacy"}, policy(self.r, self.p)["version"])
        self.r.task_update(self.p, "ordinary", "Current ordinary observation")
        problem = self.r.feedback(self.p, "ordinary", "A current project convention", category="project")
        class Reviewer(fake_reviewer("skip")):
            def __init__(child, args, **kwargs):
                super().__init__(args, **kwargs)
                child.log = kwargs["stdout"]
            def communicate(child, *args, **kwargs):
                result = super().communicate(*args, **kwargs)
                child.log.write((encode({"type": "turn.completed", "usage": {"input_tokens": 600, "output_tokens": 120}}) + "\n").encode())
                return result
        with patch("agent_learning.admin.subprocess.Popen", Reviewer), patch(
                "agent_learning.admin._accept_response", side_effect=ResponseRejected("candidate_batch_ambiguous")):
            failed = admin_once(self.r, self.p, problem["id"])
        identity = failed["call_id"]
        result = self.recover(identity)
        self.assertEqual(result["status"], "accepted", result)
        before = self.r.db.execute("SELECT retry_allowed,usage FROM admin_calls WHERE id=?", (identity,)).fetchone()
        with self.assertRaisesRegex(ContractError, "accepted review receipt"):
            retry_call(self.r, self.p, identity, "User confirmed a retry")
        self.assertEqual(tuple(self.r.db.execute("SELECT retry_allowed,usage FROM admin_calls WHERE id=?", (identity,)).fetchone()), tuple(before))

    def test_paid_claim_waits_for_independent_local_recheck_lease(self):
        identity = self.failed()
        now = time.time()
        self.r.db.execute("INSERT INTO admin_response_rechecks VALUES(?,?,?,?,?,'validating','owner',?,NULL,?,?)",
            (self.p, self.r._session_scope(), "in-progress", "digest", identity, now + 30, now, now))
        count = self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0]
        problem = self.r.db.execute("SELECT problem,revision FROM admin_calls WHERE id=?", (identity,)).fetchone()
        with self.r.transaction():
            result = claim_call(self.r, self.p, problem[0], problem[1], "fresh-review", self.r.host_epoch(), 10)
        self.assertEqual(result["status"], "waiting_for_review")
        self.assertEqual(result["active_state"], "local_recheck")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], count)
        cfg = self.r._scheduler_config()
        cfg["enabled"] = True
        self.assertEqual(self.r._scheduler_admin_state(self.p, cfg)[3], "active_review")


class HistoricalRecoveryHealthTests(unittest.TestCase):
    setUp = history_fixtures.HistoryCandidateTests.setUp

    def test_completed_failed_history_run_does_not_offer_unauthorized_local_recheck(self):
        class Reviewer(history_fixtures.history_reviewer(history_fixtures.multiple)):
            def __init__(child, args, **kwargs):
                super().__init__(args, **kwargs)
                child.log = kwargs["stdout"]
            def communicate(child, *args, **kwargs):
                result = super().communicate(*args, **kwargs)
                child.log.write((encode({"type": "turn.completed", "usage": {"input_tokens": 600, "output_tokens": 120}}) + "\n").encode())
                return result
        job = self.r.history_prepare(self.p, self.workspace)["job_id"]
        with patch("agent_learning.admin.subprocess.Popen", Reviewer), patch(
                "agent_learning.admin._accept_response", side_effect=ResponseRejected("candidate_batch_ambiguous")):
            self.r.history_run(self.p, job, allow_review=True, max_calls=1)
        call = dict(self.r.db.execute("SELECT * FROM admin_calls").fetchone())
        historical = dict(self.r.db.execute("SELECT * FROM history_jobs WHERE id=?", (job,)).fetchone())
        saved = status(self.r, self.p, call["id"])
        self.assertTrue(saved["available"])
        self.assertFalse(saved["can_recheck"])
        self.assertEqual(saved["reason"], "history_run_reapproval_required")
        self.assertEqual(historical["state"], "paused")
        self.assertIsNone(historical["owner"])
        before = self.r.db.total_changes
        with patch("agent_learning.admin.subprocess.Popen") as launch, self.assertRaisesRegex(ContractError, "history_run_reapproval_required"):
            recheck(self.r, self.p, call["id"], request_id="explicit-local-recheck", expected_version=saved["version"],
                expected_session_epoch=self.r.session_epoch(), expected_governance_epoch=self.r.host_epoch(), confirmed=True)
        launch.assert_not_called()
        self.assertEqual(self.r.db.total_changes, before)
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (call["id"],)).fetchone()), call)
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM history_jobs WHERE id=?", (job,)).fetchone()), historical)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_response_rechecks").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
