"""管理员宿主／模型路由的离线策略和快照边界检查。"""
from __future__ import annotations

import contextlib
import copy
import sqlite3
import tempfile
from pathlib import Path
import unittest

from agent_learning.admin_routing import (AdminRoutingMixin, install_admin_routing,
                                          validate_admin_routing_config, validate_executor_command,
                                          validate_model_identifier)
from agent_learning.host_sessions import HostSessionMixin
from agent_learning.runtime import ContractError


class RoutingRuntime(AdminRoutingMixin, HostSessionMixin):
    def __init__(self, database, host="codex", instance="default", config=None):
        self.host, self.host_instance = host, instance
        self.host_scope = host + ":" + instance
        self.config = {"admin": {"enabled": False, "command": ["codex"], "max_calls_per_day": 5,
                                 "max_reported_tokens_per_day": None, "token_reservation_per_call": 4000}}
        if config:
            self.config["admin"].update(copy.deepcopy(config))
        self.db = sqlite3.connect(database, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE IF NOT EXISTS host_state(id INTEGER PRIMARY KEY,epoch INTEGER NOT NULL)")
        self.db.execute("INSERT OR IGNORE INTO host_state VALUES(1,0)")
        self.db.execute("CREATE TABLE IF NOT EXISTS host_scopes(scope TEXT PRIMARY KEY,epoch INTEGER NOT NULL)")
        self.db.execute("INSERT OR IGNORE INTO host_scopes VALUES(?,0)", (self.host_scope,))
        with self.transaction():
            install_admin_routing(self)

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def host_epoch(self):
        return self.db.execute("SELECT epoch FROM host_state WHERE id=1").fetchone()[0]

    def assert_host_epoch(self, expected_epoch):
        current = self.host_epoch()
        if expected_epoch is not None and expected_epoch != current:
            raise ContractError("reconnect_required: account context changed")
        return current

    def changed_account(self):
        with self.transaction():
            self.db.execute("UPDATE host_state SET epoch=epoch+1 WHERE id=1")
            self.db.execute("UPDATE host_scopes SET epoch=? WHERE scope=?", (self.host_epoch(), self.host_scope))


class AdminRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "routing.sqlite"
        self.r = self.runtime()
        self.project = "project-one"

    def runtime(self, host="codex", instance="default", config=None):
        runtime = RoutingRuntime(self.database, host, instance, config)
        self.addCleanup(runtime.db.close)
        return runtime

    def enable(self, **kwargs):
        return self.r.admin_configure(self.project, True, **kwargs)

    def test_storage_install_requires_write_transaction_and_replays_idempotently(self):
        with self.assertRaisesRegex(ContractError, "write transaction"):
            install_admin_routing(self.r)
        with self.r.transaction():
            install_admin_routing(self.r)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_host_policies").fetchone()[0], 0)

    def test_defaults_are_disabled_and_cannot_resolve_admitted_call(self):
        policy = self.r.admin_policy(self.project)
        self.assertFalse(policy["enabled"])
        self.assertEqual(policy["policy_version"], 0)
        with self.assertRaisesRegex(ContractError, "administrator_disabled"):
            self.r.resolve_admin_executor(self.project)
        executor = self.r.resolve_admin_executor(self.project, require_enabled=False)
        self.assertEqual(executor["provider"], "codex")
        self.assertEqual(executor["model"], "")
        self.assertEqual(executor["model_source"], "provider_cli_default")

    def test_project_and_host_scope_enable_is_independent(self):
        self.enable()
        other = self.runtime("antigravity")
        self.assertTrue(self.r.admin_policy(self.project)["enabled"])
        self.assertFalse(self.r.admin_policy("other-project")["enabled"])
        self.assertFalse(other.admin_policy(self.project)["enabled"])
        other.admin_configure(self.project, True)
        self.assertEqual(other.resolve_admin_executor(self.project)["provider"], "antigravity")
        self.assertEqual(other.resolve_admin_executor(self.project)["command"], ["agy"])

    def test_host_instances_share_knowledge_identity_but_not_policy(self):
        self.enable()
        other = self.runtime("codex", "secondary")
        self.assertFalse(other.admin_policy(self.project)["enabled"])
        other.admin_configure(self.project, True, model_mode="fixed", model="gpt-6-mini")
        self.assertEqual(other.resolve_admin_executor(self.project)["model"], "gpt-6-mini")
        self.assertEqual(self.r.resolve_admin_executor(self.project)["model"], "")

    def test_legacy_explicit_enabled_config_keeps_both_existing_hosts_compatible(self):
        codex = self.runtime(config={"enabled": True})
        native = self.runtime("antigravity", config={"enabled": True})
        self.assertTrue(codex.admin_policy(self.project)["enabled"])
        self.assertTrue(native.admin_policy(self.project)["enabled"])
        native.admin_configure(self.project, False)
        self.assertFalse(native.admin_policy(self.project)["enabled"])
        self.assertTrue(codex.admin_policy(self.project)["enabled"])

    def test_policy_replay_is_idempotent_and_change_increments_version(self):
        first = self.enable()
        same = self.enable(expected_policy_version=first["policy_version"])
        self.assertFalse(same["changed"])
        self.assertEqual(same["policy_version"], 1)
        changed = self.r.admin_configure(self.project, False, expected_policy_version=1)
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["policy_version"], 2)
        with self.assertRaisesRegex(ContractError, "policy changed"):
            self.enable(expected_policy_version=1)

    def test_default_mode_does_not_claim_gui_model_inheritance(self):
        self.enable()
        self.r.admin_observe_model(self.project, "conversation", "gpt-current")
        executor = self.r.resolve_admin_executor(self.project, "conversation")
        self.assertEqual(executor["model"], "")
        self.assertEqual(executor["model_source"], "provider_cli_default")
        self.assertIsNone(executor["model_observation_signature"])

    def test_fixed_model_is_explicit_and_supports_alias_identifiers(self):
        self.enable(provider="antigravity", model_mode="fixed", model="google/gemini:fast-2.5")
        executor = self.r.resolve_admin_executor(self.project)
        self.assertEqual(executor["provider"], "antigravity")
        self.assertEqual(executor["model"], "google/gemini:fast-2.5")
        self.assertEqual(executor["model_source"], "operator_fixed")
        self.assertEqual(executor["command"], ["agy"])

    def test_current_model_requires_an_active_root_observation(self):
        self.enable(model_mode="current")
        with self.assertRaisesRegex(ContractError, "current_model_unavailable"):
            self.r.resolve_admin_executor(self.project, "conversation")
        ignored = self.r.admin_observe_model(self.project, "conversation", "child-model", agent_id="child")
        self.assertFalse(ignored["recorded"])
        self.r.admin_observe_model(self.project, "conversation", "root-model")
        executor = self.r.resolve_admin_executor(self.project, "conversation")
        self.assertEqual(executor["model"], "root-model")
        self.r.admin_observe_model(self.project, "conversation", active=False)
        with self.assertRaisesRegex(ContractError, "current_model_unavailable"):
            self.r.resolve_admin_executor(self.project, "conversation")

    def test_observation_replay_keeps_snapshot_but_changed_model_invalidates_it(self):
        self.enable(model_mode="current")
        first = self.r.admin_observe_model(self.project, "conversation", "first-model")
        snapshot = self.r.resolve_admin_executor(self.project, "conversation")
        replay = self.r.admin_observe_model(self.project, "conversation", "first-model")
        self.assertFalse(replay["changed"])
        self.assertEqual(first["version"], replay["version"])
        self.r.assert_admin_executor_snapshot(self.project, snapshot)
        self.r.admin_observe_model(self.project, "conversation", "second-model")
        with self.assertRaisesRegex(ContractError, "executor_changed"):
            self.r.assert_admin_executor_snapshot(self.project, snapshot)

    def test_multiple_distinct_current_models_require_exact_conversation(self):
        self.enable(model_mode="current")
        self.r.admin_observe_model(self.project, "a", "model-a")
        self.r.admin_observe_model(self.project, "b", "model-b")
        with self.assertRaisesRegex(ContractError, "current_model_ambiguous"):
            self.r.resolve_admin_executor(self.project)
        self.assertEqual(self.r.resolve_admin_executor(self.project, "a")["model"], "model-a")
        self.assertEqual(self.r.resolve_admin_executor(self.project, "b")["model"], "model-b")

    def test_multiple_same_current_models_can_resolve_without_guessing_latest(self):
        self.enable(model_mode="current")
        self.r.admin_observe_model(self.project, "a", "shared-model")
        self.r.admin_observe_model(self.project, "b", "shared-model")
        self.assertEqual(self.r.resolve_admin_executor(self.project)["model"], "shared-model")

    def test_other_project_or_host_model_never_supplies_current_model(self):
        self.enable(model_mode="current")
        self.r.admin_observe_model("other-project", "conversation", "wrong-project-model")
        native = self.runtime("antigravity")
        native.admin_observe_model(self.project, "conversation", "wrong-host-model")
        with self.assertRaisesRegex(ContractError, "current_model_unavailable"):
            self.r.resolve_admin_executor(self.project, "conversation")

    def test_current_host_model_cannot_be_reused_for_another_provider(self):
        self.enable(provider="antigravity", model_mode="current")
        self.r.admin_observe_model(self.project, "conversation", "codex-model")
        with self.assertRaisesRegex(ContractError, "provider_mismatch"):
            self.r.resolve_admin_executor(self.project, "conversation")

    def test_account_change_keeps_preference_but_requires_new_model_observation(self):
        self.enable(model_mode="current")
        self.r.admin_observe_model(self.project, "conversation", "old-account-model")
        snapshot = self.r.resolve_admin_executor(self.project, "conversation")
        self.r.changed_account()
        self.assertTrue(self.r.admin_policy(self.project)["enabled"])
        with self.assertRaisesRegex(ContractError, "current_model_unavailable"):
            self.r.resolve_admin_executor(self.project, "conversation")
        with self.assertRaises(ContractError):
            self.r.assert_admin_executor_snapshot(self.project, snapshot)
        self.r.admin_observe_model(self.project, "conversation", "new-account-model", expected_epoch=1)
        self.assertEqual(self.r.resolve_admin_executor(self.project, "conversation")["model"], "new-account-model")

    def test_stale_observation_and_configuration_epochs_cannot_write(self):
        self.r.changed_account()
        for action in (
                lambda: self.r.admin_observe_model(self.project, "conversation", "model", expected_epoch=0),
                lambda: self.r.admin_configure(self.project, True, expected_epoch=0)):
            with self.assertRaisesRegex(ContractError, "reconnect_required"):
                action()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_model_observations").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_host_policies").fetchone()[0], 0)

    def test_global_governance_epoch_change_invalidates_call_snapshot(self):
        self.enable()
        snapshot = self.r.resolve_admin_executor(self.project)
        native = self.runtime("antigravity")
        native.changed_account()
        with self.assertRaisesRegex(ContractError, "executor_changed"):
            self.r.assert_admin_executor_snapshot(self.project, snapshot)

    def test_policy_version_change_invalidates_snapshot_without_quota_mutation(self):
        self.enable()
        snapshot = self.r.resolve_admin_executor(self.project)
        before = copy.deepcopy(self.r.config)
        self.r.admin_configure(self.project, True, model_mode="fixed", model="new-model")
        with self.assertRaisesRegex(ContractError, "executor_changed"):
            self.r.assert_admin_executor_snapshot(self.project, snapshot)
        self.assertEqual(self.r.config, before)

    def test_snapshot_forgery_or_project_conversation_swap_is_rejected(self):
        self.enable()
        snapshot = self.r.resolve_admin_executor(self.project, "a")
        forged = {**snapshot, "model": "forged"}
        for action in (
                lambda: self.r.assert_admin_executor_snapshot(self.project, forged),
                lambda: self.r.assert_admin_executor_snapshot("other-project", snapshot),
                lambda: self.r.assert_admin_executor_snapshot(self.project, snapshot, "b")):
            with self.assertRaises(ContractError):
                action()

    def test_explicit_command_is_frozen_per_policy_and_does_not_modify_config(self):
        self.enable(command=["C:/Tools/codex.exe"])
        snapshot = self.r.resolve_admin_executor(self.project)
        self.r.config["admin"]["command"] = ["different-codex"]
        self.assertEqual(self.r.resolve_admin_executor(self.project)["command"], ["C:/Tools/codex.exe"])
        self.r.assert_admin_executor_snapshot(self.project, snapshot)

    def test_implicit_provider_command_is_also_frozen_when_operator_configures(self):
        self.enable(provider="antigravity")
        self.r.config["admin"]["antigravity_command"] = ["other-agy"]
        self.assertEqual(self.r.resolve_admin_executor(self.project)["command"], ["agy"])

    def test_same_provider_model_mode_change_preserves_existing_custom_command(self):
        custom = ["C:/Tools/wrapper-codex.exe"]
        self.enable(provider="host", model_mode="fixed", model="first-model", command=custom)
        policy = self.r.admin_configure(self.project, True, provider="codex", model_mode="default")
        self.assertEqual(policy["command"], custom)
        self.assertEqual(policy["model_mode"], "default")
        self.assertEqual(self.r.resolve_admin_executor(self.project)["command"], custom)

    def test_switching_actual_provider_chooses_its_own_default_command(self):
        self.enable(provider="codex", model_mode="fixed", model="first-model", command=["custom-codex"])
        policy = self.r.admin_configure(self.project, True, provider="antigravity", model_mode="default")
        self.assertEqual(policy["command"], ["agy"])

    def test_legacy_config_command_change_invalidates_existing_snapshot(self):
        self.r.config["admin"]["enabled"] = True
        snapshot = self.r.resolve_admin_executor(self.project)
        self.r.config["admin"]["command"] = ["new-codex"]
        with self.assertRaisesRegex(ContractError, "executor_changed"):
            self.r.assert_admin_executor_snapshot(self.project, snapshot)

    def test_effective_config_keeps_budget_readable_when_current_route_is_ambiguous(self):
        self.enable(model_mode="current")
        self.r.admin_observe_model(self.project, "a", "a-model")
        self.r.admin_observe_model(self.project, "b", "b-model")
        cfg = self.r.admin_effective_config(self.project)
        self.assertTrue(cfg["enabled"])
        self.assertFalse(cfg["routing_ready"])
        self.assertIsNone(cfg["executor"])
        self.assertEqual(cfg["max_calls_per_day"], 5)
        self.assertEqual(cfg["routing_reason"], "administrator_current_model_ambiguous")
        self.assertTrue(self.r.admin_effective_config(self.project, "a")["routing_ready"])

    def test_readonly_status_cannot_open_policy_or_model_override(self):
        status = self.r.admin_routing_status(self.project)
        self.assertTrue(status["routing_ready"])
        self.assertFalse(status["policy"]["enabled"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_host_policies").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_model_observations").fetchone()[0], 0)

    def test_model_identifiers_reject_options_whitespace_control_and_oversize(self):
        for value in ("-m", "--model=x", "model name", "model\nsecret", "", "a" * 201, [], None):
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_model_identifier(value)
        self.assertEqual(validate_model_identifier("provider/model:alias.v2"), "provider/model:alias.v2")
        self.assertEqual(validate_model_identifier("", allow_empty=True), "")

    def test_credential_shaped_values_cannot_be_persisted_as_model_identifiers(self):
        for value in ("sk-proj-" + "x" * 25, "github_pat_" + "x" * 25, "ghp_" + "x" * 30, "AIza" + "x" * 30):
            with self.subTest(value=value), self.assertRaises(ContractError):
                self.r.admin_observe_model(self.project, "conversation", value)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_model_observations").fetchone()[0], 0)

    def test_command_cannot_smuggle_credentials_or_model_overrides(self):
        for value in (["codex", "--api-key", "private"], ["codex", "Bearer private"], ["codex", "--model=x"],
                      ["codex", "-m", "other-model"], ["--model"], ["codex", "\x00"], [], "codex"):
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_executor_command(value)
        self.assertEqual(validate_executor_command(["C:/Program Files/Codex/codex.exe"]), ["C:/Program Files/Codex/codex.exe"])

    def test_config_requires_explicit_fixed_mode_for_nonempty_model(self):
        for config in ({"model": "override"}, {"model_mode": "fixed"}, {"provider": "remote"}, {"enabled": "yes"}):
            with self.subTest(config=config), self.assertRaises(ContractError):
                validate_admin_routing_config(config)
        cfg = validate_admin_routing_config({"enabled": False, "max_calls_per_day": 5})
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["provider"], "host")
        self.assertEqual(cfg["model_mode"], "default")

    def test_execution_plan_requires_claim_transaction_and_is_immutable_idempotent(self):
        self.enable()
        executor = self.r.resolve_admin_executor(self.project)
        with self.assertRaisesRegex(ContractError, "write transaction"):
            self.r.record_admin_execution(self.project, "call-1", executor)
        with self.r.transaction():
            first = self.r.record_admin_execution(self.project, "call-1", executor)
            replay = self.r.record_admin_execution(self.project, "call-1", executor)
        self.assertTrue(first["changed"])
        self.assertFalse(replay["changed"])
        self.assertEqual(first["created"], replay["created"])
        self.r.admin_configure(self.project, True, model_mode="fixed", model="another-model")
        new_executor = self.r.resolve_admin_executor(self.project)
        with self.r.transaction(), self.assertRaisesRegex(ContractError, "immutable execution plan"):
            self.r.record_admin_execution(self.project, "call-1", new_executor)
        plan = self.r.admin_execution_plan(self.project, "call-1")
        self.assertEqual(plan["executor"]["model"], "")

    def test_disabled_history_review_plan_needs_explicit_allowance_without_enabling_policy(self):
        executor = self.r.resolve_admin_executor(self.project, require_enabled=False)
        with self.r.transaction(), self.assertRaisesRegex(ContractError, "administrator_disabled"):
            self.r.record_admin_execution(self.project, "history-call", executor)
        with self.r.transaction():
            self.r.record_admin_execution(self.project, "history-call", executor, require_enabled=False)
        self.assertFalse(self.r.admin_policy(self.project)["enabled"])

    def test_execution_plan_read_is_project_scoped_and_status_is_bounded(self):
        self.enable(model_mode="fixed", model="known-model")
        executor = self.r.resolve_admin_executor(self.project)
        with self.r.transaction():
            for index in range(10):
                self.r.record_admin_execution(self.project, f"call-{index}", executor)
        with self.assertRaisesRegex(ContractError, "absent"):
            self.r.admin_execution_plan("other-project", "call-0")
        calls = self.r.admin_routing_status(self.project)["recent_calls"]
        self.assertEqual(len(calls), 8)
        self.assertTrue(all("command" not in call for call in calls))
        self.assertEqual(calls[0]["model"], "known-model")


if __name__ == "__main__":
    unittest.main()
