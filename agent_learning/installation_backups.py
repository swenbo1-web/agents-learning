"""只读备份计划与可逆迁移；不删除安装、运行副本或知识数据。"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .install_update import (INSTALL_MANIFEST, _canonical, _json, _linked,
                            TARGET_HOSTS, inspect_installation, installation_lock, safe_path, snapshot)
from .runtime import ContractError
from .release import filesystem_path


def backup_root(destination):
    target = safe_path(destination)
    key = hashlib.sha256(os.path.normcase(str(target)).encode("utf-8")).hexdigest()[:24]
    root = safe_path(target.parent.parent / "agent-learning-installations" / key / "backups")
    if root == target.parent or target.parent in root.parents or root.anchor != target.anchor:
        raise ContractError("backup directory must stay outside the plugin discovery directory on the same volume")
    return root


def _record_path(backup):
    return backup.with_name(backup.name + ".json")


def _record(previous, backup, *, original=None):
    return {"schema": 1, "plugin": "agent-learning", "installation": str(previous["target"]),
            "target_host": previous["target_host"], "release_id": previous["release"]["release_id"],
            "backup_directory": str(backup), "original_directory": str(original) if original else None,
            "snapshot": previous["snapshot"], "created": datetime.now(timezone.utc).isoformat()}


def _write_record(backup, record):
    with _record_path(backup).open("x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def allocate_backup(previous):
    """Reserve a same-volume external location before the reversible switch."""
    root = backup_root(previous["target"])
    root.mkdir(parents=True, exist_ok=True)
    root = safe_path(root)
    if root.stat().st_dev != previous["target"].stat().st_dev:
        raise ContractError("installation backup must use the same filesystem volume")
    # Keep names short: adding a long timestamp to deep workspace paths can
    # break Windows consumers that do not use the extended-path API.
    backup = root / uuid.uuid4().hex[:16]
    if backup.exists() or _linked(backup):
        raise ContractError("installation backup destination already exists")
    _write_record(backup, _record(previous, backup))
    return backup


def remove_backup_record(backup):
    """Only remove our descriptor after a successful rollback, never backup data."""
    descriptor = _record_path(backup)
    try:
        if not _linked(descriptor) and _json(descriptor).get("backup_directory") == str(backup):
            descriptor.unlink()
    except (OSError, ContractError):
        pass


def _bindings(receipt):
    workspaces = receipt.get("workspaces") or ([receipt["workspace"]] if receipt.get("workspace") else [])
    return {"home": receipt.get("home"), "workspaces": sorted(set(workspaces)),
            "panel_workspace": receipt.get("panel_workspace")}


def _candidates(target, root, limit):
    prefix = ".alp-backup-" + target.name[:40] + "-"
    result, truncated, scan_limited, scanned = [], False, False, 0
    for directory, legacy in ((target.parent, True), (root, False)):
        if not directory.exists():
            continue
        with os.scandir(directory) as paths:
            for path in paths:
                scanned += 1
                if scanned > 4096:
                    truncated = scan_limited = True
                    break
                matching = path.name.startswith(prefix) if legacy else not path.name.endswith(".json")
                if not matching:
                    continue
                if len(result) >= limit:
                    truncated = True
                    break
                result.append((Path(path.path), legacy))
        if truncated:
            break
    result.sort(key=lambda item: (not item[1], item[0].name))
    return result, truncated, scan_limited, min(scanned, 4096)


def _metadata_receipt(directory):
    file = safe_path(directory / "runtime-release.json")
    if not file.is_file() or file.stat().st_size > 65536:
        raise ContractError("backup receipt is missing or exceeds the metadata limit")
    receipt = _json(file)
    if (receipt.get("target_host", "codex") not in TARGET_HOSTS
            or not isinstance(receipt.get("version"), str) or len(receipt["version"]) > 32
            or not isinstance(receipt.get("release_id"), str) or len(receipt["release_id"]) > 128):
        raise ContractError("backup receipt metadata is invalid")
    return receipt


def backup_quick_preview(destination, *, limit=20):
    """UI inventory only: no release hashing, authority or migration plan."""
    if type(limit) is not int or not 1 <= limit <= 256:
        raise ContractError("backup preview limit must be an integer from 1 to 256")
    target = safe_path(destination)
    if not target.is_dir():
        raise ContractError("installation destination must be an existing directory")
    receipt = _metadata_receipt(target)
    root = backup_root(target)
    candidates, truncated, scan_limited, scanned = _candidates(target, root, limit)
    entries = []
    for path, legacy in candidates:
        item = {"directory": str(path), "location": "legacy" if legacy else "external",
                "verification": "metadata_only", "migration_allowed": False,
                "cleanup_candidate": False, "delete_allowed": False, "retain": False,
                "blockers": ["not_deeply_verified"]}
        try:
            safe_path(path)
            saved = _metadata_receipt(path)
            item.update(version=saved["version"], release_id=saved["release_id"],
                        target_host=saved.get("target_host", "codex"))
        except (OSError, UnicodeError, ValueError, ContractError) as error:
            item["blockers"].append(str(error))
        entries.append(item)
    return {"status": "metadata_preview", "verification": "metadata_only",
            "installation": str(target), "target_host": receipt.get("target_host", "codex"),
            "backup_root": str(root), "entries": entries, "limit": limit,
            "truncated": truncated, "scan_limited": scan_limited, "scanned_entries": scanned,
            "retention_scope": "not_evaluated", "models_started": False,
            "deletion_supported": False, "next_step": "Run a full backup preview before any relocation; this inventory does not verify ownership, managed content, references or recoverability."}


def _inspect_backup(path, current, legacy):
    from .install import _hook_commands
    entry = {"directory": str(path), "location": "legacy" if legacy else "external",
             "migration_allowed": False, "cleanup_candidate": False, "delete_allowed": False,
             "blockers": []}
    try:
        safe_path(path)
        if not path.is_dir() or _linked(path):
            raise ContractError("backup is not a regular directory")
        info = inspect_installation(path, _hook_commands, target_host=current["target_host"],
                                    installation_target=current["target"])
        if INSTALL_MANIFEST not in info["managed"]:
            raise ContractError("backup has no complete installation manifest")
        if _bindings(info["receipt"]) != _bindings(current["receipt"]):
            raise ContractError("backup project or knowledge binding differs from the active installation")
        if not legacy:
            descriptor = _record_path(path)
            if _linked(descriptor):
                raise ContractError("backup descriptor must not be a link")
            record = _json(descriptor)
            if (record.get("schema") != 1 or record.get("plugin") != "agent-learning"
                    or record.get("installation") != str(current["target"])
                    or record.get("target_host") != current["target_host"]
                    or record.get("backup_directory") != str(path)
                    or record.get("release_id") != info["release"]["release_id"]
                    or record.get("snapshot") != info["snapshot"]):
                raise ContractError("backup descriptor identity or snapshot has changed")
        entry.update(release_id=info["release"]["release_id"], version=info["release"]["version"],
                     target_host=info["target_host"], files=len(info["snapshot"]["files"]),
                     bytes=sum(filesystem_path(path / name).stat().st_size for name in info["snapshot"]["files"]),
                     extra_files=info["extra_files"], snapshot=info["snapshot"],
                     modified_ns=path.stat().st_mtime_ns, migration_allowed=legacy)
        if info["extra_files"]:
            entry["blockers"].append("user_extra_files")
        # Only the active installation's generated entry points can be checked
        # locally. A preview never claims that arbitrary host config is unused.
        needle = os.path.normcase(str(path)).replace("\\", "/")
        for name in ("runtime-release.json", ".mcp.json", "mcp_config.json", "hooks.json", "hooks/hooks.json"):
            file = current["target"] / name
            if file.is_file():
                value = os.path.normcase(file.read_text(encoding="utf-8")).replace("\\\\", "/").replace("\\", "/")
                if needle in value:
                    entry["blockers"].append("referenced_by_active_installation")
                    entry["migration_allowed"] = False
                    break
        entry["cleanup_candidate"] = not entry["blockers"]
    except (OSError, UnicodeError, ContractError, TypeError, ValueError) as error:
        entry["migration_allowed"] = entry["cleanup_candidate"] = False
        entry["blockers"].append(str(error))
    return entry


def _backup_preview(destination, *, keep=2, limit=128):
    """Preview relocation and human cleanup review; never deletes anything."""
    from .install import _hook_commands
    if type(keep) is not int or not 1 <= keep <= 20:
        raise ContractError("backup retention must be an integer from 1 to 20")
    if type(limit) is not int or not 1 <= limit <= 256:
        raise ContractError("backup preview limit must be an integer from 1 to 256")
    target = safe_path(destination)
    current = inspect_installation(target, _hook_commands)
    root = backup_root(target)
    prefix = ".alp-backup-" + target.name[:40] + "-"
    legacy_name = re.compile(re.escape(prefix) + r"[0-9a-f]{32}$")
    # Legacy entries precede archives so confirmed relocation makes progress.
    candidates, truncated, scan_limited, scanned = _candidates(target, root, limit)
    entries = []
    for path, legacy in candidates:
        if legacy and (len(target.name) > 40 or not legacy_name.fullmatch(path.name)):
            entries.append({"directory": str(path), "location": "legacy", "migration_allowed": False,
                            "cleanup_candidate": False, "delete_allowed": False,
                            "blockers": ["ambiguous_truncated_installation_name" if len(target.name) > 40
                                         else "unrecognized_backup_name"]})
        else:
            entries.append(_inspect_backup(path, current, legacy))
    recoverable = sorted((item for item in entries if "snapshot" in item and not item["blockers"]),
                         key=lambda item: (item["modified_ns"], item["directory"]), reverse=True)
    # Retain recent distinct recoverable releases, including an older version
    # even when several identical-version interpreter/policy backups exist.
    kept, releases = set(), set()
    for item in recoverable:
        if item["release_id"] not in releases and len(releases) < keep:
            releases.add(item["release_id"])
            kept.add(item["directory"])
    for item in entries:
        item["retain"] = item["directory"] in kept
        if item["retain"]:
            item["cleanup_candidate"] = False
            item["blockers"].append("retained_recoverable_release")
    identity = {"installation": str(target), "snapshot": current["snapshot"], "keep": keep,
                "entries": entries, "limit": limit, "truncated": truncated, "scan_limited": scan_limited}
    return {"status": "preview", "installation": str(target), "target_host": current["target_host"],
            "backup_root": str(root), "keep": keep, "entries": entries,
            "limit": limit, "truncated": truncated, "scan_limited": scan_limited,
            "retention_scope": "visible_entries_only", "scanned_entries": min(scanned, 4096),
            "plan_id": hashlib.sha256(_canonical(identity)).hexdigest(),
            "models_started": False, "deletion_supported": False,
            "references_checked": "active_installation_entries_only",
            "next_step": "Only validated legacy backups can be relocated. Cleanup candidates require separate review of external host references; this operation never deletes files."}


def backup_preview(destination, *, keep=2, limit=128):
    """Public preview excludes per-file hashes; its CAS identity still covers them."""
    plan = _backup_preview(destination, keep=keep, limit=limit)
    entries = []
    for original in plan["entries"]:
        item = {key: value for key, value in original.items() if key not in {"snapshot", "extra_files"}}
        if "snapshot" in original:
            item["snapshot_digest"] = hashlib.sha256(_canonical(original["snapshot"])).hexdigest()
        extras = original.get("extra_files", [])
        item.update(extra_count=len(extras), extra_files=extras[:20], extra_files_truncated=len(extras) > 20)
        entries.append(item)
    return {**plan, "entries": entries}


def migrate_backups(destination, *, expected_plan_id, confirm=False, keep=2, limit=128):
    """Move verified legacy backups outside discovery, with all-or-rollback."""
    from .install import _hook_commands
    if confirm is not True or not isinstance(expected_plan_id, str) or len(expected_plan_id) != 64:
        raise ContractError("backup migration requires confirmation and the current preview plan_id")
    target = safe_path(destination)
    moved = []
    with installation_lock(target):
        plan = _backup_preview(target, keep=keep, limit=limit)
        if plan["plan_id"] != expected_plan_id:
            raise ContractError("backup migration preview changed; review a fresh plan")
        backup = None
        try:
            for entry in plan["entries"]:
                if not entry["migration_allowed"]:
                    continue
                original = safe_path(entry["directory"])
                inspected = inspect_installation(original, _hook_commands, target_host=plan["target_host"],
                                                 installation_target=target)
                inspected["target"] = target
                if inspected["snapshot"] != entry["snapshot"]:
                    raise ContractError("legacy backup changed before relocation")
                # allocate_backup checks the active installation's filesystem.
                backup = allocate_backup(inspected)
                descriptor = _json(_record_path(backup))
                descriptor["original_directory"] = str(original)
                _record_path(backup).write_text(json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
                original.rename(backup)
                moved.append((original, backup, entry["snapshot"]))
                if snapshot(backup) != entry["snapshot"]:
                    raise ContractError("legacy backup changed during relocation")
        except (OSError, ContractError):
            if backup is not None and not backup.exists():
                remove_backup_record(backup)
            for old, new, observed in reversed(moved):
                if old.exists() or _linked(old):
                    raise ContractError("backup migration interrupted; retained backup at " + str(new)) from None
                try:
                    new.rename(old)
                    remove_backup_record(new)
                except OSError:
                    raise ContractError("backup migration interrupted; retained backup at " + str(new)) from None
            raise ContractError("backup migration failed; moved directories were restored") from None
    return {"status": "migrated" if moved else "unchanged", "installation": str(target),
            "moved": [{"previous_directory": str(old), "backup_directory": str(new)} for old, new, _ in moved],
            "models_started": False, "deleted": 0, "host_reload_required": False,
            "knowledge_and_config": "external_untouched"}
