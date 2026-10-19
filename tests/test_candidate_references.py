"""Candidate references are clues with durable provenance, never publication."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent_learning import candidate_references as refs
from agent_learning.runtime import ContractError, Runtime, encode


class CandidateReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="candidate-reference-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={"work_memory": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.target = self.r.task_update(self.p, "consumer", "Investigate memory leak", new_task=True)
        self.handle = self.r.register_context(self.p, "consumer")["context_handle"]

    def enable(self):
        current = refs.policy(self.r, self.p)
        return refs.configure(self.r, self.p, True, current["version"])

    def candidate(self, title="Python memory leak", content="Inspect retained Python allocations and compare snapshots", *,
                  validation="reported", outcome="reported", kind="Stop", operation_id=None,
                  conversation="source", payload=None, evidence=None):
        task = self.r.task(self.p, conversation) or self.r.task_update(self.p, conversation, title, new_task=True)
        event, _ = self.r._event(self.p, conversation, kind, payload or {"last_assistant_message": content},
            task_id=task["id"], task_revision=task["revision"], outcome=outcome, operation_id=operation_id)
        problem = self.r._problem(self.p, conversation, title + str(event["id"]), "operation", [event["id"]], task_binding=task)
        body = {"problem_id": problem["id"], "problem_revision": problem["revision"], "title": title,
            "content": content, "category": "operation", "knowledge_kind": "solution", "metadata": {},
            "applicability": "Python allocation analysis in this project", "limitations": "Reported observation; validate before using",
            "validation_level": validation, "evidence_refs": evidence or [event["id"]]}
        response = self.r.submit_candidate(self.p, **body)
        return response["candidate_id"], body, problem, event, task

    def test_policy_default_off_explicit_cas_and_read_only_queries(self):
        identity, _, _, _, _ = self.candidate()
        before = self.r.db.total_changes
        self.assertEqual(refs.policy(self.r, self.p), {"enabled": False, "version": 0})
        self.assertEqual(refs.search(self.r, self.p, "Python memory leak"), [])
        self.assertFalse(refs.assess(self.r, self.p, identity)["eligible"])
        self.assertEqual(self.r.db.total_changes, before)
        self.enable()
        self.assertTrue(refs.assess(self.r, self.p, identity)["eligible"])
        with self.assertRaises(ContractError):
            refs.configure(self.r, self.p, True, 0)
        with self.assertRaises(ContractError):
            refs.configure(self.r, self.p, 1, 1)

    def test_index_recall_relevance_empty_query_and_project_isolation(self):
        identity, _, _, _, _ = self.candidate()
        self.candidate("Excel workbook checking", "Compare workbook sheet structure", conversation="other")
        chinese, _, _, _, _ = self.candidate("内存泄漏定位", "比较对象引用和内存泄漏快照", conversation="zh")
        self.enable()
        self.assertEqual(refs.search(self.r, self.p, "Python memory leak")[0]["candidate_id"], identity)
        self.assertEqual(refs.search(self.r, self.p, "内存泄漏")[0]["candidate_id"], chinese)
        self.assertEqual(refs.search(self.r, self.p, ""), [])
        self.assertEqual(refs.search(self.r, self.p, "查"), [])
        self.assertEqual(refs.search(self.r, self.p, "Rust authentication signatures"), [])
        foreign = self.root / "foreign"
        foreign.mkdir()
        other = self.r.project(foreign)
        refs.configure(self.r, other, True, 0)
        self.assertEqual(refs.search(self.r, other, "Python memory leak"), [])
        with self.assertRaises(ContractError):
            refs.search(self.r, other, "Python memory leak", context_handle=self.handle)

    def test_index_status_changes_and_optional_projection_failure(self):
        identity, _, _, _, _ = self.candidate()
        self.enable()
        self.r.db.execute("UPDATE candidates SET status='rejected' WHERE id=?", (identity,))
        self.assertEqual(refs.search(self.r, self.p, "Python memory leak"), [])
        self.r.db.execute("DROP TABLE candidate_reference_fts")
        with self.assertRaises(refs.ReferenceUnavailable):
            refs.search(self.r, self.p, "Python memory leak")

    def test_source_revision_and_operation_conflict_block_reported_candidates(self):
        identity, _, problem, _, _ = self.candidate()
        self.enable()
        old = refs.assess(self.r, self.p, identity)
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (problem["id"],))
        self.assertEqual(refs.assess(self.r, self.p, identity)["reason"], "source_case_changed")
        self.assertIsNone(refs.revalidate(self.r, self.p, old))
        second, _, _, event, task = self.candidate("Connection retry", "Reconnect only after checking the actual route", conversation="conflict")
        self.r.db.execute("INSERT INTO operations(project,id,conversation,task_id,task_revision,agent,state,last_event,conflict,decision) "
            "VALUES(?,?,?,?,?,NULL,'conflict',?,1,'denied')", (self.p, "conflicted-op", "conflict", task["id"], task["revision"], event["id"]))
        self.r.db.execute("UPDATE events SET operation='conflicted-op' WHERE id=?", (event["id"],))
        self.assertEqual(refs.assess(self.r, self.p, second)["reason"], "source_operation_conflict")

    def test_paging_fixes_source_version_and_records_complete_response(self):
        identity, _, problem, _, _ = self.candidate(content="allocation " * 250)
        self.enable()
        item = refs.assess(self.r, self.p, identity)
        first = refs.read(self.r, self.p, self.handle, identity, item["reference_version"], max_chars=512)
        self.assertLessEqual(len(encode(first)), 512)
        self.assertGreater(len(first["content"]), 0)
        self.assertEqual(first["next_offset"], len(first["content"]))
        self.assertTrue(refs.exposed(self.r, self.p, self.handle, item))
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (problem["id"],))
        with self.assertRaises(ContractError):
            refs.read(self.r, self.p, self.handle, identity, item["reference_version"], offset=first["next_offset"], max_chars=512)

    def test_persistent_dedup_ephemeral_compaction_and_task_boundary(self):
        identity, _, _, _, _ = self.candidate()
        self.enable()
        item = refs.search(self.r, self.p, "Python memory leak")[0]
        refs.record_exposure(self.r, self.p, self.handle, item, context_epoch=2)
        kwargs = {"context_handle": self.handle, "context_epoch": 2}
        self.assertEqual(refs.search(self.r, self.p, "Python memory leak", delivery="persistent", **kwargs), [])
        self.assertEqual(refs.search(self.r, self.p, "Python memory leak", delivery="ephemeral", **kwargs)[0]["candidate_id"], identity)
        self.assertTrue(refs.search(self.r, self.p, "Python memory leak", delivery="persistent", compact=True, **kwargs))
        self.r.task_update(self.p, "consumer", "New task", new_task=True)
        self.assertTrue(refs.search(self.r, self.p, "Python memory leak", delivery="persistent", **kwargs))

    def test_notification_pending_until_complete_ack_and_no_stale_ack(self):
        identity, _, _, _, _ = self.candidate()
        self.enable()
        item = refs.assess(self.r, self.p, identity)
        refs.record_exposure(self.r, self.p, self.handle, item)
        self.r.db.execute("UPDATE candidates SET status='deferred' WHERE id=?", (identity,))
        notes = refs.notifications(self.r, self.p, self.handle)
        self.assertEqual(len(notes), 1)
        self.assertEqual(len(refs.notifications(self.r, self.p, self.handle)), 1)
        self.r.db.execute("UPDATE candidates SET status='rejected' WHERE id=?", (identity,))
        self.assertEqual(refs.ack_notifications(self.r, self.p, self.handle, notes), 0)
        current = refs.notifications(self.r, self.p, self.handle)
        self.assertEqual(refs.ack_notifications(self.r, self.p, self.handle, current), 1)
        self.assertEqual(refs.notifications(self.r, self.p, self.handle), [])

    def test_publication_redirect_prevents_candidate_resurrection_after_revoke(self):
        identity, _, _, _, _ = self.candidate()
        self.enable()
        item = refs.assess(self.r, self.p, identity)
        result = self.r.publish(self.p, identity)
        redirect = refs.read(self.r, self.p, self.handle, identity, item["reference_version"])
        self.assertIsNone(redirect["content"])
        self.assertTrue(redirect["redirect"]["active"])
        self.r.revoke(self.p, result["knowledge_id"], result["revision"], request_id="revoke-ref", reason="Incorrect", actor="test")
        self.assertFalse(refs.assess(self.r, self.p, identity)["redirect"]["active"])
        self.assertEqual(refs.search(self.r, self.p, "Python memory leak"), [])

    def test_counterexample_requires_context_exposure_and_idempotent_receipt(self):
        identity, _, _, _, _ = self.candidate()
        self.enable()
        item = refs.assess(self.r, self.p, identity)
        kwargs = {"summary": "This recommendation does not match the active allocator", "idempotency_key": "concern-1",
            "expected_task_id": self.target["id"], "expected_task_revision": self.target["revision"]}
        with self.assertRaises(ContractError):
            refs.feedback(self.r, self.p, self.handle, identity, item["reference_version"], **kwargs)
        refs.record_exposure(self.r, self.p, self.handle, item)
        first = refs.feedback(self.r, self.p, self.handle, identity, item["reference_version"], **kwargs)
        self.assertEqual(first["state"], "disputed")
        self.assertEqual(refs.feedback(self.r, self.p, self.handle, identity, item["reference_version"], **kwargs), first)
        self.assertEqual(refs.assess(self.r, self.p, identity)["reason"], "candidate_disputed")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM knowledge_feedback").fetchone()[0], 0)
        with self.assertRaises(ContractError):
            refs.feedback(self.r, self.p, self.handle, identity, item["reference_version"], **{**kwargs, "summary": "Different request"})

    def test_possible_influence_is_not_adoption_and_only_same_task_public_events(self):
        identity, _, _, _, _ = self.candidate()
        self.enable()
        item = refs.assess(self.r, self.p, identity)
        refs.record_exposure(self.r, self.p, self.handle, item)
        event, _ = self.r._event(self.p, "consumer", "Stop", {"last_assistant_message": "Reported observation"},
            task_id=self.target["id"], task_revision=1, outcome="reported")
        self.assertEqual(refs.record_possible_influence(self.r, self.p, event["id"]), 1)
        risk = refs.lineage_risk(self.r, self.p, event["id"])
        self.assertTrue(risk["possible_influence"])
        self.assertFalse(risk["blocked"])
        tool, _ = self.r._event(self.p, "consumer", "PostToolUse", {"tool": "python"},
            task_id=self.target["id"], task_revision=1, outcome="succeeded")
        self.assertEqual(refs.record_possible_influence(self.r, self.p, tool["id"]), 0)
        self.r.db.execute("UPDATE candidates SET status='rejected' WHERE id=?", (identity,))
        self.assertTrue(refs.lineage_risk(self.r, self.p, event["id"])["blocked"])
        self.assertIsNone(refs.lineage_risk(self.r, self.p, tool["id"]))
        task = self.r.task_update(self.p, "consumer", "Independent stage", new_task=True)
        fresh, _ = self.r._event(self.p, "consumer", "Stop", {"last_assistant_message": "New result"},
            task_id=task["id"], task_revision=1, outcome="reported")
        self.assertEqual(refs.influence_for_event(self.r, self.p, fresh), [])

    def test_formal_equivalent_and_conflicting_titles_are_excluded(self):
        first, _, _, _, _ = self.candidate()
        self.enable()
        self.r.publish(self.p, first)
        second, _, _, _, _ = self.candidate(conversation="fresh-source")
        self.assertEqual(refs.assess(self.r, self.p, second)["reason"], "equivalent_formal_knowledge")
        third, _, _, _, _ = self.candidate(content="Contradict the previous allocator guidance", conversation="conflicting-source")
        self.assertEqual(refs.assess(self.r, self.p, third)["reason"], "knowledge_title_requires_review")

    def test_projection_installation_is_idempotent_and_requires_transaction(self):
        identity, _, _, _, _ = self.candidate()
        with self.assertRaises(ContractError):
            refs.install(self.r)
        with self.r.transaction():
            refs.install(self.r)
            refs.install(self.r)
        self.enable()
        self.assertEqual(refs.search(self.r, self.p, "Python memory leak")[0]["candidate_id"], identity)


if __name__ == "__main__":
    unittest.main()
