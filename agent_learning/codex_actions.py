"""Explicit project Actions registration; never changes the user's environment."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import uuid

from .install import _hook_commands
from .install_update import installation_lock, inspect_installation, python_executable, safe_path
from .runtime import ContractError


ACTION_FILE = "agent-learning.toml"
MANIFEST_FILE = "agent-learning.action.json"
MANAGED_HEADER = "# Agent Learning managed project action; update through codex-action.\n"
FIELDS = {"schema_version", "kind", "workspace", "installation", "python", "runtime_directory",
          "home", "config", "host_instance", "platform", "sha256"}


def _value(value):
    result = str(value)
    if not result or "\0" in result or any(ord(char) < 32 for char in result):
        raise ContractError("invalid action path")
    return result


def build_action_command(receipt, installation, *, home, config, host_instance="default", platform=None):
    """Return the real terminal command; quotation never interpolates a path."""
    platform = platform or sys.platform
    if platform not in {"win32", "linux", "darwin"} or host_instance != "default":
        raise ContractError("Codex actions require a supported platform and the default host instance")
    home, config = _value(home), _value(config)
    arguments = [_value(receipt["python"]), "-B", _value(Path(receipt["runtime_directory"]) / "scripts/learning.py"),
                 "--home", home, "--host", "codex", "--host-instance", host_instance,
                 "codex-panel", "--installation", _value(installation)]
    if platform == "win32":
        quoted = lambda value: "'" + value.replace("'", "''") + "'"
        script = ("$env:ALP_HOME=" + quoted(home) + "; $env:ALP_CONFIG=" + quoted(config)
                  + "; & " + " ".join(quoted(argument) for argument in arguments) + "; exit $LASTEXITCODE")
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded
    return shlex.join(["env", "ALP_HOME=" + home, "ALP_CONFIG=" + config, *arguments])


def _binding(installation, workspace, *, require_panel=True):
    inspected = inspect_installation(installation, _hook_commands, target_host="codex")
    receipt = inspected["receipt"]
    if not receipt.get("panel_workspace"):
        raise ContractError("Codex Actions require a project-bound installation; export with --workspace first")
    if require_panel and "agent_learning/codex_panel.py" not in inspected["release"]["files"]:
        raise ContractError("installed runtime has no direct Codex console; upgrade the project plugin first")
    bound = safe_path(receipt["panel_workspace"])
    actual = safe_path(workspace) if workspace is not None else bound
    if actual != bound or not actual.is_dir():
        raise ContractError("Codex action workspace must exactly match the installed project")
    receipt = dict(receipt, python=str(python_executable(receipt["python"])))
    return receipt, inspected["target"], bound


def _paths(home, config):
    default = (Path(os.environ["LOCALAPPDATA"]) / "AgentLearning" if os.environ.get("LOCALAPPDATA")
               else Path.home() / ".local/share/agent-learning")
    home = safe_path(home if home is not None else os.environ.get("ALP_HOME") or default)
    config = safe_path(config if config is not None else os.environ.get("ALP_CONFIG") or home / "config.json")
    if any(path.exists() and not path.is_dir() for path in (home, *home.parents)):
        raise ContractError("knowledge directory must be a directory")
    if config.exists() and not config.is_file():
        raise ContractError("configuration must be a file")
    return home, config


def _prior(action, manifest):
    safe_path(action)
    safe_path(manifest)
    if not action.exists() and not manifest.exists():
        return None
    if not action.is_file() or not manifest.is_file():
        raise ContractError("existing Action files are not a complete managed installation; preserved")
    raw, metadata = action.read_bytes(), manifest.read_bytes()
    if len(raw) > 131072 or len(metadata) > 65536:
        raise ContractError("managed Action files exceed their budget")
    try:
        body = json.loads(metadata)
    except (ValueError, UnicodeError, RecursionError):
        raise ContractError("managed Action metadata could not be verified") from None
    if (not isinstance(body, dict) or set(body) != FIELDS or type(body.get("schema_version")) is not int
            or body["schema_version"] != 1 or body.get("kind") != "agent-learning-codex-action"
            or body.get("platform") not in {"win32", "linux", "darwin"} or body.get("host_instance") != "default"
            or any(not isinstance(body.get(field), str) or not Path(body[field]).is_absolute()
                   for field in ("workspace", "installation", "python", "runtime_directory", "home", "config"))
            or not raw.startswith(MANAGED_HEADER.encode("utf-8"))
            or body.get("sha256") != hashlib.sha256(raw).hexdigest()):
        raise ContractError("managed Action files changed; preserve the edits before updating")
    return raw, metadata, body


def _write_atomic(path, raw):
    staging = path.parent / (".alp-action-" + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(path)
        os.replace(staging, path)
    finally:
        if staging.exists():
            staging.unlink()


def install_action(installation, workspace=None, remove=False, *, home=None, config=None, host_instance="default"):
    """Write one owned environment, leaving all user setup, actions and comments intact."""
    if type(remove) is not bool or host_instance != "default":
        raise ContractError("Codex action removal must be boolean and host instance must be default")
    installation = safe_path(installation)
    if remove and not installation.exists():
        if workspace is None:
            raise ContractError("specify the bound workspace to remove an uninstalled plugin's Action")
        bound = safe_path(workspace)
        if not bound.is_dir():
            raise ContractError("Codex action workspace must exist")
        receipt = None
    else:
        receipt, installation, bound = _binding(installation, workspace, require_panel=not remove)
    action = safe_path(bound / ".codex/environments" / ACTION_FILE)
    manifest = safe_path(action.parent / MANIFEST_FILE)
    with installation_lock(action):
        previous = _prior(action, manifest)
        if previous and (safe_path(previous[2]["workspace"]) != bound
                         or safe_path(previous[2]["installation"]) != installation):
            raise ContractError("managed Action belongs to another installation; remove it through that installation first")
        if remove:
            if not previous:
                return {"status": "absent", "action_file": str(action), "administrator_started": False}
            # Verify both owned files immediately before deleting either one.
            if _prior(action, manifest) != previous:
                raise ContractError("managed Action changed while removing; preserved")
            action.unlink()
            try:
                manifest.unlink()
            except BaseException:
                _write_atomic(action, previous[0])
                raise
            return {"status": "removed", "action_file": str(action), "administrator_started": False}
        home, config = _paths(home, config)
        command = build_action_command(receipt, installation, home=home, config=config, host_instance=host_instance)
        platform = sys.platform
        quote = lambda value: json.dumps(value, ensure_ascii=False)
        raw = (MANAGED_HEADER + 'version = 1\nname = "Agent Learning"\n\n[setup]\nscript = ""\n\n'
               + '[[actions]]\nname = "Agent Learning"\nicon = "debug"\n'
               + "command = " + quote(command) + "\nplatform = " + quote(platform) + "\n").encode("utf-8")
        body = {"schema_version": 1, "kind": "agent-learning-codex-action", "workspace": str(bound),
                "installation": str(installation), "python": receipt["python"],
                "runtime_directory": str(safe_path(receipt["runtime_directory"])), "home": str(home),
                "config": str(config), "host_instance": host_instance, "platform": platform,
                "sha256": hashlib.sha256(raw).hexdigest()}
        metadata = (json.dumps(body, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        unchanged = previous is not None and previous[:2] == (raw, metadata)
        if not unchanged:
            if _prior(action, manifest) != previous:
                raise ContractError("managed Action changed while preparing; preserved")
            try:
                _write_atomic(action, raw)
                _write_atomic(manifest, metadata)
            except BaseException:
                if previous:
                    _write_atomic(action, previous[0])
                    _write_atomic(manifest, previous[1])
                elif action.exists():
                    action.unlink()
                raise
    return {"status": "unchanged" if unchanged else "installed", "action_name": "Agent Learning",
            "action_file": str(action), "workspace": str(bound), "installation": str(installation),
            "home": str(home), "config": str(config), "manual_command": command,
            "select_environment": "Agent Learning", "selected_automatically": False,
            "administrator_started": False,
            "next_step": "Select Agent Learning in the project's local Environment menu, then click its top-bar Action. "
                         "To keep your current environment, add manual_command as an Action there instead. "
                         "Opening the console starts no administrator model."}
