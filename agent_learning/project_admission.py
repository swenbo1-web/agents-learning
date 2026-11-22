"""Read-only project admission for automatically discovered host entrypoints.

An existing database project is never installation evidence. Admission examines
bounded installation identity files and project-local wiring before Runtime can
be opened. Full release verification remains the executable entrypoint's job.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .install_update import safe_path
from .runtime import ContractError, encode


MAX_IDENTITY_BYTES = 2 * 1024 * 1024


def workspace_path(value):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ContractError("panel workspace is required")
    text = str(value)
    if text.startswith("file:"):
        parsed = urlsplit(text)
        if parsed.scheme != "file" or parsed.query or parsed.fragment or parsed.netloc not in {"", "localhost"}:
            raise ContractError("unsupported panel workspace URI")
        text = unquote(parsed.path)
        if os.name == "nt" and len(text) > 2 and text[0] == "/" and text[2] == ":":
            text = text[1:]
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ContractError("panel workspace must be absolute")
    path = safe_path(path)
    if not path.is_dir():
        raise ContractError("panel workspace must be an existing directory")
    return path


def same_workspace(actual, bound):
    """Allow subdirectories while keeping nested checkouts and links separate."""
    try:
        actual, bound = workspace_path(actual), workspace_path(bound)
        actual.relative_to(bound)
        cursor = actual
        while cursor != bound:
            marker = cursor / ".git"
            if marker.exists() or marker.is_symlink():
                return False
            cursor = cursor.parent
        return True
    except (ContractError, OSError, ValueError):
        return False


def _identity(root, name):
    path = safe_path(root / name)
    if not path.is_file() or path.stat().st_size > MAX_IDENTITY_BYTES:
        raise ContractError("project_not_installed")
    with path.open("rb") as stream:
        raw = stream.read(MAX_IDENTITY_BYTES + 1)
    if len(raw) > MAX_IDENTITY_BYTES:
        raise ContractError("project_not_installed")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ContractError("project_not_installed")
    return value, hashlib.sha256(raw).hexdigest()


def _configured_installation(root):
    """Read only owned MCP fields, using the existing Python 3.10 TOML subset."""
    path = safe_path(root / ".codex/config.toml")
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_IDENTITY_BYTES:
        raise ContractError("project_not_installed")
    from .codex_connect import SERVER, _document, _string, _strings
    entries = _document(path.read_text(encoding="utf-8-sig"))[1]
    enabled = entries.get((*SERVER, "enabled"))
    if enabled and enabled[2].split("#", 1)[0].strip() == "false":
        return None
    arguments = entries.get((*SERVER, "args"))
    if arguments is None:
        return None
    args = _strings(arguments[2])
    flags = [flag for flag in ("--panel-installation", "--plugin-installation") if flag in args]
    if "--panel-discovery" in args or not flags or any(args.count(flag) != 1 for flag in flags):
        return None
    targets = []
    for flag in flags:
        index = args.index(flag)
        if index + 1 >= len(args):
            raise ContractError("project_not_installed")
        targets.append(safe_path(args[index + 1]))
    if len(set(targets)) != 1:
        raise ContractError("project_not_installed")
    configured = entries.get((*SERVER, "cwd"))
    if configured and workspace_path(_string(configured[2])) != root:
        raise ContractError("project_not_installed")
    return targets[0]


def _candidates(workspace):
    for root in (workspace, *workspace.parents):
        try:
            configured = _configured_installation(root)
        except (ContractError, OSError, ValueError, TypeError):
            configured = None
        if configured is not None:
            yield configured, None
        local = root / ".agents/plugins/agent-learning"
        if local.is_dir():
            yield local, root
        marker = root / ".git"
        if marker.exists() or marker.is_symlink():
            break
        if root.name.lower() == "local" and root.parent.name.lower() == "build":
            break


def _binding(workspace, installation, owner, host, runtime_directory, release, linked=None):
    installation = safe_path(installation)
    receipt, receipt_hash = _identity(installation, "runtime-release.json")
    if receipt.get("target_host", "codex") != host:
        raise ContractError("project_not_installed")
    runtime = safe_path(receipt["runtime_directory"])
    if runtime_directory is not None and runtime != safe_path(runtime_directory):
        raise ContractError("project_not_installed")
    if not runtime.is_dir() or runtime == installation or runtime in installation.parents or installation in runtime.parents:
        raise ContractError("project_not_installed")
    current = release if release is not None else _identity(runtime, "release.json")[0]
    fields = ("release_id", "version", "protocol_version", "content_digest")
    if (any(not isinstance(current.get(key), str) or receipt.get(key) != current[key] for key in fields)
            or runtime.name != current["release_id"]):
        raise ContractError("project_not_installed")
    if host == "codex":
        declared = receipt.get("panel_workspace")
        bound = workspace_path(declared) if declared is not None else owner
    elif host == "dsh":
        roots = receipt.get("workspaces") or [receipt.get("workspace")]
        bound = next((workspace_path(item) for item in roots if item and same_workspace(workspace, item)), None)
    else:
        bound = workspace_path(receipt["workspace"])
    inherited = linked is not None and bound == linked.primary
    if bound is None or not (inherited or same_workspace(workspace, bound)):
        raise ContractError("project_not_installed")
    from .install import _hook_commands
    from .install_update import (_expected_hook_document, antigravity_hooks,
                                codex_plugin_binding, dsh_hooks, mcp_document)
    if host == "codex":
        plugin_name, mcp_name, hooks_name = ".codex-plugin/plugin.json", ".mcp.json", "hooks/hooks.json"
        expected_hooks = _expected_hook_document(runtime, receipt["python"], receipt["hook_mode"], _hook_commands)
    else:
        plugin_name, mcp_name, hooks_name = "plugin.json", "mcp_config.json", "hooks.json"
        builder = antigravity_hooks if host == "antigravity" else dsh_hooks
        pinned = None if host == "dsh" and len(receipt.get("workspaces", [])) > 1 else receipt.get("workspace")
        expected_hooks = builder(runtime, receipt["python"], receipt["hook_mode"], _hook_commands, pinned, receipt.get("home"))
    plugin, plugin_hash = _identity(installation, plugin_name)
    mcp, mcp_hash = _identity(installation, mcp_name)
    hooks, hooks_hash = _identity(installation, hooks_name)
    if plugin.get("name") != "agent-learning" or host != "antigravity" and plugin.get("version") != receipt["version"]:
        raise ContractError("project_not_installed")
    pinned = None if host == "dsh" and len(receipt.get("workspaces", [])) > 1 else receipt.get("workspace")
    expected_mcp = mcp_document(runtime, receipt["python"], host, pinned, receipt.get("home"),
        panel_installation=installation if "panel_workspace" in receipt else None,
        plugin_installation=codex_plugin_binding(receipt, installation))
    if mcp != expected_mcp or hooks != expected_hooks:
        raise ContractError("project_not_installed")
    manifest_path = installation / "installation-manifest.json"
    if manifest_path.exists():
        manifest, _ = _identity(installation, "installation-manifest.json")
        expected_hashes = {"runtime-release.json": receipt_hash, plugin_name: plugin_hash,
                           mcp_name: mcp_hash, hooks_name: hooks_hash}
        if (manifest.get("plugin") != "agent-learning" or manifest.get("release_id") != receipt["release_id"]
                or manifest.get("schema") not in {1, 2}
                or manifest.get("target_host", "codex") != host
                or any(manifest.get("files", {}).get(name) != value for name, value in expected_hashes.items())):
            raise ContractError("project_not_installed")
    result = {"installation": str(installation), "workspace": str(bound), "runtime_directory": str(runtime),
              "release_id": receipt["release_id"], "receipt_digest": hashlib.sha256(encode(receipt).encode("utf-8")).hexdigest()}
    if inherited:
        result["checkout"] = str(linked.checkout)
        result["worktree_registration"] = (str(linked.git_dir), linked.git_dir_identity)
    return result


def require_project_installation(workspace, installation=None, *, host="codex", runtime_directory=None, release=None):
    """Require project opt-in before an automatic Hook or project worker opens Runtime."""
    try:
        workspace = workspace_path(workspace)
        if host not in {"codex", "antigravity", "dsh"}:
            raise ContractError("project_not_installed")
        linked = None
        primary_candidates = ()
        local_candidates = ()
        if host == "codex":
            from .git_worktrees import linked_worktree_identity
            linked = linked_worktree_identity(workspace)
            local_candidates = tuple(_candidates(workspace))
            if linked is not None:
                primary_candidates = tuple(_candidates(linked.primary))
        if installation is not None:
            target = safe_path(installation)
            owner = next((root for candidate, root in (*local_candidates, *primary_candidates)
                          if root is not None and safe_path(candidate) == target), None)
            candidates = [(target, owner)]
        elif host == "codex":
            candidates = (*local_candidates, *primary_candidates)
        else:
            raise ContractError("project_not_installed")
        found = False
        for target, owner in candidates:
            if installation is None:
                try:
                    receipt, _ = _identity(safe_path(target), "runtime-release.json")
                except (ContractError, OSError, ValueError, TypeError, KeyError):
                    found = True
                    continue
                if receipt.get("target_host", "codex") != host:
                    continue
            found = True
            try:
                # A copied receipt cannot stand in for project opt-in.
                inherited_link = linked if any(
                    target == candidate for candidate, _ in primary_candidates) else None
                return _binding(workspace, target, owner, host, runtime_directory, release, inherited_link)
            except (ContractError, OSError, ValueError, TypeError, KeyError):
                if installation is not None:
                    raise ContractError("project_installation_invalid") from None
        if found:
            raise ContractError("project_installation_invalid")
        raise ContractError("project_not_installed")
    except ContractError as exc:
        if str(exc) == "project_installation_invalid":
            raise
        raise ContractError("project_not_installed") from None
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise ContractError("project_not_installed") from None


def codex_project_binding(workspace):
    """Return the shared installation of a linked checkout, if it is admitted."""
    from .git_worktrees import linked_worktree_identity
    if linked_worktree_identity(workspace) is None:
        return None
    try:
        binding = require_project_installation(workspace, host="codex")
    except ContractError:
        return None
    return binding if "checkout" in binding else None
