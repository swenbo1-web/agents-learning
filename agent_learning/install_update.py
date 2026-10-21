"""安装目录身份校验与可回滚切换；不会创建 Runtime 或打开知识数据库。"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from .runtime import ContractError
from .release import PROTOCOL_VERSION, filesystem_path


INSTALL_MANIFEST = "installation-manifest.json"
INSTALL_SCHEMA = 2
TARGET_HOSTS = ("codex", "antigravity", "dsh")
# Published host scripts that were previously distributed as unmanaged extras.
# Hash normalized line endings so Git's Windows checkout does not change identity.
# Unknown/custom content is never adopted automatically; the old tree is backed up.
DSH_HOST_ADOPTION = {
    "host/delegation-policy.mjs": frozenset({
        "4a857b0d5f814d073ccb45a45e29367357ae493a423910b3c5ff89f446bad7e9",
        "18df01c59b916652a036e4b7ae653ed79286e5cbf35cb37431c2904d370739da"}),
    "host/google-thinking.mjs": frozenset({"bc6f3f95860a514d23fef7497d7d980473411d1a3dbbe925ee1822a3b1236ab0"}),
    "host/messages-compat.mjs": frozenset({"0a47516917723cbc23cf966b45b87aec8b35e201cadd8366dbc2fd5966de1afd"}),
    "host/net-tune.mjs": frozenset({"12a8304058964ae68c79758775ca9612718f588efae388e98185fb7618c064fd"}),
}
_VERSION = re.compile(r"^(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})$")
_PROTOCOL = re.compile(r"^[1-9][0-9]{0,8}$")


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _linked(path):
    try:
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = filesystem_path(path).lstat()
    except FileNotFoundError:
        return False
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & 0x400))


def safe_path(value):
    """在 resolve 之前拒绝链接、Windows junction 和路径穿越。"""
    try:
        path = Path(value).expanduser()
        if ".." in path.parts:
            raise ContractError("installation paths must not contain parent traversal")
        path = path.absolute()
        for item in (path, *path.parents):
            if _linked(item):
                raise ContractError("installation paths must not contain symbolic links or junctions")
        return path.resolve()
    except ContractError:
        raise
    except (OSError, TypeError, ValueError):
        raise ContractError("installation path could not be verified") from None


def python_executable(value, *, require_available=True):
    """Validate a pinned interpreter without resolving its virtualenv symlink.

    Unlike writable installation paths, interpreter aliases are valid. Keep
    their invocation path: resolving a venv's Python can change its environment.
    Existing UI metadata may retain an unavailable interpreter for safe removal.
    """
    try:
        if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
            raise ValueError
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or require_available and not path.is_file():
            raise ValueError
        return path
    except (OSError, TypeError, ValueError, RuntimeError):
        raise ContractError("installed Python interpreter is invalid or unavailable; run installation-check "
                            "with an independent Python, then repair-python --confirm") from None


def _hash_file(path):
    digest = hashlib.sha256()
    with filesystem_path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot(root):
    root = filesystem_path(safe_path(root))
    if not root.is_dir():
        raise ContractError("installation destination must be an existing directory")
    info = root.stat()
    files, directories = {}, []
    for current, subdirs, names in os.walk(root, followlinks=False):
        current = Path(current)
        for name in subdirs:
            child = current / name
            if _linked(child):
                raise ContractError("installation directories must not contain links or junctions")
            directories.append(child.relative_to(root).as_posix())
        for name in names:
            child = current / name
            if _linked(child) or not child.is_file():
                raise ContractError("installation files must be regular files")
            files[child.relative_to(root).as_posix()] = _hash_file(child)
    return {"root_identity": [info.st_dev, info.st_ino], "files": files,
            "directories": sorted(directories)}


def _json(path):
    try:
        value = json.loads(filesystem_path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ContractError("existing installation identity could not be verified") from None


def payload_mapping(files, target_host):
    """固定副本保留各宿主资源；导出目录仅激活指定宿主的原生入口。"""
    if target_host not in TARGET_HOSTS:
        raise ContractError("target host must be codex, antigravity or dsh")
    if target_host == "codex":
        return {name: name for name in files}
    result = {name: name for name in files if name.startswith(("agent_learning/", "scripts/", "hooks/"))
              or name in ("README.md", "LICENSE", "config.example.json")}
    prefix = f"host_resources/{target_host}/"
    result.update({name[len(prefix):]: name for name in files if name.startswith(prefix)})
    return result


def validate_previous_release(root):
    """使用副本自己的版本/协议验证旧清单，不调用只接受当前版本的校验器。"""
    root = safe_path(root)
    tree = snapshot(root)
    manifest = _json(root / "release.json")
    if (set(manifest) != {"version", "protocol_version", "files", "content_digest", "release_id"}
            or not isinstance(manifest.get("version"), str) or not _VERSION.fullmatch(manifest["version"])
            or not isinstance(manifest.get("protocol_version"), str)
            or not _PROTOCOL.fullmatch(manifest["protocol_version"])
            or not 1 <= int(manifest["protocol_version"]) <= int(PROTOCOL_VERSION)
            or not isinstance(manifest.get("files"), dict)):
        raise ContractError("existing fixed runtime release manifest is invalid")
    files = {name: value for name, value in tree["files"].items()
             if name != "release.json" and not name.endswith(".pyc") and "__pycache__" not in Path(name).parts}
    body = {"version": manifest["version"], "protocol_version": manifest["protocol_version"], "files": files}
    digest = hashlib.sha256(_canonical(body)).hexdigest()
    if (manifest["files"] != files or manifest["content_digest"] != digest
            or manifest["release_id"] != manifest["version"] + "-" + digest
            or root.name != manifest["release_id"]):
        raise ContractError("existing fixed runtime release has changed or lost its pinned identity")
    plugin = _json(root / ".codex-plugin/plugin.json")
    if plugin.get("name") != "agent-learning" or plugin.get("version") != manifest["version"]:
        raise ContractError("existing fixed runtime release is not an agent-learning plugin")
    return manifest


def _expected_hook_document(runtime, python, mode, command_builder):
    hooks = _json(runtime / "hooks/hooks.json")
    try:
        if not isinstance(hooks.get("hooks"), dict) or not hooks["hooks"]:
            raise TypeError
        for event, groups in hooks["hooks"].items():
            if not event or not isinstance(groups, list) or not groups:
                raise TypeError
            command, windows = command_builder(python, runtime / "hooks/dispatch.py", event, mode)
            for group in groups:
                if (not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
                        or not group["hooks"]):
                    raise TypeError
                for handler in group["hooks"]:
                    if not isinstance(handler, dict) or handler.get("type") != "command":
                        raise TypeError
                    handler["command"] = command
                    handler.pop("command_windows", None)
                    handler["commandWindows"] = windows
    except (KeyError, TypeError, AttributeError):
        raise ContractError("existing fixed runtime Hook contract is invalid") from None
    return hooks


def antigravity_hooks(runtime, python, mode, command_builder, workspace, home):
    """生成原生命名 Hook 文档；观察安装不接管宿主工具审批。"""
    hooks = _json(runtime / "host_resources/antigravity/hooks.json")
    try:
        if set(hooks) != {"agent-learning"} or not isinstance(hooks["agent-learning"], dict):
            raise TypeError
        definition = hooks["agent-learning"]
        if set(definition) != {"PreInvocation", "PostToolUse", "PreToolUse", "Stop"}:
            raise TypeError
        if mode != "enforce":
            definition.pop("PreToolUse")
        for event, handlers in definition.items():
            if not isinstance(handlers, list) or not handlers:
                raise TypeError
            command, windows = command_builder(python, runtime / "hooks/antigravity_dispatch.py", event, mode,
                                               host="antigravity", workspace=workspace, home=home)
            for item in handlers:
                if not isinstance(item, dict):
                    raise TypeError
                children = item.get("hooks") if event in ("PreToolUse", "PostToolUse") else [item]
                if not isinstance(children, list) or not children:
                    raise TypeError
                for handler in children:
                    if not isinstance(handler, dict) or handler.get("type") != "command":
                        raise TypeError
                    handler["command"] = windows if os.name == "nt" else command
    except (KeyError, TypeError, AttributeError):
        raise ContractError("existing fixed runtime Antigravity Hook contract is invalid") from None
    return hooks


def dsh_hooks(runtime, python, mode, command_builder, workspace=None, home=None):
    """生成 DSH 原生 Hook 文档，固定绑定本机解释器与 dsh_dispatch.py。"""
    hooks = _json(runtime / "host_resources/dsh/hooks.json")
    try:
        if set(hooks) != {"agent-learning"} or not isinstance(hooks["agent-learning"], dict):
            raise TypeError
        definition = hooks["agent-learning"]
        expected_events = {
            "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
            "Stop", "Interrupt", "SubagentStart", "SubagentStop",
        }
        if set(definition) != expected_events:
            raise TypeError
        for event, handlers in definition.items():
            if not isinstance(handlers, list) or not handlers:
                raise TypeError
            command, windows = command_builder(
                python, runtime / "hooks/dsh_dispatch.py", event, mode,
                host="dsh", workspace=workspace, home=home,
            )
            for item in handlers:
                if not isinstance(item, dict):
                    raise TypeError
                children = item.get("hooks") if event in ("PreToolUse", "PostToolUse") else [item]
                if not isinstance(children, list) or not children:
                    raise TypeError
                for handler in children:
                    if not isinstance(handler, dict) or handler.get("type") != "command":
                        raise TypeError
                    handler["command"] = windows if os.name == "nt" else command
    except (KeyError, TypeError, AttributeError):
        raise ContractError("existing fixed runtime DSH Hook contract is invalid") from None
    return hooks


def mcp_document(runtime, python, host="codex", workspace=None, home=None, *, panel_installation=None):
    arguments = [str(runtime / "scripts/learning.py"), "serve", "--role", "worker"]
    if host == "antigravity":
        arguments = ["-B", str(runtime / "scripts/learning.py"), "--workspace", workspace, "--home", home,
                     "serve", "--role", "worker", "--host", "antigravity"]
    elif host == "dsh":
        arguments = ["-B", str(runtime / "scripts/learning.py")]
        if workspace is not None:
            arguments.extend(["--workspace", workspace])
        if home is not None:
            arguments.extend(["--home", home])
        arguments.extend(["serve", "--role", "worker", "--host", "dsh"])
    if panel_installation is not None:
        if host != "codex":
            raise ContractError("MCP panel installation binding applies only to Codex")
        arguments.extend(["--panel-installation", str(panel_installation)])
    return {"mcpServers": {"agent_learning": {"command": python, "args": arguments}}}


def inspect_installation(destination, command_builder, expected_release_id=None, *, target_host=None,
                         installation_target=None):
    """只读身份、受管理文件及用户额外文件；旧版本不要求已有安装清单。"""
    target = safe_path(destination)
    # Backups retain the MCP panel's original, explicit installation path.
    # This optional identity is only supplied by the backup inspector.
    bound_target = safe_path(installation_target) if installation_target is not None else target
    observed = snapshot(target)
    if any(Path(name).name.lower() in {"runtime.sqlite3", "runtime.sqlite3-wal", "runtime.sqlite3-shm", "runtime.sqlite3-journal"}
           for name in observed["files"]):
        raise ContractError("installation contains a knowledge database; move its data outside the export before upgrading")
    if ("pyproject.toml" in observed["files"] or "release.json" in observed["files"]
            or ".git" in observed["files"] or ".git" in observed["directories"]):
        raise ContractError("refusing to replace a source checkout or fixed runtime release")
    receipt = _json(target / "runtime-release.json")
    host = receipt.get("target_host", "codex")
    if host not in TARGET_HOSTS:
        raise ContractError("existing installation target host is invalid")
    if target_host is not None and host != target_host:
        raise ContractError("refusing to overwrite an installation for a different target host")
    required = {"runtime_directory", "release_id", "version", "protocol_version", "content_digest", "python", "hook_mode"}
    if (not required.issubset(receipt) or not isinstance(receipt.get("runtime_directory"), str)
            or not Path(receipt["runtime_directory"]).is_absolute()
            or not isinstance(receipt.get("python"), str) or not receipt["python"]
            or not Path(receipt["python"]).is_absolute()
            or receipt.get("hook_mode") not in (None, "observe", "enforce")):
        raise ContractError("existing installation runtime receipt is invalid")
    if "panel_workspace" in receipt:
        value = receipt["panel_workspace"]
        if host != "codex" or not isinstance(value, str) or not Path(value).is_absolute():
            raise ContractError("existing installation panel workspace binding is invalid")
        panel_workspace = safe_path(value)
        if not panel_workspace.is_dir() or str(panel_workspace) != value:
            raise ContractError("existing installation panel workspace must be a canonical existing directory")
    if host == "antigravity":
        if (receipt.get("hook_mode") not in ("observe", "enforce")
                or any(not isinstance(receipt.get(name), str) or not Path(receipt[name]).is_absolute()
                       for name in ("workspace", "home"))):
            raise ContractError("existing Antigravity installation binding is invalid")
        for name in ("workspace", "home"):
            safe_path(receipt[name])
    elif host == "dsh":
        for name in ("workspace", "home"):
            if receipt.get(name) is not None:
                if not isinstance(receipt[name], str) or not Path(receipt[name]).is_absolute():
                    raise ContractError("existing DSH installation binding is invalid")
                safe_path(receipt[name])
        if "workspaces" in receipt:
            workspaces = receipt.get("workspaces")
            if (not isinstance(workspaces, list) or not workspaces
                    or any(not isinstance(item, str) or not Path(item).is_absolute() for item in workspaces)):
                raise ContractError("existing DSH installation workspaces binding is invalid")
            for item in workspaces:
                safe_path(item)
    runtime = safe_path(receipt["runtime_directory"])
    if target == runtime or target in runtime.parents or runtime in target.parents:
        raise ContractError("existing installation and fixed runtime must use separate directories")
    release = validate_previous_release(runtime)
    if any(receipt[key] != release[key] for key in ("release_id", "version", "protocol_version", "content_digest")):
        raise ContractError("existing installation receipt does not match its fixed runtime")
    if expected_release_id is not None and receipt["release_id"] != expected_release_id:
        raise ContractError("existing installation release changed; expected release identity does not match")
    plugin_path = ".codex-plugin/plugin.json" if host == "codex" else "plugin.json"
    plugin = _json(target / plugin_path)
    if host == "codex":
        if (plugin.get("name") != "agent-learning" or plugin.get("version") != release["version"]
                or plugin.get("skills") != "./skills/" or plugin.get("hooks") != "./hooks/hooks.json"
                or plugin.get("mcpServers") != "./.mcp.json"):
            raise ContractError("existing installation is not a valid agent-learning plugin export")
    elif host == "dsh":
        pkg = _json(target / "package.json")
        if (plugin.get("name") != "agent-learning" or plugin.get("version") != release["version"]
                or plugin.get("skills") != "./skills/" or plugin.get("hooks") != "./hooks.json"
                or plugin.get("mcpServers") != "./mcp_config.json" or plugin.get("hostEntry") != "./host/index.mjs"
                or pkg.get("name") != "agent-learning" or pkg.get("version") != release["version"]):
            raise ContractError("existing installation is not a valid DSH plugin export")
    elif (set(plugin) != {"name", "description"} or plugin.get("name") != "agent-learning"
          or not isinstance(plugin.get("description"), str)):
        raise ContractError("existing installation is not a valid Antigravity plugin export")
    # 这些文件只含安装时的固定路径；其余受管理文件必须与旧发布副本一致。
    dsh_multi = host == "dsh" and isinstance(receipt.get("workspaces"), list) and len(receipt["workspaces"]) > 1
    pinned_workspace = None if dsh_multi else receipt.get("workspace")
    mcp = mcp_document(runtime, receipt["python"], host, pinned_workspace, receipt.get("home"),
                       panel_installation=bound_target if "panel_workspace" in receipt else None)
    mcp_path = ".mcp.json" if host == "codex" else "mcp_config.json"
    hook_path = "hooks/hooks.json" if host == "codex" else "hooks.json"
    if host == "codex":
        expected_hooks = _expected_hook_document(runtime, receipt["python"], receipt["hook_mode"], command_builder)
    elif host == "antigravity":
        expected_hooks = antigravity_hooks(runtime, receipt["python"], receipt["hook_mode"],
                                           command_builder, receipt["workspace"], receipt["home"])
    else:
        expected_hooks = dsh_hooks(runtime, receipt["python"], receipt["hook_mode"],
                                   command_builder, pinned_workspace, receipt.get("home"))
    if _json(target / mcp_path) != mcp:
        raise ContractError("existing installation MCP configuration was modified")
    if _json(target / hook_path) != expected_hooks:
        raise ContractError("existing installation Hook configuration was modified")
    mapping = payload_mapping(release["files"], host)
    managed = set(mapping) | {"runtime-release.json"}
    for name in managed - {"runtime-release.json", mcp_path, hook_path}:
        if observed["files"].get(name) != release["files"].get(mapping[name]):
            raise ContractError("existing installation managed file is missing or modified: " + name)
    if INSTALL_MANIFEST in observed["files"]:
        installation = _json(target / INSTALL_MANIFEST)
        if type(installation.get("schema")) is not int:
            raise ContractError("existing installation file manifest is invalid or managed files changed")
        expected = {"schema": installation.get("schema"), "plugin": "agent-learning", "release_id": release["release_id"],
                    "files": {name: observed["files"][name] for name in sorted(managed)}}
        if installation.get("schema") == INSTALL_SCHEMA:
            expected["target_host"] = host
        elif installation.get("schema") != 1 or host != "codex":
            raise ContractError("existing installation file manifest is invalid or managed files changed")
        if installation != expected:
            raise ContractError("existing installation file manifest is invalid or managed files changed")
        managed.add(INSTALL_MANIFEST)
    extras = sorted(set(observed["files"]) - managed)
    return {"target": target, "runtime_directory": runtime, "receipt": receipt, "release": release,
            "snapshot": observed, "managed": managed, "extra_files": extras, "target_host": host}


def write_installation_manifest(root, release_id, target_host="codex"):
    observed = snapshot(root)
    manifest = {"schema": INSTALL_SCHEMA, "plugin": "agent-learning", "release_id": release_id, "target_host": target_host,
                "files": {name: value for name, value in sorted(observed["files"].items()) if name != INSTALL_MANIFEST}}
    (root / INSTALL_MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def preserve_extra_files(previous, staging):
    """新清单先写，再复制额外文件，避免把个人文件归入插件管理范围。"""
    fresh = snapshot(staging)
    adopted = []
    for name in previous["snapshot"]["directories"]:
        if name in fresh["files"]:
            raise ContractError("user directory conflicts with a new managed plugin path: " + name)
    for name in previous["extra_files"]:
        path = Path(name)
        if (previous["target_host"] == "dsh" and name in DSH_HOST_ADOPTION and name in fresh["files"]):
            old = (previous["target"] / name).read_bytes()
            normalized = hashlib.sha256(old.replace(b"\r\n", b"\n")).hexdigest()
            if (previous["snapshot"]["files"][name] == fresh["files"][name]
                    or normalized in DSH_HOST_ADOPTION[name]):
                adopted.append(name)
                continue
        if (name in fresh["files"] or name in fresh["directories"]
                or any(parent.as_posix() in fresh["files"] for parent in path.parents if parent != Path("."))):
            raise ContractError("user file conflicts with a new managed plugin path: " + name)
    for name in previous["extra_files"]:
        if name in adopted:
            continue
        destination = staging / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(previous["target"] / name, destination)
    for name in previous["snapshot"]["directories"]:
        destination = staging / name
        if not destination.exists():
            destination.mkdir(parents=True, exist_ok=True)
    return adopted


@contextmanager
def installation_lock(target):
    """同目录安装与升级互斥；遗留锁不自动删除，避免抢占仍在运行的进程。"""
    target = safe_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(os.path.normcase(str(target)).encode("utf-8")).hexdigest()[:24]
    lock = target.parent / (".alp-install-" + key + ".lock")
    token = uuid.uuid4().hex
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ContractError("another installation is in progress or an interrupted installation lock needs review") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"schema": 1, "target": str(target), "pid": os.getpid(), "token": token}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        # 只解除仍属于本次调用的锁；外部替换的锁保留。
        try:
            if not _linked(lock) and _json(lock).get("token") == token:
                lock.unlink()
        except (OSError, ContractError):
            pass


def switch_installation(previous, staging):
    """同卷切换，旧副本移出宿主发现目录；失败尽可能恢复。"""
    target = safe_path(previous["target"])
    staging = safe_path(staging)
    if staging.parent != target.parent or not staging.name.startswith(".alp-staging-"):
        raise ContractError("refusing to publish an unexpected staging directory")
    if snapshot(target) != previous["snapshot"]:
        raise ContractError("installation destination changed during preparation; it was preserved")
    validate_previous_release(previous["runtime_directory"])
    from .installation_backups import allocate_backup, remove_backup_record
    backup = allocate_backup(previous)
    moved = False
    try:
        safe_path(backup)
        if backup.exists():
            raise ContractError("installation backup destination already exists")
        target.rename(backup)
        moved = True
        if snapshot(backup) != previous["snapshot"]:
            raise ContractError("installation destination changed during switching")
        if target.exists() or _linked(target):
            raise ContractError("installation destination appeared during switching; backup was preserved")
        staging.rename(target)
    except (OSError, ContractError) as error:
        if moved and not target.exists() and not _linked(target):
            try:
                backup.rename(target)
                remove_backup_record(backup)
            except OSError:
                raise ContractError("installation switch failed; restore the retained backup at " + str(backup)) from None
        if moved and backup.exists():
            raise ContractError("installation switch failed; existing destination and backup were preserved at " + str(backup)) from None
        if not moved:
            remove_backup_record(backup)
        if isinstance(error, ContractError):
            raise
        raise ContractError("installation switch failed; previous installation was preserved or restored") from None
    return backup
