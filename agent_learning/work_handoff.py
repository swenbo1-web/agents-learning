"""Bounded, verbatim previews of already eligible public task handoffs.

This module selects text; it does not verify sources or grant task permission.
The caller must retain the original record and its read-required/source labels.
"""
import re


_MAX_SCAN_CHARS = 16384
_MAX_SCAN_LINES = 256
_MAX_PREVIEW_CHARS = 512

_PENDING = re.compile(
    r"未完成|尚未|仍未|待验证|未验证|尚待|仍需|还需|仍待|待办|未解决|阻塞|待确认|需确认|需要验证|待补|待运行|"
    r"\b(?:pending|unfinished|unresolved|unverified|incomplete|remaining)\b|"
    r"\bnot\s+yet\b|\bstill\s+(?:needs?|requires?|pending)\b|"
    r"\bneeds?\s+(?:verification|validation|testing)\b|"
    r"\bnot\s+(?:verified|validated|tested|completed)\b", re.I)
_CONSTRAINT = re.compile(
    r"禁止|不得|不要|必须|仅限|只允许|保留|保持|不变量|约束|限制|不启用|不修改|不调用|不能|不可|未经|更正|纠正|撤回|"
    r"\b(?:must|mustn't|never|cannot|can't|preserve|constraints?|invariants?|correction)\b|"
    r"\b(?:do\s+not|don't|only\s+(?:if|when|after|before)|restricted\s+to|limited\s+to)\b|"
    r"\bkeep\s+\S+", re.I)
_NEXT = re.compile(
    r"下一步|接下来|后续(?:需要|需|将|计划|工作|步骤)|"
    r"\b(?:next\s+steps?|follow[ -]up|to[ -]do|todo)\b|\bnext\s*:", re.I)
_CONDITIONAL = re.compile(
    r"仅在|仅当|只有|除非|未经|前提|条件|"
    r"\b(?:if|unless|until|before|after|without|provided|except|only)\b", re.I)
_CORRECTION = re.compile(r"更正|纠正|撤回|\b(?:correction|retracted|withdrawn)\b", re.I)
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+\S")
_LABEL = re.compile(
    r"(?:未完成(?:事项|工作)?|待办(?:事项)?|待验证(?:事项)?|关键限制|约束(?:条件)?|"
    r"保留不变量|不变量|下一步(?:计划|工作)?|后续工作|更正|撤回|"
    r"pending(?:\s+(?:items|work))?|remaining(?:\s+(?:items|work))?|"
    r"unverified|constraints?|invariants?|next\s+steps?|limitations?|correction)", re.I)
_BULLET = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)、]\s+|[☐□]\s*)")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_LOG = re.compile(
    r"^\s*(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|"
    r"\[(?:TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL)\]|"
    r"(?:TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL)\s*[:\[])|"
    r'^\s*(?:\{\s*["\']|["\'][^"\']+["\']\s*:)', re.I)
_LOG_LABEL = re.compile(r"(?:日志|运行日志|工具输出|logs?|tool\s+output|traceback)", re.I)


def _priority(text):
    if _PENDING.search(text) or _CONSTRAINT.search(text):
        return 0
    if _NEXT.search(text):
        return 1
    return 2


