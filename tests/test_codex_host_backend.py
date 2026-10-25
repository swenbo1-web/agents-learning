"""Offline tests for the live Codex desktop backend; never launch a model."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from agent_learning import admin_providers as providers, host_cli
from agent_learning.runtime import Runtime


class CodexHostBackendTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "build" / "local" / "fix1424" / "codex-host-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="backend-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        values = {"ALP_HOME": str(self.root / "home"), "ALP_CONFIG": str(self.root / "config.json"),
                  "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"), "CODEX_HOME": str(self.root / "codex-home"),
                  "TMP": str(self.root / "tmp"), "TEMP": str(self.root / "tmp"), "TMPDIR": str(self.root / "tmp"),
                  "CODEX_CLI_PATH": "", host_cli._BACKEND_ENV: "", "PYTHONDONTWRITEBYTECODE": "1"}
        for name in ("ALP_HOME", "ALP_RUNTIME_STORE", "CODEX_HOME", "TMP"):
            Path(values[name]).mkdir(parents=True, exist_ok=True)
        environment = patch.dict(os.environ, values)
        environment.start()
        self.addCleanup(environment.stop)
        self.backend = self.executable(self.root / "OpenAI" / "Codex" / "bin" / "release-current" / "codex.exe")
        self.path_cli = self.executable(self.root / "path" / "codex.exe")
        self.pid = os.getpid()
        self.shell_pid, self.backend_pid = self.pid + 1000000, self.pid + 1000001
        self.images = {
            self.pid: host_cli._ProcessImage(self.pid, self.shell_pid, 300, str(self.root / "python.exe")),
            self.shell_pid: host_cli._ProcessImage(self.shell_pid, self.backend_pid, 200, str(self.root / "pwsh.exe")),
            self.backend_pid: host_cli._ProcessImage(self.backend_pid, 1, 100, str(self.backend))}
        self.original_workspace = host_cli._workspace_root
        for target, kwargs in (("agent_learning.host_cli._workspace_root", {"return_value": self.workspace}),
                               ("agent_learning.host_cli._process_image", {"side_effect": self.images.get}),
                               ("agent_learning.admin_providers.shutil.which", {"return_value": str(self.path_cli)})):
            mocked = patch(target, **kwargs)
            value = mocked.start()
            self.addCleanup(mocked.stop)
            if target.endswith("_process_image"):
                self.read_image = value
            elif target.endswith("shutil.which"):
                self.which = value

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"offline fixture; never executed")
        return path

    def resolve(self, command=None):
        return providers.resolve_command(command or ["codex"], _windows=True)

    def unavailable(self, command=None):
        with self.assertRaises(FileNotFoundError) as caught:
            self.resolve(command)
        self.assertEqual(caught.exception.launch_reason, "host_cli_unavailable")
        self.which.assert_not_called()

    def background_environment(self, command="admin-drain", host="codex"):
        runtime = object.__new__(Runtime)
        runtime.home = self.root / "home"
        runtime.host, runtime.host_instance = host, "offline-instance"
        with patch("subprocess.Popen") as launch:
            runtime._launch_background(self.workspace, command, [])
        return launch.call_args.kwargs["env"]

    def test_default_exe_name_and_exact_http_preset_follow_live_backend(self):
        for command in (["codex"], ["codex.exe"], providers.codex_http_command()):
            with self.subTest(command=command):
                result = self.resolve(command)
                self.assertEqual(result["args"], [str(self.backend), *command[1:]])
                self.assertEqual(result["executable_source"], "host_ancestor")
                self.assertEqual(result["launch_kind"], "executable")
        self.which.assert_not_called()

    def test_hint_has_priority_without_reading_the_process_chain(self):
        with patch.dict(os.environ, {"CODEX_CLI_PATH": str(self.path_cli)}):
            result = self.resolve()
        self.assertEqual(result["args"], [str(self.path_cli)])
        self.assertEqual(result["executable_source"], "host_cli")
        self.read_image.assert_not_called()
        self.which.assert_not_called()

    def test_explicit_paths_and_custom_arguments_preserve_selection(self):
        with patch.dict(os.environ, {"CODEX_CLI_PATH": "invalid hint"}):
            for command in ([str(self.path_cli)], [str(self.path_cli), "trusted-mode"],
                            ["codex", "trusted-mode"], ["custom/codex"]):
                with self.subTest(command=command):
                    result = self.resolve(command)
                    self.assertEqual(result["args"], [str(self.path_cli), *command[1:]])
        self.read_image.assert_not_called()

    def test_explicit_http_executable_preserves_selection(self):
        command = [str(self.path_cli), *providers.codex_http_command()[1:]]
        with patch.dict(os.environ, {"CODEX_CLI_PATH": "invalid hint"}):
            self.assertEqual(self.resolve(command)["args"], command)
        self.read_image.assert_not_called()

    def test_missing_explicit_path_does_not_select_a_host_ancestor(self):
        with patch.object(providers.shutil, "which", return_value=None):
            with self.assertRaises(FileNotFoundError):
                self.resolve([str(self.root / "absent" / "codex.exe")])
        self.read_image.assert_not_called()

    def test_invalid_hint_fails_closed_for_default_and_http(self):
        for hint in ("relative/codex.exe", str(self.root / "absent" / "codex.exe"),
                     str(self.root / "other.exe"), str(self.backend.parent)):
            for command in (["codex"], providers.codex_http_command()):
                with self.subTest(hint=hint, command=command), patch.dict(os.environ, {"CODEX_CLI_PATH": hint}):
                    self.unavailable(command)
        self.read_image.assert_not_called()

    def test_explicit_hint_and_command_inside_workspace_keep_their_contract(self):
        selected = self.executable(self.workspace / "codex.exe")
        with patch.dict(os.environ, {"CODEX_CLI_PATH": str(selected)}):
            self.assertEqual(self.resolve()["args"], [str(selected)])
        self.assertEqual(self.resolve([str(selected)])["args"], [str(selected)])
        self.read_image.assert_not_called()

    def test_no_backend_in_parent_chain_uses_original_path_fallback(self):
        self.images[self.backend_pid] = replace(self.images[self.backend_pid], image=str(self.root / "other.exe"))
        self.assertEqual(self.resolve()["args"], [str(self.path_cli)])
        self.which.assert_called_once_with("codex")

    def test_ui_and_unknown_same_name_images_are_not_backend_candidates(self):
        for image in (self.root / "OpenAI" / "Codex" / "Codex.exe", self.root / "unknown" / "codex.exe",
                      self.root / "bin" / "release-newest" / "codex.exe"):
            with self.subTest(image=image):
                self.images[self.backend_pid] = replace(self.images[self.backend_pid], image=str(image))
                self.assertEqual(self.resolve()["args"], [str(self.path_cli)])

    def test_missing_or_denied_process_metadata_does_not_guess_other_processes(self):
        self.images.pop(self.shell_pid)
        self.assertEqual(self.resolve()["args"], [str(self.path_cli)])
        self.assertEqual(self.read_image.call_count, 2)

    def test_transient_metadata_failure_is_not_cached(self):
        current = self.images.pop(self.pid)
        self.assertEqual(self.resolve()["args"], [str(self.path_cli)])
        self.images[self.pid] = current
        self.assertEqual(self.resolve()["args"], [str(self.backend)])

    def test_non_windows_without_hint_keeps_path_fallback(self):
        result = providers.resolve_command(["codex"], _windows=False)
        self.assertEqual(result["args"], [str(self.path_cli)])
        self.read_image.assert_not_called()

    def test_non_windows_still_honors_explicit_host_hint(self):
        with patch.dict(os.environ, {"CODEX_CLI_PATH": str(self.backend)}):
            result = providers.resolve_command(["codex"], _windows=False)
        self.assertEqual(result["executable_source"], "host_cli")
        self.read_image.assert_not_called()

    def test_cycle_and_newer_parent_pid_do_not_establish_trusted_ancestry(self):
        original = self.images[self.shell_pid]
        for identity in (replace(original, parent_pid=self.pid), replace(original, created=400),
                         replace(original, pid=original.pid + 9), replace(original, created=0)):
            with self.subTest(identity=identity):
                self.images[self.shell_pid] = identity
                self.assertEqual(self.resolve()["args"], [str(self.path_cli)])

    def test_parent_chain_has_a_fixed_depth_bound(self):
        self.images.clear()
        for index in range(host_cli._MAX_ANCESTORS + 3):
            pid = self.pid if index == 0 else self.pid + 2000000 + index
            parent = self.pid + 2000000 + index + 1
            image = str(self.backend) if index == host_cli._MAX_ANCESTORS + 1 else str(self.root / "python.exe")
            self.images[pid] = host_cli._ProcessImage(pid, parent, 1000 - index, image)
        self.assertEqual(self.resolve()["args"], [str(self.path_cli)])
        self.assertEqual(self.read_image.call_count, host_cli._MAX_ANCESTORS + 1)

    def test_pid_reuse_reparenting_and_exit_during_final_check_fail_closed(self):
        original = self.images[self.pid]
        for changed in (replace(original, created=301), replace(original, parent_pid=9), None):
            calls = 0
            def observe(pid):
                nonlocal calls
                if pid == self.pid:
                    calls += 1
                    return original if calls == 1 else changed
                return self.images.get(pid)
            with self.subTest(changed=changed), patch.object(host_cli, "_process_image", side_effect=observe):
                self.unavailable()

    def test_recognized_backend_with_missing_file_fails_closed(self):
        self.backend.unlink()
        self.unavailable()

    def test_recognized_backend_directory_is_not_an_executable(self):
        self.backend.unlink()
        self.backend.mkdir()
        self.unavailable()

    def test_backend_deleted_during_process_recheck_fails_closed(self):
        observations = 0
        def observe(pid):
            nonlocal observations
            if pid == self.backend_pid:
                observations += 1
                if observations == 2:
                    self.backend.unlink()
            return self.images.get(pid)
        with patch.object(host_cli, "_process_image", side_effect=observe):
            self.unavailable()

    def test_backend_file_replaced_during_discovery_fails_closed(self):
        original = host_cli._file_identity
        calls = 0
        def inspect(path, workspace):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.backend.unlink()
                self.backend.write_bytes(b"different file identity")
            return original(path, workspace)
        with patch.object(host_cli, "_file_identity", side_effect=inspect):
            self.unavailable()

    def test_file_identity_is_rechecked_on_every_resolution(self):
        self.assertEqual(self.resolve()["args"], [str(self.backend)])
        self.backend.unlink()
        self.unavailable()

    def test_upgrade_follows_new_live_release_without_directory_enumeration(self):
        self.assertEqual(self.resolve()["args"], [str(self.backend)])
        upgraded = self.executable(self.backend.parent.parent / "release-next" / "codex.exe")
        self.executable(self.backend.parent.parent / "release-looks-newest" / "codex.exe")
        self.images[self.backend_pid] = replace(self.images[self.backend_pid], image=str(upgraded))
        with patch.object(Path, "iterdir", side_effect=AssertionError("directory scan")), \
                patch.object(Path, "glob", side_effect=AssertionError("directory scan")):
            self.assertEqual(self.resolve()["args"], [str(upgraded)])

    def test_implicit_backend_inside_workspace_is_rejected(self):
        shadow = self.executable(self.workspace / "OpenAI" / "Codex" / "bin" / "release-shadow" / "codex.exe")
        self.images[self.backend_pid] = replace(self.images[self.backend_pid], image=str(shadow))
        self.unavailable()

    def test_implicit_relative_and_traversal_images_are_rejected(self):
        for image in ("OpenAI/Codex/bin/release-shadow/codex.exe",
                      str(self.backend.parent.parent / ".." / "codex.exe")):
            with self.subTest(image=image):
                self.images[self.backend_pid] = replace(self.images[self.backend_pid], image=image)
                self.unavailable()

    @unittest.skipUnless(os.name == "nt", "Windows UNC path")
    def test_implicit_unc_image_is_rejected_without_reading_network_metadata(self):
        image = r"\\unreachable\share\OpenAI\Codex\bin\release\codex.exe"
        self.images[self.backend_pid] = replace(self.images[self.backend_pid], image=image)
        with patch.object(Path, "lstat", side_effect=AssertionError("no network metadata lookup")):
            self.unavailable()

    def test_workspace_detection_includes_checkout_root_from_a_subdirectory(self):
        (self.workspace / ".git").mkdir()
        nested = self.workspace / "nested" / "folder"
        nested.mkdir(parents=True)
        with patch.object(Path, "cwd", return_value=nested):
            self.assertEqual(self.original_workspace(), self.workspace)

    def test_file_and_parent_reparse_points_are_rejected_before_resolution(self):
        original = Path.lstat
        for linked in (self.backend, self.backend.parent):
            def inspect(path, *args, **kwargs):
                info = original(path, *args, **kwargs)
                if path == linked:
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                return info
            with self.subTest(linked=linked), patch.object(Path, "lstat", inspect):
                self.unavailable()

    def test_real_symlink_backend_is_rejected(self):
        self.backend.unlink()
        try:
            self.backend.symlink_to(self.path_cli)
        except OSError:
            self.skipTest("symbolic links unavailable")
        self.unavailable()

    def test_status_reports_launcher_only_without_spawning_processes(self):
        original = providers.resolve_command
        with patch.object(providers, "resolve_command", side_effect=lambda command: original(command, _windows=True)), \
                patch.dict(providers._CAPABILITIES, {}, clear=True), \
                patch("subprocess.Popen") as popen, patch("subprocess.run") as run:
            result = providers.executor_availability({"provider": "codex", "command": ["codex"]})
        popen.assert_not_called()
        run.assert_not_called()
        self.assertTrue(result["cli_available"])
        self.assertEqual(result["executable_source"], "host_ancestor")
        self.assertEqual(result["readiness_scope"], "launcher_only")
        self.assertFalse(result["capability_checked"])
        self.assertIsNone(result["capability_ready"])

    @unittest.skipUnless(os.name == "nt", "Windows background backend inheritance")
    def test_short_lived_hook_passes_backend_to_admin_and_scheduler_workers(self):
        for command in ("admin-drain", "scheduler-run"):
            with self.subTest(command=command):
                environment = self.background_environment(command)
                self.assertIn(host_cli._BACKEND_ENV, environment)
                self.assertEqual(os.environ[host_cli._BACKEND_ENV], "")
                self.assertEqual(environment["CODEX_CLI_PATH"], "")
                hook = self.images.pop(self.shell_pid)
                self.read_image.reset_mock()
                try:
                    with patch.dict(os.environ, environment):
                        self.assertEqual(self.resolve()["args"], [str(self.backend)])
                        self.assertEqual(self.resolve(providers.codex_http_command())["args"],
                                         [str(self.backend), *providers.codex_http_command()[1:]])
                    self.assertTrue(all(call.args == (self.backend_pid,) for call in self.read_image.call_args_list))
                    self.which.assert_not_called()
                finally:
                    self.images[self.shell_pid] = hook

    @unittest.skipUnless(os.name == "nt", "Windows background backend inheritance")
    def test_other_background_commands_and_hosts_do_not_capture_backend(self):
        for command, host in (("update-check", "codex"), ("admin-drain", "antigravity"), ("scheduler-run", "dsh")):
            with self.subTest(command=command, host=host):
                self.assertEqual(self.background_environment(command, host)[host_cli._BACKEND_ENV], "")
        self.read_image.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows background backend inheritance")
    def test_background_keeps_explicit_hint_ahead_of_ancestry(self):
        with patch.dict(os.environ, {"CODEX_CLI_PATH": str(self.path_cli)}):
            environment = self.background_environment()
        self.assertEqual(environment["CODEX_CLI_PATH"], str(self.path_cli))
        self.assertEqual(environment[host_cli._BACKEND_ENV], "")
        self.read_image.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows background backend inheritance")
    def test_recognized_failure_at_dispatch_is_carried_without_path_fallback(self):
        self.backend.unlink()
        environment = self.background_environment()
        with patch.dict(os.environ, environment):
            self.unavailable()
            # Explicit launchers preserve their contract even when discovery failed.
            self.assertEqual(self.resolve([str(self.path_cli)])["args"], [str(self.path_cli)])

    def test_inherited_deleted_or_replaced_file_fails_closed(self):
        environment = host_cli.codex_background_environment(os.environ, windows=True)
        for replacement in (None, b"replacement backend"):
            with self.subTest(replacement=replacement):
                self.backend.unlink()
                if replacement is not None:
                    self.backend.write_bytes(replacement)
                with patch.dict(os.environ, environment):
                    self.unavailable()
                if replacement is None:
                    self.backend.write_bytes(b"restored fixture")

    def test_inherited_backend_exit_and_pid_reuse_fail_closed(self):
        environment = host_cli.codex_background_environment(os.environ, windows=True)
        original = self.images[self.backend_pid]
        for changed in (None, replace(original, created=original.created + 1),
                        replace(original, image=str(self.path_cli))):
            with self.subTest(changed=changed):
                self.images[self.backend_pid] = changed
                with patch.dict(os.environ, environment):
                    self.unavailable()

    def test_invalid_inherited_snapshots_fail_closed_but_explicit_commands_win(self):
        for value in ("unavailable", "{}", "[]", "x" * (host_cli._MAX_SNAPSHOT_CHARS + 1), "[" * 2000):
            with self.subTest(value=value[:30]), patch.dict(os.environ, {host_cli._BACKEND_ENV: value}):
                self.unavailable()
                self.assertEqual(self.resolve([str(self.path_cli)])["args"], [str(self.path_cli)])
                with patch.dict(os.environ, {"CODEX_CLI_PATH": str(self.path_cli)}):
                    self.assertEqual(self.resolve()["args"], [str(self.path_cli)])
        self.read_image.assert_not_called()

    def test_inherited_snapshot_types_and_file_identity_are_strict(self):
        environment = host_cli.codex_background_environment(os.environ, windows=True)
        original = json.loads(environment[host_cli._BACKEND_ENV])
        for changes in ({"version": True}, {"pid": True}, {"created": 0}, {"file": [0] * 4},
                        {"file": [False] * 5}, {"image": "relative/codex.exe"}, {"extra": 1}):
            with self.subTest(changes=changes), patch.dict(os.environ, {
                    host_cli._BACKEND_ENV: json.dumps({**original, **changes})}):
                self.unavailable()

    @unittest.skipUnless(os.name == "nt", "Windows background backend inheritance")
    def test_worker_forwards_existing_snapshot_without_a_live_hook_chain(self):
        environment = self.background_environment()
        self.images.pop(self.shell_pid)
        self.read_image.reset_mock()
        with patch.dict(os.environ, environment):
            forwarded = self.background_environment("scheduler-run")
            self.assertEqual(forwarded[host_cli._BACKEND_ENV], environment[host_cli._BACKEND_ENV])
            self.assertEqual(self.resolve()["args"], [str(self.backend)])
        self.assertTrue(all(call.args == (self.backend_pid,) for call in self.read_image.call_args_list))

    @unittest.skipUnless(os.name == "nt", "Windows native short-lived Hook process")
    def test_native_worker_uses_snapshot_after_its_hook_process_exits(self):
        # The fixture backend is only a renamed Python interpreter. It executes
        # three small local scripts and never starts Codex or a model.
        shutil.copyfile(sys.executable, self.backend)
        (self.workspace / ".git").mkdir()
        result = self.root / "worker-result.json"
        worker, hook, controller = (self.root / name for name in ("worker.py", "hook.py", "controller.py"))
        worker.write_text(
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "from unittest.mock import patch\n"
            "from agent_learning.host_cli import _process_image\n"
            "from agent_learning.admin_providers import resolve_command\n"
            "parent = int(sys.argv[1])\n"
            "deadline = time.monotonic() + 5\n"
            "while _process_image(parent) is not None and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "assert _process_image(parent) is None, 'Hook did not exit'\n"
            "with patch('agent_learning.admin_providers.shutil.which', side_effect=AssertionError('PATH fallback')):\n"
            "    resolved = resolve_command(['codex'])\n"
            f"Path({str(result)!r}).write_text(json.dumps(resolved), encoding='utf-8')\n", encoding="utf-8")
        hook.write_text(
            "import os, subprocess, sys\n"
            "from agent_learning.host_cli import codex_background_environment\n"
            "environment = codex_background_environment(os.environ)\n"
            f"subprocess.Popen([{sys.executable!r}, '-X', 'utf8', '-B', {str(worker)!r}, str(os.getpid())],\n"
            "    env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)\n", encoding="utf-8")
        controller.write_text(
            "import os, subprocess, time\n"
            "from pathlib import Path\n"
            f"process = subprocess.run([{sys.executable!r}, '-X', 'utf8', '-B', {str(hook)!r}],\n"
            "    env=dict(os.environ), timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)\n"
            "assert process.returncode == 0\n"
            "deadline = time.monotonic() + 8\n"
            f"result = Path({str(result)!r})\n"
            "while not result.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "assert result.exists(), 'Worker did not resolve the backend'\n", encoding="utf-8")
        environment = dict(os.environ, ALP_DISABLED="1", PYTHONHOME=sys.base_prefix,
                           PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        # Windows loads Python's adjacent runtime DLL from this explicit PATH;
        # PYTHONHOME keeps the copied interpreter's standard library unchanged.
        environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
        completed = subprocess.run([str(self.backend), "-X", "utf8", "-B", str(controller)],
                                   env=environment, cwd=self.workspace, capture_output=True,
                                   timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        observed = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(observed["args"], [str(self.backend)])
        self.assertEqual(observed["executable_source"], "host_ancestor")


class NativeProcessMetadataTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows native metadata reader")
    def test_current_process_metadata_is_read_without_external_tools(self):
        with patch("subprocess.Popen") as popen, patch("subprocess.run") as run:
            identity = host_cli._process_image(os.getpid())
        popen.assert_not_called()
        run.assert_not_called()
        self.assertIsNotNone(identity)
        self.assertEqual(identity.pid, os.getpid())
        self.assertGreater(identity.created, 0)
        self.assertGreater(identity.parent_pid, 0)
        self.assertTrue(Path(identity.image).is_absolute())

    def test_invalid_process_ids_are_unavailable(self):
        for value in (0, -1, True, "1"):
            self.assertIsNone(host_cli._process_image(value))


if __name__ == "__main__":
    unittest.main()
