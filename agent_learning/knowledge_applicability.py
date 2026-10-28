"""Conservative project-version checks from bounded local manifests; no model calls."""
from __future__ import annotations

import json
from pathlib import Path
import re


_VERSION = r"[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:-[a-zA-Z0-9.-]+)?"
_AFTER = re.compile(r"\s*(?:\+|\.\*|\.x\b|以上|或|及|和|or\b|and\b|onward|through|至|到|[~^<>]|[,，、/|]\s*v?\d)", re.I)
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
                from .manifest_versions import toml_fields
                data = toml_fields(text, "project" if path.name == "pyproject.toml" else "package")
            if not isinstance(data, dict):
                continue
            name, version = data.get("name"), data.get("version")
            if isinstance(name, str) and 1 <= len(name) <= 120:
                names.add(_name(name))
            if isinstance(version, str) and re.fullmatch("v?" + _VERSION, version):
                versions.add(version.removeprefix("v"))
        except (OSError, UnicodeError, ValueError, RecursionError):
            continue
    # Conflicting root manifests can describe a monorepo. Do not guess which
    # component a generic project note belongs to.
    return {"project_version": next(iter(versions)), "project_names": sorted(names)} if len(versions) == 1 else {}


def _versions(text, pattern):
    values = []
    for match in re.finditer(pattern + r"\s*[:：=、,，`\"'（( ]*v?(" + _VERSION + r")(?![a-zA-Z0-9]|\.\d)", text, re.I):
        numeric_range = re.search(r"-\d+\.\d+", match[1])
        tail = text[match.end():].lstrip(" `\"'）)")
        if not numeric_range and not _AFTER.match(tail) and not re.match(r"\s*[-–—]\s*v?\d", tail):
            values.append(match[1])
    return values


def _manifest_applicability(item, environment):
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


# This is a small declaration grammar, not a prose/version-age classifier. The
# complete bounded input is checked so an unseen exception cannot be cut off.
_TEXT_LIMIT = 32768
_LINE_LIMIT = 256
_VERSION_TOKEN = re.compile(r"(?<![a-zA-Z0-9_.])v?(" + _VERSION + r")(?![a-zA-Z0-9_]|\.\d)", re.I)
_VERSION_LIKE = re.compile(r"v?\d+\.\d+", re.I)
_LABELS = {
    "适用条件": "applies", "适用范围": "applies", "适用版本": "applies",
    "applicability": "applies", "applicable versions": "applies",
    "限制": "limits", "limitations": "limits",
    "排除版本": "excludes", "不适用版本": "excludes", "exclusions": "excludes",
}
_LABEL = re.compile(r"^(" + "|".join(re.escape(label) for label in _LABELS) + r")\s*[:：]\s*(.*)$", re.I)
_NARRATIVE_HEADING = re.compile(
    r"^(?:历史(?:讨论|说明|记录|版本)?|引用|示例|代码|修复(?:说明|描述)|"
    r"history|historical (?:discussion|note)|quotation|quote|example|code|fix description)\s*(?:[:：]|$)", re.I)
_NARRATIVE_TITLE = re.compile(
    r"历史讨论|历史引用|引用示例|代码示例|修复说明|修复描述|"
    r"(?<![a-z0-9_.-])(?:history|historical|quotation|example|code sample|fix description)(?![a-z0-9_.-])", re.I)
_POSITIVE_PREFIX = re.compile(
    r"^(?:仅适用于|仅适用|只适用于|仅限于|仅限|仅|"
    r"only applicable to\b|only applies to\b|only for\b|only\b|"
    r"applies only to\b|applicable only to\b)\s*", re.I)
_NEGATIVE_PREFIX = re.compile(
    r"^(?:不适用于|不适用|排除|not applicable to\b|not applicable for\b|"
    r"not for\b|excludes?\b|excluded\b)\s*", re.I)


def _subject(value):
    value = value.strip()
    if not value:
        return None
    if re.fullmatch(r"项目(?:版本)?|插件(?:版本)?|project(?:\s+version)?|plugin(?:\s+version)?", value, re.I):
        return "@project"
    value = re.sub(r"^(?:工具\s*[:：]?\s*|tool\s*[:：]?\s+)", "", value, flags=re.I)
    value = re.sub(r"\s*(?:版本|\bversion)$", "", value, flags=re.I).strip()
    return _name(value) if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{1,119}", value) else False


