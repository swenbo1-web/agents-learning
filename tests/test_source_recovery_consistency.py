"""Recovered source evidence stays healthy across candidate publication.

Every event and database here is synthetic. Tool success never represents user
acceptance, and publication remains an explicit fixture action.
"""
import unittest
from unittest.mock import patch

from agent_learning import candidate_references as references
from agent_learning import knowledge_index
from agent_learning.admin_publication import _verified_gate
from agent_learning.knowledge_lineage import published_reference_state
from tests import test_formal_candidate_lineage as fixtures


class SourceRecoveryConsistencyTests(unittest.TestCase):
    setUp = fixtures.FormalCandidateLineageTests.setUp
    candidate = fixtures.FormalCandidateLineageTests.candidate
    enable = fixtures.FormalCandidateLineageTests.enable
    derived = fixtures.FormalCandidateLineageTests.derived
    approve = fixtures.FormalCandidateLineageTests.approve
    query = fixtures.FormalCandidateLineageTests.query

    def recovered(self, outcomes=("failed", "succeeded"), *, conversation="origin",
                  title="Original allocator convention", validation="tool_success"):
        self.enable()
        task = self.r.task_update(self.p, conversation, title, new_task=True)
        events = []
        for number, outcome in enumerate(outcomes):
            event, _ = self.r._event(self.p, conversation, "PostToolUse",
                {"tool_name": "Bash", "tool_input": {"command": "python verify_allocator.py"},
                 "tool_response": {"exit_code": 0 if outcome == "succeeded" else 1}},
                event_id=f"{conversation}-{number}", task_id=task["id"],
                task_revision=task["revision"], outcome=outcome)
            events.append(event)
        problem = self.r._problem(self.p, conversation, title, "operation", [e["id"] for e in events],
            task_binding=task, evidence_roles={e["id"]: "failure" if e["outcome"] == "failed"
                                             else "potential_recovery" for e in events})
        body = {"problem_id": problem["id"], "problem_revision": problem["revision"],
            "title": title, "content": "Inspect retained allocations and verify allocator recovery",
            "category": "operation", "knowledge_kind": "solution", "metadata": {},
            "applicability": "Only this allocator verification target and source case",
            "limitations": "Observed tool result; user acceptance remains unknown",
            "validation_level": validation, "evidence_refs": [e["id"] for e in events]}
        identity = self.r.submit_candidate(self.p, **body)["candidate_id"]
        return identity, body, problem, events, task

    def formal_state(self, publication):
        return published_reference_state(self.r, self.p, dict(publication, active=True),
                                         references._Assessment(), ())

    def test_recovered_source_publication_preserves_downstream_health_and_injection(self):
        upstream, body, problem, _, _ = self.recovered()
        self.assertTrue(references.assess(self.r, self.p, upstream)["eligible"])
        self.assertIsNone(_verified_gate(self.r, self.p, {"problem": problem}, body,
                                        {"action": "publish", "knowledge_id": None}))
        child, *_ = self.derived(upstream)
        published = self.approve(child)
        before = self.r.read(self.p, published["knowledge_id"])
        self.assertEqual(before["source_health"], "valid")
        self.assertIn("UNVERIFIED_DERIVED_CONCLUSION", self.query())

        original = self.approve(upstream)
        self.assertFalse(self.formal_state(original)["blocked"])
        after = self.r.read(self.p, published["knowledge_id"])
        self.assertEqual(after["source_health"], "valid")
        self.assertEqual(after["revision"], before["revision"])
        self.assertGreater(after["source_version"], before["source_version"])
        self.assertIn("UNVERIFIED_DERIVED_CONCLUSION", self.query(compact=True))
        self.assertIn(published["knowledge_id"], [item["knowledge_id"] for item in self.r.search(
            self.p, "Derived retention convention", _automatic=True)["items"]])

    def test_recovered_publication_preserves_pending_child_and_work_history(self):
        upstream, *_ = self.recovered()
        child, _, _, event, _ = self.derived(upstream)
        self.r.config["work_memory"]["enabled"] = True
        self.r._work_capture_event(self.p, event["id"], self.workspace)
        row = self.r.db.execute("SELECT * FROM work_memories WHERE project=? AND event_id=?",
                                (self.p, event["id"])).fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(references.assess(self.r, self.p, child)["eligible"])
        self.assertNotEqual(self.r._work_item(self.p, row).get("automatic_eligible"), False)
        self.approve(upstream)
        self.assertTrue(references.assess(self.r, self.p, child)["eligible"])
        observed = self.r._work_item(self.p, row)
        self.assertNotEqual(observed.get("automatic_eligible"), False)
        self.assertNotIn("candidate_reference_risk", observed)

    def test_success_from_another_source_cannot_recover_an_unresolved_source(self):
        failed, *_ = self.recovered(("failed",), validation="reported")
        original = self.r.publish(self.p, failed)
        healthy, *_ = self.recovered(("succeeded",), conversation="independent", validation="reported")
        attached = self.r.publish(self.p, healthy)
        self.assertEqual(attached["knowledge_id"], original["knowledge_id"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM knowledge_sources WHERE knowledge_id=?",
                                        (original["knowledge_id"],)).fetchone()[0], 2)
        self.assertTrue(self.formal_state(original)["blocked"])

    def test_failure_after_cited_recovery_remains_blocked(self):
        identity, *_ = self.recovered(("failed", "succeeded", "failed"))
        self.assertFalse(references.assess(self.r, self.p, identity)["eligible"])
        publication = self.r.publish(self.p, identity)
        self.assertEqual(self.formal_state(publication)["reason"], "published_reference_source_unhealthy")

    def test_recovery_does_not_override_denied_conflicted_or_missing_evidence(self):
        for index, kind in enumerate(("denied", "conflict", "missing")):
            with self.subTest(kind=kind):
                identity, _, _, events, task = self.recovered(conversation="hard-" + str(index),
                    title="Hard boundary " + str(index))
                publication = self.r.publish(self.p, identity)
                if kind == "missing":
                    self.r.db.execute("DELETE FROM events WHERE id=?", (events[0]["id"],))
                else:
                    operation = "hard-operation-" + str(index)
                    self.r.db.execute("INSERT INTO operations(project,id,conversation,task_id,task_revision,agent,"
                        "state,last_event,conflict,decision) VALUES(?,?,?,?,?,NULL,'failed',?,?,?)",
                        (self.p, operation, task["conversation"], task["id"], task["revision"], events[0]["id"],
                         int(kind == "conflict"), "denied" if kind == "denied" else "allow"))
                    self.r.db.execute("UPDATE events SET operation=? WHERE id=?", (operation, events[0]["id"]))
                self.assertTrue(self.formal_state(publication)["blocked"])

    def test_raw_denial_and_hard_source_roles_match_before_and_after_publication(self):
        for number, (outcome, role) in enumerate((("denied", "observation"), ("conflict", "observation"),
                                                 ("failed", "feedback"), ("failed", "conflict"))):
            with self.subTest(outcome=outcome, role=role):
                identity, _, problem, events, _ = self.recovered(conversation="hard-role-" + str(number),
                    title="Hard source role " + str(number))
                self.r.db.execute("UPDATE events SET outcome=? WHERE id=?", (outcome, events[0]["id"]))
                self.r.db.execute("UPDATE problem_events SET role=? WHERE problem=? AND event=?",
                                  (role, problem["id"], events[0]["id"]))
                self.assertFalse(references.assess(self.r, self.p, identity)["eligible"])
                publication = self.r.publish(self.p, identity)
                self.assertTrue(self.formal_state(publication)["blocked"])

    def test_later_success_from_a_mismatched_conversation_cannot_repair_source(self):
        identity, _, _, events, _ = self.recovered()
        self.r.db.execute("UPDATE events SET conversation='different-conversation' WHERE id=?", (events[1]["id"],))
        self.assertEqual(references.assess(self.r, self.p, identity)["reason"], "source_identity_mismatch")
        publication = self.r.publish(self.p, identity)
        self.assertEqual(self.formal_state(publication)["reason"], "published_reference_source_unhealthy")

    def test_recovered_source_still_respects_case_change_dispute_and_revocation(self):
        identity, _, problem, _, _ = self.recovered()
        publication = self.approve(identity)
        self.assertFalse(self.formal_state(publication)["blocked"])
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (problem["id"],))
        self.assertEqual(self.formal_state(publication)["reason"], "published_reference_source_changed")
        self.r.db.execute("UPDATE problems SET revision=revision-1 WHERE id=?", (problem["id"],))
        self.r.feedback(self.p, "counterexample", "Independent allocator counterexample",
            knowledge_id=publication["knowledge_id"], knowledge_revision=publication["revision"],
            feedback_kind="counterexample")
        self.assertEqual(self.formal_state(publication)["reason"], "published_reference_disputed")
        self.r.revoke(self.p, publication["knowledge_id"], publication["revision"])
        self.assertTrue(published_reference_state(self.r, self.p, dict(publication, active=False),
                                                references._Assessment(), ())["blocked"])

    def test_previous_policy_negative_cache_is_not_reused_after_recovery_fix(self):
        upstream, *_ = self.recovered()
        child, *_ = self.derived(upstream)
        publication = self.approve(child)
        self.approve(upstream)
        old_fault = {"blocked": True, "reason": "published_reference_source_unhealthy"}
        with patch.object(knowledge_index, "_MATCH_POLICY_VERSION", 5), \
             patch("agent_learning.knowledge_lineage.published_reference_state", return_value=old_fault):
            self.assertEqual(self.r.search(self.p, "Derived retention convention", _automatic=True)["items"], [])
        self.assertEqual(self.r.read(self.p, publication["knowledge_id"])["source_health"], "valid")
        self.assertIn(publication["knowledge_id"], [item["knowledge_id"] for item in self.r.search(
            self.p, "Derived retention convention", _automatic=True)["items"]])

    def test_previous_context_plan_retrieves_again_without_a_new_source_event(self):
        upstream, *_ = self.recovered()
        child, *_ = self.derived(upstream)
        publication = self.approve(child)
        self.approve(upstream)
        old_fault = {"blocked": True, "reason": "published_reference_source_unhealthy"}
        with patch.object(knowledge_index, "_MATCH_POLICY_VERSION", 5), \
             patch("agent_learning.knowledge_lineage.published_reference_state", return_value=old_fault):
            self.assertNotIn("UNVERIFIED_DERIVED_CONCLUSION", self.query())
        before = self.r.read(self.p, publication["knowledge_id"])["source_version"]
        self.assertIn("UNVERIFIED_DERIVED_CONCLUSION", self.query())
        self.assertEqual(self.r.read(self.p, publication["knowledge_id"])["source_version"], before)


if __name__ == "__main__":
    unittest.main()
