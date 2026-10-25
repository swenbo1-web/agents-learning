"""Bounded public fields from the transcript explicitly supplied by an AG hook.

The JSONL schema is checked, never inferred from tools, private databases or
model reasoning.  Unknown layouts leave a capture gap and do not gate the host.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time


MAX_SCAN_BYTES = 1024 * 1024
MAX_LINE_BYTES = 256 * 1024
MAX_RECORDS = 1024
READ_SECONDS = .15
_TRANSCRIPT_FILENAMES = ("transcript.jsonl", "transcript_full.jsonl")


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _failure(reason):
    return {"available": False, "reason": reason, "source": "antigravity_transcript"}


def _regular_path(raw):
    native, value, artifact = (raw.get(key) for key in
                               ("conversationId", "transcriptPath", "artifactDirectoryPath"))
    if not all(isinstance(item, str) and item and "\0" not in item for item in (native, value, artifact)):
        return None, "transcript_path_missing"
    try:
        path, root = Path(value), Path(artifact)
        if (not path.is_absolute() or not root.is_absolute()
                or any(part in native for part in ("/", "\\"))
                or value.startswith(("\\\\", "//")) or artifact.startswith(("\\\\", "//"))):
            return None, "transcript_path_invalid"
        if root.name != native or root.parent.name != "brain":
            return None, "transcript_conversation_mismatch"
        # App 2.23 supplies its full JSONL path; other supported surfaces use
        # the compact name. Read only the explicitly supplied native path,
        # never discover or fall back to a sibling transcript.
        logs = root / ".system_generated" / "logs"
        expected = {os.path.normcase(str(logs / name)) for name in _TRANSCRIPT_FILENAMES}
        if os.path.normcase(str(path)) not in expected:
            return None, "transcript_path_invalid"
        # Reject reparse points before resolving: a matching spelling must not
        # redirect a public-file read to another conversation or arbitrary file.
        for part in (path, *path.parents):
            info = part.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return None, "transcript_path_redirected"
        path = path.resolve(strict=True)
        if not stat.S_ISREG(path.stat().st_mode):
            return None, "transcript_not_regular"
        return path, None
    except (OSError, ValueError, RuntimeError):
        return None, "transcript_unavailable"


def read_public(raw):
    """Return only structural metadata and explicitly public content fields.

    A bounded tail is sufficient only when its caller can establish the current
    user/window boundary.  ``head_omitted`` never means the whole file was read.
    No source text, private field or filesystem exception is included in errors.
    """
    started = time.monotonic()
    path, reason = _regular_path(raw)
    if path is None:
        return _failure(reason)
    try:
        expected = path.stat(follow_symlinks=False)
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(expected.st_mode)
                    or (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino)):
                return _failure("transcript_not_regular")
            offset = max(0, before.st_size - MAX_SCAN_BYTES)
            stream.seek(offset)
            data = stream.read(MAX_SCAN_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            return _failure("transcript_changed_during_read")
        if len(data) > MAX_SCAN_BYTES:
            return _failure("transcript_scan_budget")
        if offset:
            _, separator, data = data.partition(b"\n")
            if not separator:
                return _failure("transcript_scan_budget")
        if data and not data.endswith(b"\n"):
            return _failure("transcript_incomplete_line")
        lines = data.splitlines()
        if len(lines) > MAX_RECORDS:
            return _failure("transcript_scan_budget")
        records, last_step = [], -1
        for line in lines:
            if time.monotonic() - started > READ_SECONDS:
                return _failure("transcript_read_deadline")
            if not line.strip():
                continue
            if len(line) > MAX_LINE_BYTES:
                return _failure("transcript_line_budget")
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                return _failure("transcript_invalid_json")
            if not isinstance(entry, dict):
                return _failure("transcript_schema_unknown")
            step = entry.get("step_index")
            if (isinstance(step, bool) or not isinstance(step, int) or not 0 <= step < 2 ** 53
                    or not all(isinstance(entry.get(key), str) for key in ("type", "source", "status"))):
                return _failure("transcript_schema_unknown")
            if step <= last_step:
                return _failure("transcript_step_conflict")
            last_step = step
            record = {key: entry[key] for key in ("step_index", "type", "source", "status")}
            public = ((entry["type"], entry["source"]) in {
                ("USER_INPUT", "USER_EXPLICIT"), ("PLANNER_RESPONSE", "MODEL")})
            if public and isinstance(entry.get("content"), str):
                # Escaped lone surrogates are valid JSON strings but cannot be
                # persisted as UTF-8 public text; leave a bounded capture gap.
                entry["content"].encode("utf-8")
                record["content"] = entry["content"]
                truncation = entry.get("truncated_fields", [])
                if (not isinstance(truncation, list) or len(truncation) > 32
                        or any(not isinstance(item, str) for item in truncation)):
                    return _failure("transcript_schema_unknown")
                record["truncated"] = "content" in truncation
            # Do not copy thinking, tool calls/results, arbitrary metadata or any
            # other text from the parsed source object into the public result.
            records.append(record)
            del entry
        if time.monotonic() - started > READ_SECONDS:
            return _failure("transcript_read_deadline")
        return {"available": True, "source": "antigravity_transcript", "records": records,
                "head_omitted": bool(offset), "last_step": last_step,
                "file_identity": _hash([before.st_dev, before.st_ino]),
                "path_digest": _hash(os.path.normcase(str(path)))}
    except (OSError, ValueError, OverflowError):
        return _failure("transcript_unavailable")
