"""Operator controls and provenance protect references without provider calls."""
import copy
import json
import unittest
from unittest.mock import patch

from agent_learning import candidate_references as refs
from agent_learning.admin_control import control_status
from agent_learning.admin_publication import publication_gate
from agent_learning.admin_response_control import apply_response_control
from agent_learning.candidate_reference_control import apply_reference_control
from agent_learning.runtime import ContractError, Runtime
from tests import test_admin_control as control_fixtures
from tests import test_admin_response_store as response_fixtures
from tests import test_candidate_references as reference_fixtures
from tests import test_history_import as history_fixtures


class ReferenceControlsHttpTests(unittest.TestCase):
    setUp = control_fixtures.AdminControlHttpTests.setUp
    body = control_fixtures.AdminControlHttpTests.body
    stop = control_fixtures.AdminControlHttpTests.stop
    request = control_fixtures.AdminControlHttpTests.request

    def payload(self, **extra):
        return {"request_id": "reference-control-1", "expected_version": 0, "enabled": True,
                "expected_session_epoch": self.r.session_epoch(), "expected_governance_epoch": self.r.host_epoch(), **extra}

    def test_reference_control_is_separate_idempotent_and_does_not_wake(self):
        before = self.r.admin_routing_status(self.p)
        with patch("subprocess.Popen") as launch:
            code, answer = self.request("/api/admin-control/candidate-reference", self.payload())
            self.assertEqual(code, 200, answer)
            self.assertTrue(answer["settings"]["candidate_reference_policy"]["enabled"])
            self.assertFalse(answer["settings"]["enabled"])
            self.assertEqual(self.request("/api/admin-control/candidate-reference", self.payload()), (code, answer))
            code, error = self.request("/api/admin-control/candidate-reference", self.payload(enabled=False))
            self.assertEqual(code, 400, error)
            launch.assert_not_called()
        self.assertEqual(self.r.admin_routing_status(self.p), before)

    def test_reference_controls_require_operator_capability_origin_and_epoch(self):
        for headers in ({"X-Admin-Token": None}, {"Origin": "https://untrusted.example"}):
            self.assertEqual(self.request("/api/admin-control/candidate-reference", self.payload(), **headers)[0], 403)
        code, error = self.request("/api/admin-control/candidate-reference", self.payload(expected_session_epoch=1000000))
        self.assertEqual(code, 400, error)
        self.assertFalse(refs.policy(self.r, self.p)["enabled"])

    def test_response_recheck_route_cannot_launch_or_invent_old_reply(self):
        body = {"request_id": "no-public-reply", "call_id": "missing", "expected_version": 1,
                "expected_session_epoch": self.r.session_epoch(), "expected_governance_epoch": self.r.host_epoch(), "confirmed": True}
        with patch("subprocess.Popen") as launch:
            self.assertEqual(self.request("/api/admin-control/response-recheck", body)[0], 400)
            self.assertEqual(self.request("/api/admin-control/response-recheck", body, **{"X-Admin-Token": None})[0], 403)
            launch.assert_not_called()


class ReferenceProvenanceTests(unittest.TestCase):
    setUp = reference_fixtures.CandidateReferenceTests.setUp
    candidate = reference_fixtures.CandidateReferenceTests.candidate
    enable = reference_fixtures.CandidateReferenceTests.enable

    def test_current_source_conversation_is_not_fed_its_own_pending_candidate(self):
        identity, _, _, _, _ = self.candidate(conversation="consumer")
        self.enable()
        self.assertEqual(refs.search(self.r, self.p, "Python memory leak", context_handle=self.handle), [])
        self.assertTrue(refs.assess(self.r, self.p, identity)["eligible"])

    def test_candidate_file_change_stops_automatic_and_manual_reference(self):
        identity, _, problem, _, _ = self.candidate()
        self.enable()
        item = refs.assess(self.r, self.p, identity)
        with patch("agent_learning.work_files.knowledge_file_state", return_value={"source_files_changed": True}):
            self.assertEqual(refs.search(self.r, self.p, "Python memory leak", context_handle=self.handle, workspace=self.workspace), [])
            with self.assertRaises(ContractError):
                refs.read(self.r, self.p, self.handle, identity, item["reference_version"], workspace=self.workspace)

    def test_public_restatement_is_not_independent_source_for_auto_publication(self):
        identity, _, _, _, _ = self.candidate()
        self.enable()
        item = refs.assess(self.r, self.p, identity)
        refs.record_exposure(self.r, self.p, self.handle, item)
        event, _ = self.r._event(self.p, "consumer", "Stop", {"last_assistant_message": "Restated candidate clue"},
            task_id=self.target["id"], task_revision=1, outcome="reported")
        refs.record_possible_influence(self.r, self.p, event["id"])
        problem = self.r._problem(self.p, "consumer", "Restated project fact", "project", [event["id"]])
        body = {"title": "New source claim", "content": "A restatement is not independent proof", "category": "project",
            "knowledge_kind": "fact", "validation_level": "reported", "applicability": "Same project", "limitations": "Reported",
            "problem_id": problem["id"], "problem_revision": 1, "evidence_refs": [event["id"]]}
        packet = self.r.review_packet(self.p, problem["id"])
        self.assertIn("candidate_reference_provenance", packet["events"][0])
        reason = publication_gate(self.r, self.p, packet, body, {"action": "publish", "knowledge_id": None}, mode="tiered")
        self.assertEqual(reason, "candidate reference requires independent source evidence")
        self.r.db.execute("UPDATE candidates SET status='rejected' WHERE id=?", (identity,))
        self.assertTrue(refs.lineage_risk(self.r, self.p, event["id"])["blocked"])

    def test_schema20_upgrade_is_opt_in_and_preserves_existing_candidate(self):
        identity, _, _, _, _ = self.candidate()
        before = self.r.db.execute("SELECT body,status FROM candidates WHERE id=?", (identity,)).fetchone()
        self.r.db.execute("PRAGMA user_version=20")
        with patch("subprocess.Popen") as launch:
            upgraded = Runtime(self.r.home, config=copy.deepcopy(self.r.config))
            self.addCleanup(upgraded.close)
            launch.assert_not_called()
        self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 23)
        self.assertFalse(refs.policy(upgraded, self.p)["enabled"])
        self.assertEqual(upgraded.db.execute("SELECT body,status FROM candidates WHERE id=?", (identity,)).fetchone(), before)

    def test_concern_cannot_be_bypassed_by_equivalent_candidate_and_explicit_resolution_is_separate(self):
        identity, body, _, _, _ = self.candidate()
        self.enable()
        item = refs.assess(self.r, self.p, identity)
        refs.record_exposure(self.r, self.p, self.handle, item)
        refs.feedback(self.r, self.p, self.handle, identity, item["reference_version"], summary="Check this reported claim",
            idempotency_key="concern-resolution", expected_task_id=self.target["id"], expected_task_revision=self.target["revision"])
        alternative, other_body, _, _, _ = self.candidate(conversation="new-source")
        self.assertEqual(refs.assess(self.r, self.p, alternative)["reason"], "equivalent_candidate_disputed")
        self.assertEqual(refs.held_equivalent(self.r, self.p, other_body), "equivalent candidate has unresolved concerns")
        current = refs.feedback_state(self.r, self.p, identity)
        with self.assertRaises(ContractError):
            refs.resolve_feedback(self.r, self.p, identity, "0" * 64, "Checked sources")
        with patch("subprocess.Popen") as launch:
            refs.resolve_feedback(self.r, self.p, identity, current["version"], "Compared the exact cited source; keep reported")
            launch.assert_not_called()
        self.assertTrue(refs.assess(self.r, self.p, identity)["eligible"])
        self.assertIsNone(refs.held_equivalent(self.r, self.p, other_body))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)


