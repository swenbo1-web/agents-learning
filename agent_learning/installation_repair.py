"""Explicit interpreter rebinding of an intact export, retaining its frozen release."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile

from .installation_health import select_python
from .install_update import (INSTALL_MANIFEST, antigravity_hooks, dsh_hooks, installation_lock,
    inspect_installation, mcp_document, preserve_extra_files, safe_path, switch_installation,
    write_installation_manifest, _expected_hook_document, _json)
from .release import filesystem_path
from .runtime import ContractError


def repair_python(destination, python=None, *, expected_release_id=None, allow_volatile_python=False):
    from .install import _hook_commands, _cleanup_owned
    selected, health = select_python(python, allow_volatile=allow_volatile_python)
    target = safe_path(destination)
    staging = None
    identity = None
    with installation_lock(target):
        previous = inspect_installation(target, _hook_commands, expected_release_id)
        receipt = previous["receipt"]
        host = previous["target_host"]
        result = {"status": "unchanged", "installation": str(target), "target_host": host,
                  "version": receipt["version"], "release_id": receipt["release_id"],
                  "interpreter": health, "models_started": False, "knowledge_and_config": "external_untouched",
                  "host_reload_required": True, "hook_review_required": True,
                  "next_steps": (["codex-connect --repair-python", "codex-action (if previously registered)"]
                                 if host == "codex" else ["Reload the installed host plugin."])}
        if host == "antigravity":
            result["next_steps"].append("Refresh any registered UI bridge with install-ui --upgrade.")
        if receipt["python"] == selected:
            return result
        try:
            staging = Path(tempfile.mkdtemp(prefix=".alp-staging-", dir=target.parent))
            info = staging.stat()
            identity = (info.st_dev, info.st_ino)
            for name in sorted(previous["managed"] - {INSTALL_MANIFEST}):
                dest = filesystem_path(staging / name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(filesystem_path(target / name), dest)
            runtime = previous["runtime_directory"]
            multi = host == "dsh" and len(receipt.get("workspaces") or []) > 1
            workspace = None if multi else receipt.get("workspace")
            mcp = mcp_document(runtime, selected, host, workspace, receipt.get("home"),
                               panel_installation=target if "panel_workspace" in receipt else None)
            if host == "codex":
                hooks = _expected_hook_document(runtime, selected, receipt["hook_mode"], _hook_commands)
            elif host == "antigravity":
                hooks = antigravity_hooks(runtime, selected, receipt["hook_mode"], _hook_commands,
                                          receipt["workspace"], receipt["home"])
            else:
                hooks = dsh_hooks(runtime, selected, receipt["hook_mode"], _hook_commands,
                                  workspace, receipt.get("home"))
            for name, body in ((".mcp.json" if host == "codex" else "mcp_config.json", mcp),
                               ("hooks/hooks.json" if host == "codex" else "hooks.json", hooks),
                               ("runtime-release.json", dict(receipt, python=selected))):
                filesystem_path(staging / name).write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n",
                                                          encoding="utf-8")
            # Keep the old installation format too: an interpreter repair is
            # not an upgrade of the frozen runtime or its inspector contract.
            if INSTALL_MANIFEST in previous["snapshot"]["files"]:
                old_manifest = _json(target / INSTALL_MANIFEST)
                write_installation_manifest(staging, receipt["release_id"], host)
                if old_manifest["schema"] == 1:
                    manifest = _json(staging / INSTALL_MANIFEST)
                    manifest["schema"] = 1
                    manifest.pop("target_host", None)
                    (staging / INSTALL_MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            preserved = preserve_extra_files(previous, staging)
            backup = switch_installation(previous, staging)
            staging = None
            return {**result, "status": "repaired", "previous_python": receipt["python"],
                    "backup_directory": str(backup),
                    "preserved_extra_files": [n for n in previous["extra_files"] if n not in preserved]}
        except OSError:
            raise ContractError("interpreter repair failed; existing installation or retained backup was preserved") from None
        finally:
            _cleanup_owned(staging, target.parent, identity)
