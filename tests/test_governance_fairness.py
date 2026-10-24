"""Bounded routing, shared budgets and publication; isolated deterministic hosts."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import _select_review
from agent_learning.admin_lifecycle import claim_call, finish_call, mark_launch_intent
from agent_learning.admin_publication import publication_gate
from agent_learning.admin_queue import (claim_dispatch, dispatch_diagnostic, eligible_query,
    ready_executor, record_executor_failure, scan_signatures, executor_signature, routing_signature)
from agent_learning.admin_usage import (admin_admission_status, configure_project_budget,
    pending_phase_reservation, project_budget_policy)
from agent_learning.phase_review import configure, policy
from agent_learning.runtime import ContractError, Runtime
from agent_learning.scheduler import SCHEDULER_DEFAULTS
from tests import test_phase_review as phase_fixtures
from tests import test_admin_review_flow as flow_fixtures


class ExecutorFairnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="governance-fairness-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.config = {"admin": {"enabled": True, "command": [sys.executable], "model_mode": "current"}}
        self.r = Runtime(self.root / "home", config=self.config)
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)

    def cases(self, blocked=40):
        for number in range(blocked + 1):
            conversation = "blocked-" + str(number) if number < blocked else "ready"
            self.r.admin_observe_model(self.p, conversation, "old-model" if number < blocked else "ready-model")
            problem = self.r.feedback(self.p, conversation, "Independent observation " + str(number))
            self.r.db.execute("UPDATE problems SET created=? WHERE id=?", (number + 1, problem["id"]))
        self.plan = self.r.resolve_admin_executor(self.p, "blocked-0")
        record_executor_failure(self.r, self.p, self.plan, "configuration_change_required")

    def select(self, limit=10):
        with self.r.transaction():
            return _select_review(self.r, self.p, None, limit, 120, self.r.host_epoch())

    def test_legacy_dispatch_and_selection_reach_ready_executor_after_forty_blocked(self):
        self.cases()
        with patch("agent_learning.admin_providers.executor_availability", return_value={"cli_available": True}):
            token = claim_dispatch(self.r, self.p)
            self.assertIsNotNone(token)
            result = self.select()
        self.assertEqual(result["status"], "reserved")
        self.assertEqual(result["selected"]["conversation"], "ready")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM problems WHERE status='pending'").fetchone()[0], 41)

    def test_routing_progress_survives_reopen_and_candidate_generation(self):
        self.cases(blocked=11)
        with patch("agent_learning.admin_providers.executor_availability", return_value={"cli_available": True}):
            self.assertEqual(self.select()["status"], "executor_not_ready")
            self.r.db.execute("INSERT INTO admin_queue_epochs(project,candidates) VALUES(?,1) "
                "ON CONFLICT(project) DO UPDATE SET candidates=candidates+1", (self.p,))
            self.r.close()
            self.r = Runtime(self.root / "home", config=self.config)
            self.addCleanup(self.r.close)
            result = self.select()
        self.assertEqual(result["status"], "reserved")
        self.assertEqual(result["selected"]["conversation"], "ready")

    def test_recovered_executor_restarts_routing_order_and_unknown_call_stays_locked(self):
        self.cases(blocked=11)
        with patch("agent_learning.admin_providers.executor_availability", return_value={"cli_available": True}):
            self.assertEqual(self.select()["status"], "executor_not_ready")
            self.r.db.execute("DELETE FROM admin_executor_failures")
            result = self.select()
            self.assertTrue(result["selected"]["conversation"].startswith("blocked-"))
            mark_launch_intent(self.r, result["call_id"], result["owner"], self.r.host_epoch())
            finish_call(self.r, result["call_id"], "timeout_unknown")
            self.r.db.execute("DELETE FROM admin_review_checks")
            next_result = self.select()
        self.assertNotEqual(result["selected"]["id"], next_result["selected"]["id"])
        old = self.r.db.execute("SELECT state,retry_allowed FROM admin_calls WHERE id=?", (result["call_id"],)).fetchone()
        self.assertEqual(tuple(old), ("timeout_unknown", 0))

    def test_readonly_route_checks_do_not_advance_scan(self):
        self.cases()
        signature, preflight = scan_signatures(self.r, self.p)
        query, args = eligible_query(signature, preflight_signature=preflight)
        before = self.r.db.total_changes
        with patch("agent_learning.admin_providers.executor_availability", return_value={"cli_available": True}):
            self.assertFalse(ready_executor(self.r, self.p, query, args))
            self.assertEqual(dispatch_diagnostic(self.r, self.p)["reason"], "configuration_change_required")
        self.assertEqual(self.r.db.total_changes, before)

    def test_larger_backlog_continues_after_a_bounded_route_scan(self):
        self.cases(blocked=130)
        self.r.config["scheduler"].update(SCHEDULER_DEFAULTS, enabled=True, poll_seconds=1)
        with patch("agent_learning.admin_providers.executor_availability", return_value={"cli_available": True}):
            with self.r.transaction():
                first = self.r._scheduler_admin_state(self.p, self.r._scheduler_config())
            self.assertEqual((first[1], first[3]), ("waiting", "executor_scan_pending"))
            self.assertIsNotNone(first[2])
            self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_review_checks WHERE blocked_call='executor_scan'").fetchone()[0], 120)
            with self.r.transaction():
                second = self.r._scheduler_admin_state(self.p, self.r._scheduler_config())
        self.assertEqual(second[1], "ready")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_expired_backoff_restarts_routing_order_without_an_explicit_retry(self):
        self.cases(blocked=11)
        record_executor_failure(self.r, self.p, self.plan, "executor_launch_failed")
        with patch("agent_learning.admin_providers.executor_availability", return_value={"cli_available": True}):
            self.assertEqual(self.select()["status"], "executor_not_ready")
            self.r.db.execute("UPDATE admin_executor_failures SET next_try=0")
            recovered = self.select()
        self.assertEqual(recovered["selected"]["conversation"], "blocked-0")

    def test_three_ready_hosts_rotate_after_the_last_selected_host(self):
        for scope, updated in (("antigravity:default", 2), ("dsh:default", 1)):
            self.r.db.execute("INSERT INTO admin_control_state(project,host_scope,enabled,version,updated) VALUES(?,?,1,1,?)",
                              (self.p, scope, updated))
        self.r.db.execute("INSERT INTO scheduler_jobs(project,kind,state,signature,created,updated) VALUES(?,'admin','ready','probe',0,0)", (self.p,))
        chosen = []
        with patch.object(self.r, "_scheduler_eval_admin_for_scope", return_value=("probe", "ready", 0, "")):
            for _ in range(9):
                result = self.r._scheduler_admin_selection(self.p, dict(SCHEDULER_DEFAULTS, enabled=True))
                chosen.append(result[4])
                self.r.db.execute("UPDATE scheduler_jobs SET summary=? WHERE project=? AND kind='admin'",
                                  (json.dumps({"host_scope": result[4]}), self.p))
        self.assertEqual(chosen, ["codex:default", "dsh:default", "antigravity:default"] * 3)

    def test_three_host_cycle_survives_a_different_waking_host(self):
        for scope in ("codex:default", "antigravity:default", "dsh:default"):
            self.r.db.execute("INSERT INTO admin_control_state(project,host_scope,enabled,version,updated) VALUES(?,?,1,1,1)",
                              (self.p, scope))
        self.r.db.execute("INSERT INTO scheduler_jobs(project,kind,state,signature,created,updated) VALUES(?,'admin','ready','probe',0,0)", (self.p,))
        chosen = []
        with patch.object(self.r, "_scheduler_eval_admin_for_scope", return_value=("probe", "ready", 0, "")):
            for caller in ("codex:default", "antigravity:default", "dsh:default") * 2:
                with self.r._scheduler_host_scope(caller):
                    result = self.r._scheduler_admin_selection(self.p, dict(SCHEDULER_DEFAULTS, enabled=True))
                chosen.append(result[4])
                self.r.db.execute("UPDATE scheduler_jobs SET summary=? WHERE project=? AND kind='admin'",
                                  (json.dumps({"host_scope": result[4]}), self.p))
        self.assertEqual(chosen, ["codex:default", "dsh:default", "antigravity:default"] * 2)


class PhaseAdmissionTests(unittest.TestCase):
    setUp = phase_fixtures.PhaseReviewTests.setUp
    phase = phase_fixtures.PhaseReviewTests.phase

    def setup_pending(self):
        configure(self.r, self.p, {"daily_tokens": 5000}, policy(self.r, self.p)["version"])
        self.phase("one")
        self.phase("two", "Another independent convention")
        self.r.config["scheduler"].update(SCHEDULER_DEFAULTS, enabled=True, poll_seconds=1)

    def test_dynamic_pending_reservation_blocks_dispatch_and_scheduler_without_spinning(self):
        self.setup_pending()
        required = pending_phase_reservation(self.r, self.p)
        self.assertGreater(required, 5000)
        before = self.r.db.total_changes
        admission = admin_admission_status(self.r, self.p)
        self.assertEqual(self.r.db.total_changes, before)
        self.assertEqual(admission["admission_status"], "token_budget_exhausted")
        self.assertEqual(admission["required_token_reservation"], required)
        with patch("agent_learning.admin.admin_batch") as batch, patch("subprocess.Popen") as launch:
            self.assertIsNone(claim_dispatch(self.r, self.p))
            result = self.r.scheduler_run(self.p, max_seconds=7)
        batch.assert_not_called()
        launch.assert_not_called()
        self.assertEqual((result["processed"], result["ticks"]), (0, 1))
        job = self.r.db.execute("SELECT state,wait_reason,next_due FROM scheduler_jobs WHERE project=? AND kind='admin'", (self.p,)).fetchone()
        self.assertEqual((job["state"], job["wait_reason"]), ("waiting", "token_budget_exhausted"))
        self.assertEqual(job["next_due"], admission["next_reset_at"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_budget_increase_wakes_same_frozen_batch_and_legacy_claim_keeps_fixed_reservation(self):
        self.setup_pending()
        problem = self.r.feedback(self.p, "explicit", "Explicit independent case")
        with self.r.transaction():
            call = claim_call(self.r, self.p, problem["id"], problem["revision"], "explicit", self.r.host_epoch(), 5)
        self.assertEqual(call["status"], "reserved")
        finish_call(self.r, call["call_id"], "not_started")
        configure_project_budget(self.r, self.p, {"max_reported_tokens_per_day": 100000},
                                 expected_version=project_budget_policy(self.r, self.p)["version"])
        self.assertEqual(admin_admission_status(self.r, self.p)["admission_status"], "available")
        self.assertEqual(self.r._scheduler_admin_state(self.p, self.r._scheduler_config())[1], "ready")

    def test_explicit_history_request_keeps_its_own_reservation_beside_pending_phase(self):
        from agent_learning.history_review_control import request_history_review
        self.setup_pending()
        body = flow_fixtures.ReviewFlowTests.history(self)
        self.assertEqual(admin_admission_status(self.r, self.p)["admission_status"], "token_budget_exhausted")
        with patch.object(self.r, "_launch_background") as launch:
            result = request_history_review(self.r, self.p, body, details=False)
        self.assertEqual(result["status"], "accepted")
        launch.assert_called_once()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)


class CurrentEquivalentPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="equivalent-publication-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "project").mkdir()
        self.r = Runtime(self.root / "home")
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.root / "project")

    def candidate(self, number, content):
        problem = self.r.feedback(self.p, "source-" + str(number), "Project storage observation " + str(number), category="project")
        refs = [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        body = {"problem_id": problem["id"], "problem_revision": problem["revision"], "title": "Memory storage",
            "content": content, "category": "project", "knowledge_kind": "fact", "metadata": {},
            "applicability": "Current project", "limitations": "Reported project source only",
            "validation_level": "reported", "evidence_refs": refs}
        return self.r.submit_candidate(self.p, **body)["candidate_id"], body, problem

    def gate(self, body, problem):
        return publication_gate(self.r, self.p, self.r.review_packet(self.p, problem["id"]), body,
            {"action": "publish", "knowledge_id": None, "expected_revision": None}, mode="tiered")

    def revised(self):
        first, _, _ = self.candidate(1, "SQLite")
        knowledge = self.r.publish(self.p, first)
        second, _, _ = self.candidate(2, "Other store")
        self.r.publish(self.p, second, knowledge_id=knowledge["knowledge_id"], expected_revision=1)
        return knowledge

    def test_return_to_current_equivalent_revision_can_attach_automatically(self):
        knowledge = self.revised()
        third, _, _ = self.candidate(3, "SQLite")
        self.r.publish(self.p, third, knowledge_id=knowledge["knowledge_id"], expected_revision=2)
        fresh, body, problem = self.candidate(4, "SQLite")
        self.assertIsNone(self.gate(body, problem))
        result = self.r.publish(self.p, fresh)
        self.assertTrue(result["deduplicated"])
        self.assertEqual((result["knowledge_id"], result["revision"]), (knowledge["knowledge_id"], 3))

    def test_retired_body_and_revocation_still_block_automatic_publication(self):
        knowledge = self.revised()
        _, body, problem = self.candidate(3, "SQLite")
        self.assertEqual(self.gate(body, problem), "equivalent knowledge was superseded")
        self.r.revoke(self.p, knowledge["knowledge_id"], 2)
        self.assertEqual(self.gate(body, problem), "equivalent knowledge was revoked")

    def test_current_equivalent_claim_with_counterexample_still_requires_governance(self):
        knowledge = self.revised()
        third, _, _ = self.candidate(3, "SQLite")
        self.r.publish(self.p, third, knowledge_id=knowledge["knowledge_id"], expected_revision=2)
        self.r.feedback(self.p, "counterexample", "Observed contradiction", category="project",
            knowledge_id=knowledge["knowledge_id"], knowledge_revision=3, feedback_kind="counterexample")
        _, body, problem = self.candidate(4, "SQLite")
        self.assertEqual(self.gate(body, problem), "unresolved knowledge counterexample")


class ExecutorSignatureCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="executor-signature-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        from agent_learning.dsh_profile import ENVIRONMENT
        self.environment = patch.dict(os.environ, {ENVIRONMENT: "", "DSH_HOME": str(self.root / "dsh")})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_incomplete_sdk_metadata_is_signable_and_completion_invalidates_signature(self):
        plan = {"provider": "dsh", "command": [sys.executable], "model": "shared-model", "session_epoch": 0}
        with patch("agent_learning.admin_providers.dsh_sdk_launcher", return_value={"args": [sys.executable]}):
            missing_anchor = executor_signature(plan)
        with patch("agent_learning.admin_providers.dsh_sdk_launcher", return_value={"adapter": "pending"}):
            missing_args = executor_signature(plan)
        anchor = self.root / "package" / "bin" / "runner.mjs"
        anchor.parent.mkdir(parents=True)
        anchor.write_text("// deterministic adapter placeholder", encoding="utf-8")
        manifest = anchor.parent.parent / "package.json"
        manifest.write_text('{"version":"1"}', encoding="utf-8")
        with patch("agent_learning.admin_providers.dsh_sdk_launcher", return_value={"args": [sys.executable], "anchor": str(anchor)}):
            ready_metadata = executor_signature(plan)
            self.assertNotEqual(missing_anchor, ready_metadata)
            self.assertNotEqual(missing_args, ready_metadata)
            manifest.write_text('{"version":"2-longer"}', encoding="utf-8")
            self.assertNotEqual(ready_metadata, executor_signature(plan))

    def test_profile_identity_still_invalidates_partial_sdk_signature(self):
        plan = {"provider": "dsh", "command": [sys.executable], "model": "same-model", "session_epoch": 0,
                "dsh_profile": {"source": "host_snapshot", "profile_directory": str(self.root / "profiles" / "one"),
                                "sha256": "a" * 64}}
        changed = dict(plan, dsh_profile=dict(plan["dsh_profile"], sha256="b" * 64))
        with patch("agent_learning.admin_providers.dsh_sdk_launcher", return_value={"args": [sys.executable]}):
            self.assertNotEqual(executor_signature(plan), executor_signature(changed))

    def test_routing_signatures_survive_pending_sdk_and_follow_each_host_configuration(self):
        workspace = self.root / "project"
        workspace.mkdir()
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                runtime = Runtime(self.root / host, host=host, config={"admin": {"enabled": True,
                    "command": [sys.executable], "model_mode": "fixed", "model": "shared-model"}})
                self.addCleanup(runtime.close)
                project = runtime.project(workspace)
                with patch("agent_learning.admin_providers.dsh_sdk_launcher", return_value={"args": [sys.executable]}):
                    before = routing_signature(runtime, project)
                    runtime.config["admin"]["model"] = "configured-other-model"
                    self.assertNotEqual(before, routing_signature(runtime, project))
                    if host == "dsh":
                        with self.assertRaisesRegex(ContractError, "dsh_profile_unavailable"):
                            runtime.resolve_admin_executor(project)
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
