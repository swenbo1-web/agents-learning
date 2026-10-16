"""升级命令、缓存状态和后台接线；全部数据与进程均使用临时目录。"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.cli import main
from agent_learning.antigravity import AntigravityAdapter
from agent_learning.install import install
from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime
from agent_learning import runtime as runtime_module
from agent_learning import software_updates as updates


class SoftwareUpdateIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="alp-update-integration-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home = self.base / "data"
        self.work = self.base / "workspace"
        self.work.mkdir()
        self.config = self.base / "config.json"
        self.config.write_text("{}", encoding="utf-8")
        environment = patch.dict(os.environ, {
            "ALP_HOME": str(self.home), "ALP_CONFIG": str(self.config),
            "ALP_RUNTIME_STORE": str(self.base / "releases"),
            "CODEX_HOME": str(self.base / "codex"), "PYTHONDONTWRITEBYTECODE": "1",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def runtime(self, config=None):
        runtime = Runtime(self.home, config=config)
        self.addCleanup(runtime.close)
        return runtime

    def cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["--home", str(self.home), "--workspace", str(self.work), *args])
        return code, json.loads(output.getvalue() or errors.getvalue())

    def hook(self, runtime, source="startup", **extra):
        return runtime.handle_hook({"hook_event_name": "SessionStart", "session_id": "chat",
                                    "cwd": str(self.work), "source": source, **extra})

    @staticmethod
    def release():
        return {"id": 100, "tag_name": "v99.0.0", "draft": False, "prerelease": False,
                "published_at": "2026-10-11T00:00:00Z",
                "html_url": "https://github.com/swenbo1-web/agents-learning/releases/tag/v99.0.0",
                "body": "untrusted release instructions must never become task context"}

    def test_defaults_and_partial_config_keep_other_capabilities_disabled(self):
        runtime = self.runtime({"software_updates": {"enabled": True}})
        self.assertEqual(runtime.config["software_updates"], {**updates.DEFAULTS, "enabled": True})
        self.assertFalse(runtime.config["admin"]["enabled"])
        self.assertFalse(runtime.config["scheduler"]["enabled"])
        self.assertFalse(runtime.config["hindsight"]["enabled"])
        self.assertEqual(runtime.db.execute("PRAGMA user_version").fetchone()[0], 17)

    def test_invalid_update_config_rejected_before_database_open(self):
        for supplied in ([], {"enabled": "yes"}, {"repository": "https://example.org/code"},
                         {"interval_seconds": 0}, {"unknown": True}):
            with self.subTest(config=supplied), self.assertRaises(ContractError):
                Runtime(self.home, config={"software_updates": supplied})
            self.assertFalse((self.home / "runtime.sqlite3").exists())

    def test_update_status_is_pure_cached_read_without_runtime(self):
        with patch("agent_learning.cli.Runtime", side_effect=AssertionError("must not open SQLite")), \
             patch.object(updates, "_fetch", side_effect=AssertionError("must not use network")):
            code, result = self.cli("update-status")
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "unchecked")
        self.assertFalse(self.home.exists())

    def test_explicit_check_allowed_when_automatic_disabled_without_runtime(self):
        with patch("agent_learning.cli.Runtime", side_effect=AssertionError("must not open SQLite")), \
             patch.object(updates, "_fetch", return_value=(200, {}, [self.release()])) as fetch:
            code, result = self.cli("update-check", "--force")
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "update_available")
        self.assertFalse(result["automatic_enabled"])
        fetch.assert_called_once()
        self.assertFalse((self.home / "runtime.sqlite3").exists())

    def test_manual_network_failure_reports_error_not_up_to_date(self):
        with patch.object(updates, "_fetch", side_effect=OSError("private details")):
            code, result = self.cli("update-check")
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["error_reason"], "network_error")
        self.assertNotIn("private details", json.dumps(result))

    def test_cli_invalid_config_returns_failure_without_database(self):
        self.config.write_text('{"software_updates":{"enabled":"yes"}}', encoding="utf-8")
        code, result = self.cli("update-status")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "error")
        self.assertFalse((self.home / "runtime.sqlite3").exists())

    def test_cli_upgrade_flags_dispatch(self):
        with patch("agent_learning.cli.install", return_value={"status": "updated"}) as installer:
            code, result = self.cli("install", str(self.base / "plugin"), "--upgrade",
                                    "--expected-release-id", "old-release", "--hook-mode", "enforce")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(installer.call_args.kwargs, {"runtime_store": None, "hook_mode": "enforce",
                                                "upgrade": True, "expected_release_id": "old-release", "host": "codex",
                                                "panel_workspace": str(self.work)})

    def test_export_cannot_silently_ignore_nondefault_host_instance(self):
        with patch("agent_learning.cli.install") as installer:
            code, result = self.cli("--host-instance", "other", "install", str(self.base / "plugin"))
        self.assertEqual(code, 1)
        self.assertIn("host_instance=default", result["reason"])
        installer.assert_not_called()
        self.assertFalse((self.home / "runtime.sqlite3").exists())

    def test_automatic_default_does_not_reserve_or_spawn(self):
        runtime = self.runtime()
        with patch.object(updates, "reserve_automatic_check") as reserve, \
             patch.object(runtime, "_launch_background") as launch:
            self.hook(runtime)
        reserve.assert_not_called()
        launch.assert_not_called()

    def test_root_startup_and_resume_schedule_outside_transaction(self):
        for source in ("startup", "resume", None):
            with self.subTest(source=source):
                runtime = Runtime(self.base / str(source), config={"software_updates": {"enabled": True}})
                with contextlib.closing(runtime), patch.object(runtime, "_launch_background") as launch:
                    launch.side_effect = lambda *args: self.assertFalse(runtime.db.in_transaction)
                    result = self.hook(runtime, source)
                self.assertIn("additionalContext", result["hookSpecificOutput"])
                launch.assert_called_once()
                self.assertEqual(launch.call_args.args[:2], (str(self.work), "update-check"))
                self.assertEqual(launch.call_args.args[2][0], "--reservation")
                self.assertEqual(len(launch.call_args.args[2][1]), 32)

    def test_compact_clear_and_subagent_do_not_schedule(self):
        runtime = self.runtime({"software_updates": {"enabled": True}})
        with patch.object(updates, "reserve_automatic_check") as reserve:
            self.hook(runtime, "compact")
            self.hook(runtime, "clear")
            self.hook(runtime, "startup", agent_id="child")
        reserve.assert_not_called()

    def test_prompt_and_tool_events_do_not_schedule(self):
        runtime = self.runtime({"software_updates": {"enabled": True}})
        with patch.object(updates, "reserve_automatic_check") as reserve:
            for kind in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
                runtime.handle_hook({"hook_event_name": kind, "session_id": "chat", "cwd": str(self.work),
                                     "prompt": "compiler", "tool_name": "example", "tool_response": {"success": True}})
        reserve.assert_not_called()

    def test_two_conversations_share_one_reservation(self):
        runtime = self.runtime({"software_updates": {"enabled": True}})
        with patch.object(runtime, "_launch_background") as launch:
            self.hook(runtime)
            self.hook(runtime, session_id="other-chat")
        launch.assert_called_once()

    def test_launch_failure_cancels_reservation_without_losing_context(self):
        runtime = self.runtime({"software_updates": {"enabled": True}})
        with patch.object(runtime, "_launch_background", side_effect=OSError("not started")), \
             patch.object(updates, "cancel_reserved_check", wraps=updates.cancel_reserved_check) as cancel:
            result = self.hook(runtime)
        cancel.assert_called_once()
        self.assertTrue(updates.automatic_check_due(self.home, runtime.config["software_updates"]))
        self.assertIn("additionalContext", result["hookSpecificOutput"])
        self.assertIn("systemMessage", result)

    def test_available_version_notice_does_not_enter_knowledge_context(self):
        runtime = self.runtime({"software_updates": {"enabled": True}})
        with patch.object(updates, "_fetch", return_value=(200, {}, [self.release()])):
            updates.check_updates(self.home, runtime.config["software_updates"], force=True)
        with patch.object(runtime, "_launch_background") as launch:
            result = self.hook(runtime)
        launch.assert_not_called()
        self.assertIn("99.0.0", result["systemMessage"])
        self.assertNotIn("99.0.0", result["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("untrusted release", json.dumps(result))

    def cache_release(self, runtime, **fields):
        with patch.object(updates, "_fetch", return_value=(200, {}, [{**self.release(), **fields}])):
            return updates.check_updates(self.home, runtime.config["software_updates"], force=True)

    def test_update_ask_is_once_per_version_across_codex_conversations(self):
        runtime = self.runtime({"software_updates": {"enabled": True}, "injection_max_chars": 128})
        self.cache_release(runtime, body="新增项目历史吸收与双宿主支持")
        with patch.object(runtime, "_launch_background") as launch:
            first = self.hook(runtime)
            second = self.hook(runtime, session_id="another-chat")
        launch.assert_not_called()
        self.assertIn("是否现在更新", first["systemMessage"])
        self.assertIn("历史吸收", first["systemMessage"])
        self.assertNotIn("systemMessage", second)
        self.assertLessEqual(len(first["hookSpecificOutput"]["additionalContext"]), 128)
        self.assertEqual(updates.notification_status(self.home, runtime.config["software_updates"])["state"], "delivered")

    def test_finished_background_result_can_notify_on_next_user_prompt(self):
        runtime = self.runtime({"software_updates": {"enabled": True}})
        with patch.object(runtime, "_launch_background") as launch:
            initial = self.hook(runtime)
        self.assertNotIn("systemMessage", initial)
        token = launch.call_args.args[2][1]
        with patch.object(updates, "_fetch", return_value=(200, {}, [self.release()])):
            updates.check_updates(self.home, runtime.config["software_updates"], reservation=token)
        with patch.object(runtime, "_launch_background") as launch:
            result = runtime.handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "chat",
                                          "cwd": str(self.work), "prompt": "继续"})
        launch.assert_not_called()
        self.assertIn("99.0.0", result["systemMessage"])

    def test_context_failure_does_not_claim_or_consume_update_notice(self):
        runtime = self.runtime({"software_updates": {"enabled": True}})
        self.cache_release(runtime)
        with patch.object(runtime, "_context", side_effect=ContractError("context failed")):
            with self.assertRaises(ContractError):
                self.hook(runtime)
        state = updates.notification_status(self.home, runtime.config["software_updates"])
        self.assertEqual(state["state"], "available")
        self.assertEqual(state["delivered_channels"], [])

    def test_delivery_state_write_failure_does_not_break_task_and_can_retry(self):
        runtime = self.runtime({"software_updates": {"enabled": True}})
        self.cache_release(runtime)
        with patch.object(updates, "confirm_update_notice", side_effect=OSError("not writable")):
            first = self.hook(runtime)
        self.assertIn("additionalContext", first["hookSpecificOutput"])
        self.assertNotIn("systemMessage", first)
        self.assertEqual(updates.notification_status(self.home, runtime.config["software_updates"])["state"], "available")
        second = self.hook(runtime, session_id="retry-chat")
        self.assertIn("99.0.0", second["systemMessage"])

    def ag_runtime(self, enabled=True):
        runtime = Runtime(self.home, config={"software_updates": {"enabled": enabled}}, host="antigravity")
        self.addCleanup(runtime.close)
        return runtime, AntigravityAdapter(runtime, self.work)

    def ag_invocation(self, adapter, number=0):
        return adapter.handle("PreInvocation", {"conversationId": "ag-chat", "workspacePaths": [str(self.work)],
            "modelName": "fixture", "transcriptPath": "not-read.jsonl", "invocationNum": number,
            "initialNumSteps": number})

    def test_antigravity_defaults_do_not_check_or_deliver_updates(self):
        runtime, adapter = self.ag_runtime(False)
        with patch.object(runtime, "_launch_background") as launch, \
             patch.object(updates, "reserve_automatic_check") as reserve:
            output = self.ag_invocation(adapter)
        launch.assert_not_called()
        reserve.assert_not_called()
        self.assertNotIn("更新提示", json.dumps(output, ensure_ascii=False))

    def test_antigravity_checks_in_background_with_local_shared_reservation(self):
        runtime, adapter = self.ag_runtime()
        with patch.object(runtime, "_launch_background") as launch:
            launch.side_effect = lambda *args: self.assertFalse(runtime.db.in_transaction)
            self.ag_invocation(adapter, 0)
            self.ag_invocation(adapter, 1)
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[1], "update-check")

    def test_antigravity_ephemeral_ask_is_not_saved_or_replayed_as_user_notice(self):
        runtime, adapter = self.ag_runtime()
        self.cache_release(runtime, body="新增历史知识吸收")
        with patch.object(runtime, "_launch_background") as launch:
            first = self.ag_invocation(adapter)
            replay = self.ag_invocation(adapter)
            later = self.ag_invocation(adapter, 1)
        launch.assert_not_called()
        self.assertNotIn("systemMessage", first)
        self.assertIn("是否现在更新", first["injectSteps"][-1]["ephemeralMessage"])
        self.assertIn("请向用户转述", first["injectSteps"][-1]["ephemeralMessage"])
        self.assertNotIn("99.0.0", json.dumps(replay))
        self.assertNotIn("99.0.0", json.dumps(later))
        for row in runtime.db.execute("SELECT output FROM antigravity_receipts"):
            self.assertNotIn("99.0.0", row["output"])
        codex = self.runtime({"software_updates": {"enabled": True}})
        result = self.hook(codex)
        self.assertIn("99.0.0", result["systemMessage"])

    def test_antigravity_notice_failure_retries_on_a_new_safe_event(self):
        runtime, adapter = self.ag_runtime()
        self.cache_release(runtime)
        with patch.object(updates, "confirm_update_notice", side_effect=OSError("cannot confirm")):
            first = self.ag_invocation(adapter)
        self.assertNotIn("99.0.0", json.dumps(first))
        later = self.ag_invocation(adapter, 1)
        self.assertIn("99.0.0", later["injectSteps"][-1]["ephemeralMessage"])

    def test_runtime_and_mcp_status_only_read_update_cache(self):
        runtime = self.runtime()
        project = runtime.project(self.work)
        with patch.object(updates, "_fetch", side_effect=AssertionError("no network")):
            status = runtime.status(project)
            mcp_status = Server(runtime, project).call("learning_status", {})
        self.assertEqual(status["software_updates"]["state"], "unchecked")
        self.assertEqual(mcp_status["software_updates"]["state"], "unchecked")

    def test_reserved_child_reloads_disabled_config_and_cancels(self):
        settings = {**updates.DEFAULTS, "enabled": True}
        token = updates.reserve_automatic_check(self.home, settings)
        with patch.object(updates, "_fetch", side_effect=AssertionError("disabled child must not request")):
            code, result = self.cli("update-check", "--reservation", token)
        self.assertEqual(code, 0)
        self.assertFalse(result["automatic_enabled"])
        self.assertTrue(updates.automatic_check_due(self.home, settings))
        self.assertFalse((self.home / "runtime.sqlite3").exists())

    def test_background_entry_ignores_workspace_package(self):
        runtime = self.runtime()
        attacker = self.work / "agent_learning"
        attacker.mkdir()
        (attacker / "__init__.py").write_text("", encoding="utf-8")
        (attacker / "cli.py").write_text('raise RuntimeError("workspace_package_selected")', encoding="utf-8")
        with patch("subprocess.Popen") as spawn:
            runtime._launch_background(self.work, "update-status", [])
        argv = spawn.call_args.args[0]
        self.assertEqual(argv[1], "-B")
        self.assertEqual(Path(argv[2]), Path(__file__).resolve().parents[1] / "scripts/learning.py")
        result = subprocess.run(argv, cwd=self.work, env=spawn.call_args.kwargs["env"],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(json.loads(result.stdout)["state"], "unchecked")
        self.assertNotIn("workspace_package_selected", result.stderr.decode("utf-8", "replace"))

    def test_background_fixed_entry_rejects_modified_release(self):
        receipt = install(self.base / "plugin", runtime_store=self.base / "releases")
        fixed = Path(receipt["runtime_directory"])
        (fixed / "README.md").write_text("changed", encoding="utf-8")
        runtime = self.runtime()
        with patch.object(runtime_module, "__file__", str(fixed / "agent_learning/runtime.py")), \
             patch("subprocess.Popen") as spawn:
            runtime._launch_background(self.work, "update-status", [])
        result = subprocess.run(spawn.call_args.args[0], cwd=self.work,
                                env=spawn.call_args.kwargs["env"], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("release", result.stderr.decode("utf-8", "replace"))

    def test_account_change_does_not_remove_public_update_cache(self):
        runtime = self.runtime()
        with patch.object(updates, "_fetch", return_value=(200, {}, [self.release()])):
            updates.check_updates(self.home, runtime.config["software_updates"], force=True)
        runtime.account_changed("actual-account-change")
        self.assertTrue(updates.update_status(self.home, runtime.config["software_updates"])["update_available"])


if __name__ == "__main__":
    unittest.main()
