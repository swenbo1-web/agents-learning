"""Explicit Codex project Actions, with no live host or knowledge database."""
import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.codex_actions import ACTION_FILE, MANIFEST_FILE, build_action_command, install_action
from agent_learning.install import _hook_commands, install
from agent_learning.install_update import inspect_installation, snapshot
from agent_learning.runtime import ContractError


class CodexActionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ca-")
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self._cleanup)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.target = self.root / "core"
        self.home = self.root / "knowledge"
        self.config = self.root / "chosen config.json"
        self.environments = self.workspace / ".codex/environments"
        self.action = self.environments / ACTION_FILE
        self.manifest = self.environments / MANIFEST_FILE
        self.store = self.root / "releases"

    def _cleanup(self):
        from agent_learning.release import filesystem_path
        if self.root.exists() and self.root.name.startswith("ca-"):
            shutil.rmtree(filesystem_path(self.root), ignore_errors=True)
        self.temp.cleanup()

    def initial(self, *, bound=True, **options):
        return install(self.target, runtime_store=self.store,
                       panel_workspace=self.workspace if bound else None, **options)

    def register(self, **options):
        return install_action(self.target, self.workspace, home=self.home, config=self.config, **options)

    def test_registers_separate_environment_preserving_existing_comments_setup_and_actions(self):
        core = self.initial()
        self.environments.mkdir(parents=True)
        main = self.environments / "environment.toml"
        original = b'# user comment\nversion = 1\nname = "My environment"\n[setup]\nscript = "build"\n[[actions]]\nname = "Run"\ncommand = "npm run dev"\n'
        main.write_bytes(original)
        before = snapshot(self.target)
        with patch("agent_learning.cli.Runtime", side_effect=AssertionError("No database")):
            result = self.register()
        self.assertEqual(main.read_bytes(), original)
        self.assertEqual(snapshot(self.target), before)
        self.assertFalse(self.home.exists())
        self.assertFalse(self.config.exists())
        self.assertEqual(result["status"], "installed")
        self.assertFalse(result["selected_automatically"])
        self.assertFalse(result["administrator_started"])
        self.assertEqual(result["select_environment"], "Agent Learning")
        body = self.action.read_text(encoding="utf-8")
        self.assertIn('[[actions]]\nname = "Agent Learning"\nicon = "debug"', body)
        self.assertIn('[setup]\nscript = ""', body)
        metadata = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(metadata["home"], str(self.home))
        self.assertEqual(metadata["config"], str(self.config))
        self.assertEqual(metadata["runtime_directory"], core["runtime_directory"])
        self.assertEqual(metadata["workspace"], str(self.workspace))
        try:
            import tomllib
        except ImportError:
            tomllib = None
        if tomllib:
            document = tomllib.loads(body)
            self.assertEqual(document["actions"], [{"name": "Agent Learning", "icon": "debug",
                              "command": result["manual_command"], "platform": sys.platform}])

    def test_same_registration_is_idempotent_and_does_not_rewrite_files(self):
        self.initial()
        self.register()
        before = (self.action.read_bytes(), self.manifest.read_bytes(), self.action.stat().st_mtime_ns,
                  self.manifest.stat().st_mtime_ns)
        with patch("agent_learning.codex_actions._write_atomic", side_effect=AssertionError("No rewrite")):
            result = self.register()
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(before, (self.action.read_bytes(), self.manifest.read_bytes(), self.action.stat().st_mtime_ns,
                                 self.manifest.stat().st_mtime_ns))

    def test_interpreter_alias_is_preserved_while_workspace_links_stay_rejected(self):
        from agent_learning.install_update import _linked

        executable = self.root / "venv/bin/python"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"isolated interpreter placeholder; never executed")
        # Alias ownership only; the non-executable fixture is never probed.
        with patch("agent_learning.installation_health.select_python", return_value=(str(executable), {})):
            self.initial()

        # Simulate Unix Python/venv links even on Windows without link privileges.
        def linked(path):
            return path in {executable, executable.parent} or _linked(path)

        with patch("agent_learning.install_update._linked", side_effect=linked):
            result = self.register()
            self.assertEqual(self.register()["status"], "unchanged")
        metadata = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(metadata["python"], str(executable))
        self.assertIn(str(executable), build_action_command({"python": executable,
                      "runtime_directory": self.root}, self.target, home=self.home,
                      config=self.config, platform="linux"))
        self.assertEqual(result["status"], "installed")
        before = (self.action.read_bytes(), self.manifest.read_bytes())
        with patch("agent_learning.install_update._linked", side_effect=lambda path: path == self.workspace or _linked(path)):
            with self.assertRaises(ContractError):
                self.register()
        self.assertEqual((self.action.read_bytes(), self.manifest.read_bytes()), before)

    def test_interpreter_path_requires_absolute_available_file(self):
        from agent_learning.install_update import python_executable

        for value in (None, "python", str(self.root), str(self.root / "missing-python"),
                      str(self.root / ".." / "python"), str(self.root / "bad\0python"),
                      str(self.root / "bad\npython")):
            with self.subTest(value=value), self.assertRaises(ContractError):
                python_executable(value)
        missing = self.root / "removed-python"
        self.assertEqual(python_executable(str(missing), require_available=False), missing)

    def test_environment_data_bindings_are_frozen_and_can_be_explicitly_updated(self):
        self.initial()
        with patch.dict(os.environ, {"ALP_HOME": str(self.home), "ALP_CONFIG": str(self.config)}):
            first = install_action(self.target, self.workspace)
        self.assertEqual(first["home"], str(self.home))
        self.assertEqual(first["config"], str(self.config))
        other = self.root / "other home"
        second = install_action(self.target, self.workspace, home=other, config=other / "config.json")
        self.assertEqual(second["status"], "installed")
        self.assertEqual(second["home"], str(other))
        self.assertFalse(other.exists())

    def test_default_configuration_path_is_frozen_even_when_it_does_not_exist(self):
        self.initial()
        with patch.dict(os.environ, {"ALP_HOME": str(self.home)}, clear=False), \
                patch.dict(os.environ, {"ALP_CONFIG": ""}):
            result = install_action(self.target)
        self.assertEqual(result["config"], str(self.home / "config.json"))
        self.assertIn("ALP_CONFIG", build_action_command({"python": sys.executable,
                      "runtime_directory": self.root}, self.target, home=self.home,
                      config=self.home / "config.json", platform="linux"))
        self.assertFalse(self.home.exists())

    def test_wrong_host_unbound_or_different_project_is_rejected(self):
        self.initial(bound=False)
        with self.assertRaises(ContractError):
            self.register()
        install(self.target, runtime_store=self.store, upgrade=True, panel_workspace=self.workspace)
        other = self.root / "other-project"
        other.mkdir()
        with self.assertRaises(ContractError):
            install_action(self.target, other, home=self.home)
        wrong = self.root / "ag-core"
        install(wrong, runtime_store=self.store, host="antigravity", workspace=self.workspace, home=self.home)
        with self.assertRaises(ContractError):
            install_action(wrong, self.workspace, home=self.home)
        self.assertFalse(self.action.exists())

    def test_old_runtime_without_console_requires_upgrade_before_registration(self):
        self.initial()
        observed = inspect_installation(self.target, _hook_commands)
        observed["release"]["files"].pop("agent_learning/codex_panel.py")
        with patch("agent_learning.codex_actions.inspect_installation", return_value=observed), \
                self.assertRaisesRegex(ContractError, "upgrade"):
            self.register()
        self.assertFalse(self.action.exists())

    def test_existing_unmanaged_environment_is_never_overwritten(self):
        self.initial()
        self.environments.mkdir(parents=True)
        self.action.write_bytes(b"# user's chosen filename\n")
        before = self.action.read_bytes()
        with self.assertRaises(ContractError):
            self.register()
        self.assertEqual(self.action.read_bytes(), before)
        self.assertFalse(self.manifest.exists())

    def test_changed_managed_action_and_metadata_are_preserved(self):
        self.initial()
        self.register()
        original_action, original_manifest = self.action.read_bytes(), self.manifest.read_bytes()
        for path, altered in ((self.action, original_action + b"# personal edit\n"),
                              (self.manifest, b'{"schema_version":1}')):
            self.action.write_bytes(original_action)
            self.manifest.write_bytes(original_manifest)
            path.write_bytes(altered)
            for remove in (False, True):
                with self.subTest(path=path, remove=remove), self.assertRaises(ContractError):
                    self.register(remove=remove)
                self.assertEqual(path.read_bytes(), altered)

    def test_failed_metadata_update_restores_both_owned_files(self):
        self.initial()
        self.register()
        original = (self.action.read_bytes(), self.manifest.read_bytes())
        from agent_learning.codex_actions import _write_atomic
        failed = False

        def write_or_fail(path, raw):
            nonlocal failed
            if path == self.manifest and not failed:
                failed = True
                raise OSError("isolated metadata failure")
            return _write_atomic(path, raw)

        with patch("agent_learning.codex_actions._write_atomic", side_effect=write_or_fail), self.assertRaises(OSError):
            install_action(self.target, self.workspace, home=self.root / "changed home", config=self.config)
        self.assertEqual((self.action.read_bytes(), self.manifest.read_bytes()), original)
        self.assertFalse(list(self.environments.glob(".alp-action-*.tmp")))

    def test_cli_registers_without_constructing_default_runtime(self):
        from agent_learning.cli import main
        with patch("agent_learning.codex_actions.install_action", return_value={"status": "installed"}) as registered, \
                patch("agent_learning.cli.Runtime", side_effect=AssertionError("No default database")), \
                redirect_stdout(io.StringIO()):
            code = main(["--home", str(self.home), "--workspace", str(self.workspace), "codex-action",
                         "--installation", str(self.target), "--config", str(self.config)])
        self.assertEqual(code, 0)
        registered.assert_called_once_with(str(self.target), workspace=str(self.workspace), home=str(self.home),
                                           config=str(self.config), host_instance="default", remove=False)

    def test_remove_deletes_only_owned_files_and_preserves_user_environment(self):
        self.initial()
        self.register()
        main = self.environments / "environment.toml"
        main.write_bytes(b"# original environment\n")
        before = snapshot(self.target)
        self.assertEqual(self.register(remove=True)["status"], "removed")
        self.assertEqual(main.read_bytes(), b"# original environment\n")
        self.assertFalse(self.action.exists())
        self.assertFalse(self.manifest.exists())
        self.assertEqual(snapshot(self.target), before)
        self.assertEqual(self.register(remove=True)["status"], "absent")

    def test_can_remove_owned_action_after_core_uninstall_when_workspace_is_explicit(self):
        self.initial()
        self.register()
        moved = self.root / "old-core"
        self.target.relative_to(self.root)
        moved.relative_to(self.root)
        self.target.rename(moved)
        result = install_action(self.target, self.workspace, remove=True)
        self.assertEqual(result["status"], "removed")
        self.assertTrue(moved.exists())

    def test_invalid_host_instance_and_removal_type_are_rejected(self):
        self.initial()
        for options in ({"host_instance": "other"}, {"remove": "yes"}):
            with self.subTest(options=options), self.assertRaises(ContractError):
                self.register(**options)
        self.assertFalse(self.action.exists())

    def test_symlink_environment_is_rejected_without_following_it(self):
        self.initial()
        self.workspace.joinpath(".codex").mkdir()
        external = self.root / "outside"
        external.mkdir()
        try:
            self.environments.symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("symbolic links unavailable for this Windows account")
        with self.assertRaises(ContractError):
            self.register()
        self.assertEqual(list(external.iterdir()), [])

    def test_windows_encoding_and_posix_quoting_preserve_special_characters(self):
        receipt = {"python": "C:/python path/' $()`python.exe", "runtime_directory": "C:/runtime ' $()` dir"}
        home, config, target = "C:/home ' $()`", "C:/cfg ' $()`/config.json", "C:/plugin ' $()`"
        command = build_action_command(receipt, target, home=home, config=config, platform="win32")
        prefix = "powershell.exe -NoProfile -NonInteractive -EncodedCommand "
        self.assertTrue(command.startswith(prefix))
        script = base64.b64decode(command[len(prefix):]).decode("utf-16-le")
        self.assertIn("$env:ALP_HOME='C:/home '' $()`'", script)
        self.assertIn("$env:ALP_CONFIG='C:/cfg '' $()`/config.json'", script)
        self.assertTrue(script.endswith("; exit $LASTEXITCODE"))
        for platform in ("darwin", "linux"):
            arguments = shlex.split(build_action_command(receipt, target, home=home, config=config, platform=platform))
            self.assertEqual(arguments[:4], ["env", "ALP_HOME=" + home, "ALP_CONFIG=" + config, receipt["python"]])
            self.assertEqual(arguments[-2:], ["--installation", target])
            self.assertNotIn("--workspace", arguments)

    def test_actual_shell_keeps_frozen_data_paths_and_arguments_literal(self):
        runtime = self.root / "runtime ' $x `tick"
        (runtime / "scripts").mkdir(parents=True)
        script = runtime / "scripts/learning.py"
        script.write_text("import os,sys,json;print(json.dumps({'argv':sys.argv[1:],'home':os.environ['ALP_HOME'],'config':os.environ['ALP_CONFIG']}))\n", encoding="utf-8")
        home = self.root / "home ' $x `tick"
        config = self.root / "config ' $x `tick.json"
        target = self.root / "plugin ' $x `tick"
        command = build_action_command({"python": sys.executable, "runtime_directory": runtime}, target,
                                       home=home, config=config)
        args = command.split(" ") if sys.platform == "win32" else ["/bin/sh", "-c", command]
        result = subprocess.run(args, cwd=self.workspace, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["home"], str(home))
        self.assertEqual(observed["config"], str(config))
        self.assertEqual(observed["argv"], ["--home", str(home), "--host", "codex", "--host-instance", "default",
                                           "codex-panel", "--installation", str(target)])
        self.assertFalse(home.exists())


if __name__ == "__main__":
    unittest.main()
