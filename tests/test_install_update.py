import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_learning import install as installer
from agent_learning.install import install
from agent_learning.install_update import (INSTALL_MANIFEST, inspect_installation, installation_lock,
                                           _expected_hook_document, safe_path, snapshot, validate_previous_release)
from agent_learning.runtime import ContractError


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class InstallUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-install-update-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / "plugin export"
        self.store = self.root / "fixed releases"

    def initial(self, mode="enforce"):
        return install(self.target, runtime_store=self.store, hook_mode=mode)

    def make_legacy(self, version="0.11.0", protocol="7"):
        """构造有真实文件清单的旧版本 export，而非修改真实安装。"""
        current = self.initial()
        old = Path(current["runtime_directory"])
        (self.target / INSTALL_MANIFEST).unlink()
        plugin = read_json(old / ".codex-plugin/plugin.json")
        plugin["version"] = version
        write_json(old / ".codex-plugin/plugin.json", plugin)
        for name in ("agent_learning/__init__.py", "agent_learning/release.py"):
            file = old / name
            text = file.read_text(encoding="utf-8")
            if name.endswith("__init__.py"):
                text = text.replace(current["version"], version)
            else:
                from agent_learning.release import PROTOCOL_VERSION
                text = text.replace('PROTOCOL_VERSION = "' + PROTOCOL_VERSION + '"', 'PROTOCOL_VERSION = "' + protocol + '"')
            file.write_text(text, encoding="utf-8")
        with (old / "README.md").open("a", encoding="utf-8") as stream:
            stream.write("\nEarlier exported payload\n")
        files = {path.relative_to(old).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(old.rglob("*")) if path.is_file() and path.name != "release.json"
                 and path.suffix != ".pyc" and "__pycache__" not in path.parts}
        body = {"version": version, "protocol_version": protocol, "files": files}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=True,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()
        body.update(content_digest=digest, release_id=version + "-" + digest)
        write_json(old / "release.json", body)
        release = self.store / body["release_id"]
        old.rename(release)
        for name in files:
            if name not in (".mcp.json", "hooks/hooks.json"):
                shutil.copy2(release / name, self.target / name)
        receipt = read_json(self.target / "runtime-release.json")
        receipt.update({key: body[key] for key in ("version", "protocol_version", "content_digest", "release_id")})
        receipt["runtime_directory"] = str(release)
        write_json(self.target / "runtime-release.json", receipt)
        write_json(self.target / ".mcp.json", {"mcpServers": {"agent_learning": {"command": sys.executable,
                   "args": [str(release / "scripts/learning.py"), "serve", "--role", "worker"]}}})
        hooks = read_json(release / "hooks/hooks.json")
        for event, groups in hooks["hooks"].items():
            command, windows = installer._hook_commands(sys.executable, release / "hooks/dispatch.py", event, "enforce")
            for group in groups:
                for handler in group["hooks"]:
                    handler["command"] = command
                    handler.pop("command_windows", None)
                    handler["commandWindows"] = windows
        write_json(self.target / "hooks/hooks.json", hooks)
        return release, body

    def test_fresh_export_has_managed_file_manifest_and_default_refuses_replacement(self):
        result = self.initial()
        manifest = read_json(self.target / INSTALL_MANIFEST)
        self.assertEqual(manifest["release_id"], result["release_id"])
        self.assertIn("runtime-release.json", manifest["files"])
        observed = snapshot(self.target)
        with self.assertRaisesRegex(ContractError, "already exists"):
            install(self.target, runtime_store=self.store)
        self.assertEqual(snapshot(self.target), observed)

    def test_upgrade_keeps_custom_files_external_data_old_release_and_policy(self):
        first = self.initial()
        personal = self.target / "personal/config.json"
        personal.parent.mkdir()
        personal.write_bytes(b'{"personal": true}')
        empty = self.target / "empty personal directory"
        empty.mkdir()
        external = self.root / "knowledge"
        external.mkdir()
        (external / "config.json").write_bytes(b"user config")
        (external / "runtime.sqlite3").write_bytes(b"user database")
        external_before = snapshot(external)
        old_before = snapshot(Path(first["runtime_directory"]))
        with patch.dict(os.environ, {"ALP_HOME": str(external), "ALP_RUNTIME_STORE": str(self.root / "ignored env store")}):
            result = install(self.target, upgrade=True, expected_release_id=first["release_id"])
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["hook_mode"], "enforce")
        self.assertEqual(Path(result["runtime_directory"]).parent, self.store)
        self.assertEqual(personal.read_bytes(), b'{"personal": true}')
        self.assertTrue(empty.is_dir())
        backup = Path(result["backup_directory"])
        self.assertEqual((backup / "personal/config.json").read_bytes(), personal.read_bytes())
        self.assertEqual(result["preserved_extra_files"], ["personal/config.json"])
        self.assertNotIn("personal/config.json", read_json(self.target / INSTALL_MANIFEST)["files"])
        self.assertEqual(snapshot(external), external_before)
        self.assertEqual(snapshot(Path(first["runtime_directory"])), old_before)
        self.assertFalse((self.root / "ignored env store").exists())
        self.assertTrue(result["host_reload_required"])
        inspect_installation(self.target, installer._hook_commands)
        install(self.target, upgrade=True)
        self.assertNotIn("personal/config.json", read_json(self.target / INSTALL_MANIFEST)["files"])
        self.assertEqual(personal.read_bytes(), b'{"personal": true}')

    def test_same_version_different_digest_can_upgrade(self):
        from agent_learning import __version__
        from agent_learning.release import PROTOCOL_VERSION
        old, manifest = self.make_legacy(version=__version__, protocol=PROTOCOL_VERSION)
        result = install(self.target, upgrade=True)
        self.assertEqual(result["version"], __version__)
        self.assertNotEqual(result["release_id"], manifest["release_id"])
        self.assertTrue(old.is_dir())

    def test_downgrade_is_rejected(self):
        self.make_legacy(version="99.0.0")
        before = snapshot(self.target)
        with self.assertRaisesRegex(ContractError, "downgrade"):
            install(self.target, upgrade=True)
        self.assertEqual(snapshot(self.target), before)

    def test_future_or_zero_protocol_is_rejected(self):
        release, _ = self.make_legacy(protocol="999")
        with self.assertRaisesRegex(ContractError, "manifest is invalid"):
            validate_previous_release(release)

    def test_old_release_malformed_manifest_fields_fail_as_contract_errors(self):
        first = self.initial()
        release = Path(first["runtime_directory"])
        manifest_file = release / "release.json"
        original = read_json(manifest_file)
        corruptions = [
            {"version": "9" * 5000 + ".0.0"},
            {"version": ["0.11.0"]},
            {"version": "00.11.0"},
            {"protocol_version": "0008"},
            {"protocol_version": "8" * 5000},
            {"protocol_version": "0"},
            {"protocol_version": []},
            {"files": []},
            {"files": None},
            {"release_id": {}},
            {"content_digest": []},
        ]
        for corruption in corruptions:
            with self.subTest(corruption_field=next(iter(corruption))):
                value = dict(original, **corruption)
                write_json(manifest_file, value)
                with self.assertRaises(ContractError):
                    validate_previous_release(release)
        write_json(manifest_file, [original])
        with self.assertRaises(ContractError):
            validate_previous_release(release)
        write_json(manifest_file, original)

    def test_receipt_wrong_field_types_are_rejected_without_mutation(self):
        self.initial()
        receipt_file = self.target / "runtime-release.json"
        original = read_json(receipt_file)
        corruptions = [{"hook_mode": []}, {"hook_mode": {}}, {"runtime_directory": []},
                       {"python": None}, {"release_id": []}, {"protocol_version": {}},
                       {"runtime_directory": str(self.root / "null\0byte")},
                       {"python": "python"}]
        for corruption in corruptions:
            with self.subTest(corruption_field=next(iter(corruption))):
                write_json(receipt_file, dict(original, **corruption))
                observed = snapshot(self.target)
                with self.assertRaises(ContractError):
                    install(self.target, upgrade=True)
                self.assertEqual(snapshot(self.target), observed)
                self.assertFalse(list(self.root.glob(".alp-backup-*")))
        write_json(receipt_file, original)

    def test_old_hook_structure_type_errors_are_contract_errors(self):
        runtime = self.root / "malformed legacy hooks"
        (runtime / "hooks").mkdir(parents=True)
        documents = [[], {}, {"hooks": []}, {"hooks": {}}, {"hooks": {"SessionStart": 0}},
                     {"hooks": {"SessionStart": [{}]}},
                     {"hooks": {"SessionStart": [{"hooks": 0}]}},
                     {"hooks": {"SessionStart": [{"hooks": ["wrong handler"]}]}},
                     {"hooks": {"SessionStart": [{"hooks": [{"type": "prompt"}]}]}}]
        for index, document in enumerate(documents):
            with self.subTest(document=index):
                write_json(runtime / "hooks/hooks.json", document)
                with self.assertRaises(ContractError):
                    _expected_hook_document(runtime, sys.executable, "enforce", installer._hook_commands)

    def test_existing_fixed_release_junction_is_rejected_without_following_it(self):
        first = self.initial()
        release = Path(first["runtime_directory"])
        before = snapshot(release)
        target = self.root / "second export"
        original = Path.lstat
        def marked_junction(path):
            information = original(path)
            if path == release:
                return SimpleNamespace(st_mode=information.st_mode, st_file_attributes=0x400)
            return information
        with patch.object(Path, "lstat", new=marked_junction):
            with self.assertRaisesRegex(ContractError, "links or junctions"):
                install(target, runtime_store=self.store)
        self.assertFalse(target.exists())
        self.assertEqual(snapshot(release), before)
        self.assertFalse(list(self.store.glob(".alp-staging-*")))

    def test_live_knowledge_database_inside_export_is_rejected(self):
        self.initial()
        data = self.target / "data"
        data.mkdir()
        database = data / "runtime.sqlite3"
        database.write_bytes(b"live database")
        (data / "runtime.sqlite3-wal").write_bytes(b"live WAL")
        with self.assertRaisesRegex(ContractError, "knowledge database"):
            install(self.target, upgrade=True)
        self.assertEqual(database.read_bytes(), b"live database")
        self.assertFalse(list(self.root.glob(".alp-backup-*")))

    def test_legacy_version_and_protocol_are_accepted_and_entries_switch_together(self):
        old, manifest = self.make_legacy()
        old_snapshot = snapshot(old)
        inspected = inspect_installation(self.target, installer._hook_commands)
        self.assertEqual(inspected["release"]["protocol_version"], "7")
        result = install(self.target, upgrade=True, expected_release_id=manifest["release_id"])
        self.assertEqual(result["previous_release_id"], manifest["release_id"])
        runtime = Path(result["runtime_directory"])
        self.assertNotEqual(runtime, old)
        self.assertEqual(snapshot(old), old_snapshot)
        mcp = read_json(self.target / ".mcp.json")["mcpServers"]["agent_learning"]
        self.assertEqual(Path(mcp["args"][0]).parents[1], runtime)
        hooks = read_json(self.target / "hooks/hooks.json")
        for event, groups in hooks["hooks"].items():
            command, windows = installer._hook_commands(sys.executable, runtime / "hooks/dispatch.py", event, "enforce")
            for group in groups:
                for handler in group["hooks"]:
                    self.assertEqual(handler["command"], command)
                    self.assertEqual(handler["commandWindows"], windows)
        self.assertTrue((self.target / INSTALL_MANIFEST).is_file())

    def test_non_plugin_missing_or_source_destination_is_rejected(self):
        with self.assertRaisesRegex(ContractError, "existing plugin"):
            install(self.target, runtime_store=self.store, upgrade=True)
        self.target.mkdir()
        sentinel = self.target / "user data"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(ContractError):
            install(self.target, runtime_store=self.store, upgrade=True)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertFalse(self.store.exists())
        sentinel.unlink()
        self.target.rmdir()
        self.initial()
        (self.target / "pyproject.toml").write_text("source marker", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "source checkout"):
            install(self.target, upgrade=True)

    def test_modified_managed_file_and_modified_fixed_release_are_rejected(self):
        first = self.initial()
        file = self.target / "README.md"
        original = file.read_bytes()
        file.write_bytes(b"customized managed file")
        with self.assertRaisesRegex(ContractError, "managed file"):
            install(self.target, upgrade=True)
        self.assertEqual(file.read_bytes(), b"customized managed file")
        file.write_bytes(original)
        (Path(first["runtime_directory"]) / "README.md").write_bytes(b"broken runtime")
        with self.assertRaisesRegex(ContractError, "fixed runtime release"):
            install(self.target, upgrade=True)
        self.assertFalse(list(self.root.glob(".alp-backup-*")))

    def test_modified_host_configuration_and_manifest_are_rejected(self):
        self.initial()
        mcp_file = self.target / ".mcp.json"
        original = mcp_file.read_bytes()
        value = read_json(mcp_file)
        value["mcpServers"]["agent_learning"]["args"].append("--hostile")
        write_json(mcp_file, value)
        with self.assertRaisesRegex(ContractError, "MCP configuration"):
            install(self.target, upgrade=True)
        mcp_file.write_bytes(original)
        manifest = read_json(self.target / INSTALL_MANIFEST)
        manifest["files"]["README.md"] = "0" * 64
        write_json(self.target / INSTALL_MANIFEST, manifest)
        with self.assertRaisesRegex(ContractError, "file manifest"):
            install(self.target, upgrade=True)

    def test_expected_identity_mismatch_is_read_only(self):
        self.initial()
        before = snapshot(self.target)
        with self.assertRaisesRegex(ContractError, "expected release identity"):
            install(self.target, upgrade=True, expected_release_id="0.0.1-" + "0" * 64)
        self.assertEqual(snapshot(self.target), before)
        self.assertFalse(list(self.root.glob(".alp-backup-*")))

    def test_new_managed_path_conflicting_with_old_extra_is_rejected(self):
        self.initial()
        extra = self.target / "agent_learning/future_module.py"
        extra.write_bytes(b"user code")
        original = installer._copy_payload
        count = 0
        def new_managed(source, package, staging):
            nonlocal count
            count += 1
            original(source, package, staging)
            if count == 2:
                (staging / "agent_learning/future_module.py").write_bytes(b"new plugin code")
        with patch.object(installer, "_copy_payload", side_effect=new_managed):
            with self.assertRaisesRegex(ContractError, "conflicts"):
                install(self.target, upgrade=True)
        self.assertEqual(extra.read_bytes(), b"user code")
        self.assertFalse(list(self.root.glob(".alp-backup-*")))

    def test_preparation_failure_preserves_target(self):
        self.initial()
        before = snapshot(self.target)
        original = installer._copy_payload
        count = 0
        def interrupted(source, package, staging):
            nonlocal count
            count += 1
            original(source, package, staging)
            if count == 2:
                raise OSError("copy failure")
        with patch.object(installer, "_copy_payload", side_effect=interrupted):
            with self.assertRaises(ContractError):
                install(self.target, upgrade=True)
        self.assertEqual(snapshot(self.target), before)
        self.assertFalse(list(self.root.glob(".alp-staging-*")))
        self.assertFalse(list(self.root.glob(".alp-install-*.lock")))

    def test_switch_failure_restores_old_target(self):
        self.initial()
        before = snapshot(self.target)
        original = Path.rename
        def interrupted(path, destination):
            if path.name.startswith(".alp-staging-") and path.parent == self.root:
                raise OSError("Windows files in use")
            return original(path, destination)
        with patch.object(Path, "rename", new=interrupted):
            with self.assertRaisesRegex(ContractError, "previous installation"):
                install(self.target, upgrade=True)
        self.assertEqual(snapshot(self.target), before)
        self.assertFalse(list(self.root.glob(".alp-backup-*")))
        self.assertFalse(list(self.root.glob(".alp-staging-*")))

    def test_destination_change_during_preparation_is_preserved(self):
        self.initial()
        original = installer._copy_payload
        count = 0
        def concurrent(source, package, staging):
            nonlocal count
            count += 1
            original(source, package, staging)
            if count == 2:
                (self.target / "concurrent-user-file").write_bytes(b"external change")
        with patch.object(installer, "_copy_payload", side_effect=concurrent):
            with self.assertRaisesRegex(ContractError, "changed during preparation"):
                install(self.target, upgrade=True)
        self.assertEqual((self.target / "concurrent-user-file").read_bytes(), b"external change")
        self.assertFalse(list(self.root.glob(".alp-backup-*")))

    def test_destination_change_between_check_and_rename_is_restored(self):
        self.initial()
        original = Path.rename
        def concurrent(path, destination):
            result = original(path, destination)
            if path == self.target:
                (Path(destination) / "concurrent-user-file").write_bytes(b"external change")
            return result
        with patch.object(Path, "rename", new=concurrent):
            with self.assertRaisesRegex(ContractError, "changed during switching"):
                install(self.target, upgrade=True)
        self.assertEqual((self.target / "concurrent-user-file").read_bytes(), b"external change")
        self.assertFalse(list(self.root.glob(".alp-backup-*")))

    def test_interrupted_switch_does_not_overwrite_new_destination(self):
        self.initial()
        original = Path.rename
        def concurrent(path, destination):
            result = original(path, destination)
            if path == self.target:
                self.target.mkdir()
                (self.target / "other-owner").write_bytes(b"keep external owner")
            return result
        with patch.object(Path, "rename", new=concurrent):
            with self.assertRaisesRegex(ContractError, "backup were preserved"):
                install(self.target, upgrade=True)
        self.assertEqual((self.target / "other-owner").read_bytes(), b"keep external owner")
        backups = list(self.root.glob(".alp-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "runtime-release.json").is_file())

    def test_concurrent_upgrade_lock_does_not_replace_target(self):
        self.initial()
        before = snapshot(self.target)
        with installation_lock(self.target):
            with self.assertRaisesRegex(ContractError, "another installation"):
                install(self.target, upgrade=True)
        self.assertEqual(snapshot(self.target), before)
        self.assertFalse(list(self.root.glob(".alp-install-*.lock")))

    def test_explicit_hook_policy_and_runtime_store_can_change(self):
        self.initial()
        selected_store = self.root / "other store"
        result = install(self.target, upgrade=True, runtime_store=selected_store, hook_mode="observe")
        self.assertEqual(result["hook_mode"], "observe")
        self.assertEqual(Path(result["runtime_directory"]).parent, selected_store)

    def test_symlink_and_parent_traversal_are_rejected(self):
        with self.assertRaisesRegex(ContractError, "parent traversal"):
            safe_path(self.root / "nested/../export")
        self.initial()
        link = self.root / "alias"
        try:
            link.symlink_to(self.target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("creating a directory symlink is unavailable")
        with self.assertRaisesRegex(ContractError, "links or junctions"):
            install(link, upgrade=True)
        inner = self.target / "linked file"
        inner.symlink_to(self.target / "README.md")
        with self.assertRaisesRegex(ContractError, "regular files"):
            install(self.target, upgrade=True)


if __name__ == "__main__":
    unittest.main()
