"""审查后的账户、全量证据及计量回归；隔离 SQLite 与内存审核器。"""
import contextlib
import copy
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import _fingerprint, _finish, _usage, admin_batch, admin_once
from agent_learning.admin_decisions import review_signature
from agent_learning.admin_lifecycle import claim_call, finish_call, mark_launch_intent, mark_started
from agent_learning.admin_usage import admin_budget_status
from agent_learning.event_capture import business_evidence_digest
from agent_learning.runtime import ContractError, Runtime
from tests.test_module6_queue import fake_reviewer


class AdminAuditRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-admin-audit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "state", config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.config["admin"].update(enabled=True, command=[sys.executable], max_calls_per_day=10,
                                      max_concurrent_calls=1, token_reservation_per_call=4000)
        self.r.task_update(self.p, "chat", "Investigate supplied observations")

    def problem(self):
        return self.r.feedback(self.p, "chat", "Compiler failure", category="operation")

    def start_call(self):
        with self.r.transaction():
            call = claim_call(self.r, self.p, "accounting", 1, "signature", self.r.host_epoch(), 10)
        mark_launch_intent(self.r, call["call_id"], call["owner"], call["host_epoch"])
        mark_started(self.r, call["call_id"], call["owner"])
        return call

    def log(self, events):
        path = self.root / "usage.log"
        path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        return path

    def publish(self):
        problem = self.problem()
        packet = self.r.review_packet(self.p, problem["id"])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="Compiler observation", content="Check supplied environment", category="operation",
            applicability="Same supplied observation", limitations="Reported only",
            evidence_refs=[event["id"] for event in packet["events"]], validation_level="reported")
        return self.r.publish(self.p, candidate["candidate_id"])

    def swap_before_next_transaction(self):
        original, pending = self.r.transaction, [True]

        @contextlib.contextmanager
        def transaction():
            if pending[0]:
                pending[0] = False
                self.r.account_changed("transaction-boundary")
            with original():
                yield

        return patch.object(self.r, "transaction", transaction)

    def assert_no_admin_work(self):
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)

    def test_scheduler_cannot_recapture_new_account_at_admin_entry(self):
        self.problem()
        self.r.config["scheduler"].update(enabled=True)

        def changed(runtime, project, **kwargs):
            self.assertEqual(kwargs["expected_epoch"], 0)
            runtime.account_changed("scheduler-entry")
            return admin_batch(runtime, project, **kwargs)

        with patch("agent_learning.admin.admin_batch", side_effect=changed), patch(
                "agent_learning.admin.subprocess.Popen") as process, self.assertRaises(ContractError):
            self.r.scheduler_tick(self.p)
        process.assert_not_called()
        self.assert_no_admin_work()

    def test_batch_cannot_recapture_new_account_at_once_entry(self):
        self.problem()

        def changed(runtime, project, **kwargs):
            self.assertEqual(kwargs["expected_epoch"], 0)
            runtime.account_changed("batch-entry")
            return admin_once(runtime, project, **kwargs)

        with patch("agent_learning.admin.admin_once", side_effect=changed), patch(
                "agent_learning.admin.subprocess.Popen") as process, self.assertRaises(ContractError):
            admin_batch(self.r, self.p)
        process.assert_not_called()
        self.assert_no_admin_work()

    def test_once_rechecks_account_inside_selection_transaction(self):
        self.problem()
        with self.swap_before_next_transaction(), patch("agent_learning.admin.subprocess.Popen") as process:
            with self.assertRaises(ContractError):
                admin_once(self.r, self.p)
        process.assert_not_called()
        self.assert_no_admin_work()

    def test_disabled_admin_still_rejects_stale_explicit_account(self):
        self.r.config["admin"]["enabled"] = False
        self.r.account_changed("disabled-entry")
        for action in (admin_once, admin_batch):
            with self.subTest(action=action.__name__), self.assertRaises(ContractError):
                action(self.r, self.p, expected_epoch=0)

    def test_sync_batch_cannot_recapture_account_at_once_entry(self):
        self.r.config["hindsight"]["enabled"] = True
        self.publish()
        original = self.r.sync_once

        def changed(project, **kwargs):
            self.assertEqual(kwargs["expected_epoch"], 0)
            self.r.account_changed("sync-batch-entry")
            return original(project, **kwargs)

        with patch.object(self.r, "sync_once", side_effect=changed), patch(
                "agent_learning.hindsight.HindsightClient.retain") as retain, self.assertRaises(ContractError):
            self.r.sync_batch(self.p)
        retain.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT state FROM outbox").fetchone()[0], "pending")

    def test_sync_rechecks_account_inside_claim_transaction(self):
        self.r.config["hindsight"]["enabled"] = True
        self.publish()
        with self.swap_before_next_transaction(), patch("agent_learning.hindsight.HindsightClient.retain") as retain:
            with self.assertRaises(ContractError):
                self.r.sync_once(self.p)
        retain.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT state FROM outbox").fetchone()[0], "pending")

    def test_sync_confirmation_after_account_change_is_only_late_evidence(self):
        self.r.config["hindsight"]["enabled"] = True
        self.publish()

        def late_confirmation(*args, **kwargs):
            self.r.account_changed("sync-response-boundary")
            return {"success": True, "async": False, "items_count": 1}

        with patch("agent_learning.hindsight.HindsightClient.retain", side_effect=late_confirmation):
            result = self.r.sync_once(self.p)
        self.assertEqual(result["status"], "reconnect_required")
        row = self.r.db.execute("SELECT state,write_outcome,attempts FROM outbox").fetchone()
        self.assertEqual(tuple(row), ("blocked", "unknown", 1))
        self.assertEqual(self.r.db.execute("SELECT state FROM sync_receipts").fetchone()[0], "late_synced")

    def test_budget_only_changes_do_not_change_review_signature(self):
        problem = self.problem()
        packet = self.r.review_packet(self.p, problem["id"])
        evidence = _fingerprint(packet)
        baseline = review_signature(packet, self.r.config["admin"], evidence)
        for key, value in (("max_concurrent_calls", 2), ("max_reported_tokens_per_day", 50000),
                           ("token_reservation_per_call", 8000)):
            cfg = dict(self.r.config["admin"], **{key: value})
            self.assertEqual(baseline, review_signature(packet, cfg, evidence), key)
        self.assertNotEqual(baseline, review_signature(packet, dict(self.r.config["admin"], model="new"), evidence))

    def test_budget_only_change_reuses_completed_review_without_model_call(self):
        self.problem()
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer()) as _:
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        self.r.config["admin"]["max_concurrent_calls"] = 2
        with patch("agent_learning.admin.subprocess.Popen") as process:
            self.assertEqual(admin_once(self.r, self.p)["status"], "scan_complete")
        process.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_unfinished_and_failed_turns_keep_log_usage_partial(self):
        complete = {"type": "turn.completed", "turn_id": "one", "usage": {"input_tokens": 100, "output_tokens": 20}}
        for final in ({"type": "turn.started", "turn_id": "two"},
                      {"type": "turn.failed", "turn_id": "two"},
                      {"type": "turn.cancelled", "turn_id": "two"},
                      {"type": "turn.started"}):
            with self.subTest(final=final):
                usage = _usage(self.log([complete, final]), 0)
                self.assertEqual(usage["token_totals"], {"input_tokens": 100, "output_tokens": 20})
                self.assertEqual(usage["completeness"], "partial")
                self.assertGreater(usage["unreported_turns"], 0)

    def test_started_failed_anonymous_turn_does_not_release_reservation(self):
        call = self.start_call()
        path = self.log([{"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}},
                         {"type": "turn.started"}, {"type": "turn.failed"}])
        usage = _usage(path, 0)
        finish_call(self.r, call["call_id"], "timeout_unknown", usage)
        self.assertEqual(usage["completeness"], "partial")
        budget = admin_budget_status(self.r)
        self.assertEqual(budget["reported_tokens"], 120)
        self.assertEqual(budget["accounted_tokens"], 4000)

    def test_complete_turn_only_log_does_not_settle_unknown_call(self):
        call = self.start_call()
        path = self.log([{"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}}])
        self.assertEqual(_usage(path, 0)["completeness"], "complete")
        usage = _finish(self.r, call["call_id"], "timeout_unknown", path, 0)
        self.assertEqual(usage["completeness"], "partial")
        self.assertEqual(admin_budget_status(self.r)["accounted_tokens"], 4000)

    def test_completed_cycles_and_duplicate_started_events_are_complete(self):
        events = [{"type": "turn.started", "turn_id": "one"}] * 2
        events += [{"type": "turn.completed", "turn_id": "one", "usage": {"input_tokens": 5, "output_tokens": 2}}]
        events += [{"type": "turn.started"}, {"type": "turn.completed", "usage": {"input_tokens": 8, "output_tokens": 1}}]
        usage = _usage(self.log(events), 0)
        self.assertEqual(usage["completeness"], "complete")
        self.assertEqual(usage["unreported_turns"], 0)
        self.assertEqual(usage["token_totals"], {"input_tokens": 13, "output_tokens": 3})

    def test_failed_terminal_after_completed_turn_preserves_usage_and_reservation(self):
        call = self.start_call()
        path = self.log([{"type": "turn.started", "turn_id": "one"},
                         {"type": "turn.completed", "turn_id": "one", "usage": {
                             "input_tokens": 100, "output_tokens": 20}},
                         {"type": "turn.failed", "turn_id": "one"}])
        usage = _usage(path, 0)
        self.assertEqual(usage["token_totals"], {"input_tokens": 100, "output_tokens": 20})
        self.assertEqual(usage["completeness"], "partial")
        finish_call(self.r, call["call_id"], "skip", usage)
        self.assertEqual(admin_budget_status(self.r)["accounted_tokens"], 4000)

    def test_completed_terminal_after_failed_turn_preserves_usage_and_reservation(self):
        call = self.start_call()
        path = self.log([{"type": "turn.started", "turn_id": "one"},
                         {"type": "turn.failed", "turn_id": "one"},
                         {"type": "turn.completed", "turn_id": "one", "usage": {
                             "input_tokens": 100, "output_tokens": 20}}])
        usage = _usage(path, 0)
        self.assertEqual(usage["token_totals"], {"input_tokens": 100, "output_tokens": 20})
        self.assertEqual(usage["completeness"], "partial")
        finish_call(self.r, call["call_id"], "skip", usage)
        self.assertEqual(admin_budget_status(self.r)["accounted_tokens"], 4000)

    def test_business_digest_distinguishes_hidden_log_middle(self):
        problem = self.problem()
        packet = self.r.review_packet(self.p, problem["id"])
        first, changed = copy.deepcopy(packet), copy.deepcopy(packet)
        preview = {"tool_name": "Shell", "tool_response": "same visible head and tail", "truncated": True}
        full = {"tool_name": "Shell", "tool_response": "head" + "x" * 10000 + "root cause A" + "x" * 10000 + "tail"}
        first["events"][0].update(payload=preview, business_digest=business_evidence_digest(full))
        changed["events"][0].update(payload=preview, business_digest=business_evidence_digest(
            dict(full, tool_response=full["tool_response"].replace("cause A", "cause B"))))
        self.assertNotEqual(_fingerprint(first), _fingerprint(changed))

    def test_business_digest_ignores_transport_but_keeps_nested_business_identity(self):
        original = {"conversation_id": "first", "tool_use_id": "one", "timestamp": 10,
                    "tool_input": {"id": "resource", "timestamp": 20}, "metadata": {"empty": None}}
        changed = dict(original, conversation_id="other", tool_use_id="two", timestamp=99)
        self.assertEqual(business_evidence_digest(original), business_evidence_digest(changed))
        changed["tool_input"] = {"id": "other-resource", "timestamp": 20}
        self.assertNotEqual(business_evidence_digest(original), business_evidence_digest(changed))

    def test_legacy_truncated_evidence_never_deduplicates_distinct_events(self):
        problem = self.problem()
        first = self.r.review_packet(self.p, problem["id"])
        first["evidence_truncated"] = True
        first["events"][0].update(payload={"truncated": True, "content": "same head/tail"}, business_digest=None)
        changed = copy.deepcopy(first)
        changed["events"][0]["id"] = "different-legacy-event"
        self.assertNotEqual(_fingerprint(first), _fingerprint(changed))

    def test_full_visible_event_matches_digest_even_when_other_evidence_is_truncated(self):
        problem = self.problem()
        first = self.r.review_packet(self.p, problem["id"])
        first["evidence_truncated"] = True
        first["events"][0].pop("business_digest", None)
        changed = copy.deepcopy(first)
        changed["events"][0]["id"] = "different-transport-event"
        changed["events"][0]["business_digest"] = business_evidence_digest(first["events"][0]["payload"])
        self.assertEqual(_fingerprint(first), _fingerprint(changed))


if __name__ == "__main__":
    unittest.main()
