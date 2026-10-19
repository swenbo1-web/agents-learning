"""Bounded stage scans advance past local blockers without spending or unlocking."""
import copy
import json
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_lifecycle import claim_call, finish_call, mark_launch_intent
from agent_learning.admin_queue import claim_dispatch, release_dispatch
from agent_learning.phase_review import configure, policy, seal, status
from tests import test_phase_review as fixtures


class PhaseScanProgressTests(unittest.TestCase):
    setUp = fixtures.PhaseReviewTests.setUp
    phase = fixtures.PhaseReviewTests.phase

    def backlog(self, *, count=100, ready_tail=True, lock_state=None):
        """Create genuine ordered stage sources; no test-only cursor mutation."""
        conversation = "long-stage"
        task = self.r.task_update(self.p, conversation, "Review sourced stage conventions", new_task=True)
        prompt, _ = self.r._event(self.p, conversation, "UserPromptSubmit",
            {"prompt": "Keep the project convention and an independent tail constraint"},
            outcome="reported", task_id=task["id"], task_revision=task["revision"])
        failures = []
        for number in range(count):
            event, _ = self.r._event(self.p, conversation, "PostToolUse",
                {"tool_name": "offline-check", "error": "Unrecovered stage failure " + str(number)},
                outcome="failed", task_id=task["id"], task_revision=task["revision"])
            failures.append(self.r._problem(self.p, conversation,
                "Unrecovered stage failure " + str(number), "operation", [event["id"]], task_binding=task))
        tail = self.r._problem(self.p, conversation, "Independent tail convention requiring review",
            "project", [prompt["id"]], task_binding=task) if ready_tail else None
        locked = None
        if lock_state:
            with self.r.transaction():
                locked = claim_call(self.r, self.p, failures[0]["id"], failures[0]["revision"],
                    "preserve-real-source-lock", self.r.host_epoch(), 5)
            self.assertEqual(locked["status"], "reserved")
            mark_launch_intent(self.r, locked["call_id"], locked["owner"], self.r.host_epoch())
            finish_call(self.r, locked["call_id"], lock_state)
        self.r.task_update(self.p, conversation, task["goal"], status="delivered",
            expected_task_id=task["id"], expected_write_version=task["write_version"])
        stop, _ = self.r._event(self.p, conversation, "Stop",
            {"last_assistant_message": "Delivered the current stage conventions"}, outcome="reported",
            task_id=task["id"], task_revision=task["revision"])
        with self.r.transaction():
            seal(self.r, self.p, conversation, stop["id"])
        self.phase("other-stage", "Another independent sourced convention")
        return task, failures, tail, locked

    def advance(self, count=8):
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("skip")):
            return [admin_once(self.r, self.p) for _ in range(count)]

    def reviewed(self, identity):
        return bool(self.r.db.execute("SELECT 1 FROM review_receipts WHERE project=? AND problem=? "
            "AND decision='skip' LIMIT 1", (self.p, identity)).fetchone())

    def test_one_hundred_unready_cases_cannot_starve_later_complete_case(self):
        _, _, tail, _ = self.backlog()
        self.advance()
        self.assertTrue(self.reviewed(tail["id"]), "A complete case after the bounded first page must become reachable")
        calls = self.r.db.execute("SELECT COUNT(*) FROM admin_calls WHERE project=?", (self.p,)).fetchone()[0]
        self.assertLessEqual(calls, 3, "Local unready cases must not each consume a model call")

    def test_full_unready_round_stops_without_repeated_empty_batches_or_calls(self):
        self.backlog(ready_tail=False)
        self.advance()
        before_calls = self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0]
        before_batches = self.r.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0]
        with patch("agent_learning.admin.subprocess.Popen") as model:
            for _ in range(3):
                admin_once(self.r, self.p)
        model.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], before_calls)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0], before_batches)

    def test_new_recovery_revision_reenters_after_completed_unready_scan(self):
        task, failures, _, _ = self.backlog(ready_tail=False)
        self.advance()
        source = failures[0]
        self.assertFalse(self.reviewed(source["id"]))
        event, _ = self.r._event(self.p, "long-stage", "PostToolUse",
            {"tool_name": "offline-check", "result": "A new recovery result after the original delivery"},
            outcome="succeeded", task_id=task["id"], task_revision=task["revision"])
        updated = self.r._attach_case_evidence(source["id"], [(event["id"], "potential_recovery")])
        self.assertGreater(updated["revision"], source["revision"])
        self.advance()
        self.assertTrue(self.reviewed(source["id"]), "Real evidence changes must wake a paused scan without a new delivery")

    def pending_members(self):
        self.phase("first", "First independent sourced convention")
        self.phase("second", "Second independent sourced convention")
        row = self.r.db.execute("SELECT packet FROM phase_review_batches WHERE state='pending'").fetchone()
        self.assertIsNotNone(row)
        packet = json.loads(row[0])
        return {item["problem"]["id"] for item in packet["items"]}

    def test_changed_comparison_replans_unpaid_members_without_advancing_past_them(self):
        members = self.pending_members()
        original_packet = self.r.review_packet
        def changed_packet(*args, **kwargs):
            packet = copy.deepcopy(original_packet(*args, **kwargs))
            if packet["problem"]["id"] in members:
                packet["related_knowledge"].append({"knowledge_id": "new-comparison", "revision": 1,
                    "content": "A newly visible project convention", "source_health": "valid"})
            return packet
        # Only comparison material changes; case revisions and phase settings
        # remain identical, so a cursor reset cannot rely on those revisions.
        with patch.object(self.r, "review_packet", side_effect=changed_packet):
            self.advance()
        self.assertTrue(all(self.reviewed(identity) for identity in members),
                        "Superseding an unpaid frozen batch must retain every original member")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_input_policy_change_releases_unpaid_batch_without_losing_members(self):
        members = self.pending_members()
        cfg = policy(self.r, self.p)
        configure(self.r, self.p, {"max_input_tokens": cfg["max_input_tokens"] + 1000}, cfg["version"])
        self.advance()
        self.assertTrue(all(self.reviewed(identity) for identity in members))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_later_dispatch_event_advances_local_scan_without_manual_review(self):
        from agent_learning import phase_review
        _, _, tail, _ = self.backlog(count=300)
        original_prepare = phase_review.prepare
        def one_page(*args, **kwargs):
            kwargs["_page_budget"] = 1
            return original_prepare(*args, **kwargs)
        # A finite worker pass stops with later source pages still uncovered.
        with patch.object(phase_review, "prepare", side_effect=one_page):
            self.advance(count=1)
        self.assertFalse(self.reviewed(tail["id"]))
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM phase_review_batches WHERE state='pending'").fetchone())
        with patch("agent_learning.admin.subprocess.Popen") as model:
            token = claim_dispatch(self.r, self.p)
        model.assert_not_called()
        self.assertIsNotNone(token, "A later normal dispatch event must find ready material beyond the old page")
        pending = self.r.db.execute("SELECT packet FROM phase_review_batches WHERE state='pending'").fetchone()
        self.assertIsNotNone(pending)
        self.assertIn(tail["id"], {item["problem"]["id"] for item in json.loads(pending[0])["items"]})
        release_dispatch(self.r, self.p, token)

    def ask_for_evidence(self):
        members = self.pending_members()
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("needs_evidence")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "needs_evidence", result)
        return members

    def recover_member(self, identity):
        source = self.r.db.execute("SELECT * FROM problems WHERE id=?", (identity,)).fetchone()
        event, _ = self.r._event(self.p, source["conversation"], "PostToolUse",
            {"tool_name": "offline-check", "result": "New successful verification of the cited convention"},
            outcome="succeeded", task_id=source["task_id"], task_revision=source["task_revision"])
        updated = self.r._attach_case_evidence(identity, [(event["id"], "potential_recovery")])
        self.assertGreater(updated["revision"], source["revision"])
        return updated

    def test_needs_evidence_stage_sleeps_unchanged_and_wakes_on_real_source_revision(self):
        members = self.ask_for_evidence()
        self.assertEqual(status(self.r, self.p)["phases"].get("partial"), 2,
                         "Fully visited material still needs a wakeable evidence-wait state")
        before = self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0]
        with patch("agent_learning.admin.subprocess.Popen") as model:
            for _ in range(3):
                admin_once(self.r, self.p)
        model.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], before)
        identity = sorted(members)[0]
        self.recover_member(identity)
        self.advance()
        self.assertTrue(self.reviewed(identity), "New evidence must reach review without an additional stage delivery")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], before + 1)

    def test_global_comparison_wakeup_cannot_erase_unchanged_evidence_wait(self):
        members = self.ask_for_evidence()
        # Another knowledge publication can invalidate the local scan signature
        # without changing this case's actual visible comparison or evidence.
        self.r.db.execute("UPDATE projects SET generation=generation+1 WHERE id=?", (self.p,))
        with patch("agent_learning.admin.subprocess.Popen") as model:
            for _ in range(3):
                admin_once(self.r, self.p)
        model.assert_not_called()
        self.assertEqual(status(self.r, self.p)["phases"].get("partial"), 2,
                         "Reusing an unchanged needs-evidence receipt must preserve its wakeup path")
        identity = sorted(members)[0]
        self.recover_member(identity)
        self.advance()
        self.assertTrue(self.reviewed(identity))

    def test_only_phase_containing_waiting_member_remains_partial(self):
        self.pending_members()
        base = fixtures.reviewer("needs_evidence")
        class MixedReviewer(base):
            def communicate(inner, data=None, timeout=None):
                result = super().communicate(data, timeout)
                packet = json.loads(data.decode().split("<untrusted_evidence>", 1)[1]
                                    .rsplit("</untrusted_evidence>", 1)[0])
                conversations = {item["problem"]["id"]: item["problem"]["conversation"] for item in packet["items"]}
                response = json.loads(inner.output.read_text(encoding="utf-8"))
                for item in response["items"]:
                    if conversations[item["problem_id"]] == "second":
                        item["decision"] = "skip"
                inner.output.write_text(json.dumps(response), encoding="utf-8")
                return result
        with patch("agent_learning.admin.subprocess.Popen", MixedReviewer):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "needs_evidence", result)
        states = {row["conversation"]: row["state"] for row in self.r.db.execute("SELECT conversation,state FROM review_phases")}
        self.assertEqual(states, {"first": "partial", "second": "completed"},
                         "An independent completed stage must not inherit a sibling's evidence wait")

    def test_same_batch_duplicate_inherits_evidence_wait_and_own_recovery_wakes_it(self):
        goal = "The same sourced project convention"
        self.phase("first", goal)
        self.phase("second", goal)
        duplicate = self.r.db.execute("SELECT problem_id FROM phase_review_members "
            "WHERE state='duplicate_pending' LIMIT 1").fetchone()
        self.assertIsNotNone(duplicate, "This scenario requires genuine semantic evidence reuse")
        identity = duplicate["problem_id"]
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("needs_evidence")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "needs_evidence", result)
        self.assertEqual(status(self.r, self.p)["phases"].get("partial"), 2,
                         "An equivalent source inherits missing proof; it is not a completed skip")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        self.recover_member(identity)
        self.advance()
        self.assertTrue(self.reviewed(identity), "The duplicate's own new evidence must reach review")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 2,
                         "The original unchanged source must not be reviewed again with its duplicate")
        self.assertEqual(status(self.r, self.p)["phases"].get("partial"), 1)

    def test_cross_batch_reused_needs_evidence_remains_partial_without_another_call(self):
        goal = "A repeatable convention still awaiting independent proof"
        self.phase("first", goal)
        self.phase("second", goal)
        with patch("agent_learning.admin.subprocess.Popen", fixtures.reviewer("needs_evidence")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "needs_evidence", result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        with patch("agent_learning.admin.subprocess.Popen") as model:
            self.phase("third", goal)
            self.phase("fourth", goal)
            for _ in range(3):
                admin_once(self.r, self.p)
        model.assert_not_called()
        self.assertEqual(status(self.r, self.p)["phases"].get("partial"), 4,
                         "Reusing the earlier needs-evidence receipt must not finish the newer phases")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_scan_continuation_preserves_failed_and_unknown_source_locks(self):
        for lock_state in ("failed", "timeout_unknown"):
            with self.subTest(state=lock_state):
                # Each state gets an independent store, avoiding unrelated pending stages.
                if lock_state == "timeout_unknown":
                    self.doCleanups()
                    self.setUp()
                task, failures, tail, locked = self.backlog(lock_state=lock_state)
                event, _ = self.r._event(self.p, "long-stage", "PostToolUse",
                    {"tool_name": "offline-check", "result": "Later success is not retry permission"},
                    outcome="succeeded", task_id=task["id"], task_revision=task["revision"])
                self.r._attach_case_evidence(failures[0]["id"], [(event["id"], "potential_recovery")])
                self.advance()
                self.assertTrue(self.reviewed(tail["id"]), "A protected source cannot block an unrelated complete case")
                self.assertFalse(self.reviewed(failures[0]["id"]), "Scanning cannot authorize retry of a paid/unknown source")
                row = self.r.db.execute("SELECT state,retry_allowed FROM admin_calls WHERE id=?",
                    (locked["call_id"],)).fetchone()
                self.assertEqual(tuple(row), (lock_state, 0))
                self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls WHERE problem=?",
                    (failures[0]["id"],)).fetchone()[0], 1)
                self.assertIsNotNone(self.r.db.execute("SELECT reserved_tokens FROM admin_call_budgets WHERE call_id=?",
                    (locked["call_id"],)).fetchone())


if __name__ == "__main__":
    unittest.main()
