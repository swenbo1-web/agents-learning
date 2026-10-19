"""All publication entry points preserve unresolved candidate concerns."""
import unittest
from agent_learning import candidate_references as refs
from agent_learning.candidate_review import prepare_review
from agent_learning.runtime import ContractError
from tests import test_candidate_references as fixtures


class PublicationReferenceGuardTests(unittest.TestCase):
    setUp = fixtures.CandidateReferenceTests.setUp
    candidate = fixtures.CandidateReferenceTests.candidate
    enable = fixtures.CandidateReferenceTests.enable

    def concern(self, identity):
        self.enable()
        item = refs.assess(self.r, self.p, identity)
        refs.record_exposure(self.r, self.p, self.handle, item)
        refs.feedback(self.r, self.p, self.handle, identity, item["reference_version"],
            summary="This candidate statement requires checking", idempotency_key="publication-concern",
            expected_task_id=self.target["id"], expected_task_revision=self.target["revision"])

    def resolve(self, identity):
        current = refs.feedback_state(self.r, self.p, identity)
        refs.resolve_feedback(self.r, self.p, identity, current["version"], "Compared the original evidence explicitly")

    def test_direct_publication_cannot_bypass_own_or_equivalent_concerns(self):
        original, _, _, _, _ = self.candidate()
        self.concern(original)
        equivalent, _, _, _, _ = self.candidate(conversation="other-source")
        for identity in (original, equivalent):
            with self.subTest(candidate=identity):
                self.assertFalse(prepare_review(self.r, self.p, [identity], "approve")["ready"])
                with self.assertRaisesRegex(ContractError, "concerns"):
                    self.r.publish(self.p, identity)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
        self.resolve(original)
        self.assertEqual(self.r.publish(self.p, equivalent)["status"], "published")

    def test_resolving_concerns_does_not_skip_current_source_validation(self):
        original, _, problem, _, _ = self.candidate()
        self.concern(original)
        self.resolve(original)
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (problem["id"],))
        with self.assertRaisesRegex(ContractError, "problem changed"):
            self.r.publish(self.p, original)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM publication_receipts").fetchone()[0], 0)

    def test_deferred_equivalent_does_not_hide_older_unresolved_concern(self):
        original, _, _, _, _ = self.candidate()
        self.concern(original)
        held, _, _, _, _ = self.candidate(conversation="later-held-source")
        self.r.db.execute("UPDATE candidates SET status='deferred' WHERE id=?", (held,))
        fresh, _, _, _, _ = self.candidate(conversation="fresh-source")
        with self.assertRaisesRegex(ContractError, "unresolved concerns"):
            self.r.publish(self.p, fresh)
        self.resolve(original)
        self.assertEqual(self.r.publish(self.p, held)["status"], "published",
                         "Explicit approval of a deferred candidate remains a valid operation")

    def test_comparison_bound_cannot_silently_allow_publication(self):
        for number in range(33):
            identity, _, _, _, _ = self.candidate(content=f"Independent content {number}", conversation=f"source-{number}")
        plan = prepare_review(self.r, self.p, [identity], "approve")
        self.assertFalse(plan["ready"])
        self.assertIn("candidate_comparison_limit", {item["code"] for item in plan["items"][0]["preflight"]["issues"]})
        with self.assertRaisesRegex(ContractError, "comparison requires review"):
            self.r.publish(self.p, identity)

    def test_current_explicit_revision_can_accept_equivalent_new_source(self):
        first, _, _, _, _ = self.candidate(conversation="original-source")
        knowledge = self.r.publish(self.p, first)
        changed, _, _, _, _ = self.candidate(content="A different current convention", conversation="change-source")
        second = self.r.publish(self.p, changed, knowledge_id=knowledge["knowledge_id"], expected_revision=1)
        restored, _, _, _, _ = self.candidate(conversation="explicit-revision-source")
        current = self.r.publish(self.p, restored, knowledge_id=knowledge["knowledge_id"], expected_revision=second["revision"])
        fresh, _, _, _, _ = self.candidate(conversation="additional-current-source")
        result = self.r.publish(self.p, fresh)
        self.assertTrue(result["deduplicated"])
        self.assertEqual((result["knowledge_id"], result["revision"]), (current["knowledge_id"], current["revision"]))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
