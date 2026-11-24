"""Stage review lifecycle boundaries, isolated SQLite and deterministic reviewers."""
import json
import re
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_lifecycle import retry_call
from agent_learning.mcp import Server
from agent_learning.phase_review import configure, prepare
from tests import test_phase_host_integration as host_fixtures
from tests import test_phase_review as fixtures


class PhaseStopTurnBoundaryTests(unittest.TestCase):
    runtime = host_fixtures.PhaseHostIntegrationTests.runtime
    host_events = host_fixtures.PhaseHostIntegrationTests.host_events

    def setUp(self):
        host_fixtures.PhaseHostIntegrationTests.setUp(self)
        self.native = "trusted-turn-chat"

    def _connect(self, host="codex"):
        self.r, self.p = self.runtime(host)
        configure(self.r, self.p, {"mode": "phase", "every_phases": 2}, 0)
        self.send, self.conversation = self.host_events(self.r, native=self.native)
        self.server = Server(self.r, self.p, workspace=self.workspace)

    def _task(self, handle, goal, **changes):
        current = self.r.task(self.p, self.conversation)
        if current:
            changes.update(expected_task_id=current["id"], expected_write_version=current["write_version"])
        return self.server.call("task_update", {"context_handle": handle, "goal": goal, **changes})

    def _hook(self, kind, **observations):
        return self.r.handle_hook({"cwd": str(self.workspace), "session_id": self.native,
                                   "hook_event_name": kind, **observations})

    @staticmethod
    def _handle(result):
        context = result["hookSpecificOutput"]["additionalContext"]
        return re.search(r"context_handle=([0-9a-f]+)", context).group(1)

    def _prompt(self, turn, prompt):
        return self._handle(self.send("prompt", turn=turn, text=prompt))

    def _deliver(self, turn, prompt):
        handle = self._prompt(turn, prompt)
        self._task(handle, prompt, new_task=True)
        return self._task(handle, prompt, status="delivered")

    def _receipts(self):
        return {row["task_id"]: dict(row) for row in self.r.db.execute(
            "SELECT p.task_id,p.state,p.event_id,p.delivery_version,e.task_id AS event_task,"
            "e.task_write_version,e.task_status,json_extract(e.payload,'$.turn_id') AS turn_id "
            "FROM review_phases p LEFT JOIN events e ON e.project=p.project AND e.id=p.event_id WHERE p.project=?",
            (self.p,))}

    def test_late_parent_stop_does_not_seal_or_sweep_a_newer_delivery(self):
        self._connect()
        first = self._deliver(11, "First turn documents ORIGINAL_STAGE_INPUT")
        next_goal = "Second turn documents CURRENT_STAGE_INPUT"
        handle = self._prompt(22, next_goal)
        self._task(handle, next_goal, new_task=True)
        self._hook("PreToolUse", turn_id="22", tool_use_id="pending-operation",
                   tool_name="read_file", tool_input={"path": "release.md"})
        second = self._task(handle, next_goal, status="delivered")
        self.send("stop", turn=11, text="ORIGINAL_STAGE_REPLY")
        receipts = self._receipts()
        for delivery in (first, second):
            self.assertEqual(receipts[delivery["id"]]["state"], "awaiting_stop")
            self.assertIsNone(receipts[delivery["id"]]["event_id"])
        late = self.r.db.execute("SELECT task_id FROM events WHERE project=? AND type='Stop' "
                                 "AND json_extract(payload,'$.turn_id')='11'", (self.p,)).fetchone()
        self.assertIsNotNone(late)
        self.assertIsNone(late["task_id"])
        self.assertEqual(self.r.db.execute("SELECT state FROM operations WHERE project=? AND task_id=?",
                                           (self.p, second["id"])).fetchone()[0], "started")

        self.send("stop", turn=22, text="CURRENT_STAGE_REPLY")
        receipts = self._receipts()
        current = receipts[second["id"]]
        self.assertEqual(current["state"], "sealed")
        self.assertEqual((current["event_task"], current["turn_id"], current["task_write_version"],
                          current["task_status"]), (second["id"], "22", second["write_version"], "delivered"))
        self.assertEqual(self.r.db.execute("SELECT state FROM operations WHERE project=? AND task_id=?",
                                           (self.p, second["id"])).fetchone()[0], "unknown")
        self.assertIsNone(receipts[first["id"]]["event_id"])
        self.send("stop", turn=11, text="ORIGINAL_STAGE_REPLY")
        self.send("stop", turn=22, text="CURRENT_STAGE_REPLY")
        self.assertEqual(self._receipts(), receipts)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_late_dsh_stop_cannot_claim_the_next_delivery(self):
        self._connect("dsh")
        self.send("start", turn=0)
        first = self._deliver(11, "DSH first release convention")
        second = self._deliver(22, "DSH second release convention")
        self.send("stop", turn=11, text="Late DSH first conclusion")
        self.assertTrue(all(row["event_id"] is None for row in self._receipts().values()))
        self.send("stop", turn=22, text="DSH current conclusion")
        receipts = self._receipts()
        self.assertIsNone(receipts[first["id"]]["event_id"])
        self.assertEqual((receipts[second["id"]]["state"], receipts[second["id"]]["turn_id"],
                          receipts[second["id"]]["task_write_version"]),
                         ("sealed", "turn-22", second["write_version"]))

    def test_late_projected_prompt_cannot_replace_the_current_parent_turn(self):
        self._connect()
        first = self._deliver(11, "First user prompt with source details")
        second = self._deliver(22, "Current user prompt with different evidence")
        self.send("prompt", turn=11, text="Earlier host prompt projected with shorter details")
        self.assertEqual(self.r.task(self.p, self.conversation)["write_version"], second["write_version"])

        self.send("stop", turn=22, text="Current stage reply after late prompt")
        receipts = self._receipts()
        self.assertIsNone(receipts[first["id"]]["event_id"])
        self.assertEqual((receipts[second["id"]]["state"], receipts[second["id"]]["turn_id"]), ("sealed", "22"))

    def test_repeated_current_prompt_can_recover_context_without_fencing_task(self):
        self._connect()
        goal = "Validate a published release procedure"
        handle = self._prompt(14, goal)
        task = self._task(handle, goal, new_task=True)
        self.assertEqual(self._prompt(14, goal), handle)
        self.assertEqual(self._prompt(14, "Shortened host projection"), handle)
        self.assertEqual(self.r.task(self.p, self.conversation)["write_version"], task["write_version"])

    def test_projected_old_prompt_without_delivery_cannot_replace_current_turn(self):
        self._connect()
        self._prompt(15, "An earlier prompt without a delivery")
        task = self._deliver(25, "Current delivery with a native parent turn")
        self.assertEqual(self.send("prompt", turn=15, text="Shortened old prompt"), {})
        self.assertEqual(self.r.task(self.p, self.conversation)["write_version"], task["write_version"])
        self.send("stop", turn=25, text="Current result after an old prompt was replayed")
        receipt = self._receipts()[task["id"]]
        self.assertEqual((receipt["state"], receipt["turn_id"]), ("sealed", "25"))

    def test_delivery_projection_failure_keeps_original_stop_for_recovery(self):
        self._connect()
        task = self._deliver(61, "Preserve the original release evidence")
        with (
            patch.object(self.r, "_capture_delivery", side_effect=sqlite3.OperationalError("Local projection failed")),
            patch("agent_learning.admin_queue.claim_dispatch", return_value=None),
        ):
            self.send("stop", turn=61, text="The source was checked before confirming delivery")

        self.assertEqual(self._receipts()[task["id"]]["state"], "awaiting_stop")
        event = self.r.db.execute("SELECT id,task_write_version FROM events WHERE project=? AND type='Stop' "
                                  "AND task_id=?", (self.p, task["id"])).fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(event["task_write_version"], task["write_version"])
        receipt = self.r.db.execute("SELECT t.stop_event,q.event_id FROM review_phases p "
            "JOIN phase_delivery_turns t ON t.phase_id=p.id JOIN phase_seal_requests q ON q.phase_id=p.id "
            "WHERE p.project=? AND p.task_id=?", (self.p, task["id"])).fetchone()
        self.assertIsNotNone(receipt)
        self.assertEqual(tuple(receipt), (event["id"], event["id"]))

        with self.r.transaction():
            prepare(self.r, self.p, force=True)
        recovered = self._receipts()[task["id"]]
        self.assertEqual((recovered["state"], recovered["event_id"]), ("queued", event["id"]))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM phase_seal_requests WHERE project=?", (self.p,)).fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_unmatched_stop_and_child_do_not_close_a_turn_bound_delivery(self):
        self._connect()
        task = self._deliver(31, "Record a release convention")
        self.send("stop", turn=99, text="Unmatched parent reply")
        self._hook("Stop", last_assistant_message="Parent reply without turn identity")
        self.send("stop", turn=31, text="Child reply", child=True)
        self.assertIsNone(self._receipts()[task["id"]]["event_id"])

        self.send("stop", turn=31, text="Host confirms release convention")
        receipt = self._receipts()[task["id"]]
        self.assertEqual(receipt["state"], "sealed")
        self.assertEqual((receipt["turn_id"], receipt["task_write_version"]), ("31", task["write_version"]))

    def test_an_interim_stop_does_not_close_a_later_delivery_in_the_same_turn(self):
        self._connect()
        goal = "Record a result after the host continues an unfinished turn"
        handle = self._prompt(33, goal)
        self._task(handle, goal, new_task=True)
        self.send("stop", turn=33, text="Work is still in progress")
        self.assertEqual(self._receipts(), {})

        task = self._task(handle, goal, status="delivered")
        self._hook("Stop", turn_id="33", stop_hook_active=True, last_assistant_message="Result verified after continuation")
        receipt = self._receipts()[task["id"]]
        self.assertEqual((receipt["state"], receipt["turn_id"], receipt["task_write_version"]),
                         ("sealed", "33", task["write_version"]))

    def test_legacy_prompt_and_stop_without_turn_keep_exact_delivery_recovery(self):
        self._connect()
        goal = "Record convention for a host without turn identifiers"
        handle = self._handle(self._hook("UserPromptSubmit", prompt=goal))
        self._task(handle, goal, new_task=True)
        task = self._task(handle, goal, status="delivered")

        self._hook("Stop", last_assistant_message="Legacy host confirms release convention")
        receipt = self._receipts()[task["id"]]
        self.assertEqual(receipt["state"], "sealed")
        self.assertIsNone(receipt["turn_id"])
        self.assertEqual(receipt["task_write_version"], task["write_version"])

    def test_corrected_result_in_original_turn_seals_exact_corrected_write(self):
        self._connect()
        handle = self._prompt(41, "Record the test result")
        self._task(handle, "Record the test result", new_task=True)
        initial = self._task(handle, "Record the test result", status="delivered", scope={"result": "initial"})
        revised = self._task(handle, "Record the test result", status="delivered", scope={"result": "checked result"})
        self.assertEqual((revised["id"], revised["revision"]), (initial["id"], initial["revision"]))
        self.assertGreater(revised["write_version"], initial["write_version"])

        self._hook("SessionStart", source="compact", turn_id="42")
        self.send("stop", turn=41, text="The result was checked")
        receipt = self._receipts()[initial["id"]]
        self.assertEqual(receipt["state"], "sealed")
        self.assertEqual(receipt["task_write_version"], revised["write_version"])
        self.assertEqual(receipt["delivery_version"], revised["write_version"])


