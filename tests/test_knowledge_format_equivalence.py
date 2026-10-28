"""Only lossless leading labels may share a claim; governance remains explicit."""
import json
import unittest

from agent_learning import candidate_references as references
from agent_learning.admin_governance import propose_governance, execute_governance
from agent_learning.candidate_review import prepare_review, commit_review
from agent_learning.knowledge_equivalence import format_variants, presentation_fingerprints, same_presentation_claim
from agent_learning.runtime import ContractError, knowledge_fingerprint
from tests import test_candidate_equivalent_governance as fixtures
from tests import test_module7_releases as release_fixtures


class KnowledgeFormatTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.CandidateEquivalentGovernanceTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_finite_labels_do_not_change_stored_fingerprint_contract(self):
        _, original, _, _ = self.f.candidate()
        for prefix in ("结论：", "结论： ", "Conclusion:", "Conclusion: "):
            with self.subTest(prefix=prefix):
                labelled = dict(original, content=prefix + original["content"])
                self.assertNotEqual(knowledge_fingerprint(original), knowledge_fingerprint(labelled))
                self.assertTrue(same_presentation_claim(original, labelled))
                self.assertTrue(same_presentation_claim(labelled, original))
                self.assertLessEqual(len(format_variants(labelled)), 5)
        for text in ("结论：", "结论：结论：Use local SQLite", "结论：\nUse local SQLite", "# Conclusion\nUse local SQLite",
                     "结论：  Use local SQLite", "```\nUse local SQLite\n```", " Use local SQLite"):
            with self.subTest(text=text):
                self.assertEqual(len(format_variants(dict(original, content=text))), 1)

    def test_no_business_field_or_factual_character_is_normalized(self):
        _, original, _, _ = self.f.candidate()
        changes = {"title": "Project storage ", "category": "operation", "knowledge_kind": "solution",
                   "applicability": "Another project", "limitations": "No limitations",
                   "validation_level": "tool_success", "metadata": {"version": "v2"}}
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = dict(original, **{field: value}, content="结论：" + original["content"])
                self.assertFalse(same_presentation_claim(original, changed))
        for text in ("Do not use local SQLite", "Use local SQLite v2", "Use local SQLite.", "Use local sqlite"):
            self.assertFalse(same_presentation_claim(original, dict(original, content="结论：" + text)))

    def test_length_limit_uses_the_same_payload_in_both_directions(self):
        _, original, _, _ = self.f.candidate()
        for size in (19988, 19998, 19999, 20000, 20001, 20012):
            plain = dict(original, content="x" * size)
            for prefix in ("结论：", "结论： ", "Conclusion:", "Conclusion: "):
                labelled = dict(plain, content=prefix + plain["content"])
                with self.subTest(size=size, prefix=prefix):
                    expected = size <= 20000
                    self.assertEqual(same_presentation_claim(plain, labelled), expected)
                    self.assertEqual(same_presentation_claim(labelled, plain), expected)
                    if expected:
                        self.assertEqual(presentation_fingerprints(plain), presentation_fingerprints(labelled))
                    else:
                        self.assertEqual(format_variants(plain), [plain])
                        self.assertEqual(format_variants(labelled), [labelled])

    def test_format_source_merges_without_body_rewrite_and_replays_idempotently(self):
        f = self.f
        first, body, _, _ = f.candidate()
        old = f.r.publish(f.p, first)
        before = f.r.db.execute("SELECT body FROM knowledge WHERE id=?", (old["knowledge_id"],)).fetchone()[0]
        candidate, _, problem, _ = f.candidate(content="结论：" + body["content"])
        self.assertIsNone(f.gate(json.loads(f.r.db.execute("SELECT body FROM candidates WHERE id=?", (candidate,)).fetchone()[0]), problem))
        result = f.r.publish(f.p, candidate)
        self.assertEqual(result["knowledge_id"], old["knowledge_id"])
        self.assertTrue(result["format_equivalent"])
        self.assertEqual(result["revision"], 1)
        self.assertEqual(f.r.publish(f.p, candidate), result)
        self.assertEqual(f.r.db.execute("SELECT body FROM knowledge WHERE id=?", (old["knowledge_id"],)).fetchone()[0], before)
        self.assertEqual(f.r.db.execute("SELECT COUNT(*) FROM knowledge_sources").fetchone()[0], 2)
        self.assertEqual(f.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 1)
        stored = json.loads(f.r.db.execute("SELECT body FROM candidates WHERE id=?", (candidate,)).fetchone()[0])
        self.assertEqual(stored["content"], "结论：" + body["content"])
        self.assertEqual(f.r.doctor()["status"], "ok")

    def test_formatting_cannot_resurrect_revoked_or_superseded_claim(self):
        f = self.f
        first, body, _, _ = f.candidate()
        old = f.r.publish(f.p, first)
        f.r.revoke(f.p, old["knowledge_id"], expected_revision=1)
        candidate, changed, problem, _ = f.candidate(content="结论：" + body["content"])
        self.assertIsNotNone(f.gate(changed, problem))
        self.assertFalse(prepare_review(f.r, f.p, [candidate], "approve")["ready"])
        with self.assertRaisesRegex(ContractError, "revoked|restore"):
            f.r.publish(f.p, candidate)
        f.r.restore(f.p, old["knowledge_id"], 1)
        update, *_ = f.candidate(content="Use local SQLite v2")
        f.r.publish(f.p, update, knowledge_id=old["knowledge_id"], expected_revision=1)
        with self.assertRaisesRegex(ContractError, "superseded|current revision"):
            f.r.publish(f.p, candidate)

    def test_formatting_cannot_bypass_counterexamples_or_candidate_holds(self):
        f = self.f
        first, body, _, _ = f.candidate()
        old = f.r.publish(f.p, first)
        hold, *_ = f.candidate(content="Conclusion: " + body["content"])
        f.r.db.execute("UPDATE candidates SET status='deferred' WHERE id=?", (hold,))
        candidate, changed, problem, _ = f.candidate(content="结论：" + body["content"])
        self.assertEqual(references.held_equivalent(f.r, f.p, changed), "equivalent candidate is held")
        self.assertIsNotNone(f.gate(changed, problem))
        with self.assertRaisesRegex(ContractError, "held"):
            f.r.publish(f.p, candidate)
        f.r.db.execute("UPDATE candidates SET status='candidate' WHERE id=?", (hold,))
        f.concern(hold)
        with self.assertRaisesRegex(ContractError, "concerns|disput"):
            f.r.publish(f.p, candidate)
        f.r.db.execute("DELETE FROM candidate_reference_feedback")
        f.r.task_update(f.p, "counter", "Compare current claim")
        f.r.feedback(f.p, "counter", "A counterexample", category="project", knowledge_id=old["knowledge_id"],
                     knowledge_revision=1, feedback_kind="counterexample")
        self.assertIsNotNone(f.gate(changed, problem))
        with self.assertRaisesRegex(ContractError, "counterexample|disputed"):
            f.r.publish(f.p, candidate)

    def test_explicit_manual_approval_and_attachment_keep_original_revision(self):
        f = self.f
        first, body, _, _ = f.candidate()
        old = f.r.publish(f.p, first)
        candidate, _, problem, _ = f.candidate(content="结论：" + body["content"])
        plan = prepare_review(f.r, f.p, [candidate], "approve")
        self.assertTrue(plan["ready"], plan)
        result = commit_review(f.r, f.p, plan, "approve-format")
        self.assertTrue(result["results"][0]["deduplicated"])
        attach, _, problem, _ = f.candidate(content="Conclusion: " + body["content"])
        proposal = propose_governance(f.r, f.p, problem["id"], problem["revision"], "attach", candidate_id=attach,
            knowledge_id=old["knowledge_id"], expected_revision=1, reason="Independent equivalent source")
        attached = execute_governance(f.r, f.p, proposal["proposal_id"])["publication"]
        self.assertEqual(attached["knowledge_id"], old["knowledge_id"])
        self.assertEqual(attached["revision"], 1)

    def test_unpublished_reference_cannot_bypass_a_formatted_hold(self):
        f = self.f
        held, body, _, _ = f.candidate()
        candidate, *_ = f.candidate(content="结论：" + body["content"])
        references.configure(f.r, f.p, True, 0)
        f.r.db.execute("UPDATE candidates SET status='deferred' WHERE id=?", (held,))
        assessed = references.assess(f.r, f.p, candidate)
        self.assertFalse(assessed["eligible"])
        self.assertEqual(assessed["reason"], "equivalent_candidate_held")
        f.r.db.execute("UPDATE candidates SET status='candidate' WHERE id=?", (held,))
        f.concern(held)
        assessed = references.assess(f.r, f.p, candidate)
        self.assertFalse(assessed["eligible"])
        self.assertEqual(assessed["reason"], "equivalent_candidate_disputed")

    def test_explicit_approval_of_deferred_format_matches_exact_approval_behavior(self):
        f = self.f
        first, body, _, _ = f.candidate()
        f.r.publish(f.p, first)
        candidate, *_ = f.candidate(content="结论：" + body["content"])
        f.r.db.execute("UPDATE candidates SET status='deferred' WHERE id=?", (candidate,))
        plan = prepare_review(f.r, f.p, [candidate], "approve")
        self.assertTrue(plan["ready"], plan)
        result = commit_review(f.r, f.p, plan, "approve-deferred-format")
        self.assertTrue(result["results"][0]["deduplicated"])

    def test_restore_can_use_a_current_healthy_formatted_source(self):
        f = self.f
        first, body, problem, _ = f.candidate()
        old = f.r.publish(f.p, first)
        formatted, *_ = f.candidate(content="结论：" + body["content"])
        f.r.publish(f.p, formatted)
        f.r.revoke(f.p, old["knowledge_id"], expected_revision=1)
        f.r.feedback(f.p, problem["conversation"], "Original case changed", category="project")
        result = f.r.restore(f.p, old["knowledge_id"], 1)
        self.assertEqual(result["status"], "restored")


class FormatMaturityTests(unittest.TestCase):
    def test_independent_formatted_tool_sources_keep_maturity_evidence(self):
        f = release_fixtures.Module7ReleasesTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        first = f.source("first", content="Adjust compiler include path")
        second = f.source("second", content="结论：Adjust compiler include path")
        self.assertEqual(first["knowledge_id"], second["knowledge_id"])
        maturity = f.maturity(first)
        self.assertTrue(maturity["eligible"], maturity)
        self.assertEqual(maturity["independent_validation_count"], 2)
        self.assertNotIn("supporting_source_contract_invalid", maturity["reasons"])
        self.assertEqual(f.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
