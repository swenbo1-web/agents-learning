"""Interpreter preflight and installation diagnostics; no Runtime or model calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .install_update import python_executable
from .runtime import ContractError


_PROBE = """import json,sys,sqlite3,ssl
c=sqlite3.connect(':memory:')
c.execute('CREATE VIRTUAL TABLE probe USING fts5(content)')
c.close()
print(json.dumps({'kind':'alp-python-check','version':list(sys.version_info[:3]),'base_prefix':sys.base_prefix}))
"""


def interpreter_risks(value):
    """Recognize known disposable locations, not every directory named cache."""
    locations = [str(value)]
    try:
        locations.append(str(Path(value).resolve()))
    except (OSError, ValueError, RuntimeError):
        pass
    temporary = {tempfile.gettempdir(), *(os.environ.get(key, "") for key in ("TMP", "TEMP", "TMPDIR"))}
    reasons = set()
    for location in locations:
        normalized = "/" + location.replace("\\", "/").strip("/").casefold() + "/"
        if ("/.cache/codex-runtimes/" in normalized or "/codex-runtime-install-" in normalized
                or "/openai/codex/runtimes/" in normalized):
            reasons.add("codex_runtime_cache")
        if "/.codex/plugins/cache/" in normalized:
            reasons.add("host_plugin_cache")
        if "/uv/cache/" in normalized or "/.cache/uv/" in normalized:
            reasons.add("package_cache")
        for root in temporary:
            root = root.replace("\\", "/").rstrip("/").casefold()
            candidate = location.replace("\\", "/").casefold()
            if len(root) > 2 and (candidate == root or candidate.startswith(root + "/")):
                reasons.add("temporary_directory")
    return sorted(reasons)


def python_health(value, *, probe=False):
    try:
        path = python_executable(str(value), require_available=False)
    except ContractError:
        return {"state": "invalid", "available": False, "usable": False, "risks": [], "probed": False}
    report = {"path": str(path), "available": path.is_file(), "usable": False,
              "risks": interpreter_risks(path), "probed": False}
    if not report["available"]:
        return {**report, "state": "missing"}
    if probe:
        try:
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            result = subprocess.run([str(path), "-I", "-S", "-B", "-c", _PROBE],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=10, check=False, **options)
            if result.returncode or len(result.stdout) > 4096:
                raise ValueError
            body = json.loads(result.stdout)
            version = body["version"]
            if (body.get("kind") != "alp-python-check" or not isinstance(version, list) or len(version) != 3
                    or any(type(part) is not int for part in version)):
                raise ValueError
            report.update(probed=True, version=version)
            if version < [3, 10, 0]:
                return {**report, "state": "unsupported_version"}
            if isinstance(body.get("base_prefix"), str):
                report["risks"] = sorted(set(report["risks"]) | set(interpreter_risks(body["base_prefix"])))
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError):
            return {**report, "state": "probe_failed"}
    return {**report, "state": "volatile" if report["risks"] else "healthy", "usable": True}


def select_python(value=None, *, allow_volatile=False):
    if type(allow_volatile) is not bool:
        raise ContractError("allow_volatile_python must be boolean")
    report = python_health(sys.executable if value is None else value, probe=True)
    if not report["usable"]:
        raise ContractError("Python is unavailable or incompatible (" + report["state"]
                            + "); choose an absolute Python 3.10+ path with sqlite3, FTS5 and ssl using --python")
    if report["risks"] and not allow_volatile:
        raise ContractError("Python is in a disposable cache or temporary directory ("
                            + ", ".join(report["risks"]) + "); use --python with an independent interpreter, "
                            "or explicitly accept the risk with --allow-volatile-python")
    return report["path"], report


def installation_check(destination):
    from .install import _hook_commands
    from .install_update import inspect_installation
    inspected = inspect_installation(destination, _hook_commands)
    receipt = inspected["receipt"]
    interpreter = python_health(receipt["python"], probe=True)
    return {"status": "healthy" if interpreter["state"] == "healthy" else "repair_needed",
            "installation": str(inspected["target"]), "target_host": inspected["target_host"],
            "version": receipt["version"], "release_id": receipt["release_id"], "interpreter": interpreter,
            "installation_integrity": "verified", "models_started": False, "knowledge_opened": False,
            "host_connection_verified": False,
            "next_steps": ([] if interpreter["state"] == "healthy" else [
                "Run repair-python with an independent Python 3.10+ interpreter and --confirm.",
                "Refresh the host connection and reload it; repair does not enable Hooks or administrators."])}
