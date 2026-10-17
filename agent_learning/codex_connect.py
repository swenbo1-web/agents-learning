"""Explicit Codex project wiring and host UI discovery; no runtime/model calls.

Only owned MCP fields are edited. A lexical TOML scanner preserves unrelated
tables, comments and multiline strings without adding a parser dependency.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import uuid

from .codex_actions import _binding, _paths, _write_atomic
from .install_update import installation_lock, python_executable, safe_path, validate_previous_release
from .runtime import ContractError


SERVER = ("mcp_servers", "agent_learning")
ENV = (*SERVER, "env")
PLUGIN = ("plugins", "agent-learning@agent-learning-local", "mcp_servers", "agent_learning")
_KEY = re.compile(r'''(?:"(?:\\.|[^"\\])*"|'[^']*'|[A-Za-z0-9_-]+)''')
MAX_CONFIG = 2 * 1024 * 1024


def _string(value):
    value = value.strip()
    match = _KEY.match(value)
    if not match or value[0] not in "\"'" or value[match.end():].strip() and not value[match.end():].lstrip().startswith("#"):
        raise ValueError("expected a TOML string")
    token = match.group()
    return token[1:-1] if token[0] == "'" else json.loads(token)


def _strings(value):
    """Decode only the string-array subset used by MCP argv, including TOML literals."""
    value, offset, result = value.strip(), 1, []
    if not value.startswith("["):
        raise ValueError("expected string array")
    expect_value = True
    while offset < len(value):
        char = value[offset]
        if char.isspace():
            offset += 1
            continue
        if char == "#":
            end = value.find("\n", offset)
            offset = len(value) if end < 0 else end + 1
            continue
        if char == "]":
            tail = value[offset + 1:].strip()
            if tail and not tail.startswith("#"):
                raise ValueError("unexpected array suffix")
            return result
        if char == "," and not expect_value:
            expect_value, offset = True, offset + 1
            continue
        match = _KEY.match(value, offset)
        if not expect_value or not match or char not in "\"'":
            raise ValueError("expected string array member")
        result.append(_string(match.group()))
        expect_value, offset = False, match.end()
    raise ValueError("incomplete string array")


def _key(value):
    if not value:
        raise ContractError("empty Codex configuration key; configuration preserved")
    parts, offset = [], 0
    while offset < len(value):
        match = _KEY.match(value, offset)
        if not match:
            raise ContractError("unsupported Codex configuration key; configuration preserved")
        token = match.group()
        parts.append(_string(token) if token[0] in "\"'" else token)
        offset = match.end()
        while offset < len(value) and value[offset].isspace():
            offset += 1
        if offset == len(value):
            break
        if value[offset] != ".":
            raise ContractError("unsupported Codex configuration key; configuration preserved")
        offset += 1
        while offset < len(value) and value[offset].isspace():
            offset += 1
        if offset == len(value):
            raise ContractError("incomplete Codex configuration key; configuration preserved")
    return tuple(parts)


def _statements(text):
    start = offset = depth = 0
    quote, triple, comment = None, False, False
    while offset < len(text):
        char = text[offset]
        if comment:
            if char == "\n":
                comment = False
        elif quote:
            if quote == '"' and char == "\\":
                offset += 2
                continue
            if triple and text.startswith(quote * 3, offset):
                quote, triple = None, False
                offset += 3
                continue
            if not triple and char == quote:
                quote = None
        elif char == "#":
            comment = True
        elif char in "\"'":
            quote, triple = char, text.startswith(char * 3, offset)
            if triple:
                offset += 3
                continue
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth < 0:
                raise ContractError("unbalanced Codex configuration; preserved")
        if char == "\n" and quote is None and depth == 0:
            yield start, offset + 1, text[start:offset + 1]
            start = offset + 1
        offset += 1
    if quote is not None or depth:
        raise ContractError("incomplete Codex configuration; preserved")
    if start < len(text):
        yield start, len(text), text[start:]


def _document(text):
    section, sections, entries = (), {}, {}
    for start, end, raw in _statements(text):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            # Headers cannot contain multiline strings. A table array is kept
            # distinct so it cannot masquerade as one of our ordinary tables.
            match = re.fullmatch(r"(\[\[?)(.*?)\]\]?\s*(?:#[^\n]*)?", stripped)
            if not match:
                raise ContractError("unsupported Codex table header; configuration preserved")
            section = _key(match[2].strip())
            if match[1] == "[[":
                section = ("@array", *section)
            if section in sections and section in {SERVER, ENV, PLUGIN}:
                raise ContractError("duplicate Agent Learning table; configuration preserved")
            sections[section] = end
            continue
        match = re.match(r"\s*((?:\"(?:\\.|[^\"\\])*\"|'[^']*'|[A-Za-z0-9_-]+)(?:\s*\.\s*(?:\"(?:\\.|[^\"\\])*\"|'[^']*'|[A-Za-z0-9_-]+))*)\s*=", raw)
        if not match:
            raise ContractError("unsupported Codex assignment; configuration preserved")
        path = (*section, *_key(match[1]))
        if path in entries:
            raise ContractError("duplicate Codex configuration field; preserved")
        entries[path] = (start, end, raw[match.end():].strip())
    return sections, entries


def _owned_server(entries, receipt):
    command, arguments = entries.get((*SERVER, "command")), entries.get((*SERVER, "args"))
    if command is None and arguments is None:
        return
    if command is None or arguments is None:
        raise ContractError("existing agent_learning connection is incomplete; preserved")
    try:
        executable = _string(command[2])
        args = _strings(arguments[2])
        if not isinstance(executable, str) or not isinstance(args, list) or any(not isinstance(x, str) for x in args):
            raise ValueError
        if python_executable(executable) != python_executable(receipt["python"]) or "serve" not in args:
            raise ValueError
        scripts = [safe_path(item) for item in args if item.replace("\\", "/").endswith("/scripts/learning.py")]
        if len(scripts) != 1:
            raise ValueError
        validate_previous_release(scripts[0].parent.parent)
    except (OSError, ValueError, TypeError, ContractError):
        raise ContractError("existing agent_learning connection could not be verified; preserved") from None


def _patch(text, desired, receipt):
    sections, entries = _document(text)
    # Inline tables and dotted root assignments would collide with table
    # insertion. Preserve such custom configurations for explicit editing.
    prefixes = {SERVER, ENV, PLUGIN}
    if any(path in entries for target in prefixes for path in (target[:i] for i in range(1, len(target) + 1))):
        raise ContractError("inline Agent Learning configuration requires manual wiring; preserved")
    _owned_server(entries, receipt)
    edits, additions = [], {}
    for section, fields in desired.items():
        for key, value in fields.items():
            existing = entries.get((*section, key))
            rendered = (key + " = " + json.dumps(value, ensure_ascii=False) + "\n") if value is not None else ""
            if existing:
                edits.append((existing[0], existing[1], rendered))
            elif value is not None:
                additions.setdefault(section, []).append(rendered)
    for section, lines in additions.items():
        if section in sections:
            edits.append((sections[section], sections[section], "".join(lines)))
        else:
            header = ".".join(part if re.fullmatch(r"[A-Za-z0-9_-]+", part) else json.dumps(part) for part in section)
            edits.append((len(text), len(text), "\n[" + header + "]\n" + "".join(lines)))
    for start, end, value in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True):
        text = text[:start] + value + text[end:]
    _document(text)
    return text


def connect(installation, workspace=None, *, home=None, config=None, codex_home=None, check=False):
    """Wire one bound project and the shared descriptor, preserving other keys."""
    if type(check) is not bool:
        raise ContractError("Codex connection check must be boolean")
    receipt, installation, workspace = _binding(installation, workspace)
    if not (Path(receipt["runtime_directory"]) / "agent_learning/codex_connect.py").is_file():
        raise ContractError("upgrade the project plugin before connecting native UI discovery")
    home, config = _paths(home, config)
    codex_home = safe_path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    global_path = safe_path(codex_home / "config.toml")
    project_path = safe_path(workspace / ".codex/config.toml")
    if global_path == project_path:
        raise ContractError("Codex home and project configuration must be separate")
    base = ["-B", str(Path(receipt["runtime_directory"]) / "scripts/learning.py"), "serve", "--role", "worker"]
    shared = {SERVER: {"command": receipt["python"], "args": [*base, "--panel-discovery"], "cwd": None, "enabled": True},
              ENV: {"ALP_HOME": str(home), "ALP_CONFIG": str(config)}, PLUGIN: {"enabled": False}}
    project = {SERVER: {"command": receipt["python"], "args": [*base, "--panel-installation", str(installation)],
                        "cwd": str(workspace), "enabled": True}, ENV: {"ALP_HOME": str(home), "ALP_CONFIG": str(config)},
               PLUGIN: {"enabled": False}}
    paths = sorted((global_path, project_path), key=str)
    with ExitStack() as stack:
        if not check:
            for path in paths:
                stack.enter_context(installation_lock(path))
        before, after = {}, {}
        for path, desired in ((global_path, shared), (project_path, project)):
            if path.exists() and (not path.is_file() or path.stat().st_size > MAX_CONFIG):
                raise ContractError("Codex configuration is not a bounded file; preserved")
            before[path] = path.read_bytes() if path.exists() else None
            try:
                text = before[path].decode("utf-8-sig") if before[path] is not None else ""
                after[path] = _patch(text, desired, receipt).encode("utf-8")
            except (ValueError, UnicodeError, RecursionError):
                raise ContractError("Codex configuration could not be verified; preserved") from None
        changed = [path for path in paths if before[path] != after[path]]
        result = {"status": "needs_connection" if check and changed else "connected", "workspace": str(workspace),
                  "project_config": str(project_path), "host_config": str(global_path), "changes_needed": [str(p) for p in changed],
                  "menu_scope": "host", "controls_scope": "installed_project", "desktop_render_verified": False,
                  "restart_required": bool(changed), "administrator_started": False}
        if check or not changed:
            return result
        backup = safe_path(codex_home / "agent-learning-backups" / uuid.uuid4().hex)
        backup.mkdir(parents=True, exist_ok=False)
        written = []
        try:
            _write_atomic(backup / "paths.json", (json.dumps([str(p) for p in changed], ensure_ascii=False) + "\n").encode("utf-8"))
            for index, path in enumerate(changed):
                current = path.read_bytes() if path.exists() else None
                if current != before[path]:
                    raise ContractError("Codex configuration changed during connection; preserved")
                if before[path] is not None:
                    _write_atomic(backup / (str(index) + ".toml"), before[path])
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_atomic(path, after[path])
                written.append(path)
        except BaseException:
            for path in reversed(written):
                if path.read_bytes() == after[path]:
                    if before[path] is None:
                        path.unlink()
                    else:
                        _write_atomic(path, before[path])
            raise
        return {**result, "changes_needed": [], "changed_configs": [str(p) for p in changed], "backup_directory": str(backup)}
