"""原子导出插件资源，MCP 与 Hook 使用同一稳定发布副本。"""
from __future__ import annotations

import base64
from importlib import resources
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

from . import __version__
from .release import MANIFEST_NAME, ReleaseError, manifest_for, validate_release_root
from .runtime import ContractError
from .install_update import (installation_lock, inspect_installation, preserve_extra_files,
                             safe_path, switch_installation, write_installation_manifest,
                             TARGET_HOSTS, antigravity_hooks, mcp_document, payload_mapping)


RESOURCE_DIRS = ("scripts", "hooks", "skills", ".codex-plugin", ".agents", "host_resources")
RESOURCE_FILES = ("README.md", "LICENSE", "config.example.json", ".mcp.json")
REQUIRED_FILES = (*RESOURCE_FILES, "scripts/learning.py", "hooks/dispatch.py", "hooks/hooks.json",
                  "skills/learning-worker/SKILL.md", "skills/learning-admin/SKILL.md",
                  ".codex-plugin/plugin.json", ".agents/plugins/marketplace.json")
ANTIGRAVITY_FILES = ("plugin.json", "mcp_config.json", "hooks.json", "rules/learning.md",
                     "skills/learning-worker/SKILL.md", "skills/learning-admin/SKILL.md")


def _resource_root():
    source = Path(__file__).resolve().parents[1]
    if (source / MANIFEST_NAME).is_file():
        validate_release_root(source)
        return source
    if ((source / "pyproject.toml").is_file()
            or ((source / ".codex-plugin/plugin.json").is_file() and (source / "scripts/learning.py").is_file())):
        return source
    # 包内资源跟随实际导入的版本，不猜测 prefix/user/target 安装目录。
    bundled = resources.files("agent_learning").joinpath("_plugin_resources")
    if bundled.is_dir():
        return bundled
    raise ContractError("plugin export resources are missing; use the complete source checkout or reinstall the wheel")


def _validate_antigravity_resources(root):
    for name in ANTIGRAVITY_FILES:
        if not root.joinpath(name).is_file():
            raise ContractError("Antigravity plugin resource is missing: " + name)
    manifest = json.loads(root.joinpath("plugin.json").read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or set(manifest) != {"name", "description"}
            or manifest.get("name") != "agent-learning" or not isinstance(manifest.get("description"), str)):
        raise ContractError("Antigravity plugin manifest is invalid")
    hooks = json.loads(root.joinpath("hooks.json").read_text(encoding="utf-8"))
    if (not isinstance(hooks, dict) or set(hooks) != {"agent-learning"}
            or not isinstance(hooks["agent-learning"], dict)):
        raise ContractError("Antigravity plugin Hook resource is invalid")
    events = hooks["agent-learning"]
    if (not {"PreInvocation", "PostToolUse", "Stop"}.issubset(events)
            or set(events) - {"PreInvocation", "PostToolUse", "PreToolUse", "Stop"}):
        raise ContractError("Antigravity plugin Hook events are invalid")
    for event, handlers in events.items():
        if not isinstance(handlers, list) or not handlers:
            raise ContractError("Antigravity plugin Hook handlers are invalid")
        for item in handlers:
            if not isinstance(item, dict):
                raise ContractError("Antigravity plugin Hook handler is invalid")
            children = item.get("hooks") if event in ("PreToolUse", "PostToolUse") else [item]
            if not isinstance(children, list) or not children:
                raise ContractError("Antigravity plugin Hook handlers are invalid")
            for handler in children:
                if (not isinstance(handler, dict) or handler.get("type") != "command"
                        or not isinstance(handler.get("command"), str) or not handler["command"]
                        or set(handler) - {"type", "command", "timeout"}
                        or type(handler.get("timeout")) is not int or not 1 <= handler["timeout"] <= 30):
                    raise ContractError("Antigravity plugin Hook handler contract is invalid")
    if not isinstance(json.loads(root.joinpath("mcp_config.json").read_text(encoding="utf-8")), dict):
        raise ContractError("Antigravity plugin MCP resource is invalid")
    for name in ("skills/learning-worker/SKILL.md", "skills/learning-admin/SKILL.md"):
        if not root.joinpath(name).read_text(encoding="utf-8").startswith("---\n"):
            raise ContractError("Antigravity plugin skill resource is invalid: " + name)


