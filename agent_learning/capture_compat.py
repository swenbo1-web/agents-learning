"""Verify pre-1.4.42 capture identities without retaining legacy plaintext.

The frozen masker is used only to compare a supplied full observation with an
already stored digest. It is never an output sanitizer or a migration writer.
"""
from __future__ import annotations

import hashlib
import json
import re

_KEY = re.compile(r"(?i)(password|secret|api.?key|authorization|token)$")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s\"']+")
_ASSIGNMENT = re.compile(r"(?i)((?:api[_-]?key|password|secret|access_token)\s*[=:]\s*)([^\s,;]+)")
_MASKED = re.compile(r"\[REDACTED\](?:[&#]|$)")


def _assignment(match):
    return match[0] if _MASKED.match(match[2]) else match[1] + "[REDACTED]"


def legacy_clean(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _KEY.search(key) else legacy_clean(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [legacy_clean(item) for item in value]
    if isinstance(value, str):
        return _ASSIGNMENT.sub(_assignment, _BEARER.sub("Bearer [REDACTED]", value))
    return value


def legacy_digest(value):
    text = json.dumps(legacy_clean(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def matches_digest(stored, current, original):
    """Unknown signatures are not evidence; legacy equality must be exact."""
    return bool(stored and (stored == current or stored == legacy_digest(original)))
