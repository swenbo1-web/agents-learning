"""Publication permissions are not new evidence; summaries preserve actual source scope."""
import json
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_control import apply_control, control_status
from agent_learning.admin_decisions import persist_receipt, review_signature
from agent_learning.admin_workload import evidence_wait_signature, hold_unchanged_review
from agent_learning.runtime import digest
from tests import test_admin_workload as workload_fixtures
from tests import test_work_summaries as summary_fixtures
from tests.test_module6_queue import fake_reviewer


class EvidenceWaitFlowTests(unittest.TestCase):
    setUp = workload_fixtures.AdminWorkloadTests.setUp
    problem = workload_fixtures.AdminWorkloadTests.problem

    def configure_publication(self, enabled, mode):
        state = control_status(self.r, self.p, details=False)
        return apply_control(self.r, self.p, {"request_id": f"policy-{enabled}-{mode}",
            "expected_policy_version": state["policy"]["policy_version"],
            "expected_settings_version": state["settings_version"],
            "expected_session_epoch": state["session_epoch"], "expected_governance_epoch": state["governance_epoch"],
            "expected_project_budget_version": state["project_budget_policy"]["version"],
            "enabled": True, "model_mode": "default", "model": "", "project_limits": {},
            "auto_publish": enabled, "publication_mode": mode, "start": False}, details=False)

    def test_publication_switches_do_not_buy_the_same_missing_evidence_review_again(self):
        problem = self.problem("Explicit explanation still needs a verification")
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("needs_evidence")):
            admin_once(self.r, self.p)
        for enabled, mode in ((True, "verified_operation_solutions"), (True, "tiered"), (False, "tiered")):
            self.configure_publication(enabled, mode)
            with patch("agent_learning.admin.subprocess.Popen") as model:
                result = admin_once(self.r, self.p)
            model.assert_not_called()
            self.assertIn(result["status"], {"needs_evidence", "scan_complete", "idle"})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("skip")):
            result = admin_once(self.r, self.p, problem["id"])
        self.assertEqual(result["status"], "skip")

    def test_v1_signed_wait_memos_survive_only_the_known_publication_fields(self):
        from agent_learning.admin import _fingerprint
        for mode in (None, "verified_operation_solutions", "tiered"):
            for enabled in (False, True):
                with self.subTest(mode=mode, enabled=enabled):
                    problem = self.problem(f"prior review {mode} {enabled}")
                    packet = self.r.review_packet(self.p, problem["id"])
                    evidence = _fingerprint(packet)
                    cfg = self.r.admin_effective_config(self.p)
                    prior = {k: v for k, v in cfg.items() if k not in
                             {"model", "model_mode", "model_source", "publication_mode"}}
                    prior["auto_publish"] = enabled
                    if mode is not None:
                        prior["publication_mode"] = mode
                    signed = digest(["evidence-wait-v1", review_signature(packet, prior, evidence)])
                    persist_receipt(self.r, self.p, packet, evidence, "prior-"+problem["id"], None,
                        {"decision": "needs_evidence", "reason": "Still needs an actual verification"},
                        evidence_wait_signature=signed)
                    self.assertTrue(hold_unchanged_review(self.r, self.p, packet, evidence, cfg))
                    self.assertFalse(hold_unchanged_review(self.r, self.p, packet, evidence,
                                                         dict(cfg, max_input_chars=cfg["max_input_chars"] + 1000)))
                    self.assertFalse(hold_unchanged_review(self.r, self.p, packet, evidence,
                                                         dict(cfg, provider="changed-provider")))
                    self.assertFalse(hold_unchanged_review(self.r, self.p, packet, "different-evidence", cfg))

    def test_changed_evidence_still_enters_automatic_review_after_a_publication_change(self):
        problem = self.problem()
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("needs_evidence")):
            admin_once(self.r, self.p)
        self.configure_publication(True, "tiered")
        event = self.r.record_event(self.p, "root", "PostToolUse", {"tool_response": {"exit_code": 0}}, outcome="succeeded")
        self.r._attach_case_evidence(problem["id"], [(event["id"], "potential_recovery")])
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("skip")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "skip")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 2)


class SummaryScopeFlowTests(unittest.TestCase):
    setUp = summary_fixtures.WorkSummaryTests.setUp
    event = summary_fixtures.WorkSummaryTests.event
    sources = summary_fixtures.WorkSummaryTests.sources
    queue = summary_fixtures.WorkSummaryTests.queue
    run_review = summary_fixtures.WorkSummaryTests.run_review

    def unknown_source(self, text):
        with self.r.transaction():
            event, _ = self.r._event(self.p, "chat", "Stop", {"last_assistant_message": text}, outcome="reported")
            self.r._work_capture_event(self.p, event["id"], self.workspace)
        return event

    def test_unbound_sources_do_not_claim_the_current_task_goal(self):
        self.unknown_source("Compared legacy storage options; current deployment validation remains pending.")
        self.unknown_source("Recorded the earlier project's SQLite choice; no animation work was verified.")
        queued = self.queue()
        packet = self.r.work_summary_packet(self.p, queued["problem_id"])
        self.assertEqual(packet["scope"], "conversation")
        self.assertNotIn("animation mapping", packet["problem"]["summary"])
        self.assertTrue(all(not event["task_binding_known"] for event in packet["events"]))
        reviewed = self.run_review(queued)
        row = self.r.db.execute("SELECT task_id,task_revision,json_extract(body,'$.scope') FROM work_memories WHERE id=?",
                                (reviewed["memory_id"],)).fetchone()
        self.assertEqual(tuple(row), (None, None, "conversation"))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_mixed_scope_is_consistent_in_packet_and_accepted_summary(self):
        self.sources()
        self.unknown_source("Earlier storage notes have no confirmed task binding; preserve that uncertainty.")
        queued = self.queue()
        packet = self.r.work_summary_packet(self.p, queued["problem_id"])
        self.assertEqual(packet["scope"], "conversation")
        self.assertEqual({event["task_binding_known"] for event in packet["events"]}, {True, False})
        reviewed = self.run_review(queued)
        row = self.r.db.execute("SELECT body FROM work_memories WHERE id=?", (reviewed["memory_id"],)).fetchone()
        self.assertEqual(json.loads(row["body"])["scope"], "conversation")
        self.assertEqual(json.loads(row["body"])["coverage"]["scope"], "conversation")

    def test_known_task_sources_keep_their_original_task_scope(self):
        self.sources()
        queued = self.queue()
        packet = self.r.work_summary_packet(self.p, queued["problem_id"])
        self.assertEqual(packet["scope"], "task")
        self.assertIn("animation mapping", packet["problem"]["summary"])
        reviewed = self.run_review(queued)
        row = self.r.db.execute("SELECT task_id,task_revision,json_extract(body,'$.scope') FROM work_memories WHERE id=?",
                                (reviewed["memory_id"],)).fetchone()
        self.assertEqual(tuple(row), (self.task["id"], self.task["revision"], "task"))

    def test_legacy_window_title_does_not_force_unbound_sources_into_its_task(self):
        self.unknown_source("Older project conventions were discussed without a confirmed task binding.")
        self.unknown_source("A later public note retained limitations about deployment verification.")
        queued = self.queue()
        self.r.db.execute("UPDATE problems SET summary=? WHERE id=?",
                          ("工作历史摘要（reported，非正式知识）：animation mapping", queued["problem_id"]))
        packet = self.r.work_summary_packet(self.p, queued["problem_id"])
        self.assertEqual(packet["scope"], "conversation")
        self.assertNotIn("animation mapping", packet["problem"]["summary"])


if __name__ == "__main__":
    unittest.main()
