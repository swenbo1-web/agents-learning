"""Conservative project-version checks from bounded local manifests; no model calls."""
from __future__ import annotations

import json
from pathlib import Path
import re


_VERSION = r"[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:-[a-zA-Z0-9.-]+)?"
_AFTER = re.compile(r"\s*(?:\+|以上|及以上|或更高|及后续|or later|and later|onward|through|至|到|[~^<>])", re.I)
_HISTORICAL = re.compile(r"历史快照|文档快照|旧版快照|historical snapshot|old snapshot|document snapshot", re.I)


def _name(value):
    return value.casefold().replace("_", "-")


def project_environment(workspace):
    if workspace is None:
        return {}
    root = Path(workspace).resolve()
    versions, names = set(), {_name(root.name)}
    for relative in (".codex-plugin/plugin.json", "package.json", "pyproject.toml", "Cargo.toml"):
        path = root / relative
        try:
            if not path.resolve().is_relative_to(root) or path.stat().st_size > 65536:
                continue
            with path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                continue
            text = raw.decode("utf-8-sig")
            if path.suffix == ".json":
                data = json.loads(text)
            else:
                section = "project" if path.name == "pyproject.toml" else "package"
                match = re.search(r"(?ms)^\[" + section + r"\]\s*\n(.*?)(?=^\[|\Z)", text)
                data = dict(re.findall(r'''(?m)^\s*(name|version)\s*=\s*["']([^"'\n]+)["']\s*(?:#.*)?$''',
                                       match[1])) if match else {}
            if not isinstance(data, dict):
                continue
            name, version = data.get("name"), data.get("version")
            if isinstance(name, str) and 1 <= len(name) <= 120:
                names.add(_name(name))
            if isinstance(version, str) and re.fullmatch("v?" + _VERSION, version):
                versions.add(version.removeprefix("v"))
        except (OSError, UnicodeError, ValueError):
            continue
    # Conflicting root manifests can describe a monorepo. Do not guess which
    # component a generic project note belongs to.
    return {"project_version": next(iter(versions)), "project_names": sorted(names)} if len(versions) == 1 else {}


def _versions(text, pattern):
    values = []
    for match in re.finditer(pattern + r"\s*[:：=、,，`\"'（( ]*v?(" + _VERSION + r")(?![a-zA-Z0-9]|\.\d)", text, re.I):
        if not _AFTER.match(text[match.end():]) and not re.match(r"\s*[-–—]\s*v?\d", text[match.end():]):
            values.append(match[1])
    return values


def automatic_applicability(item, environment):
    current = environment.get("project_version") if environment else None
    if not current:
        return {"status": "unknown"}
    names = environment["project_names"]
    metadata = item.get("metadata", {})
    declared = []
    if _name(str(metadata.get("tool", ""))) in names:
        version = metadata.get("tool_version", "")
        if isinstance(version, str) and re.fullmatch("v?" + _VERSION, version):
            declared.append(version.removeprefix("v"))
    if item.get("category") == "project":
        conditions = "\n".join(str(item.get(key, "")) for key in ("applicability", "limitations"))
        declared += _versions(conditions, r"(?:项目版本|project\s+version|插件版本|plugin\s+version)")
        text = "\n".join(str(item.get(key, "")) for key in ("title", "content", "applicability", "limitations"))
        normalized = _name(text)
        named = any(re.search(r"(?<![a-z0-9_-])" + re.escape(name) + r"(?![a-z0-9_-])", normalized)
                    for name in names if len(name) >= 3)
        if named and _HISTORICAL.search(str(item.get("limitations", ""))):
            declared += _versions(text, r"(?:兼容基线[\s\S]{0,80}?(?:版本|version)|compatibility\s+baseline[\s\S]{0,80}?version)")
    if not declared:
        return {"status": "unknown"}
    def matches(version):
        # A two-component declaration covers patches in that minor line.
        return version == current or (version.count(".") == 1 and current.startswith(version + "."))
    return {"status": "compatible" if any(matches(value) for value in declared) else "incompatible",
            "reason": "project_version", "current_version": current,
            "declared_versions": sorted(set(declared))[:8]}
