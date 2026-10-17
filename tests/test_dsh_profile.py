"""Profile identities use trusted host configuration, never project/tool evidence."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.dsh import DshAdapter
from agent_learning.dsh_profile import ENVIRONMENT, assert_profile, resolve_profile
from agent_learning.runtime import ContractError
from tests.test_admin_routing import RoutingRuntime


class DshProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-dsh-profile-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = {"version": 1, "profile_directory": str(self.root / "profiles/web"), "entries": [
            {"name": "@deepseek-ai/dsh-llm-pi-ai", "config": {"providers": {"route": {"baseUrl": "https://web.invalid", "apiKey": "PRIVATE_FAKE_CONFIG"}}}}
        ]}
        self.file = self.root / "profile.json"
        self.file.write_text(json.dumps(self.profile), encoding="utf-8")

    def test_explicit_snapshot_identity_has_no_provider_configuration_and_detects_changes(self):
        identity = resolve_profile(str(self.file))
        self.assertEqual(assert_profile(identity), identity)
        self.assertNotIn("PRIVATE_FAKE_CONFIG", json.dumps(identity))
        self.assertNotIn("web.invalid", json.dumps(identity))
        self.file.write_text(json.dumps(self.profile) + " ", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "dsh_profile_changed"):
            assert_profile(identity)

    def test_missing_or_outside_host_snapshot_is_unavailable(self):
        with patch.dict(os.environ, {ENVIRONMENT: "", "DSH_HOME": str(self.root)}):
            with self.assertRaisesRegex(ContractError, "dsh_profile_unavailable"):
                resolve_profile()
        bad = {**self.profile, "profile_directory": str(self.root / "arbitrary-project")}
        with patch.dict(os.environ, {ENVIRONMENT: json.dumps(bad), "DSH_HOME": str(self.root)}):
            with self.assertRaisesRegex(ContractError, "dsh_profile_invalid"):
                resolve_profile()

    def test_explicit_relative_sdk_configuration_path_is_rejected(self):
        self.profile["entries"] = [{"name": "@deepseek-ai/dsh-settings-file", "config": {"path": "relative-settings.yaml"}}]
        self.file.write_text(json.dumps(self.profile), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "dsh_profile_invalid"):
            resolve_profile(str(self.file))

    def test_standard_sdk_executor_freezes_profile_and_detects_configuration_drift(self):
        runtime = RoutingRuntime(self.root / "routing.sqlite", host="dsh", config={"enabled": True, "dsh_profile": str(self.file)})
        self.addCleanup(runtime.db.close)
        with patch("agent_learning.admin_providers.dsh_sdk_launcher", return_value={"args": ["node", "runner"]}):
            snapshot = runtime.resolve_admin_executor("project")
            with runtime.transaction():
                runtime.record_admin_execution("project", "call-one", snapshot)
            stored = runtime.admin_execution_plan("project", "call-one")["executor"]
            self.assertEqual(stored["dsh_profile"], snapshot["dsh_profile"])
            self.assertNotIn("PRIVATE_FAKE_CONFIG", json.dumps(stored))
            self.file.write_text(json.dumps(self.profile) + " ", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "administrator_executor_changed"):
                runtime.assert_admin_executor_snapshot("project", snapshot)

    def test_explicit_custom_launcher_requires_no_profile(self):
        runtime = RoutingRuntime(self.root / "custom.sqlite", host="dsh", config={"enabled": True, "dsh_command": [sys.executable]})
        self.addCleanup(runtime.db.close)
        self.assertNotIn("dsh_profile", runtime.resolve_admin_executor("project"))

    def test_review_configuration_tracks_profile_content_when_snapshot_path_stays_same(self):
        from agent_learning.admin import _review_configuration
        first = resolve_profile(str(self.file))
        self.file.write_text(json.dumps(self.profile) + " ", encoding="utf-8")
        second = resolve_profile(str(self.file))
        base = {"provider": "dsh", "model": "route/model", "model_mode": "fixed", "command": ["dsh"]}
        self.assertNotEqual(_review_configuration({}, {**base, "dsh_profile": first}),
                            _review_configuration({}, {**base, "dsh_profile": second}))

    def test_non_dsh_reviewer_does_not_inherit_private_host_snapshot(self):
        from agent_learning.admin import admin_once
        from agent_learning.runtime import Runtime
        from tests.test_module6_queue import fake_reviewer
        workspace = self.root / "workspace"
        workspace.mkdir()
        runtime = Runtime(self.root / "state", config={"admin": {"enabled": True, "command": [sys.executable]}})
        self.addCleanup(runtime.close)
        project = runtime.project(workspace)
        problem = runtime.feedback(project, "conversation", "bounded private environment test")
        fake = fake_reviewer("skip")
        calls = []
        def start(args, **kwargs):
            self.assertNotIn(ENVIRONMENT, kwargs["env"])
            calls.append(True)
            return fake(args, **kwargs)
        with patch.dict(os.environ, {ENVIRONMENT: "PRIVATE_FAKE_CONFIG"}), patch("agent_learning.admin.subprocess.Popen", side_effect=start):
            result = admin_once(runtime, project, problem["id"])
        self.assertEqual(result["status"], "skip")
        self.assertEqual(len(calls), 1)

    def test_capability_help_never_inherits_private_host_snapshot(self):
        from agent_learning.admin_providers import executor_capability
        for provider, name, flags in (
            ("codex", "codex", b"--ignore-user-config --ignore-rules --ephemeral --output-schema --output-last-message"),
            ("antigravity", "agy", b"--input-format --output-format --json-schema --agent --print-timeout"),
        ):
            with self.subTest(provider=provider):
                def probe(_args, **kwargs):
                    self.assertNotIn(ENVIRONMENT, kwargs["env"])
                    kwargs["stdout"].write(flags)
                    return subprocess.CompletedProcess(_args, 0)
                plan = {"provider": provider, "command": [name], "model": "isolated-profile-test-" + provider}
                with patch.dict(os.environ, {ENVIRONMENT: "PRIVATE_FAKE_CONFIG"}), \
                        patch("agent_learning.admin_providers.resolve_command", return_value={"args": [name], "launch_kind": "executable"}), \
                        patch("agent_learning.admin_providers.subprocess.run", side_effect=probe) as process:
                    self.assertTrue(executor_capability(plan)["ready"])
                process.assert_called_once()

    def test_background_job_signal_timeout_or_nested_exit_is_terminal(self):
        adapter = object.__new__(DshAdapter)
        for result in ({"jobId": "job-one", "exitCode": None, "signal": "SIGTERM"},
                       {"session_id": "job-one", "timedOut": True},
                       {"value": {"jobId": "job-one", "exitCode": 2}},
                       {"jobId": "job-one", "content": [{"type": "text", "text": "[exit code: 2]"}]}):
            with self.subTest(result=result):
                normalized = adapter._normalize_tool_response("pwsh", {"command": "demo"}, {"result": result})
                self.assertTrue(normalized["isError"])
                self.assertNotEqual(normalized.get("status"), "running")
        normalized = adapter._normalize_tool_response("pwsh", {}, {"result": {"jobId": "active", "exitCode": None}})
        self.assertEqual(normalized["status"], "running")

    @unittest.skipUnless(shutil.which("node"), "Node is required for native loader snapshot checks")
    def test_native_snapshot_uses_effective_loader_not_tool_paths_or_global_environment(self):
        bridge = Path(__file__).resolve().parents[1] / "host_resources/dsh/host/index.mjs"
        script = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
const {reviewProfileEnvironment} = await import(pathToFileURL(process.argv[1]).href);
const profile = JSON.parse(process.argv[2]);
const ctx = {baseUrl:pathToFileURL(profile.profile_directory+'/').href,loader:{*entries() {
 for (const entry of profile.entries) yield {options:{name:entry.name,config:{wrong:true}},fiber:{config:entry.config},disabled:false};
 yield {options:{name:'project-plugin'},fiber:{config:{private:'must not capture'}}};
}}};
const before=process.env.ALP_DSH_REVIEW_PROFILE;
const env=reviewProfileEnvironment(ctx);
assert.deepEqual(JSON.parse(env.ALP_DSH_REVIEW_PROFILE).entries,profile.entries);
assert.equal(process.env.ALP_DSH_REVIEW_PROFILE,before);
ctx.loader={*entries() {yield {options:{name:'@deepseek-ai/dsh-settings-file'},fiber:{config:{path:'relative-settings.yaml',dshHome:'relative-host-home'}}};}};
const resolved=JSON.parse(reviewProfileEnvironment(ctx).ALP_DSH_REVIEW_PROFILE).entries[0].config;
assert.equal(resolved.path,resolve('relative-settings.yaml'));
assert.ok(resolved.dshHome.endsWith('relative-host-home'));
ctx.baseUrl=pathToFileURL(process.cwd()+'/').href;
assert.equal(reviewProfileEnvironment(ctx).ALP_DSH_REVIEW_PROFILE,'');
process.stdout.write('ok');
"""
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script, str(bridge), json.dumps(self.profile)],
            cwd=self.root, env={**os.environ, "DSH_HOME": str(self.root), ENVIRONMENT: "stale-other-profile"}, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(result.stdout, b"ok")
