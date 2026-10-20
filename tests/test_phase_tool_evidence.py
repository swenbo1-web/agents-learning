"""Related tool observations stay in their original stage, identity and budget."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.phase_review import configure, policy, prepare, seal, status
from agent_learning.runtime import ContractError, Runtime
from tests import test_phase_review as review_fixtures


class PhaseToolEvidenceTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "build" / "local" / "r86-tools"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="isolated-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("project", "tmp", "home", "runtime-releases", "codex"):
            (self.root / name).mkdir()
        self.workspace = self.root / "project"
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"ALP_HOME": str(self.root / "home"), "ALP_CONFIG": str(self.root / "config.json"),
            "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"), "CODEX_HOME": str(self.root / "codex"),
            "ALP_DSH_REVIEW_PROFILE": "", "PYTHONDONTWRITEBYTECODE": "1",
            **{key: str(self.root / "tmp") for key in ("TMP", "TEMP", "TMPDIR")}}).start()
        patch.object(tempfile, "tempdir", str(self.root / "tmp")).start()
        self.r = Runtime(self.root / "home", config={"scheduler": {"enabled": False}, "admin": {
            "enabled": True, "command": [sys.executable], "timeout_seconds": 5,
            "max_input_chars": 16000, "auto_publish": False}})
        self.addCleanup(self.r.close)
        patch.object(self.r, "_launch_admin").start()
        patch.object(self.r, "_launch_scheduler").start()
        self.p = self.r.project(self.workspace)
        self.native = "phase-tools"
        self.c = self.r.host_conversation(self.native)
        self.hook("SessionStart", source="startup", model="offline-review-model")
        configure(self.r, self.p, {"mode": "phase", "every_phases": 1}, 0)
        self.begin()

    def hook(self, kind, **values):
        return self.r.handle_hook({"cwd": str(self.workspace), "session_id": self.native,
                                   "hook_event_name": kind, **values})

    def begin(self, goal="Inspect the terminal diagnosis and retain a sourced conclusion"):
        task = self.r.task_update(self.p, self.c, goal, new_task=True)
        self.hook("UserPromptSubmit", turn_id="prompt-" + task["id"], prompt=goal)
        return task

    def tool(self, identity, output, *, response=None, agent=None, tool_name="exec_command", command=None):
        values = {"turn_id": "tool-" + identity, "tool_use_id": identity, "tool_name": tool_name,
                  "tool_input": {"cmd": command or "offline-inspection " + identity}}
        if agent:
            values["agent_id"] = agent
        self.hook("PreToolUse", **values)
        pre = self.r.db.execute("SELECT operation FROM events WHERE conversation=? AND type='PreToolUse' "
                                "ORDER BY rowid DESC LIMIT 1", (self.c,)).fetchone()
        self.assertIsNotNone(pre)
        result = {"exit_code": 0, "output": output} if response is None else {**response, "output": output}
        self.hook("PostToolUse", **values, tool_response=result)
        row = self.r.db.execute("SELECT * FROM events WHERE conversation=? AND type='PostToolUse' "
            "AND operation=? ORDER BY rowid DESC LIMIT 1", (self.c, pre["operation"])).fetchone()
        self.assertIsNotNone(row, "the fixture must capture a real native PostToolUse event")
        return dict(row)

    def finish(self, reply="TERM_DIAG_ALPHA identifies the terminal observation", *, turn="delivery"):
        task = self.r.task(self.p, self.c)
        self.r.task_update(self.p, self.c, task["goal"], status="delivered",
            expected_task_id=task["id"], expected_write_version=task["write_version"])
        self.hook("Stop", turn_id=turn, last_assistant_message=reply)
        return self.r.db.execute("SELECT * FROM review_phases WHERE project=? AND conversation=? "
            "ORDER BY created DESC LIMIT 1", (self.p, self.c)).fetchone()

    def public_cases(self, phase):
        return self.r.db.execute("SELECT * FROM problems WHERE project=? AND summary LIKE ? ORDER BY created,id",
                                 (self.p, "%[" + phase["id"] + "] part %")).fetchall()

    def packet(self, phase):
        cases = self.public_cases(phase)
        self.assertEqual(len(cases), 1, "small public stages should retain one original public case")
        return self.r.review_packet(self.p, cases[0]["id"])

    def selected_tools(self, packet):
        return [event for event in packet["events"] if event["type"] == "PostToolUse"]

    def candidate(self, packet, refs, validation="tool_success"):
        return {"problem_id": packet["problem"]["id"], "problem_revision": packet["problem"]["revision"],
            "title": "Terminal observation", "content": "The cited terminal observation was captured.",
            "category": "operation", "knowledge_kind": "solution", "validation_level": validation,
            "metadata": {key: "" for key in ("tool", "tool_version", "error_code", "environment", "verification")},
            "applicability": "This isolated terminal task", "limitations": "Tool outcome is not professional validation",
            "evidence_refs": refs}

    def test_succeeded_original_observation_is_attached_to_existing_case(self):
        observed = self.tool("diagnostic", "TERM_DIAG_ALPHA was observed")
        phase = self.finish()
        packet = self.packet(phase)
        tools = self.selected_tools(packet)
        self.assertEqual([event["id"] for event in tools], [observed["id"]])
        self.assertEqual(tools[0]["outcome"], "succeeded")
        self.assertTrue(tools[0]["current_tool_success"])
        self.assertEqual(packet["phase_tool_context"]["selected_count"], 1)
        self.assertFalse(packet["incomplete"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM problems WHERE category='operation'").fetchone()[0], 0)

    def test_unknown_observation_is_visible_but_cannot_become_tool_success(self):
        self.assert_non_success({}, "unknown")

    def test_call_completed_observation_is_visible_but_cannot_become_tool_success(self):
        self.assert_non_success({"isError": False}, "call_completed")

    def assert_non_success(self, response, expected):
        observed = self.tool("incomplete-result", "TERM_DIAG_ALPHA was reported", response=response)
        self.assertEqual(observed["outcome"], expected)
        phase = self.finish()
        packet = self.packet(phase)
        tools = self.selected_tools(packet)
        self.assertEqual([event["id"] for event in tools], [observed["id"]])
        self.assertEqual(tools[0]["outcome"], expected)
        self.assertFalse(tools[0]["current_tool_success"])
        with self.assertRaises(ContractError):
            self.r.submit_candidate(self.p, **self.candidate(packet, [observed["id"]]))
        self.assertEqual(self.r.db.execute("SELECT outcome FROM events WHERE id=?", (observed["id"],)).fetchone()[0], expected)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_selection_receipt_is_audit_only_not_an_extra_case_or_citable_source(self):
        self.tool("diagnostic", "TERM_DIAG_ALPHA was observed")
        phase = self.finish()
        packet = self.packet(phase)
        audit = self.r.db.execute("SELECT id FROM events WHERE type='PhaseToolEvidenceSelection'").fetchone()
        self.assertIsNotNone(audit)
        self.assertNotIn(audit[0], {event["id"] for event in packet["events"]})
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM problem_events WHERE event=?", (audit[0],)).fetchone())
        with self.assertRaises(ContractError):
            self.r.submit_candidate(self.p, **self.candidate(packet, [audit[0]], "reported"))

    def test_no_output_association_and_generic_status_do_not_attach_tools(self):
        self.tool("generic", "The project check succeeded and the connection status is normal")
        self.tool("input-only", "A different observable result", command="offline TERM_DIAG_ALPHA")
        phase = self.finish("TERM_DIAG_ALPHA was discussed; the connection status is normal")
        self.assertEqual(self.selected_tools(self.packet(phase)), [])

    def test_pure_generic_status_vocabulary_cannot_establish_relevance(self):
        self.tool("generic", "The project check succeeded and the connection status is normal")
        phase = self.finish("The project check succeeded and the connection status is normal")
        self.assertEqual(self.selected_tools(self.packet(phase)), [])

    def test_chinese_technical_phrase_can_associate_a_public_conclusion(self):
        observed = self.tool("chinese-diagnostic", "环境变量缺失导致终端初始化失败")
        phase = self.finish("终端诊断确认环境变量缺失会中断渲染")
        self.assertEqual([event["id"] for event in self.selected_tools(self.packet(phase))], [observed["id"]])

    def test_existing_fault_and_recovery_operations_are_not_duplicated_in_public_case(self):
        self.tool("failure", "TERM_DIAG_ALPHA validation failed", response={"exit_code": 1},
                  command="offline-identical-terminal-target")
        recovered = self.tool("recovery", "TERM_DIAG_ALPHA validation passed", command="offline-identical-terminal-target")
        phase = self.finish()
        packet = self.packet(phase)
        self.assertEqual(self.selected_tools(packet), [])
        self.assertTrue(self.r.db.execute("SELECT 1 FROM problem_events pe JOIN problems p ON p.id=pe.problem "
            "WHERE pe.event=? AND p.category='operation'", (recovered["id"],)).fetchone())
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM problems WHERE category='operation'").fetchone()[0], 1)
        self.assertGreater(packet["phase_tool_context"]["excluded"]["existing_case"], 0)

    def test_subagent_observation_is_not_borrowed_by_the_root_conclusion(self):
        self.hook("SubagentStart", agent_id="native-child", turn_id="child-start")
        child = self.tool("child-diagnostic", "TERM_DIAG_ALPHA was observed by a child", agent="native-child")
        self.assertEqual(child["agent"], "native-child")
        self.assertIsNotNone(child["assignment_id"])
        phase = self.finish()
        self.assertEqual(self.selected_tools(self.packet(phase)), [])

    def test_another_task_observation_is_not_borrowed(self):
        old = self.tool("old-task", "TERM_DIAG_ALPHA from a different task")
        self.begin("Inspect a different task's terminal conclusion")
        phase = self.finish()
        self.assertNotEqual(old["task_id"], phase["task_id"])
        self.assertEqual(self.selected_tools(self.packet(phase)), [])

    def test_observation_before_resumed_stage_window_is_not_borrowed(self):
        old = self.tool("old-window", "TERM_DIAG_ALPHA before the resumed phase")
        task = self.r.task(self.p, self.c)
        paused = self.r.task_update(self.p, self.c, task["goal"], status="paused",
            expected_task_id=task["id"], expected_write_version=task["write_version"])
        self.r.task_update(self.p, self.c, task["goal"], status="active", resume=True,
            expected_task_id=task["id"], expected_write_version=paused["write_version"])
        self.hook("UserPromptSubmit", turn_id="resumed-window", prompt="Inspect the resumed terminal phase")
        phase = self.finish()
        self.assertLessEqual(old["created"], phase["start_time"])
        self.assertEqual(self.selected_tools(self.packet(phase)), [])

    def test_equal_clock_resume_keeps_original_stage_order(self):
        with patch("time.time", return_value=2000000000.0):
            self.begin("Inspect the terminal stage at one clock tick")
            old = self.tool("same-tick-before", "TERM_DIAG_ALPHA belongs to the earlier active suffix")
            task = self.r.task(self.p, self.c)
            paused = self.r.task_update(self.p, self.c, task["goal"], status="paused",
                expected_task_id=task["id"], expected_write_version=task["write_version"])
            self.r.task_update(self.p, self.c, task["goal"], status="active", resume=True,
                expected_task_id=task["id"], expected_write_version=paused["write_version"])
            current = self.tool("same-tick-after", "TERM_DIAG_ALPHA belongs to the current suffix")
            phase = self.finish()
        self.assertEqual(old["created"], current["created"])
        self.assertEqual(old["created"], phase["start_time"])
        self.assertEqual([event["id"] for event in self.selected_tools(self.packet(phase))], [current["id"]])

    def test_equal_clock_opt_in_excludes_earlier_same_contract_observation(self):
        with patch("time.time", return_value=2000000000.0):
            configure(self.r, self.p, {"mode": "legacy"}, policy(self.r, self.p)["version"])
            old = self.tool("before-opt-in", "TERM_DIAG_ALPHA before explicit opt-in")
            configure(self.r, self.p, {"mode": "phase"}, policy(self.r, self.p)["version"])
            current = self.tool("after-opt-in", "TERM_DIAG_ALPHA after explicit opt-in")
            phase = self.finish()
        self.assertEqual(old["task_write_version"], current["task_write_version"])
        self.assertEqual(old["created"], phase["start_time"])
        self.assertEqual([event["id"] for event in self.selected_tools(self.packet(phase))], [current["id"]])

    def test_same_clock_recovery_cannot_borrow_a_later_resumed_delivery(self):
        import sqlite3
        from agent_learning.phase_seal_recovery import recover
        with patch("time.time", return_value=2000000000.0):
            self.begin("Inspect TERM_DIAG_ALPHA in two ordered suffixes")
            original = self.tool("original-suffix", "TERM_DIAG_ALPHA original observation")
            with patch("agent_learning.phase_review.seal_phase", side_effect=sqlite3.OperationalError("delayed seal")):
                phase = self.finish("TERM_DIAG_ALPHA ORIGINAL_STAGE_DELIVERY", turn="original-stop")
                task = self.r.task(self.p, self.c)
                self.r.task_update(self.p, self.c, task["goal"], resume=True, status="active",
                    expected_task_id=task["id"], expected_write_version=task["write_version"])
                future = self.tool("future-suffix", "TERM_DIAG_ALPHA FUTURE_STAGE_OBSERVATION")
                self.finish("TERM_DIAG_ALPHA FUTURE_STAGE_DELIVERY", turn="future-stop")
            with self.r.transaction():
                recover(self.r, self.p, force=True)
        packet = self.packet(phase)
        self.assertEqual(original["created"], future["created"])
        self.assertEqual([event["id"] for event in self.selected_tools(packet)], [original["id"]])
        self.assertNotIn("FUTURE_STAGE", json.dumps(packet["events"]))

    def test_previous_account_observation_is_not_borrowed(self):
        old = self.tool("old-account", "TERM_DIAG_ALPHA in the previous account context")
        original_epoch = self.r.session_epoch()
        self.r.account_changed(notification_id="explicit-isolated-account-switch")
        self.hook("SessionStart", source="resume", model="offline-review-model", turn_id="new-account-start")
        self.begin("Inspect the current account terminal conclusion")
        phase = self.finish()
        self.assertGreater(phase["session_epoch"], original_epoch)
        self.assertEqual(self.selected_tools(self.packet(phase)), [])
        self.assertIsNotNone(self.r.db.execute("SELECT id FROM events WHERE id=?", (old["id"],)).fetchone())

    def test_unpaired_tool_observation_cannot_establish_a_stage_association(self):
        self.hook("PostToolUse", turn_id="orphan", tool_use_id="orphan", tool_name="exec_command",
                  tool_response={"exit_code": 0, "output": "TERM_DIAG_ALPHA without a Pre observation"})
        phase = self.finish()
        self.assertEqual(self.selected_tools(self.packet(phase)), [])

    def test_large_complete_payload_is_omitted_without_blocking_the_public_case(self):
        self.r.config["event_max_chars"] = 20000
        self.tool("large", "TERM_DIAG_ALPHA " + "large_observation " * 600)
        phase = self.finish()
        packet = self.packet(phase)
        self.assertEqual(self.selected_tools(packet), [])
        self.assertFalse(packet["incomplete"])
        self.assertGreater(packet["phase_tool_context"]["omitted_matching"], 0)
        with patch("agent_learning.admin.subprocess.Popen", review_fixtures.reviewer("skip")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "skip", result)

    def test_projected_tool_payload_is_not_misrepresented_as_complete_evidence(self):
        self.tool("projected", "TERM_DIAG_ALPHA " + "partial_observation " * 1000)
        phase = self.finish()
        packet = self.packet(phase)
        self.assertEqual(self.selected_tools(packet), [])
        self.assertFalse(packet["incomplete"])
        self.assertGreater(packet["phase_tool_context"]["excluded"]["partial_source"], 0)

    def test_completion_partial_is_not_silently_attached_as_complete_evidence(self):
        observed = self.tool("partial-completion", "TERM_DIAG_ALPHA is only a partial observation",
                             response={"exit_code": 0, "completion": "partial"})
        phase = self.finish()
        packet = self.packet(phase)
        self.assertEqual(self.selected_tools(packet), [])
        self.assertFalse(packet["incomplete"])
        self.assertGreater(packet["phase_tool_context"]["excluded"]["partial_source"], 0)
        self.assertEqual(json.loads(observed["payload"])["tool_response"]["completion"], "partial")

    def test_delivery_projection_does_not_hide_an_otherwise_unclaimed_tool(self):
        observed = self.tool("delivery-evidence", "TERM_DIAG_ALPHA was observed")
        phase = self.finish()
        self.assertTrue(self.r.db.execute("SELECT 1 FROM deliveries d JOIN problem_events pe ON pe.problem=d.problem_id "
            "WHERE d.project=? AND pe.event=?", (self.p, observed["id"])).fetchone(),
            "the actual delivery projection must also reference the observation in this fixture")
        self.assertEqual([event["id"] for event in self.selected_tools(self.packet(phase))], [observed["id"]])

    def test_optional_tools_cannot_block_a_public_case_at_the_low_token_limit(self):
        self.begin("Inspect TERM")
        configure(self.r, self.p, {"max_input_tokens": 18000}, policy(self.r, self.p)["version"])
        self.r.config["event_max_chars"] = 20000
        observed = self.tool("token-limited", "TERM_DIAG_ALPHA " + "观测记录" * 300)
        phase = self.finish("TERM_DIAG_ALPHA was observed")
        packet = self.packet(phase)
        self.assertEqual(self.selected_tools(packet), [])
        self.assertFalse(packet["incomplete"])
        self.assertGreater(packet["phase_tool_context"]["excluded"]["budget"], 0)
        self.assertLess(len(observed["payload"]), packet["phase_tool_context"]["tool_payload_budget"],
                        "this rejection exercises token admission rather than the optional payload character cap")
        self.r.db.execute("SAVEPOINT inspect_token_boundary")
        try:
            self.r._attach_case_evidence(packet["problem"]["id"], [(observed["id"], "observation")])
            expanded = self.r.review_packet(self.p, packet["problem"]["id"], max_chars=11800)
            self.assertFalse(expanded["incomplete"], "the same expanded packet fits the character budget")
            self.assertFalse(expanded["evidence_truncated"])
        finally:
            self.r.db.execute("ROLLBACK TO inspect_token_boundary")
            self.r.db.execute("RELEASE inspect_token_boundary")
        pending = self.r.db.execute("SELECT packet FROM phase_review_batches WHERE state='pending'").fetchone()
        self.assertIsNotNone(pending, "the existing public case still needs an admissible frozen batch")
        self.assertEqual(len(json.loads(pending[0])["items"]), 1)
        with patch("agent_learning.admin.subprocess.Popen", review_fixtures.reviewer("skip")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "skip", result)

    def test_same_phase_observation_survives_a_legal_execution_fence(self):
        observed = self.tool("before-fence", "TERM_DIAG_ALPHA was observed before another user message")
        self.hook("UserPromptSubmit", turn_id="same-phase-follow-up", prompt="Inspect the same terminal phase again")
        task = self.r.task(self.p, self.c)
        self.assertGreater(task["write_version"], observed["task_write_version"])
        phase = self.finish()
        self.assertLess(observed["task_write_version"], phase["delivery_version"])
        self.assertEqual([event["id"] for event in self.selected_tools(self.packet(phase))], [observed["id"]])

    def test_twenty_phase_wrapper_keeps_public_case_reviewable_at_token_boundary(self):
        import copy
        import sqlite3
        from agent_learning.admin_usage import estimate_reservation
        from agent_learning.phase_review import instruction
        from agent_learning.phase_seal_recovery import recover
        current = policy(self.r, self.p)
        configure(self.r, self.p, {"every_phases": 20, "max_input_tokens": 40000}, current["version"])
        # Retain real original Stops while deferring their optional association.
        # The same captured stages can then be replayed at a measured boundary.
        with patch("agent_learning.phase_review.seal_phase", side_effect=sqlite3.OperationalError("isolated delayed seal")), \
                patch("agent_learning.admin_queue.claim_dispatch", return_value=None):
            first = None
            for index in range(20):
                if index:
                    self.begin("Inspect the source identity for terminal diagnostic " + str(index))
                if not index:
                    self.tool("twenty-phase", "TERM_DIAG_ALPHA " + "观测记录" * 300)
                phase = self.finish("TERM_DIAG_ALPHA identifies the original terminal observation " + str(index),
                                    turn="twenty-delivery-" + str(index))
                first = first or phase
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM phase_seal_requests").fetchone()[0], 20)

        self.r.db.execute("SAVEPOINT measure_twenty_phase_wrapper")
        try:
            for _ in range(3):
                recover(self.r, self.p, force=True)
            prepare(self.r, self.p)
            measured = json.loads(self.r.db.execute("SELECT packet FROM phase_review_batches WHERE state='pending'").fetchone()[0])
            self.assertEqual(len(measured["phase_ids"]), 20)
            expanded = next(item for item in measured["items"] if item["problem"]["id"] == self.public_cases(first)[0]["id"])
            self.assertEqual(len(self.selected_tools(expanded)), 1)
            # Use the real parent, all twenty identifiers and real tool metadata.
            # Remove siblings so the limit tests admission of this original case.
            measured = copy.deepcopy(measured)
            measured["items"] = [expanded]
            measured.update(omitted_problem_ids=[], reused_problem_ids=[], omitted_source_count=0,
                            coverage_complete=False)
            narrow = estimate_reservation(self.r, self.p, instruction(measured) + " " * 3000,
                                          phase=True)["estimated_input_tokens"] - 1
        finally:
            self.r.db.execute("ROLLBACK TO measure_twenty_phase_wrapper")
            self.r.db.execute("RELEASE measure_twenty_phase_wrapper")
        self.assertGreaterEqual(narrow, 16000)
        current = policy(self.r, self.p)
        configure(self.r, self.p, {"max_input_tokens": narrow}, current["version"])
        with patch("agent_learning.admin.subprocess.Popen") as model:
            with self.r.transaction():
                for _ in range(3):
                    recover(self.r, self.p, force=True)
                prepare(self.r, self.p)
        model.assert_not_called()
        packet = self.packet(first)
        self.assertEqual(self.selected_tools(packet), [], "optional evidence must leave room for the full batch wrapper")
        self.assertGreater(packet["phase_tool_context"]["excluded"].get("budget", 0), 0)
        self.assertFalse(packet["incomplete"])
        frozen = json.loads(self.r.db.execute("SELECT packet FROM phase_review_batches WHERE state='pending'").fetchone()[0])
        self.assertEqual(len(frozen["phase_ids"]), 20)
        self.assertIn(packet["problem"]["id"], {item["problem"]["id"] for item in frozen["items"]})
        self.assertTrue(estimate_reservation(self.r, self.p, instruction(frozen), phase=True)["admitted"])
        with patch("agent_learning.admin.subprocess.Popen", review_fixtures.reviewer("skip")):
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")

    def test_bounded_tool_scan_exposes_partial_coverage_without_creating_cases(self):
        for number in range(7):
            self.tool("scan-" + str(number), "TERM_SCAN_" + str(number) + " was observed")
        with patch("agent_learning.phase_tool_evidence.SCAN_LIMIT", 4):
            phase = self.finish("Terminal identifiers: " + " ".join("TERM_SCAN_" + str(number) for number in range(7)))
        packet = self.packet(phase)
        self.assertTrue(packet["phase_tool_context"]["scan_limited"])
        self.assertLessEqual(len(self.selected_tools(packet)), 3)
        self.assertEqual(len(self.public_cases(phase)), 1)
        self.assertFalse(packet["incomplete"], "bounded optional scope cannot impersonate a complete tool audit")

    def test_latest_post_observation_is_selected_once_per_operation(self):
        running = self.tool("background", "TERM_DIAG_ALPHA process is running", response={"session_id": 123})
        self.hook("PostToolUse", turn_id="tool-background", tool_use_id="background", tool_name="exec_command",
                  tool_input={"cmd": "offline-inspection background"},
                  tool_response={"exit_code": 0, "output": "TERM_DIAG_ALPHA process finished"})
        latest = self.r.db.execute("SELECT id,outcome FROM events WHERE operation=? AND type='PostToolUse' "
                                   "ORDER BY rowid DESC LIMIT 1", (running["operation"],)).fetchone()
        self.assertEqual(latest["outcome"], "succeeded")
        self.assertNotEqual(latest["id"], running["id"])
        phase = self.finish()
        self.assertEqual([event["id"] for event in self.selected_tools(self.packet(phase))], [latest["id"]])

    def test_native_host_output_limits_are_preserved_by_the_shared_selection(self):
        from agent_learning.antigravity import AntigravityAdapter
        from agent_learning.dsh import DshAdapter
        from tests import test_phase_host_integration as hosts
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                runtime, project = hosts.PhaseHostIntegrationTests.runtime(self, host, "native-tool-" + host)
                configure(runtime, project, {"mode": "phase", "every_phases": 1}, 0)
                native = "native-related-tool-" + host
                send, conversation = hosts.PhaseHostIntegrationTests.host_events(self, runtime, native=native)
                send("start", turn=0)
                task = runtime.task_update(project, conversation, "Inspect a native terminal diagnostic",
                                           scope={"mode": "execute"}, new_task=True)
                send("prompt", turn=1, text=task["goal"])
                if host == "antigravity":
                    adapter = AntigravityAdapter(runtime, self.workspace, "enforce")
                    current = runtime.task(project, conversation)
                    runtime.approve_execution(project, conversation, current["id"], current["write_version"],
                        {"mode": "execute", "allowed_tools": ["run_command"],
                         "tool_rules": {"run_command": {"action": "execute", "path_fields": ["Cwd"]}},
                         "allowed_paths": [str(self.workspace)]}, workspace=str(self.workspace))
                    payload = {"conversationId": native, "workspacePaths": [str(self.workspace)],
                        "modelName": "offline-host-model", "stepIdx": 2,
                        "toolCall": {"name": "run_command", "args": {"CommandLine": "inspect TERM_NATIVE_DIAG",
                                     "Cwd": str(self.workspace)}}}
                    adapter.handle("PreToolUse", payload)
                    # Antigravity currently exposes completion/error, not tool
                    # stdout. A fabricated result field must not become proof.
                    adapter.handle("PostToolUse", {**payload, "error": "", "result": {"exit_code": 0,
                                                    "output": "TERM_NATIVE_DIAG"}})
                elif host == "dsh":
                    adapter = DshAdapter(runtime, self.workspace)
                    payload = {"sessionId": native, "turn": 2, "callId": "native-inspection", "name": "pwsh",
                               "arguments": {"command": "inspect TERM_NATIVE_DIAG"}}
                    adapter.handle("PreToolUse", payload)
                    adapter.handle("PostToolUse", {**payload, "result": {"exit_code": 0, "output": "TERM_NATIVE_DIAG"}})
                else:
                    payload = {"cwd": str(self.workspace), "session_id": native, "turn_id": "2",
                        "tool_use_id": "native-inspection", "tool_name": "exec_command",
                        "tool_input": {"cmd": "inspect TERM_NATIVE_DIAG"}}
                    runtime.handle_hook({**payload, "hook_event_name": "PreToolUse"})
                    runtime.handle_hook({**payload, "hook_event_name": "PostToolUse",
                                         "tool_response": {"exit_code": 0, "output": "TERM_NATIVE_DIAG"}})
                observed = runtime.db.execute("SELECT id,outcome FROM events WHERE conversation=? AND type='PostToolUse'",
                                              (conversation,)).fetchone()
                self.assertIsNotNone(observed)
                hosts.PhaseHostIntegrationTests.deliver(self, runtime, project, conversation)
                send("stop", turn=3, text="TERM_NATIVE_DIAG describes the native terminal observation")
                case = runtime.db.execute("SELECT id FROM problems WHERE project=? AND task_id=? "
                    "AND summary LIKE '阶段公开记录：%'", (project, task["id"])).fetchone()
                self.assertIsNotNone(case)
                packet = runtime.review_packet(project, case[0])
                tools = self.selected_tools(packet)
                if host == "antigravity":
                    self.assertEqual(observed["outcome"], "call_completed")
                    self.assertEqual(tools, [], "missing native stdout cannot be guessed from command input")
                else:
                    self.assertEqual([event["id"] for event in tools], [observed["id"]])
                    self.assertTrue(tools[0]["current_tool_success"])

    def test_optional_selection_failure_rolls_back_refs_and_preserves_public_review(self):
        import sqlite3
        observed = self.tool("selection-fault", "TERM_DIAG_ALPHA was observed")
        def fail_selection(runtime, project, _phase, _task, _stop, cases):
            runtime._attach_case_evidence(cases[0][0]["id"], [(observed["id"], "observation")])
            raise sqlite3.OperationalError("isolated optional association failure")
        with patch("agent_learning.phase_tool_evidence._attach", side_effect=fail_selection), \
                patch("agent_learning.admin.subprocess.Popen") as model:
            phase = self.finish()
        model.assert_not_called()
        self.assertNotEqual(phase["state"], "awaiting_stop")
        packet = self.packet(phase)
        self.assertEqual(self.selected_tools(packet), [])
        self.assertEqual(packet["problem"]["revision"], 1, "partial optional mutation is rolled back")
        self.assertTrue(packet["phase_tool_context"]["selection_unavailable"])
        self.assertFalse(packet["incomplete"])
        self.assertIsNotNone(self.r.db.execute("SELECT id FROM events WHERE id=?", (observed["id"],)).fetchone())
        metric = self.r.db.execute("SELECT value FROM metrics WHERE project=? AND name='phase_tool_context_unavailable'",
                                   (self.p,)).fetchone()
        self.assertEqual(metric[0], 1)
        self.assertIsNotNone(self.r.db.execute("SELECT 1 FROM phase_review_batches WHERE state='pending'").fetchone())

    def test_multiple_tools_keep_one_public_case_and_one_bounded_admin_call(self):
        markers = ["TERM_DIAG_" + str(index) for index in range(6)]
        for index, marker in enumerate(markers):
            self.tool("observation-" + str(index), marker + " was captured")
        phase = self.finish("The terminal observations are " + " ".join(markers))
        packet = self.packet(phase)
        self.assertEqual(len(self.selected_tools(packet)), 3)
        self.assertEqual(packet["phase_tool_context"]["selected_count"], 3)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM problems WHERE category='operation'").fetchone()[0], 0)
        pending = json.loads(self.r.db.execute("SELECT packet FROM phase_review_batches WHERE state='pending'").fetchone()[0])
        self.assertEqual(len(pending["items"]), 1)
        with patch("agent_learning.admin.subprocess.Popen", review_fixtures.reviewer("skip")):
            self.assertEqual(admin_once(self.r, self.p)["status"], "skip")
        with patch("agent_learning.admin.subprocess.Popen") as model:
            admin_once(self.r, self.p)
        model.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_original_tool_selection_recovers_and_replays_exactly_once_after_task_switch(self):
        tool = self.tool("recoverable", "TERM_DIAG_ALPHA is preserved across local capture recovery")
        original = self.r._work_capture_event
        def capture(project, event_id, *args, **kwargs):
            kind = self.r.db.execute("SELECT type FROM events WHERE id=?", (event_id,)).fetchone()[0]
            if kind == "Stop":
                raise OSError("isolated capture interruption")
            return original(project, event_id, *args, **kwargs)
        # Suppress automatic write-side compensation while injecting the fault.
        with patch.object(self.r, "_work_capture_event", side_effect=capture), patch("agent_learning.admin_queue.claim_dispatch", return_value=None):
            phase = self.finish()
        self.assertEqual(phase["state"], "awaiting_stop")
        original_stop = self.r.db.execute("SELECT event_id FROM phase_seal_requests WHERE phase_id=?", (phase["id"],)).fetchone()[0]
        self.begin("A later unrelated task")
        with self.r.transaction():
            prepare(self.r, self.p)
        phase = self.r.db.execute("SELECT * FROM review_phases WHERE id=?", (phase["id"],)).fetchone()
        packet = self.packet(phase)
        self.assertEqual([event["id"] for event in self.selected_tools(packet)], [tool["id"]])
        before = [(row["id"], row["revision"]) for row in self.public_cases(phase)]
        with self.r.transaction():
            seal(self.r, self.p, self.c, original_stop)
            prepare(self.r, self.p)
        self.assertEqual(before, [(row["id"], row["revision"]) for row in self.public_cases(phase)])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='PhaseToolEvidenceSelection'").fetchone()[0], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
