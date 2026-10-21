"""Panel backup inventory is bounded, optional and never a migration grant."""
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_learning.cli import main
from agent_learning.installation_maintenance import panel_backup_preview
from agent_learning.runtime import ContractError


class InstallationMaintenanceTests(unittest.TestCase):
    def test_panel_inventory_is_bounded_unverified_and_does_not_expose_authority(self):
        item = {"directory": str(Path("backup-root") / "old-release"), "version": "1.0.0",
                "location": "legacy", "retain": False, "migration_allowed": False}
        with patch("agent_learning.installation_backups.backup_quick_preview", return_value={
                "entries": [item], "backup_root": "outside-discovery", "truncated": True}) as preview:
            result = panel_backup_preview("verified-installation")
        preview.assert_called_once_with("verified-installation", limit=20)
        self.assertEqual(result["status"], "preview")
        self.assertEqual(result["legacy_count"], 1)
        self.assertIsNone(result["migration_count"])
        self.assertFalse(result["verified"])
        self.assertFalse(result["retention_evaluated"])
        self.assertTrue(result["truncated"])
        self.assertFalse(result["deletion_supported"])
        self.assertNotIn("plan_id", result)
        self.assertTrue(result["entries"][0]["needs_review"])

    def test_optional_inventory_failure_does_not_hide_panel_health(self):
        with patch("agent_learning.installation_backups.backup_quick_preview", side_effect=ContractError("bad archive")):
            self.assertEqual(panel_backup_preview("verified"), {
                "status": "unavailable", "reason": "backup_preview_unavailable"})

    def test_cli_preview_stays_outside_runtime_and_does_not_mutate(self):
        with patch("agent_learning.cli.Runtime", side_effect=AssertionError("must not open knowledge")), \
                patch("agent_learning.installation_backups.backup_preview", return_value={"status": "preview"}) as preview, \
                patch("builtins.print"):
            self.assertEqual(main(["installation-backups", "--installation", "fixture", "--limit", "20"]), 0)
        preview.assert_called_once_with("fixture", keep=2, limit=20)

    def test_cli_relocation_keeps_explicit_confirmation_plan_and_batch_limit(self):
        with patch("agent_learning.cli.Runtime", side_effect=AssertionError("must not open knowledge")), \
                patch("agent_learning.installation_backups.migrate_backups", return_value={"status": "migrated"}) as migrate, \
                patch("builtins.print"):
            self.assertEqual(main(["relocate-installation-backups", "--installation", "fixture",
                                   "--plan-id", "f" * 64, "--limit", "20", "--confirm"]), 0)
        migrate.assert_called_once_with("fixture", keep=2, limit=20, expected_plan_id="f" * 64, confirm=True)
