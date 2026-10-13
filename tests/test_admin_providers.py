"""宿主管理员协议的隔离检查；假 CLI 不调用模型或真实宿主配置。"""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_learning.admin import RESPONSE_SCHEMA, admin_once
from agent_learning.admin_providers import (antigravity_usage, build_execution, collect_antigravity_stream,
                                           executor_availability, read_response, validate_command, validate_model)
from agent_learning.runtime import ContractError, Runtime
from tests.test_module6_queue import fake_reviewer


def stream_events(response=None, **values):
    result = {"status": "SUCCESS", "num_turns": 1, "structured_output": response or
              {"decision": "skip", "reason": "Controlled review", "candidate": None, "missing_evidence": [], "proposal": None},
              "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 80, "total_tokens": 120}}
    result.update(values)
    return [{"event": "init", "init": {"tools": [], "permission_mode": "request-review", "agent": "alp-review"}},
            {"event": "result", "result": result}]


class ProviderProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-provider-protocol-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.schema = self.directory / "schema.json"
        self.schema.write_text(json.dumps(RESPONSE_SCHEMA), encoding="utf-8")
        self.output = self.directory / "output.json"
        self.log = self.directory / "events.log"

    def write_events(self, events):
        logged = []
        for event in events:
            logged.append(event)
            if event.get("event") == "init":
                logged.append({"event": "adapter_input_sent"})
        self.log.write_text("".join(json.dumps(event) + "\n" for event in logged), encoding="utf-8")

    def build(self, provider="antigravity", model="", mode="default"):
        return build_execution(["fake-cli"], provider, model, self.directory, self.schema, self.output,
                               "Private evidence with quoted \"text\" and newline\n", 7, selection_mode=mode)

    def test_antigravity_uses_single_stdin_event_and_no_prompt_arguments(self):
        execution = self.build()
        self.assertNotIn("Private evidence", " ".join(execution["args"]))
        self.assertNotIn("--model", execution["args"])
        self.assertNotIn("--dangerously-skip-permissions", execution["args"])
        message = json.loads(execution["stdin_data"])
        self.assertEqual(message["event"], "user")
        self.assertEqual(message["message"]["content"], "Private evidence with quoted \"text\" and newline\n")
        self.assertEqual(execution["stdin_data"].count(b"\n"), 1)
        agent = (self.directory / ".agents/agents/alp-review.md").read_text(encoding="utf-8")
        for value in ("tools: []", "mcpServers: []", "skills: []", "plugins: []", 'commandExecutionPolicy: "off"', "subagent: false"):
            self.assertIn(value, agent)

    def test_explicit_model_and_codex_contract_compatibility(self):
        execution = self.build("codex", "gpt-test", "fixed")
        self.assertIn("--ignore-user-config", execution["args"])
        self.assertEqual(execution["args"][execution["args"].index("--model") + 1], "gpt-test")
        self.assertEqual(execution["args"][execution["args"].index("--output-last-message") + 1], str(self.output))
        self.assertEqual(execution["args"][-1], "-")
        self.assertTrue(execution["stdin_data"].startswith(b"Private evidence"))
        ag = self.build("antigravity", "gemini-test", "current")
        self.assertEqual(ag["args"][ag["args"].index("--model") + 1], "gemini-test")

    def test_invalid_model_and_resume_or_bypass_command_rejected(self):
        for model in ("--bad", "bad model", "x\n--flag", None, "x" * 201):
            with self.assertRaises(ContractError):
                validate_model(model)
        for flag in ("--continue", "--conversation=old", "--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox", "resume",
                     "--yolo", "-c", "--model=gpt-test", "--agent=other", "--sandbox=danger-full-access", "--output-format=text"):
            with self.assertRaises(ContractError):
                validate_command(["fake-cli", flag])

    def test_only_terminal_structured_response_is_accepted(self):
        self.write_events(stream_events())
        response = read_response("antigravity", self.output, self.log)
        self.assertEqual(response["decision"], "skip")
        for values in ({"status": "ERROR"}, {"status": "RUNNING"}, {"denied_actions": ["command"]},
                       {"error": "failed"}, {"num_turns": 2}, {"num_turns": True}, {"num_turns": 1.0}, {"structured_output": None}):
            self.write_events(stream_events(**values))
            with self.assertRaises(ContractError):
                read_response("antigravity", self.output, self.log)

    def test_unverified_tools_duplicate_terminal_and_tool_steps_rejected(self):
        for mutation in ("tools", "permission", "duplicate", "tool", "no_init", "malformed"):
            events = stream_events()
            if mutation == "tools":
                events[0]["init"]["tools"] = ["run_command"]
            elif mutation == "permission":
                events[0]["init"]["permission_mode"] = "always-proceed"
            elif mutation == "duplicate":
                events.append(events[-1])
            elif mutation == "tool":
                events.insert(1, {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "run_command"}})
            elif mutation == "no_init":
                events = events[1:]
            self.write_events(events)
            if mutation == "malformed":
                with self.log.open("a", encoding="utf-8") as stream:
                    stream.write("{truncated\n")
            with self.assertRaises(ContractError, msg=mutation):
                read_response("antigravity", self.output, self.log)

    def test_stream_filter_drops_reasoning_and_nonterminal_text(self):
        events = stream_events()
        events.insert(1, {"event": "step_update", "step_update": {"step_type": "thinking", "text_delta": "PRIVATE REASONING TEXT"}})
        events.insert(2, {"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "PUBLIC TEXT DUPLICATE"}})
        gate = threading.Event()
        class GatedStream(io.BytesIO):
            first = True
            def readline(self, size=-1):
                if self.first:
                    self.first = False
                else:
                    gate.wait(timeout=2)
                return super().readline(size)
        thread, state = collect_antigravity_stream(GatedStream("".join(json.dumps(event) + "\n" for event in events).encode()), self.log)
        self.assertTrue(state["initialized"].wait(timeout=2))
        state["mark_input_sent"]()
        gate.set()
        thread.join(timeout=2)
        self.assertTrue(state["init_safe"])
        self.assertFalse(state["error"])
        content = self.log.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE REASONING", content)
        self.assertNotIn("PUBLIC TEXT DUPLICATE", content)
        self.assertEqual(read_response("antigravity", self.output, self.log)["decision"], "skip")

    def test_stream_filter_terminates_when_initial_tools_are_nonempty(self):
        events = stream_events()
        events[0]["init"]["tools"] = ["run_command"]
        killed = []
        thread, state = collect_antigravity_stream(io.BytesIO("".join(json.dumps(event) + "\n" for event in events).encode()), self.log, lambda: killed.append(True))
        thread.join(timeout=2)
        self.assertTrue(state["error"])
        self.assertFalse(state["init_safe"])
        self.assertTrue(killed)

    def test_cli_model_or_agent_override_mismatch_stops_before_input(self):
        for field, value in (("model", "different-model"), ("agent", "other-agent")):
            events = stream_events()
            events[0]["init"]["model"] = "requested-model"
            events[0]["init"][field] = value
            thread, state = collect_antigravity_stream(io.BytesIO((json.dumps(events[0]) + "\n").encode()), self.log,
                                                      expected_model="requested-model")
            thread.join(timeout=2)
            self.assertTrue(state["error"])
            self.assertFalse(state["init_safe"])
            with self.assertRaises(ContractError):
                state["mark_input_sent"]()

    def test_terminal_cumulative_usage_is_not_added_to_step_usage(self):
        events = stream_events()
        events.insert(1, {"event": "step_update", "step_update": {"usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}})
        self.write_events(events)
        usage = antigravity_usage(self.log, 1.2)
        self.assertEqual(usage["token_totals"], {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 80, "total_tokens": 120})
        self.assertEqual(usage["completeness"], "complete")

    def test_missing_conflicting_and_duplicate_usage_stays_partial(self):
        for values in ({"input_tokens": 100}, {"input_tokens": 100, "output_tokens": 20},
                       {"input_tokens": 100, "output_tokens": 20, "total_tokens": 500},
                       {"input_tokens": True, "output_tokens": 20, "total_tokens": 20},
                       {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "cache_read_tokens": 10, "cached_input_tokens": 20}):
            self.write_events(stream_events(usage=values))
            self.assertNotEqual(antigravity_usage(self.log, 1)["completeness"], "complete")
        events = stream_events()
        events.append(events[-1])
        self.write_events(events)
        usage = antigravity_usage(self.log, 1)
        self.assertEqual(usage["token_totals"]["total_tokens"], 120)
        self.assertEqual(usage["completeness"], "partial")

    def test_no_usage_is_unknown_and_protocol_output_is_bounded(self):
        self.write_events(stream_events(usage={}))
        self.assertEqual(antigravity_usage(self.log, 1)["completeness"], "unknown")
        self.log.write_bytes(b"x" * (256 * 1024 + 1))
        with self.assertRaises(ContractError):
            read_response("antigravity", self.output, self.log)
        self.assertTrue(antigravity_usage(self.log, 1)["log_tail_truncated"])

    def test_protocol_failure_cannot_be_revived_by_later_safe_init(self):
        for prefix in (b"{broken-json\n", b"x" * (256 * 1024 + 1) + b"\n"):
            raw = prefix + "".join(json.dumps(event) + "\n" for event in stream_events()).encode()
            killed = []
            thread, state = collect_antigravity_stream(io.BytesIO(raw), self.log, lambda: killed.append(True))
            thread.join(timeout=2)
            self.assertTrue(state["error"])
            self.assertFalse(state["init_safe"])
            self.assertTrue(killed)
            with self.assertRaises(ContractError):
                state["mark_input_sent"]()

    def test_result_before_input_and_events_after_terminal_are_rejected(self):
        thread, state = collect_antigravity_stream(io.BytesIO("".join(json.dumps(event) + "\n" for event in stream_events()).encode()), self.log)
        thread.join(timeout=2)
        self.assertTrue(state["error"])
        with self.assertRaises(ContractError):
            read_response("antigravity", self.output, self.log)
        events = stream_events()
        events.append({"event": "step_update", "step_update": {"step_type": "agent_response"}})
        self.write_events(events)
        with self.assertRaises(ContractError):
            read_response("antigravity", self.output, self.log)
        self.log.write_text(json.dumps(stream_events()[1]) + "\n" + json.dumps(stream_events()[0]) + "\n", encoding="utf-8")
        with self.assertRaises(ContractError):
            read_response("antigravity", self.output, self.log)

    def test_cli_availability_checks_no_credentials_or_process(self):
        with patch("subprocess.Popen") as process:
            known = executor_availability({"command": [sys.executable, "-B"]})
            missing = executor_availability({"command": [str(self.directory / "missing-cli.exe")]})
        process.assert_not_called()
        self.assertTrue(known["cli_available"])
        self.assertEqual(known["authentication"], "provider_cli_managed_not_checked")
        self.assertFalse(missing["cli_available"])
        self.assertEqual(missing["reason"], "cli_unavailable")


class ProviderLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-provider-lifecycle-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "state"
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.home, config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "chat", "provider review")
        self.problem = self.r.feedback(self.p, "chat", "provider problem", category="operation")
        self.r.config["admin"].update(enabled=True, command=[sys.executable], max_calls_per_day=20)

    def test_existing_codex_fake_reviewer_remains_compatible(self):
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("skip")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "skip")
        self.assertEqual(result["usage"]["executor"]["provider"], "codex")

    def fake_ag_command(self, tools=False, tool_step=False, status="SUCCESS", wait_marker=None):
        script = Path(self.temp.name) / "fake_ag.py"
        script.write_text(
            "import json,sys,pathlib\n"
            "def emit(obj): print(json.dumps(obj),flush=True)\n"
            "emit({'event':'init','init':{'tools':" + repr(["run_command"] if tools else []) + ", 'permission_mode':'request-review','agent':'alp-review'}})\n"
            "line=sys.stdin.readline()\n"
            "if not line: sys.exit(0)\n"
            "message=json.loads(line)\n"
            + ("pathlib.Path(" + repr(str(wait_marker)) + ").write_text('stdin received')\n" if wait_marker else "")
            + ("emit({'event':'step_update','step_update':{'step_type':'tool','tool_name':'run_command'}})\n" if tool_step else "")
            + "emit({'event':'step_update','step_update':{'step_type':'thinking','text_delta':'FAKE PRIVATE REASONING'}})\n"
            "packet=json.loads(message['message']['content'].split('<untrusted_evidence>',1)[1].rsplit('</untrusted_evidence>',1)[0])\n"
            "body={'decision':'skip','reason':'Fake AG review','candidate':None,'missing_evidence':[],'proposal':None}\n"
            "emit({'event':'result','result':{'status':" + repr(status) + ",'num_turns':1,'structured_output':body,'usage':{'input_tokens':100,'output_tokens':20,'cache_read_tokens':80,'total_tokens':120}}})\n",
            encoding="utf-8")
        return [sys.executable, "-B", str(script)]

    def test_real_fake_ag_subprocess_stdin_filter_and_usage(self):
        self.r.config["admin"].update(provider="antigravity", antigravity_command=self.fake_ag_command())
        result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "skip")
        self.assertEqual(result["usage"]["token_totals"]["total_tokens"], 120)
        self.assertEqual(result["usage"]["completeness"], "complete")
        self.assertEqual(result["usage"]["executor"]["provider"], "antigravity")
        self.assertNotIn("FAKE PRIVATE REASONING", json.dumps(dict(self.r.db.execute("SELECT * FROM admin_calls").fetchone())))

    def test_unsafe_ag_init_never_receives_evidence(self):
        marker = Path(self.temp.name) / "stdin-marker"
        self.r.config["admin"].update(provider="antigravity", antigravity_command=self.fake_ag_command(tools=True, wait_marker=marker))
        result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(marker.exists())
        self.assertEqual(result["usage"]["completeness"], "unknown")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts").fetchone()[0], 0)

    def test_failed_or_tool_invoking_ag_results_keep_usage_but_no_receipt(self):
        self.r.config["admin"].update(provider="antigravity", antigravity_command=self.fake_ag_command(status="ERROR"))
        result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["usage"]["token_totals"]["total_tokens"], 120)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts").fetchone()[0], 0)

    def test_model_policy_change_during_call_rejects_writeback(self):
        def change_model(packet, process):
            self.r.admin_configure(self.p, True, provider="codex", model_mode="fixed", model="changed-model")
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("candidate", change_model)):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_execution_plans").fetchone()[0], 1)

    def test_model_change_cannot_retry_same_unknown_problem(self):
        self.r.admin_configure(self.p, True, provider="codex", model_mode="fixed", model="first-model")
        def timeout(packet, process):
            raise subprocess.TimeoutExpired("fake-reviewer", 1)
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip", timeout)) as model:
            first = admin_once(self.r, self.p)
            self.assertEqual(first["status"], "timeout_unknown")
            self.r.admin_configure(self.p, True, provider="codex", model_mode="fixed", model="second-model")
            second = admin_once(self.r, self.p, self.problem["id"])
        self.assertEqual(model.call_count, 1)
        self.assertEqual(second["status"], "idle")

    def test_observed_current_model_is_passed_to_codex_and_fenced(self):
        self.r.admin_observe_model(self.p, "chat", "observed-model")
        self.r.admin_configure(self.p, True, provider="host", model_mode="current")
        def change_observation(packet, process):
            self.r.admin_observe_model(self.p, "chat", "new-observed-model")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip", change_observation)) as model:
            result = admin_once(self.r, self.p)
        args = model.call_args.args[0]
        self.assertEqual(args[args.index("--model") + 1], "observed-model")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts").fetchone()[0], 0)

    def test_current_without_observation_and_missing_cli_never_reserve(self):
        self.r.admin_configure(self.p, True, provider="host", model_mode="current")
        with patch("agent_learning.admin.subprocess.Popen") as model:
            result = admin_once(self.r, self.p)
        model.assert_not_called()
        self.assertEqual(result["status"], "executor_not_ready")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
        self.r.admin_configure(self.p, True, provider="codex", model_mode="default", command=[str(Path(self.temp.name) / "missing-cli.exe")])
        with patch("agent_learning.admin.subprocess.Popen") as model:
            result = admin_once(self.r, self.p)
        model.assert_not_called()
        self.assertEqual(result["reason"], "cli_unavailable")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_current_ready_case_is_found_beyond_ten_unobserved_cases(self):
        for index in range(12):
            conversation = "unobserved-" + str(index)
            self.r.task_update(self.p, conversation, "Unobserved old task")
            self.r.feedback(self.p, conversation, "Older case " + str(index), category="project")
        self.r.task_update(self.p, "observed-late", "Current observed task")
        ready = self.r.feedback(self.p, "observed-late", "Ready later case", category="project")
        self.r.admin_observe_model(self.p, "observed-late", "observed-model")
        self.r.admin_configure(self.p, True, provider="host", model_mode="current")
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("skip")):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "skip")
        row = self.r.db.execute("SELECT problem FROM admin_calls WHERE id=?", (result["call_id"],)).fetchone()
        self.assertEqual(row[0], ready["id"])

    def test_history_current_uses_unique_live_root_observation(self):
        (self.workspace / "README.md").write_text("Historical SQLite project fact.", encoding="utf-8")
        self.r.admin_observe_model(self.p, "chat", "observed-model")
        self.r.admin_configure(self.p, True, provider="host", model_mode="current")
        job = self.r.history_prepare(self.p, self.workspace)["job_id"]
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            result = self.r.history_run(self.p, job, allow_review=True)
        args = model.call_args.args[0]
        self.assertEqual(args[args.index("--model") + 1], "observed-model")
        self.assertEqual(result["job"]["counts"]["review_decision_cases"], 1)
        plan = self.r.admin_execution_plan(self.p, result["job"]["last_result"]["reviews"][0]["call_id"])
        self.assertIsNone(plan["executor"]["conversation"])

    def test_same_evidence_and_model_reuses_receipt_across_conversation_and_account(self):
        self.r.admin_configure(self.p, True, provider="codex", model_mode="fixed", model="same-model")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            admin_once(self.r, self.p, self.problem["id"])
            self.r.task_update(self.p, "second-chat", "provider review")
            second = self.r.feedback(self.p, "second-chat", "provider problem", category="operation")
            same = admin_once(self.r, self.p, second["id"])
            self.assertEqual(same["status"], "skip")
            self.r.account_changed()
            self.r.admin_configure(self.p, True, provider="codex", model_mode="fixed", model="same-model")
            self.r.task_update(self.p, "new-account-chat", "provider review")
            third = self.r.feedback(self.p, "new-account-chat", "provider problem", category="operation")
            admin_once(self.r, self.p, third["id"])
        self.assertEqual(model.call_count, 1)

    def test_provider_profile_switch_does_not_clear_unknown_call(self):
        def timeout(packet, process):
            raise subprocess.TimeoutExpired("fake-reviewer", 1)
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip", timeout)) as model:
            first = admin_once(self.r, self.p, self.problem["id"])
            self.assertEqual(first["status"], "timeout_unknown")
            self.r.admin_configure(self.p, True, provider="antigravity", model_mode="fixed", model="different-provider-model",
                                   command=self.fake_ag_command())
            second = admin_once(self.r, self.p, self.problem["id"])
        self.assertEqual(second["status"], "idle")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls WHERE state='timeout_unknown'").fetchone()[0], 1)

    def test_native_counter_regression_requires_new_trusted_model_observation(self):
        from agent_learning.antigravity import AntigravityAdapter, reset_session
        native = Runtime(self.home, config={}, host="antigravity")
        self.addCleanup(native.close)
        native.admin_configure(self.p, False, model_mode="current")
        adapter = AntigravityAdapter(native, self.workspace)
        conversation = native.host_conversation("native-chat")
        def invocation(number, steps, model):
            return adapter.handle("PreInvocation", {"conversationId": "native-chat", "workspacePaths": [str(self.workspace)],
                                  "modelName": model, "invocationNum": number, "initialNumSteps": steps})
        invocation(10, 100, "old-native-model")
        old = native.resolve_admin_executor(self.p, conversation, require_enabled=False)
        invocation(9, 90, "stale-regression-model")
        row = native.db.execute("SELECT model,active FROM admin_model_observations WHERE project=? AND host_scope=? AND conversation=?",
                                (self.p, native.host_scope, conversation)).fetchone()
        self.assertEqual(row["model"], "old-native-model")
        self.assertFalse(row["active"])
        with self.assertRaises(ContractError):
            native.resolve_admin_executor(self.p, conversation, require_enabled=False)
        with self.assertRaises(ContractError):
            native.assert_admin_executor_snapshot(self.p, old, require_enabled=False)
        reset_session(native, self.workspace, "native-chat")
        with self.assertRaises(ContractError):
            native.resolve_admin_executor(self.p, conversation, require_enabled=False)
        invocation(0, 0, "new-native-model")
        current = native.resolve_admin_executor(self.p, conversation, require_enabled=False)
        self.assertEqual(current["model"], "new-native-model")
        self.assertFalse(native.admin_policy(self.p)["enabled"])

    def test_native_explicit_reset_invalidates_unambiguous_model_snapshot(self):
        from agent_learning.antigravity import AntigravityAdapter, reset_session
        native = Runtime(self.home, config={}, host="antigravity")
        self.addCleanup(native.close)
        native.admin_configure(self.p, False, model_mode="current")
        adapter = AntigravityAdapter(native, self.workspace)
        raw = {"conversationId": "native-chat", "workspacePaths": [str(self.workspace)], "modelName": "old-model", "invocationNum": 0, "initialNumSteps": 0}
        adapter.handle("PreInvocation", raw)
        conversation = native.host_conversation("native-chat")
        old = native.resolve_admin_executor(self.p, conversation, require_enabled=False)
        reset_session(native, self.workspace, "native-chat")
        with self.assertRaises(ContractError):
            native.assert_admin_executor_snapshot(self.p, old, require_enabled=False)
        adapter.handle("PreInvocation", {**raw, "modelName": "new-model"})
        self.assertEqual(native.resolve_admin_executor(self.p, conversation, require_enabled=False)["model"], "new-model")

    def test_not_ready_scheduler_does_not_launch_or_wait_out_runtime_budget(self):
        self.r.config["scheduler"].update(enabled=True, max_run_seconds=5, max_tick_seconds=5)
        self.r.admin_configure(self.p, True, provider="host", model_mode="current")
        with patch.object(self.r, "_launch_scheduler") as launch:
            self.r.handle_hook({"hook_event_name": "Stop", "session_id": "chat", "cwd": str(self.workspace),
                                "turn_id": "unobserved-stop", "last_assistant_message": "task stopped"})
        launch.assert_not_called()
        waiting = self.r.db.execute("SELECT state,next_due,wait_reason FROM scheduler_jobs WHERE project=? AND kind='admin'", (self.p,)).fetchone()
        self.assertEqual(waiting["wait_reason"], "executor_not_ready")
        self.assertIsNone(waiting["next_due"])
        with patch("agent_learning.scheduler.time.sleep", side_effect=AssertionError("not ready job must not wait")), patch("agent_learning.admin.subprocess.Popen") as model:
            result = self.r.scheduler_run(self.p, max_seconds=5)
        model.assert_not_called()
        self.assertEqual(result["processed"], 0)
        self.r.admin_observe_model(self.p, "chat", "current-model")
        self.assertTrue(self.r.scheduler_wake(self.p))
        self.r.admin_configure(self.p, True, provider="codex", model_mode="default", command=[str(Path(self.temp.name) / "missing-cli.exe")])
        with patch.object(self.r, "_launch_scheduler") as launch:
            self.r.handle_hook({"hook_event_name": "Stop", "session_id": "chat", "cwd": str(self.workspace),
                                "turn_id": "missing-cli-stop", "last_assistant_message": "task stopped"})
        launch.assert_not_called()
        self.r.admin_configure(self.p, True, provider="codex", model_mode="default", command=[sys.executable])
        self.assertTrue(self.r.scheduler_wake(self.p))


if __name__ == "__main__":
    unittest.main()
