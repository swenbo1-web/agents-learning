"""管理员持久决定、增量签名与真实 SQLite 回归，不调用模型。"""
import copy
from pathlib import Path
import tempfile
import unittest

from agent_learning.admin import _fingerprint
from agent_learning.admin_decisions import (dependency_signature, find_receipt, get_reviews,
                                            install_decisions, persist_receipt, receipt_for_problem,
                                            review_signature)
from agent_learning.runtime import ContractError, Runtime


class Module6DecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-module6-decisions-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home = root / "state"
        self.workspace = root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.home)
        self.addCleanup(lambda: self.r.close())
        self.p = self.r.project(self.workspace)
        self.cfg = copy.deepcopy(self.r.config["admin"])
        self.r.task_update(self.p, "chat", "Diagnose the supplied observation", {})
        self.problem = self.r.feedback(self.p, "chat", "An exact tool issue", category="operation")
        self.packet = self.r.review_packet(self.p, self.problem["id"])
        self.evidence = _fingerprint(self.packet)
        self.signature = review_signature(self.packet, self.cfg, self.evidence)

    def persist(self, decision="needs_evidence", **kwargs):
        body = {"decision": decision, "reason": "Need exact input and environment", "candidate": None}
        body.update(kwargs)
        return persist_receipt(self.r, self.p, self.packet, self.evidence, self.signature, "call-1", body)

    def test_related_knowledge_revision_content_and_health_change_signature(self):
        packet = copy.deepcopy(self.packet)
        packet["related_knowledge"] = [{"knowledge_id": "k", "revision": 1, "content": "Resolution A",
                                         "source_health": "valid", "current_tool_success": True}]
        signature = review_signature(packet, self.cfg, self.evidence)
        for key, value in (("revision", 2), ("content", "Resolution B"), ("source_health", "conflicted"),
                           ("current_tool_success", False), ("knowledge_id", "other")):
            changed = copy.deepcopy(packet)
            changed["related_knowledge"][0][key] = value
            self.assertNotEqual(signature, review_signature(changed, self.cfg, self.evidence), key)
        self.assertNotEqual(self.signature, signature)

    def test_candidate_body_keeps_business_version_but_ignores_case_and_event_identity(self):
        packet = copy.deepcopy(self.packet)
        packet["related_candidates"] = [{"candidate_id": "candidate", "status": "candidate",
                                           "body": {"problem_id": "old", "problem_revision": 1,
                                                    "evidence_refs": ["old-event"], "content": "Resolution",
                                                    "metadata": {"tool_version": "1"}}}]
        changed = copy.deepcopy(packet)
        changed["related_candidates"][0]["body"].update(problem_id="new", problem_revision=2,
                                                         evidence_refs=["new-event"])
        self.assertEqual(dependency_signature(packet), dependency_signature(changed))
        changed["related_candidates"][0]["body"]["metadata"]["tool_version"] = "2"
        self.assertNotEqual(dependency_signature(packet), dependency_signature(changed))

    def test_candidate_produced_from_same_evidence_does_not_trigger_another_review(self):
        packet = copy.deepcopy(self.packet)
        packet["related_candidates"] = [{"candidate_id": "generated", "content": "Existing review result",
                                           "origin_evidence_signature": self.evidence}]
        self.assertEqual(self.signature, review_signature(packet, self.cfg, self.evidence))
        packet["related_candidates"][0]["origin_evidence_signature"] = "other-evidence"
        self.assertNotEqual(self.signature, review_signature(packet, self.cfg, self.evidence))
        packet["related_candidates"][0].pop("origin_evidence_signature")
        self.assertNotEqual(self.signature, review_signature(packet, self.cfg, self.evidence))

    def test_same_evidence_candidate_does_not_hide_new_knowledge(self):
        packet = copy.deepcopy(self.packet)
        packet["related_candidates"] = [{"candidate_id": "generated", "origin_evidence_signature": self.evidence}]
        packet["related_knowledge"] = [{"knowledge_id": "outside", "revision": 1, "content": "New conclusion"}]
        self.assertNotEqual(self.signature, review_signature(packet, self.cfg, self.evidence))

    def test_packet_candidate_origin_is_read_by_exact_indexed_identity(self):
        body = {"problem_id": self.problem["id"], "problem_revision": self.problem["revision"],
                "title": "Exact tool issue", "content": "Diagnose exact tool issue from observation",
                "category": "operation", "applicability": "Exact supplied tool issue",
                "limitations": "Only observed records", "evidence_refs": [e["id"] for e in self.packet["events"]],
                "validation_level": "reported"}
        candidate = self.r.submit_candidate(self.p, **body)
        self.persist("candidate", candidate=body, candidate_id=candidate["candidate_id"])
        another = self.r.feedback(self.p, "chat", "Exact tool issue on another occasion", category="operation")
        packet = self.r.review_packet(self.p, another["id"])
        summaries = {item["candidate_id"]: item for item in packet["related_candidates"]}
        self.assertEqual(summaries[candidate["candidate_id"]]["origin_evidence_signature"], self.evidence)
        plan = self.r.db.execute("EXPLAIN QUERY PLAN SELECT evidence_signature FROM review_receipts WHERE project=? "
                                 "AND decision='candidate' AND json_extract(response,'$.candidate_id')=? "
                                 "ORDER BY created DESC LIMIT 1", (self.p, candidate["candidate_id"])).fetchall()
        self.assertTrue(any("review_receipts_candidate_origin" in str(tuple(row)) for row in plan))

    def test_target_knowledge_and_policy_changes_invalidate_review(self):
        packet = copy.deepcopy(self.packet)
        packet["target_knowledge"] = {"knowledge_id": "target", "revision": 2, "content": "Old claim",
                                      "source_health": "valid"}
        self.assertNotEqual(self.signature, review_signature(packet, self.cfg, self.evidence))
        for key, value in (("command", ["another-reviewer"]), ("model", "other-model"),
                           ("max_input_chars", 12000), ("auto_publish", True), ("review_policy", "v2")):
            cfg = dict(self.cfg, **{key: value})
            self.assertNotEqual(self.signature, review_signature(self.packet, cfg, self.evidence), key)
        cfg = dict(self.cfg, enabled=True, max_calls_per_day=10, timeout_seconds=100,
                   max_cases_per_batch=12, max_calls_per_batch=2, max_batch_seconds=60)
        self.assertEqual(self.signature, review_signature(self.packet, cfg, self.evidence))

    def test_review_instruction_and_packet_format_invalidate_signature(self):
        from unittest.mock import patch
        with patch("agent_learning.admin_decisions.REVIEW_PREFIX", "New rules"):
            self.assertNotEqual(self.signature, review_signature(self.packet, self.cfg, self.evidence))
        with patch("agent_learning.admin_decisions.PACKET_FORMAT", 999):
            self.assertNotEqual(self.signature, review_signature(self.packet, self.cfg, self.evidence))

    def test_dependency_order_transport_changes_do_not_invalidate(self):
        packet = copy.deepcopy(self.packet)
        packet["related_knowledge"] = [{"knowledge_id": "a", "revision": 1, "created": 2},
                                        {"knowledge_id": "b", "revision": 3}]
        changed = copy.deepcopy(packet)
        changed["related_knowledge"].reverse()
        changed["related_knowledge"][1]["created"] = 99
        self.assertEqual(dependency_signature(packet), dependency_signature(changed))

    def test_needs_evidence_is_durable_without_changing_problem_or_resolution(self):
        result = self.persist()
        self.assertEqual(result["review_state"], "awaiting_evidence")
        self.r.close()
        self.r = Runtime(self.home)
        receipt = find_receipt(self.r, self.p, self.signature)
        self.assertEqual(receipt["response"]["missing_evidence"],
                         [{"description": "Need exact input and environment", "structured": False}])
        row = self.r.db.execute("SELECT status,resolution_status FROM problems WHERE id=?", (self.problem["id"],)).fetchone()
        self.assertEqual(tuple(row), ("pending", "open"))
        self.assertEqual(get_reviews(self.r, self.p, self.problem["id"])["items"][0]["state"], "awaiting_evidence")

    def test_structured_missing_evidence_is_bounded_and_not_inferred(self):
        self.persist(missing_evidence=[{"field": "tool_version", "description": "Exact installed version"},
                                       "Failed command"] + ["Other"] * 10)
        receipt = find_receipt(self.r, self.p, self.signature)
        items = receipt["response"]["missing_evidence"]
        self.assertEqual(len(items), 8)
        self.assertEqual(items[0]["field"], "tool_version")
        self.assertTrue(all(item["structured"] for item in items))

    def test_failed_unknown_calls_are_not_reusable_decisions(self):
        install_decisions(self.r)
        for state in ("failed", "timeout_unknown", "account_changed_unknown", "started"):
            with self.assertRaises(ContractError):
                self.persist(state)
        self.assertIsNone(find_receipt(self.r, self.p, self.signature))
        self.assertEqual(get_reviews(self.r, self.p)["items"], [])

    def test_preflight_block_is_visible_but_not_a_reusable_model_decision(self):
        self.persist("preflight_blocked")
        self.assertIsNone(find_receipt(self.r, self.p, self.signature))
        receipt = receipt_for_problem(self.r, self.p, self.problem["id"], self.problem["revision"], self.signature)
        self.assertEqual(receipt["decision"], "preflight_blocked")
        self.assertEqual(get_reviews(self.r, self.p)["items"][0]["state"], "awaiting_evidence")

    def test_receipt_idempotent_and_conflicting_decision_rejected(self):
        self.persist("skip")
        self.persist("skip")
        self.assertEqual(len(get_reviews(self.r, self.p)["items"]), 1)
        with self.assertRaises(ContractError):
            self.persist("needs_evidence")

    def test_governance_result_compatibility_persists_existing_proposal_reference(self):
        result = persist_receipt(self.r, self.p, self.packet, self.evidence, self.signature, "call-1",
                                 {"status": "governance", "reason": "Propose dismissing a contradicted claim",
                                  "proposal_id": "proposal-1"})
        self.assertEqual(result["review_state"], "proposal_ready")
        receipt = find_receipt(self.r, self.p, self.signature)
        self.assertEqual(receipt["decision"], "governance")
        self.assertEqual(get_reviews(self.r, self.p)["items"][0]["proposal_id"], "proposal-1")

    def test_new_evidence_invalidates_signature_and_stale_decision_cannot_commit(self):
        self.persist()
        self.r.feedback(self.p, "chat", "An exact tool issue", category="operation", resolution="New observation")
        packet = self.r.review_packet(self.p, self.problem["id"])
        signature = review_signature(packet, self.cfg, _fingerprint(packet))
        self.assertNotEqual(self.signature, signature)
        self.assertIsNone(find_receipt(self.r, self.p, signature))
        self.assertIsNone(find_receipt(self.r, self.p, self.signature))
        self.assertFalse(get_reviews(self.r, self.p)["items"][0]["current_material"])
        with self.assertRaises(ContractError):
            self.persist()

    def test_existing_transaction_is_not_committed_by_receipt(self):
        with self.assertRaises(RuntimeError):
            with self.r.transaction():
                self.persist()
                self.assertTrue(self.r.db.in_transaction)
                raise RuntimeError("rollback parent operation")
        install_decisions(self.r)
        self.assertIsNone(find_receipt(self.r, self.p, self.signature))

    def test_review_list_budget_and_project_isolation(self):
        self.persist(reason="password=secret-value " + "x" * 2000)
        receipt = find_receipt(self.r, self.p, self.signature)
        self.assertNotIn("secret-value", receipt["reason"])
        self.assertLessEqual(len(receipt["reason"]), 1000)
        self.assertEqual(get_reviews(self.r, "other-project")["items"], [])
        self.assertIsNone(find_receipt(self.r, "other-project", self.signature))
        for limit in (0, 101, True):
            with self.assertRaises(ContractError):
                get_reviews(self.r, self.p, limit=limit)

    def test_review_list_has_actual_total_character_budget(self):
        from agent_learning.runtime import encode
        for index in range(12):
            problem = self.r.feedback(self.p, "chat", f"Distinct issue {index}", category="operation")
            packet = self.r.review_packet(self.p, problem["id"])
            evidence = _fingerprint(packet)
            signature = review_signature(packet, self.cfg, evidence)
            persist_receipt(self.r, self.p, packet, evidence, signature, f"call-{index}",
                            {"decision": "needs_evidence", "reason": "r" * 1000, "candidate": None})
        result = get_reviews(self.r, self.p)
        self.assertTrue(result["truncated"])
        self.assertGreater(len(result["items"]), 0)
        self.assertLess(len(result["items"]), 12)
        self.assertLessEqual(len(encode(result)), 12000)


if __name__ == "__main__":
    unittest.main()
