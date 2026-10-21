"""Recovery boundaries: bounded supplementation, actual host dispatch and child lifecycles."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.runtime import Runtime
from tests.test_module6_queue import fake_reviewer


class RecoveryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "checkout"
        self.workspace.mkdir()
        self.home = Path(self.temp.name) / "state"
        self.r = self.runtime()
        self.p = self.r.project(self.workspace)

    def runtime(self, host="codex", config=None):
        runtime = Runtime(self.home, config=config or {}, host=host)
        self.addCleanup(runtime.close)
        return runtime

    def capture(self, text, *, runtime=None, kind="Stop", conversation="chat"):
        runtime = runtime or self.r
        conversation = runtime.host_conversation(conversation)
        payload = {"prompt" if kind == "UserPromptSubmit" else "last_assistant_message": text}
        with runtime.transaction():
            runtime.bind_session(self.p, conversation)
            event, _ = runtime._event(self.p, conversation, kind, payload, outcome="reported")
            runtime._work_capture_event(self.p, event["id"], self.workspace, raw=payload)
        return event


class SummarySupplementRecoveryTests(RecoveryFixture):
    def setUp(self):
        super().setUp()
        self.r.config["admin"].update(enabled=True, command=[sys.executable], timeout_seconds=10,
                                      max_input_chars=16000, max_calls_per_day=100)
        self.r.config["work_memory"].update(summary_min_events=2, summary_max_events=2,
                                            summary_strategy="all_windows")
        self.r.configure_work_summary(self.p, True)

    def queue(self):
        return self.r._work_queue_summary(self.p, "chat", self.workspace)

    def review(self, window, decision):
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer(decision)) as model:
            result = admin_once(self.r, self.p, window["problem_id"])
        self.assertEqual(model.call_count, 1, result)
        self.assertEqual(result["status"], decision, result)
        return result

    def state(self, window):
        return self.r.db.execute("SELECT state FROM work_summary_links WHERE problem_id=?",
                                 (window["problem_id"],)).fetchone()[0]

    def initial(self):
        self.capture("Documented optical calibration requirements.", kind="UserPromptSubmit")
        self.capture("Public analysis of units; hardware confirmation pending.")
        window = self.queue()
        self.review(window, "needs_evidence")
        return window

    def test_full_window_reserves_new_evidence_and_preserves_omission_coverage(self):
        old = self.initial()
        supplement = self.capture("Supplementary actual calibration units and measured result.")
        replacement = self.queue()
        self.assertEqual(replacement["status"], "pending", replacement)
        self.assertEqual(replacement["source_count"], 2)
        self.assertEqual(replacement["omitted_previous_sources"], 1)
        self.assertEqual(self.state(old), "superseded")
        packet = self.r.work_summary_packet(self.p, replacement["problem_id"])
        self.assertIn(supplement["id"], {e["id"] for e in packet["events"]})
        self.assertNotEqual(packet["events"][0]["id"], supplement["id"])
        self.assertEqual(packet["events"][-1]["id"], supplement["id"])
        self.assertEqual(packet["supplementation"]["omitted_previous_sources"], 1)
        accepted = self.review(replacement, "candidate")
        self.assertEqual(accepted["continuation"]["status"], "insufficient_delta")
        self.assertEqual(self.queue()["status"], "insufficient_delta")
        self.assertEqual(self.r.work_memory_status(self.p)["model_summaries"], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_memories WHERE event_id IS NOT NULL").fetchone()[0], 3)
        body = json.loads(self.r.db.execute("SELECT body FROM work_memories WHERE id=?", (accepted["memory_id"],)).fetchone()[0])
        self.assertEqual(body["coverage"]["supplementation"]["omitted_previous_sources"], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0], 0)

    def test_single_source_limit_can_advance_without_requeuing_replaced_window(self):
        self.r.config["work_memory"].update(summary_min_events=1, summary_max_events=1)
        self.capture("Initial observation requiring confirmation.")
        old = self.queue()
        self.review(old, "needs_evidence")
        new = self.capture("New measured confirmation provided.")
        replacement = self.queue()
        self.assertEqual(replacement["source_count"], 1)
        packet = self.r.work_summary_packet(self.p, replacement["problem_id"])
        self.assertEqual([e["id"] for e in packet["events"]], [new["id"]])
        accepted = self.review(replacement, "candidate")
        self.assertEqual(accepted["continuation"]["status"], "insufficient_delta")
        self.assertEqual(self.state(old), "superseded")

    def test_multiple_supplement_rounds_wait_without_repeated_calls_until_new_source(self):
        first = self.initial()
        self.assertEqual(self.queue()["status"], "awaiting_evidence")
        self.capture("First supplementary observation; still awaiting measurement.")
        second = self.queue()
        self.review(second, "needs_evidence")
        self.assertEqual(self.queue()["status"], "awaiting_evidence")
        self.capture("Second supplementary actual hardware measurement.")
        third = self.queue()
        accepted = self.review(third, "candidate")
        self.assertEqual(accepted["continuation"]["status"], "insufficient_delta")
        self.assertEqual(self.state(first), "superseded")
        self.assertEqual(self.state(second), "superseded")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 3)

    def test_oversize_new_evidence_does_not_consume_waiting_window(self):
        old = self.initial()
        self.r.config["admin"]["max_input_chars"] = 4000
        self.capture("Supplement requiring the entire quoted observation: " + "q" * 1300,
                     kind="UserPromptSubmit")
        blocked = self.queue()
        self.assertEqual(blocked["status"], "input_budget_exceeded", blocked)
        self.assertGreater(blocked["required_max_input_chars"], 4000)
        self.assertEqual(self.state(old), "awaiting_evidence")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)


class SchedulerScopeRecoveryTests(RecoveryFixture):
    def setUp(self):
        super().setUp()
        self.r.config["work_memory"]["summary_strategy"] = "all_windows"
        self.r.config["scheduler"]["enabled"] = True
        self.r.config["admin"].update(command=[sys.executable], max_input_chars=16000,
                                      max_calls_per_day=100, max_calls_per_batch=1, timeout_seconds=1)
        self.r.admin_configure(self.p, True, provider="codex", model_mode="default", command=[sys.executable])
        self.dsh = self.runtime("dsh", config={"scheduler": {"enabled": True}, "admin": {
            "command": [sys.executable], "max_input_chars": 16000, "max_calls_per_day": 100,
            "max_calls_per_batch": 1, "timeout_seconds": 1}})
        # Explicit deterministic launcher avoids invoking either platform CLI.
        self.dsh.admin_configure(self.p, True, provider="codex", model_mode="default", command=[sys.executable])

    def window(self, runtime, conversation):
        runtime.config["work_memory"]["summary_strategy"] = "all_windows"
        runtime.configure_work_summary(self.p, True)
        self.capture("Requirements for " + conversation, runtime=runtime, conversation=conversation, kind="UserPromptSubmit")
        self.capture("Visible result for " + conversation, runtime=runtime, conversation=conversation)
        return runtime._work_queue_summary(self.p, runtime.host_conversation(conversation), self.workspace)

    def test_enabled_but_idle_current_host_dispatches_other_ready_summary(self):
        self.window(self.dsh, "remote")
        self.assertEqual(self.r._scheduler_eval_admin_for_scope(self.p, self.r._scheduler_config(), "codex:default")[1], "idle")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("candidate")) as model:
            tick = self.r.scheduler_tick(self.p, max_jobs=1, max_seconds=3)
        self.assertEqual(model.call_count, 1, tick)
        self.assertEqual(tick["items"][0]["host_scope"], "dsh:default")
        plan = json.loads(self.r.db.execute("SELECT body FROM admin_execution_plans").fetchone()[0])
        self.assertEqual(plan["host_scope"], "dsh:default")
        self.assertEqual(self.r._session_scope(), "codex:default")
        self.assertEqual(self.r.work_memory_status(self.p)["model_summaries"], 1)

    def test_claimed_scope_is_not_replaced_when_policy_changes_before_dispatch(self):
        self.window(self.dsh, "remote")
        original, changed = self.r.assert_host_epoch, []
        def change_after_claim(expected=None):
            row = self.r.db.execute("SELECT state FROM scheduler_jobs WHERE project=? AND kind='admin'", (self.p,)).fetchone()
            if row and row[0] == "running" and not changed:
                changed.append(True)
                self.dsh.admin_configure(self.p, False, provider="codex", model_mode="default", command=[sys.executable])
            return original(expected)
        with patch.object(self.r, "assert_host_epoch", side_effect=change_after_claim), \
                patch("agent_learning.admin.subprocess.Popen") as model:
            tick = self.r.scheduler_tick(self.p, max_jobs=1, max_seconds=3)
        model.assert_not_called()
        self.assertTrue(changed)
        self.assertEqual(tick["items"][0]["status"], "state_changed")
        self.assertEqual(tick["items"][0]["host_scope"], "dsh:default")
        self.assertEqual(self.r._session_scope(), "codex:default")

    def test_dispatch_exception_restores_owner_scope_and_records_selected_host(self):
        self.window(self.dsh, "remote")
        with patch("agent_learning.admin.admin_batch", side_effect=RuntimeError("simulated worker failure")):
            tick = self.r.scheduler_tick(self.p, max_jobs=1, max_seconds=3)
        self.assertEqual(tick["items"][0]["status"], "failed")
        self.assertEqual(tick["items"][0]["host_scope"], "dsh:default")
        self.assertEqual(self.r._session_scope(), "codex:default")
        self.assertIsNone(self.r.db.execute("SELECT owner FROM scheduler_jobs WHERE kind='admin'").fetchone()[0])

    def test_ready_hosts_rotate_after_a_completed_batch(self):
        self.window(self.r, "local-first")
        self.window(self.dsh, "remote")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("candidate")) as model:
            first = self.r.scheduler_tick(self.p, max_jobs=1, max_seconds=3)
            self.window(self.r, "local-second")
            second = self.r.scheduler_tick(self.p, max_jobs=1, max_seconds=3)
        self.assertEqual(model.call_count, 2, (first, second))
        self.assertEqual(first["items"][0]["host_scope"], "codex:default")
        self.assertEqual(second["items"][0]["host_scope"], "dsh:default")


class OperationLifecycleRecoveryTests(RecoveryFixture):
    def hook(self, kind, **fields):
        return self.r.handle_hook({"hook_event_name": kind, "session_id": "chat", "cwd": str(self.workspace), **fields})

    def start(self, ident, *, agent=None, running=False):
        values = {"tool_use_id": ident, "tool_name": "Bash", "tool_input": {"cmd": ident}}
        if agent:
            values["agent_id"] = agent
        self.hook("PreToolUse", **values)
        if running:
            self.hook("PostToolUse", **values, tool_response={"session_id": ident, "exit_code": None})

    def states(self):
        return {json.loads(row["payload"])["tool_input"]["cmd"]: row["state"] for row in self.r.db.execute(
            "SELECT o.state,e.payload FROM operations o JOIN events e ON e.id=o.last_event")}

    def test_root_stop_only_retires_root_unacknowledged_operations(self):
        self.start("root-started")
        self.start("root-running", running=True)
        self.start("child-started", agent="child")
        self.start("child-running", agent="child", running=True)
        self.hook("Stop", turn_id="root-stop", last_assistant_message="Parent turn finished.")
        self.assertEqual(self.states(), {"root-started": "unknown", "root-running": "running",
                                         "child-started": "started", "child-running": "running"})

    def test_root_session_start_does_not_terminate_children_or_confirmed_background_operations(self):
        for source in ("startup", "resume", "clear"):
            with self.subTest(source=source):
                self.start(source + "-root")
                self.start(source + "-child", agent="child")
                self.start(source + "-background", running=True)
                self.hook("SessionStart", source=source)
                state = self.states()
                self.assertEqual(state[source + "-root"], "unknown")
                self.assertEqual(state[source + "-child"], "started")
                self.assertEqual(state[source + "-background"], "running")

    def test_subagent_stop_only_retires_that_agents_operations(self):
        self.start("root")
        self.start("child-a", agent="child-a")
        self.start("child-a-background", agent="child-a", running=True)
        self.start("child-b", agent="child-b")
        self.hook("SubagentStop", agent_id="child-a")
        self.assertEqual(self.states(), {"root": "started", "child-a": "unknown",
                                         "child-a-background": "unknown", "child-b": "started"})


if __name__ == "__main__":
    unittest.main()
