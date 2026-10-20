"""Exact duplicate provenance must not exhaust a title-history sample.

These checks use isolated local databases, original public sources and the same
automatic/manual governance gates. They never call a model or installed host.
"""
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_learning import candidate_references as references
from agent_learning.admin_publication import publication_gate
from agent_learning.candidate_review import commit_review, prepare_review
from agent_learning.runtime import ContractError, Runtime, encode, knowledge_fingerprint


class CandidateEquivalentGovernanceTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "build" / "local" / "fix144-dedup"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="case-", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.counter = 0

    def candidate(self, **changes):
        self.counter += 1
        chat = f"source-{self.counter}"
        task = self.r.task_update(self.p, chat, "Record a sourced project fact")
        event, _ = self.r._event(self.p, chat, "Stop", {"last_assistant_message": "Project storage is SQLite"},
            task_id=task["id"], task_revision=task["revision"], outcome="reported")
        problem = self.r._problem(self.p, chat, "Independent source " + str(self.counter), "project",
                                  [event["id"]], task_binding=task)
        body = dict(problem_id=problem["id"], problem_revision=problem["revision"], title="Project storage",
            content="Use local SQLite", category="project", knowledge_kind="fact", metadata={},
            applicability="This project", limitations="Reported observation; compare the current implementation",
            validation_level="reported", evidence_refs=[event["id"]])
        body.update(changes)
        identity = self.r.submit_candidate(self.p, **body)["candidate_id"]
        return identity, body, problem, event

    def flood(self, count=40, **changes):
        return [self.candidate(**changes) for _ in range(count)]

    def gate(self, body, problem):
        return publication_gate(self.r, self.p, self.r.review_packet(self.p, problem["id"]), body,
                                {"action": "publish", "knowledge_id": None}, mode="tiered")

    def concern(self, identity):
        self.r.db.execute("INSERT INTO candidate_reference_feedback VALUES(?,?,?,?,?,?,?)",
            (self.p, identity, "a" * 64, "operator-concern", "unresolved", "Compare this claim", 1.0))

    def test_forty_independent_sources_merge_to_one_formal_revision(self):
        results = []
        with patch("subprocess.Popen") as model:
            for _ in range(40):
                identity, body, problem, _ = self.candidate()
                self.assertIsNone(self.gate(body, problem))
                results.append(self.r.publish(self.p, identity))
            model.assert_not_called()
        self.assertEqual(len({row["knowledge_id"] for row in results}), 1)
        self.assertEqual({row["revision"] for row in results}, {1})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM knowledge_sources").fetchone()[0], 40)
        self.assertTrue(all(row.get("deduplicated") for row in results[1:]))

    def test_manual_approval_beyond_thirty_two_sources_keeps_duplicate_provenance(self):
        for identity, *_ in self.flood(34):
            self.r.publish(self.p, identity)
        identity, _, _, _ = self.candidate()
        plan = prepare_review(self.r, self.p, [identity], "approve")
        self.assertTrue(plan["ready"], plan)
        answer = commit_review(self.r, self.p, plan, "approve-thirty-fifth")
        self.assertTrue(answer["results"][0]["deduplicated"], answer)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM knowledge_sources").fetchone()[0], 35)

    def test_oldest_reject_and_defer_remain_visible_after_healthy_source_history(self):
        identity, body, _, _ = self.candidate()
        self.flood()
        for status in ("rejected", "deferred"):
            with self.subTest(status=status):
                self.r.db.execute("UPDATE candidates SET status=? WHERE id=?", (status, identity))
                self.assertEqual(references.held_equivalent(self.r, self.p, body), "equivalent candidate is held")
                # An explicit manual hold override remains its own contract.
                self.assertIsNone(references.held_equivalent(self.r, self.p, body, include_held=False))
        new_id, new_body, problem, _ = self.candidate()
        self.assertEqual(self.gate(new_body, problem), "equivalent candidate is held")

    def test_oldest_unresolved_concern_blocks_auto_manual_and_explicit_update(self):
        identity, body, _, _ = self.candidate()
        published = self.r.publish(self.p, identity)
        self.flood()
        self.concern(identity)
        new_id, new_body, problem, _ = self.candidate()
        self.assertEqual(references.held_equivalent(self.r, self.p, new_body, include_held=False),
                         "equivalent candidate has unresolved concerns")
        self.assertEqual(self.gate(new_body, problem), "equivalent candidate has unresolved concerns")
        self.assertFalse(prepare_review(self.r, self.p, [new_id], "approve")["ready"])
        for target in ({}, {"knowledge_id": published["knowledge_id"], "expected_revision": 1}):
            with self.subTest(target=target), self.assertRaisesRegex(ContractError, "unresolved concerns"):
                self.r.publish(self.p, new_id, **target)
        self.r.db.execute("UPDATE candidate_reference_feedback SET state='resolved'")
        self.assertIsNone(references.held_equivalent(self.r, self.p, new_body))

    def test_full_business_fields_match_fingerprint_not_title_alone(self):
        identity, body, _, _ = self.candidate(metadata={"tool": "SQLite", "environment": "Windows"})
        self.r.db.execute("UPDATE candidates SET status='rejected' WHERE id=?", (identity,))
        alternatives = {"title": "project storage", "content": "Use PostgreSQL", "category": "professional",
            "knowledge_kind": "solution", "applicability": "Another project", "limitations": "Already verified",
            "validation_level": "tool_success", "metadata": {"tool": "SQLite", "environment": "Linux"}}
        for field, changed in alternatives.items():
            with self.subTest(field=field):
                probe = dict(body, **{field: changed})
                self.assertNotEqual(knowledge_fingerprint(body), knowledge_fingerprint(probe))
                self.assertIsNone(references.held_equivalent(self.r, self.p, probe))
        reordered = dict(body, metadata={"environment": "Windows", "tool": "SQLite"},
                         evidence_refs=["different-source"], problem_id="different-problem")
        self.assertEqual(knowledge_fingerprint(body), knowledge_fingerprint(reordered))
        self.assertEqual(references.held_equivalent(self.r, self.p, reordered), "equivalent candidate is held")

    def test_legacy_missing_defaults_and_escaped_text_keep_exact_comparison(self):
        identity, body, _, _ = self.candidate(title='Storage "quoted" \\ path', content="保存 SQLite\n多行记录",
                                             knowledge_kind="solution")
        stored = json.loads(self.r.db.execute("SELECT body FROM candidates WHERE id=?", (identity,)).fetchone()[0])
        stored.pop("metadata")
        stored.pop("knowledge_kind")
        self.r.db.execute("UPDATE candidates SET status='deferred',body=? WHERE id=?", (encode(stored), identity))
        self.assertEqual(knowledge_fingerprint(stored), knowledge_fingerprint(body))
        self.assertEqual(references.held_equivalent(self.r, self.p, body), "equivalent candidate is held")
        self.assertEqual(references.held_equivalent(self.r, self.p, stored), "equivalent candidate is held")

    def test_old_unrelated_held_claims_do_not_poison_an_exact_healthy_merge(self):
        for index in range(40):
            identity, *_ = self.candidate(content="One different rejected convention")
            self.r.db.execute("UPDATE candidates SET status='rejected' WHERE id=?", (identity,))
            self.concern(identity)
        _, body, _, _ = self.candidate()
        self.assertIsNone(references.held_equivalent(self.r, self.p, body))

    def test_different_claim_limit_counts_business_variants_not_source_count(self):
        self.flood()
        for number in range(31):
            self.candidate(content=f"Distinct project convention {number}")
        identity, body, _, _ = self.candidate()
        # 72 original sources, but only 32 distinct business claims.
        self.assertIsNone(references.held_equivalent(self.r, self.p, body))
        self.candidate(content="The thirty-third distinct project convention")
        self.assertEqual(references.held_equivalent(self.r, self.p, body), "candidate comparison requires review")
        plan = prepare_review(self.r, self.p, [identity], "approve")
        self.assertFalse(plan["ready"])
        self.assertIn("candidate_comparison_limit", {issue["code"] for issue in plan["items"][0]["preflight"]["issues"]})
        with self.assertRaisesRegex(ContractError, "comparison requires review"):
            self.r.publish(self.p, identity)

    def test_same_title_different_formal_claim_still_requires_comparison(self):
        identity, *_ = self.candidate()
        self.r.publish(self.p, identity)
        self.flood()
        _, body, problem, _ = self.candidate(content="Use PostgreSQL instead of SQLite")
        self.assertIsNone(references.held_equivalent(self.r, self.p, body))
        self.assertEqual(self.gate(body, problem), "conflicting knowledge requires review")

    def test_new_sources_upstream_risk_cannot_hide_in_large_title_history(self):
        upstream, *_ = self.candidate(title="Original source convention", content="A disputed source")
        self.r.db.execute("UPDATE candidates SET status='rejected' WHERE id=?", (upstream,))
        self.flood()
        identity, body, problem, event = self.candidate()
        references.record_lineage(self.r, self.p, event["id"],
            [{"candidate_id": upstream, "reference_version": "a" * 64}], "explicit_derivation")
        self.assertEqual(self.gate(body, problem), "candidate reference source requires review")
        self.assertFalse(prepare_review(self.r, self.p, [identity], "approve")["ready"])
        with self.assertRaisesRegex(ContractError, "source lineage"):
            self.r.publish(self.p, identity)

    def test_read_only_model_and_active_cursor_need_no_function_registration(self):
        identity, body, _, _ = self.candidate()
        self.r.db.execute("UPDATE candidates SET status='deferred' WHERE id=?", (identity,))
        self.flood()
        db = sqlite3.connect((self.r.home / "runtime.sqlite3").as_uri() + "?mode=ro", uri=True)
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        view = SimpleNamespace(db=db)
        before = db.total_changes
        cursor = db.execute("SELECT id FROM candidates WHERE project=?", (self.p,))
        for _ in range(3):
            self.assertIsNotNone(cursor.fetchone())
            self.assertEqual(references.held_equivalent(view, self.p, body), "equivalent candidate is held")
        self.assertEqual(db.total_changes, before)

    def test_other_project_held_equivalent_does_not_block(self):
        identity, body, _, _ = self.candidate()
        self.r.db.execute("UPDATE candidates SET status='rejected' WHERE id=?", (identity,))
        self.concern(identity)
        other = self.r.project(self.root / "other-project")
        self.assertIsNone(references.held_equivalent(self.r, other, body))


if __name__ == "__main__":
    unittest.main()
