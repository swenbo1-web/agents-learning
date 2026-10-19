"""Reference invalidation and provenance remain bounded and independent of UI/model calls."""
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from agent_learning import candidate_references as refs
from agent_learning.admin_publication import publication_gate
from agent_learning.runtime import encode
from agent_learning.work_files import normalize_file_target
from tests import test_candidate_references as fixtures


class CandidateContextLifecycleTests(unittest.TestCase):
    setUp = fixtures.CandidateReferenceTests.setUp
    candidate = fixtures.CandidateReferenceTests.candidate
    enable = fixtures.CandidateReferenceTests.enable

    def exposed_file(self):
        identity, _, _, event, _ = self.candidate()
        self.enable()
        target = self.workspace / "allocation.py"
        target.write_text("original", encoding="utf-8")
        link = normalize_file_target(str(target), self.workspace)
        self.r.db.execute("INSERT INTO work_file_links VALUES(?,?,?,?,?,?,?)", (self.p, event["id"],
            link["checkout_key"], link["relative_path"], link["path_key"], "read_target", encode(link["version"])))
        item = refs.assess(self.r, self.p, identity)
        refs.read(self.r, self.p, self.handle, identity, item["reference_version"], workspace=self.workspace)
        return identity, target, target.stat()

    def test_file_change_warns_previously_exposed_context(self):
        identity, target, _ = self.exposed_file()
        target.write_text("new larger content", encoding="utf-8")
        self.assertEqual(refs.search(self.r, self.p, "Python memory leak", context_handle=self.handle,
                                     workspace=self.workspace), [])
        notes = refs.notifications(self.r, self.p, self.handle, workspace=self.workspace)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["reason"], "reference_environment_or_files_changed")
        text = self.r._context(self.p, "consumer", query="continue", context_handle=self.handle,
                               workspace=self.workspace)
        self.assertIn(identity, text)
        self.assertIn("请停止依据旧摘要执行", text)
        self.assertEqual(refs.notifications(self.r, self.p, self.handle, workspace=self.workspace), [])

    def test_undelivered_file_warning_survives_restored_stat(self):
        identity, target, captured = self.exposed_file()
        target.write_text("new larger content", encoding="utf-8")
        self.r.config["injection_max_chars"] = 128
        small = self.r._context(self.p, "consumer", query="continue", context_handle=self.handle,
                                workspace=self.workspace)
        self.assertNotIn(identity, small)
        self.assertTrue(self.r.db.execute("SELECT pending_notice FROM candidate_reference_exposures WHERE candidate_id=?",
                                         (identity,)).fetchone()[0])
        target.write_text("original", encoding="utf-8")
        os.utime(target, ns=(captured.st_atime_ns, captured.st_mtime_ns))
        notes = refs.notifications(self.r, self.p, self.handle, workspace=self.workspace)
        self.assertEqual(len(notes), 1)
        self.r.config["injection_max_chars"] = 2400
        text = self.r._context(self.p, "consumer", query="continue", context_handle=self.handle,
                               workspace=self.workspace)
        self.assertIn("请停止依据旧摘要执行", text)
        self.assertEqual(refs.notifications(self.r, self.p, self.handle, workspace=self.workspace), [])

    def test_changed_file_snapshot_cannot_acknowledge_older_warning(self):
        _, target, _ = self.exposed_file()
        target.write_text("first new file state", encoding="utf-8")
        notes = refs.notifications(self.r, self.p, self.handle, workspace=self.workspace)
        target.write_text("second new file state with different length", encoding="utf-8")
        self.assertEqual(refs.ack_notifications(self.r, self.p, self.handle, notes, workspace=self.workspace), 0)
        current = refs.notifications(self.r, self.p, self.handle, workspace=self.workspace)
        self.assertNotEqual(notes[0]["id"], current[0]["id"])
        self.assertEqual(refs.ack_notifications(self.r, self.p, self.handle, current, workspace=self.workspace), 1)

    def test_environment_change_warns_without_touching_database_source(self):
        identity, _, _, _, _ = self.candidate()
        self.enable()
        manifest = self.workspace / "package.json"
        manifest.write_text('{"name":"memory-project","version":"1.0.0"}', encoding="utf-8")
        item = refs.assess(self.r, self.p, identity)
        refs.read(self.r, self.p, self.handle, identity, item["reference_version"], workspace=self.workspace)
        manifest.write_text('{"name":"memory-project","version":"2.0.0"}', encoding="utf-8")
        self.assertEqual(refs.assess(self.r, self.p, identity)["reference_version"], item["reference_version"])
        notes = refs.notifications(self.r, self.p, self.handle, workspace=self.workspace)
        self.assertEqual(len(notes), 1)
        self.assertIn("environment", notes[0]["reason"])

    def public_restatement(self):
        identity, _, _, _, _ = self.candidate()
        self.enable()
        refs.record_exposure(self.r, self.p, self.handle, refs.assess(self.r, self.p, identity))
        event, _ = self.r._event(self.p, "consumer", "Stop", {"last_assistant_message": "A repeated project claim"},
            task_id=self.target["id"], task_revision=self.target["revision"], outcome="reported")
        return identity, event

    def test_public_provenance_exists_when_work_memory_disabled(self):
        _, event = self.public_restatement()
        self.assertFalse(self.r.config["work_memory"]["enabled"])
        self.assertIsNone(self.r._work_capture_event(self.p, event["id"], self.workspace))
        self.assertTrue(refs.lineage_risk(self.r, self.p, event["id"])["possible_influence"])
        problem = self.r._problem(self.p, "consumer", "A fresh project fact", "project", [event["id"]])
        body = {"problem_id": problem["id"], "problem_revision": problem["revision"], "title": "Repeated project claim",
            "content": "A repeated project claim", "category": "project", "knowledge_kind": "fact",
            "applicability": "Current project", "limitations": "Reported only", "validation_level": "reported",
            "evidence_refs": [event["id"]]}
        reason = publication_gate(self.r, self.p, self.r.review_packet(self.p, problem["id"]), body,
                                  {"action": "publish", "knowledge_id": None}, mode="tiered")
        self.assertEqual(reason, "candidate reference requires independent source evidence")

    def test_work_projection_failure_cannot_erase_public_provenance(self):
        _, event = self.public_restatement()
        with self.r.transaction(), patch.object(self.r, "_work_capture_event", side_effect=sqlite3.OperationalError("projection unavailable")):
            self.r._work_capture_safely(self.p, event["id"], self.workspace)
        self.assertTrue(refs.lineage_risk(self.r, self.p, event["id"])["possible_influence"])

    def test_manual_hold_override_still_checks_equivalent_concerns(self):
        identity, body, _, _, _ = self.candidate()
        self.enable()
        item = refs.assess(self.r, self.p, identity)
        refs.record_exposure(self.r, self.p, self.handle, item)
        refs.feedback(self.r, self.p, self.handle, identity, item["reference_version"],
            summary="Current evidence contradicts this candidate", idempotency_key="held-concern",
            expected_task_id=self.target["id"], expected_task_revision=self.target["revision"])
        self.r.db.execute("UPDATE candidates SET status='deferred' WHERE id=?", (identity,))
        self.assertEqual(refs.held_equivalent(self.r, self.p, body), "equivalent candidate is held")
        self.assertEqual(refs.held_equivalent(self.r, self.p, body, include_held=False),
                         "equivalent candidate has unresolved concerns")

    def graph(self, levels, width=3):
        prior = []
        for depth in range(levels):
            layer = []
            for index in range(width):
                identity, _, _, event, _ = self.candidate(title=f"Graph {depth} item {index}",
                    content=f"Distinct graph body {depth}/{index}", conversation=f"graph-{depth}-{index}")
                if prior:
                    refs.record_lineage(self.r, self.p, event["id"],
                        [{"candidate_id": node, "reference_version": "a" * 64} for node in prior])
                layer.append(identity)
            prior = layer
        return prior[0]

    def test_shared_dag_uses_memo_instead_of_exponential_revisits(self):
        self.enable()
        identity = self.graph(6)
        original = refs.assess
        with patch.object(refs, "assess", side_effect=original) as calls:
            result = refs.assess(self.r, self.p, identity)
        self.assertTrue(result["eligible"])
        self.assertLessEqual(calls.call_count, 45)  # Previously 364 visits for 16 reachable candidates.

    def test_whole_traversal_budget_fails_closed(self):
        self.enable()
        identity = self.graph(3)
        with patch.object(refs, "MAX_ASSESSMENTS", 2):
            self.assertFalse(refs.assess(self.r, self.p, identity)["eligible"])
        with patch.object(refs, "MAX_SOURCE_VISITS", 0):
            self.assertEqual(refs.assess(self.r, self.p, identity)["reason"], "candidate_lineage_budget_exceeded")
        with patch.object(refs, "MAX_LINEAGE_VISITS", 0):
            self.assertFalse(refs.assess(self.r, self.p, identity)["eligible"])

    def test_short_path_memo_cannot_bypass_longer_path_depth_limit(self):
        self.enable()
        leaf, _, _, _, _ = self.candidate(title="shared leaf", conversation="leaf")
        chain = leaf
        for index in range(refs.MAX_ANCESTORS):
            identity, _, _, event, _ = self.candidate(title=f"Chain {index}", conversation=f"chain-{index}")
            refs.record_lineage(self.r, self.p, event["id"], [{"candidate_id": chain, "reference_version": "a" * 64}])
            chain = identity
        root, _, _, event, _ = self.candidate(title="Shared and deep root", conversation="root")
        refs.record_lineage(self.r, self.p, event["id"], [
            {"candidate_id": leaf, "reference_version": "a" * 64},
            {"candidate_id": chain, "reference_version": "a" * 64}])
        self.assertFalse(refs.assess(self.r, self.p, root)["eligible"])


if __name__ == "__main__":
    unittest.main()
