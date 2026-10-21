import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid

from agent_learning import install as installer
from agent_learning.install import install
from agent_learning.install_update import INSTALL_MANIFEST, snapshot
from agent_learning.installation_backups import backup_preview, backup_quick_preview, backup_root, migrate_backups
from agent_learning.runtime import ContractError
from agent_learning.release import filesystem_path


class InstallMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="alp-maintenance-")
        # The backup archive adds legitimate directory levels. Python 3.10's
        # TemporaryDirectory cleanup does not enable Windows long paths.
        def cleanup():
            shutil.rmtree(filesystem_path(self.root), ignore_errors=True)
            self.tmp.cleanup()
        self.addCleanup(cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.target = self.workspace / ".agents/plugins/agent-learning"
        self.store = self.root / "runtime-releases"
        self.home = self.root / "home"

    def initial(self, host="codex", **kwargs):
        binding = {"workspace": self.workspace, "home": self.home} if host != "codex" else {}
        return install(self.target, runtime_store=self.store, host=host, **binding, **kwargs)

    def legacy(self):
        old = self.target.parent / (".alp-backup-agent-learning-" + uuid.uuid4().hex)
        shutil.copytree(self.target, old)
        return old

    def test_identical_three_host_installs_preserve_extras_and_all_target_mtimes(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                if self.target.exists():
                    shutil.rmtree(self.target)
                self.initial(host)
                extra = self.target / "personal/notes.txt"
                extra.parent.mkdir()
                extra.write_bytes(b"private notes")
                (self.target / "empty-personal").mkdir()
                before = snapshot(self.target)
                mtimes = {p.relative_to(self.target).as_posix(): p.stat().st_mtime_ns
                          for p in [self.target, *self.target.rglob("*")]}
                result = install(self.target, upgrade=True, host=host)
                self.assertEqual(result["status"], "unchanged")
                self.assertNotIn("backup_directory", result)
                self.assertFalse(result["host_reload_required"])
                self.assertFalse(result["hook_review_required"])
                self.assertEqual(result["preserved_extra_files"], ["personal/notes.txt"])
                self.assertEqual(snapshot(self.target), before)
                self.assertEqual({p.relative_to(self.target).as_posix(): p.stat().st_mtime_ns
                                  for p in [self.target, *self.target.rglob("*")]}, mtimes)
                self.assertFalse(backup_root(self.target).exists())

    def test_policy_change_creates_recoverable_backup_outside_discovery(self):
        self.initial(hook_mode="observe")
        (self.target / "user-notes.txt").write_bytes(b"preserved user file")
        before = snapshot(self.target)
        result = install(self.target, upgrade=True, hook_mode="enforce")
        self.assertEqual(result["status"], "updated")
        backup = Path(result["backup_directory"])
        self.assertEqual(backup.parent, backup_root(self.target))
        self.assertNotIn(self.target.parent, backup.parents)
        self.assertEqual(snapshot(backup), before)
        plan = backup_preview(self.target)
        self.assertEqual(len(plan["entries"]), 1)
        self.assertIn("user_extra_files", plan["entries"][0]["blockers"])
        self.assertFalse(plan["entries"][0]["delete_allowed"])

    def test_interpreter_and_runtime_paths_are_compared_instead_of_version(self):
        self.initial()
        changed = self.root / "independent-python.exe"
        with patch("agent_learning.installation_health.select_python", return_value=(str(changed), {})):
            result = install(self.target, upgrade=True)
        self.assertEqual(result["status"], "updated")
        self.assertTrue(Path(result["backup_directory"]).is_dir())
        self.assertEqual(result["python"], str(changed))

    def test_identical_upgrade_rechecks_runtime_before_returning_unchanged(self):
        initial = self.initial()
        before = snapshot(self.target)
        copy = installer._copy_payload
        calls = 0
        def concurrent(source, package, staging):
            nonlocal calls
            calls += 1
            copy(source, package, staging)
            if calls == 2:
                (Path(initial["runtime_directory"]) / "README.md").write_text("concurrent change", encoding="utf-8")
        with patch.object(installer, "_copy_payload", side_effect=concurrent), self.assertRaises(ContractError):
            install(self.target, upgrade=True)
        self.assertEqual(snapshot(self.target), before)
        self.assertFalse(backup_root(self.target).exists())

    def test_dsh_additional_binding_updates_once_then_is_unchanged(self):
        self.initial("dsh")
        second = self.root / "second-project"
        second.mkdir()
        updated = install(self.target, upgrade=True, host="dsh", workspace=second)
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(set(updated["workspaces"]), {str(self.workspace), str(second)})
        self.assertEqual(install(self.target, upgrade=True, host="dsh")["status"], "unchanged")

    def test_complete_legacy_backup_migrates_without_touching_active_install(self):
        self.initial(panel_workspace=self.workspace)
        old = self.legacy()
        before, active = snapshot(old), snapshot(self.target)
        plan = backup_preview(self.target)
        self.assertTrue(plan["entries"][0]["migration_allowed"])
        result = migrate_backups(self.target, expected_plan_id=plan["plan_id"], confirm=True)
        self.assertEqual(result["status"], "migrated")
        self.assertEqual(result["deleted"], 0)
        moved = Path(result["moved"][0]["backup_directory"])
        self.assertFalse(old.exists())
        self.assertEqual(snapshot(moved), before)
        self.assertEqual(snapshot(self.target), active)
        self.assertEqual(backup_preview(self.target)["entries"][0]["blockers"], ["retained_recoverable_release"])

    def test_unknown_corrupt_and_no_manifest_backups_are_not_migrated(self):
        self.initial()
        missing = self.legacy()
        (missing / INSTALL_MANIFEST).unlink()
        corrupt = self.legacy()
        (corrupt / "runtime-release.json").write_text("{}", encoding="utf-8")
        unknown = self.target.parent / ".alp-backup-agent-learning-unknown"
        unknown.mkdir()
        before = snapshot(self.target.parent)
        plan = backup_preview(self.target)
        self.assertEqual(len(plan["entries"]), 3)
        self.assertTrue(all(not item["migration_allowed"] and item["blockers"] for item in plan["entries"]))
        result = migrate_backups(self.target, expected_plan_id=plan["plan_id"], confirm=True)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(snapshot(self.target.parent), before)

    def test_extra_files_block_cleanup_but_are_preserved_by_migration(self):
        self.initial("antigravity")
        old = self.legacy()
        (old / "user.txt").write_bytes(b"keep")
        plan = backup_preview(self.target)
        entry = plan["entries"][0]
        self.assertTrue(entry["migration_allowed"])
        self.assertIn("user_extra_files", entry["blockers"])
        self.assertFalse(entry["cleanup_candidate"])
        result = migrate_backups(self.target, expected_plan_id=plan["plan_id"], confirm=True)
        self.assertEqual((Path(result["moved"][0]["backup_directory"]) / "user.txt").read_bytes(), b"keep")

    def test_foreign_workspace_backup_is_not_adopted(self):
        self.initial("antigravity")
        foreign = self.root / "foreign"
        foreign.mkdir()
        old = self.target.parent / (".alp-backup-agent-learning-" + uuid.uuid4().hex)
        install(old, runtime_store=self.store, host="antigravity", workspace=foreign, home=self.home)
        entry = backup_preview(self.target)["entries"][0]
        self.assertFalse(entry["migration_allowed"])
        self.assertIn("binding differs", entry["blockers"][0])

    def test_changed_preview_is_rejected_without_moving_backups(self):
        self.initial()
        old = self.legacy()
        plan = backup_preview(self.target)
        (old / "new-user-file").write_bytes(b"changed after preview")
        with self.assertRaisesRegex(ContractError, "preview changed"):
            migrate_backups(self.target, expected_plan_id=plan["plan_id"], confirm=True)
        self.assertTrue(old.is_dir())

    def test_migration_requires_explicit_confirmation(self):
        self.initial()
        self.legacy()
        plan = backup_preview(self.target)
        with self.assertRaisesRegex(ContractError, "confirmation"):
            migrate_backups(self.target, expected_plan_id=plan["plan_id"])

    def test_failed_second_migration_rolls_back_first(self):
        self.initial()
        old = sorted([self.legacy(), self.legacy()])
        before = [snapshot(item) for item in old]
        plan = backup_preview(self.target)
        rename = Path.rename
        def fail_second(path, destination):
            if path == old[1]:
                raise OSError("fixture files in use")
            return rename(path, destination)
        with patch.object(Path, "rename", new=fail_second), self.assertRaisesRegex(ContractError, "were restored"):
            migrate_backups(self.target, expected_plan_id=plan["plan_id"], confirm=True)
        self.assertEqual([snapshot(item) for item in old], before)
        self.assertFalse(list(backup_root(self.target).iterdir()))

    def test_tampered_external_descriptor_blocks_cleanup(self):
        self.initial(hook_mode="observe")
        updated = install(self.target, upgrade=True, hook_mode="enforce")
        backup = Path(updated["backup_directory"])
        descriptor = backup.with_name(backup.name + ".json")
        record = json.loads(descriptor.read_text(encoding="utf-8"))
        record["installation"] = str(self.root / "other-plugin")
        descriptor.write_text(json.dumps(record), encoding="utf-8")
        entry = backup_preview(self.target)["entries"][0]
        self.assertFalse(entry["cleanup_candidate"])
        self.assertIn("identity", entry["blockers"][0])

    def test_symlink_backup_is_refused_without_traversal(self):
        self.initial()
        old = self.target.parent / (".alp-backup-agent-learning-" + uuid.uuid4().hex)
        try:
            old.symlink_to(self.target, target_is_directory=True)
        except OSError:
            self.skipTest("symbolic links unavailable")
        entry = backup_preview(self.target)["entries"][0]
        self.assertFalse(entry["migration_allowed"])
        self.assertFalse(entry["cleanup_candidate"])

    def test_large_enumeration_deep_checks_only_visible_entries(self):
        self.initial()
        for index in range(500):
            (self.target.parent / (".alp-backup-agent-learning-" + format(index, "032x"))).mkdir()
        with patch("agent_learning.installation_backups._inspect_backup", return_value={
                "directory": "fixture", "migration_allowed": False, "blockers": ["unverified"]}) as inspect:
            plan = backup_preview(self.target, limit=20)
        self.assertEqual(inspect.call_count, 20)
        self.assertEqual(len(plan["entries"]), 20)
        self.assertTrue(plan["truncated"])
        self.assertEqual(plan["retention_scope"], "visible_entries_only")
        self.assertTrue(all("snapshot" not in entry for entry in plan["entries"]))

    def test_quick_inventory_never_hashes_or_authorizes_backup_migration(self):
        self.initial()
        self.legacy()
        with patch("agent_learning.installation_backups.inspect_installation", side_effect=AssertionError("no hashing")):
            plan = backup_quick_preview(self.target)
        self.assertEqual(plan["verification"], "metadata_only")
        self.assertNotIn("plan_id", plan)
        self.assertEqual(len(plan["entries"]), 1)
        self.assertFalse(plan["entries"][0]["migration_allowed"])
        self.assertFalse(plan["entries"][0]["cleanup_candidate"])


if __name__ == "__main__":
    unittest.main()
