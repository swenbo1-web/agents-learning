"""Positive topic projection for matching; never rewrite delivered evidence."""
from __future__ import annotations

import re


_LIMIT = {"限制", "限制条件", "不适用", "不适用范围", "排除", "排除范围",
          "limitations", "restrictions", "exclusions", "not applicable"}
_CONCLUSION = {"结论", "处理结论", "解决结论", "conclusion", "decision", "resolution"}
_PEER = _LIMIT | {"结论", "处理结论", "解决结论", "背景", "过程", "原因", "分析", "细节",
                  "适用", "适用条件", "条件", "前提", "证据", "验证", "更正", "版本", "时间",
                  "conclusion", "decision", "resolution", "background", "analysis", "details",
                  "applicability", "conditions", "evidence", "verification", "correction", "version"}
_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_EXCLUSION = re.compile(r"不能用于|不用于|不适用于?|不涉及|不针对|不能诊断|"
    r"\b(?:not applicable|not intended for|does not (?:address|diagnose|cover|apply)|"
    r"cannot (?:diagnose|be used for))\b", re.I)
_SENTENCE = re.compile(r"(?<=[。！？；;\n])|(?<=[.!?])\s+")
_CONTRAST = re.compile(r"[,，]\s*(?:但是?|but\b|however\b)\s*", re.I)
_SETTING = re.compile(r"(?:[A-Z][a-z0-9]+){2,}|[a-z]+(?:[A-Z][a-z0-9]+)+")
_INLINE_LIMIT = re.compile(r"(?<=[。；;.!?\t ])(?=(?:\*\*)?(?:" +
    "|".join(re.escape(label) for label in sorted(_LIMIT, key=len, reverse=True)) + r")\s*[:：])", re.I)
_INLINE_CODE = re.compile(r"(`+).*?\1")


def _topic_lines(text):
    """Virtual matching boundaries only; source text and excerpt offsets stay intact."""
    fence = None
    for line in text.splitlines(keepends=True):
        marker = _FENCE.match(line)
        if marker:
            run = marker[1]
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not line[marker.end():].strip():
                fence = None
            yield line
            continue
        if fence is not None:
            yield line
            continue
        protected = [(match.start(), match.end()) for match in _INLINE_CODE.finditer(line)]
        offset = 0
        for match in _INLINE_LIMIT.finditer(line):
            if any(start <= match.start() < end for start, end in protected):
                continue
            yield line[offset:match.start()]
            offset = match.start()
        yield line[offset:]


def topic_text(text):
    """Remove explicit exclusion material from matching, retaining prohibitions.

    A conclusion such as 'never retry indefinitely' still describes its topic.
    Only labelled restriction sections and explicit out-of-scope statements
    cannot supply a positive match. The original source remains untouched.
    """
    result, restricted, level, fence = [], False, 0, None
    conclusion, conclusion_level = False, 0
    for line in _topic_lines(text):
        marker = _FENCE.match(line)
        if marker:
            run = marker[1]
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not line[marker.end():].strip():
                fence = None
        heading = _HEADING.match(line.rstrip()) if fence is None else None
        if fence is None:
            value = (heading[2] if heading else line).strip().strip("*").strip()
            label = re.split(r"[:：]", value, maxsplit=1)[0].strip().strip("*").casefold()
            if heading and restricted and len(heading[1]) <= level:
                restricted = False
            if heading and len(heading[1]) <= conclusion_level:
                conclusion = False
            if label in _PEER and (not restricted or (heading and len(heading[1]) <= level)
                                  or (not heading and level == 0)):
                restricted = label in _LIMIT
                level = len(heading[1]) if heading else 0
                if not heading or len(heading[1]) <= conclusion_level or not conclusion:
                    conclusion = label in _CONCLUSION and not restricted
                    conclusion_level = level
        if not restricted:
            if conclusion:
                # "SQLite cannot be used for cross-machine transactions" can
                # itself be the answer. Explicit conclusions retain negations.
                result.append(line)
            else:
                for sentence in _SENTENCE.split(line):
                    clauses = _CONTRAST.split(sentence) if _EXCLUSION.search(sentence) else [sentence]
                    result.extend(clause for clause in clauses if not _EXCLUSION.search(clause))
    return " ".join(result)


def topic_body(body):
    result = dict(body, limitations="")
    for field in ("title", "content", "applicability"):
        if isinstance(body.get(field), str):
            result[field] = topic_text(body[field])
    return result


def focused_settings(query, generic_names):
    """A single complete CamelCase setting can survive explanatory prose.

    This is an optional relevance aid, never a replacement for hard file/error
    identities. Truncated input or multiple possible settings cannot use it.
    """
    if len(query) > 8000:
        return []
    names = list(dict.fromkeys(word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9]*", query)
        if len(word) >= 6 and _SETTING.fullmatch(word) and word.lower() not in generic_names))
    return names if len(names) == 1 else []
