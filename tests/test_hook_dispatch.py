"""Hook 故障策略与真实入口检查；隔离数据目录，不启用宿主或模型。"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("alp_hook_dispatch", ROOT / "hooks/dispatch.py")
dispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatch)


class HookDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="alp hook faults ")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "data"
        self.home.mkdir()
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()
        self.environment = os.environ.copy()
        for key in ("ALP_CONFIG", "ALP_DISABLED", "ALP_HOOK_MODE"):
            self.environment.pop(key, None)
        self.environment.update(ALP_HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1")
        self.event = {
            "hook_event_name": "PreToolUse", "session_id": "hook-fault-check",
            "cwd": str(self.workspace), "tool_use_id": "call-a", "tool_name": "Bash",
            "tool_input": {"command": "example"},
        }

    def configure(self, value):
        (self.home / "config.json").write_text(json.dumps(value), encoding="utf-8")

    def invoke(self, body=None, *, arguments=None, environment=None, runtime_error=None,
               validation_error=None, response=None, buffer=None):
        body = json.dumps(self.event).encode("utf-8") if body is None else body
        output = io.StringIO()
        runtime = Mock()
        runtime.config = {"enforce_task_policy": False}
        runtime.handle_hook.return_value = {} if response is None else response
        factory = Mock(return_value=runtime, side_effect=runtime_error)
        release = types.ModuleType("agent_learning.release")
        release.validate_entry = Mock(side_effect=validation_error)
        with patch.dict(os.environ, environment or self.environment, clear=True), \
                patch.object(dispatch.sys, "stdin", types.SimpleNamespace(buffer=buffer or io.BytesIO(body))), \
                patch.object(dispatch.sys, "stdout", output), \
                patch.dict(sys.modules, {"agent_learning.release": release}), \
                patch("agent_learning.runtime.Runtime", factory):
            code = dispatch.main(arguments if arguments is not None else ["--event", "PreToolUse"])
        self.assertEqual(code, 0)
        wire = output.getvalue()
        self.assertEqual(len(wire.splitlines()), 1)
        self.assertNotIn("private-fault-detail", wire)
        return json.loads(wire), runtime, factory, release

    def assert_denied(self, result, classification):
        output = result["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn(classification, output["permissionDecisionReason"])

    def test_fixed_pretool_entry_denies_invalid_json_in_enforce_mode(self):
        result, _, factory, _ = self.invoke(
            b"{private-fault-detail", arguments=["--event", "PreToolUse", "--hook-mode", "enforce"])
        self.assert_denied(result, "input_invalid")
        factory.assert_not_called()

    def test_environment_mode_denies_decode_failure(self):
        result, _, factory, _ = self.invoke(
            b"\xff", environment=dict(self.environment, ALP_HOOK_MODE="enforce"))
        self.assert_denied(result, "input_invalid")
        factory.assert_not_called()

    def test_bounded_read_rejects_oversized_input(self):
        buffer = Mock()
        buffer.read.return_value = b"x" * (dispatch.MAX_INPUT_BYTES + 1)
        result, _, factory, _ = self.invoke(
            buffer=buffer, arguments=["--event", "PreToolUse", "--hook-mode", "enforce"])
        buffer.read.assert_called_once_with(dispatch.MAX_INPUT_BYTES + 1)
        self.assert_denied(result, "input_too_large")
        factory.assert_not_called()

    def test_deeply_nested_json_is_a_safe_input_failure(self):
        result, _, factory, _ = self.invoke(
            b"[" * 2000 + b"0" + b"]" * 2000,
            arguments=["--event", "PreToolUse", "--hook-mode", "enforce"])
        self.assert_denied(result, "input_invalid")
        factory.assert_not_called()

    def test_event_mismatch_uses_fixed_event_for_fault_policy(self):
        body = json.dumps(dict(self.event, hook_event_name="Stop")).encode("utf-8")
        result, _, factory, _ = self.invoke(
            body, arguments=["--event", "PreToolUse", "--hook-mode", "enforce"])
        self.assert_denied(result, "event_mismatch")
        factory.assert_not_called()

    def test_missing_common_identity_is_rejected_before_runtime(self):
        result, _, factory, _ = self.invoke(
            json.dumps(dict(self.event, session_id=None)).encode("utf-8"),
            arguments=["--event", "PreToolUse", "--hook-mode", "enforce"])
        self.assert_denied(result, "input_invalid")
        factory.assert_not_called()

    def test_bad_config_without_confirmed_mode_reports_unknown_protection(self):
        (self.home / "config.json").write_text("{private-fault-detail", encoding="utf-8")
        result, _, _, _ = self.invoke(runtime_error=ValueError("private-fault-detail"))
        self.assertIn("runtime_unavailable", result["systemMessage"])
        self.assertIn("Protection mode is unknown", result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", result)

    def test_valid_config_enforce_denies_database_failure(self):
        self.configure({"enforce_task_policy": True})
        result, _, _, _ = self.invoke(runtime_error=OSError("private-fault-detail"))
        self.assert_denied(result, "runtime_unavailable")

    def test_explicit_enforce_survives_bad_config(self):
        (self.home / "config.json").write_text("{", encoding="utf-8")
        result, _, _, _ = self.invoke(
            arguments=["--event", "PreToolUse", "--hook-mode", "enforce"],
            runtime_error=ValueError("private-fault-detail"))
        self.assert_denied(result, "runtime_unavailable")

    def test_observe_mode_warns_without_deny(self):
        result, _, _, _ = self.invoke(
            arguments=["--event", "PreToolUse", "--hook-mode", "observe"],
            runtime_error=OSError("private-fault-detail"))
        self.assertIn("runtime_unavailable", result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", result)

    def test_release_guard_runs_before_runtime_and_fails_closed(self):
        result, _, factory, release = self.invoke(
            arguments=["--event", "PreToolUse", "--hook-mode", "enforce"],
            validation_error=ValueError("private-fault-detail"))
        self.assert_denied(result, "release_unverified")
        factory.assert_not_called()
        release.validate_entry.assert_called_once_with(dispatch.__file__)

    def test_posttool_failure_remains_informational_under_enforcement(self):
        body = json.dumps(dict(self.event, hook_event_name="PostToolUse")).encode("utf-8")
        result, _, _, _ = self.invoke(
            body, arguments=["--event", "PostToolUse", "--hook-mode", "enforce"],
            runtime_error=OSError("private-fault-detail"))
        self.assertIn("runtime_unavailable", result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", result)

    def test_entry_mode_is_applied_to_runtime_and_takes_precedence(self):
        self.configure({"enforce_task_policy": False})
        result, runtime, _, _ = self.invoke(
            arguments=["--event", "PreToolUse", "--hook-mode", "enforce"],
            environment=dict(self.environment, ALP_HOOK_MODE="observe"))
        self.assertEqual(result, {})
        self.assertTrue(runtime.config["enforce_task_policy"])
        runtime.close.assert_called_once()

    def test_bad_response_fails_closed_and_closes_runtime(self):
        result, runtime, _, _ = self.invoke(
            arguments=["--event", "PreToolUse", "--hook-mode", "enforce"], response=[])
        self.assert_denied(result, "response_invalid")
        runtime.close.assert_called_once()

    def test_recorded_stop_diagnostic_is_preserved(self):
        body = json.dumps(dict(self.event, hook_event_name="Stop")).encode("utf-8")
        response = {"systemMessage": "Agent Learning [admin_schedule_failed]: event recorded."}
        result, runtime, _, _ = self.invoke(body, arguments=["--event", "Stop"], response=response)
        self.assertEqual(result, response)
        runtime.close.assert_called_once()

    def test_no_argument_source_entry_remains_compatible(self):
        result, runtime, _, _ = self.invoke(arguments=[])
        self.assertEqual(result, {})
        runtime.handle_hook.assert_called_once_with(self.event)

    def run_entry(self, arguments):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "hooks/dispatch.py"), *arguments],
            input=json.dumps(self.event, ensure_ascii=False).encode("utf-8"),
            cwd=self.workspace, env=self.environment, capture_output=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b"")
        return json.loads(result.stdout.decode("utf-8"))

    def test_real_entry_enforce_overrides_observe_config(self):
        self.configure({"enforce_task_policy": False})
        result = self.run_entry(["--event", "PreToolUse", "--hook-mode", "enforce"])
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("当前任务状态", result["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertTrue((self.home / "runtime.sqlite3").is_file())

    def test_real_entry_observe_overrides_enforce_config(self):
        self.configure({"enforce_task_policy": True})
        result = self.run_entry(["--event", "PreToolUse", "--hook-mode", "observe"])
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