def _version_clause(text, label):
    text = text.strip().rstrip("。.;；！!").strip()
    versions = list(_VERSION_TOKEN.finditer(text))
    if len(versions) != 1 or len(versions[0][1]) > 64:
        return None
    match = versions[0]
    version = match[1]
    if re.search(r"-v?\d+\.\d+", version, re.I):
        return None
    before, after = text[:match.start()].strip(), text[match.end():].strip()
    sign = None
    if re.fullmatch(r"不适用|(?:is\s+)?not applicable", after, re.I):
        sign = -1
    elif not after and (prefix := _NEGATIVE_PREFIX.match(before)):
        sign, before = -1, before[prefix.end():]
    elif not after and (prefix := _POSITIVE_PREFIX.match(before)):
        sign, before = 1, before[prefix.end():]
    elif re.fullmatch(r"only", after, re.I):
        sign = 1
    elif not after and label in {"applies", "excludes"}:
        sign = -1 if label == "excludes" else 1
    if sign is None:
        return None
    subject = _subject(before)
    return (subject, version, sign) if subject is not False else None


def _declarations(item):
    """Return only whole labeled clauses; code, quotations and prose stay data."""
    fields = [item.get(key, "") for key in ("title", "content", "applicability", "limitations")]
    if any(not isinstance(value, str) for value in fields) or sum(map(len, fields)) > _TEXT_LIMIT:
        return [], True
    if item.get("content_complete") is False or item.get("truncated") is True:
        return [], True
    raw_clauses, guarded = [], bool(_NARRATIVE_TITLE.search(item.get("title", "")))
    for key, kind in (("applicability", "applies"), ("limitations", "limits")):
        value = item.get(key, "")
        if value.strip():
            for line in value.splitlines():
                if line.strip():
                    labeled = _LABEL.fullmatch(line.strip())
                    raw_clauses.append((_LABELS[labeled[1].casefold()], labeled[2]) if labeled else (kind, line))
    lines = item.get("content", "").splitlines()
    if len(lines) > _LINE_LIMIT or len(raw_clauses) > _LINE_LIMIT:
        return [], True
    fence, section = None, None
    for raw in lines:
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            marker = line[:3]
            fence = None if fence == marker else marker if fence is None else fence
            section = None
            continue
        if fence or raw.startswith(("    ", "\t")) or line.startswith(">") or not line:
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
        text = heading[1].strip() if heading else line
        if _NARRATIVE_HEADING.match(text):
            guarded = True
        labeled = _LABEL.fullmatch(text)
        if labeled:
            kind, payload = _LABELS[labeled[1].casefold()], labeled[2]
            section = kind if not payload else None
            if payload:
                raw_clauses.append((kind, payload))
        elif text.casefold() in _LABELS and heading:
            section = _LABELS[text.casefold()]
        elif heading or re.match(r"^[^:：]{1,40}[:：]", text):
            section = None
        elif section:
            raw_clauses.append((section, text))
    if len(raw_clauses) > _LINE_LIMIT:
        return [], True
    declarations, ambiguous = [], False
    for label, clause in raw_clauses[:_LINE_LIMIT]:
        if not _VERSION_LIKE.search(clause):
            continue
        parsed = _version_clause(clause, label)
        if parsed is None:
            ambiguous = True
        else:
            declarations.append(parsed)
    return declarations, ambiguous or (guarded and bool(declarations))


def _query_target(query):
    if not isinstance(query, str) or len(query) > 4096 or any(mark in query for mark in ("`", '"', "'", "\n")):
        return None
    versions = list(_VERSION_TOKEN.finditer(query))
    if len(versions) != 1:
        return None
    match = versions[0]
    if len(match[1]) > 64 or re.search(r"-v?\d+\.\d+", match[1], re.I):
        return None
    before = query[:match.start()]
    named = re.search(r"(?<![a-zA-Z0-9_./-])([a-zA-Z][a-zA-Z0-9_.-]{1,119})(?:\s+(?:version|版本))?\s*$", before, re.I)
    if not named or _AFTER.match(query[match.end():]):
        return None
    name = _name(named[1])
    if name in {"version", "project", "plugin", "only", "from", "to", "at", "in", "for"}:
        return None
    return name, match[1]


