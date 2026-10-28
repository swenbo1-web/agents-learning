"""Bounded presentation variants, never semantic or global fingerprint equality.

Only one explicit leading conclusion label can differ. Original business objects
and stored fingerprints remain unchanged; consumers keep their normal authority,
source, revision and governance checks.
"""
from __future__ import annotations

import re


_PREFIXES = ("结论： ", "结论：", "Conclusion: ", "Conclusion:")
_MAX_BASE_CHARS = 20000
_LABEL = re.compile(r"^(?:#{1,6}\s+)?(?:\*\*)?(?:结论[:：]?|conclusion[:：]?)\s*(?:\*\*)?", re.I | re.M)
_MARKUP = re.compile(r"^(?:\s|#{1,6}\s|`{3,}|~{3,}|>|[-*+]\s)")


def format_variants(body):
    """Return deterministic finite full-body variants; ambiguous text stays exact."""
    content = body.get("content")
    if (not isinstance(content, str) or not content
            or len(content) > _MAX_BASE_CHARS + max(map(len, _PREFIXES))):
        return [body]
    prefix = next((value for value in _PREFIXES if content.startswith(value)), None)
    base = content[len(prefix):] if prefix else content
    if not base or len(base) > _MAX_BASE_CHARS or _MARKUP.match(base) or _LABEL.search(base):
        return [body]
    texts = [content, base, *(value + base for value in _PREFIXES)]
    result, seen = [], set()
    for text in texts:
        if text not in seen:
            seen.add(text)
            result.append(body if text == content else dict(body, content=text))
    return result


def presentation_fingerprints(body):
    from .runtime import knowledge_fingerprint
    return tuple(sorted({knowledge_fingerprint(value) for value in format_variants(body)}))


def same_presentation_claim(left, right):
    """All existing fingerprint fields remain exact, including grade and metadata."""
    from .runtime import knowledge_fingerprint
    return knowledge_fingerprint(right) in presentation_fingerprints(left)


def fingerprint_query(body):
    fingerprints = presentation_fingerprints(body)
    return ",".join("?" for _ in fingerprints), fingerprints
