"""Read-only, thread-safe lifetime checks for a panel-opened browser."""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import stat
import threading

from .host_sessions import host_scope_key
from .install_update import safe_path
from .runtime import ContractError, encode


MAX_IDENTITY_BYTES = 2 * 1024 * 1024


def _database_identity(database):
    path = safe_path(database)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ContractError("panel browser requires an existing regular database")
    return path, (info.st_dev, info.st_ino, getattr(info, "st_birthtime_ns", None))


def _identity_files(root, host):
    names = ("runtime-release.json", "installation-manifest.json",
             ".codex-plugin/plugin.json" if host == "codex" else "plugin.json")
    result = {}
    receipt = None
    for name in names:
        path = safe_path(root / name)
        if not path.is_file() or path.stat().st_size > MAX_IDENTITY_BYTES:
            raise ContractError("panel installation identity is unavailable")
        with path.open("rb") as stream:
            raw = stream.read(MAX_IDENTITY_BYTES + 1)
        if len(raw) > MAX_IDENTITY_BYTES:
            raise ContractError("panel installation identity exceeds budget")
        result[name] = hashlib.sha256(raw).hexdigest()
        if name == "runtime-release.json":
            receipt = json.loads(raw)
            if not isinstance(receipt, dict):
                raise ContractError("panel installation receipt is invalid")
    return result, receipt


def make_panel_guard(installation, binding, database, host_scope, session_epoch, governance_epoch):
    """Freeze installation/database identity; never reuse Runtime's connection.

    A detected replacement, missing identity, account change or check failure
    revokes this browser permanently. A newly opened panel creates a new guard.
    """
    if (not isinstance(binding, dict) or not isinstance(host_scope, str)
            or any(type(value) is not int or value < 0 for value in (session_epoch, governance_epoch))):
        raise ContractError("invalid panel browser lifetime binding")
    host, separator, instance = host_scope.partition(":")
    if not separator or host not in {"codex", "antigravity"} or host_scope_key(host, instance) != host_scope:
        raise ContractError("invalid panel browser host scope")
    root = safe_path(installation)
    database, database_identity = _database_identity(database)
    if not root.is_dir() or str(root) != binding.get("installation"):
        raise ContractError("panel browser installation does not match")
    expected_files, receipt = _identity_files(root, host)
    expected_binding = dict(binding)

    def receipt_matches(value):
        workspace = value.get("panel_workspace") if host == "codex" else value.get("workspace")
        return (value.get("target_host", "codex") == host
                and value.get("release_id") == expected_binding.get("release_id")
                and workspace == expected_binding.get("workspace")
                and hashlib.sha256(encode(value).encode("utf-8")).hexdigest() == expected_binding.get("receipt_digest"))

    if not receipt_matches(receipt):
        raise ContractError("panel browser installation identity changed")
    lock = threading.Lock()
    revoked = False

    def guard():
        nonlocal revoked
        with lock:
            if revoked:
                return False
            try:
                if safe_path(installation) != root or not root.is_dir():
                    raise ContractError("panel installation is unavailable")
                observed, current = _identity_files(root, host)
                if observed != expected_files or not receipt_matches(current):
                    raise ContractError("panel installation changed")
                workspace = safe_path(expected_binding["workspace"])
                if not workspace.is_dir():
                    raise ContractError("panel project is unavailable")
                if _database_identity(database) != (database, database_identity):
                    raise ContractError("panel database changed")
                with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1,
                                                       isolation_level=None)) as connection:
                    connection.execute("PRAGMA query_only=ON")
                    connection.execute("BEGIN")
                    current_session = connection.execute("SELECT epoch FROM host_scopes WHERE scope=?", (host_scope,)).fetchone()
                    current_governance = connection.execute("SELECT epoch FROM host_state WHERE id=1").fetchone()
                    if (current_session != (session_epoch,) or current_governance != (governance_epoch,)):
                        raise ContractError("panel browser account context expired")
                    connection.execute("ROLLBACK")
                if _database_identity(database) != (database, database_identity):
                    raise ContractError("panel database changed during check")
                return True
            except Exception:
                revoked = True
                return False

    return guard