def _validate_resources(root, host="codex", *, bundle=False):
    try:
        if host == "antigravity" and not bundle:
            _validate_antigravity_resources(root)
            if not root.joinpath("hooks/antigravity_dispatch.py").is_file():
                raise ContractError("Antigravity Hook dispatcher is missing")
            return
        for name in REQUIRED_FILES:
            if not root.joinpath(name).is_file():
                raise ContractError("plugin resource is missing: " + name)
        manifest = json.loads(root.joinpath(".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        if (manifest.get("name") != "agent-learning" or manifest.get("version") != __version__
                or manifest.get("skills") != "./skills/" or manifest.get("hooks") != "./hooks/hooks.json"
                or manifest.get("mcpServers") != "./.mcp.json"):
            raise ContractError("plugin resources do not match the installed package version or entry contract")
        hooks = json.loads(root.joinpath("hooks/hooks.json").read_text(encoding="utf-8"))
        if not isinstance(hooks.get("hooks"), dict) or not hooks["hooks"]:
            raise ContractError("plugin hook resource is invalid")
        for groups in hooks["hooks"].values():
            for group in groups:
                for handler in group["hooks"]:
                    if handler.get("type") != "command":
                        raise ContractError("plugin hook handler is invalid")
        for name in (".mcp.json", ".agents/plugins/marketplace.json", "config.example.json"):
            if not isinstance(json.loads(root.joinpath(name).read_text(encoding="utf-8")), dict):
                raise ContractError("plugin JSON resource is invalid: " + name)
        for name in ("skills/learning-worker/SKILL.md", "skills/learning-admin/SKILL.md"):
            if not root.joinpath(name).read_text(encoding="utf-8").startswith("---\n"):
                raise ContractError("plugin skill resource is invalid: " + name)
        if bundle:
            _validate_antigravity_resources(root.joinpath("host_resources/antigravity"))
            if not root.joinpath("hooks/antigravity_dispatch.py").is_file():
                raise ContractError("Antigravity Hook dispatcher is missing")
    except ContractError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
        raise ContractError("plugin export resources could not be validated") from None


def _copy_resource(source, target):
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            if child.name != "__pycache__" and not child.name.endswith(".pyc"):
                _copy_resource(child, target / child.name)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


def _copy_payload(source, package, target):
    shutil.copytree(package, target / "agent_learning",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_plugin_resources"))
    for name in (*RESOURCE_DIRS, *RESOURCE_FILES):
        _copy_resource(source.joinpath(name), target / name)


def _default_store():
    configured = os.environ.get("ALP_RUNTIME_STORE")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "AgentLearning/runtime-releases"
    return Path.home() / ".local/share/agent-learning/runtime-releases"


def _overlaps(left, right):
    return left == right or left in right.parents or right in left.parents


def _cleanup_owned(staging, parent, identity=None):
    # 仅删除本次 mkdtemp 返回且仍位于预期父目录的临时目录。
    if staging is not None:
        if not staging.exists():
            return
        resolved = safe_path(staging)
        if resolved.parent != parent.resolve() or not staging.name.startswith(".alp-staging-"):
            raise ContractError("refusing to remove an unexpected staging path")
        if identity is not None:
            observed = staging.stat()
            if (observed.st_dev, observed.st_ino) != identity:
                raise ContractError("refusing to remove a staging directory replaced by another process")
        shutil.rmtree(staging)


def _release(source, package, store):
    store.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".alp-staging-", dir=store))
    stat = staging.stat()
    staging_identity = (stat.st_dev, stat.st_ino)
    try:
        _copy_payload(source, package, staging)
        manifest = manifest_for(staging)
        (staging / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        validate_release_root(staging)
        target = safe_path(store / manifest["release_id"])
        if target.exists():
            if validate_release_root(target) != manifest:
                raise ContractError("existing runtime release has a different identity")
        else:
            try:
                staging.rename(target)
                staging = None
            except OSError:
                if not target.exists() or validate_release_root(target) != manifest:
                    raise
        return target, manifest
    finally:
        _cleanup_owned(staging, store, staging_identity)


def _hook_commands(python, script_path, event_name, hook_mode, *, host="codex", workspace=None, home=None):
    arguments = [str(script_path), "--event", event_name]
    if host == "antigravity":
        arguments = ["-B", *arguments, "--workspace", workspace, "--home", home]
    if hook_mode is not None:
        arguments.extend(["--hook-mode", hook_mode])
    command = " ".join(shlex.quote(value) for value in [python, *arguments])
    script = "& " + " ".join("'" + value.replace("'", "''") + "'" for value in [python, *arguments])
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    windows = "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded
    return command, windows


def install(destination, runtime_store=None, hook_mode=None, *, upgrade=False, expected_release_id=None,
            host="codex", workspace=None, home=None):
    """导出或显式覆盖已验证的插件目录；知识库与用户宿主配置不参与切换。"""
    if not isinstance(upgrade, bool):
        raise ContractError("upgrade must be a boolean")
    if expected_release_id is not None and (not upgrade or not isinstance(expected_release_id, str)):
        raise ContractError("expected release identity requires an explicit upgrade")
    if hook_mode not in (None, "observe", "enforce"):
        raise ContractError("hook mode must be observe or enforce")
    if host not in TARGET_HOSTS:
        raise ContractError("target host must be codex or antigravity")
    if host == "codex" and (workspace is not None or home is not None):
        raise ContractError("fixed workspace and home bindings apply to the Antigravity installation")
    source = _resource_root()
    _validate_resources(source, bundle=True)
    package = Path(__file__).resolve().parent
    target = safe_path(destination)
    if target.exists() and not upgrade:
        raise ContractError("installation destination already exists; choose a new directory or use --upgrade")
    if upgrade and not target.exists():
        raise ContractError("upgrade requires an existing plugin installation directory")
    # 在写入 store 前确定旧身份；锁内会再次核对以覆盖并发调用。
    previous = inspect_installation(target, _hook_commands, expected_release_id, target_host=host) if upgrade else None
    if previous is not None:
        if tuple(map(int, previous["release"]["version"].split("."))) > tuple(map(int, __version__.split("."))):
            raise ContractError("refusing to downgrade a newer plugin installation")
        if hook_mode is None:
            hook_mode = previous["receipt"]["hook_mode"]
        if runtime_store is None:
            runtime_store = previous["runtime_directory"].parent
        if host == "antigravity":
            workspace = workspace if workspace is not None else previous["receipt"]["workspace"]
            home = home if home is not None else previous["receipt"]["home"]
    if host == "antigravity":
        if workspace is None:
            raise ContractError("Antigravity export requires an explicit workspace")
        workspace = safe_path(workspace)
        if not workspace.is_dir():
            raise ContractError("Antigravity workspace must be an existing directory")
        default_home = (Path(os.environ["LOCALAPPDATA"]) / "AgentLearning" if os.environ.get("LOCALAPPDATA")
                        else Path.home() / ".local/share/agent-learning")
        home = safe_path(home if home is not None else os.environ.get("ALP_HOME") or default_home)
        if any(path.exists() and not path.is_dir() for path in (home, *home.parents)):
            raise ContractError("Antigravity knowledge home and its existing parents must be directories")
        hook_mode = hook_mode or "observe"
    store = safe_path(runtime_store if runtime_store is not None else _default_store())
    protected = [package]
    source_release = isinstance(source, Path) and (source / MANIFEST_NAME).is_file()
    if isinstance(source, Path):
        protected.append(source.resolve())
    # 从固定release再次导出时，允许复用包含当前副本的store；仍禁止写入副本内部。
    store_conflict = any(_overlaps(store, path) and not
                         (source_release and store in source.resolve().parents and source.resolve() in (path, *path.parents))
                         for path in protected)
    if any(_overlaps(target, path) for path in protected) or store_conflict or _overlaps(target, store):
        raise ContractError("export and runtime store must use separate directories outside the source and installed package")
    if host == "antigravity":
        # 默认 home 是数据容器，runtime-releases 是其中唯一保留的运行副本目录。
        # 数据本身仍不能落入运行副本、源码或导出目录。
        reserved_store = home / "runtime-releases"
        source_in_reserved_store = (source_release and source.parent == reserved_store
                                    and source.name == validate_release_root(source)["release_id"])
        home_source_conflict = any(
            _overlaps(home, path) and not
            (source_in_reserved_store and source in (path, *path.parents))
            for path in protected)
        if (workspace == target or target in workspace.parents or workspace == store or store in workspace.parents
                or _overlaps(home, target) or home_source_conflict
                or (_overlaps(home, store) and store != reserved_store)):
            raise ContractError("Antigravity bindings must keep project and knowledge data outside export and runtime directories")
    try:
        with installation_lock(target):
            if previous is not None:
                locked = inspect_installation(target, _hook_commands, expected_release_id, target_host=host)
                if locked["snapshot"] != previous["snapshot"]:
                    raise ContractError("installation changed before update lock was acquired")
            elif target.exists():
                raise ContractError("installation destination appeared during export; it was preserved")
            runtime_directory, release = _release(source, package, store)
            staging = Path(tempfile.mkdtemp(prefix=".alp-staging-", dir=target.parent))
            stat = staging.stat()
            staging_identity = (stat.st_dev, stat.st_ino)
            try:
                # 宿主资源也来自刚验证的发布副本，避免技能与运行代码分裂。
                if host == "codex":
                    _copy_payload(runtime_directory, runtime_directory / "agent_learning", staging)
                else:
                    for name, original in payload_mapping(release["files"], host).items():
                        _copy_resource(runtime_directory / original, staging / name)
                mcp = mcp_document(runtime_directory, sys.executable, host,
                                   str(workspace) if workspace is not None else None,
                                   str(home) if home is not None else None)
                mcp_file = staging / (".mcp.json" if host == "codex" else "mcp_config.json")
                mcp_file.write_text(json.dumps(mcp, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                hook_file = staging / ("hooks/hooks.json" if host == "codex" else "hooks.json")
                if host == "antigravity":
                    hooks = antigravity_hooks(runtime_directory, sys.executable, hook_mode, _hook_commands,
                                              str(workspace), str(home))
                else:
                    hooks = json.loads(hook_file.read_text(encoding="utf-8"))
                    for event_name, groups in hooks["hooks"].items():
                        command, windows = _hook_commands(sys.executable, runtime_directory / "hooks/dispatch.py", event_name, hook_mode)
                        for group in groups:
                            for handler in group["hooks"]:
                                handler["command"] = command
                                handler.pop("command_windows", None)
                                handler["commandWindows"] = windows
                hook_file.write_text(json.dumps(hooks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                receipt = {"runtime_directory": str(runtime_directory),
                    "release_id": release["release_id"], "version": release["version"],
                    "protocol_version": release["protocol_version"], "content_digest": release["content_digest"],
                    "python": sys.executable, "hook_mode": hook_mode, "target_host": host}
                if host == "antigravity":
                    receipt.update(workspace=str(workspace), home=str(home))
                (staging / "runtime-release.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                _validate_resources(staging, host)
                write_installation_manifest(staging, release["release_id"], host)
                backup = None
                if previous is not None:
                    preserve_extra_files(previous, staging)
                    backup = switch_installation(previous, staging)
                else:
                    if target.exists():
                        raise ContractError("installation destination appeared during export; it was preserved")
                    staging.rename(target)
                staging = None
            finally:
                _cleanup_owned(staging, target.parent, staging_identity)
    except (OSError, ReleaseError):
        raise ContractError("plugin export could not complete; final destination was not published") from None
    result = {"status": "updated" if upgrade else "exported", "plugin_directory": str(target), "python": sys.executable,
            "runtime_directory": str(runtime_directory), "release_id": release["release_id"],
            "version": __version__, "hook_mode": hook_mode, "target_host": host,
            "next_step": "将此目录加入本地插件源并安装；保留固定运行目录及解释器，在Codex界面审阅并信任Hooks。"}
    if host == "antigravity":
        result.update(workspace=str(workspace), home=str(home),
                      next_step="在Antigravity中加载此原生插件目录，核对MCP与Hooks已启用；固定项目安装不要全局用于其他项目。")
    if previous is not None:
        result.update({"previous_release_id": previous["release"]["release_id"], "backup_directory": str(backup),
                       "preserved_extra_files": previous["extra_files"], "knowledge_and_config": "external_untouched",
                       "host_reload_required": True, "hook_review_required": True,
                       "next_step": "重新加载或重新安装宿主缓存中的插件，并审阅Hook路径变化；旧目录与固定副本保留，可用于恢复。"})
    return result
