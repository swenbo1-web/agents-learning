"""知识治理真实 SQLite 回归；提议与受信执行分离，不调用模型。"""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent_learning.admin_governance import execute_governance, install_governance, propose_governance
from agent_learning.review_packet import review_instruction
from agent_learning.runtime import ContractError, Runtime


class Module6GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-module6-governance-test-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.r = Runtime(Path(self.temp.name) / "state")
        self.addCleanup(self.r.close)
        install_governance(self.r)
        self.p = self.r.project(self.workspace)
        self.task = self.r.task_update(self.p, "chat", "Review runtime observations")

    def candidate(self, problem, title="Compiler include remedy", content="Adjust CXX include directory"):
        refs = [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        return self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title=title, content=content, category="operation", applicability="Ubuntu gcc",
            limitations="Recorded observation only", evidence_refs=refs, validation_level="reported")

    def knowledge(self, content="Adjust CXX include directory"):
        source = self.r.feedback(self.p, "chat", "Original source", category="operation")
        return self.r.publish(self.p, self.candidate(source, content=content)["candidate_id"])

    def counterexample(self, knowledge, summary="Regression reproduced elsewhere", refs=None):
        return self.r.feedback(self.p, "chat", summary, category="operation", event_refs=refs,
                               knowledge_id=knowledge["knowledge_id"], knowledge_revision=knowledge["revision"],
                               feedback_kind="counterexample")

    def refs(self, problem):
        return [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]

    def propose(self, problem, action, knowledge=None, candidate=None, **kwargs):
        return propose_governance(self.r, self.p, problem["id"], problem["revision"], action,
            candidate_id=candidate["candidate_id"] if candidate else None,
            knowledge_id=knowledge["knowledge_id"] if knowledge else None,
            expected_revision=knowledge["revision"] if knowledge else None,
            reason="Only the referenced actual observations", evidence_refs=self.refs(problem), **kwargs)

    def test_proposal_alone_does_not_publish_and_execution_replays_receipt(self):
        problem = self.r.feedback(self.p, "chat", "New fact", category="operation")
        candidate = self.candidate(problem)
        proposal = self.propose(problem, "publish", candidate=candidate)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
        self.assertEqual(proposal, self.propose(problem, "publish", candidate=candidate))
        first = execute_governance(self.r, self.p, proposal["proposal_id"])
        self.assertEqual(first, execute_governance(self.r, self.p, proposal["proposal_id"]))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM publication_receipts").fetchone()[0], 1)

    def test_publishing_new_knowledge_does_not_clear_old_counterexample(self):
        knowledge = self.knowledge()
        problem = self.counterexample(knowledge)
        candidate = self.candidate(problem, content="Use a different directory")
        result = execute_governance(self.r, self.p, self.propose(problem, "publish", candidate=candidate)["proposal_id"])
        self.assertNotEqual(result["publication"]["knowledge_id"], knowledge["knowledge_id"])
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["pending_counterexample_count"], 1)
        self.assertEqual(self.r.db.execute("SELECT status FROM problems WHERE id=?", (problem["id"],)).fetchone()[0], "reviewed")

    def test_reviewed_legacy_counterexample_migrates_as_unresolved(self):
        knowledge = self.knowledge()
        problem = self.counterexample(knowledge)
        with self.r.transaction():
            self.r.db.execute("UPDATE problems SET status='reviewed' WHERE id=?", (problem["id"],))
            self.r.db.execute("DROP TRIGGER governance_feedback_disposition")
            for column in ("disposition", "disposition_reason", "disposition_receipt"):
                self.r.db.execute("ALTER TABLE knowledge_feedback DROP COLUMN " + column)
        before = self.r.db.execute("SELECT version FROM knowledge_source_versions WHERE project=? AND knowledge_id=? AND revision=?",
                                   (self.p, knowledge["knowledge_id"], knowledge["revision"])).fetchone()[0]
        install_governance(self.r)
        state = self.r.read(self.p, knowledge["knowledge_id"])
        self.assertEqual(state["pending_counterexample_count"], 1)
        self.assertGreater(state["source_version"], before)
        version = state["source_version"]
        install_governance(self.r)
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["source_version"], version)

    def test_review_status_alone_cannot_clear_warning(self):
        knowledge = self.knowledge()
        problem = self.counterexample(knowledge)
        with self.r.transaction():
            self.r.db.execute("UPDATE problems SET status='reviewed' WHERE id=?", (problem["id"],))
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["pending_counterexample_count"], 1)

    def test_target_is_present_without_keyword_overlap(self):
        knowledge = self.knowledge()
        problem = self.counterexample(knowledge)
        packet = self.r.review_packet(self.p, problem["id"])
        self.assertEqual(packet["target_knowledge"]["knowledge_id"], knowledge["knowledge_id"])
        self.assertEqual(packet["target_knowledge"]["content"], "Adjust CXX include directory")
        self.assertFalse(packet["incomplete"])

    def test_target_uses_exact_historical_revision(self):
        knowledge = self.knowledge()
        problem = self.counterexample(knowledge)
        changed = self.r.feedback(self.p, "chat", "Changed source", category="operation")
        self.r.publish(self.p, self.candidate(changed, content="New directory")["candidate_id"],
                       knowledge_id=knowledge["knowledge_id"], expected_revision=1)
        target = self.r.review_packet(self.p, problem["id"])["target_knowledge"]
        self.assertEqual(target["revision"], 1)
        self.assertEqual(target["active_revision"], 2)
        self.assertEqual(target["content"], "Adjust CXX include directory")

    def test_revoked_target_remains_available_in_packet(self):
        knowledge = self.knowledge()
        problem = self.counterexample(knowledge)
        self.r.revoke(self.p, knowledge["knowledge_id"])
        target = self.r.review_packet(self.p, problem["id"])["target_knowledge"]
        self.assertIsNone(target["active_revision"])
        self.assertEqual(target["content"], "Adjust CXX include directory")

    def test_large_target_is_explicitly_omitted_and_pageable(self):
        content = "reference " * 700
        knowledge = self.knowledge(content)
        problem = self.counterexample(knowledge)
        packet = self.r.review_packet(self.p, problem["id"], max_chars=2400)
        self.assertTrue(packet["incomplete"])
        self.assertIsNone(packet["target_knowledge"])
        self.assertIn("target_knowledge_budget", packet["omission_reasons"])
        self.assertEqual(len(packet["events"]), 1)
        self.assertLessEqual(len(review_instruction(packet)), 2400)
        pieces, offset, fingerprint = [], 0, None
        while True:
            page = self.r.review_packet(self.p, problem["id"], offset=offset,
                expected_revision=problem["revision"], max_chars=2400, section="target")
            self.assertLessEqual(len(review_instruction(page)), 2400)
            self.assertTrue(page["incomplete"])
            fingerprint = fingerprint or page["target_page"]["digest"]
            self.assertEqual(fingerprint, page["target_page"]["digest"])
            pieces.append(page["target_page"]["content"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual(json.loads("".join(pieces))["content"], content)

    def test_target_pagination_rejects_changed_case_revision(self):
        knowledge = self.knowledge()
        problem = self.counterexample(knowledge)
        self.counterexample(knowledge)
        with self.assertRaisesRegex(ContractError, "problem changed"):
            self.r.review_packet(self.p, problem["id"], offset=1, expected_revision=problem["revision"], section="target")

    def test_update_disposes_only_matching_revision_and_keeps_id(self):
        knowledge = self.knowledge()
        problem = self.counterexample(knowledge)
        candidate = self.candidate(problem, content="Corrected directory")
        proposal = self.propose(problem, "update", knowledge, candidate)
        result = execute_governance(self.r, self.p, proposal["proposal_id"])
        self.assertEqual(result["publication"]["knowledge_id"], knowledge["knowledge_id"])
        self.assertEqual(result["publication"]["revision"], 2)
        self.assertEqual(result["disposition"]["state"], "revised")
        self.assertEqual(self.r._knowledge_feedback_state(self.p, knowledge["knowledge_id"], 1)["pending_counterexample_count"], 0)
        self.assertEqual(result, execute_governance(self.r, self.p, proposal["proposal_id"]))

    def test_revoke_is_explicit_and_replays_after_target_is_inactive(self):
        knowledge = self.knowledge()
        problem = self.counterexample(knowledge)
        proposal = self.propose(problem, "revoke", knowledge)
        self.assertIsNotNone(self.r.read(self.p, knowledge["knowledge_id"]))
        result = execute_governance(self.r, self.p, proposal["proposal_id"])
        self.assertEqual(result["disposition"]["state"], "revoked")
        self.assertEqual(result, execute_governance(self.r, self.p, proposal["proposal_id"]))
        with self.assertRaises(ContractError):
            self.r.read(self.p, knowledge["knowledge_id"])

    def test_wrong_target_is_rejected(self):
        knowledge = self.knowledge()
        other = self.knowledge("A different reference")
        problem = self.counterexample(knowledge)
        with self.assertRaisesRegex(ContractError, "differs"):
            self.propose(problem, "revoke", other)

    def test_active_revision_cas_and_problem_revision_cas(self):
        knowledge = self.knowledge()
        problem = self.counterexample(knowledge)
        proposal = self.propose(problem, "revoke", knowledge)
        new_source = self.r.feedback(self.p, "chat", "Different source", category="operation")
        self.r.publish(self.p, self.candidate(new_source, content="New content")["candidate_id"],
                       knowledge_id=knowledge["knowledge_id"], expected_revision=1)
        with self.assertRaisesRegex(ContractError, "active revision"):
            execute_governance(self.r, self.p, proposal["proposal_id"])
        self.counterexample(knowledge)
        with self.assertRaisesRegex(ContractError, "problem changed"):
            execute_governance(self.r, self.p, proposal["proposal_id"])
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["revision"], 2)

    def test_deleted_evidence_rejects_execution(self):
        knowledge = self.knowledge()
        problem = self.counterexample(knowledge)
        proposal = self.propose(problem, "revoke", knowledge)
        self.r.db.execute("DELETE FROM events WHERE id=?", (self.refs(problem)[0],))
        with self.assertRaisesRegex(ContractError, "source is absent"):
            execute_governance(self.r, self.p, proposal["proposal_id"])

    def test_dismiss_requires_additional_real_evidence_and_dirties_source(self):
        knowledge = self.knowledge()
        problem = self.counterexample(knowledge)
        with self.assertRaisesRegex(ContractError, "additional supporting evidence"):
            self.propose(problem, "dismiss_counterexample", knowledge)
        event = self.r.record_event(self.p, "chat", "PostToolUse", {"tool_name": "Read", "tool_response": {"exit_code": 0}},
                                   task_id=self.task["id"], task_revision=self.task["revision"], outcome="succeeded")
        problem = self.r._attach_case_evidence(problem["id"], [(event["id"], "observation")])
        before = self.r.read(self.p, knowledge["knowledge_id"])["source_version"]
        proposal = self.propose(problem, "dismiss_counterexample", knowledge)
        result = execute_governance(self.r, self.p, proposal["proposal_id"])
        self.assertEqual(result["disposition"]["state"], "dismissed")
        state = self.r.read(self.p, knowledge["knowledge_id"])
        self.assertEqual(state["pending_counterexample_count"], 0)
        self.assertGreater(state["source_version"], before)
        self.assertEqual(result, execute_governance(self.r, self.p, proposal["proposal_id"]))
        reopened = self.counterexample(knowledge)
        self.assertEqual(reopened["id"], problem["id"])
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["pending_counterexample_count"], 1)

    def test_attach_requires_equivalence_and_preserves_warning(self):
        knowledge = self.knowledge()
        problem = self.counterexample(knowledge)
        candidate = self.candidate(problem)
        result = execute_governance(self.r, self.p, self.propose(problem, "attach", knowledge, candidate)["proposal_id"])
        self.assertEqual(result["publication"]["revision"], 1)
        self.assertTrue(result["publication"]["deduplicated"])
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["pending_counterexample_count"], 1)
        new_problem = self.counterexample(knowledge, summary="Another counterexample")
        changed = self.candidate(new_problem, content="Different content")
        proposal = self.propose(new_problem, "attach", knowledge, changed)
        with self.assertRaisesRegex(ContractError, "equivalent"):
            execute_governance(self.r, self.p, proposal["proposal_id"])

    def test_unchanged_update_cannot_clear_counterexample(self):
        knowledge = self.knowledge()
        problem = self.counterexample(knowledge)
        candidate = self.candidate(problem)
        proposal = self.propose(problem, "update", knowledge, candidate)
        with self.assertRaisesRegex(ContractError, "changed knowledge"):
            execute_governance(self.r, self.p, proposal["proposal_id"])
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["revision"], 1)
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["pending_counterexample_count"], 1)
        self.assertEqual(self.r.db.execute("SELECT status FROM candidates WHERE id=?", (candidate["candidate_id"],)).fetchone()[0], "candidate")

    def test_previously_published_candidate_cannot_change_publication_target(self):
        knowledge = self.knowledge()
        problem = self.counterexample(knowledge)
        candidate = self.candidate(problem, content="A revised directory")
        different = self.r.publish(self.p, candidate["candidate_id"])
        proposal = self.propose(problem, "update", knowledge, candidate)
        with self.assertRaisesRegex(ContractError, "another request"):
            execute_governance(self.r, self.p, proposal["proposal_id"])
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["revision"], 1)
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["pending_counterexample_count"], 1)
        self.assertIsNotNone(self.r.read(self.p, different["knowledge_id"]))

    def test_receipt_failure_rolls_back_publication_and_case_state(self):
        problem = self.r.feedback(self.p, "chat", "New fact", category="operation")
        candidate = self.candidate(problem)
        proposal = self.propose(problem, "publish", candidate=candidate)
        self.r.db.execute("CREATE TRIGGER refuse_receipt BEFORE UPDATE OF state ON governance_proposals "
                          "WHEN NEW.state='executed' BEGIN SELECT RAISE(ABORT,'receipt storage failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            execute_governance(self.r, self.p, proposal["proposal_id"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM publication_receipts").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT status FROM problems WHERE id=?", (problem["id"],)).fetchone()[0], "pending")
        self.assertEqual(self.r.db.execute("SELECT state FROM governance_proposals").fetchone()[0], "proposed")

    def test_host_epoch_blocks_old_execution(self):
        knowledge = self.knowledge()
        problem = self.counterexample(knowledge)
        proposal = self.propose(problem, "revoke", knowledge)
        old_epoch = self.r.host_epoch()
        self.r.account_changed()
        with self.assertRaises(ContractError):
            execute_governance(self.r, self.p, proposal["proposal_id"], expected_epoch=old_epoch)
        self.assertIsNotNone(self.r.read(self.p, knowledge["knowledge_id"]))
