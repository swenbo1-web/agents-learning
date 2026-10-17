"""Codex catalog/project wiring, with isolated installations and no host/model."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.cli import main, parser
from agent_learning.codex_connect import ENV, PLUGIN, SERVER, _document, _patch, connect
from agent_learning.codex_actions import _write_atomic
from agent_learning.install import install
from agent_learning.runtime import ContractError


class CodexConnectionDocumentTests(unittest.TestCase):
    def test_multiline_strings_comments_and_unrelated_tables_are_preserved(self):
        text = '# keep\ninstructions = """Example:\n[mcp_servers.agent_learning]\ncommand = "fake"\n"""\n\n'
        text += '[other]\nvalues = [\n "a#b",\n "[mcp_servers.agent_learning]",\n]\n'
        text += '[[other.records]]\nname = "record"\n'
        result = _patch(text, {SERVER: {"command": "python", "args": ["serve"]}}, {})
        self.assertTrue(result.startswith(text))
        self.assertIn('command = "python"', result)
        self.assertEqual(len([p for p in _document(result)[1] if p == (*SERVER, "command")]), 1)

    def test_dotted_and_quoted_keys_are_edited_without_duplicate_tables(self):
        text = '[ "mcp_servers" . agent_learning ] # old header\nstartup_timeout_sec = 40\n'
        result = _patch(text, {SERVER: {"command": "python", "args": ["serve"]}}, {})
        self.assertIn('startup_timeout_sec = 40', result)
        self.assertEqual(result.count('old header'), 1)
        self.assertEqual(_document(result)[1][(*SERVER, "command")][2], '"python"')

    def test_inline_configuration_is_preserved_for_manual_resolution(self):
        for text in ('mcp_servers = {agent_learning = {command="custom"}}\n',
                     '[mcp_servers]\nagent_learning = {command="custom"}\n'):
            with self.assertRaisesRegex(ContractError, "manual wiring"):
                _patch(text, {SERVER: {"command": "python"}}, {})

    def test_incomplete_strings_arrays_or_duplicate_fields_are_rejected(self):
        for text in ('[]\n', 'note = """unfinished', 'value = [1,2', '[other]\nx=1\nx=2\n',
                     '[mcp_servers.agent_learning]\n[mcp_servers.agent_learning]\n'):
            with self.assertRaises(ContractError):
                _patch(text, {SERVER: {"command": "python"}}, {})

    def test_array_of_tables_repeat_keys_across_elements(self):
        # 数组表的每个元素是独立表，同名键不是重复字段；否则合法配置会被拒。
        text = '[[profiles]]\nname = "a"\n\n[[profiles]]\nname = "b"\n'
        result = _patch(text, {SERVER: {"command": "python", "args": ["serve"]}}, {})
        self.assertTrue(result.startswith(text))
        self.assertEqual(result.count('[[profiles]]'), 2)
        import tomllib
        parsed = tomllib.loads(result)
        self.assertEqual(parsed["profiles"], [{"name": "a"}, {"name": "b"}])
        # 同一元素内的真实重复仍必须拒绝。
        with self.assertRaises(ContractError):
            _patch('[[profiles]]\nname = "a"\nname = "b"\n', {SERVER: {"command": "python"}}, {})


class CodexConnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cc-")
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.cleanup)
        self.workspace, self.target = self.root / "项目 workspace", self.root / "export"
        self.workspace.mkdir()
        self.home, self.codex_home = self.root / "knowledge", self.root / "codex"
        self.config = self.home / "settings.json"
        self.exported = install(self.target, runtime_store=self.root / "releases", panel_workspace=self.workspace)
        self.global_path, self.project_path = self.codex_home / "config.toml", self.workspace / ".codex/config.toml"

    def cleanup(self):
        from agent_learning.release import filesystem_path
        if self.root.exists():
            shutil.rmtree(filesystem_path(self.root), ignore_errors=True)
        self.temp.cleanup()

    def connect(self, **options):
        return connect(self.target, self.workspace, home=self.home, config=self.config,
                       codex_home=self.codex_home, **options)

    def seed(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def fields(self, path):
        return _document(path.read_text(encoding="utf-8"))[1]

    def test_check_does_not_create_host_config_project_config_or_knowledge(self):
        state = self.connect(check=True)
        self.assertEqual(state["status"], "needs_connection")
        self.assertEqual(len(state["changes_needed"]), 2)
        for path in (self.codex_home, self.home, self.project_path):
            self.assertFalse(path.exists())

    def test_connect_is_idempotent_and_separates_catalog_from_project_binding(self):
        state = self.connect()
        self.assertFalse(state["administrator_started"])
        self.assertEqual(state["changes_needed"], [])
        self.assertEqual((state["menu_scope"], state["controls_scope"]), ("host", "installed_project"))
        global_fields, project_fields = self.fields(self.global_path), self.fields(self.project_path)
        self.assertIn("--panel-discovery", json.loads(global_fields[(*SERVER, "args")][2]))
        self.assertNotIn("--panel-installation", global_fields[(*SERVER, "args")][2])
        self.assertIn("--panel-installation", json.loads(project_fields[(*SERVER, "args")][2]))
        self.assertEqual(json.loads(project_fields[(*SERVER, "cwd")][2]), str(self.workspace))
        for fields in (global_fields, project_fields):
            self.assertEqual(json.loads(fields[(*ENV, "ALP_HOME")][2]), str(self.home))
            self.assertEqual(json.loads(fields[(*ENV, "ALP_CONFIG")][2]), str(self.config))
            self.assertEqual(fields[(*PLUGIN, "enabled")][2], "false")
        again = self.connect()
        self.assertFalse(again["restart_required"])
        self.assertNotIn("backup_directory", again)
        self.assertEqual(self.connect(check=True)["status"], "connected")
        self.assertFalse(self.home.exists())

    def test_existing_configuration_comments_and_private_fields_stay_in_place(self):
        old = '# keep model settings\nmodel = "configured-model"\n\n[other]\nnote = "private-fixture"\n'
        self.seed(self.global_path, old)
        state = self.connect()
        self.assertTrue(self.global_path.read_text(encoding="utf-8").startswith(old))
        backup = Path(state["backup_directory"])
        saved = [p.read_text(encoding="utf-8") for p in backup.glob("*.toml")]
        self.assertIn(old, saved)
        self.assertNotIn("private-fixture", json.dumps(state))

    def test_foreign_mcp_connection_is_not_overwritten_in_either_config(self):
        old = '[mcp_servers.agent_learning]\ncommand = "another-program"\nargs = ["serve"]\n'
        self.seed(self.project_path, old)
        with self.assertRaisesRegex(ContractError, "could not be verified"):
            self.connect()
        self.assertEqual(self.project_path.read_text(encoding="utf-8"), old)
        self.assertFalse(self.global_path.exists())

    def test_known_runtime_can_upgrade_while_preserving_unrelated_server_fields(self):
        self.connect()
        old = self.project_path.read_text(encoding="utf-8")
        old = old.replace('[mcp_servers.agent_learning]\n', '[mcp_servers.agent_learning]\nstartup_timeout_sec = 45\n')
        self.project_path.write_text(old, encoding="utf-8")
        self.connect()
        self.assertIn('startup_timeout_sec = 45', self.project_path.read_text(encoding="utf-8"))

    def test_previous_documented_literal_and_multiline_argv_can_be_upgraded(self):
        self.connect()
        old = self.project_path.read_text(encoding="utf-8")
        args = json.loads(self.fields(self.project_path)[(*SERVER, "args")][2])
        replacement = '[\n' + ''.join("  '" + item + "', # argv\n" for item in args) + ']'
        old = old.replace(json.dumps(args, ensure_ascii=False), replacement)
        self.project_path.write_text(old, encoding="utf-8")
        self.connect()
        self.assertEqual(json.loads(self.fields(self.project_path)[(*SERVER, "args")][2]), args)

    def test_second_write_failure_rolls_back_the_first_and_keeps_original_backup(self):
        old = '# original global\n'
        self.seed(self.global_path, old)
        failed = False
        def write(path, raw):
            nonlocal failed
            if path == self.project_path and not failed:
                failed = True
                raise OSError("isolated write failure")
            return _write_atomic(path, raw)
        with patch("agent_learning.codex_connect._write_atomic", side_effect=write):
            with self.assertRaisesRegex(OSError, "isolated write failure"):
                self.connect()
        self.assertEqual(self.global_path.read_text(encoding="utf-8"), old)
        self.assertFalse(self.project_path.exists())

    def test_concurrent_project_edit_is_preserved_and_own_global_write_is_rolled_back(self):
        old, changed = '# original\n', '# external edit\n'
        self.seed(self.global_path, old)
        def write(path, raw):
            result = _write_atomic(path, raw)
            if path == self.global_path and raw.decode("utf-8") != old:
                self.seed(self.project_path, changed)
            return result
        with patch("agent_learning.codex_connect._write_atomic", side_effect=write):
            with self.assertRaisesRegex(ContractError, "changed during"):
                self.connect()
        self.assertEqual(self.global_path.read_text(encoding="utf-8"), old)
        self.assertEqual(self.project_path.read_text(encoding="utf-8"), changed)

    def test_other_workspace_cannot_reuse_an_installed_projects_binding(self):
        other = self.root / "other"
        other.mkdir()
        with self.assertRaisesRegex(ContractError, "exactly match"):
            connect(self.target, other, codex_home=self.codex_home)

    def test_cli_exposes_explicit_connection_and_read_only_check(self):
        args = parser().parse_args(["codex-connect", "--installation", "export", "--check"])
        self.assertTrue(args.check)
        with patch("agent_learning.codex_connect.connect", return_value={"status": "connected"}) as method, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--workspace", "project", "codex-connect", "--installation", "export", "--check"]), 0)
        self.assertEqual(method.call_args.kwargs["check"], True)


if __name__ == "__main__":
    unittest.main()