class PhaseReviewBoundaryTests(unittest.TestCase):
    setUp = fixtures.PhaseReviewTests.setUp
    phase = fixtures.PhaseReviewTests.phase

    def test_current_model_dedup_ignores_unrelated_ambiguous_observation(self):
        self.r.admin_configure(self.p, True, 'host', 'current', '', expected_policy_version=0)
        for conversation in ('one', 'two', 'three', 'four'):
            self.r.admin_observe_model(self.p, conversation, 'model-a')
        self.r.admin_observe_model(self.p, 'unrelated-chat', 'model-b')
        self.phase('one'); self.phase('two')
        with patch('agent_learning.admin.subprocess.Popen', fixtures.reviewer('skip')):
            self.assertEqual(admin_once(self.r, self.p)['status'], 'skip')
        before = self.r.db.execute('SELECT COUNT(*) FROM admin_calls').fetchone()[0]
        self.phase('three'); self.phase('four')
        with patch('agent_learning.admin.subprocess.Popen') as launch:
            admin_once(self.r, self.p)
            self.assertEqual(launch.call_count, 0, 'Equivalent stage evidence should reuse its accepted review')
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM admin_calls').fetchone()[0], before)

    def test_explicit_retry_reopens_same_frozen_phase_batch(self):
        self.phase('one'); self.phase('two', 'Other facts')
        class Timeout(fixtures.reviewer()):
            def communicate(self, *args, **kwargs):
                raise subprocess.TimeoutExpired('controlled', 5)
        with patch('agent_learning.admin.subprocess.Popen', Timeout):
            first = admin_once(self.r, self.p)
        self.assertEqual(first['status'], 'timeout_unknown', first)
        call = self.r.db.execute('SELECT id,problem FROM admin_calls').fetchone()
        self.assertEqual(retry_call(self.r, self.p, call['id'], 'User confirmed bounded retry')['status'], 'retry_allowed')
        self.r.db.execute('UPDATE admin_executor_failures SET next_try=0')
        with patch('agent_learning.admin.subprocess.Popen', fixtures.reviewer('skip')):
            second = admin_once(self.r, self.p)
        self.assertEqual(second['status'], 'skip', second)
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM admin_calls').fetchone()[0], 2)
        self.assertEqual(self.r.db.execute('SELECT state FROM admin_calls WHERE id=?', (call['id'],)).fetchone()[0], 'timeout_unknown')

    def test_unknown_phase_is_not_retried_without_explicit_recovery(self):
        self.phase('one'); self.phase('two', 'Other facts')
        class Timeout(fixtures.reviewer()):
            def communicate(self, *args, **kwargs):
                raise subprocess.TimeoutExpired('controlled', 5)
        with patch('agent_learning.admin.subprocess.Popen', Timeout):
            self.assertEqual(admin_once(self.r, self.p)['status'], 'timeout_unknown')
        with patch('agent_learning.admin.subprocess.Popen') as launch:
            admin_once(self.r, self.p)
            self.assertEqual(launch.call_count, 0, 'Unknown results require explicit recovery')
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM admin_calls').fetchone()[0], 1)

    def test_changed_comparison_is_replanned_before_paying_for_old_snapshot(self):
        self.phase('one'); self.phase('two', 'Other facts')
        member = self.r.db.execute('SELECT problem_id FROM phase_review_members LIMIT 1').fetchone()[0]
        self.r.db.execute('INSERT INTO admin_case_epochs(project,problem,revision) VALUES(?,?,1) '
                          'ON CONFLICT(project,problem) DO UPDATE SET revision=revision+1', (self.p, member))
        with patch('agent_learning.admin.subprocess.Popen', fixtures.reviewer('skip')):
            result = admin_once(self.r, self.p)
        self.assertEqual(result['status'], 'skip', result)
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM admin_calls').fetchone()[0], 1)

    def test_case_scan_bound_never_claims_unseen_stage_material_fully_covered(self):
        event = self.phase('one')
        task = self.r.task(self.p, 'one')
        for number in range(110):
            self.r._problem(self.p, 'one',
                '阶段公开记录：A sourced project fact [' + format(number, '032x') + '] part 1',
                'mixed', [event['id']], task_binding=task)
        self.phase('two', 'Other independent convention')
        frozen = json.loads(self.r.db.execute('SELECT packet FROM phase_review_batches').fetchone()[0])
        total = self.r.db.execute("SELECT COUNT(*) FROM problems WHERE conversation='one'").fetchone()[0]
        self.assertGreater(total, 100)
        self.assertFalse(frozen['coverage_complete'], 'Bounded case scan must retain an explicit continuation')


if __name__ == '__main__':
    unittest.main()
