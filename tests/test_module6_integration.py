"""管理员对外接口及接收边界的隔离验证，不调用真实模型。"""
import contextlib
import io
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_batch, admin_once
from agent_learning.admin_decisions import get_reviews
from agent_learning.admin_lifecycle import retry_call
from agent_learning.admin_queue import claim_dispatch
from agent_learning.cli import main
from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime
from tests.test_module6_queue import fake_reviewer


class Module6IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-module6-entry-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "state"
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.home)
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.task = self.r.task_update(self.p, "chat", "compilerprobe")
        self.r.config["admin"].update(enabled=True, command=[sys.executable], max_calls_per_day=20)

    def problem(self, summary="compilerprobe", **values):
        return self.r.feedback(self.p, "chat", summary, category=values.pop("category", "operation"), **values)

    def candidate(self, problem, **values):
        refs = [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        body = dict(problem_id=problem["id"], problem_revision=problem["revision"], title="compilerprobe",
                    content="compilerprobe", category="operation", applicability="local compiler",
                    limitations="observed only", evidence_refs=refs, validation_level="reported")
        body.update(values)
        return self.r.submit_candidate(self.p, **body)

    def cli(self, *args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = main(["--home", str(self.home), "--workspace", str(self.workspace), *args])
        self.assertEqual(code, 0)
        return json.loads(stream.getvalue())

    def test_admin_mcp_proposes_but_cannot_execute(self):
        problem = self.problem()
        candidate = self.candidate(problem)
        args = dict(problem_id=problem["id"], problem_revision=problem["revision"], action="publish",
                    candidate_id=candidate["candidate_id"], reason="recorded observation")
        worker, admin = Server(self.r, self.p), Server(self.r, self.p, "admin")
        with self.assertRaises(ContractError):
            worker.call("propose_governance", args)
        proposal = admin.call("propose_governance", args)
        with self.assertRaises(ContractError):
            admin.call("governance_execute", {"proposal_id": proposal["proposal_id"]})
        self.assertEqual(self.r.status(self.p)["counts"]["active"], 0)
        result = self.cli("governance-execute", proposal["proposal_id"])
        self.assertEqual(result["state"], "executed")
        self.assertEqual(result, self.cli("governance-execute", proposal["proposal_id"]))

    def test_needs_evidence_reason_survives_restart_and_does_not_repeat(self):
        problem = self.problem()
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("needs_evidence")) as model:
            result = admin_once(self.r, self.p)
            self.assertEqual(result["status"], "needs_evidence")
            admin_batch(self.r, self.p)
            self.assertEqual(model.call_count, 1)
        receipt = self.cli("reviews", "--problem", problem["id"])["items"][0]
        self.assertEqual(receipt["state"], "awaiting_evidence")
        self.assertTrue(receipt["reason"])
        self.assertTrue(receipt["missing_evidence"])

    def test_new_candidate_wakes_compare_but_unrelated_candidate_costs_no_model(self):
        problem = self.problem()
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("needs_evidence")):
            admin_once(self.r, self.p)
        unrelated = self.problem("unrelatedxy")
        self.candidate(unrelated, title="unrelatedxy", content="unrelatedxy")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            self.assertTrue(claim_dispatch(self.r, self.p))
            admin_once(self.r, self.p, problem["id"])
            self.assertEqual(model.call_count, 0)
        self.candidate(unrelated, title="compilerprobe", content="compilerprobe alternative")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            result = admin_once(self.r, self.p, problem["id"])
            self.assertEqual(result["status"], "skip")
            self.assertEqual(model.call_count, 1)

    def test_review_context_changed_during_model_rejects_even_skip(self):
        source = self.problem("source")
        knowledge = self.r.publish(self.p, self.candidate(source)["candidate_id"])
        problem = self.problem()
        def revoke(packet, reviewer):
            self.assertTrue(packet["related_knowledge"])
            self.r.revoke(self.p, knowledge["knowledge_id"], expected_revision=self.r.read(self.p, knowledge["knowledge_id"])["revision"])
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("skip", revoke)):
            result = admin_once(self.r, self.p, problem["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(get_reviews(self.r, self.p, problem["id"])["items"], [])

    def test_unknown_cross_case_cache_wakes_after_explicit_retry(self):
        original = self.problem()
        def fail(packet, reviewer):
            reviewer.returncode = 2
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("skip", fail)):
            failed = admin_once(self.r, self.p, original["id"])
        self.r.task_update(self.p, "other", "compilerprobe")
        other = self.r.feedback(self.p, "other", "compilerprobe", category="operation")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            self.assertEqual(admin_once(self.r, self.p, other["id"])["status"], "review_required")
            self.assertEqual(model.call_count, 0)
            retry_call(self.r, self.p, failed["call_id"], "reviewed failure; allow one retry")
            self.assertIsNone(self.r.db.execute("SELECT 1 FROM admin_review_checks WHERE problem=?", (other["id"],)).fetchone())
            self.assertEqual(admin_once(self.r, self.p, original["id"])["status"], "skip")
            result = admin_once(self.r, self.p, other["id"])
            self.assertEqual(result["status"], "skip")
            self.assertEqual(model.call_count, 1)

    def test_auto_publish_restricts_professional_and_pattern(self):
        self.r.config["admin"]["auto_publish"] = True
        for category, kind in (("professional", "solution"), ("operation", "pattern")):
            problem = self.problem(category + kind, category=category)
            event = self.r.record_event(self.p, "chat", "PostToolUse", {"exit_code": 0}, outcome="succeeded",
                                        task_id=self.task["id"], task_revision=self.task["revision"])
            problem = self.problem(category + kind, category=category, event_refs=[event["id"]])
            base = fake_reviewer("candidate")
            class Reviewer(base):
                def communicate(self, data=None, timeout=None):
                    super().communicate(data, timeout)
                    body = json.loads(self.output.read_text(encoding="utf-8"))
                    body["candidate"].update(validation_level="tool_success", knowledge_kind=kind)
                    self.output.write_text(json.dumps(body), encoding="utf-8")
            with patch("agent_learning.admin.subprocess.Popen", Reviewer):
                result = admin_once(self.r, self.p, problem["id"])
            self.assertEqual(result["status"], "candidate")
            self.assertEqual(result["publication"]["reason"], "requires operation solution")
        self.assertEqual(self.r.status(self.p)["counts"]["active"], 0)

    def test_auto_publish_operation_solution_has_published_receipt(self):
        self.r.config["admin"]["auto_publish"] = True
        event = self.r.record_event(self.p, "chat", "PostToolUse", {"exit_code": 0}, outcome="succeeded",
                                    task_id=self.task["id"], task_revision=self.task["revision"])
        problem = self.problem(event_refs=[event["id"]])
        base = fake_reviewer("candidate")
        class Reviewer(base):
            def communicate(self, data=None, timeout=None):
                super().communicate(data, timeout)
                body = json.loads(self.output.read_text(encoding="utf-8"))
                body["candidate"]["validation_level"] = "tool_success"
                self.output.write_text(json.dumps(body), encoding="utf-8")
        with patch("agent_learning.admin.subprocess.Popen", Reviewer):
            result = admin_once(self.r, self.p, problem["id"])
        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(get_reviews(self.r, self.p, problem["id"])["items"][0]["state"], "published")
        self.assertEqual(self.r.status(self.p)["governance_states"], {"executed": 1})

    def test_model_revoke_is_only_a_persisted_proposal(self):
        source = self.problem("source")
        knowledge = self.r.publish(self.p, self.candidate(source)["candidate_id"])
        problem = self.problem("counterexample", knowledge_id=knowledge["knowledge_id"], knowledge_revision=knowledge["revision"])
        base = fake_reviewer("governance")
        class Reviewer(base):
            def communicate(self, data=None, timeout=None):
                super().communicate(data, timeout)
                body = json.loads(self.output.read_text(encoding="utf-8"))
                body["proposal"] = dict(action="revoke", knowledge_id=knowledge["knowledge_id"], expected_revision=knowledge["revision"])
                self.output.write_text(json.dumps(body), encoding="utf-8")
        with patch("agent_learning.admin.subprocess.Popen", Reviewer):
            result = admin_once(self.r, self.p, problem["id"])
        self.assertEqual(result["status"], "governance")
        self.assertEqual(result["state"], "proposed")
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["pending_counterexample_count"], 1)
        self.assertEqual(get_reviews(self.r, self.p, problem["id"])["items"][0]["state"], "proposal_ready")

    def test_migration_error_closes_connection_and_preserves_retry(self):
        home = Path(self.temp.name) / "failed-migration"
        with patch("agent_learning.admin_governance.install_governance", side_effect=RuntimeError("migration failure")):
            with self.assertRaises(RuntimeError):
                Runtime(home)
        with contextlib.closing(Runtime(home)) as recovered:
            self.assertEqual(recovered.db.execute("PRAGMA user_version").fetchone()[0], 15)

    def test_duplicate_origin_new_evidence_invalidates_without_losing_history(self):
        original = self.problem()
        self.r.task_update(self.p, "other", "compilerprobe")
        duplicate = self.r.feedback(self.p, "other", "compilerprobe", category="operation")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            admin_once(self.r, self.p, original["id"])
            reused = admin_once(self.r, self.p, duplicate["id"])
            self.assertTrue(reused["duplicate_of"])
            self.assertEqual(model.call_count, 1)
            self.problem(resolution="new contradictory observation")
            self.assertTrue(self.r.review_packet(self.p, duplicate["id"])["review_context_revision"])
            fresh = admin_once(self.r, self.p, duplicate["id"])
            self.assertEqual(fresh["status"], "skip")
            self.assertEqual(model.call_count, 2)
        self.assertEqual(len(get_reviews(self.r, self.p, duplicate["id"])["items"]), 2)


if __name__ == "__main__":
    unittest.main()