def _groups(text):
    """Keep paragraphs/bullets whole, together with their heading conditions."""
    scan = text[:_MAX_SCAN_CHARS]
    groups, headings = [], []
    label, prelude, current = None, None, None
    fence, opaque_section = None, False
    exhausted = len(scan) == len(text)

    def flush():
        nonlocal current
        if current is not None:
            start, end, context, _, _ = current
            spans = tuple(context) + ((start, end),)
            priority = min(_priority(text[a:b]) for a, b in spans)
            groups.append((priority, spans))
            current = None

    for number, match in enumerate(re.finditer(r"[^\n]+(?:\n|$)|\n", scan)):
        if number >= _MAX_SCAN_LINES:
            exhausted = False
            break
        raw = match.group().rstrip("\r\n")
        # The scan boundary is not a sentence or line boundary.
        if match.end() == len(scan) and not exhausted and not match.group().endswith("\n"):
            break
        stripped = raw.strip()
        start = match.start() + len(raw) - len(raw.lstrip())
        end = match.start() + len(raw.rstrip())
        marker = _FENCE.match(raw)
        if fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1]:
                # A closing fence cannot carry an extra language or payload.
                if not raw[marker.end():].strip():
                    fence = None
            continue
        if marker:
            flush()
            prelude = None
            fence = (marker[1][0], len(marker[1]))
            continue
        if not stripped:
            if current is not None and _CONDITIONAL.search(text[current[0]:current[1]]):
                prelude = (current[0], current[1])
            flush()
            continue
        # Quoted/log/code headers must not become context for following prose.
        if (stripped.startswith(">") or _LOG.match(raw)
                or ((raw.startswith("    ") or raw.startswith("\t"))
                    and (current is None or not current[3]))):
            flush()
            prelude = None
            continue

        markdown = _HEADING.match(raw)
        label_text = stripped.strip("#*_ \t").rstrip(":：").strip()
        is_label = bool(_LABEL.fullmatch(label_text) or _LOG_LABEL.fullmatch(label_text))
        colon = stripped.endswith((":", "：")) and not _BULLET.match(raw)
        if markdown or is_label or colon:
            flush()
            prelude = None
            if markdown:
                level = len(markdown[1])
                headings = [(n, span) for n, span in headings if n < level]
                headings.append((level, (start, end)))
                label = None
            else:
                label = (start, end)
            opaque_section = bool(_LOG_LABEL.fullmatch(label_text)) or any(
                _LOG_LABEL.fullmatch(text[a:b].strip("#*_ \t").rstrip(":：").strip())
                for _, (a, b) in headings)
            continue
        if opaque_section:
            flush()
            prelude = None
            continue

        bullet = bool(_BULLET.match(raw))
        indent = len(raw[:len(raw) - len(raw.lstrip())].expandtabs(4))
        nested = bool(bullet and current is not None and current[3] and indent > current[4])
        if bullet and current is not None and not nested:
            if not current[3] and _CONDITIONAL.search(text[current[0]:current[1]]):
                # A condition immediately introducing a list applies to its items.
                prelude = (current[0], current[1])
                current = None
            else:
                flush()
        if current is None:
            if not bullet:
                prelude = None
            context = [span for _, span in headings]
            if label is not None:
                context.append(label)
            if prelude is not None:
                context.append(prelude)
            current = (start, end, context, bullet, indent)
        else:
            current = (current[0], end, current[2], current[3], current[4])
    if exhausted:
        flush()
    return groups


def _render(text, spans):
    ordered = sorted(set(spans))
    merged = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    parts, previous = [], 0
    for start, end in merged:
        gap = text[previous:start]
        if gap.strip():
            parts.append("…\n")
        elif parts:
            parts.append(gap)
        parts.append(text[start:end])
        previous = end
    # Do not inspect an arbitrarily large tail merely to mark its omission.
    if merged and (len(text) > _MAX_SCAN_CHARS or text[previous:_MAX_SCAN_CHARS].strip()):
        parts.append("\n…")
    return "".join(parts)


def select_handoff_preview(text: str, max_chars: int = 160) -> str:
    """Select complete original groups without rewriting their contents.

    Explicit pending/verification/constraint phrases precede next-step groups.
    Selection is restored to source order; ellipses mark omitted source groups.
    Headings and list conditions travel with their complete paragraph/bullet.
    Explicit correction groups must fit in full before other groups are shown.
    Fenced code, quotes, indented code and obvious logs receive no preview.
    With no priority group, only the first complete prose group is considered.
    If a complete group and its context cannot fit, return an empty preview.

    At most 16,384 source characters and 256 lines are scanned; output is capped
    at 512 characters even when a caller asks for more. Source/task eligibility
    is the caller's responsibility, as is preserving read-required metadata.
    Selection cues do not establish semantic completeness or authority.
    """
    if not isinstance(text, str) or type(max_chars) is not int or max_chars <= 0:
        return ""
    budget = min(max_chars, _MAX_PREVIEW_CHARS)
    groups = _groups(text)
    priority = [(rank, spans) for rank, spans in groups if rank < 2]
    candidates = sorted(priority, key=lambda item: (item[0], item[1][-1][0])) if priority else groups[:1]
    selected = [span for _, spans in groups if any(_CORRECTION.search(text[a:b]) for a, b in spans)
                for span in spans]
    result = _render(text, selected) if selected else ""
    if len(result) > budget:
        return ""
    for _, spans in candidates:
        preview = _render(text, selected + list(spans))
        if len(preview) <= budget:
            selected.extend(spans)
            result = preview
    return result
