"""Bounded, read-only backup information for already verified project panels."""
from __future__ import annotations

from pathlib import Path


def panel_backup_preview(installation):
    from .installation_backups import backup_quick_preview
    from .runtime import ContractError

    try:
        plan = backup_quick_preview(installation, limit=20)
        entries = plan["entries"]
        selected = []
        for item in entries[:20]:
            selected.append({"name": Path(item["directory"]).name[:140],
                "version": item.get("version"), "location": item["location"],
                "retained": bool(item.get("retain")),
                "migration_allowed": bool(item.get("migration_allowed")),
                "needs_review": True})
        return {"status": "preview", "count": len(entries), "retention_evaluated": False,
            "legacy_count": sum(item["location"] == "legacy" for item in entries),
            "migration_count": None, "verified": False,
            "backup_root": str(plan["backup_root"]), "entries": selected,
            "truncated": bool(plan.get("truncated")) or len(entries) > len(selected),
            "scan_limited": bool(plan.get("scan_limited")), "deletion_supported": False}
    except (ContractError, OSError, ValueError, TypeError, KeyError):
        # Maintenance is optional; a broken old backup must not hide controls
        # for a healthy current installation or start any repair automatically.
        return {"status": "unavailable", "reason": "backup_preview_unavailable"}
