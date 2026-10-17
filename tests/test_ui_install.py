"""UI-only Antigravity export contracts, without a host or knowledge database."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.cli import main
from agent_learning.install import install
from agent_learning.install_update import snapshot
from agent_learning.runtime import ContractError
from agent_learning.ui_install import MANIFEST, REGISTRY, install_ui


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, body):
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class UiInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ui-")
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self._cleanup_root)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.home = self.root / "home"
        self.store = self.root / "releases"
        self.core = self.workspace / ".agents/plugins/agent-learning"
        self.target = self.root / "global/agent-learning-ui"

    def _cleanup_root(self):
        from agent_learning.release import filesystem_path
        if self.root.exists() and self.root.name.startswith("ui-"):
            shutil.rmtree(filesystem_path(self.root), ignore_errors=True)
        self.temp.cleanup()

    def core_install(self, destination=None, workspace=None, home=None, **options):
        return install(destination or self.core, runtime_store=self.store, host="antigravity",
                       workspace=workspace or self.workspace, home=home or self.home, **options)

    def test_ui_export_has_only_ui_resources_and_creates_no_knowledge_or_host_settings(self):
        core = self.core_install()
        core_before = snapshot(self.core)
        release_before = snapshot(Path(core["runtime_directory"]))
        with patch("agent_learning.cli.Runtime", side_effect=AssertionError("No runtime should be opened")):
            result = install_ui(self.target, [self.core])
        self.assertEqual(result["registered_projects"], 1)
        self.assertTrue(result["ui_only"])
        self.assertEqual(result["enabled"], "requires_host_activation")
        self.assertFalse(self.home.exists())
        self.assertEqual(snapshot(self.core), core_before)
        self.assertEqual(snapshot(Path(core["runtime_directory"])), release_before)
        files = set(snapshot(self.target)["files"])
        self.assertEqual(files, {"plugin.json", MANIFEST, REGISTRY, "sidecars/control/sidecar.json",
                                 "sidecars/control/main.mjs", "sidecars/control/index.html"})
        for forbidden in ("hooks.json", "mcp_config.json", "runtime-release.json", "runtime.sqlite3", "config.json"):
            self.assertNotIn(forbidden, files)
        self.assertFalse((self.target / "skills").exists())
        registry = read_json(self.target / REGISTRY)
        self.assertEqual(registry["schema_version"], 1)
        entry = registry["projects"][0]
        self.assertEqual(entry["workspace"], str(self.workspace))
        self.assertEqual(entry["installation"], str(self.core))
        self.assertEqual(entry["receipt_sha256"], hashlib.sha256((self.core / "runtime-release.json").read_bytes()).hexdigest())
        self.assertEqual(entry["runtime_directory"], core["runtime_directory"])
        self.assertEqual(result["host_activation"], {"sidecars": {"agent-learning-ui/control": {"enabled": True}}})

    def test_multiple_projects_add_refresh_remove_without_touching_core(self):
        self.core_install()
        install_ui(self.target, [self.core])
        other_workspace = self.root / "second workspace"
        other_workspace.mkdir()
        other = other_workspace / ".agents/plugins/agent-learning"
        self.core_install(destination=other, workspace=other_workspace)
        result = install_ui(self.target, [other], upgrade=True)
        self.assertEqual(result["registered_projects"], 2)
        self.assertTrue(Path(result["backup"]).is_dir())
        old_digest = next(entry["receipt_sha256"] for entry in read_json(self.target / REGISTRY)["projects"]
                          if entry["installation"] == str(self.core))
        self.core_install(upgrade=True, hook_mode="enforce")
        install_ui(self.target, [self.core], upgrade=True)
        entries = read_json(self.target / REGISTRY)["projects"]
        self.assertEqual(len(entries), 2)
        self.assertNotEqual(next(entry["receipt_sha256"] for entry in entries
                                 if entry["installation"] == str(self.core)), old_digest)
        before = snapshot(self.core)
        result = install_ui(self.target, [self.core], upgrade=True, remove=True)
        self.assertEqual(result["registered_projects"], 1)
        self.assertEqual(read_json(self.target / REGISTRY)["projects"][0]["installation"], str(other))
        self.assertEqual(snapshot(self.core), before)
        install_ui(self.target, [other], upgrade=True, remove=True)
        self.assertEqual(read_json(self.target / REGISTRY)["projects"], [])
        self.assertFalse(self.home.exists())

    def test_interpreter_alias_is_valid_for_registration_refresh_and_removal(self):
        from agent_learning.install_update import _linked

        executable = self.root / "venv/bin/python"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"isolated interpreter placeholder; never executed")
        with patch("agent_learning.install.sys.executable", str(executable)):
            self.core_install()

        def linked(path):
            return path in {executable, executable.parent} or _linked(path)

        with patch("agent_learning.install_update._linked", side_effect=linked):
            install_ui(self.target, [self.core])
            install_ui(self.target, [self.core], upgrade=True)
        entry = read_json(self.target / REGISTRY)["projects"][0]
        self.assertEqual(entry["python"], str(executable))
        executable.unlink()
        result = install_ui(self.target, [self.core], upgrade=True, remove=True)
        self.assertEqual(result["registered_projects"], 0)
        self.assertFalse(self.home.exists())

    def test_interpreter_alias_support_does_not_allow_linked_workspace(self):
        from agent_learning.install_update import _linked

        self.core_install()
        with patch("agent_learning.install_update._linked", side_effect=lambda path: path == self.workspace or _linked(path)):
            with self.assertRaises(ContractError):
                install_ui(self.target, [self.core])
        self.assertFalse(self.target.exists())

    def test_remove_registration_after_core_was_uninstalled(self):
        self.core_install()
        install_ui(self.target, [self.core])
        moved = self.root / "old-core"
        self.core.relative_to(self.root)
        moved.relative_to(self.root)
        self.core.rename(moved)
        result = install_ui(self.target, [self.core], upgrade=True, remove=True)
        self.assertEqual(result["registered_projects"], 0)
        self.assertTrue(moved.is_dir())

    def test_extra_files_and_empty_directories_survive_updates(self):
        self.core_install()
        install_ui(self.target, [self.core])
        extra = self.target / "personal/settings.json"
        extra.parent.mkdir()
        extra.write_bytes(b'{"custom":true}')
        empty = self.target / "personal/empty"
        empty.mkdir()
        result = install_ui(self.target, [self.core], upgrade=True)
        self.assertEqual(extra.read_bytes(), b'{"custom":true}')
        self.assertTrue(empty.is_dir())
        backup = Path(result["backup"])
        self.assertEqual((backup / "personal/settings.json").read_bytes(), extra.read_bytes())
        self.assertTrue((backup / "personal/empty").is_dir())
        self.assertNotIn("personal/settings.json", read_json(self.target / MANIFEST)["files"])

    def test_staging_and_backups_stay_outside_the_global_plugin_discovery_root(self):
        self.core_install()
        create_directory = tempfile.mkdtemp
        with patch("agent_learning.ui_install.tempfile.mkdtemp", wraps=create_directory) as staged:
            install_ui(self.target, [self.core])
            result = install_ui(self.target, [self.core], upgrade=True)
        transactions = self.target.parent.parent / "agent-learning-ui-installations"
        self.assertEqual(Path(result["backup"]).parent, transactions)
        self.assertEqual(len(staged.call_args_list), 2)
        for call in staged.call_args_list:
            self.assertEqual(call.kwargs["dir"], transactions)
        self.assertFalse(list(self.target.parent.glob(".alp-staging-*")))
        self.assertFalse(list(self.target.parent.glob(".alp-backup-ui-*")))

    def test_transaction_directory_must_not_overlap_knowledge_home(self):
        home = self.target.parent.parent / "agent-learning-ui-installations"
        self.core_install(home=home)
        with self.assertRaises(ContractError):
            install_ui(self.target, [self.core])
        self.assertFalse(home.exists())
        self.assertFalse(self.target.exists())

    def test_concurrent_destination_change_is_preserved_before_switch(self):
        self.core_install()
        install_ui(self.target, [self.core])
        calls = 0
        def observed_snapshot(path):
            nonlocal calls
            if Path(path) == self.target:
                calls += 1
                if calls == 2:
                    (self.target / "concurrent.txt").write_bytes(b"user change")
            return snapshot(path)
        with patch("agent_learning.ui_install.snapshot", side_effect=observed_snapshot), self.assertRaises(ContractError):
            install_ui(self.target, [self.core], upgrade=True)
        self.assertEqual((self.target / "concurrent.txt").read_bytes(), b"user change")
        self.assertTrue((self.target / MANIFEST).is_file())

    def test_existing_unmanaged_directory_is_preserved(self):
        self.core_install()
        self.target.mkdir(parents=True)
        (self.target / "personal.txt").write_bytes(b"preserve")
        before = snapshot(self.target)
        with self.assertRaises(ContractError):
            install_ui(self.target, [self.core], upgrade=True)
        self.assertEqual(snapshot(self.target), before)

    def test_failed_staging_switch_restores_original_export_and_extras(self):
        self.core_install()
        install_ui(self.target, [self.core])
        extra = self.target / "personal.txt"
        extra.write_bytes(b"keep original")
        empty = self.target / "empty personal directory"
        empty.mkdir()
        before = snapshot(self.target)
        original_rename = Path.rename

        def fail_staging_switch(path, destination):
            if path.name.startswith(".alp-staging-") and Path(destination) == self.target:
                raise OSError("isolated simulated switch failure")
            return original_rename(path, destination)

        with patch.object(Path, "rename", new=fail_staging_switch), self.assertRaises(OSError):
            install_ui(self.target, [self.core], upgrade=True)
        self.assertEqual(snapshot(self.target), before)
        self.assertEqual(extra.read_bytes(), b"keep original")
        self.assertTrue(empty.is_dir())
        self.assertFalse(list(self.target.parent.glob(".alp-staging-*")))
        self.assertFalse(list(self.target.parent.glob(".alp-backup-ui-*")))
        transactions = self.target.parent.parent / "agent-learning-ui-installations"
        self.assertEqual(list(transactions.iterdir()), [])

    def test_requires_explicit_upgrade_and_existing_update_target(self):
        self.core_install()
        for options in ({"upgrade": True}, {"remove": True}, {"upgrade": True, "remove": True}):
            with self.subTest(options=options), self.assertRaises(ContractError):
                install_ui(self.target, [self.core], **options)
        install_ui(self.target, [self.core])
        before = snapshot(self.target)
        with self.assertRaises(ContractError):
            install_ui(self.target, [self.core])
        self.assertEqual(snapshot(self.target), before)

    def test_wrong_host_and_missing_workspace_binding_are_rejected(self):
        wrong = self.root / "codex-core"
        install(wrong, runtime_store=self.store)
        with self.assertRaises(ContractError):
            install_ui(self.target, [wrong])
        self.core_install()
        receipt = read_json(self.core / "runtime-release.json")
        receipt.pop("workspace")
        write_json(self.core / "runtime-release.json", receipt)
        with self.assertRaises(ContractError):
            install_ui(self.target, [self.core])
        self.assertFalse(self.target.exists())

    def test_old_verified_core_without_bridge_support_requires_upgrade(self):
        self.core_install()
        from agent_learning.ui_install import inspect_installation
        def older_release(*args, **kwargs):
            inspected = inspect_installation(*args, **kwargs)
            inspected["release"]["files"].pop("agent_learning/ui_install.py")
            return inspected
        with patch("agent_learning.ui_install.inspect_installation", side_effect=older_release), self.assertRaisesRegex(ContractError, "upgrade the project core"):
            install_ui(self.target, [self.core])
        self.assertFalse(self.target.exists())

    def test_overlap_with_source_workspace_installation_runtime_or_home_is_rejected(self):
        core = self.core_install()
        from agent_learning.install import _resource_root
        paths = [_resource_root() / "new-ui", self.workspace / "ui", self.core / "ui",
                 Path(core["runtime_directory"]) / "ui", self.home / "ui", self.home]
        for target in paths:
            with self.subTest(target=target), self.assertRaises(ContractError):
                install_ui(target, [self.core])
            self.assertFalse(target.exists())
        self.assertFalse(self.home.exists())

    def test_registry_tampering_is_rejected_without_replacing_destination(self):
        self.core_install()
        install_ui(self.target, [self.core])
        registry_path = self.target / REGISTRY
        registry = read_json(registry_path)
        registry["projects"] = []
        write_json(registry_path, registry)
        before = snapshot(self.target)
        with self.assertRaises(ContractError):
            install_ui(self.target, [self.core], upgrade=True)
        self.assertEqual(snapshot(self.target), before)

    def test_manifest_cannot_remove_managed_hashes_to_bypass_integrity(self):
        self.core_install()
        install_ui(self.target, [self.core])
        manifest = read_json(self.target / MANIFEST)
        manifest["files"] = {}
        write_json(self.target / MANIFEST, manifest)
        (self.target / "sidecars/control/main.mjs").write_text("// changed\n", encoding="utf-8")
        before = snapshot(self.target)
        with self.assertRaises(ContractError):
            install_ui(self.target, [self.core], upgrade=True)
        self.assertEqual(snapshot(self.target), before)

    def test_ui_directory_containing_database_is_not_moved_or_copied(self):
        self.core_install()
        install_ui(self.target, [self.core])
        database = self.target / "personal/runtime.sqlite3"
        database.parent.mkdir()
        database.write_bytes(b"isolated fake database")
        before = snapshot(self.target)
        with self.assertRaises(ContractError):
            install_ui(self.target, [self.core], upgrade=True)
        self.assertEqual(snapshot(self.target), before)

    def test_malformed_arguments_are_rejected_before_export(self):
        self.core_install()
        for installations in (None, [], "a path", [self.core] * 129):
            with self.subTest(installations=type(installations)), self.assertRaises(ContractError):
                install_ui(self.target, installations)
        for options in ({"upgrade": "yes"}, {"remove": 1}):
            with self.subTest(options=options), self.assertRaises(ContractError):
                install_ui(self.target, [self.core], **options)
        self.assertFalse(self.target.exists())


class UiInstallCliTests(unittest.TestCase):
    def test_install_ui_cli_delegates_before_runtime_creation(self):
        output = io.StringIO()
        with patch("agent_learning.ui_install.install_ui", return_value={"ui_only": True}) as exported, \
                patch("agent_learning.cli.Runtime", side_effect=AssertionError("Do not create a database")), \
                redirect_stdout(output):
            result = main(["install-ui", "global-ui", "--installation", "project-a", "--installation", "project-b", "--upgrade"])
        self.assertEqual(result, 0)
        exported.assert_called_once_with("global-ui", ["project-a", "project-b"], upgrade=True, remove=False)
        self.assertTrue(json.loads(output.getvalue())["ui_only"])

    def test_install_ui_cli_error_still_opens_no_runtime(self):
        with patch("agent_learning.ui_install.install_ui", side_effect=ContractError("invalid registration")), \
                patch("agent_learning.cli.Runtime", side_effect=AssertionError("Do not create a database")), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(main(["install-ui", "global-ui", "--installation", "project-a"]), 1)


if __name__ == "__main__":
    unittest.main()
