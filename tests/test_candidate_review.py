"""Candidate review checks use explicit isolated SQLite homes and no model."""
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_learning.candidate_review import (candidate_review_detail, candidate_review_list,
    commit_review, prepare_review)
from agent_learning.runtime import ContractError, Runtime, encode


class CandidateReviewTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "build" / "local" / "candidate-review-tests"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="run-", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.home = Path(self.temp.name) / "home"
        self.r = Runtime(self.home, config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "chat", "Inspect candidate approval")

    def source(self, title="Project storage", content="Use local SQLite", **changes):
        problem = self.r.feedback(self.p, "chat", title + " observed", category="project")
        refs = [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        body = dict(problem_id=problem["id"], problem_revision=problem["revision"], title=title,
                    content=content, category="project", knowledge_kind="fact", applicability="This project",
                    limitations="Reported historical observation", evidence_refs=refs, validation_level="reported")
        body.update(changes)
        candidate = self.r.submit_candidate(self.p, **body)["candidate_id"]
        return problem, candidate

    def plan(self, identity, action="approve", **kwargs):
        return prepare_review(self.r, self.p, identity if isinstance(identity, list) else [identity], action, **kwargs)

    def commit(self, plan, request="user-click"):
        return commit_review(self.r, self.p, plan, request)

    def history(self):
        number = self.r.db.execute("SELECT COUNT(*) FROM history_jobs").fetchone()[0]
        (self.workspace / "README.md").write_text(f"# Notes {number}\nProject stores observations in SQLite.\n", encoding="utf-8")
        job = self.r.history_prepare(self.p, self.workspace)["job_id"]
        self.r.history_run(self.p, job)
        case = self.r.db.execute("SELECT p.* FROM problems p JOIN history_case_links h ON h.problem_id=p.id "
                                 "WHERE h.project=? AND h.job_id=? LIMIT 1", (self.p, job)).fetchone()
        refs = [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (case["id"],))]
        candidate = self.r.submit_candidate(self.p, problem_id=case["id"], problem_revision=case["revision"],
            title="Historical project fact", content="The imported project notes describe SQLite storage.", category="project",
            knowledge_kind="fact", applicability="Archived source version", limitations="Not current implementation verification",
            evidence_refs=refs, validation_level="reported")["candidate_id"]
        self.r.db.execute("UPDATE history_jobs SET state='paused',owner=NULL,lease_until=NULL,review_enabled=0,last_result=? WHERE id=?",
                          (encode({"reason": "review_budget_exhausted"}), job))
        return job, candidate

    def test_list_detail_and_prepare_do_not_write_or_call_model(self):
        _, identity = self.source(content="SQLite " * 2000)
        before = self.r.db.total_changes
        with patch("agent_learning.admin.subprocess.Popen") as model:
            listed = candidate_review_list(self.r, self.p)
            detail = candidate_review_detail(self.r, self.p, identity, limit=10)
            self.assertTrue(self.plan(identity)["ready"])
        model.assert_not_called()
        self.assertEqual(before, self.r.db.total_changes)
        self.assertEqual(listed["items"][0]["candidate_id"], identity)
        self.assertEqual(len(detail["field"]["text"]), 10)
        self.assertEqual(detail["field"]["next_offset"], 10)
        self.assertNotIn("content", detail["body"])
        self.assertTrue(detail["evidence"]["items"])

    def test_pure_read_model_and_query_only_connection(self):
        _, identity = self.source()
        connection = sqlite3.connect((self.home / "runtime.sqlite3").as_uri() + "?mode=ro", uri=True)
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        view = SimpleNamespace(db=connection)
        self.assertEqual(candidate_review_list(view, self.p)["total"], 1)
        self.assertTrue(candidate_review_detail(view, self.p, identity)["preflight"]["publishable"])
        self.assertEqual(connection.total_changes, 0)

    def test_approve_replays_durable_receipt_and_preserves_evidence_level(self):
        _, identity = self.source()
        plan = self.plan(identity)
        with patch("agent_learning.admin.subprocess.Popen") as model:
            first = self.commit(plan)
        model.assert_not_called()
        self.assertEqual(first, self.commit(plan))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='CandidateReview'").fetchone()[0], 1)
        self.assertEqual(json.loads(self.r.db.execute("SELECT body FROM knowledge").fetchone()[0])["validation_level"], "reported")
        other = Runtime(self.home, config={})
        self.addCleanup(other.close)
        self.assertEqual(first, commit_review(other, self.p, plan, "user-click"))

    def test_request_id_cannot_be_reused_for_another_action(self):
        _, first = self.source()
        self.commit(self.plan(first, "defer"))
        with self.assertRaises(ContractError):
            self.commit(self.plan(first, "reject"))

    def test_reject_prevents_legacy_publication_and_defer_can_be_approved(self):
        _, rejected = self.source("Rejected observation")
        self.commit(self.plan(rejected, "reject"), "reject")
        with self.assertRaises(ContractError):
            self.r.publish(self.p, rejected)
        _, deferred = self.source("Deferred observation")
        self.commit(self.plan(deferred, "defer"), "defer")
        self.assertEqual(candidate_review_list(self.r, self.p)["items"][0]["status"], "deferred")
        self.commit(self.plan(deferred), "approve-deferred")
        self.assertEqual(candidate_review_list(self.r, self.p, status="published")["total"], 1)

    def independent_candidate(self, original, conversation="independent-source", **changes):
        body = json.loads(self.r.db.execute("SELECT body FROM candidates WHERE id=?", (original,)).fetchone()[0])
        self.r.task_update(self.p, conversation, "Record an independent project observation")
        problem = self.r.feedback(self.p, conversation, "Independent reported source", category="project")
        refs = [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        body.update(problem_id=problem["id"], problem_revision=problem["revision"], evidence_refs=refs, **changes)
        return self.r.submit_candidate(self.p, **body)["candidate_id"]

    def test_equivalent_revoked_content_requires_explicit_restore(self):
        _, original = self.source()
        knowledge = self.r.publish(self.p, original)
        self.r.revoke(self.p, knowledge["knowledge_id"], knowledge["revision"])
        new_candidate = self.independent_candidate(original)
        plan = self.plan(new_candidate)
        self.assertFalse(plan["ready"])
        self.assertIn("equivalent_knowledge_revoked", {item["code"] for item in plan["items"][0]["preflight"]["issues"]})
        with self.assertRaisesRegex(ContractError, "revoked"):
            self.r.publish(self.p, new_candidate)
        with self.assertRaises(ContractError):
            self.commit(plan, "cannot-resurrect")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
        self.r.restore(self.p, knowledge["knowledge_id"], knowledge["revision"])
        result = self.commit(self.plan(new_candidate), "attach-after-explicit-restore")
        self.assertEqual(result["results"][0]["knowledge_id"], knowledge["knowledge_id"])

    def test_equivalent_superseded_content_cannot_return_as_new_knowledge(self):
        _, original = self.source()
        knowledge = self.r.publish(self.p, original)
        revision_two = self.independent_candidate(original, content="Use the newer project storage convention")
        updated = self.r.publish(self.p, revision_two, knowledge_id=knowledge["knowledge_id"], expected_revision=1)
        self.assertEqual(updated["revision"], 2)
        stale = self.independent_candidate(original, conversation="stale-report")
        plan = self.plan(stale)
        self.assertFalse(plan["ready"])
        self.assertIn("equivalent_knowledge_superseded", {item["code"] for item in plan["items"][0]["preflight"]["issues"]})
        with self.assertRaisesRegex(ContractError, "superseded"):
            self.r.publish(self.p, stale)
        revision_three = self.independent_candidate(original, conversation="newer-report", content="New explicit revision")
        self.assertEqual(self.r.publish(self.p, revision_three, knowledge_id=knowledge["knowledge_id"], expected_revision=2)["revision"], 3)

    def test_new_counterexample_invalidates_prepared_equivalent_approval(self):
        _, original = self.source()
        knowledge = self.r.publish(self.p, original)
        new_candidate = self.independent_candidate(original)
        plan = self.plan(new_candidate)
        self.assertTrue(plan["ready"])
        self.r.feedback(self.p, "counterexample", "The project storage claim is wrong", category="project",
                        knowledge_id=knowledge["knowledge_id"], knowledge_revision=1, feedback_kind="counterexample")
        with self.assertRaisesRegex(ContractError, "changed"):
            self.commit(plan, "old-preview-must-not-approve")
        refreshed = self.plan(new_candidate)
        self.assertFalse(refreshed["ready"])
        self.assertIn("equivalent_knowledge_disputed", {item["code"] for item in refreshed["items"][0]["preflight"]["issues"]})
        with self.assertRaisesRegex(ContractError, "counterexample"):
            self.r.publish(self.p, new_candidate)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM publication_receipts").fetchone()[0], 1)

    def test_changed_body_invalidates_plan(self):
        _, identity = self.source()
        plan = self.plan(identity)
        body = json.loads(self.r.db.execute("SELECT body FROM candidates WHERE id=?", (identity,)).fetchone()[0])
        body["content"] = "Changed after user preview"
        self.r.db.execute("UPDATE candidates SET body=? WHERE id=?", (encode(body), identity))
        with self.assertRaisesRegex(ContractError, "changed"):
            self.commit(plan)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_changed_source_payload_or_problem_invalidates_plan(self):
        for change in ("event", "problem", "task"):
            with self.subTest(change=change):
                problem, identity = self.source("Change " + change)
                plan = self.plan(identity)
                if change == "event":
                    self.r.db.execute("UPDATE events SET payload='{}' WHERE id IN (SELECT event FROM problem_events WHERE problem=?)", (problem["id"],))
                elif change == "problem":
                    self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (problem["id"],))
                else:
                    self.r.db.execute("UPDATE task_snapshots SET goal='Changed' WHERE project=? AND id=?", (self.p, problem["task_id"]))
                with self.assertRaises(ContractError):
                    self.commit(plan, "changed-" + change)

    def test_deleted_evidence_blocks_publication(self):
        problem, identity = self.source()
        self.r.db.execute("DELETE FROM problem_events WHERE problem=?", (problem["id"],))
        detail = candidate_review_detail(self.r, self.p, identity)
        self.assertFalse(detail["preflight"]["publishable"])
        self.assertTrue(detail["evidence"]["items"][0]["missing"])
        with self.assertRaises(ContractError):
            self.commit(self.plan(identity))

    def test_account_epoch_change_invalidates_plan(self):
        _, identity = self.source()
        plan = self.plan(identity)
        self.r.scope_account_changed()
        with self.assertRaisesRegex(ContractError, "identity changed"):
            self.commit(plan)

    def test_other_host_cannot_commit_prepared_plan(self):
        _, identity = self.source()
        plan = self.plan(identity)
        other = Runtime(self.home, config={}, host="antigravity")
        self.addCleanup(other.close)
        with self.assertRaisesRegex(ContractError, "identity changed"):
            commit_review(other, self.p, plan, "wrong-host")

    def test_atomic_rollback_when_second_publication_fails(self):
        _, first = self.source("First")
        _, second = self.source("Second")
        plan = self.plan([first, second])
        original = self.r.publish
        def failing(project, candidate_id, **kwargs):
            if candidate_id == second:
                raise ContractError("simulated second publication failure")
            return original(project, candidate_id, **kwargs)
        with patch.object(self.r, "publish", side_effect=failing):
            with self.assertRaises(ContractError):
                self.commit(plan)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM publication_receipts").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='CandidateReview'").fetchone()[0], 0)
        self.assertTrue(self.plan([first, second])["ready"])

    def test_budget_paused_history_approval_does_not_resume_job(self):
        job, identity = self.history()
        with self.assertRaises(ContractError):
            self.r.publish(self.p, identity)
        before = dict(self.r.db.execute("SELECT * FROM history_jobs WHERE id=?", (job,)).fetchone())
        detail = candidate_review_detail(self.r, self.p, identity)
        self.assertTrue(detail["preflight"]["history_budget_paused"])
        self.assertTrue(detail["preflight"]["publishable"])
        self.commit(self.plan(identity))
        self.assertEqual(before, dict(self.r.db.execute("SELECT * FROM history_jobs WHERE id=?", (job,)).fetchone()))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_user_pause_cancel_unknown_failure_and_epoch_block_history_approval(self):
        for action in ("pause", "cancel", "failure", "epoch"):
            with self.subTest(action=action):
                job, identity = self.history()
                if action == "pause":
                    self.r.history_pause(self.p, job)
                elif action == "cancel":
                    self.r.history_cancel(self.p, job)
                elif action == "failure":
                    self.r.db.execute("UPDATE history_jobs SET last_result=? WHERE id=?", (encode({"reason": "timeout_unknown"}), job))
                else:
                    self.r.scope_account_changed()
                self.assertFalse(candidate_review_detail(self.r, self.p, identity)["preflight"]["publishable"])
                with self.assertRaises(ContractError):
                    self.commit(self.plan(identity), "blocked-" + action)

    def test_failed_resume_cannot_reuse_an_old_budget_pause_reason(self):
        job, identity = self.history()
        with patch.object(self.r, "_history_materialize", side_effect=ContractError("controlled failed resume")):
            with self.assertRaises(ContractError):
                self.r.history_run(self.p, job)
        self.assertEqual(json.loads(self.r.db.execute("SELECT last_result FROM history_jobs WHERE id=?", (job,)).fetchone()[0])["reason"], "run_failed")
        self.assertFalse(candidate_review_detail(self.r, self.p, identity)["preflight"]["publishable"])

    def test_batch_bounds_filters_and_project_isolation(self):
        _, identity = self.source("100% literal_title")
        self.assertEqual(candidate_review_list(self.r, self.p, q="% literal_")["total"], 1)
        self.assertEqual(candidate_review_list(self.r, self.p, q="missing")["total"], 0)
        other_path = Path(self.temp.name) / "other"
        other_path.mkdir()
        other = self.r.project(other_path)
        self.assertEqual(candidate_review_list(self.r, other)["total"], 0)
        with self.assertRaises(LookupError):
            candidate_review_detail(self.r, other, identity)
        for selections in ([], [identity] * 21, [identity, identity]):
            with self.assertRaises(ContractError):
                self.plan(selections)

    def test_changed_update_target_invalidates_plan(self):
        _, initial = self.source("Initial knowledge")
        published = self.r.publish(self.p, initial)
        _, replacement = self.source("New knowledge content")
        selection = {"candidate_id": replacement, "knowledge_id": published["knowledge_id"], "expected_revision": 1}
        plan = self.plan([selection])
        self.r.revoke(self.p, published["knowledge_id"], expected_revision=1)
        with self.assertRaises(ContractError):
            self.commit(plan)

    def test_small_capture_budget_never_truncates_commit_receipt(self):
        self.r.config["event_max_chars"] = 128
        _, identity = self.source()
        plan = self.plan(identity)
        result = self.commit(plan)
        self.assertEqual(result, self.commit(plan))
        self.assertGreater(len(self.r.db.execute("SELECT payload FROM events WHERE type='CandidateReview'").fetchone()[0]), 128)


if __name__ == "__main__":
    unittest.main()
