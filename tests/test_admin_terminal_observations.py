"""Protocol, public response and decision observations stay independent and bounded."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import ResponseRejected, _usage, admin_once
from agent_learning.admin_providers import antigravity_usage
from agent_learning.admin_queue import record_executor_failure
from agent_learning.admin_response_store import status as response_status
from agent_learning.runtime import Runtime


REPLY = {"decision": "skip", "reason": "Synthetic public observation", "candidate": None}
TOKENS = {"input_tokens": 100, "output_tokens": 20}


class TerminalUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-terminal-usage-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def log(self, events):
        path = self.root / "events.jsonl"
        path.write_bytes(b"".join((value.encode() if isinstance(value, str) else
                                   json.dumps(value).encode()) + b"\n" for value in events))
        return path

    def assert_protocol(self, usage, observed, status):
        self.assertIs(usage["observations"]["protocol_terminal_observed"], observed)
        self.assertEqual(usage["observations"]["protocol_terminal_status"], status)

    def test_terminal_without_usage_is_observed_without_zero_token_fabrication(self):
        for kind, status in (("turn.completed", "completed"), ("turn.failed", "failed"),
                             ("turn.cancelled", "cancelled"), ("turn.canceled", "cancelled")):
            with self.subTest(kind=kind):
                usage = _usage(self.log([{"type": kind, "turn_id": "one"}]), 1)
                self.assert_protocol(usage, True, status)
                self.assertEqual(usage["token_totals"], {})
                self.assertEqual(usage["completeness"], "unknown")

    def test_intermediate_error_is_diagnostic_and_never_a_terminal(self):
        event = {"type": "error", "message": "PRIVATE", "error": {"code": "rate_limit_exceeded"}}
        usage = _usage(self.log([event]), 1)
        self.assert_protocol(usage, False, "unknown")
        self.assertEqual(usage["provider_diagnostic"]["reason_code"], "provider_quota_exceeded")
        usage = _usage(self.log([event, {"type": "turn.completed", "turn_id": "one", "usage": TOKENS}]), 1)
        self.assert_protocol(usage, True, "completed")
        self.assertEqual(usage["completeness"], "complete")
        self.assertNotIn("PRIVATE", json.dumps(usage))

    def test_identified_replay_is_idempotent_but_conflicting_terminals_are_not(self):
        complete = {"type": "turn.completed", "turn_id": "one", "usage": TOKENS}
        usage = _usage(self.log([complete, complete]), 1)
        self.assert_protocol(usage, True, "completed")
        self.assertEqual(usage["token_totals"], TOKENS)
        self.assertEqual(usage["reported_turns"], 1)
        for events in ([complete, {"type": "turn.failed", "turn_id": "one"}],
                       [{"type": "turn.failed", "turn_id": "one"}, complete],
                       [complete, dict(complete, usage={"input_tokens": 101, "output_tokens": 20})]):
            with self.subTest(events=events):
                usage = _usage(self.log(events), 1)
                self.assert_protocol(usage, True, "conflicted")
                self.assertEqual(usage["completeness"], "partial")

    def test_anonymous_repeat_is_ambiguous_but_new_started_turn_is_distinct(self):
        complete = {"type": "turn.completed", "usage": TOKENS}
        usage = _usage(self.log([complete, complete]), 1)
        self.assert_protocol(usage, True, "conflicted")
        self.assertEqual(usage["completeness"], "partial")
        usage = _usage(self.log([complete, {"type": "turn.started"}, {"type": "turn.failed"}]), 1)
        self.assert_protocol(usage, True, "failed")

    def test_pending_or_incomplete_protocol_does_not_claim_confirmed_completion(self):
        complete = {"type": "turn.completed", "turn_id": "one", "usage": TOKENS}
        for tail in ({"type": "turn.started", "turn_id": "two"}, "{malformed", "x" * 65538):
            with self.subTest(tail_type=type(tail).__name__):
                self.assert_protocol(_usage(self.log([complete, tail]), 1), True, "unknown")
        self.assert_protocol(_usage(None, 1), False, "unknown")
        self.assert_protocol(_usage(self.root / "missing", 1), False, "unknown")

    def test_non_json_known_error_is_bounded_diagnostic_without_terminal(self):
        usage = _usage(self.log(["Error: Not logged in", "private arbitrary credential error text"]), 1)
        self.assert_protocol(usage, False, "unknown")
        self.assertEqual(usage["provider_diagnostic"]["reason_code"], "invalid_credential")
        self.assertEqual(usage["token_totals"], {})
        self.assertNotIn("private", json.dumps(usage))

    def test_stream_provider_protocol_status_does_not_imply_response_collection(self):
        for provider in ("antigravity", "dsh"):
            for status, expected in (("SUCCESS", "completed"), ("ERROR", "failed"), ("CANCELLED", "cancelled")):
                with self.subTest(provider=provider, status=status):
                    event = {"event": "result", "result": {"status": status, "num_turns": 1}}
                    usage = antigravity_usage(self.log([event]), 1, provider=provider)
                    self.assert_protocol(usage, True, expected)
                    self.assertEqual(usage["completeness"], "unknown")
                    self.assertNotIn("response_collected", usage["observations"])
            event = {"event": "result", "result": {"status": "SUCCESS", "num_turns": 1}}
            self.assert_protocol(antigravity_usage(self.log([event, event]), 1, provider=provider), True, "conflicted")
            self.assert_protocol(antigravity_usage(self.log([event, {"event": "adapter_protocol_error"}]), 1,
                                                 provider=provider), True, "conflicted")


class TerminalExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-terminal-execution-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        workspace = self.root / "project"
        workspace.mkdir()
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": True, "command": [sys.executable],
                         "timeout_seconds": 10, "max_calls_per_day": 20}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(workspace)
        self.problem = self.r.feedback(self.p, "chat", "Synthetic independent terminal observation")

    def run_codex(self, events, *, code=0, response=REPLY, timeout=False, acceptance_error=None):
        class Reviewer:
            def __init__(child, args, **kwargs):
                child.output = Path(args[args.index("--output-last-message") + 1])
                child.log, child.returncode, child.pid = kwargs["stdout"], code, 4242
            def communicate(child, *args, **kwargs):
                for event in events:
                    line = event if isinstance(event, str) else json.dumps(event)
                    child.log.write((line + "\n").encode())
                child.log.flush()
                if response is not None:
                    child.output.write_text(json.dumps(response), encoding="utf-8")
                if timeout:
                    raise subprocess.TimeoutExpired("synthetic-reviewer", 10)
                return None, None
            def poll(child): return child.returncode
            def kill(child): child.returncode = -9
            def wait(child, timeout=None): return child.returncode
        with patch("agent_learning.admin_providers.executor_capability", return_value={"ready": True}), \
                patch("agent_learning.admin.subprocess.Popen", Reviewer), \
                patch("agent_learning.admin._usage", wraps=_usage) as scanner, \
                patch("agent_learning.admin_queue.record_executor_failure", wraps=record_executor_failure) as failure:
            if acceptance_error is None:
                result = admin_once(self.r, self.p, self.problem["id"])
            else:
                with patch("agent_learning.admin._accept_response", side_effect=acceptance_error):
                    result = admin_once(self.r, self.p, self.problem["id"])
        self.assertEqual(scanner.call_count, 1)
        stored = json.loads(self.r.db.execute("SELECT usage FROM admin_calls WHERE id=?", (result["call_id"],)).fetchone()[0])
        self.assertEqual(stored["execution"], result["usage"]["execution"])
        return result, failure

    def test_completed_protocol_without_response_does_not_open_local_recheck(self):
        result, _ = self.run_codex([{"type": "turn.completed", "turn_id": "one"}], response=None)
        execution = result["usage"]["execution"]
        self.assertEqual(result["status"], "failed")
        self.assertTrue(execution["protocol_terminal_observed"])
        self.assertEqual(execution["protocol_terminal_status"], "completed")
        self.assertFalse(execution["response_collected"])
        self.assertFalse(execution["terminal_observed"])
        self.assertFalse(response_status(self.r, self.p, result["call_id"])["can_recheck"])
        self.assertEqual(result["usage"]["token_totals"], {})

    def test_failure_diagnostic_is_available_before_backoff_registration(self):
        result, failure = self.run_codex([{"type": "turn.failed", "turn_id": "one",
            "error": {"code": "model_not_found", "status_code": 404, "message": "PRIVATE"}}], code=1)
        self.assertEqual(failure.call_args.args[3], "model_unavailable")
        details = failure.call_args.args[4]
        self.assertEqual(details["provider_diagnostic"]["http_status"], 404)
        self.assertEqual(details["protocol_terminal_status"], "failed")
        self.assertFalse(details["response_collected"])
        self.assertEqual(result["usage"]["execution"]["reason_code"], "model_unavailable")
        self.assertFalse(result["usage"]["execution"]["terminal_observed"])
        row = self.r.db.execute("SELECT state,retry_allowed FROM admin_calls").fetchone()
        self.assertEqual(tuple(row), ("failed", 0))
        self.assertEqual(self.r.db.execute("SELECT completeness FROM admin_call_budgets").fetchone()[0], "unknown")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_non_json_failure_is_also_registered_before_finish(self):
        result, failure = self.run_codex(["Error: Not logged in"], code=1)
        self.assertEqual(failure.call_args.args[3], "invalid_credential")
        self.assertFalse(result["usage"]["execution"]["protocol_terminal_observed"])
        self.assertEqual(result["usage"]["execution"]["protocol_terminal_status"], "unknown")

    def test_intermediate_provider_error_does_not_replace_local_response_failure(self):
        events = [{"type": "error", "error": {"code": "rate_limit_exceeded"}},
                  {"type": "turn.completed", "turn_id": "one", "usage": TOKENS}]
        result, failure = self.run_codex(events, response=["not a response object"])
        execution = result["usage"]["execution"]
        self.assertEqual(failure.call_args.args[3], "executor_response_invalid")
        self.assertEqual(execution["reason_code"], "executor_response_invalid")
        self.assertEqual(execution["provider_diagnostic"]["reason_code"], "provider_quota_exceeded")
        self.assertEqual(execution["protocol_terminal_status"], "completed")
        self.assertFalse(execution["response_collected"])

    def test_intermediate_provider_error_does_not_replace_local_decision_rejection(self):
        events = [{"type": "error", "error": {"code": "rate_limit_exceeded"}},
                  {"type": "turn.completed", "turn_id": "one", "usage": TOKENS}]
        result, failure = self.run_codex(events, acceptance_error=ResponseRejected("candidate_batch_ambiguous"))
        execution = result["usage"]["execution"]
        self.assertEqual(failure.call_args.args[3], "executor_decision_uncommitted")
        self.assertEqual(execution["reason_code"], "executor_decision_uncommitted")
        self.assertEqual(execution["provider_diagnostic"]["reason_code"], "provider_quota_exceeded")
        self.assertTrue(execution["response_collected"])

    def test_response_collection_and_rejection_remain_distinct_from_protocol(self):
        result, _ = self.run_codex([], acceptance_error=ResponseRejected("candidate_batch_ambiguous"))
        execution = result["usage"]["execution"]
        self.assertEqual(result["status"], "failed")
        self.assertTrue(execution["response_collected"])
        self.assertTrue(execution["terminal_observed"])
        self.assertFalse(execution["protocol_terminal_observed"])
        self.assertEqual(execution["protocol_terminal_status"], "unknown")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts").fetchone()[0], 0)

    def test_success_without_protocol_stream_preserves_existing_fake_cli_contract(self):
        result, _ = self.run_codex([])
        execution = result["usage"]["execution"]
        self.assertEqual(result["status"], "skip")
        self.assertTrue(execution["response_collected"])
        self.assertTrue(execution["terminal_observed"])
        self.assertFalse(execution["protocol_terminal_observed"])

    def test_timeout_retains_unknown_call_despite_completed_protocol(self):
        result, failure = self.run_codex([{"type": "turn.completed", "turn_id": "one", "usage": TOKENS}], timeout=True)
        execution = result["usage"]["execution"]
        self.assertEqual(result["status"], "timeout_unknown")
        self.assertTrue(execution["protocol_terminal_observed"])
        self.assertFalse(execution["terminal_observed"])
        self.assertFalse(execution["response_collected"])
        self.assertEqual(execution["returncode"], -9)
        self.assertTrue(result["usage"]["call_outcome_unknown"])
        self.assertEqual(result["usage"]["completeness"], "partial")
        self.assertEqual(failure.call_args.args[3], "executor_timeout")

    def test_stream_providers_collect_response_independently_of_terminal_success(self):
        for provider, status, expected in (("antigravity", "SUCCESS", "skip"), ("dsh", "ERROR", "failed")):
            with self.subTest(provider=provider):
                self.r.db.execute("DELETE FROM admin_executor_failures")
                problem = self.r.feedback(self.p, "chat", f"Synthetic {provider} protocol observation")
                script = self.root / f"fake_{provider}.py"
                script.write_text("import json,sys\n"
                    "def emit(value): print(json.dumps(value),flush=True)\n"
                    "emit({'event':'init','init':{'tools':[],'permission_mode':'request-review','agent':'alp-review'}})\n"
                    "sys.stdin.readline()\n"
                    "emit({'event':'result','result':{'status':" + repr(status) + ", 'num_turns':1,"
                    "'structured_output':" + repr(REPLY) + "}})\n", encoding="utf-8")
                self.r.config["admin"].update(provider=provider, **{provider + "_command": [sys.executable, "-B", str(script)]})
                with patch("agent_learning.admin_providers.executor_capability", return_value={"ready": True}):
                    result = admin_once(self.r, self.p, problem["id"])
                self.assertEqual(result["status"], expected, result)
                execution = result["usage"]["execution"]
                self.assertTrue(execution["protocol_terminal_observed"])
                self.assertEqual(execution["protocol_terminal_status"], "completed" if status == "SUCCESS" else "failed")
                self.assertIs(execution["response_collected"], status == "SUCCESS")
                self.assertEqual(result["usage"]["completeness"], "unknown")


if __name__ == "__main__":
    unittest.main()
