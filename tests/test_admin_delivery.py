"""Admission, real process acknowledgement and safe Windows launcher contracts."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent_learning.admin import admin_batch, admin_drain, admin_once
from agent_learning.admin_control import control_status
from agent_learning.admin_lifecycle import claim_call, mark_launch_intent, finish_call
from agent_learning.admin_providers import build_execution, dsh_sdk_launcher, executor_availability, executor_capability, resolve_command
from agent_learning.admin_queue import (background_plan, claim_dispatch, claim_dispatch_worker, dispatch_diagnostic,
    executor_backoff, record_executor_failure, release_dispatch)
from agent_learning.runtime import Runtime


class AdminDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-admin-delivery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.home = self.root / "home"
        self.r = Runtime(self.home, config={"admin": {"enabled": True, "command": [sys.executable], "timeout_seconds": 10}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.problem = self.r.feedback(self.p, "chat", "An observable project observation")

    def test_reservation_is_not_worker_running_and_is_short(self):
        token = claim_dispatch(self.r, self.p)
        self.assertTrue(token)
        status = control_status(self.r, self.p)
        self.assertFalse(status["runner"]["running"])
        self.assertEqual(status["runner"]["state"], "worker_reserved")
        self.assertLessEqual(status["diagnostic"]["next_due"], time.time() + 30)
        self.assertTrue(claim_dispatch_worker(self.r, self.p, token, 600))
        self.assertTrue(control_status(self.r, self.p)["runner"]["running"])
        self.assertEqual(dispatch_diagnostic(self.r, self.p)["phase"], "worker_claimed")
        self.assertFalse(claim_dispatch_worker(self.r, self.p, token, 600))

    def test_duplicate_drain_does_not_delete_owner_reservation(self):
        token = claim_dispatch(self.r, self.p)
        self.assertTrue(claim_dispatch_worker(self.r, self.p, token, 600))
        result = admin_drain(self.r, self.p, dispatch_token=token)
        self.assertEqual(result["status"], "dispatch_expired")
        self.assertIsNotNone(self.r.db.execute("SELECT 1 FROM admin_dispatch WHERE token=?", (token,)).fetchone())

    def test_legacy_single_batch_cannot_release_or_use_another_workers_claim(self):
        token = claim_dispatch(self.r, self.p)
        self.assertTrue(claim_dispatch_worker(self.r, self.p, token, 600))
        with patch("agent_learning.admin.admin_once") as model:
            result = admin_batch(self.r, self.p, dispatch_token=token)
        model.assert_not_called()
        self.assertEqual(result["status"], "dispatch_expired")
        self.assertIsNotNone(self.r.db.execute("SELECT 1 FROM admin_dispatch WHERE token=?", (token,)).fetchone())

    def test_disabled_single_batch_cannot_release_an_unclaimed_reservation(self):
        token = claim_dispatch(self.r, self.p)
        self.r.config["admin"]["enabled"] = False
        self.assertEqual(admin_batch(self.r, self.p, dispatch_token=token)["status"], "disabled")
        self.assertIsNotNone(self.r.db.execute("SELECT 1 FROM admin_dispatch WHERE token=?", (token,)).fetchone())

    def test_disabled_duplicate_drain_cannot_release_existing_worker(self):
        token = claim_dispatch(self.r, self.p)
        self.assertTrue(claim_dispatch_worker(self.r, self.p, token, 600))
        self.r.config["admin"]["enabled"] = False
        result = admin_drain(self.r, self.p, dispatch_token=token)
        self.assertEqual(result["status"], "disabled")
        self.assertIsNotNone(self.r.db.execute("SELECT 1 FROM admin_dispatch WHERE token=?", (token,)).fetchone())

    def test_invalid_settings_duplicate_drain_cannot_release_existing_worker(self):
        from agent_learning.runtime import ContractError
        token = claim_dispatch(self.r, self.p)
        self.assertTrue(claim_dispatch_worker(self.r, self.p, token, 600))
        self.r.config["admin"]["timeout_seconds"] = 0
        with self.assertRaises(ContractError):
            admin_drain(self.r, self.p, dispatch_token=token)
        self.assertIsNotNone(self.r.db.execute("SELECT 1 FROM admin_dispatch WHERE token=?", (token,)).fetchone())

    def test_cross_host_expired_dispatch_cannot_penalize_recovering_host(self):
        other = Runtime(self.home, config={"admin": {"enabled": True, "antigravity_command": [sys.executable]}}, host="antigravity")
        self.addCleanup(other.close)
        token = claim_dispatch(other, self.p)
        self.assertTrue(token)
        row = self.r.db.execute("SELECT host_scope FROM admin_dispatch WHERE token=?", (token,)).fetchone()
        self.assertEqual(row["host_scope"], other._session_scope())
        self.r.db.execute("UPDATE admin_dispatch SET lease_until=? WHERE token=?", (time.time() - 1, token))
        self.assertIsNotNone(claim_dispatch(self.r, self.p))
        self.assertIsNone(executor_backoff(self.r, self.p, background_plan(self.r)))
        row = self.r.db.execute("SELECT reason FROM admin_dispatch_diagnostics WHERE project=? AND host_scope=?",
                                (self.p, other._session_scope())).fetchone()
        self.assertEqual(row["reason"], "worker_ack_expired")

    def test_legacy_unknown_dispatch_scope_never_invents_executor_owner(self):
        token = claim_dispatch(self.r, self.p)
        self.r.db.execute("UPDATE admin_dispatch SET host_scope='',lease_until=? WHERE token=?", (time.time() - 1, token))
        self.assertIsNotNone(claim_dispatch(self.r, self.p))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_executor_failures").fetchone()[0], 0)

    def test_expired_unacknowledged_worker_records_failure_and_backoff(self):
        token = claim_dispatch(self.r, self.p)
        self.r.db.execute("UPDATE admin_dispatch SET lease_until=? WHERE token=?", (time.time() - 1, token))
        self.assertIsNone(claim_dispatch(self.r, self.p))
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM admin_dispatch").fetchone())
        diagnostic = dispatch_diagnostic(self.r, self.p)
        self.assertEqual(diagnostic["reason"], "worker_launch_backoff")
        self.assertEqual(diagnostic["failure_reason"], "worker_ack_expired")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_immediate_python_failure_releases_and_redacts(self):
        token = claim_dispatch(self.r, self.p)
        process = Mock()
        process.wait.return_value = 7
        with patch.object(self.r, "_launch_background", return_value=process):
            with self.assertRaises(ChildProcessError):
                self.r._launch_admin(self.workspace, token)
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM admin_dispatch").fetchone())
        self.assertEqual(dispatch_diagnostic(self.r, self.p)["reason"], "worker_launch_backoff")
        self.assertFalse(control_status(self.r, self.p)["runner"]["running"])

    def test_python_survival_is_only_dispatch_not_model_ack(self):
        token = claim_dispatch(self.r, self.p)
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("private argv", 0.05)
        with patch.object(self.r, "_launch_background", return_value=process):
            result = self.r._launch_admin(self.workspace, token)
        self.assertEqual(result["reason"], "awaiting_worker_ack")
        self.assertFalse(control_status(self.r, self.p)["runner"]["running"])

    def test_failed_model_spawn_is_free_and_retries_have_backoff(self):
        with patch("agent_learning.admin.subprocess.Popen", side_effect=PermissionError(13, "PRIVATE_TOKEN")) as launch:
            first = admin_once(self.r, self.p)
            second = admin_once(self.r, self.p)
        self.assertEqual(first["status"], "not_started")
        self.assertEqual(first["reason"], "cli_permission_denied")
        self.assertEqual(first["errno"], 13)
        self.assertEqual(second["status"], "executor_not_ready")
        self.assertEqual(second["reason"], "executor_backoff")
        self.assertEqual(launch.call_count, 1)
        status = control_status(self.r, self.p)
        self.assertEqual(status["usage"]["used_calls"], 0)
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(status))

    def test_circuit_backoff_is_shared_per_executor_not_problem(self):
        plan = self.r.resolve_admin_executor(self.p, "chat")
        for _ in range(3):
            record_executor_failure(self.r, self.p, plan, "executor_launch_failed")
        self.r.feedback(self.p, "another-chat", "A second different observation")
        self.assertIsNone(claim_dispatch(self.r, self.p))
        self.assertEqual(dispatch_diagnostic(self.r, self.p)["reason"], "executor_circuit_open")
        self.assertGreater(executor_backoff(self.r, self.p, plan)["next_try"], time.time() + 890)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_unknown_paid_call_does_not_retry(self):
        with self.r.transaction():
            claim = claim_call(self.r, self.p, self.problem["id"], 1, "fingerprint", self.r.host_epoch(), 10)
        mark_launch_intent(self.r, claim["call_id"], claim["owner"], self.r.host_epoch())
        finish_call(self.r, claim["call_id"], "timeout_unknown")
        with patch("subprocess.Popen") as process:
            self.assertIsNone(claim_dispatch(self.r, self.p))
        process.assert_not_called()
        self.assertEqual(dispatch_diagnostic(self.r, self.p)["reason"], "unknown_call_requires_review")

    def test_status_diagnostic_is_readonly_without_models(self):
        before = self.r.db.total_changes
        with patch("subprocess.Popen") as process:
            status = control_status(self.r, self.p)
        self.assertEqual(self.r.db.total_changes, before)
        self.assertEqual(status["diagnostic"]["reason"], "ready")
        process.assert_not_called()

    def test_schema16_dispatch_migration_keeps_records_and_caps_unacked(self):
        now = time.time()
        self.r.db.execute("DROP TABLE admin_dispatch")
        self.r.db.execute("CREATE TABLE admin_dispatch(project TEXT PRIMARY KEY,token TEXT NOT NULL,host_epoch INTEGER NOT NULL,lease_until REAL NOT NULL,created REAL NOT NULL)")
        self.r.db.execute("INSERT INTO admin_dispatch VALUES(?,?,?,?,?)", (self.p, "old", self.r.host_epoch(), now + 600, now))
        self.r.db.execute("DROP TABLE admin_executor_failures")
        self.r.db.execute("DROP TABLE admin_dispatch_diagnostics")
        self.r.db.execute("PRAGMA user_version=16")
        other = Runtime(self.home, config={})
        self.addCleanup(other.close)
        self.assertEqual(other.db.execute("PRAGMA user_version").fetchone()[0], 24)
        row = other.db.execute("SELECT * FROM admin_dispatch WHERE token='old'").fetchone()
        self.assertEqual(row["phase"], "reserved")
        self.assertIsNone(row["claimed_at"])
        self.assertEqual(row["host_scope"], "", "schema 16 cannot prove the original host")
        self.assertEqual(row["lease_until"], now + 30)
        self.assertEqual(other.db.execute("SELECT COUNT(*) FROM problems WHERE project=?", (self.p,)).fetchone()[0], 1)

    def test_real_isolated_python_worker_acknowledges_without_any_model_work(self):
        token = claim_dispatch(self.r, self.p)
        # No eligible work remains; the real child may acknowledge and exit,
        # but cannot launch a reviewer. All configuration and logs are temporary.
        self.r.db.execute("UPDATE problems SET status='reviewed' WHERE project=?", (self.p,))
        config = self.home / "isolated-config.json"
        config.write_text(json.dumps({"admin": {"enabled": True, "command": [sys.executable]}}), encoding="utf-8")
        original, launched = self.r._launch_background, []
        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            launched.append(process)
            return process
        with patch.dict(os.environ, {"ALP_CONFIG": str(config)}), patch.object(self.r, "_launch_background", side_effect=launch):
            self.r._launch_admin(self.workspace, token)
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0].wait(timeout=20), 0)
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM admin_dispatch WHERE token=?", (token,)).fetchone())
        self.assertEqual(self.r.db.execute("SELECT reason FROM admin_dispatch_diagnostics WHERE project=?", (self.p,)).fetchone()[0], "worker_claimed")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)


class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-windows-launcher-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "node_modules" / "@provider" / "dsh" / "lib" / "bin.js"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("// fake package cli", encoding="utf-8")
        self.node = self.root / "node.exe"
        self.node.write_text("test placeholder; never executed", encoding="utf-8")
        self.shim = self.root / "dsh.cmd"
        self.shim.write_text('@ECHO off\nSET "_prog=node"\n'
            'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\\node_modules\\@provider\\dsh\\lib\\bin.js" %*\n', encoding="utf-8")

    def test_npm_shim_becomes_exact_node_argv_without_shell(self):
        with patch("subprocess.Popen") as launch:
            result = resolve_command([str(self.shim)], _windows=True)
        launch.assert_not_called()
        self.assertEqual(result["args"], [str(self.node.resolve()), str(self.script.resolve())])
        self.assertEqual(result["launch_kind"], "npm_node")

    def test_unknown_shim_missing_script_and_traversal_are_refused(self):
        original = self.shim.read_text()
        for text in ('@echo off\ncall secret-credential-loader\n',
                     original.replace("node_modules\\@provider", "..\\node_modules\\@provider"),
                     original.replace("bin.js", "absent.js"),
                     original.replace(" %*", " %* & invoke-more")):
            self.shim.write_text(text)
            with self.assertRaises(ValueError):
                resolve_command([str(self.shim)], _windows=True)

    def test_windows_script_requires_explicit_interpreter(self):
        script = self.root / "reviewer.ps1"
        script.write_text("never executed", encoding="utf-8")
        with patch("subprocess.Popen") as process:
            with self.assertRaises(ValueError):
                resolve_command([str(script)], _windows=True)
        process.assert_not_called()

    def test_capability_uses_help_without_prompt_and_rejects_missing_contract(self):
        launcher = self.root / ("codex.exe" if sys.platform == "win32" else "codex")
        launcher.write_text("never executed", encoding="utf-8")
        def unsupported(args, **kwargs):
            self.assertEqual(args[-2:], ["exec", "--help"])
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertNotIn("shell", kwargs)
            kwargs["stdout"].write(b"Usage: old cli without required flags")
            return Mock(returncode=0)
        with patch("agent_learning.admin_providers.subprocess.run", side_effect=unsupported) as help_call:
            result = executor_capability({"command": [str(launcher)], "provider": "codex"})
            repeated = executor_capability({"command": [str(launcher)], "provider": "codex"})
        self.assertEqual(help_call.call_count, 1)
        self.assertEqual(result, repeated)
        self.assertEqual(result["reason"], "cli_protocol_unsupported")


class DshSdkLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-dsh-sdk-launcher-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.anchor = self.root / "node_modules/@deepseek-ai/dsh/lib/bin.js"
        self.anchor.parent.mkdir(parents=True)
        self.anchor.write_text("// isolated trusted CLI anchor; not executed", encoding="utf-8")
        self.node = self.root / ("node.exe" if sys.platform == "win32" else "node")
        self.node.write_text("never executed", encoding="utf-8")
        self.command = [str(self.node), str(self.anchor)]
        self.profile_file = self.root / "trusted-profile.json"
        self.profile_file.write_text(json.dumps({"version": 1, "profile_directory": str(self.root / "profiles/web"),
            "entries": [{"name": "@deepseek-ai/dsh-llm-deepseek", "config": {}}]}), encoding="utf-8")
        from agent_learning.dsh_profile import resolve_profile
        self.profile = resolve_profile(str(self.profile_file))

    def test_real_dsh_anchor_builds_fixed_tool_free_runner_and_schema_stdin(self):
        schema = self.root / "response.schema.json"
        schema.write_text(json.dumps({"type": "object", "properties": {"decision": {"type": "string"}}}), encoding="utf-8")
        with patch("subprocess.Popen") as process:
            execution = build_execution(self.command, "dsh", "provider/model", self.root, schema, self.root / "result",
                                        "Private supplied evidence", 10, selection_mode="first", dsh_profile=self.profile)
        process.assert_not_called()
        self.assertTrue(execution["command_resolved"])
        self.assertEqual(execution["args"][0], str(self.node.resolve()))
        self.assertTrue(execution["args"][1].endswith("runner.mjs"))
        self.assertEqual(execution["args"][execution["args"].index("--sdk-anchor") + 1], str(self.anchor.resolve()))
        self.assertNotIn("--agent", execution["args"])
        self.assertNotIn("Private supplied evidence", " ".join(execution["args"]))
        event = json.loads(execution["stdin_data"])
        self.assertEqual(event["response_schema"]["type"], "object")
        self.assertEqual(event["message"]["content"], "Private supplied evidence")

    def test_sdk_probe_imports_without_prompt_and_preserves_custom_protocol(self):
        def probe(args, **kwargs):
            self.assertIn("--probe", args)
            self.assertIn("--sdk-anchor", args)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            kwargs["stdout"].write(b'{"protocol":"alp-dsh-sdk-v1","ready":true}\n')
            return Mock(returncode=0)
        plan = {"provider": "dsh", "command": self.command, "dsh_profile": self.profile}
        with patch("agent_learning.admin_providers.subprocess.run", side_effect=probe) as process:
            result = executor_capability(plan)
        self.assertTrue(result["ready"])
        self.assertEqual(result["checked"], "sdk")
        process.assert_called_once()
        self.assertIsNone(dsh_sdk_launcher([sys.executable]))

    def test_missing_or_wrong_standard_dsh_package_fails_closed(self):
        bad = self.root / ("dsh.exe" if sys.platform == "win32" else "dsh")
        bad.write_text("never executed", encoding="utf-8")
        with self.assertRaises(ValueError):
            dsh_sdk_launcher([str(bad)])


if __name__ == "__main__":
    unittest.main()
