"""Project opt-in precedes automatic Codex collection and context generation."""
from __future__ import annotations

import io
import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.install import install
from agent_learning.project_admission import require_project_installation, same_workspace
from agent_learning.runtime import ContractError


ROOT = Path(__file__).resolve().parents[1]


class ProjectAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="alp-project-admission-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.target = self.workspace / ".agents/plugins/agent-learning"
        self.store = self.root / "releases"

    def install(self, *, bound=True, target=None):
        return install(target or self.target, runtime_store=self.store,
                       panel_workspace=self.workspace if bound else None)

    def test_uninstalled_project_has_no_admission_and_opens_no_runtime(self):
        with patch("agent_learning.runtime.Runtime", side_effect=AssertionError("no database")):
            with self.assertRaisesRegex(ContractError, "project_not_installed"):
                require_project_installation(self.workspace)
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_installed_project_subdirectory_uses_canonical_project_identity(self):
        result = self.install()
        child = self.workspace / "src"
        child.mkdir()
        with patch("agent_learning.install_update.inspect_installation", side_effect=AssertionError("no full scan")):
            binding = require_project_installation(child, runtime_directory=result["runtime_directory"])
        self.assertEqual(binding["workspace"], str(self.workspace))
        self.assertEqual(binding["installation"], str(self.target))

    def test_legacy_unbound_local_export_is_project_opt_in(self):
        self.install(bound=False)
        self.assertEqual(require_project_installation(self.workspace, self.target)["workspace"], str(self.workspace))

    def test_neighbor_and_nested_checkout_do_not_inherit_parent_admission(self):
        self.install()
        neighbor = self.root / "project-other"
        nested = self.workspace / "nested"
        neighbor.mkdir()
        nested.mkdir()
        (nested / ".git").mkdir()
        for path in (neighbor, nested):
            with self.subTest(path=path), self.assertRaisesRegex(ContractError, "project_not_installed"):
                require_project_installation(path)
            self.assertFalse(same_workspace(path, self.workspace))

    def test_build_local_artifacts_do_not_inherit_repository_installation(self):
        self.install()
        artifact = self.workspace / "build/local/uninstalled project"
        artifact.mkdir(parents=True)
        with self.assertRaisesRegex(ContractError, "project_not_installed"):
            require_project_installation(artifact)

    def test_unrelated_explicit_installation_cannot_authorize_another_workspace(self):
        self.install()
        other = self.root / "other"
        other.mkdir()
        with self.assertRaisesRegex(ContractError, "project_installation_invalid"):
            require_project_installation(other, self.target)

    def test_changed_receipt_and_runtime_mismatch_reject_before_runtime(self):
        result = self.install()
        with self.assertRaisesRegex(ContractError, "project_installation_invalid"):
            require_project_installation(self.workspace, runtime_directory=self.root / "other runtime")
        receipt = self.target / "runtime-release.json"
        value = json.loads(receipt.read_text(encoding="utf-8"))
        value["release_id"] = "changed"
        receipt.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "project_installation_invalid"):
            require_project_installation(self.workspace, runtime_directory=result["runtime_directory"])

    def test_removed_receipt_is_invalid_for_an_existing_installation(self):
        self.install()
        (self.target / "runtime-release.json").unlink()
        with self.assertRaisesRegex(ContractError, "project_installation_invalid"):
            require_project_installation(self.workspace)

    def test_other_host_installation_does_not_enable_codex_hooks(self):
        install(self.target, runtime_store=self.store, host="antigravity",
                workspace=self.workspace, home=self.root / "home")
        with self.assertRaisesRegex(ContractError, "project_not_installed"):
            require_project_installation(self.workspace)
        with self.assertRaisesRegex(ContractError, "project_installation_invalid"):
            require_project_installation(self.workspace, self.target)

    def configure_external(self, target):
        config = self.workspace / ".codex/config.toml"
        config.parent.mkdir(exist_ok=True)
        config.write_text("[mcp_servers.agent_learning]\nargs = " +
            json.dumps(["learning.py", "serve", "--panel-installation", str(target)]) + "\n", encoding="utf-8")

    def test_external_bound_installation_is_discovered_from_project_wiring(self):
        target = self.root / "external plugin"
        self.install(target=target)
        self.configure_external(target)
        binding = require_project_installation(self.workspace)
        self.assertEqual(binding["installation"], str(target))

    def test_external_unbound_installation_does_not_infer_project_authority(self):
        target = self.root / "external plugin"
        self.install(target=target, bound=False)
        self.configure_external(target)
        with self.assertRaisesRegex(ContractError, "project_installation_invalid"):
            require_project_installation(self.workspace)

    def test_project_wiring_accepts_matching_worker_and_panel_bindings(self):
        target = self.root / "external plugin"
        self.install(target=target)
        config = self.workspace / ".codex/config.toml"
        config.parent.mkdir()
        args = ["learning.py", "serve", "--plugin-installation", str(target), "--panel-installation", str(target)]
        config.write_text("[mcp_servers.agent_learning]\nargs = " + json.dumps(args) + "\n", encoding="utf-8")
        self.assertEqual(require_project_installation(self.workspace)["installation"], str(target))
        args[-1] = str(self.root / "other target")
        config.write_text("[mcp_servers.agent_learning]\nargs = " + json.dumps(args) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "project_not_installed"):
            require_project_installation(self.workspace)

    def test_unrelated_unparseable_toml_does_not_hide_local_installation(self):
        self.install()
        config = self.workspace / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text('other = "unfinished\n', encoding="utf-8")
        self.assertEqual(require_project_installation(self.workspace)["installation"], str(self.target))

    def test_uninstalled_source_hook_skips_before_config_release_or_runtime(self):
        spec = importlib.util.spec_from_file_location("admission_dispatch", ROOT / "hooks/dispatch.py")
        dispatch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dispatch)
        event = {"hook_event_name": "UserPromptSubmit", "session_id": "uninstalled", "cwd": str(self.workspace)}
        output = io.StringIO()
        with patch.dict(os.environ, {"ALP_HOME": str(self.root / "must not exist")}, clear=True), \
                patch.object(dispatch.sys, "stdin", type("Input", (), {"buffer": io.BytesIO(json.dumps(event).encode())})()), \
                patch.object(dispatch.sys, "stdout", output), \
                patch.object(dispatch, "_mode", side_effect=AssertionError("must not read config")), \
                patch("agent_learning.release.validate_entry", side_effect=AssertionError("must not scan release")), \
                patch("agent_learning.runtime.Runtime", side_effect=AssertionError("must not open database")):
            self.assertEqual(dispatch.main(["--event", "UserPromptSubmit"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {})
        self.assertFalse((self.root / "must not exist").exists())

    def test_fixed_hook_skips_uninstalled_workspace_without_creating_home(self):
        result = self.install()
        other = self.root / "uninstalled"
        other.mkdir()
        home = self.root / "unused home"
        environment = {**os.environ, "ALP_HOME": str(home), "ALP_CONFIG": str(home / "config.json"),
                       "ALP_HOOK_MODE": "enforce", "PYTHONDONTWRITEBYTECODE": "1"}
        environment.pop("ALP_DISABLED", None)
        event = {"hook_event_name": "PreToolUse", "session_id": "uninstalled", "cwd": str(other),
                 "tool_name": "read", "tool_use_id": "call", "tool_input": {"path": "README.md"}}
        completed = subprocess.run([sys.executable, "-B", str(Path(result["runtime_directory"]) / "hooks/dispatch.py"),
            "--event", "PreToolUse", "--hook-mode", "enforce"], input=json.dumps(event).encode(),
            cwd=other, env=environment, capture_output=True, timeout=15, check=False)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(json.loads(completed.stdout), {})
        self.assertFalse(home.exists())

    def test_first_hook_in_non_git_subdirectory_belongs_to_installed_project(self):
        result = self.install()
        child = self.workspace / "src"
        child.mkdir()
        home = self.root / "fresh home"
        environment = {**os.environ, "ALP_HOME": str(home), "ALP_CONFIG": str(home / "config.json"),
                       "ALP_HOOK_MODE": "observe", "PYTHONDONTWRITEBYTECODE": "1"}
        environment.pop("ALP_DISABLED", None)
        event = {"hook_event_name": "UserPromptSubmit", "session_id": "first-subdir", "cwd": str(child),
                 "prompt": "Read a project file."}
        completed = subprocess.run([sys.executable, "-B", str(Path(result["runtime_directory"]) / "hooks/dispatch.py"),
            "--event", "UserPromptSubmit", "--hook-mode", "observe"], input=json.dumps(event).encode(),
            cwd=child, env=environment, capture_output=True, timeout=15, check=False)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        self.assertNotIn("systemMessage", json.loads(completed.stdout))
        with closing(sqlite3.connect((home / "runtime.sqlite3").as_uri() + "?mode=ro", uri=True)) as database:
            self.assertEqual(database.execute("SELECT count(*) FROM projects").fetchone()[0], 1)
            paths = database.execute("SELECT path,project FROM project_paths").fetchall()
        self.assertIn(os.path.normcase(str(self.workspace)), {row[0] for row in paths})
        self.assertEqual(len({row[1] for row in paths}), 1)


if __name__ == "__main__":
    unittest.main()
