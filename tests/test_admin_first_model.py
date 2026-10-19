"""First root model bindings are local, epoch-scoped and never invoke models."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_learning.admin_providers import build_execution
from agent_learning.admin_queue import claim_dispatch, scan_signature
from agent_learning.admin_routing import install_admin_routing, validate_admin_routing_config
from agent_learning.runtime import ContractError, Runtime
from tests.test_admin_routing import RoutingRuntime


class AdminFirstModelTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "build" / "local" / "admin-first-model-tests"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="run-", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "routing.sqlite"
        self.r = self.routing()
        self.p = "project-one"

    def routing(self, host="codex", instance="default"):
        runtime = RoutingRuntime(self.database, host, instance)
        self.addCleanup(runtime.db.close)
        return runtime

    def enable(self, runtime=None, project=None):
        runtime = runtime or self.r
        return runtime.admin_configure(project or self.p, True, model_mode="first")

    def test_first_valid_root_is_bound_even_when_administrator_is_disabled(self):
        self.assertFalse(self.r.admin_policy(self.p)["enabled"])
        observation = self.r.admin_observe_model(self.p, "initial-chat", "first-model")
        self.assertTrue(observation["first_recorded"])
        binding = self.r.admin_first_model_binding(self.p)
        self.assertEqual(binding["conversation"], "initial-chat")
        self.assertEqual(binding["model"], "first-model")
        self.assertFalse(self.r.admin_policy(self.p)["enabled"])
        self.enable()
        executor = self.r.resolve_admin_executor(self.p, "another-chat")
        self.assertEqual(executor["model"], "first-model")
        self.assertEqual(executor["model_source"], "first_host_observation")
        self.assertEqual(executor["conversation"], "another-chat")

    def test_root_switch_new_chat_and_session_end_do_not_change_binding(self):
        self.r.admin_observe_model(self.p, "initial-chat", "first-model")
        self.enable()
        snapshot = self.r.resolve_admin_executor(self.p, "case-chat")
        binding = self.r.admin_first_model_binding(self.p)
        self.r.admin_observe_model(self.p, "initial-chat", "later-model")
        self.r.admin_observe_model(self.p, "second-chat", "other-model")
        self.r.admin_observe_model(self.p, "initial-chat", active=False)
        self.r.admin_observe_model(self.p, "second-chat", active=False)
        self.assertEqual(binding, self.r.admin_first_model_binding(self.p))
        self.assertEqual(self.r.resolve_admin_executor(self.p, "case-chat"), snapshot)
        self.r.assert_admin_executor_snapshot(self.p, snapshot)

    def test_binding_persists_across_reopen_and_disable_enable(self):
        self.r.admin_observe_model(self.p, "initial-chat", "first-model")
        binding = self.r.admin_first_model_binding(self.p)
        self.enable()
        self.r.admin_configure(self.p, False, model_mode="first")
        other = self.routing()
        self.assertEqual(binding, other.admin_first_model_binding(self.p))
        self.enable(other)
        self.assertEqual(other.resolve_admin_executor(self.p)["model"], "first-model")

    def test_invalid_missing_inactive_and_child_models_never_bind(self):
        self.assertFalse(self.r.admin_observe_model(self.p, "chat")["recorded"])
        self.assertFalse(self.r.admin_observe_model(self.p, "chat", "child-model", agent_id="child")["recorded"])
        self.r.admin_observe_model(self.p, "chat", "inactive-model", active=False)
        for model in ("", "--model=x", "model name", "ghp_" + "x" * 30):
            with self.subTest(model=model), self.assertRaises(ContractError):
                self.r.admin_observe_model(self.p, "chat", model)
        self.assertIsNone(self.r.admin_first_model_binding(self.p))

    def test_binding_never_crosses_project_host_instance_or_account(self):
        self.r.admin_observe_model(self.p, "chat", "codex-model")
        for runtime, project in ((self.r, "another-project"), (self.routing("antigravity"), self.p),
                                 (self.routing(instance="second"), self.p)):
            with self.subTest(scope=runtime.host_scope, project=project):
                self.enable(runtime, project)
                with self.assertRaisesRegex(ContractError, "first_model_unavailable"):
                    runtime.resolve_admin_executor(project)
        self.enable()
        old = self.r.resolve_admin_executor(self.p)
        self.r.changed_account()
        self.assertTrue(self.r.admin_policy(self.p)["enabled"])
        self.assertIsNone(self.r.admin_first_model_binding(self.p))
        with self.assertRaises(ContractError):
            self.r.resolve_admin_executor(self.p)
        with self.assertRaises(ContractError):
            self.r.assert_admin_executor_snapshot(self.p, old)
        with self.assertRaisesRegex(ContractError, "reconnect_required"):
            self.r.admin_observe_model(self.p, "old-chat", "old-model", expected_epoch=0)
        self.r.admin_observe_model(self.p, "new-chat", "new-account-model", expected_epoch=1)
        self.assertEqual(self.r.resolve_admin_executor(self.p)["model"], "new-account-model")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_first_models WHERE project=?", (self.p,)).fetchone()[0], 2)

    def test_old_mutable_observation_is_not_migrated_into_first_binding(self):
        self.r.admin_observe_model(self.p, "old-chat", "last-observed-model")
        self.r.db.execute("DROP TABLE admin_first_models")
        with self.r.transaction():
            install_admin_routing(self.r)
        self.assertIsNone(self.r.admin_first_model_binding(self.p))
        replay = self.r.admin_observe_model(self.p, "old-chat", "last-observed-model")
        self.assertFalse(replay["changed"])
        self.assertTrue(replay["first_recorded"])

    def test_two_runtime_observers_race_to_exactly_one_persistent_binding(self):
        gate, results, errors = threading.Barrier(2), [], []
        guard = threading.Lock()
        def observe(model):
            runtime = None
            try:
                runtime = RoutingRuntime(self.database)
                gate.wait(timeout=5)
                result = runtime.admin_observe_model(self.p, model + "-chat", model)
                with guard:
                    results.append(result)
            except BaseException as exc:
                with guard:
                    errors.append(exc)
            finally:
                if runtime is not None:
                    runtime.db.close()
        threads = [threading.Thread(target=observe, args=(model,)) for model in ("model-a", "model-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [], repr(errors))
        self.assertEqual(len(results), 2)
        winners = [result for result in results if result["first_recorded"]]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.r.admin_first_model_binding(self.p)["model"], winners[0]["model"])

    def test_other_provider_cannot_receive_the_hosts_first_model(self):
        self.r.admin_observe_model(self.p, "chat", "codex-model")
        with self.assertRaisesRegex(ContractError, "provider_mismatch"):
            self.r.admin_configure(self.p, True, provider="antigravity", model_mode="first")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_host_policies").fetchone()[0], 0)
        self.r.admin_configure(self.p, True, provider="codex", model_mode="first")
        self.assertEqual(self.r.resolve_admin_executor(self.p)["provider"], "codex")

    def test_first_status_is_readonly_and_waits_without_binding(self):
        self.enable()
        before = self.r.db.total_changes
        with patch("agent_learning.admin.subprocess.Popen") as model:
            status = self.r.admin_routing_status(self.p)
        model.assert_not_called()
        self.assertFalse(status["routing_ready"])
        self.assertEqual(status["reason"], "administrator_first_model_unavailable")
        self.assertIsNone(status["first_binding"])
        self.assertEqual(status["observed_models"], [])
        self.assertEqual(before, self.r.db.total_changes)

    def test_status_model_options_are_bounded_valid_and_account_scoped(self):
        for number in range(35):
            self.r.admin_observe_model(self.p, f"chat-{number}", f"model-{number:02}")
        self.r.admin_observe_model("other-project", "other-chat", "foreign-project-model")
        self.r.db.execute("UPDATE admin_model_observations SET model=? WHERE conversation='chat-34'", ("ghp_" + "x" * 30,))
        status = self.r.admin_routing_status(self.p)
        options = status["observed_models"]
        self.assertLessEqual(len(options), 30)
        self.assertTrue(all(option["source"] == "host_observation" for option in options))
        self.assertNotIn("ghp_", json.dumps(status))
        self.assertNotIn("foreign-project-model", json.dumps(options))
        self.assertEqual(status["first_binding"]["model"], "model-00")
        self.r.changed_account()
        after = self.r.admin_routing_status(self.p)
        self.assertEqual(after["observed_models"], [])
        self.assertIsNone(after["first_binding"])

    def test_policy_mode_change_invalidates_frozen_first_execution(self):
        self.r.admin_observe_model(self.p, "chat", "first-model")
        self.enable()
        snapshot = self.r.resolve_admin_executor(self.p)
        with self.r.transaction():
            self.r.record_admin_execution(self.p, "first-call", snapshot)
        version = self.r.admin_policy(self.p)["policy_version"]
        self.r.admin_configure(self.p, True, model_mode="fixed", model="operator-model", expected_policy_version=version)
        with self.assertRaises(ContractError):
            self.r.assert_admin_executor_snapshot(self.p, snapshot)
        self.assertEqual(self.r.admin_execution_plan(self.p, "first-call")["executor"]["model"], "first-model")

    def test_first_selection_builds_an_explicit_model_on_all_hosts(self):
        directory = Path(self.temp.name) / "execution"
        directory.mkdir()
        with patch("agent_learning.admin.subprocess.Popen") as process:
            # Standard DSH SDK discovery has separate launcher regressions;
            # this protocol check uses an explicit launcher without a host install.
            for provider, command in (("codex", ["codex"]), ("antigravity", ["agy"]), ("dsh", [sys.executable])):
                plan = build_execution(command, provider, "bound-model", directory,
                    directory / "schema.json", directory / "output.json", "Review supplied observations", 10,
                    selection_mode="first")
                self.assertEqual(plan["selection_mode"], "first")
                self.assertEqual(plan["args"][plan["args"].index("--model") + 1], "bound-model")
        process.assert_not_called()
        with self.assertRaises(ContractError):
            build_execution(["codex"], "codex", "", directory, directory / "schema", directory / "output", "Review", 10,
                            selection_mode="first")

    def test_legacy_default_config_remains_compatible(self):
        self.assertEqual(validate_admin_routing_config({})["model_mode"], "default")
        self.assertEqual(validate_admin_routing_config({"model_mode": "first"})["model_mode"], "first")


class FirstModelQueueTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "build" / "local" / "admin-first-model-tests"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="queue-", dir=root)
        self.addCleanup(self.temp.cleanup)
        workspace = Path(self.temp.name) / "project"
        workspace.mkdir()
        self.r = Runtime(Path(self.temp.name) / "home", config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(workspace)
        self.r.admin_configure(self.p, True, model_mode="first", command=[sys.executable])

    def test_first_scan_signature_ignores_other_root_model_changes(self):
        initial = scan_signature(self.r, self.p)
        self.r.admin_observe_model(self.p, "first-chat", "first-model")
        bound = scan_signature(self.r, self.p)
        self.assertNotEqual(initial, bound)
        self.r.admin_observe_model(self.p, "first-chat", "later-model")
        self.r.admin_observe_model(self.p, "another-chat", "different-model")
        self.r.admin_observe_model(self.p, "first-chat", active=False)
        self.assertEqual(bound, scan_signature(self.r, self.p))

    def test_first_dispatch_handles_other_conversations_without_their_own_model(self):
        self.r.admin_observe_model(self.p, "first-chat", "first-model")
        self.r.task_update(self.p, "case-chat", "Review the supplied observation")
        self.r.feedback(self.p, "case-chat", "An observable operation failed", category="operation")
        with patch("agent_learning.admin_queue.time.time", return_value=100000):
            token = claim_dispatch(self.r, self.p)
        self.assertIsNotNone(token)
        dispatch = self.r.db.execute("SELECT lease_until FROM admin_dispatch WHERE token=?", (token,)).fetchone()
        self.assertEqual(dispatch[0], 100000 + 30, "Python reservation waits only for worker acknowledgement")

    def test_first_dispatch_waits_for_observation_without_spending_model_calls(self):
        self.r.task_update(self.p, "case-chat", "Review the supplied observation")
        self.r.feedback(self.p, "case-chat", "An observable operation failed", category="operation")
        self.assertIsNone(claim_dispatch(self.r, self.p))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_schema15_upgrade_preserves_knowledge_routing_and_unknown_calls_without_inventing_first(self):
        self.r.task_update(self.p, "legacy-chat", "Preserve the existing project knowledge")
        problem = self.r.feedback(self.p, "legacy-chat", "A documented legacy observation", category="project")
        refs = [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="Legacy project fact", content="The legacy project used local observations.", category="project",
            knowledge_kind="fact", applicability="Archived project conditions", limitations="Reported evidence only",
            evidence_refs=refs, validation_level="reported")
        published = self.r.publish(self.p, candidate["candidate_id"])
        policy = self.r.admin_configure(self.p, True, model_mode="fixed", model="legacy-fixed-model", command=[sys.executable])
        self.r.admin_observe_model(self.p, "legacy-chat", "latest-mutable-observation")
        uncertain = self.r.feedback(self.p, "legacy-chat", "A separate uncertain legacy attempt", category="operation")
        self.r.db.execute("INSERT INTO admin_calls(id,project,problem,revision,day,state,created,finished,retry_allowed,host_epoch) "
                          "VALUES(?,?,?,?,?,'timeout_unknown',1,2,0,?)",
                          ("legacy-unknown-call", self.p, uncertain["id"], uncertain["revision"], "2026-01-01", self.r.host_epoch()))
        before = dict(self.r.db.execute("SELECT * FROM knowledge WHERE project=? AND id=?", (self.p, published["knowledge_id"])).fetchone())
        for table in ("admin_first_models", "admin_control_state", "admin_control_receipts", "admin_runtime_settings"):
            self.r.db.execute("DROP TABLE " + table)
        self.r.db.execute("PRAGMA user_version=15")
        home = self.r.home
        self.r.close()
        upgraded = Runtime(home, config={})
        self.addCleanup(upgraded.close)
        self.assertEqual(upgraded.db.execute("PRAGMA user_version").fetchone()[0], 24)
        after = dict(upgraded.db.execute("SELECT * FROM knowledge WHERE project=? AND id=?", (self.p, published["knowledge_id"])).fetchone())
        self.assertEqual(before, after)
        self.assertEqual(upgraded.db.execute("SELECT revision FROM active WHERE project=? AND id=?", (self.p, published["knowledge_id"])).fetchone()[0], published["revision"])
        current_policy = upgraded.admin_policy(self.p)
        self.assertEqual(current_policy["policy_version"], policy["policy_version"])
        self.assertEqual(current_policy["model_mode"], "fixed")
        self.assertEqual(current_policy["model"], "legacy-fixed-model")
        self.assertTrue(current_policy["enabled"])
        unknown = upgraded.db.execute("SELECT state,retry_allowed FROM admin_calls WHERE id='legacy-unknown-call'").fetchone()
        self.assertEqual(tuple(unknown), ("timeout_unknown", 0))
        self.assertIsNone(upgraded.admin_first_model_binding(self.p))
        self.assertEqual(upgraded.db.execute("SELECT model FROM admin_model_observations WHERE project=?", (self.p,)).fetchone()[0], "latest-mutable-observation")
        for table in ("admin_first_models", "admin_control_state", "admin_control_receipts", "admin_runtime_settings"):
            self.assertIsNotNone(upgraded.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


if __name__ == "__main__":
    unittest.main()
