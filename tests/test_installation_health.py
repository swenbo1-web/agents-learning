"""Lost-interpreter recovery without live hosts, knowledge or model calls."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.cli import main
from agent_learning.codex_connect import connect, _document, SERVER
from agent_learning.install import install, _hook_commands
from agent_learning.install_update import inspect_installation, snapshot
from agent_learning.installation_health import installation_check, interpreter_risks, python_health, select_python
from agent_learning.installation_repair import repair_python
from agent_learning.release import filesystem_path, validate_release_root
from agent_learning.runtime import ContractError


class InterpreterHealthTests(unittest.TestCase):
    def test_known_host_caches_and_temporary_locations_are_classified(self):
        for value, reason in (("C:/Users/u/.cache/codex-runtimes/current/python.exe", "codex_runtime_cache"),
                              ("C:/Users/u/AppData/Local/OpenAI/Codex/runtimes/python/abc/python.exe", "codex_runtime_cache"),
                              ("/home/u/.codex/plugins/cache/plugin/python", "host_plugin_cache"),
                              ("C:/Users/u/AppData/Local/uv/cache/archive/python.exe", "package_cache"),
                              (str(Path(tempfile.gettempdir()) / "python"), "temporary_directory")):
            with self.subTest(value=value):
                self.assertIn(reason, interpreter_risks(value))
        self.assertEqual(interpreter_risks("/opt/hostedtoolcache/Python/3.12/bin/python"), [])
        self.assertEqual(interpreter_risks("C:/Users/u/AppData/Roaming/uv/python/cpython/python.exe"), [])

    def test_real_interpreter_probe_and_missing_interpreter(self):
        report = python_health(sys.executable, probe=True)
        self.assertTrue(report["usable"])
        self.assertTrue(report["probed"])
        self.assertGreaterEqual(report["version"], [3, 10, 0])
        missing = Path(tempfile.gettempdir()) / "alp-python-not-present-12526/python.exe"
        with patch("agent_learning.installation_health.subprocess.run", side_effect=AssertionError("no launch")):
            self.assertEqual(python_health(missing, probe=True)["state"], "missing")

    def test_probe_is_bounded_isolated_and_does_not_disclose_stderr(self):
        for outcome in (subprocess.TimeoutExpired("fixture", 10), OSError("private-fixture"),
                        subprocess.CompletedProcess([], 1, b"", b"private-fixture"),
                        subprocess.CompletedProcess([], 0, b"not-json", b"private-fixture")):
            options = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            with self.subTest(outcome=type(outcome).__name__), \
                    patch("agent_learning.installation_health.subprocess.run", **options) as run:
                report = python_health(sys.executable, probe=True)
                self.assertEqual(report["state"], "probe_failed")
                self.assertNotIn("private-fixture", json.dumps(report))
                self.assertEqual(run.call_args.args[0][1:4], ["-I", "-S", "-B"])
                self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_old_python_and_volatile_base_prefix_are_detected(self):
        for version, prefix, state in (([3, 9, 9], "/opt/python", "unsupported_version"),
                                       ([3, 12, 0], "/home/u/.cache/codex-runtimes/current", "volatile")):
            body = {"kind": "alp-python-check", "version": version, "base_prefix": prefix}
            with patch("agent_learning.installation_health.subprocess.run",
                       return_value=subprocess.CompletedProcess([], 0, json.dumps(body).encode())):
                self.assertEqual(python_health(sys.executable, probe=True)["state"], state)

    def test_disposable_interpreter_needs_explicit_override(self):
        report = {"path": sys.executable, "usable": True, "risks": ["codex_runtime_cache"], "state": "volatile"}
        with patch("agent_learning.installation_health.python_health", return_value=report):
            with self.assertRaisesRegex(ContractError, "--allow-volatile-python"):
                select_python()
            self.assertEqual(select_python(allow_volatile=True), (sys.executable, report))


class InstallationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.area = tempfile.TemporaryDirectory(prefix="ih-")
        self.root = Path(self.area.name).resolve()
        self.addCleanup(self.cleanup)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.target, self.store = self.root / "export", self.root / "r"
        self.home = self.root / "knowledge"
        self.codex_home = self.root / "codex"

    def cleanup(self):
        if self.root.exists():
            shutil.rmtree(filesystem_path(self.root), ignore_errors=True)
        self.area.cleanup()

    def lost_installation(self, host="codex", *, multi=False):
        old = self.root / "removed/python.exe"
        health = {"path": str(old), "state": "healthy", "usable": True, "risks": []}
        options = ({"panel_workspace": self.workspace} if host == "codex" else
                   {"workspace": self.workspace, "home": self.home})
        # Construct the real export contract with a simulated old interpreter;
        # no placeholder executable or old model process is ever launched.
        with patch("agent_learning.installation_health.select_python", return_value=(str(old), health)):
            install(self.target, runtime_store=self.store, host=host, hook_mode="enforce", **options)
            if multi:
                other = self.root / "second"
                other.mkdir()
                install(self.target, upgrade=True, host=host, workspace=other)
        return inspect_installation(self.target, _hook_commands)

    def test_bad_selection_stops_install_before_any_export_or_store_write(self):
        with self.assertRaisesRegex(ContractError, "--python"):
            install(self.target, runtime_store=self.store, python=str(self.root / "missing/python"))
        self.assertFalse(self.target.exists())
        self.assertFalse(self.store.exists())

    def test_cache_rejection_is_read_only_and_override_is_reported(self):
        report = {"path": sys.executable, "usable": True, "risks": ["codex_runtime_cache"], "state": "volatile"}
        with patch("agent_learning.installation_health.python_health", return_value=report):
            with self.assertRaisesRegex(ContractError, "disposable"):
                install(self.target, runtime_store=self.store)
            self.assertFalse(self.store.exists())
            result = install(self.target, runtime_store=self.store, allow_volatile_python=True)
        self.assertEqual(result["interpreter"]["risks"], ["codex_runtime_cache"])

    def test_routine_upgrade_keeps_original_python_instead_of_the_current_host(self):
        first = install(self.target, runtime_store=self.store)
        with patch("agent_learning.installation_health.sys.executable", str(self.root / "host-cache/python.exe")):
            result = install(self.target, upgrade=True)
        self.assertEqual(result["python"], first["python"])

    def test_upgrade_of_missing_interpreter_requires_explicit_replacement(self):
        self.lost_installation()
        before = snapshot(self.target)
        with self.assertRaisesRegex(ContractError, "--python"):
            install(self.target, upgrade=True)
        self.assertEqual(snapshot(self.target), before)
        result = install(self.target, upgrade=True, python=sys.executable)
        self.assertEqual(result["python"], sys.executable)

    def test_missing_python_diagnosis_never_opens_runtime_or_changes_export(self):
        self.lost_installation()
        before = snapshot(self.target)
        with patch("agent_learning.runtime.Runtime.__init__", side_effect=AssertionError("no Runtime")):
            result = installation_check(self.target)
        self.assertEqual(result["status"], "repair_needed")
        self.assertEqual(result["interpreter"]["state"], "missing")
        self.assertEqual(snapshot(self.target), before)
        self.assertFalse(self.home.exists())
        self.assertFalse(result["host_connection_verified"])

    def test_three_host_repairs_keep_exact_release_bindings_extras_and_external_data(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                self.target = self.root / host
                prior = self.lost_installation(host, multi=host == "dsh")
                release = validate_release_root(prior["runtime_directory"])
                extra = self.target / "personal.txt"
                extra.write_text("private fixture", encoding="utf-8")
                self.home.mkdir(exist_ok=True)
                marker = self.home / "runtime.sqlite3"
                marker.write_bytes(b"untouched database fixture")
                with patch("agent_learning.runtime.Runtime.__init__", side_effect=AssertionError("no Runtime")):
                    result = repair_python(self.target, sys.executable, expected_release_id=release["release_id"])
                current = inspect_installation(self.target, _hook_commands)
                expected = dict(prior["receipt"], python=sys.executable)
                self.assertEqual(current["receipt"], expected)
                self.assertEqual(validate_release_root(prior["runtime_directory"]), release)
                self.assertEqual(marker.read_bytes(), b"untouched database fixture")
                self.assertEqual(extra.read_text(), "private fixture")
                self.assertIn("personal.txt", current["extra_files"])
                self.assertEqual(result["status"], "repaired")
                self.assertEqual(result["preserved_extra_files"], ["personal.txt"])
                saved = json.loads((Path(result["backup_directory"]) / "runtime-release.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["python"], prior["receipt"]["python"])
                before = snapshot(self.target)
                with patch("agent_learning.installation_repair.switch_installation", side_effect=AssertionError("no rewrite")):
                    self.assertEqual(repair_python(self.target, sys.executable)["status"], "unchanged")
                self.assertEqual(snapshot(self.target), before)

    def test_modified_files_wrong_identity_and_switch_failure_preserve_installation(self):
        self.lost_installation()
        before = snapshot(self.target)
        with self.assertRaisesRegex(ContractError, "expected release identity"):
            repair_python(self.target, expected_release_id="wrong")
        self.assertEqual(snapshot(self.target), before)
        with patch("agent_learning.installation_repair.switch_installation", side_effect=OSError("fixture")):
            with self.assertRaisesRegex(ContractError, "repair failed"):
                repair_python(self.target)
        self.assertEqual(snapshot(self.target), before)
        self.assertFalse(list(self.target.parent.glob(".alp-staging-*")))
        (self.target / "skills/learning-worker/SKILL.md").write_text("user change", encoding="utf-8")
        modified = snapshot(self.target)
        with self.assertRaises(ContractError):
            repair_python(self.target)
        self.assertEqual(snapshot(self.target), modified)

    def seed_connection(self, old, *, command=None, extra=None):
        for path, tail in ((self.codex_home / "config.toml", ["--panel-discovery"]),
                           (self.workspace / ".codex/config.toml", ["--panel-installation", str(self.target)])):
            argv = ["-B", str(old["runtime_directory"] / "scripts/learning.py"), "serve", "--role", "worker", *tail]
            if extra:
                argv += extra
            text = ('# unrelated setting\nmodel = "retained"\n[mcp_servers.agent_learning]\ncommand = '
                    + json.dumps(command or old["receipt"]["python"]) + '\nargs = ' + json.dumps(argv) + '\n')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def test_codex_reconnection_requires_explicit_repair_and_preserves_user_settings(self):
        old = self.lost_installation()
        self.seed_connection(old)
        original = (self.codex_home / "config.toml").read_bytes()
        repair_python(self.target)
        options = {"home": self.home, "codex_home": self.codex_home}
        with self.assertRaises(ContractError):
            connect(self.target, self.workspace, **options)
        self.assertEqual((self.codex_home / "config.toml").read_bytes(), original)
        check = connect(self.target, self.workspace, repair_python=True, check=True, **options)
        self.assertEqual(check["status"], "needs_connection")
        self.assertEqual((self.codex_home / "config.toml").read_bytes(), original)
        result = connect(self.target, self.workspace, repair_python=True, **options)
        self.assertEqual(result["status"], "connected")
        for path in (self.codex_home / "config.toml", self.workspace / ".codex/config.toml"):
            text = path.read_text(encoding="utf-8")
            self.assertIn('model = "retained"', text)
            fields = _document(text)[1]
            self.assertEqual(json.loads(fields[(*SERVER, "command")][2]), sys.executable)
        self.assertFalse(self.home.exists())

    def test_reconnection_cannot_rebind_a_different_project_working_directory(self):
        old = self.lost_installation()
        self.seed_connection(old)
        repair_python(self.target)
        other = self.root / "other-project"
        other.mkdir()
        path = self.workspace / ".codex/config.toml"
        with path.open("a", encoding="utf-8") as stream:
            stream.write('cwd = ' + json.dumps(str(other)) + '\n')
        before = path.read_bytes()
        with self.assertRaises(ContractError):
            connect(self.target, self.workspace, home=self.home, codex_home=self.codex_home, repair_python=True)
        self.assertEqual(path.read_bytes(), before)

    def test_reconnection_rejects_foreign_programs_modified_args_and_other_projects(self):
        old = self.lost_installation()
        repair_python(self.target)
        for command, extra in ((str(self.root / "node.exe"), None), (None, ["--resume", "other"]),
                               (None, ["--panel-installation", str(self.root / "other")])):
            with self.subTest(command=command, extra=extra):
                self.seed_connection(old, command=command, extra=extra)
                before = (self.codex_home / "config.toml").read_bytes()
                with self.assertRaises(ContractError):
                    connect(self.target, self.workspace, home=self.home, codex_home=self.codex_home, repair_python=True)
                self.assertEqual((self.codex_home / "config.toml").read_bytes(), before)

    def test_cli_requires_confirmation_and_diagnostics_do_not_initialize_runtime(self):
        self.lost_installation()
        before = snapshot(self.target)
        for command, expected in (("installation-check", "repair_needed"), ("repair-python", "error")):
            output, error = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error), \
                    patch("agent_learning.runtime.Runtime.__init__", side_effect=AssertionError("no Runtime")):
                code = main([command, "--installation", str(self.target)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue() or error.getvalue())["status"], expected)
        self.assertEqual(snapshot(self.target), before)
        self.assertFalse(self.home.exists())
