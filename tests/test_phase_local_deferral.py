"""Conservative local deferral through real host callbacks; no real model work."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.phase_review import configure, policy, prepare, status
from tests import test_phase_host_integration as host_fixtures
from tests import test_phase_review as review_fixtures


class PhaseLocalDeferralTests(unittest.TestCase):
    runtime = host_fixtures.PhaseHostIntegrationTests.runtime
    host_events = host_fixtures.PhaseHostIntegrationTests.host_events
    deliver = host_fixtures.PhaseHostIntegrationTests.deliver

    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / "build" / "local" / "phase-local-deferral-tests"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="isolated-", dir=temporary_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"ALP_DSH_REVIEW_PROFILE": ""}).start()

    def setup_host(self, host="codex", suffix=""):
        runtime, project = self.runtime(host, host + suffix)
        configure(runtime, project, {"mode": "phase", "every_phases": 1}, 0)
        send, conversation = self.host_events(runtime, native="routine" + suffix)
        # Antigravity's native start is itself a public PreInvocation prompt.
        # It must carry the same intent as this routine-only fixture.
        send("start", turn=0, text="检查 Agent Learning 连接")
        return runtime, project, send, conversation

    def begin(self, runtime, project, send, conversation, *, goal="检查 Agent Learning 连接", prompt=None):
        task = runtime.task_update(project, conversation, goal, new_task=True)
        send("prompt", turn=1, text=prompt if prompt is not None else goal)
        return task

    def finish(self, runtime, project, send, conversation, *, reply="Agent Learning 连接正常。", turn=2):
        self.deliver(runtime, project, conversation)
        send("stop", turn=turn, text=reply)
        return runtime.db.execute("SELECT * FROM review_phases WHERE project=? AND conversation=? "
            "ORDER BY created DESC LIMIT 1", (project, conversation)).fetchone()

    def pending_packet(self, runtime):
        row = runtime.db.execute("SELECT packet FROM phase_review_batches WHERE state='pending' "
                                 "ORDER BY created DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def test_each_native_host_defers_only_routine_without_calls_or_fake_reviews(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                runtime, project, send, conversation = self.setup_host(host)
                self.begin(runtime, project, send, conversation)
                phase = self.finish(runtime, project, send, conversation)
                self.assertEqual(phase["state"], "routine_deferred")
                self.assertIsNotNone(phase["event_id"], "the original public delivery must remain stored")
                self.assertTrue(runtime.db.execute("SELECT 1 FROM problems p JOIN problem_events pe "
                    "ON pe.problem=p.id WHERE p.project=? AND p.task_id=?", (project, phase["task_id"])).fetchone())
                before = runtime.db.total_changes
                view = status(runtime, project)
                self.assertEqual(view["locally_deferred_phase_count"], 1)
                self.assertEqual(runtime.db.total_changes, before, "viewing status cannot schedule or classify work")
                with patch("agent_learning.admin.subprocess.Popen") as model:
                    admin_once(runtime, project)
                self.assertEqual(model.call_count, 0, "automatic routine work must not launch a reviewer")
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0], 0)
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM review_receipts").fetchone()[0], 0,
                                 "local deferral must not impersonate a reviewed skip")
                send("stop", turn=2, text="Agent Learning 连接正常。")
                send("stop", turn=3, text="Agent Learning 连接正常。")
                self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM review_phases").fetchone()[0], 1)
                self.assertEqual(status(runtime, project)["locally_deferred_phase_count"], 1)

    def test_new_fact_fix_request_and_mixed_scope_are_not_suppressed(self):
        variants = (
            {"reply": "Agent Learning 连接正常。项目部署需要保留同一 ALP_HOME 路径。"},
            {"reply": "Agent Learning 连接正常。已修复数据库连接目录错误。"},
            {"goal": "检查 Agent Learning 连接并修复数据库错误", "prompt": "检查 Agent Learning 连接并修复数据库错误"},
            {"goal": "检查 Agent Learning 连接", "prompt": "检查 Agent Learning 连接，另外记录本项目部署约束"},
            {"goal": "解释连接池在高并发下的性能", "prompt": "检查连接状态，分析连接池的专业优化方法"},
        )
        for number, variant in enumerate(variants):
            with self.subTest(variant=variant):
                runtime, project, send, conversation = self.setup_host(suffix="-mixed-" + str(number))
                values = dict(variant)
                reply = values.pop("reply", "Agent Learning 连接正常。")
                self.begin(runtime, project, send, conversation, **values)
                phase = self.finish(runtime, project, send, conversation, reply=reply)
                self.assertNotEqual(phase["state"], "routine_deferred")
                self.assertEqual(status(runtime, project)["locally_deferred_phase_count"], 0)
                self.assertIsNotNone(self.pending_packet(runtime), "uncertain or substantive work keeps normal review")

    def test_antigravity_native_input_diagnostic_matches_saved_public_request(self):
        from agent_learning.runtime import digest
        runtime, project, send, conversation = self.setup_host("antigravity", "-native-input")
        self.begin(runtime, project, send, conversation)
        metadata = json.loads(runtime.db.execute("SELECT payload FROM events WHERE conversation=? "
            "AND type='AntigravityPublicInput' ORDER BY rowid DESC LIMIT 1", (conversation,)).fetchone()[0])
        request = runtime.db.execute("SELECT payload FROM events WHERE conversation=? "
            "AND type='UserPromptSubmit' ORDER BY rowid DESC LIMIT 1", (conversation,)).fetchone()[0]
        self.assertEqual(metadata["public_input_digest"], digest(json.loads(request)["prompt"]))
        self.assertNotIn("lastUserInput", metadata, "the diagnostic must not duplicate public text")
        self.assertFalse(metadata["public_window"]["available"],
                         "complete native input does not require a transcript response window")
        self.assertEqual(self.finish(runtime, project, send, conversation)["state"], "routine_deferred")

    def test_antigravity_unknown_or_incomplete_input_metadata_keeps_normal_review(self):
        changes = (
            (("public_input_digest",), None),
            (("public_input_digest",), "0" * 64),
            (("host_epoch",), -1),
            (("generation",), True),
            (("capture_coverage",), []),
            (("capture_coverage", "workspace_association"), "unknown_multi_workspace"),
            (("capture_coverage", "trajectory_identity"), "ambiguous"),
            (("capture_coverage", "lastUserInput", "available"), False),
            (("capture_coverage", "lastUserInput", "full_original_available"), False),
            (("capture_coverage", "lastUserInput", "truncated"), True),
            (("capture_coverage", "lastUserInput", "source"), "unrecognized_source"),
        )
        for number, (path, value) in enumerate(changes):
            with self.subTest(path=path, value=value):
                runtime, project, send, conversation = self.setup_host("antigravity", "-coverage-" + str(number))
                self.begin(runtime, project, send, conversation)
                event = runtime.db.execute("SELECT id,payload FROM events WHERE conversation=? "
                    "AND type='AntigravityPublicInput' ORDER BY rowid DESC LIMIT 1", (conversation,)).fetchone()
                body = json.loads(event["payload"])
                target = body
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                runtime.db.execute("UPDATE events SET payload=? WHERE id=?", (json.dumps(body), event["id"]))
                phase = self.finish(runtime, project, send, conversation)
                self.assertNotEqual(phase["state"], "routine_deferred")
                self.assertIsNotNone(self.pending_packet(runtime))

    def test_antigravity_metadata_without_original_public_request_keeps_normal_review(self):
        runtime, project, send, conversation = self.setup_host("antigravity", "-missing-public-input")
        self.begin(runtime, project, send, conversation)
        retained = runtime.db.execute("SELECT payload FROM events WHERE conversation=? "
            "AND type='PhaseRequestContext' ORDER BY rowid DESC LIMIT 1", (conversation,)).fetchone()
        self.assertIsNotNone(retained, "the derived routine label alone must not prove captured input")
        original = json.loads(retained[0])["original_event_id"]
        runtime.db.execute("DELETE FROM events WHERE project=? AND id=?", (project, original))
        phase = self.finish(runtime, project, send, conversation)
        self.assertNotEqual(phase["state"], "routine_deferred")
        self.assertIsNotNone(self.pending_packet(runtime))

    def test_antigravity_mixed_native_request_keeps_normal_review(self):
        runtime, project, send, conversation = self.setup_host("antigravity", "-mixed-native-input")
        self.begin(runtime, project, send, conversation,
                   prompt="检查 Agent Learning 连接，另外记录本项目的部署限制")
        phase = self.finish(runtime, project, send, conversation)
        self.assertNotEqual(phase["state"], "routine_deferred")
        self.assertIsNotNone(self.pending_packet(runtime))

    def test_continuation_alone_does_not_establish_explicit_routine_intent(self):
        runtime, project, send, conversation = self.setup_host(suffix="-continuation")
        self.begin(runtime, project, send, conversation, prompt="继续核对")
        phase = self.finish(runtime, project, send, conversation)
        self.assertNotEqual(phase["state"], "routine_deferred",
                            "a task label and continuation cannot replace a real status request")
        self.assertIsNotNone(self.pending_packet(runtime))

    def test_punctuation_only_stop_is_not_a_verified_routine_reply(self):
        runtime, project, send, conversation = self.setup_host(suffix="-empty-reply")
        self.begin(runtime, project, send, conversation)
        phase = self.finish(runtime, project, send, conversation, reply="。？！；")
        self.assertNotEqual(phase["state"], "routine_deferred")
        self.assertEqual(status(runtime, project)["locally_deferred_phase_count"], 0)

    def test_failed_and_unknown_tool_results_keep_normal_review_path(self):
        for outcome, response in (("failed", {"exit_code": 1, "stderr": "cannot open database"}),
                                  ("unknown", {"output": "No terminal result reported"})):
            with self.subTest(outcome=outcome):
                runtime, project, send, conversation = self.setup_host(suffix="-tool-" + outcome)
                self.begin(runtime, project, send, conversation)
                runtime.handle_hook({"cwd": str(self.workspace), "session_id": "routine-tool-" + outcome,
                    "hook_event_name": "PreToolUse", "turn_id": "tool-turn", "tool_use_id": "inspection-op",
                    "tool_name": "exec_command", "tool_input": {"cmd": "offline connection inspection"}})
                runtime.handle_hook({"cwd": str(self.workspace), "session_id": "routine-tool-" + outcome,
                    "hook_event_name": "PostToolUse", "turn_id": "tool-turn", "tool_use_id": "inspection-op",
                    "tool_name": "exec_command", "tool_input": {"cmd": "offline connection inspection"},
                    "tool_response": response})
                observed = runtime.db.execute("SELECT outcome,task_id FROM events WHERE conversation=? AND type='PostToolUse'",
                                              (conversation,)).fetchone()
                self.assertIsNotNone(observed, "the callback must store a real tool observation")
                self.assertEqual(observed[0], outcome)
                self.assertEqual(observed["task_id"], runtime.task(project, conversation)["id"],
                                 "the real pre/post pair must bind the observation to this task")
                phase = self.finish(runtime, project, send, conversation)
                self.assertNotEqual(phase["state"], "routine_deferred")
                self.assertEqual(status(runtime, project)["locally_deferred_phase_count"], 0)

    def test_unpaired_tool_observation_is_not_silently_omitted_from_routine_window(self):
        runtime, project, send, conversation = self.setup_host(suffix="-unpaired-tool")
        self.begin(runtime, project, send, conversation)
        runtime.handle_hook({"cwd": str(self.workspace), "session_id": "routine-unpaired-tool",
            "hook_event_name": "PostToolUse", "turn_id": "unpaired", "tool_use_id": "missing-pre",
            "tool_name": "exec_command", "tool_response": {"output": "No terminal result reported"}})
        observed = runtime.db.execute("SELECT task_id,outcome FROM events WHERE conversation=? AND type='PostToolUse'",
                                      (conversation,)).fetchone()
        self.assertIsNotNone(observed)
        self.assertIsNone(observed["task_id"], "this fixture exercises the real unpaired tool contract")
        self.assertEqual(observed["outcome"], "unknown")
        phase = self.finish(runtime, project, send, conversation)
        self.assertNotEqual(phase["state"], "routine_deferred",
                            "incomplete tool correlation cannot establish a pure status-only stage")

    def test_truncated_public_observation_cannot_establish_pure_routine_intent(self):
        runtime, project, send, conversation = self.setup_host(suffix="-truncated")
        task = self.begin(runtime, project, send, conversation)
        runtime._event(project, conversation, "UserPromptSubmit", {"prompt": "检查 Agent Learning 连接", "truncated": True},
                       task_id=task["id"], task_revision=task["revision"], outcome="reported")
        phase = self.finish(runtime, project, send, conversation)
        self.assertNotEqual(phase["state"], "routine_deferred")
        self.assertEqual(status(runtime, project)["locally_deferred_phase_count"], 0)

    def test_independent_case_keeps_routine_named_delivery_on_normal_path(self):
        runtime, project, send, conversation = self.setup_host(suffix="-independent")
        task = self.begin(runtime, project, send, conversation)
        prompt = runtime.db.execute("SELECT id FROM events WHERE conversation=? AND type='UserPromptSubmit' "
                                    "ORDER BY created DESC LIMIT 1", (conversation,)).fetchone()[0]
        problem = runtime._problem(project, conversation, "Project uses a dedicated shared knowledge directory",
                                   "project", [prompt], task_binding=task)
        phase = self.finish(runtime, project, send, conversation)
        self.assertNotEqual(phase["state"], "routine_deferred")
        packet = self.pending_packet(runtime)
        self.assertIn(problem["id"], {item["problem"]["id"] for item in packet["items"]})

    def test_late_feedback_wakes_retained_phase_without_another_delivery(self):
        runtime, project, send, conversation = self.setup_host(suffix="-late-feedback")
        task = self.begin(runtime, project, send, conversation)
        phase = self.finish(runtime, project, send, conversation)
        self.assertEqual(phase["state"], "routine_deferred")
        feedback = runtime.feedback(project, conversation, "The status check exposed a project routing constraint",
            category="project", event_refs=[phase["event_id"]], resolution="Preserve the shared project home",
            expected_task_id=task["id"], expected_task_revision=task["revision"])
        with runtime.transaction():
            prepare(runtime, project)
        current = runtime.db.execute("SELECT state FROM review_phases WHERE id=?", (phase["id"],)).fetchone()[0]
        self.assertNotEqual(current, "routine_deferred")
        packet = self.pending_packet(runtime)
        self.assertIsNotNone(packet)
        self.assertIn(feedback["id"], {item["problem"]["id"] for item in packet["items"]})
        self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM review_phases").fetchone()[0], 1)
        self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_late_independent_case_reactivates_old_routine_material(self):
        runtime, project, send, conversation = self.setup_host(suffix="-late-case")
        task = self.begin(runtime, project, send, conversation)
        phase = self.finish(runtime, project, send, conversation)
        self.assertEqual(phase["state"], "routine_deferred")
        problem = runtime._problem(project, conversation, "Keep independent project facts from the inspection",
                                   "project", [phase["event_id"]], task_binding=task)
        with runtime.transaction():
            prepare(runtime, project)
        self.assertIn(problem["id"], {item["problem"]["id"] for item in self.pending_packet(runtime)["items"]})
        self.assertEqual(status(runtime, project)["locally_deferred_phase_count"], 0)

    def test_explicit_force_can_review_routine_but_cannot_spend_past_token_cap(self):
        runtime, project, send, conversation = self.setup_host(suffix="-force")
        self.begin(runtime, project, send, conversation)
        phase = self.finish(runtime, project, send, conversation)
        self.assertEqual(phase["state"], "routine_deferred")
        with runtime.transaction():
            prepare(runtime, project, force=True)
        packet = self.pending_packet(runtime)
        self.assertIsNotNone(packet, "an explicit review request can include retained routine evidence")
        self.assertEqual(packet["phase_ids"], [phase["id"]])
        with patch("agent_learning.admin.subprocess.Popen", review_fixtures.reviewer("skip")):
            result = admin_once(runtime, project)
        self.assertEqual(result["status"], "skip", result)
        self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        with runtime.transaction():
            prepare(runtime, project, force=True)
        with patch("agent_learning.admin.subprocess.Popen") as model:
            admin_once(runtime, project)
        self.assertEqual(model.call_count, 0, "the already-reviewed routine source cannot be relaunched")
        # A separate local fixture applies the cap before any forced plan. A
        # budget change does not imply permission to preserve a stale plan.
        runtime, project, send, conversation = self.setup_host(suffix="-force-capped")
        configure(runtime, project, {"daily_tokens": 1}, policy(runtime, project)["version"])
        self.begin(runtime, project, send, conversation)
        self.finish(runtime, project, send, conversation)
        with runtime.transaction():
            prepare(runtime, project, force=True)
        with patch("agent_learning.admin.subprocess.Popen") as model:
            admin_once(runtime, project)
        self.assertEqual(model.call_count, 0, "the project token cap still applies to explicit review")
        self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_force_never_reopens_routine_material_from_an_old_account_boundary(self):
        runtime, project, send, conversation = self.setup_host(suffix="-epoch")
        self.begin(runtime, project, send, conversation)
        phase = self.finish(runtime, project, send, conversation)
        self.assertEqual(phase["state"], "routine_deferred")
        runtime.account_changed(notification_id="real-operator-account-change")
        with runtime.transaction():
            prepare(runtime, project, force=True)
        self.assertIsNone(self.pending_packet(runtime))
        self.assertEqual(runtime.db.execute("SELECT state FROM review_phases WHERE id=?", (phase["id"],)).fetchone()[0],
                         "routine_deferred", "force must not borrow evidence across governance or session epochs")
        self.assertEqual(status(runtime, project)["historical_boundary_phases"], 1)

    def test_explicit_evidence_wait_remains_a_reviewed_wait_not_a_new_local_deferral(self):
        runtime, project, send, conversation = self.setup_host(suffix="-reviewed-wait")
        self.begin(runtime, project, send, conversation)
        phase = self.finish(runtime, project, send, conversation)
        self.assertEqual(phase["state"], "routine_deferred")
        with runtime.transaction():
            prepare(runtime, project, force=True)
        with patch("agent_learning.admin.subprocess.Popen", review_fixtures.reviewer("needs_evidence")):
            result = admin_once(runtime, project)
        self.assertEqual(result["status"], "needs_evidence", result)
        view = status(runtime, project)
        self.assertEqual(view["locally_deferred_phase_count"], 0,
                         "an actual evidence-wait receipt supersedes local routine triage")
        self.assertEqual(view["phases"].get("partial"), 1)
        self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        with patch("agent_learning.admin.subprocess.Popen") as model:
            admin_once(runtime, project)
            admin_once(runtime, project)
        self.assertEqual(model.call_count, 0, "unchanged needs-evidence material must remain dormant")
        self.assertEqual(status(runtime, project)["locally_deferred_phase_count"], 0)

    def test_full_normal_batch_takes_priority_and_retains_routine_for_later(self):
        runtime, project, send, conversation = self.setup_host(suffix="-priority")
        self.begin(runtime, project, send, conversation)
        routine = self.finish(runtime, project, send, conversation, turn=30)
        self.assertEqual(routine["state"], "routine_deferred")
        runtime.config["admin"]["max_input_chars"] = 64000
        normal_send, normal_conversation = self.host_events(runtime, native="normal-with-many-cases")
        normal_send("start", turn=0)
        task = self.begin(runtime, project, normal_send, normal_conversation,
                          goal="Deliver independent professional project conventions")
        prompt = runtime.db.execute("SELECT id FROM events WHERE conversation=? AND type='UserPromptSubmit' "
                                    "ORDER BY created DESC LIMIT 1", (normal_conversation,)).fetchone()[0]
        for number in range(7):
            runtime._problem(project, normal_conversation, "Independent reusable project fact number " + str(number),
                             "project", [prompt], task_binding=task)
        normal = self.finish(runtime, project, normal_send, normal_conversation,
                             reply="Delivered independent professional conventions with original evidence")
        packet = self.pending_packet(runtime)
        self.assertIsNotNone(packet)
        self.assertEqual(len(packet["items"]), 8)
        self.assertTrue(all(item["problem"]["task_id"] == normal["task_id"] for item in packet["items"]),
                        "old routine observations cannot displace a ready professional source")
        current = runtime.db.execute("SELECT state FROM review_phases WHERE id=?", (routine["id"],)).fetchone()[0]
        self.assertEqual(current, "routine_deferred", "budget leftovers must retain their original local state")
        with patch("agent_learning.admin.subprocess.Popen", review_fixtures.reviewer("skip")):
            result = admin_once(runtime, project)
        self.assertEqual(result["status"], "skip", result)
        with patch("agent_learning.admin.subprocess.Popen") as model:
            admin_once(runtime, project)
            admin_once(runtime, project)
        self.assertEqual(model.call_count, 0, "the unreviewed routine cannot launch after real work is settled")
        self.assertEqual(status(runtime, project)["locally_deferred_phase_count"], 1)

    def test_partial_routine_leftover_cannot_start_its_own_automatic_review(self):
        runtime, project, send, conversation = self.setup_host(suffix="-partial-routine")
        runtime.config["admin"]["max_input_chars"] = 64000
        self.begin(runtime, project, send, conversation)
        for turn in (2, 3, 4):
            send("stop", turn=turn, text="Agent Learning 连接正常。")
            send("prompt", turn=turn + 10, text="检查 Agent Learning 连接")
        routine = self.finish(runtime, project, send, conversation, turn=30)
        self.assertEqual(routine["state"], "routine_deferred")
        source_cases = runtime.db.execute("SELECT COUNT(*) FROM problems WHERE task_id=? "
            "AND summary LIKE '阶段公开记录：%'", (routine["task_id"],)).fetchone()[0]
        self.assertGreater(source_cases, 1, "the native multi-reply stage must really create multiple source chunks")
        normal_send, normal_conversation = self.host_events(runtime, native="normal-before-routine-tail")
        normal_send("start", turn=0)
        task = self.begin(runtime, project, normal_send, normal_conversation, goal="Deliver project facts before routine tail")
        prompt = runtime.db.execute("SELECT id FROM events WHERE conversation=? AND type='UserPromptSubmit' "
                                    "ORDER BY created DESC LIMIT 1", (normal_conversation,)).fetchone()[0]
        for number in range(6):
            runtime._problem(project, normal_conversation, "Independent professional constraint " + str(number),
                             "project", [prompt], task_binding=task)
        self.finish(runtime, project, normal_send, normal_conversation, reply="Delivered the independent project constraints")
        packet = self.pending_packet(runtime)
        self.assertEqual(len(packet["items"]), 8)
        self.assertEqual(sum(item["problem"]["task_id"] == routine["task_id"] for item in packet["items"]), 1)
        with patch("agent_learning.admin.subprocess.Popen", review_fixtures.reviewer("skip")):
            result = admin_once(runtime, project)
        self.assertEqual(result["status"], "skip", result)
        self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        with patch("agent_learning.admin.subprocess.Popen") as model:
            admin_once(runtime, project)
            admin_once(runtime, project)
        self.assertEqual(model.call_count, 0, "a remaining routine chunk needs later useful work or explicit review")
        self.assertEqual(status(runtime, project)["locally_deferred_phase_count"], 1)

    def test_small_character_budget_keeps_unincluded_routine_locally_deferred(self):
        from agent_learning.phase_review import instruction
        runtime, project, send, conversation = self.setup_host(suffix="-char-priority")
        runtime.config["admin"]["max_input_chars"] = 64000
        self.begin(runtime, project, send, conversation)
        routine = self.finish(runtime, project, send, conversation)
        normal_send, normal_conversation = self.host_events(runtime, native="normal-char-budget")
        normal_send("start", turn=0)
        self.begin(runtime, project, normal_send, normal_conversation, goal="Deliver professional constraints")
        with patch("agent_learning.phase_review.prepare"):
            normal = self.finish(runtime, project, normal_send, normal_conversation,
                reply="The project release constraint uses a dedicated isolated runtime directory. " * 7)
        # Measure the current complete wrapper rather than rely on a historical
        # fixed cap that transport/schema improvements may make spacious again.
        runtime.db.execute("SAVEPOINT measure_normal_character_budget")
        try:
            prepare(runtime, project)
            measured = self.pending_packet(runtime)
            self.assertEqual(len(measured["items"]), 2)
            single = json.loads(json.dumps(measured))
            single["items"] = [item for item in single["items"]
                               if item["problem"]["task_id"] == normal["task_id"]]
            single["phase_ids"] = [normal["id"]]
            maximum = len(instruction(single)) + 128
            self.assertLess(maximum, len(instruction(measured)), "the routine source must exceed the measured cap")
        finally:
            runtime.db.execute("ROLLBACK TO measure_normal_character_budget")
            runtime.db.execute("RELEASE measure_normal_character_budget")
        runtime.config["admin"]["max_input_chars"] = maximum
        with runtime.transaction():
            prepare(runtime, project)
        packet = self.pending_packet(runtime)
        self.assertIsNotNone(packet, "a small budget must still admit the available normal source")
        self.assertEqual(len(packet["items"]), 1)
        self.assertEqual(packet["items"][0]["problem"]["task_id"], normal["task_id"])
        self.assertLessEqual(len(instruction(packet)), maximum)
        self.assertEqual(packet["phase_ids"], [normal["id"]], "an unincluded routine stage is not part of this batch")
        request = runtime.db.execute("SELECT e.payload FROM problem_events pe JOIN events e ON e.id=pe.event "
            "WHERE pe.problem=? AND e.type='PhaseReviewRequested'", (packet["problem"]["id"],)).fetchone()
        self.assertIsNotNone(request)
        self.assertEqual(json.loads(request[0])["phase_ids"], packet["phase_ids"],
                         "the durable request and final batch must describe the same selected stages")
        self.assertEqual(runtime.db.execute("SELECT state FROM review_phases WHERE id=?", (routine["id"],)).fetchone()[0],
                         "routine_deferred")

    def test_normal_phase_coalesces_routine_with_spare_capacity_and_keeps_both_sources(self):
        runtime, project, send, conversation = self.setup_host(suffix="-coalesce")
        runtime.config["admin"]["max_input_chars"] = 64000
        self.begin(runtime, project, send, conversation)
        routine = self.finish(runtime, project, send, conversation)
        normal_send, normal_conversation = self.host_events(runtime, native="normal-coalesced")
        normal_send("start", turn=0)
        self.begin(runtime, project, normal_send, normal_conversation, goal="Record a reusable project release convention")
        normal = self.finish(runtime, project, normal_send, normal_conversation,
                             reply="Record the project release convention with its original public evidence")
        packet = self.pending_packet(runtime)
        self.assertIsNotNone(packet)
        self.assertEqual(set(packet["phase_ids"]), {normal["id"], routine["id"]})
        task_ids = [item["problem"]["task_id"] for item in packet["items"]]
        self.assertEqual(task_ids[0], normal["task_id"], "normal material must own the first budget slots")
        self.assertIn(routine["task_id"], task_ids[1:])
        with patch("agent_learning.admin.subprocess.Popen", review_fixtures.reviewer("skip")):
            result = admin_once(runtime, project)
        self.assertEqual(result["status"], "skip", result)
        self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        self.assertTrue(runtime.db.execute("SELECT 1 FROM review_receipts r JOIN problems p ON p.id=r.problem "
            "WHERE p.task_id=?", (routine["task_id"],)).fetchone(), "a real model receipt can settle retained routine")
        with patch("agent_learning.admin.subprocess.Popen") as model:
            admin_once(runtime, project)
            admin_once(runtime, project)
        self.assertEqual(model.call_count, 0, "settled routine material must not produce another automatic call")

    def test_one_routine_and_one_normal_delivery_preserve_every_two_frequency(self):
        runtime, project, send, conversation = self.setup_host(suffix="-frequency")
        configure(runtime, project, {"every_phases": 2}, policy(runtime, project)["version"])
        self.begin(runtime, project, send, conversation)
        routine = self.finish(runtime, project, send, conversation)
        self.assertIsNone(self.pending_packet(runtime))
        normal_send, normal_conversation = self.host_events(runtime, native="normal-second-delivery")
        normal_send("start", turn=0)
        self.begin(runtime, project, normal_send, normal_conversation, goal="Retain a genuine project build constraint")
        normal = self.finish(runtime, project, normal_send, normal_conversation,
                             reply="The project build constraint is described in this delivered stage")
        packet = self.pending_packet(runtime)
        self.assertIsNotNone(packet, "two genuine deliveries still satisfy the configured frequency")
        self.assertEqual(set(packet["phase_ids"]), {routine["id"], normal["id"]})
        self.assertEqual(policy(runtime, project)["every_phases"], 2)

    def test_different_current_models_do_not_borrow_routine_sources(self):
        runtime, project, send, conversation = self.setup_host(suffix="-model-boundary")
        runtime.admin_configure(project, True, "host", "current", "", expected_policy_version=0)
        runtime.admin_observe_model(project, conversation, "review-model-a")
        self.begin(runtime, project, send, conversation)
        routine = self.finish(runtime, project, send, conversation)
        normal_send, normal_conversation = self.host_events(runtime, native="other-model-conversation")
        normal_send("start", turn=0)
        runtime.admin_observe_model(project, normal_conversation, "review-model-b")
        self.begin(runtime, project, normal_send, normal_conversation, goal="Deliver a fact for the currently selected model")
        normal = self.finish(runtime, project, normal_send, normal_conversation,
                             reply="This delivered project fact keeps its actual review model")
        packet = self.pending_packet(runtime)
        self.assertIsNotNone(packet)
        self.assertEqual(packet["phase_ids"], [normal["id"]])
        self.assertNotIn(routine["task_id"], {item["problem"]["task_id"] for item in packet["items"]})

    def test_different_host_does_not_absorb_another_hosts_deferred_delivery(self):
        codex, project = self.runtime("codex", shared=True)
        configure(codex, project, {"mode": "phase", "every_phases": 1}, 0)
        send, conversation = self.host_events(codex, native="codex-only-routine")
        send("start", turn=0)
        self.begin(codex, project, send, conversation)
        routine = self.finish(codex, project, send, conversation)
        dsh, same_project = self.runtime("dsh", shared=True)
        self.assertEqual(project, same_project)
        configure(dsh, project, {"mode": "phase", "every_phases": 1}, 0)
        normal_send, normal_conversation = self.host_events(dsh, native="dsh-only-delivery")
        normal_send("start", turn=0)
        self.begin(dsh, project, normal_send, normal_conversation, goal="Deliver a DSH project fact")
        normal = self.finish(dsh, project, normal_send, normal_conversation, reply="Delivered DSH fact with original evidence")
        packet = self.pending_packet(dsh)
        self.assertIsNotNone(packet)
        self.assertEqual(packet["phase_ids"], [normal["id"]])
        self.assertEqual(status(codex, project)["locally_deferred_phase_count"], 1)
        self.assertNotIn(routine["id"], packet["phase_ids"])

    def test_unready_normal_stage_cannot_launch_a_routine_only_batch(self):
        runtime, project, send, conversation = self.setup_host(suffix="-unready")
        self.begin(runtime, project, send, conversation)
        routine = self.finish(runtime, project, send, conversation)
        normal_send, normal_conversation = self.host_events(runtime, native="normal-needs-evidence")
        normal_send("start", turn=0)
        task = self.begin(runtime, project, normal_send, normal_conversation, goal="A professional claim requiring new evidence")
        original_packet = runtime.review_packet

        def unavailable_normal_packet(*args, **kwargs):
            packet = original_packet(*args, **kwargs)
            if packet["problem"].get("task_id") == task["id"]:
                packet["incomplete"] = True
            return packet

        with patch.object(runtime, "review_packet", side_effect=unavailable_normal_packet):
            self.finish(runtime, project, normal_send, normal_conversation, reply="The professional result needs more evidence")
            with runtime.transaction():
                prepare(runtime, project)
            with patch("agent_learning.admin.subprocess.Popen") as model:
                admin_once(runtime, project)
            self.assertEqual(model.call_count, 0, "routine evidence cannot initiate an unready batch")
        packet = self.pending_packet(runtime)
        self.assertFalse(packet and packet["items"], "an unready professional stage cannot justify a routine-only model call")
        self.assertEqual(runtime.db.execute("SELECT state FROM review_phases WHERE id=?", (routine["id"],)).fetchone()[0],
                         "routine_deferred")


if __name__ == "__main__":
    unittest.main()