class HistoricalReferenceAccessTests(unittest.TestCase):
    setUp = history_fixtures.HistoryImportTests.setUp
    prepare = history_fixtures.HistoryImportTests.prepare
    materialize = history_fixtures.HistoryImportTests.materialize
    case = history_fixtures.HistoryImportTests.case
    candidate = history_fixtures.HistoryImportTests.candidate

    def test_frozen_batch_grant_does_not_start_review_and_budget_pause_does_not_revoke_it(self):
        job = self.materialize()
        identity = self.candidate(job)["candidate_id"]
        refs.configure(self.r, self.p, True, 0)
        self.assertEqual(refs.assess(self.r, self.p, identity)["reason"], "history_reference_not_authorized")
        with patch("subprocess.Popen") as launch:
            grant = refs.grant_history(self.r, self.p, job, True, 0)
            launch.assert_not_called()
        self.assertTrue(refs.assess(self.r, self.p, identity)["eligible"])
        self.r.db.execute("UPDATE history_jobs SET state='paused',last_result=? WHERE id=?",
                          (json.dumps({"reason": "review_budget_exhausted"}), job))
        self.assertTrue(refs.assess(self.r, self.p, identity)["eligible"])
        refs.grant_history(self.r, self.p, job, False, grant["version"])
        self.assertFalse(refs.assess(self.r, self.p, identity)["eligible"])
        refs.grant_history(self.r, self.p, job, True, grant["version"] + 1)
        self.r.db.execute("UPDATE history_jobs SET state='cancelled' WHERE id=?", (job,))
        self.assertFalse(refs.assess(self.r, self.p, identity)["eligible"])

    def test_account_boundary_requires_explicit_historical_reference_reconfirmation(self):
        job = self.materialize()
        refs.configure(self.r, self.p, True, 0)
        refs.grant_history(self.r, self.p, job, True, 0)
        self.r.account_changed()
        self.assertFalse(refs.history_grant(self.r, self.p, job)["enabled"])
        refs.grant_history(self.r, self.p, job, True, 1)
        self.assertTrue(refs.history_grant(self.r, self.p, job)["enabled"])


class ResponseControlTests(unittest.TestCase):
    setUp = response_fixtures.AdminResponseStoreTests.setUp
    phase = response_fixtures.AdminResponseStoreTests.phase
    pending = response_fixtures.AdminResponseStoreTests.pending
    failed = response_fixtures.AdminResponseStoreTests.failed

    def test_control_rechecks_without_model_and_preserves_paid_accounting(self):
        from agent_learning.admin_response_store import status
        identity = self.failed()
        before = tuple(self.r.db.execute("SELECT state,usage,retry_allowed FROM admin_calls WHERE id=?", (identity,)).fetchone())
        body = {"request_id": "response-control", "call_id": identity, "expected_version": status(self.r, self.p, identity)["version"],
                "expected_session_epoch": self.r.session_epoch(), "expected_governance_epoch": self.r.host_epoch(), "confirmed": True}
        with patch("subprocess.Popen") as launch:
            answer = apply_response_control(self.r, self.p, body)
            again = apply_response_control(self.r, self.p, body)
            launch.assert_not_called()
        self.assertEqual(answer["status"], "accepted", answer)
        self.assertEqual(again["result"], answer["result"])
        self.assertEqual(tuple(self.r.db.execute("SELECT state,usage,retry_allowed FROM admin_calls WHERE id=?", (identity,)).fetchone()), before)
