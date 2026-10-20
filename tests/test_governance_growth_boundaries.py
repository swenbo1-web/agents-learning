"""Source accumulation is not a provenance fault; explicit review can recover.

All fixtures stay under build/local and use only simulated public observations.
"""
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_learning import candidate_references as refs
from agent_learning.candidate_review import commit_review, prepare_review
from agent_learning.knowledge_lineage import published_reference_state
from agent_learning.runtime import ContractError
from tests import test_candidate_references as fixtures


class GovernanceGrowthBoundaryTests(unittest.TestCase):
    candidate = fixtures.CandidateReferenceTests.candidate
    enable = fixtures.CandidateReferenceTests.enable

    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "build" / "local" / "fix145-governance"
        root.mkdir(parents=True, exist_ok=True)
        with patch("tempfile.tempdir", str(root)):
            fixtures.CandidateReferenceTests.setUp(self)
        self.enable()

    def sources(self, count=70):
        entries = [self.candidate(title="Shared source convention", conversation=f"original-{number}")
                   for number in range(count)]
        for identity, *_ in entries:
            publication = self.r.publish(self.p, identity)
        return publication, entries

    def state(self, publication, evaluation=None):
        return published_reference_state(self.r, self.p, dict(publication, active=True),
                                         evaluation or refs._Assessment(), ())

    def variants(self):
        first, *_ = self.candidate(title="Variant convention", content="Current original convention", conversation="original")
        publication = self.r.publish(self.p, first)
        others = [self.candidate(title="Variant convention", content=f"Alternative convention {number}",
                                 conversation=f"variant-{number}")[0] for number in range(33)]
        return publication, first, others

    def test_ninety_six_normal_sources_do_not_consume_graph_budget(self):
        publication, _ = self.sources(96)
        evaluation = refs._Assessment()
        evaluation.remaining["sources"] = 0
        statements = []
        self.r.db.set_trace_callback(statements.append)
        try:
            with patch("subprocess.Popen") as model:
                result = self.state(publication, evaluation)
                model.assert_not_called()
        finally:
            self.r.db.set_trace_callback(None)
        self.assertFalse(result["blocked"], result)
        self.assertFalse(evaluation.exhausted)
        self.assertEqual(evaluation.remaining["sources"], 0)
        self.assertLessEqual(len(statements), 8, "No per-source authority SELECT loop")

    def test_sixty_fifth_source_keeps_existing_downstream_healthy(self):
        publication, entries = self.sources(64)
        child, _, _, event, _ = self.candidate(title="Derived source convention", conversation="downstream")
        refs.record_lineage(self.r, self.p, event["id"],
                            [{"candidate_id": entries[0][0], "reference_version": "a" * 64}])
        derived = self.r.publish(self.p, child)
        self.assertEqual(self.r.read(self.p, derived["knowledge_id"])["source_health"], "valid")
        identity, *_ = self.candidate(title="Shared source convention", conversation="original-65")
        self.r.publish(self.p, identity)
        current = self.r.read(self.p, derived["knowledge_id"])
        self.assertEqual(current["source_health"], "valid")
        self.assertEqual(current["revision"], derived["revision"])

    def test_unhealthy_tail_and_changed_case_are_still_blocked(self):
        publication, entries = self.sources()
        _, _, problem, event, _ = entries[-1]
        self.r.db.execute("UPDATE events SET outcome='failed' WHERE id=?", (event["id"],))
        self.assertEqual(self.state(publication)["reason"], "published_reference_source_unhealthy")
        self.r.db.execute("UPDATE events SET outcome='reported' WHERE id=?", (event["id"],))
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (problem["id"],))
        self.assertEqual(self.state(publication)["reason"], "published_reference_source_changed")

    def test_missing_event_and_malformed_source_json_have_bounded_refusals(self):
        publication, entries = self.sources()
        identity, _, problem, event, _ = entries[-1]
        self.r.db.execute("DELETE FROM problem_events WHERE problem=? AND event=?", (problem["id"], event["id"]))
        self.assertEqual(self.state(publication)["reason"], "published_reference_source_unhealthy")
        for invalid in ("{", "{}", "null", "[]"):
            with self.subTest(source=invalid):
                self.r.db.execute("UPDATE knowledge_sources SET evidence_refs=? WHERE candidate_id=?", (invalid, identity))
                self.assertEqual(self.state(publication)["reason"], "published_reference_source_unavailable")

    def test_published_counterexample_requires_resolution_despite_many_sources(self):
        publication, _ = self.sources()
        self.r.feedback(self.p, "counterexample", "The claimed convention failed",
                        knowledge_id=publication["knowledge_id"], knowledge_revision=1, feedback_kind="counterexample")
        self.assertEqual(self.state(publication)["reason"], "published_reference_disputed")
        self.r.db.execute("UPDATE knowledge_feedback SET disposition='dismissed',disposition_reason='Independent check',"
                          "disposition_receipt='simulated-resolution' WHERE knowledge_id=?", (publication["knowledge_id"],))
        self.assertFalse(self.state(publication)["blocked"])

    def test_actual_candidate_graph_still_has_a_shared_bound(self):
        publication, entries = self.sources(65)
        leaf, *_ = self.candidate(title="Independent graph root", conversation="graph-root")
        for index, (_, _, _, event, _) in enumerate(entries):
            refs.record_lineage(self.r, self.p, event["id"], [{"candidate_id": leaf, "reference_version": "b" * 64}])
            if index == 63:
                self.assertFalse(self.state(publication)["blocked"])
        result = self.state(publication)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["reason"], "candidate_lineage_budget_exceeded")

    def test_many_variants_can_attach_an_exact_current_claim(self):
        publication, _, _ = self.variants()
        identity, body, *_ = self.candidate(title="Variant convention", content="Current original convention", conversation="duplicate")
        self.assertIsNone(refs.held_equivalent(self.r, self.p, body))
        plan = prepare_review(self.r, self.p, [identity], "approve")
        self.assertTrue(plan["ready"], plan)
        result = commit_review(self.r, self.p, plan, "exact-merge")["results"][0]
        self.assertTrue(result["deduplicated"])
        self.assertEqual(result["knowledge_id"], publication["knowledge_id"])

    def test_many_variants_can_explicitly_update_current_revision(self):
        publication, _, others = self.variants()
        selection = {"candidate_id": others[-1], "knowledge_id": publication["knowledge_id"], "expected_revision": 1}
        self.assertFalse(prepare_review(self.r, self.p, [others[-1]], "approve")["ready"])
        plan = prepare_review(self.r, self.p, [selection], "approve")
        self.assertTrue(plan["ready"], plan)
        result = commit_review(self.r, self.p, plan, "explicit-new-revision")["results"][0]
        self.assertEqual(result["revision"], 2)
        with self.assertRaisesRegex(ContractError, "revision changed"):
            self.r.publish(self.p, others[-2], knowledge_id=publication["knowledge_id"], expected_revision=1)
        self.r.revoke(self.p, publication["knowledge_id"], 2)
        with self.assertRaisesRegex(ContractError, "revoked"):
            self.r.publish(self.p, others[-2], knowledge_id=publication["knowledge_id"], expected_revision=2)

    def test_rejecting_distinct_alternatives_recovers_new_manual_claim(self):
        _, _, others = self.variants()
        identity = others[-1]
        self.assertFalse(prepare_review(self.r, self.p, [identity], "approve")["ready"])
        self.r.db.executemany("UPDATE candidates SET status='rejected' WHERE id=?", [(other,) for other in others[:-1]])
        self.assertTrue(prepare_review(self.r, self.p, [identity], "approve")["ready"])

    def test_exact_merge_and_explicit_update_preserve_unresolved_candidate_concern(self):
        publication, original, others = self.variants()
        self.r.db.execute("INSERT INTO candidate_reference_feedback VALUES(?,?,?,?,?,?,?)",
                          (self.p, original, "a" * 64, "operator-concern", "unresolved", "Original evidence questioned", 1.0))
        identity, body, *_ = self.candidate(title="Variant convention", content="Current original convention", conversation="duplicate")
        self.assertEqual(refs.held_equivalent(self.r, self.p, body), "equivalent candidate has unresolved concerns")
        self.assertFalse(prepare_review(self.r, self.p, [identity], "approve")["ready"])
        with self.assertRaisesRegex(ContractError, "unresolved concerns"):
            self.r.publish(self.p, identity, knowledge_id=publication["knowledge_id"], expected_revision=1)

    def test_exact_active_merge_does_not_resurrect_formal_counterexample(self):
        publication, _, _ = self.variants()
        self.r.feedback(self.p, "counterexample", "The current convention failed",
                        knowledge_id=publication["knowledge_id"], knowledge_revision=1, feedback_kind="counterexample")
        identity, *_ = self.candidate(title="Variant convention", content="Current original convention", conversation="duplicate")
        self.assertFalse(prepare_review(self.r, self.p, [identity], "approve")["ready"])
        with self.assertRaisesRegex(ContractError, "unresolved counterexample"):
            self.r.publish(self.p, identity)

    def test_long_query_candidate_lane_preserves_error_identity(self):
        wanted, *_ = self.candidate(title="E_CRITICAL recovery", content="E_CRITICAL requires original evidence",
                                    conversation="wanted")
        self.candidate(title="按钮颜色设置", content="按钮颜色设置使用绿色", conversation="unrelated")
        query = "E_CRITICAL " + "filler " * 1400 + " 按钮颜色设置"
        result = refs.search(self.r, self.p, query)
        self.assertEqual([item["candidate_id"] for item in result], [wanted])

    def test_candidate_lane_rejects_unsafe_identity_overflow(self):
        self.candidate(title="按钮颜色设置", content="按钮颜色设置使用绿色", conversation="unrelated")
        query = " ".join(f"module{index}.py" for index in range(129)) + " E_CRITICAL 按钮颜色设置"
        self.assertEqual(refs.search(self.r, self.p, query), [])

    def test_candidate_windows_path_requires_the_actual_directory(self):
        self.candidate(title="auth.py 检查", content="auth.py 修复检查", conversation="unrelated")
        wanted, *_ = self.candidate(title="src/auth.py 检查", content="src/auth.py 修复检查", conversation="wanted")
        for query in (r"检查 src\auth.py", "检查 src/auth.py"):
            self.assertEqual([item["candidate_id"] for item in refs.search(self.r, self.p, query)], [wanted])

    def test_candidate_posix_query_recalls_windows_source_without_other_directory(self):
        self.candidate(title=r"legacy\auth.py 检查", content=r"legacy\auth.py 旧配置", conversation="other-directory")
        self.candidate(title="auth.py 检查", content="auth.py 不限定目录", conversation="no-directory")
        wanted, *_ = self.candidate(title=r"src\auth.py 检查", content=r"src\auth.py 修复配置", conversation="windows")
        for query in (r"检查 src\auth.py", "检查 src/auth.py"):
            self.assertEqual([item["candidate_id"] for item in refs.search(self.r, self.p, query)], [wanted])


if __name__ == "__main__":
    unittest.main()
