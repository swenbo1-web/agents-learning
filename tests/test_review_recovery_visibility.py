"""Interrupted history work and bounded review visibility; no real model calls."""
import json
import time
import unittest
from unittest.mock import patch

from agent_learning.admin_control import control_status
from agent_learning.admin_lifecycle import claim_call
from agent_learning.admin_recovery import apply_recovery
from agent_learning.admin_publication import publication_preview
from agent_learning.admin_review_flow import review_activity
from agent_learning.candidate_review import prepare_review, commit_review
from agent_learning.history_review_control import request_history_review, run_history_review
from agent_learning.runtime import ContractError
from tests import test_admin_review_flow as flow_fixtures
from tests.test_history_candidates import history_reviewer, multiple
from tests.test_module6_queue import fake_reviewer


class RecoveryVisibilityTests(unittest.TestCase):
    setUp = flow_fixtures.ReviewFlowTests.setUp
    history = flow_fixtures.ReviewFlowTests.history

    def request_for(self, job, request_id="resume"):
        state = control_status(self.r, self.p)
        item = next(x for x in state["recovery"]["history_jobs"] if x["id"] == job)
        return {"request_id": request_id, "job_id": job, "version": item["version"], "confirmed": True,
            "max_calls": 1, "max_seconds": 60, "expected_policy_version": state["policy"]["policy_version"],
            "expected_settings_version": state["settings_version"], "expected_session_epoch": state["session_epoch"],
            "expected_governance_epoch": state["governance_epoch"],
            "expected_project_budget_version": state["project_budget_policy"]["version"]}

    def item(self, job):
        return next(x for x in control_status(self.r, self.p)["recovery"]["history_jobs"] if x["id"] == job)

    def interrupt(self, job, state):
        self.r.db.execute("UPDATE history_jobs SET state=?,owner='abandoned',lease_until=? WHERE id=?",
                          (state, time.time() - 1, job))

    def test_expired_review_request_allows_explicit_continuation(self):
        body = self.history()
        with patch.object(self.r, "_launch_background"):
            request_history_review(self.r, self.p, body)
        self.r.db.execute("UPDATE history_review_requests SET state='running',lease_until=?", (time.time() - 1,))
        self.interrupt(body["job_id"], "reviewing")
        item = self.item(body["job_id"])
        self.assertTrue(item["can_review"])
        self.assertTrue(item["resume_required"])
        self.assertEqual(item["review_request"]["state"], "result_unknown")
        with patch.object(self.r, "_launch_background") as launch:
            request_history_review(self.r, self.p, self.request_for(body["job_id"]))
        launch.assert_called_once()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_interrupted_materialization_before_first_case_can_resume_frozen_sources(self):
        (self.workspace / "notes.md").write_text("Frozen project convention.", encoding="utf-8")
        job = self.r.history_prepare(self.p, self.workspace)["job_id"]
        self.interrupt(job, "materializing")
        self.assertEqual(self.item(job)["counts"]["cases"], 0)
        self.assertTrue(self.item(job)["can_review"])
        with patch.object(self.r, "_launch_background"):
            request_history_review(self.r, self.p, self.request_for(job))
        with patch("agent_learning.history_sources.scan_sources", side_effect=AssertionError("rescan")), \
                patch("agent_learning.admin.subprocess.Popen", fake_reviewer("skip")):
            result = run_history_review(self.r, self.p, "resume")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["counts"]["materialized_sources"], 1)
        self.assertEqual(result["counts"]["calls"], 1)

    def test_live_owner_and_cancelled_batch_keep_resume_disabled(self):
        job = self.history()["job_id"]
        for state in ("materializing", "reviewing"):
            self.r.db.execute("UPDATE history_jobs SET state=?,owner='live',lease_until=? WHERE id=?",
                              (state, time.time() + 300, job))
            self.assertFalse(self.item(job)["can_review"])
            with self.assertRaises(ContractError):
                request_history_review(self.r, self.p, self.request_for(job, state))
        self.r.db.execute("UPDATE history_jobs SET state='cancelled',owner=NULL,lease_until=NULL WHERE id=?", (job,))
        self.assertFalse(self.item(job)["can_review"])

    def test_expired_job_can_be_reauthorized_without_starting_a_model(self):
        job = self.history()["job_id"]
        self.interrupt(job, "reviewing")
        self.r.scope_account_changed("account-change")
        item = self.item(job)
        self.assertTrue(item["can_reauthorize"])
        with patch("subprocess.Popen") as model:
            apply_recovery(self.r, self.p, {"request_id": "reauthorize", "action": "reauthorize_history",
                "id": job, "version": item["version"], "confirmed": True, "reason": "Resume frozen work",
                "expected_policy_version": self.r.admin_policy(self.p)["policy_version"],
                "expected_session_epoch": self.r.session_epoch(), "expected_governance_epoch": self.r.host_epoch()})
        model.assert_not_called()
        self.assertTrue(self.item(job)["can_review"])

    def test_previous_account_request_does_not_block_current_ui_or_backend(self):
        body = self.history()
        with patch.object(self.r, "_launch_background"):
            request_history_review(self.r, self.p, body)
        self.r.scope_account_changed("new-account")
        self.r.db.execute("UPDATE history_jobs SET host_epoch=? WHERE id=?", (self.r.host_epoch(), body["job_id"]))
        item = self.item(body["job_id"])
        self.assertTrue(item["can_review"])
        self.assertFalse(item["review_request"]["current"])
        self.assertEqual(item["review_request"]["state"], "superseded")
        with patch.object(self.r, "_launch_background"):
            result = request_history_review(self.r, self.p, self.request_for(body["job_id"]))
        self.assertEqual(result["status"], "accepted")
        self.assertFalse(self.item(body["job_id"])["can_review"])

    def test_continuation_never_retries_unknown_calls(self):
        (self.workspace / "notes.md").write_text("Another frozen source.", encoding="utf-8")
        body = self.history()
        problem = self.r.db.execute("SELECT problem_id FROM history_case_links WHERE job_id=? LIMIT 1", (body["job_id"],)).fetchone()[0]
        revision = self.r.db.execute("SELECT revision FROM problems WHERE id=?", (problem,)).fetchone()[0]
        with self.r.transaction():
            claim = claim_call(self.r, self.p, problem, revision, "unknown-call", self.r.host_epoch(), 10)
            self.r.db.execute("UPDATE admin_calls SET state='orphaned_unknown',lease_until=NULL WHERE id=?", (claim["call_id"],))
        self.interrupt(body["job_id"], "reviewing")
        with patch.object(self.r, "_launch_background"):
            request_history_review(self.r, self.p, self.request_for(body["job_id"]))
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            result = run_history_review(self.r, self.p, "resume")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result["reason"], "review_required")
        old = self.r.db.execute("SELECT state,retry_allowed FROM admin_calls WHERE id=?", (claim["call_id"],)).fetchone()
        self.assertEqual(tuple(old), ("orphaned_unknown", 0))

    def test_valid_request_blocks_other_hosts_but_old_governance_does_not(self):
        from agent_learning.runtime import Runtime
        body = self.history()
        with patch.object(self.r, "_launch_background"):
            request_history_review(self.r, self.p, body)
        for host in ("antigravity", "dsh"):
            other = Runtime(self.r.home, config=self.config, host=host)
            try:
                item = next(x for x in control_status(other, self.p)["recovery"]["history_jobs"] if x["id"] == body["job_id"])
                self.assertFalse(item["can_review"])
                self.assertTrue(item["review_request"]["current"])
            finally:
                other.close()
        payload = json.loads(self.r.db.execute("SELECT body FROM history_review_requests").fetchone()[0])
        payload["expected_governance_epoch"] = -1
        self.r.db.execute("UPDATE history_review_requests SET body=?", (json.dumps(payload),))
        self.assertTrue(self.item(body["job_id"])["can_review"])

    def generate(self, *, multi=False, extra=False):
        if extra:
            (self.workspace / "notes.md").write_text("Second independent fact.", encoding="utf-8")
        job = self.history()["job_id"]
        reviewer = history_reviewer(multiple) if multi else fake_reviewer("candidate")
        with patch("agent_learning.admin.subprocess.Popen", reviewer):
            self.r.history_run(self.p, job, allow_review=True, max_calls=1)
        return job

    def test_multiple_candidates_have_separate_summaries_and_live_approval_states(self):
        self.generate(multi=True)
        item = review_activity(self.r, self.p)["recent"][0]
        self.assertEqual(len(item["candidates"]), 3)
        self.assertEqual({x["title"] for x in item["candidates"]}, {"Independent fact 0", "Independent fact 1", "Independent fact 2"})
        for candidate in item["candidates"]:
            self.assertTrue(candidate["content_summary"])
            self.assertTrue(candidate["applicability"])
            self.assertTrue(candidate["limitations"])
            self.assertTrue(candidate["evidence_refs"])
        chosen = item["candidate_ids"][:2]
        plan = prepare_review(self.r, self.p, chosen, "approve")
        commit_review(self.r, self.p, plan, "approve-two")
        updated = review_activity(self.r, self.p)["recent"][0]["candidates"]
        self.assertEqual([x["status"] for x in updated], ["published", "published", "candidate"])
        self.assertTrue(all(x["publication"]["knowledge_id"] for x in updated[:2]))

    def test_normal_budget_pause_keeps_completed_review_visible_without_model_calls(self):
        job = self.generate(extra=True)
        self.assertEqual(self.item(job)["state"], "paused")
        before = self.r.db.total_changes
        with patch("subprocess.Popen") as model:
            item = review_activity(self.r, self.p)["recent"][0]
        model.assert_not_called()
        self.assertEqual(self.r.db.total_changes, before)
        self.assertFalse(item.get("history_access_required", False))
        self.assertTrue(item["candidate_summary"])
        self.assertTrue(item["candidates"][0]["content_summary"])

    def test_deferred_candidate_keeps_its_publication_inspection_available(self):
        self.generate()
        identity = review_activity(self.r, self.p)["recent"][0]["candidate_ids"][0]
        self.r.db.execute("UPDATE candidates SET status='deferred' WHERE id=?", (identity,))
        preview = publication_preview(self.r, self.p, identity)
        self.assertEqual(preview["reason"], "history_requires_approval")
        self.assertFalse(preview["published"])

    def test_user_pause_cancellation_and_account_change_hide_candidate_text(self):
        job = self.generate(multi=True)
        for state in ("paused", "cancelled"):
            self.r.db.execute("UPDATE history_jobs SET state=?,last_result=? WHERE id=?",
                              (state, json.dumps({"reason": "user_paused"}), job))
            item = review_activity(self.r, self.p)["recent"][0]
            self.assertTrue(item["history_access_required"])
            self.assertEqual(item["candidates"], [])
            self.assertEqual(item["candidate_summary"], "")
        self.r.db.execute("UPDATE history_jobs SET state='review_complete' WHERE id=?", (job,))
        self.r.scope_account_changed("stale-review")
        self.assertEqual(review_activity(self.r, self.p)["recent"][0]["candidates"], [])
