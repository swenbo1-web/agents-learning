"""Bounded, format-aware masking of observable text, without reserializing logs.

This is not a general secret detector. Standalone credential assignment lines
end at a comma, semicolon or line end. In mixed free-form text, unquoted values
end at their first whitespace; multiword values need an explicit quote boundary.
"""
from __future__ import annotations

import json
import re


_MASK = "[REDACTED]"
_TEXT_HINTS = ("password", "secret", "token", "authorization", "bearer", "key")
_SENSITIVE_KEY = re.compile(r"(?i)(password|secret|api.?key|authorization|token)$")
# Keys are bounded; values are scanned once rather than matched by nested or
# overlapping unbounded regular expressions. JSON key escapes are decoded only
# inside this bounded key token. Public whitespace and unrelated fields survive.
_QUOTED_FIELD = re.compile(
    r'''(?P<quote>["'])(?P<key>(?:\\.|[^"'\\\r\n]){1,128})(?P=quote)[ \t\r\n]*[:=][ \t\r\n]*''')
_QUOTED_ASSIGNMENT = re.compile(
    r'''(?i)(?<![\w])(?:api[_-]?key|password|secret|access_token|token)[ \t]*[=:][ \t]*(?=["'])''')
_BASIC_AUTH = re.compile(
    r'''(?i)(\b(?:proxy-)?authorization[ \t]*[:=][ \t]*Basic[ \t]+)([^\s,;"']+)''')
_LINE_ASSIGNMENT = re.compile(
    r"(?im)^([ \t]*(?:api[_-]?key|password|secret|access_token|token)[ \t]*[=:][ \t]*)([^\r\n,;]+)")
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[^\s\"']+")
_REDACTED_ASSIGNMENT = re.compile(r"\[REDACTED\](?:[&#]|$)")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:api[_-]?key|password|secret|access_token|(?<![\w])token)\s*[=:]\s*)([^\s,;]+)")


def _quoted_end(text, start):
    quote, index = text[start], start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
        elif char == quote:
            return index + 1
        else:
            index += 1
    # A truncated credential must not retain its visible private prefix. No
    # recursive parse or attempt to reconstruct the rest of the document.
    return len(text)


def _value_end(text, start):
    if text[start] in "\"'":
        return _quoted_end(text, start)
    if text[start] not in "[{":
        end = start
        while end < len(text) and text[end] not in ",;}]\r\n":
            end += 1
        return end
    stack, index = [], start
    while index < len(text):
        char = text[index]
        if char in "\"'":
            index = _quoted_end(text, index)
            continue
        if char in "[{":
            stack.append("]" if char == "[" else "}")
            if len(stack) > 64:
                return len(text)
        elif char in "]}":
            if not stack or char != stack.pop():
                return len(text)
            if not stack:
                return index + 1
        index += 1
    return len(text)


def _mask_fields(text):
    parts, cursor = [], 0
    for match in _QUOTED_FIELD.finditer(text):
        if match.start() < cursor:
            continue
        key = match["key"]
        if match["quote"] == '"' and "\\" in key:
            try:
                key = json.loads('"' + key + '"')
            except ValueError:
                continue
        if not _SENSITIVE_KEY.search(key) or match.end() == len(text):
            continue
        start = match.end()
        if text[start] in ",;}]":
            continue
        end = _value_end(text, start)
        quote = text[start] if text[start] in "\"'" else '"'
        parts.extend((text[cursor:start], quote + _MASK + quote))
        cursor = end
    if not parts:
        return text
    parts.append(text[cursor:])
    return "".join(parts)


def _mask_quoted_assignments(text):
    parts, cursor = [], 0
    for match in _QUOTED_ASSIGNMENT.finditer(text):
        if match.start() < cursor:
            continue
        start = match.end()
        end = _quoted_end(text, start)
        parts.extend((text[cursor:start], text[start] + _MASK + text[start]))
        cursor = end
    if not parts:
        return text
    parts.append(text[cursor:])
    return "".join(parts)


def _assignment(match):
    # An already-masked URL can retain public query/fragment suffixes. A colon,
    # path or adjacent private text after the marker is never an exemption.
    value = match[2]
    if _REDACTED_ASSIGNMENT.match(value):
        return match[0]
    # The quoted-value pass produced a complete mask. Keep the original quote
    # pair so repeated cleaning doesn't break JSON, shell or configuration text.
    if value in ('"' + _MASK + '"', "'" + _MASK + "'"):
        return match[0]
    return match[1] + _MASK


def _line_assignment(match):
    value = match[2]
    # Quoted values have already been handled as complete tokens; anything
    # after their closing quote is outside that value, e.g. a public CLI flag.
    if value.startswith(('"', "'")) or _REDACTED_ASSIGNMENT.match(value):
        return match[0]
    return match[1] + _MASK


def clean_text(value):
    if type(value) is str and len(value) < 7:
        return value
    # Built-in string operations avoid dispatching to user-defined subclass
    # methods. This cheap no-op path matters for long public JSON/log output.
    # Escaped JSON keys can hide every literal hint, so unicode key escapes
    # still enter the bounded key parser.
    value = str.__str__(value)
    # Python re.I treats long s, Kelvin sign and both dotted/dotless I as
    # ASCII equivalents. The rejection hint must be at least as permissive;
    # lower() alone would skip text that the masking expressions recognize.
    lowered = str.casefold(value)
    if not value.isascii():
        lowered = lowered.replace("\u0131", "i").replace("i\u0307", "i")
    if not any(hint in lowered for hint in _TEXT_HINTS) and not ('"' in value and "\\u" in lowered):
        return value
    if '"' in value or "'" in value:
        value = _mask_fields(value)
        value = _mask_quoted_assignments(value)
    value = _LINE_ASSIGNMENT.sub(_line_assignment, value)
    value = _BASIC_AUTH.sub(lambda match: match[1] + _MASK, value)
    value = _BEARER_VALUE.sub("Bearer " + _MASK, value)
    return _SECRET_ASSIGNMENT.sub(_assignment, value)
