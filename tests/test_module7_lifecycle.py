"""生命周期真实 SQLite 回归；不连接宿主、模型或远端服务。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin_governance import execute_governance, propose_governance
from agent_learning.knowledge_lifecycle import install_lifecycle, lifecycle_state, record_action
from agent_learning.runtime import ContractError, Runtime


class Module7LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-module7-lifecycle-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(Path(self.temp.name) / "state")
        self.addCleanup(self.r.close)
        install_lifecycle(self.r)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "chat", "Validate local knowledge lifecycle")

    def source(self, summary="Source observation", content="Use the inspected configuration", refs=None, level="reported"):
        problem = self.r.feedback(self.p, "chat", summary, category="operation", event_refs=refs)
        evidence = [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="Observed configuration", content=content, category="operation", applicability="Inspected project",
            limitations="Verify equivalent environment", evidence_refs=evidence, validation_level=level)
        return problem, candidate["candidate_id"]

    def published(self, **kwargs):
        _, candidate = self.source(**kwargs)
        return self.r.publish(self.p, candidate), candidate

    def counterexample(self, knowledge, summary="Counterexample reproduced"):
        return self.r.feedback(self.p, "chat", summary, category="operation", knowledge_id=knowledge["knowledge_id"],
                               knowledge_revision=knowledge["revision"], feedback_kind="counterexample")

    def refs(self, problem):
        return [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]

    def revoke_counterexample(self, knowledge):
        problem = self.counterexample(knowledge)
        proposal = propose_governance(self.r, self.p, problem["id"], problem["revision"], "revoke",
            knowledge_id=knowledge["knowledge_id"], expected_revision=knowledge["revision"],
            reason="Observed regression requires removal", evidence_refs=self.refs(problem))
        execute_governance(self.r, self.p, proposal["proposal_id"])
        return problem

    def support(self):
        problem = self.r.feedback(self.p, "chat", "Independent fresh inspection supports reinstatement", category="operation")
        return self.refs(problem)

    def action_count(self):
        return self.r.db.execute("SELECT COUNT(*) FROM knowledge_actions WHERE project=?", (self.p,)).fetchone()[0]

    def test_publication_actions_and_historical_replay(self):
        knowledge, candidate = self.published()
        self.assertEqual(self.action_count(), 1)
        self.assertEqual(lifecycle_state(self.r, self.p, knowledge["knowledge_id"])["lifecycle_version"], 1)
        self.assertEqual(self.r.publish(self.p, candidate), knowledge)
        self.assertEqual(self.action_count(), 1)
        row = self.r.db.execute("SELECT * FROM knowledge_actions WHERE request_id=?", ("publication:" + candidate,)).fetchone()
        self.assertEqual(row["action"], "publish")
        self.assertIsNone(json.loads(row["before_state"])["active_revision"])
        self.assertEqual(json.loads(row["after_state"])["active_revision"], 1)

    def test_attach_and_update_each_advance_lifecycle_without_false_revision(self):
        knowledge, _ = self.published()
        _, candidate = self.source(summary="Second independent source")
        attached = self.r.publish(self.p, candidate)
        self.assertEqual(attached["revision"], 1)
        _, changed = self.source(summary="Revised source", content="Use the corrected configuration")
        self.r.publish(self.p, changed, knowledge_id=knowledge["knowledge_id"], expected_revision=1)
        self.assertEqual(lifecycle_state(self.r, self.p, knowledge["knowledge_id"])["lifecycle_version"], 3)
        self.assertEqual([row[0] for row in self.r.db.execute("SELECT action FROM knowledge_actions ORDER BY created")],
                         ["publish", "attach", "update"])

    def test_revoke_requires_positive_exact_revision(self):
        knowledge, _ = self.published()
        for revision in (None, True, 0, -1, "1", 2):
            with self.subTest(revision=revision), self.assertRaises(ContractError):
                self.r.revoke(self.p, knowledge["knowledge_id"], revision)
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["revision"], 1)

    def test_old_revision_cannot_revoke_new_knowledge(self):
        knowledge, _ = self.published()
        _, changed = self.source(summary="New revision", content="Corrected configuration")
        self.r.publish(self.p, changed, knowledge_id=knowledge["knowledge_id"], expected_revision=1)
        with self.assertRaises(ContractError):
            self.r.revoke(self.p, knowledge["knowledge_id"], 1)
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["revision"], 2)

    def test_revoke_receipt_replays_after_restore_without_revoking_again(self):
        knowledge, _ = self.published()
        response = self.r.revoke(self.p, knowledge["knowledge_id"], 1, request_id="remove-a", reason="Operator inspection")
        self.r.restore(self.p, knowledge["knowledge_id"], 1)
        before = lifecycle_state(self.r, self.p, knowledge["knowledge_id"])
        self.assertEqual(self.r.revoke(self.p, knowledge["knowledge_id"], 1, request_id="remove-a", reason="Operator inspection"), response)
        self.assertEqual(lifecycle_state(self.r, self.p, knowledge["knowledge_id"]), before)
        self.assertEqual(before["active_revision"], 1)

    def test_revoke_request_identity_rejects_changed_input(self):
        knowledge, _ = self.published()
        self.r.revoke(self.p, knowledge["knowledge_id"], 1, request_id="same")
        for changes in ({"reason": "Different reason"}, {"actor": "Another operator"}, {"expected_generation": 123}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.r.revoke(self.p, knowledge["knowledge_id"], 1, request_id="same", **changes)

    def test_revoke_generation_guard_keeps_all_states(self):
        knowledge, _ = self.published()
        before = lifecycle_state(self.r, self.p, knowledge["knowledge_id"])
        with self.assertRaises(ContractError):
            self.r.revoke(self.p, knowledge["knowledge_id"], 1, expected_generation=-1)
        self.assertEqual(lifecycle_state(self.r, self.p, knowledge["knowledge_id"]), before)
        self.assertEqual(self.action_count(), 1)

    def test_revoke_unknown_id_and_cross_project_rejected(self):
        knowledge, _ = self.published()
        other_path = Path(self.temp.name) / "other"
        other_path.mkdir()
        other = self.r.project(other_path)
        with self.assertRaises(ContractError):
            self.r.revoke(other, knowledge["knowledge_id"], 1, request_id="remove")
        with self.assertRaises(ContractError):
            self.r.revoke(self.p, "unknown", 1)
        self.assertEqual(self.action_count(), 1)

    def test_host_epoch_checked_before_historical_action_replay(self):
        knowledge, _ = self.published()
        epoch = self.r.host_epoch()
        response = self.r.revoke(self.p, knowledge["knowledge_id"], 1, request_id="remove", expected_epoch=epoch)
        self.r.account_changed("next-account")
        with self.assertRaises(ContractError):
            self.r.revoke(self.p, knowledge["knowledge_id"], 1, request_id="remove", expected_epoch=epoch)
        self.assertEqual(self.r.revoke(self.p, knowledge["knowledge_id"], 1, request_id="remove"), response)

    def test_counterexample_restore_requires_specific_reason_and_fresh_evidence(self):
        knowledge, _ = self.published()
        problem = self.revoke_counterexample(knowledge)
        for arguments in ({}, {"reason": "Review completed"}, {"reason": "Review completed", "evidence_refs": self.refs(problem)}):
            with self.subTest(arguments=arguments), self.assertRaises(ContractError):
                self.r.restore(self.p, knowledge["knowledge_id"], 1, **arguments)
        self.assertIsNone(lifecycle_state(self.r, self.p, knowledge["knowledge_id"])["active_revision"])

    def test_counterexample_restore_reopens_warning_and_replays_receipt(self):
        knowledge, _ = self.published()
        self.revoke_counterexample(knowledge)
        refs = self.support()
        response = self.r.restore(self.p, knowledge["knowledge_id"], 1, request_id="reinstate", reason="Fresh inspection completed", evidence_refs=refs)
        self.assertEqual(response["reopened_counterexamples"], 1)
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["pending_counterexample_count"], 1)
        self.assertEqual(self.r.restore(self.p, knowledge["knowledge_id"], 1, request_id="reinstate", reason="Fresh inspection completed", evidence_refs=refs), response)
        self.assertEqual(self.action_count(), 3)

    def test_original_source_cannot_stand_in_for_new_restore_support(self):
        knowledge, candidate = self.published()
        self.revoke_counterexample(knowledge)
        old = json.loads(self.r.db.execute("SELECT body FROM candidates WHERE id=?", (candidate,)).fetchone()[0])["evidence_refs"]
        with self.assertRaises(ContractError):
            self.r.restore(self.p, knowledge["knowledge_id"], 1, reason="Old source rechecked", evidence_refs=old)

    def test_evidence_preceding_revocation_cannot_restore(self):
        knowledge, _ = self.published()
        refs = self.support()
        self.revoke_counterexample(knowledge)
        with self.assertRaises(ContractError):
            self.r.restore(self.p, knowledge["knowledge_id"], 1, reason="Earlier inspection", evidence_refs=refs)

    def test_counterexample_itself_not_support_even_if_new(self):
        knowledge, _ = self.published()
        self.revoke_counterexample(knowledge)
        fresh_counterexample = self.counterexample(knowledge, "Another reproduced regression")
        with self.assertRaises(ContractError):
            self.r.restore(self.p, knowledge["knowledge_id"], 1, reason="Another feedback received", evidence_refs=self.refs(fresh_counterexample))

    def test_dismissed_and_revised_dispositions_remain_closed(self):
        knowledge, _ = self.published()
        reopened = self.revoke_counterexample(knowledge)
        dismissed = self.counterexample(knowledge, "Dismissed observation")
        revised = self.counterexample(knowledge, "Previously revised observation")
        self.r.db.execute("UPDATE knowledge_feedback SET disposition='dismissed' WHERE problem_id=?", (dismissed["id"],))
        self.r.db.execute("UPDATE knowledge_feedback SET disposition='revised' WHERE problem_id=?", (revised["id"],))
        self.r.restore(self.p, knowledge["knowledge_id"], 1, reason="Fresh inspection", evidence_refs=self.support())
        states = {row[0]: row[1] for row in self.r.db.execute("SELECT problem_id,disposition FROM knowledge_feedback")}
        self.assertEqual(states[reopened["id"]], "unresolved")
        self.assertEqual(states[dismissed["id"]], "dismissed")
        self.assertEqual(states[revised["id"]], "revised")

    def test_direct_revoke_unresolved_counterexample_also_requires_support(self):
        knowledge, _ = self.published()
        self.counterexample(knowledge)
        self.r.revoke(self.p, knowledge["knowledge_id"], 1)
        with self.assertRaises(ContractError):
            self.r.restore(self.p, knowledge["knowledge_id"], 1)
        self.r.restore(self.p, knowledge["knowledge_id"], 1, reason="Fresh inspection", evidence_refs=self.support())
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["pending_counterexample_count"], 1)

    def test_restore_receipt_replay_cannot_revive_subsequent_revoke(self):
        knowledge, _ = self.published()
        self.r.revoke(self.p, knowledge["knowledge_id"], 1)
        first = self.r.restore(self.p, knowledge["knowledge_id"], 1, request_id="restore")
        self.r.revoke(self.p, knowledge["knowledge_id"], 1)
        self.assertEqual(self.r.restore(self.p, knowledge["knowledge_id"], 1, request_id="restore"), first)
        self.assertIsNone(lifecycle_state(self.r, self.p, knowledge["knowledge_id"])["active_revision"])

    def test_restore_changed_request_is_rejected(self):
        knowledge, _ = self.published()
        self.r.revoke(self.p, knowledge["knowledge_id"], 1)
        self.r.restore(self.p, knowledge["knowledge_id"], 1, request_id="restore")
        with self.assertRaises(ContractError):
            self.r.restore(self.p, knowledge["knowledge_id"], 1, request_id="restore", reason="Changed explanation")

    def test_lifecycle_callback_and_receipt_rollback_on_sync_failure(self):
        knowledge, _ = self.published()
        before = lifecycle_state(self.r, self.p, knowledge["knowledge_id"])
        with patch.object(self.r, "_queue_sync", side_effect=RuntimeError("isolated failure")):
            with self.assertRaises(RuntimeError):
                self.r.revoke(self.p, knowledge["knowledge_id"], 1, request_id="fail")
        self.assertEqual(lifecycle_state(self.r, self.p, knowledge["knowledge_id"]), before)
        self.assertEqual(self.action_count(), 1)

    def test_restore_feedback_and_receipt_rollback_on_failure(self):
        knowledge, _ = self.published()
        self.revoke_counterexample(knowledge)
        refs = self.support()
        before = lifecycle_state(self.r, self.p, knowledge["knowledge_id"])
        with patch.object(self.r, "_queue_sync", side_effect=RuntimeError("isolated failure")):
            with self.assertRaises(RuntimeError):
                self.r.restore(self.p, knowledge["knowledge_id"], 1, request_id="failed-restore", reason="Fresh inspection", evidence_refs=refs)
        self.assertEqual(lifecycle_state(self.r, self.p, knowledge["knowledge_id"]), before)
        self.assertEqual(self.r.db.execute("SELECT disposition FROM knowledge_feedback").fetchone()[0], "revoked")
        self.assertEqual(self.action_count(), 2)

    def test_migration_keeps_legacy_receipts_and_does_not_invent_actions(self):
        knowledge, candidate = self.published()
        receipt = self.r.db.execute("SELECT * FROM publication_receipts").fetchone()
        original_receipt = tuple(receipt)
        self.r.db.execute("DROP TABLE knowledge_actions")
        self.r.db.execute("DROP TABLE knowledge_lifecycle_versions")
        install_lifecycle(self.r)
        install_lifecycle(self.r)
        self.assertEqual(self.action_count(), 0)
        self.assertEqual(tuple(self.r.db.execute("SELECT * FROM publication_receipts").fetchone()), original_receipt)
        self.assertEqual(self.r.publish(self.p, candidate), knowledge)
        self.assertEqual(self.action_count(), 0)

    def test_invalid_metadata_and_refs_do_not_create_actions(self):
        knowledge, _ = self.published()
        for args in ({"request_id": ""}, {"reason": ""}, {"actor": ""}, {"request_id": "x" * 201}):
            with self.subTest(args=args), self.assertRaises(ContractError):
                self.r.revoke(self.p, knowledge["knowledge_id"], 1, **args)
        self.assertEqual(self.action_count(), 1)

    def test_optional_restore_refs_are_validated_even_without_counterexamples(self):
        knowledge, _ = self.published()
        self.r.revoke(self.p, knowledge["knowledge_id"], 1)
        with self.assertRaises(ContractError):
            self.r.restore(self.p, knowledge["knowledge_id"], 1, evidence_refs=["missing-event"])
        self.assertIsNone(lifecycle_state(self.r, self.p, knowledge["knowledge_id"])["active_revision"])

    def test_reported_feedback_cannot_restore_tool_success_claim_after_counterexample(self):
        self.r.handle_hook({"hook_event_name": "PreToolUse", "session_id": "chat", "cwd": str(self.workspace),
                            "tool_use_id": "success", "tool_name": "Bash"})
        self.r.handle_hook({"hook_event_name": "PostToolUse", "session_id": "chat", "cwd": str(self.workspace),
                            "tool_use_id": "success", "tool_name": "Bash", "tool_response": {"exit_code": 0}})
        event = self.r.db.execute("SELECT id FROM events WHERE type='PostToolUse'").fetchone()[0]
        knowledge, _ = self.published(refs=[event], level="tool_success")
        self.revoke_counterexample(knowledge)
        with self.assertRaises(ContractError):
            self.r.restore(self.p, knowledge["knowledge_id"], 1, reason="Only new report", evidence_refs=self.support())
        self.assertIsNone(lifecycle_state(self.r, self.p, knowledge["knowledge_id"])["active_revision"])

    def test_real_success_support_restores_tool_success_claim(self):
        def observe(ident):
            for kind in ("PreToolUse", "PostToolUse"):
                self.r.handle_hook({"hook_event_name": kind, "session_id": "chat", "cwd": str(self.workspace),
                                    "tool_use_id": ident, "tool_name": "Bash", "tool_response": {"exit_code": 0}})
            return self.r.db.execute("SELECT id FROM events WHERE type='PostToolUse' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        first = observe("first")
        knowledge, _ = self.published(refs=[first], level="tool_success")
        self.revoke_counterexample(knowledge)
        event = observe("fresh")
        support = self.r.feedback(self.p, "chat", "Fresh successful tool observation", category="operation", event_refs=[event])
        self.r.restore(self.p, knowledge["knowledge_id"], 1, reason="Rechecked tool result", evidence_refs=self.refs(support))
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["pending_counterexample_count"], 1)

    def test_no_change_action_records_receipt_without_version_or_callback(self):
        knowledge, _ = self.published()
        before = lifecycle_state(self.r, self.p, knowledge["knowledge_id"])
        callback = patch.object(self.r, "on_lifecycle_change")
        with callback as observed, self.r.transaction():
            record_action(self.r, self.p, "unchanged", {"action": "promote"}, "promote", knowledge["knowledge_id"], 1,
                          {"changed": False, "status": "unchanged"}, actor="operator", reason="Already certified",
                          evidence_refs=[], before_state=before)
        self.assertEqual(lifecycle_state(self.r, self.p, knowledge["knowledge_id"]), before)
        observed.assert_not_called()
        self.assertEqual(self.action_count(), 2)

    def test_action_receipts_redact_credentials_consistently(self):
        knowledge, _ = self.published()
        first = self.r.revoke(self.p, knowledge["knowledge_id"], 1, request_id="redact", reason="password=private Bearer private",
                              actor="api_key=private")
        second = self.r.revoke(self.p, knowledge["knowledge_id"], 1, request_id="redact", reason="password=private Bearer private",
                               actor="api_key=private")
        self.assertEqual(first, second)
        row = self.r.db.execute("SELECT input,reason,actor FROM knowledge_actions WHERE request_id='redact'").fetchone()
        self.assertNotIn("private", " ".join(row))


if __name__ == "__main__":
    unittest.main()
