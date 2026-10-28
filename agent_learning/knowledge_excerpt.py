"""Select explicit original conclusions; never generate or verify a claim.

Offsets remain in the immutable source text so delivery coverage cannot mistake
an excerpt for a complete body. Ambiguous/unstructured documents use the legacy
query excerpt. Conditions, corrections and temporal boundaries stay attached.
"""
from __future__ import annotations

import re


_CONCLUSION = {"结论", "处理结论", "解决结论", "conclusion", "decision", "resolution"}
_BOUNDARY = {"适用", "适用条件", "前提", "条件", "限制", "注意", "例外", "更正", "纠正",
             "版本", "时间", "版本或时间", "证据", "验证", "applicability", "conditions", "limitations",
             "caveats", "exceptions", "correction", "version", "as of", "evidence", "verification"}
_OTHER = {"背景", "过程", "原因", "分析", "细节", "示例", "旧方案",
          "background", "analysis", "details", "example"}
_LABELS = _CONCLUSION | _BOUNDARY | _OTHER
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_QUALIFIER = re.compile(r"仅|只有|必须|不得|禁止|不能|不要|未验证|未经|不确定|更正|纠正|前提|除非|如果|假如|"
                        r"\b(?:if|unless|only|must|never|not|unverified|unknown|correction|except|before|after)\b", re.I)


def _label(line):
    """Recognize standalone/colon labels, never a mid-sentence keyword."""
    text = line.strip().strip("*").strip()
    label = re.split(r"[:：]", text, maxsplit=1)[0].strip().strip("*").strip().casefold()
    return label if label in _LABELS else None


def conclusion_range(text):
    """Return one contiguous conclusion + explicit boundary range, or None.

    Only top-level labels qualify. Code, quoted/list examples, nested headings
    and repeated conclusions cannot claim to be the document's final decision.
    The scan is bounded; no hashing, model, file IO or schema migration occurs.
    """
    if not isinstance(text, str) or len(text) > 20000:
        return None
    sections, offset, fence = [], 0, None
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        marker = _FENCE.match(line)
        if marker:
            run = marker[1]
            if fence is None:
                fence = (run[0], len(run))
            elif (run[0] == fence[0] and len(run) >= fence[1]
                  and not line[marker.end():].strip()):
                fence = None
            offset += len(raw)
            continue
        if fence is None and line and not line[0].isspace() and not line.startswith((">", "- ", "* ", "+ ")):
            heading = _HEADING.match(line)
            label = _label(heading[2] if heading else line)
            if heading or label:
                sections.append((offset, len(heading[1]) if heading else 0, label))
        offset += len(raw)
    conclusions = [index for index, section in enumerate(sections) if section[2] in _CONCLUSION]
    if len(conclusions) != 1 or fence is not None:
        return None
    index = conclusions[0]
    level = sections[index][1]
    # Ordinary parent headings still make the decision's scope ambiguous.
    # Explicit boundaries can use another heading style, but may never be
    # discarded by falling back to a query window around their conclusion.
    mixed_boundaries = any(section[2] in _BOUNDARY and section[1] != level for section in sections)
    parents = sections[:index] if mixed_boundaries else sections
    if any(section[1] < level and section[2] not in _BOUNDARY for section in parents):
        return None
    if mixed_boundaries:
        start, end = 0, len(text)
    else:
        peers = [i for i, section in enumerate(sections) if section[1] == level]
        selected = [index, *(i for i in peers if sections[i][2] in _BOUNDARY)]
        first, last = min(selected), max(selected)
        start = sections[first][0]
        next_peer = next((i for i in peers if i > last), None)
        end = sections[next_peer][0] if next_peer is not None else len(text)
    # A boundary outside the chosen labelled block is not safely separable.
    # Keep the complete source (usually an index-only preview), rather than
    # dropping a qualification merely because its author used another heading.
    if _QUALIFIER.search(text[:start]) or _QUALIFIER.search(text[end:]):
        start, end = 0, len(text)
    # Keep the exact original span, including all intervening paragraphs.
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if end > start else None


def conclusion_projection(text, limit):
    """None means legacy fallback; empty means the complete block needs a read."""
    span = conclusion_range(text)
    if span is None:
        return None
    start, end = span
    prefix, suffix = ("…" if start else ""), ("…" if end < len(text) else "")
    if end - start + len(prefix) + len(suffix) > limit:
        return "", 0, 0, True
    return prefix + text[start:end] + suffix, start, end, end < len(text)
