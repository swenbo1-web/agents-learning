"""Read static TOML project identity without interpreting examples as declarations."""
from __future__ import annotations

import json
import re


def _mask_multiline(text):
    result, index = [], 0
    while index < len(text):
        char = text[index]
        if char == "#":
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
        elif char in "\"'":
            start = index
            if text.startswith(char * 3, index):
                delimiter = char * 3
                end = text.find(delimiter, index + 3)
                while end >= 0 and char == '"':
                    preceding = end - 1
                    while preceding >= 0 and text[preceding] == "\\":
                        preceding -= 1
                    if (end - 1 - preceding) % 2 == 0:
                        break
                    end = text.find(delimiter, end + 1)
                if end < 0:
                    return None
                index = end + 3
                result.append("\n" * text[start:index].count("\n"))
            else:
                index += 1
                while index < len(text) and text[index] != char:
                    if text[index] == "\n":
                        return None
                    index += 2 if char == '"' and text[index] == "\\" else 1
                if index >= len(text):
                    return None
                index += 1
                result.append(text[start:index])
        else:
            result.append(char)
            index += 1
    return "".join(result)


def _fallback_fields(text, section):
    masked = _mask_multiline(text)
    if masked is None:
        return {}
    fields, active, depth = {}, False, 0
    for line in masked.splitlines():
        if depth == 0 and line.lstrip().startswith("["):
            active = bool(re.fullmatch(r"\s*\[" + re.escape(section) + r"\]\s*", line))
            continue
        if active and depth == 0:
            match = re.fullmatch(r'''\s*(name|version)\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*')\s*''', line)
            if match:
                if match[1] in fields:
                    return {}
                try:
                    fields[match[1]] = json.loads(match[2]) if match[2][0] == '"' else match[2][1:-1]
                except ValueError:
                    return {}
            elif re.match(r"\s*(name|version)\s*=", line):
                return {}
        structural = re.sub(r'''"(?:[^"\\]|\\.)*"|'[^']*' ''', "", line, flags=re.X)
        depth += sum(structural.count(char) for char in "[{") - sum(structural.count(char) for char in "]}")
        if depth < 0:
            return {}
    return fields if depth == 0 else {}


def toml_fields(text, section):
    try:
        import tomllib
    except ImportError:
        return _fallback_fields(text, section)
    try:
        value = tomllib.loads(text).get(section, {})
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}