def _mentions(text, subject):
    return bool(re.search(r"(?<![a-z0-9_.-])" + re.escape(subject) + r"(?![a-z0-9_.-])", _name(text)))


def _item_subject(item, subject):
    if _mentions(str(item.get("title", "")), subject):
        return True
    # A labeled conclusion may identify an otherwise generically titled work
    # record. A mention later in unrelated prose does not identify its subject.
    conclusions, fence = [], None
    for raw in str(item.get("content", "")).splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            marker = line[:3]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence or raw.startswith(("    ", "\t")) or line.startswith(">"):
            continue
        labeled = re.match(r"^(?:结论|conclusion)\s*[:：]\s*(.*)$", line, re.I)
        if labeled:
            conclusions.append(_name(labeled[1]))
    if len(conclusions) != 1:
        return False
    named = re.match(re.escape(subject) + r"(?![a-z0-9_.-])", conclusions[0])
    return bool(named and not re.match(r"\s*(?:与|和|及|[+/&、,，]|(?:and|vs|versus)\b)", conclusions[0][named.end():], re.I))


def _version_matches(declared, current):
    return declared == current or (declared.count(".") == 1 and current.startswith(declared + "."))


def _explicit_applicability(item, environment, query):
    declarations, ambiguous = _declarations(item)
    if ambiguous:
        return {"status": "unknown"}
    if not declarations:
        return None
    names = set(environment.get("project_names", ()))
    current = environment.get("project_version")
    target = _query_target(query)
    if not isinstance(query, str) or len(query) > 4096:
        return {"status": "unknown"}
    if isinstance(query, str) and len(list(_VERSION_TOKEN.finditer(query[:4097]))) > 1:
        return {"status": "unknown"}
    # A manifest and an explicit request for another version of that project
    # do not establish whether the request concerns its current installation.
    if target and target[0] in names and current and target[1] != current:
        return {"status": "unknown"}
    subjects = {subject for subject, _, _ in declarations if subject is not None}
    tool = _name(str(item.get("metadata", {}).get("tool", "")))
    generic_target = target if target and (tool == target[0] or _item_subject(item, target[0])) else None
    if re.search(r"与|和|及|[+/&]|\b(?:and|vs|versus)\b", str(item.get("title", "")), re.I):
        generic_target = None
    if tool and generic_target and tool != generic_target[0]:
        generic_target = None
    if (not generic_target and item.get("category") == "project" and current and
            (not target or target[0] in names) and (not tool or tool in names) and
            any(_item_subject(item, name) for name in names)):
        generic_target = ("@project", current)
    comparisons = []
    for subject, version, sign in declarations:
        if subject is None:
            if not generic_target or subjects - {generic_target[0]}:
                return {"status": "unknown"}
            subject, actual = generic_target
        elif subject == "@project":
            if not current or item.get("category") != "project" or (target and target[0] not in names):
                return {"status": "unknown"}
            actual = current
        elif target and subject == target[0]:
            actual = target[1]
        elif subject in names and current:
            actual = current
        else:
            return {"status": "unknown"}
        comparisons.append((subject, actual, version, sign))
    if len({(subject, actual) for subject, actual, _, _ in comparisons}) != 1:
        return {"status": "unknown"}
    positives = [version for _, actual, version, sign in comparisons if sign == 1]
    excluded = any(sign == -1 and _version_matches(version, actual) for _, actual, version, sign in comparisons)
    applies = any(_version_matches(version, actual) for version in positives)
    if (applies and excluded) or (not positives and not excluded):
        return {"status": "unknown"}
    return {"status": "compatible" if applies else "incompatible", "reason": "explicit_version",
            "current_version": comparisons[0][1], "version_subject": comparisons[0][0],
            "declared_versions": sorted({version for _, _, version, _ in comparisons})[:8]}


def automatic_applicability(item, environment, query=""):
    explicit = _explicit_applicability(item, environment or {}, query)
    return explicit if explicit is not None else _manifest_applicability(item, environment)
