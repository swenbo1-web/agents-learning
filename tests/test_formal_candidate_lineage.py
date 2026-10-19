"""Candidate influence cannot evade governance through formal publication.

These tests use isolated SQLite and observed public events, never a model or
real host. A possible influence is not asserted to be actual adoption.
"""
import unittest
from unittest.mock import patch

from agent_learning import candidate_references as references
from agent_learning.candidate_review import prepare_review, commit_review
from agent_learning.knowledge_lineage import install, source_state
from agent_learning.runtime import ContractError
from tests import test_candidate_references as fixtures
from tests import test_history_import as history_fixtures


class FormalCandidateLineageTests(unittest.TestCase):
    setUp = fixtures.CandidateReferenceTests.setUp
    candidate = fixtures.CandidateReferenceTests.candidate
    enable = fixtures.CandidateReferenceTests.enable

    def origin(self):
        self.enable()
        return self.candidate(title="Original allocator convention", conversation="origin")

    def derived(self, upstream, *, title="Derived retention convention", conversation="derived", relation="possible_influence"):
        item = references.assess(self.r, self.p, upstream)
        candidate, body, problem, event, task = self.candidate(title=title,
            content="UNVERIFIED_DERIVED_CONCLUSION", conversation=conversation)
        references.record_lineage(self.r, self.p, event["id"],
            [{"candidate_id": upstream, "reference_version": item["reference_version"]}], relation)
        return candidate, body, problem, event, task

    def approve(self, identity):
        plan = prepare_review(self.r, self.p, [identity], "approve")
        self.assertTrue(plan["ready"], plan)
        result = commit_review(self.r, self.p, plan, "approve-" + identity)
        return result["results"][0]

    def hold(self, identity, action="reject"):
        plan = prepare_review(self.r, self.p, [identity], action, "Operator source decision")
        return commit_review(self.r, self.p, plan, action + "-" + identity)

    def query(self, compact=False):
        return self.r._context(self.p, "consumer", query="Derived retention convention", compact=compact,
                               context_handle=self.handle, workspace=self.workspace)

    def test_approval_and_direct_publish_cannot_bypass_rejected_upstream(self):
        a, *_ = self.origin()
        b, *_ = self.derived(a)
        self.hold(a)
        self.assertFalse(references.assess(self.r, self.p, b)["eligible"])
        plan = prepare_review(self.r, self.p, [b], "approve")
        self.assertFalse(plan["ready"])
        self.assertIn("candidate_reference_source_blocked", {issue["code"] for issue in plan["items"][0]["preflight"]["issues"]})
        with self.assertRaisesRegex(ContractError, "source lineage"):
            self.r.publish(self.p, b)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_existing_explicit_target_does_not_bypass_source_lineage(self):
        target, *_ = self.candidate(title="Independent current fact", conversation="independent")
        current = self.r.publish(self.p, target)
        a, *_ = self.origin()
        b, *_ = self.derived(a)
        self.hold(a)
        plan = prepare_review(self.r, self.p, [{"candidate_id": b, "knowledge_id": current["knowledge_id"],
            "expected_revision": current["revision"]}], "approve")
        self.assertFalse(plan["ready"])
        with self.assertRaisesRegex(ContractError, "source lineage"):
            self.r.publish(self.p, b, knowledge_id=current["knowledge_id"], expected_revision=current["revision"])

    def test_approval_snapshot_detects_changed_upstream_version_before_commit(self):
        a, *_ = self.origin()
        b, *_ = self.derived(a)
        plan = prepare_review(self.r, self.p, [b], "approve")
        self.hold(a)
        with self.assertRaisesRegex(ContractError, "changed"):
            commit_review(self.r, self.p, plan, "stale-approved-source")

    def test_rejection_after_publication_preserves_body_but_blocks_injection_and_notifies(self):
        a, *_ = self.origin()
        b, *_ = self.derived(a)
        published = self.approve(b)
        before = self.r.read(self.p, published["knowledge_id"])
        self.assertIn("UNVERIFIED_DERIVED_CONCLUSION", self.query())
        self.hold(a)
        current = self.r.read(self.p, published["knowledge_id"])
        self.assertEqual(current["content"], before["content"])
        self.assertEqual(current["revision"], before["revision"])
        self.assertEqual(current["source_health"], "conflicted")
        self.assertGreater(current["source_version"], before["source_version"])
        self.assertEqual(self.r.search(self.p, "Derived retention convention", _automatic=True)["items"], [])
        notice = self.query(compact=True)
        self.assertIn(published["knowledge_id"], notice)
        self.assertIn("来源已变", notice)
        self.assertNotIn("UNVERIFIED_DERIVED_CONCLUSION", notice)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_deferred_upstream_and_small_budget_keep_complete_notice_pending(self):
        a, *_ = self.origin()
        b, *_ = self.derived(a)
        published = self.approve(b)
        self.query()
        self.hold(a, "defer")
        with self.r.transaction():
            self.assertEqual(self.r._notifications(self.p, "consumer", None, 1), [])
            notes = self.r._notifications(self.p, "consumer", None, 2400)
        self.assertEqual(len(notes), 1)
        self.assertIn(published["knowledge_id"], notes[0])

    def test_upstream_concern_and_explicit_resolution_invalidate_formal_sources(self):
        a, *_ = self.origin()
        b, *_ = self.derived(a)
        published = self.approve(b)
        before = self.r.read(self.p, published["knowledge_id"])
        item = references.assess(self.r, self.p, a)
        references.record_exposure(self.r, self.p, self.handle, item)
        references.feedback(self.r, self.p, self.handle, a, item["reference_version"],
            summary="Original recommendation needs checking", idempotency_key="upstream-concern",
            expected_task_id=self.target["id"], expected_task_revision=self.target["revision"])
        disputed = self.r.read(self.p, published["knowledge_id"])
        self.assertEqual(disputed["source_health"], "conflicted")
        self.assertGreater(disputed["source_version"], before["source_version"])
        concern = references.feedback_state(self.r, self.p, a)
        references.resolve_feedback(self.r, self.p, a, concern["version"], "Compared the original evidence explicitly")
        resolved = self.r.read(self.p, published["knowledge_id"])
        self.assertEqual(resolved["source_health"], "valid")
        self.assertGreater(resolved["source_version"], disputed["source_version"])

    def test_transitive_dependency_and_source_revision_change(self):
        a, _, origin_problem, *_ = self.origin()
        b, *_ = self.derived(a, title="Middle allocator finding", conversation="middle")
        c, *_ = self.derived(b)
        published = self.approve(c)
        before = self.r.read(self.p, published["knowledge_id"])
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (origin_problem["id"],))
        after = self.r.read(self.p, published["knowledge_id"])
        self.assertGreater(after["source_version"], before["source_version"])
        self.assertEqual(after["source_health"], "conflicted")

    def test_failed_upstream_observation_dirties_downstream(self):
        a, _, _, event, _ = self.origin()
        b, *_ = self.derived(a)
        published = self.approve(b)
        before = self.r.read(self.p, published["knowledge_id"])
        self.r.db.execute("UPDATE events SET outcome='failed' WHERE id=?", (event["id"],))
        after = self.r.read(self.p, published["knowledge_id"])
        self.assertGreater(after["source_version"], before["source_version"])
        self.assertEqual(after["source_health"], "conflicted")

    def test_published_upstream_counterexample_and_revocation_propagate(self):
        a, *_ = self.origin()
        upstream = self.r.publish(self.p, a)
        b, *_ = self.derived(a)
        published = self.approve(b)
        before = self.r.read(self.p, published["knowledge_id"])
        self.r.feedback(self.p, "formal-counterexample", "The allocator conclusion failed",
            knowledge_id=upstream["knowledge_id"], knowledge_revision=upstream["revision"], feedback_kind="counterexample")
        disputed = self.r.read(self.p, published["knowledge_id"])
        self.assertGreater(disputed["source_version"], before["source_version"])
        self.assertEqual(disputed["source_health"], "conflicted")
        self.r.revoke(self.p, upstream["knowledge_id"], upstream["revision"], request_id="revoke-upstream", reason="Invalid source")
        revoked = self.r.read(self.p, published["knowledge_id"])
        self.assertGreater(revoked["source_version"], disputed["source_version"])
        self.assertEqual(revoked["source_health"], "conflicted")
        self.assertEqual(revoked["revision"], published["revision"])

    def test_late_lineage_relation_dirties_existing_formal_knowledge(self):
        a, *_ = self.origin()
        item = references.assess(self.r, self.p, a)
        b, _, _, event, _ = self.candidate(title="Derived retention convention", conversation="derived")
        published = self.approve(b)
        before = self.r.read(self.p, published["knowledge_id"])
        self.hold(a)
        references.record_lineage(self.r, self.p, event["id"],
            [{"candidate_id": a, "reference_version": item["reference_version"]}])
        after = self.r.read(self.p, published["knowledge_id"])
        self.assertGreater(after["source_version"], before["source_version"])
        self.assertEqual(after["source_health"], "conflicted")

    def test_independent_tool_observation_does_not_inherit_unrelated_reference(self):
        a, *_ = self.origin()
        item = references.assess(self.r, self.p, a)
        references.record_exposure(self.r, self.p, self.handle, item)
        independent, *_ = self.candidate(title="Independent measured allocation result", conversation="consumer",
            kind="PostToolUse", outcome="succeeded", validation="tool_success")
        published = self.approve(independent)
        before = self.r.read(self.p, published["knowledge_id"])
        self.hold(a)
        after = self.r.read(self.p, published["knowledge_id"])
        self.assertEqual(after["source_health"], "valid")
        self.assertEqual(after["source_version"], before["source_version"])

    def test_cycle_and_shared_graph_budget_do_not_silently_allow_publication(self):
        a, _, _, source_a, _ = self.origin()
        b, *_ = self.derived(a)
        item = references.assess(self.r, self.p, b)
        references.record_lineage(self.r, self.p, source_a["id"],
            [{"candidate_id": b, "reference_version": item["reference_version"]}])
        self.assertTrue(source_state(self.r, self.p, [source_a["id"]])["blocked"])
        with self.assertRaisesRegex(ContractError, "source lineage"):
            self.r.publish(self.p, b)
        with patch.object(references, "MAX_ASSESSMENTS", 0):
            self.assertTrue(source_state(self.r, self.p, [source_a["id"]])["blocked"])

    def test_projection_rebuild_is_idempotent_and_dirties_existing_exposures(self):
        a, *_ = self.origin()
        b, *_ = self.derived(a)
        published = self.approve(b)
        before = self.r.read(self.p, published["knowledge_id"])
        self.r.db.execute("DELETE FROM candidate_lineage_dependencies")
        self.r.db.execute("DELETE FROM knowledge_lineage_projection_state")
        with self.r.transaction():
            install(self.r)
        rebuilt = self.r.read(self.p, published["knowledge_id"])
        self.assertGreater(rebuilt["source_version"], before["source_version"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidate_lineage_dependencies").fetchone()[0], 1)
        with self.r.transaction():
            install(self.r)
        self.assertEqual(self.r.read(self.p, published["knowledge_id"])["source_version"], rebuilt["source_version"])

    def test_unrelated_project_and_unrelated_candidate_do_not_dirty_formal_knowledge(self):
        a, *_ = self.origin()
        b, *_ = self.derived(a)
        published = self.approve(b)
        unrelated, *_ = self.candidate(title="Unrelated convention", conversation="unrelated")
        before = self.r.read(self.p, published["knowledge_id"])
        self.hold(unrelated)
        self.assertEqual(self.r.read(self.p, published["knowledge_id"])["source_version"], before["source_version"])
        original = self.p
        self.p = self.r.project(self.root / "other-project")
        try:
            other_a, *_ = self.candidate(title="Other project source", conversation="other-origin")
            other_b, *_ = self.derived(other_a, conversation="other-derived")
            self.approve(other_b)
            self.hold(other_a)
        finally:
            self.p = original
        self.assertEqual(self.r.read(self.p, published["knowledge_id"])["source_version"], before["source_version"])


class HistoricalFormalLineageTests(unittest.TestCase):
    setUp = history_fixtures.HistoryImportTests.setUp
    prepare = history_fixtures.HistoryImportTests.prepare
    materialize = history_fixtures.HistoryImportTests.materialize
    case = history_fixtures.HistoryImportTests.case
    candidate = history_fixtures.HistoryImportTests.candidate

    def published_derivation(self):
        job = self.materialize()
        upstream = self.candidate(job)["candidate_id"]
        references.configure(self.r, self.p, True, 0)
        references.grant_history(self.r, self.p, job, True, 0)
        item = references.assess(self.r, self.p, upstream)
        self.assertTrue(item["eligible"])
        task = self.r.task_update(self.p, "derived-history", "Compare the authorized archived clue")
        event, _ = self.r._event(self.p, "derived-history", "Stop", {"last_assistant_message": "Reported comparison"},
            outcome="reported", task_id=task["id"], task_revision=task["revision"])
        problem = self.r._problem(self.p, "derived-history", "Reported comparison", "project", [event["id"]], task_binding=task)
        references.record_lineage(self.r, self.p, event["id"],
            [{"candidate_id": upstream, "reference_version": item["reference_version"]}], "possible_influence")
        downstream = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="Project archive comparison", content="Historical project convention is a reported clue",
            category="project", knowledge_kind="fact", applicability="This source revision",
            limitations="Public report, not independent verification", evidence_refs=[event["id"]], validation_level="reported")
        plan = prepare_review(self.r, self.p, [downstream["candidate_id"]], "approve")
        self.assertTrue(plan["ready"])
        published = commit_review(self.r, self.p, plan, "approve-authorized-comparison")["results"][0]
        return job, published

    def test_withdrawing_batch_reference_grant_dirties_derived_formal_without_resuming_history(self):
        job, published = self.published_derivation()
        before = self.r.read(self.p, published["knowledge_id"])
        references.grant_history(self.r, self.p, job, False, 1)
        blocked = self.r.read(self.p, published["knowledge_id"])
        self.assertGreater(blocked["source_version"], before["source_version"])
        self.assertEqual(blocked["source_health"], "conflicted")
        self.assertEqual(blocked["content"], before["content"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
        references.grant_history(self.r, self.p, job, True, 2)
        self.assertEqual(self.r.read(self.p, published["knowledge_id"])["source_health"], "valid")

    def test_shared_governance_epoch_invalidates_only_derivation_not_formal_body(self):
        _, published = self.published_derivation()
        before = self.r.read(self.p, published["knowledge_id"])
        self.r.db.execute("UPDATE host_state SET epoch=epoch+1 WHERE id=1")
        after = self.r.read(self.p, published["knowledge_id"])
        self.assertGreater(after["source_version"], before["source_version"])
        self.assertEqual(after["source_health"], "conflicted")
        self.assertEqual(after["content"], before["content"])
        self.assertEqual(after["revision"], before["revision"])


if __name__ == "__main__":
    unittest.main()
