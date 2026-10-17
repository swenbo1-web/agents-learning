"""Synthetic provider errors and explicit retry contracts; no model requests."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_diagnostics import safe_diagnostic
from agent_learning.admin_lifecycle import retry_call
from agent_learning.admin_providers import antigravity_usage, collect_antigravity_stream, read_response
from agent_learning.admin_queue import executor_backoff, executor_signature, record_executor_failure
from agent_learning.admin_usage import admin_project_budget_status
from agent_learning.dsh_profile import configuration_identity
from agent_learning.runtime import ContractError, Runtime


def diagnosis(**values):
    return {"version": 1, "phase": "stream", "reason_code": "invalid_request_parameter",
            "http_status": 400, "parameter_code": "thinking_budget", "retry_policy": "configuration_change",
            "stream_invoked": True, "http_response_observed": True, "usage_observed": False, **values}


class SafeDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="diag-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def collect(self, events, initialized=True):
        path = self.root / "events.jsonl"
        payload = "\n".join(json.dumps(value) for value in events) + "\n"
        thread, state = collect_antigravity_stream(io.BytesIO(payload.encode()), path)
        # Byte streams can finish before this test thread gets the input gate;
        # initialized/error cases are tested separately below via real subprocess.
        thread.join(5)
        return path, state

    def test_diagnostic_rejects_untyped_extended_or_inconsistent_values(self):
        self.assertEqual(safe_diagnostic(diagnosis()), diagnosis())
        variants = [{"version": True}, {"phase": []}, {"reason_code": {}}, {"http_status": True},
                    {"http_status": 429}, {"parameter_code": []}, {"stream_invoked": 1},
                    {"http_response_observed": False}, {"phase": "response"}, {"error_text": "PRIVATE"}]
        for change in variants:
            with self.subTest(change=change):
                self.assertIsNone(safe_diagnostic(diagnosis(**change)))
        self.assertIsNone(safe_diagnostic({}))

    def test_preinit_failure_keeps_diagnostic_but_never_opens_input_gate(self):
        value = diagnosis(phase="runtime", reason_code="provider_unavailable", http_status=None,
                          parameter_code=None, retry_policy="no_automatic_retry", stream_invoked=False,
                          http_response_observed=False)
        path, state = self.collect([{"event": "result", "result": {
            "status": "ERROR", "error": True, "num_turns": 0, "diagnostic": value}}])
        self.assertFalse(state["init_safe"])
        self.assertTrue(state["error"])
        self.assertEqual(state["diagnostic"], value)
        with self.assertRaises(ContractError):
            state["mark_input_sent"]()
        self.assertEqual(antigravity_usage(path, 1)["observations"]["provider_diagnostic"], value)
        with self.assertRaises(ContractError):
            read_response("dsh", self.root / "unused", path)

    def test_success_without_usage_has_terminal_turn_but_unknown_token_usage(self):
        path = self.root / "events.jsonl"
        path.write_text(json.dumps({"event": "result", "result": {"status": "SUCCESS", "num_turns": 1}}))
        usage = antigravity_usage(path, 1)
        self.assertEqual(usage["reported_turns"], 0)
        self.assertEqual(usage["observations"]["terminal_reported_turns"], 1)
        self.assertEqual(usage["completeness"], "unknown")

    def test_error_keeps_observed_usage(self):
        path = self.root / "events.jsonl"
        path.write_text(json.dumps({"event": "result", "result": {"status": "ERROR", "error": True,
            "num_turns": 0, "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
            "diagnostic": diagnosis(phase="response", reason_code="invalid_response_json", http_status=None,
                parameter_code=None, retry_policy="no_automatic_retry", http_response_observed=False, usage_observed=True)}}))
        usage = antigravity_usage(path, 1)
        self.assertEqual(usage["token_totals"]["total_tokens"], 13)
        self.assertTrue(usage["observations"]["usage_observed"])


class ExecutorConfigurationFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cfg-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.script = self.root / "review.py"
        self.script.write_text("import json,sys\n"
            "print(json.dumps({'event':'init','init':{'tools':[],'permission_mode':'request-review',"
            "'agent':'alp-review','model':'google-vertex/gemini-4-argon'}}),flush=True)\n"
            "sys.stdin.read()\n"
            f"print(json.dumps({{'event':'result','result':{{'status':'ERROR','error':True,'num_turns':0,'diagnostic':{diagnosis()!r}}}}}),flush=True)\n"
            "sys.exit(1)\n", encoding="utf-8")
        self.r = Runtime(self.root / "home", host="dsh", config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.conversation = self.r.host_conversation("chat")
        self.r.admin_configure(self.p, True, "host", "fixed", "google-vertex/gemini-4-argon",
                               command=[sys.executable, "-B", str(self.script)])
        self.problems = [self.r.feedback(self.p, self.conversation, f"Independent visible observation {n}")["id"]
                         for n in range(3)]

    def test_parameter_error_stops_other_cases_without_erasing_original_attempt(self):
        result = admin_once(self.r, self.p, self.problems[0])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["usage"]["execution"]["provider_diagnostic"], diagnosis())
        self.assertEqual(result["usage"]["execution"]["reason_code"], "invalid_request_parameter")
        self.assertEqual(result["usage"]["reported_turns"], 0)
        for problem in self.problems[1:]:
            blocked = admin_once(self.r, self.p, problem)
            self.assertEqual(blocked["reason"], "configuration_change_required")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        self.assertEqual(admin_project_budget_status(self.r, self.p)["counted_attempts"], 1)
        retry_call(self.r, self.p, result["call_id"], "Verified adapter correction will precede another attempt")
        self.assertEqual(admin_once(self.r, self.p, self.problems[0])["reason"], "configuration_change_required")
        row = self.r.db.execute("SELECT state,retry_allowed FROM admin_calls").fetchone()
        self.assertEqual(tuple(row), ("failed", 1))
        # A corrected executable creates a different executor identity.
        self.script.write_text(self.script.read_text() + "\n# corrected adapter\n")
        plan = self.r.resolve_admin_executor(self.p, self.conversation)
        self.assertIsNone(executor_backoff(self.r, self.p, plan))

    def test_transient_failure_expires_and_model_switch_is_independent(self):
        plan = self.r.resolve_admin_executor(self.p, self.conversation)
        record_executor_failure(self.r, self.p, plan, "provider_quota_exceeded")
        self.assertIsNotNone(executor_backoff(self.r, self.p, plan))
        self.r.db.execute("UPDATE admin_executor_failures SET next_try=0")
        self.assertIsNone(executor_backoff(self.r, self.p, plan))
        record_executor_failure(self.r, self.p, plan, "configuration_change_required")
        self.assertIsNone(executor_backoff(self.r, self.p, dict(plan, model="different-model")))

    def test_settings_metadata_invalidates_failure_without_reading_settings_contents(self):
        settings = self.root / "settings.yaml"
        settings.write_text("private credentials must never be read")
        profile = self.root / "profile.json"
        profile.write_text(json.dumps({"version": 1, "profile_directory": str(self.root), "entries": [
            {"name": "@deepseek-ai/dsh-settings-file", "config": {"path": str(settings)}}]}))
        from agent_learning.dsh_profile import resolve_profile
        descriptor = resolve_profile(str(profile))
        plan = dict(self.r.resolve_admin_executor(self.p, self.conversation), dsh_profile=descriptor)
        original = executor_signature(plan)
        settings.write_text("changed configuration metadata")
        self.assertNotEqual(original, executor_signature(plan))
        self.assertNotIn("private", json.dumps(configuration_identity(descriptor)))


if __name__ == "__main__":
    unittest.main()
