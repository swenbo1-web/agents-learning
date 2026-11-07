"""Explicit project file-metadata roots; these links never grant execution access.

Project identities and existing knowledge remain separate. Configuration is the
authority; the database is never used to discover additional permitted roots.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile


MAX_LINKED_ROOTS = 8
MAX_LINKED_PROJECTS = 128
MAX_CONFIG_BYTES = 1024 * 1024
_SENSITIVE_PARTS = {".git", ".ssh", ".aws", ".gnupg", ".gcloud", ".azure", ".codex",
                    ".gemini", "secrets", "credentials", "runtime-releases"}


def _error(message):
    from .runtime import ContractError
    raise ContractError(message)


def _path_key(path):
    return os.path.normcase(os.path.normpath(str(path)))


def _absolute_path(value):
    if (not isinstance(value, str) or not value or value != value.strip() or len(value) > 2000
            or any(ord(char) < 32 for char in value)):
        _error("workspace root must be a bounded absolute path")
    path = Path(value)
    if (not path.is_absolute() or ".." in path.parts
            or (os.name == "nt" and (str(path).startswith("\\\\")
                                      or ":" in str(path)[len(path.drive):]))):
        _error("workspace root must be a local absolute path without traversal")
    if _path_key(path) != _path_key(value):
        _error("workspace root is not normalized")
    return path


def validate_linked_workspaces(value):
    """Validate the bounded JSON contract without accessing any linked root."""
    if not isinstance(value, dict) or len(value) > MAX_LINKED_PROJECTS:
        _error("linked_workspaces must be a bounded project-to-root-array object")
    result = {}
    for project, roots in value.items():
        if (not isinstance(project, str) or not project.strip() or project != project.strip()
                or len(project) > 128 or any(ord(char) < 32 for char in project)):
            _error("linked_workspaces project ID must be a bounded nonempty string")
        if not isinstance(roots, list) or len(roots) > MAX_LINKED_ROOTS:
            _error("linked_workspaces permits at most 8 roots per project")
        normalized, seen = [], set()
        for root in roots:
            path = _absolute_path(root)
            key = _path_key(path)
            if key in seen:
                _error("linked_workspaces contains duplicate roots")
            normalized.append(str(path))
            seen.add(key)
        result[project] = normalized
    return result


def _version(roots):
    return hashlib.sha256(json.dumps(roots, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def initialize_workspace_links(runtime, config_path, supplied, *, from_disk):
    """Keep the origin and startup approval snapshot distinct from mutable config."""
    runtime.config["linked_workspaces"] = validate_linked_workspaces(runtime.config["linked_workspaces"])
    runtime._workspace_links_path = Path(config_path).absolute()
    runtime._workspace_links_from_disk = from_disk
    runtime._workspace_links_snapshot_verified = True
    runtime._workspace_links_loaded = json.loads(json.dumps(runtime.config["linked_workspaces"]))
    runtime._workspace_links_disk_cache = dict(supplied)
    # The initial Runtime read and this initializer need not be one atomic
    # operation. Re-read on the first use before trusting any loaded roots.
    runtime._workspace_links_disk_signature = ("not_checked",) if from_disk else None


def _config_signature(path):
    try:
        value = path.stat()
        return (value.st_mtime_ns, value.st_size, value.st_ctime_ns, value.st_ino)
    except FileNotFoundError:
        return None
    except OSError:
        return ("unavailable",)


def _has_link(path):
    """Check each existing component before resolving a junction/symlink."""
    for part in (path, *path.parents):
        try:
            value = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
            return True
    return False


def _read_config(path):
    if _has_link(path):
        _error("workspace configuration must not follow symbolic links or reparse points")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return {}
    if len(raw) > MAX_CONFIG_BYTES:
        _error("workspace configuration exceeds byte limit")
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        _error("workspace configuration is not valid JSON: " + type(exc).__name__)
    if not isinstance(value, dict):
        _error("workspace configuration must be a JSON object")
    normalized = validate_linked_workspaces(value.get("linked_workspaces", {}))
    if "linked_workspaces" in value:
        value["linked_workspaces"] = normalized
    return value


def _current_config(runtime):
    if not runtime._workspace_links_from_disk:
        return runtime.config
    path = runtime._workspace_links_path
    signature = _config_signature(path)
    if signature != runtime._workspace_links_disk_signature:
        # Read once per observed config change. Do not inspect any former roots.
        supplied = _read_config(path)
        if _config_signature(path) != signature:
            _error("workspace configuration changed while reading; reload and retry")
        runtime._workspace_links_disk_cache = supplied
        runtime._workspace_links_disk_signature = signature
    return runtime._workspace_links_disk_cache


def _within(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _sensitive_root(path, runtime):
    from .history_sources import _sensitive
    if _sensitive(path) or any(part.casefold() in _SENSITIVE_PARTS for part in path.parts):
        return True
    if path == Path(path.anchor) or _within(Path.home(), path):
        return True
    private = [runtime.home]
    for name in ("CODEX_HOME", "ALP_RUNTIME_STORE"):
        if os.environ.get(name):
            private.append(Path(os.environ[name]).absolute())
    return any(_within(path, item) or _within(item, path) for item in private)


def _checked_root(value, runtime, *, primary=None, others=()):
    from .work_files import checkout_root
    path = _absolute_path(value)
    if _sensitive_root(path, runtime):
        _error("workspace root is sensitive or too broad")
    if _has_link(path):
        _error("workspace root must not follow symbolic links or reparse points")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        _error("workspace root must be an existing directory")
    if _path_key(path) != _path_key(resolved):
        _error("workspace root must retain its canonical path")
    if checkout_root(resolved) != resolved:
        _error("workspace root must be the exact checkout root")
    for other in ([primary] if primary else []) + list(others):
        if _within(resolved, other) or _within(other, resolved):
            _error("workspace roots must not overlap")
    return resolved


def _workspace_project(runtime, workspace, config=None):
    """Read-only equivalent of Runtime.project, without registering or merging."""
    from .runtime import digest
    from .work_files import checkout_root
    resolved = Path(workspace).resolve()
    path = _path_key(resolved)
    aliases = {}
    aliases_config = (runtime.config if config is None else config).get("project_aliases", {})
    if not isinstance(aliases_config, dict) or not all(isinstance(key, str) and isinstance(value, str)
                                                     for key, value in aliases_config.items()):
        _error("project_aliases must map workspace paths to project IDs")
    for key, project in aliases_config.items():
        normalized = _path_key(Path(key).resolve())
        if normalized in aliases and aliases[normalized] != project:
            _error("project alias collision after normalization")
        aliases[normalized] = project
    alias = next((project for key, project in sorted(aliases.items(), key=lambda pair: len(pair[0]), reverse=True)
                  if path == key or path.startswith(key.rstrip(os.sep) + os.sep)), None)
    registered = runtime.db.execute(
        "SELECT project FROM project_paths WHERE path=? OR substr(?,1,length(path)+1)=path||? "
        "ORDER BY length(path) DESC LIMIT 1", (path, path, os.sep)).fetchone()
    root = checkout_root(resolved)
    root_path = _path_key(root)
    git_root = (root / ".git").exists()
    exact = runtime.db.execute("SELECT project FROM project_paths WHERE path=?", (root_path,)).fetchone()
    return alias or ((exact["project"] if exact else digest(root_path)[:24]) if git_root
                     else registered["project"] if registered else digest(root_path)[:24])


def _assert_project(runtime, project, workspace, config=None):
    if not workspace or not Path(workspace).is_absolute():
        _error("an explicit absolute current workspace is required")
    if (not isinstance(project, str) or not runtime.db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone()
            or _workspace_project(runtime, workspace, config) != project):
        _error("workspace project identity does not match the current project")


def workspace_links_status(runtime, project, workspace):
    """Metadata only; a revoked root is discarded before touching its path."""
    from .work_files import checkout_root, checkout_key
    roots, rejected = [], []
    primary = checkout_root(workspace) if workspace else None
    if primary:
        roots.append({"path": str(primary), "checkout_key": checkout_key(primary), "primary": True})
    loaded = runtime._workspace_links_loaded.get(project, [])
    snapshot_verified = getattr(runtime, "_workspace_links_snapshot_verified", False)
    reload_required = False
    try:
        supplied = _current_config(runtime)
        # Disk configurations were already validated on load/change. Embedded
        # config is mutable, so it still receives the full contract check.
        linked = (supplied.get("linked_workspaces", {}) if runtime._workspace_links_from_disk
                  else validate_linked_workspaces(supplied.get("linked_workspaces", {})))
        current = linked.get(project, [])
        reload_required = (_version(current) != _version(loaded)) if snapshot_verified else None
        if workspace and (loaded or current) and reload_required is False:
            _assert_project(runtime, project, workspace, supplied)
    except (OSError, ValueError, RuntimeError):
        current, reload_required = [], True
    # Do not stat or resolve previously approved roots after a config change.
    if workspace and reload_required is False:
        for value in loaded:
            try:
                root = _checked_root(value, runtime, primary=primary, others=[Path(item["path"]) for item in roots[1:]])
                roots.append({"path": str(root), "checkout_key": checkout_key(root), "primary": False})
            except (OSError, ValueError, RuntimeError):
                rejected.append({"reason": "invalid_linked_root"})
    return {"roots": roots, "linked_workspaces": list(current), "reload_required": reload_required,
            "config_version": _version(current), "execution_permission": False,
            "rejected_roots": rejected,
            "workspace_verified": bool(workspace) and snapshot_verified and reload_required is False}


def file_workspace_roots(runtime, project, workspace):
    return workspace_links_status(runtime, project, workspace)["roots"]


def current_file_roots(runtime, project, workspace):
    return file_workspace_roots(runtime, project, workspace)


@contextmanager
def open_workspace_link_view(home=None, *, host="codex", host_instance="default"):
    """CLI configuration reads use an existing database; never initialize it."""
    from .runtime import Runtime, DEFAULTS
    default_home = (Path(os.environ["LOCALAPPDATA"]) / "AgentLearning"
                    if os.environ.get("LOCALAPPDATA") else Path.home() / ".local/share/agent-learning")
    selected_home = Path(home or os.environ.get("ALP_HOME") or default_home).absolute()
    database = selected_home / "runtime.sqlite3"
    if not database.is_file():
        _error("workspace project is not registered; complete project setup before linking roots")
    db = sqlite3.connect(database.as_uri() + "?mode=ro", timeout=1.5, isolation_level=None, uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA user_version").fetchone()[0] not in set(range(1, 26)):
            _error("workspace commands require an existing supported project database")
        path = Path(os.environ.get("ALP_CONFIG", selected_home / "config.json")).absolute()
        supplied = _read_config(path)
        view = Runtime.__new__(Runtime)
        view.home, view.db = selected_home, db
        view.host, view.host_instance, view.host_scope = host, host_instance, host + ":" + host_instance
        view.config = json.loads(json.dumps(DEFAULTS))
        view.config.update(supplied)
        initialize_workspace_links(view, path, supplied, from_disk=True)
        yield view
    finally:
        db.close()


def workspace_project(runtime, workspace):
    project = _workspace_project(runtime, workspace)
    _assert_project(runtime, project, workspace)
    return project


@contextmanager
def _config_lock(path):
    if _has_link(path):
        _error("workspace config lock must not follow links")
    stream = path.open("a+b")
    acquired = False
    try:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (OSError, BlockingIOError):
            _error("workspace configuration is being edited; reload and retry")
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _atomic_config(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(raw) > MAX_CONFIG_BYTES:
        _error("workspace configuration exceeds byte limit")
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def set_workspace_link(runtime, project, workspace, root, *, expected_version, confirm=False, remove=False):
    """Explicit operator CLI boundary. Save links only, with project-scoped CAS."""
    from .work_files import checkout_root
    if not confirm:
        _error("workspace link changes require --confirm")
    if not runtime._workspace_links_from_disk or not runtime._workspace_links_snapshot_verified:
        _error("workspace link changes require a file-backed configuration")
    _assert_project(runtime, project, workspace)
    if not isinstance(expected_version, str) or len(expected_version) != 64:
        _error("workspace link changes require the current config_version")
    path = runtime._workspace_links_path
    if not path.parent.is_dir() or _has_link(path):
        _error("workspace configuration parent must be an existing directory without links")
    with _config_lock(path.with_name(path.name + ".workspace-links.lock")):
        signature = _config_signature(path)
        supplied = _read_config(path)
        if _config_signature(path) != signature:
            _error("workspace configuration changed while reading; reload and retry")
        _assert_project(runtime, project, workspace, supplied)
        linked = validate_linked_workspaces(supplied.get("linked_workspaces", {}))
        previous = linked.get(project, [])
        if _version(previous) != expected_version:
            _error("workspace configuration changed; reload workspace-list before retrying")
        # Revocation is lexical: a missing/replaced/symlinked former root can
        # still be removed, without observing anything beneath that root.
        selected = str(_absolute_path(root))
        if not remove:
            _checked_root(selected, runtime, primary=checkout_root(workspace),
                          others=[Path(value) for value in previous if _path_key(value) != _path_key(selected)])
        present = any(_path_key(value) == _path_key(selected) for value in previous)
        changed = present if remove else not present
        updated = ([value for value in previous if _path_key(value) != _path_key(selected)] if remove
                   else previous + ([] if present else [selected]))
        if changed:
            if updated:
                linked[project] = updated
            else:
                linked.pop(project, None)
            validate_linked_workspaces(linked)
            supplied["linked_workspaces"] = linked
            # Preserve unrelated settings read inside the lock; detect writers
            # that do not use this lock before replacing their configuration.
            if _config_signature(path) != signature:
                _error("workspace configuration changed while saving; reload and retry")
            _atomic_config(path, supplied)
    return {"status": ("unlinked" if remove else "linked") if changed else "unchanged",
            "project": project, "config_version": _version(updated), "reload_required": changed,
            "execution_permission": False}
