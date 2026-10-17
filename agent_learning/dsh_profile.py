"""Trusted DSH model configuration snapshots; configuration never enters review evidence."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


ENVIRONMENT = "ALP_DSH_REVIEW_PROFILE"
MAX_BYTES = 1024 * 1024
MAX_ENV_CHARS = 16000
MODULES = frozenset({"@deepseek-ai/dsh-settings-file", "@deepseek-ai/dsh-credentials-local",
    "@deepseek-ai/dsh-agent-default-model", "@deepseek-ai/dsh-llm-deepseek", "@deepseek-ai/dsh-llm-pi-ai"})


def _error(reason):
    from .runtime import ContractError
    error = ContractError(reason + ": explicit trusted DSH model configuration is required")
    error.launch_reason = reason
    raise error


def resolve_profile(path=""):
    """Read one explicit operator file or one inherited host snapshot; never search profiles."""
    if path:
        source = Path(path)
        if not source.is_absolute():
            _error("dsh_profile_unavailable")
        try:
            with source.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            resolved_path = str(source.resolve())
        except (OSError, RuntimeError, ValueError):
            _error("dsh_profile_unavailable")
        descriptor = {"source": "operator_file", "path": resolved_path}
    else:
        value = os.environ.get(ENVIRONMENT)
        if not value:
            _error("dsh_profile_unavailable")
        if len(value) > MAX_ENV_CHARS:
            _error("dsh_profile_too_large")
        raw = value.encode("utf-8")
        descriptor = {"source": "host_snapshot"}
    try:
        value = json.loads(raw)
        if (len(raw) > MAX_BYTES or not isinstance(value, dict) or value.get("version") != 1
                or not isinstance(value.get("profile_directory"), str)
                or not Path(value["profile_directory"]).is_absolute()
                or not isinstance(value.get("entries"), list) or not 1 <= len(value["entries"]) <= len(MODULES)):
            _error("dsh_profile_invalid")
        names = []
        for entry in value["entries"]:
            if (not isinstance(entry, dict) or set(entry) != {"name", "config"}
                    or entry["name"] not in MODULES or not isinstance(entry["config"], dict)):
                _error("dsh_profile_invalid")
            if entry["name"] in {"@deepseek-ai/dsh-settings-file", "@deepseek-ai/dsh-credentials-local"}:
                for key in ("path", "dshHome"):
                    if key in entry["config"] and (not isinstance(entry["config"][key], str)
                            or not Path(entry["config"][key]).is_absolute()):
                        _error("dsh_profile_invalid")
            names.append(entry["name"])
        if len(set(names)) != len(names):
            _error("dsh_profile_invalid")
        directory = Path(value["profile_directory"]).resolve()
        if descriptor["source"] == "host_snapshot":
            configured_home = os.environ.get("DSH_HOME", "")
            home = Path(configured_home if configured_home.strip() else Path.home() / ".dsh").expanduser().resolve()
            if directory.parent != home / "profiles" or directory.name in {"", ".", ".."}:
                _error("dsh_profile_invalid")
    except (ValueError, TypeError, KeyError, OSError, RuntimeError):
        _error("dsh_profile_invalid")
    return {**descriptor, "profile_directory": str(directory), "sha256": hashlib.sha256(raw).hexdigest()}


def assert_profile(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("source") not in {"host_snapshot", "operator_file"}:
        _error("dsh_profile_unavailable")
    current = resolve_profile(snapshot.get("path", "") if snapshot["source"] == "operator_file" else "")
    if current != snapshot:
        _error("dsh_profile_changed")
    return current


def profile_key(snapshot):
    """A stable profile origin, independent of model/configuration revisions."""
    return os.path.normcase(str(Path(snapshot["profile_directory"]).resolve()))


def _binding_id(project, conversation):
    from .runtime import digest
    return digest([project, "dsh-profile-origin:" + conversation])


def bind_conversation_profile(runtime, project, conversation):
    """Only trusted inherited/operator configuration establishes this immutable origin."""
    from .runtime import ContractError
    try:
        snapshot = resolve_profile(runtime.config["admin"].get("dsh_profile", ""))
    except ContractError:
        return False  # Missing review configuration must not stop ordinary capture.
    key = profile_key(snapshot)
    ident = _binding_id(project, conversation)
    old = runtime.db.execute("SELECT payload FROM events WHERE project=? AND id=? AND type='DshProfileBound'",
                             (project, ident)).fetchone()
    if old:
        if json.loads(old["payload"]).get("profile_key") != key:
            _error("dsh_profile_source_mismatch")
        return False
    if not runtime.db.in_transaction:
        _error("dsh_profile_binding_requires_transaction")
    runtime._event(project, conversation, "DshProfileBound", {"profile_key": key,
        "host_scope": runtime._session_scope(), "internal_control": True},
        event_id="dsh-profile-origin:" + conversation)
    return True


def assert_conversation_profile(runtime, project, conversation, snapshot):
    if conversation is None or not conversation.startswith("dsh:"):
        return  # Explicit history and non-DSH source provider policies retain their contract.
    row = runtime.db.execute("SELECT payload FROM events WHERE project=? AND id=? AND type='DshProfileBound'",
        (project, _binding_id(project, conversation))).fetchone()
    if row is None:
        _error("dsh_profile_source_unbound")
    if json.loads(row["payload"]).get("profile_key") != profile_key(snapshot):
        _error("dsh_profile_source_mismatch")
