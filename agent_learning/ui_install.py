"""Explicit Antigravity UI-only export; no runtime, knowledge or host config writes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from . import __version__
from .install import _hook_commands, _resource_root
from .install_update import installation_lock, inspect_installation, safe_path, snapshot
from .runtime import ContractError


MANIFEST = "ui-installation.json"
REGISTRY = "ui-registry.json"
NAME = "agent-learning-ui"
MAX_PROJECTS = 128
MANAGED_FILES = frozenset(("plugin.json", REGISTRY, "sidecars/control/sidecar.json",
                           "sidecars/control/main.mjs", "sidecars/control/index.html"))
_VERSION = re.compile(r"^(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})$")


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def _json(path):
    try:
        raw = path.read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ContractError("UI installation identity could not be verified") from None


def _entry(installation):
    inspected = inspect_installation(installation, _hook_commands, target_host="antigravity")
    # This immutable release member marks the first core with this bridge and
    # its debug action. An older v1.0.0 can pass integrity checks yet lack them.
    if "agent_learning/ui_install.py" not in inspected["release"]["files"]:
        raise ContractError("upgrade the project core before registering this UI bridge")
    receipt, root = inspected["receipt"], inspected["target"]
    if not isinstance(receipt.get("workspace"), str) or not isinstance(receipt.get("home"), str):
        raise ContractError("UI registration requires a project-bound Antigravity installation")
    workspace = safe_path(receipt["workspace"])
    if not workspace.is_dir():
        raise ContractError("UI registration requires an existing workspace")
    return ({"workspace": str(workspace), "installation": str(root),
            "receipt_sha256": hashlib.sha256((root / "runtime-release.json").read_bytes()).hexdigest(),
            "python": str(safe_path(receipt["python"])),
            "runtime_directory": str(safe_path(receipt["runtime_directory"]))}, safe_path(receipt["home"]))


def _prior(target):
    observed = snapshot(target)
    if any(Path(name).name.lower() in {"runtime.sqlite3", "runtime.sqlite3-wal", "runtime.sqlite3-shm", "runtime.sqlite3-journal"}
           for name in observed["files"]):
        raise ContractError("UI destination contains a knowledge database; it was preserved")
    manifest = _json(target / MANIFEST)
    if (set(manifest) != {"schema_version", "name", "version", "files"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1 or manifest["name"] != NAME
            or not isinstance(manifest["version"], str) or not _VERSION.fullmatch(manifest["version"])
            or not isinstance(manifest["files"], dict) or set(manifest["files"]) != MANAGED_FILES
            or any(not _digest(value) for value in manifest["files"].values())):
        raise ContractError("destination is not a managed Agent Learning UI bridge")
    for name, expected in manifest["files"].items():
        if observed["files"].get(name) != expected:
            raise ContractError("managed UI resources changed; preserve them before updating")
    registry = _json(target / REGISTRY)
    if (set(registry) != {"schema_version", "projects"} or type(registry["schema_version"]) is not int
            or registry["schema_version"] != 1
            or not isinstance(registry["projects"], list) or len(registry["projects"]) > MAX_PROJECTS):
        raise ContractError("invalid UI project registry")
    fields = {"workspace", "installation", "receipt_sha256", "python", "runtime_directory"}
    workspaces, installations = set(), set()
    for entry in registry["projects"]:
        if (not isinstance(entry, dict) or set(entry) != fields
                or any(not isinstance(entry[key], str) or not Path(entry[key]).is_absolute()
                       or str(safe_path(entry[key])) != entry[key]
                       for key in fields - {"receipt_sha256"})
                or not _digest(entry["receipt_sha256"])):
            raise ContractError("invalid UI project registration")
        key, location = os.path.normcase(entry["workspace"]), os.path.normcase(entry["installation"])
        if key in workspaces or location in installations:
            raise ContractError("ambiguous UI project registry")
        workspaces.add(key)
        installations.add(location)
    return observed, manifest, registry["projects"]


def install_ui(destination, installations, *, upgrade=False, remove=False):
    """Register only explicit verified installations, atomically preserving extras."""
    if not installations or not isinstance(installations, (list, tuple)) or len(installations) > MAX_PROJECTS:
        raise ContractError("select 1..128 explicit Antigravity installations")
    if type(upgrade) is not bool or type(remove) is not bool:
        raise ContractError("UI upgrade and removal flags must be boolean")
    target = safe_path(destination)
    source = _resource_root()
    if isinstance(source, Path) and _overlap(target, source):
        raise ContractError("UI destination must not overlap the source or fixed runtime")
    bindings = [] if remove else [_entry(safe_path(path)) for path in installations]
    entries = [entry for entry, _ in bindings]
    for entry, home in bindings:
        for bound in (Path(entry["workspace"]), Path(entry["installation"]), Path(entry["runtime_directory"]), home):
            if _overlap(target, bound):
                raise ContractError("global UI export must stay outside project, knowledge and runtime directories")
    with installation_lock(target):
        previous = _prior(target) if target.exists() else None
        if previous and not upgrade:
            raise ContractError("UI bridge already exists; use --upgrade to add, refresh or remove registrations")
        if not previous and (upgrade or remove):
            raise ContractError("UI bridge does not exist; install it before updating")
        projects = list(previous[2]) if previous else []
        if remove:
            selected = {os.path.normcase(str(safe_path(path))) for path in installations}
            projects = [entry for entry in projects if os.path.normcase(entry["installation"]) not in selected]
        else:
            for entry in entries:
                projects = [old for old in projects if os.path.normcase(old["installation"]) != os.path.normcase(entry["installation"])
                            and os.path.normcase(old["workspace"]) != os.path.normcase(entry["workspace"])]
                projects.append(entry)
        if len(projects) > MAX_PROJECTS:
            raise ContractError("UI project registration limit reached")
        # Neither staged nor old plugin payloads may be discoverable as a second
        # global Sidecar. Do not depend on the host ignoring dot directories.
        transactions = safe_path(target.parent.parent / "agent-learning-ui-installations")
        boundaries = [target, *(home for _, home in bindings)]
        if isinstance(source, Path):
            boundaries.append(source)
        boundaries.extend(Path(entry[key]) for entry in projects
                          for key in ("workspace", "installation", "runtime_directory"))
        if any(_overlap(transactions, bound) for bound in boundaries):
            raise ContractError("UI transaction directory must stay outside plugins, projects, knowledge and runtime directories")
        transactions.mkdir(parents=True, exist_ok=True)
        safe_path(transactions)
        staging = Path(tempfile.mkdtemp(prefix=".alp-staging-", dir=transactions))
        staging_identity = (staging.stat().st_dev, staging.stat().st_ino)
        backup = None
        try:
            if previous:
                for relative in previous[0]["directories"]:
                    (staging / relative).mkdir(parents=True, exist_ok=True)
                for relative in previous[0]["files"]:
                    if relative not in previous[1]["files"] and relative != MANIFEST:
                        extra = staging / relative
                        extra.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target / relative, extra)
            sidecar = staging / "sidecars/control"
            sidecar.mkdir(parents=True, exist_ok=True)
            resources = source.joinpath("host_resources/antigravity/sidecars/control")
            for name in ("sidecar.json", "main.mjs", "index.html"):
                raw = resources.joinpath(name).read_bytes()
                if name == "sidecar.json":
                    metadata = json.loads(raw)
                    metadata.update(display_name="Agent Learning", description="UI-only bridge for explicitly registered project installations. No model calls on startup.")
                    raw = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                (sidecar / name).write_bytes(raw)
            (staging / "plugin.json").write_text(json.dumps({"name": NAME,
                "description": "UI-only project control bridge. Core hooks, MCP and knowledge stay in each registered project."}, indent=2) + "\n", encoding="utf-8")
            (staging / REGISTRY).write_text(json.dumps({"schema_version": 1,
                "projects": sorted(projects, key=lambda item: item["workspace"])}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            managed = {name: hashlib.sha256((staging / name).read_bytes()).hexdigest() for name in sorted(MANAGED_FILES)}
            (staging / MANIFEST).write_text(json.dumps({"schema_version": 1, "name": NAME,
                "version": __version__, "files": managed}, indent=2) + "\n", encoding="utf-8")
            if previous:
                if snapshot(target) != previous[0]:
                    raise ContractError("UI destination changed; it was preserved")
                backup = transactions / (".alp-backup-ui-" + uuid.uuid4().hex)
                target.rename(backup)
                if snapshot(backup) != previous[0]:
                    raise ContractError("UI destination changed while switching")
            safe_path(target)
            if target.exists():
                raise ContractError("UI destination appeared while preparing")
            staging.rename(target)
        except BaseException:
            if backup and backup.exists() and not target.exists():
                backup.rename(target)
            raise
        finally:
            if staging.exists() and (staging.stat().st_dev, staging.stat().st_ino) == staging_identity:
                # Only the verified staging directory created above is removed.
                shutil.rmtree(staging)
    return {"status": "installed", "target": str(target), "plugin": NAME, "registered_projects": len(projects),
            "sidecar_id": NAME + "/control", "ui_only": True, "enabled": "requires_host_activation",
            "host_activation": {"sidecars": {NAME + "/control": {"enabled": True}}},
            "backup": str(backup) if backup else None,
            "next_step": "Enable Agent Learning UI in Antigravity App customizations and reload the app. Core project installations remain separate."}
