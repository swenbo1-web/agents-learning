"""真实 Runtime／CLI／宿主适配器的离线路由整合；不启动模型或真实 Hooks。"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_queue import claim_dispatch, release_dispatch, scan_signature
from agent_learning.antigravity import AntigravityAdapter
from agent_learning.cli import main
from agent_learning.runtime import ContractError, Runtime
from tests.test_module6_queue import fake_reviewer


class AdminRoutingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="alp-routing-integrated-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        self.workspace = root / "project"
        self.workspace.mkdir()
        self.home = root / "state"
        self.config_path = root / "isolated-config.json"
        self.config_path.write_text("{}", encoding="utf-8")
        self.environment = patch.dict(os.environ, {"ALP_CONFIG": str(self.config_path), "ALP_HOME": str(self.home),
                                                   "PYTHONDONTWRITEBYTECODE": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.r = self.runtime()
        self.p = self.r.project(self.workspace)

    def runtime(self, *, host="codex", instance="default", config=None):
        runtime = Runtime(self.home, config={"admin": {"command": [sys.executable]}} if config is None else config,
                          host=host, host_instance=instance)
        self.addCleanup(runtime.close)
        return runtime

    def cli(self, *args, workspace=True, host="codex", instance="default"):
        output, error = io.StringIO(), io.StringIO()
        arguments = ["--home", str(self.home), "--host", host, "--host-instance", instance]
        if workspace:
            arguments += ["--workspace", str(self.workspace)]
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(arguments + list(args))
        value = output.getvalue() or error.getvalue()
        return code, json.loads(value)

    def codex(self, event, *, model="observed-codex-model", conversation="chat", **fields):
        raw = {"session_id": conversation, "cwd": str(self.workspace), "hook_event_name": event,
               "model": model, "transcript_path": None, **fields}
        with patch.object(self.r, "_launch_background", side_effect=AssertionError("background model unexpectedly launched")):
            return self.r.handle_hook(raw)

    def native(self, runtime=None):
        runtime = runtime or self.runtime(host="antigravity")
        return runtime, AntigravityAdapter(runtime, self.workspace)

    def invocation(self, adapter, number=0, model="observed-native-model", **fields):
        return adapter.handle("PreInvocation", {"conversationId": "native-chat", "workspacePaths": [str(self.workspace)],
            "transcriptPath": "not-read.jsonl", "modelName": model,
            "invocationNum": number, "initialNumSteps": number, **fields})

    def test_cli_configuration_requires_explicit_workspace_and_stays_off_initially(self):
        code, state = self.cli("admin-status")
        self.assertEqual(code, 0)
        self.assertFalse(state["policy"]["enabled"])
        code, failure = self.cli("admin-configure", "--enabled", "on", workspace=False)
        self.assertEqual(code, 1)
        self.assertIn("explicit --workspace", failure["reason"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_host_policies").fetchone()[0], 0)

    def test_cli_new_project_host_policy_enables_only_selected_host(self):
        code, configured = self.cli("admin-configure", "--enabled", "on")
        self.assertEqual(code, 0)
        self.assertTrue(configured["enabled"])
        self.assertEqual(configured["model_mode"], "first")
        code, codex = self.cli("admin-status")
        self.assertEqual(code, 0)
        self.assertTrue(codex["policy"]["enabled"])
        self.assertFalse(codex["ready"])
        code, native = self.cli("admin-status", host="antigravity")
        self.assertEqual(code, 0)
        self.assertFalse(native["policy"]["enabled"])

    def test_cli_on_reopens_a_previous_visual_admission_pause(self):
        code, _ = self.cli("admin-configure", "--enabled", "on")
        self.assertEqual(code, 0)
        self.r.db.execute("INSERT INTO admin_control_state VALUES(?,?,0,1,0)", (self.p, self.r._session_scope()))
        self.assertFalse(self.r.admin_effective_config(self.p)["enabled"])
        code, _ = self.cli("admin-configure", "--enabled", "on")
        self.assertEqual(code, 0)
        self.assertTrue(self.r.admin_effective_config(self.p)["enabled"])
        code, _ = self.cli("admin-configure", "--enabled", "off")
        self.assertEqual(code, 0)
        self.assertFalse(self.r.admin_effective_config(self.p)["enabled"])
        code, configured = self.cli("admin-configure", "--enabled", "on", "--model-mode", "fixed", "--model", "gemini-model", host="antigravity")
        self.assertEqual(code, 0)
        self.assertEqual(configured["host_scope"], "antigravity:default")
        code, native = self.cli("admin-status", host="antigravity")
        self.assertEqual(native["executor"]["provider"], "antigravity")
        self.assertEqual(native["executor"]["model"], "gemini-model")
        self.assertFalse(self.r.config["admin"]["enabled"])

    def test_cli_fixed_model_does_not_enable_automatic_publication(self):
        code, state = self.cli("admin-configure", "--enabled", "on", "--model-mode", "fixed", "--model", "provider/model:v2")
        self.assertEqual(code, 0)
        self.assertEqual(state["model"], "provider/model:v2")
        reopened = self.runtime()
        self.assertFalse(reopened.config["admin"]["auto_publish"])
        self.assertFalse(reopened.config["scheduler"]["enabled"])
        self.assertEqual(reopened.resolve_admin_executor(self.p)["model_source"], "operator_fixed")

    def test_cli_only_toggle_preserves_fixed_model_and_custom_wrapper(self):
        command = [sys.executable, str(self.workspace / "custom-review-wrapper.py")]
        command_file = self.workspace / "trusted-command.json"
        command_file.write_text(json.dumps(command), encoding="utf-8")
        code, first = self.cli("admin-configure", "--enabled", "on", "--provider", "codex",
                               "--model-mode", "fixed", "--model", "provider/fixed-model:v2",
                               "--command-file", str(command_file))
        self.assertEqual(code, 0)
        for state in ("off", "on"):
            code, policy = self.cli("admin-configure", "--enabled", state)
            self.assertEqual(code, 0)
            self.assertEqual(policy["enabled"], state == "on")
            self.assertEqual(policy["provider"], "codex")
            self.assertEqual(policy["model_mode"], "fixed")
            self.assertEqual(policy["model"], "provider/fixed-model:v2")
            self.assertEqual(policy["command"], command)
        code, same = self.cli("admin-configure", "--enabled", "on")
        self.assertEqual(code, 0)
        self.assertFalse(same["changed"])
        self.assertEqual(same["policy_version"], first["policy_version"] + 2)

    def test_codex_hook_model_observation_tracks_fresh_root_and_replay(self):
        self.codex("UserPromptSubmit", turn_id="turn-1", prompt="first", model="first-model")
        first = dict(self.r.db.execute("SELECT * FROM admin_model_observations").fetchone())
        self.codex("UserPromptSubmit", turn_id="turn-2", prompt="second", model="second-model")
        current = dict(self.r.db.execute("SELECT * FROM admin_model_observations").fetchone())
        self.assertEqual(current["model"], "second-model")
        self.assertGreater(current["version"], first["version"])
        self.codex("UserPromptSubmit", turn_id="turn-1", prompt="first", model="first-model")
        replay = dict(self.r.db.execute("SELECT * FROM admin_model_observations").fetchone())
        self.assertEqual(replay, current)

    def test_codex_child_and_invalid_model_do_not_replace_root_choice(self):
        self.codex("SessionStart", model="root-model")
        self.codex("SubagentStart", model="child-model", agent_id="child-a")
        self.codex("UserPromptSubmit", turn_id="bad-turn", prompt="next", model="bad model")
        row = self.r.db.execute("SELECT model FROM admin_model_observations WHERE project=?", (self.p,)).fetchone()
        self.assertEqual(row["model"], "root-model")
        metric = self.r.db.execute("SELECT value FROM metrics WHERE project=? AND name='admin_model_observation_rejected'", (self.p,)).fetchone()
        self.assertEqual(metric[0], 1)

    def test_codex_new_account_requires_new_root_observation(self):
        self.codex("SessionStart", model="old-model")
        self.r.admin_configure(self.p, True, model_mode="current")
        old = self.r.resolve_admin_executor(self.p, "chat")
        self.r.account_changed(notification_id="switch-one")
        with self.assertRaisesRegex(ContractError, "current_model_unavailable"):
            self.r.resolve_admin_executor(self.p, "chat")
        self.codex("UserPromptSubmit", turn_id="new-account-turn", prompt="resume", model="new-model")
        new = self.r.resolve_admin_executor(self.p, "chat")
        self.assertEqual(new["model"], "new-model")
        self.assertNotEqual(old["session_epoch"], new["session_epoch"])

    def test_native_fresh_invocation_observes_model_and_old_replay_cannot_replace_it(self):
        native, adapter = self.native()
        self.invocation(adapter, 0, "first-native-model")
        self.invocation(adapter, 1, "second-native-model")
        row = dict(native.db.execute("SELECT * FROM admin_model_observations WHERE project=? AND host_scope=?",
                                    (self.p, native.host_scope)).fetchone())
        self.assertEqual(row["model"], "second-native-model")
        self.invocation(adapter, 1, "stale-diagnostic-model")
        same = dict(native.db.execute("SELECT * FROM admin_model_observations WHERE project=? AND host_scope=?",
                                     (self.p, native.host_scope)).fetchone())
        self.assertEqual(row, same)
        self.invocation(adapter, 0, "first-native-model")
        final = native.db.execute("SELECT model FROM admin_model_observations WHERE project=? AND host_scope=?",
                                  (self.p, native.host_scope)).fetchone()[0]
        self.assertEqual(final, "second-native-model")

    def test_native_invalid_model_does_not_overwrite_valid_choice(self):
        native, adapter = self.native()
        self.invocation(adapter, 0, "valid-native-model")
        self.invocation(adapter, 1, "bad model")
        self.assertEqual(native.db.execute("SELECT model FROM admin_model_observations WHERE project=? AND host_scope=?",
                                          (self.p, native.host_scope)).fetchone()[0], "valid-native-model")

    def test_native_oversize_model_identifier_cannot_be_truncated_into_valid_selection(self):
        native, adapter = self.native()
        self.invocation(adapter, 0, "valid-native-model")
        self.invocation(adapter, 1, "a" * 201)
        self.assertEqual(native.db.execute("SELECT model FROM admin_model_observations WHERE project=? AND host_scope=?",
                                          (self.p, native.host_scope)).fetchone()[0], "valid-native-model")

    def test_background_process_keeps_host_instance_and_project_parameters(self):
        native = self.runtime(host="antigravity", instance="secondary")
        with patch("subprocess.Popen") as launch:
            native._launch_background(self.workspace, "admin-batch", ["--dispatch-token", "token"])
        arguments = launch.call_args.args[0]
        self.assertEqual(arguments[arguments.index("--host") + 1], "antigravity")
        self.assertEqual(arguments[arguments.index("--host-instance") + 1], "secondary")
        self.assertEqual(arguments[arguments.index("--workspace") + 1], str(self.workspace))
        self.assertEqual(arguments[arguments.index("--home") + 1], str(self.home))
        self.assertIn("-B", arguments)
        self.assertEqual(launch.call_args.kwargs["env"]["ALP_DISABLED"], "1")

    def test_schema13_migration_preserves_formal_knowledge_and_history_jobs(self):
        self.r.task_update(self.p, "chat", "compiler result")
        problem = self.r.feedback(self.p, "chat", "known compiler guidance", "professional")
        packet = self.r.review_packet(self.p, problem["id"])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="compiler guidance", content="review include directories", category="professional",
            applicability="historical compiler project", limitations="reported material", validation_level="reported",
            evidence_refs=[item["id"] for item in packet["events"]])
        knowledge = self.r.publish(self.p, candidate["candidate_id"])
        (self.workspace / "README.md").write_text("existing project history", encoding="utf-8")
        job = self.r.history_prepare(self.p, self.workspace)
        before = {table: self.r.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                  for table in ("events", "problems", "candidates", "knowledge", "history_jobs")}
        with self.r.transaction():
            for table in ("admin_host_policies", "admin_model_observations", "admin_execution_plans"):
                self.r.db.execute("DROP TABLE " + table)
            self.r.db.execute("PRAGMA user_version=13")
        self.r.close()
        upgraded = self.runtime()
        self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 17)
        for table, count in before.items():
            self.assertEqual(upgraded.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], count, table)
        self.assertEqual(upgraded.read(self.p, knowledge["knowledge_id"])["content"], "review include directories")
        self.assertEqual(upgraded.history_status(self.p, job["job_id"])["job"]["state"], "prepared")
        for table in ("admin_host_policies", "admin_model_observations", "admin_execution_plans"):
            self.assertEqual(upgraded.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)

    def test_disabled_project_policy_blocks_dispatch_even_when_legacy_global_is_enabled(self):
        self.r.config["admin"]["enabled"] = True
        self.r.task_update(self.p, "chat", "compiler issue")
        self.r.feedback(self.p, "chat", "compiler issue", "operation")
        token = claim_dispatch(self.r, self.p)
        self.assertIsNotNone(token)
        release_dispatch(self.r, self.p, token)
        self.r.admin_configure(self.p, False)
        self.assertIsNone(claim_dispatch(self.r, self.p))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_dispatch").fetchone()[0], 0)

    def test_disabled_project_policy_prevents_stop_hook_background_launch(self):
        self.r.config["admin"]["enabled"] = True
        self.r.task_update(self.p, "chat", "compiler issue")
        self.r.feedback(self.p, "chat", "compiler issue", "operation")
        self.r.admin_configure(self.p, False)
        with patch.object(self.r, "_launch_admin") as launch:
            self.codex("Stop", turn_id="disabled-stop", last_assistant_message="reported result")
        launch.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_dispatch").fetchone()[0], 0)

    def test_scheduler_admin_state_uses_per_host_policy_instead_of_shared_global_flag(self):
        self.r.config["scheduler"]["enabled"] = True
        self.r.config["admin"]["enabled"] = True
        self.r.task_update(self.p, "chat", "compiler issue")
        self.r.feedback(self.p, "chat", "compiler issue", "operation")
        self.r.admin_configure(self.p, False)
        _, state, _, reason = self.r._scheduler_admin_state(self.p, self.r._scheduler_config())
        self.assertEqual(state, "disabled")
        self.assertEqual(reason, "admin_disabled")
        self.r.admin_configure(self.p, True, model_mode="default", command=[sys.executable])
        _, state, _, _ = self.r._scheduler_admin_state(self.p, self.r._scheduler_config())
        self.assertEqual(state, "ready")
        native = self.runtime(host="antigravity", config={"scheduler": {"enabled": True}})
        _, state, _, reason = native._scheduler_admin_state(self.p, native._scheduler_config())
        self.assertFalse(native.admin_policy(self.p)["enabled"])
        self.assertEqual(state, "ready")
        self.assertEqual(reason, "")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            tick = native.scheduler_tick(self.p, max_jobs=1, max_seconds=3)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(tick["items"][0]["host_scope"], "codex:default")
        plan = json.loads(native.db.execute("SELECT body FROM admin_execution_plans").fetchone()[0])
        self.assertEqual(plan["host_scope"], "codex:default")
        self.assertEqual(native._session_scope(), "antigravity:default")

    def test_policy_and_model_changes_invalidate_scan_signature(self):
        before = scan_signature(self.r, self.p)
        self.r.admin_configure(self.p, True, model_mode="current")
        configured = scan_signature(self.r, self.p)
        self.assertNotEqual(before, configured)
        self.codex("SessionStart", model="first-model")
        first = scan_signature(self.r, self.p)
        self.assertNotEqual(first, configured)
        self.codex("UserPromptSubmit", turn_id="next-model", prompt="continue", model="second-model")
        self.assertNotEqual(scan_signature(self.r, self.p), first)

    def test_native_missing_cli_status_is_explicit_and_cannot_claim_dispatch(self):
        missing = str(self.workspace / "missing-agy-executable")
        native = self.runtime(host="antigravity", config={"admin": {"antigravity_command": [missing]}})
        conversation = native.host_conversation("native-chat")
        native.task_update(self.p, conversation, "native compiler issue")
        native.feedback(self.p, conversation, "native compiler issue", "operation")
        native.admin_configure(self.p, True, model_mode="default")
        code, state = self.cli("admin-status", host="antigravity")
        self.assertEqual(code, 0)
        self.assertTrue(state["policy"]["enabled"])
        self.assertTrue(state["routing_ready"])
        self.assertFalse(state["ready"])
        self.assertEqual(state["reason"], "cli_unavailable")
        self.assertEqual(state["availability"]["authentication"], "provider_cli_managed_not_checked")
        self.assertIsNone(claim_dispatch(native, self.p))
        self.assertEqual(native.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_current_queue_ignores_more_than_ten_foreign_host_cases_before_local_case(self):
        native = self.runtime(host="antigravity")
        for index in range(12):
            conversation = native.host_conversation(f"old-native-{index}")
            native.task_update(self.p, conversation, "old native compiler issue")
            native.feedback(self.p, conversation, f"old native compiler issue {index}", "operation")
        self.codex("SessionStart", model="current-codex-model")
        self.r.task_update(self.p, "chat", "current codex compiler issue")
        problem = self.r.feedback(self.p, "chat", "current codex compiler issue", "operation")
        self.r.admin_configure(self.p, True, model_mode="current")
        token = claim_dispatch(self.r, self.p)
        self.assertIsNotNone(token)
        release_dispatch(self.r, self.p, token)
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "skip")
        self.assertEqual(model.call_count, 1)
        call = self.r.db.execute("SELECT problem FROM admin_calls WHERE state='skip'").fetchone()
        self.assertEqual(call["problem"], problem["id"])
        plan = self.r.db.execute("SELECT body FROM admin_execution_plans").fetchone()
        self.assertEqual(json.loads(plan["body"])["model"], "current-codex-model")


if __name__ == "__main__":
    unittest.main()
