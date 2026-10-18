"""用户选择范围内的只读历史扫描；不执行资料，不读取模型内部思考。

此模块不初始化 Runtime、数据库或模型。宿主 transcript 是版本化的
输入格式，不保证能列出所有旧会话。未知结构和超限材料均明确留待处理。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote_plus


PARSER_VERSION = "history-text-1"
DEFAULT_OPTIONS = {
    "max_entries": 50000, "max_files": 2000, "max_file_bytes": 2 * 1024 * 1024,
    "max_total_bytes": 32 * 1024 * 1024, "max_chunks": 16384, "chunk_chars": 2000,
    "include": [], "exclude": [],
}
_OPTION_CEILINGS = {
    "max_entries": 200000, "max_files": 10000, "max_file_bytes": 16 * 1024 * 1024,
    "max_total_bytes": 256 * 1024 * 1024, "max_chunks": 65536, "chunk_chars": 32000,
}
_DOCUMENTS = {".md", ".markdown", ".txt", ".rst", ".adoc"}
_CODE = {
    ".py", ".pyi", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".cs", ".java", ".go", ".rs", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".swift", ".kt", ".kts", ".rb", ".php", ".lua", ".r", ".jl", ".dart",
    ".sh", ".bash", ".zsh", ".ps1", ".psm1", ".bat", ".cmd", ".sql", ".css",
    ".scss", ".sass", ".less", ".html", ".htm", ".vue", ".svelte", ".glsl",
    ".hlsl", ".usf", ".ush", ".ipynb",
}
_CONFIGURATION = {
    ".json", ".toml", ".yaml", ".yml", ".xml", ".ini", ".cfg", ".conf", ".config",
    ".cmake", ".properties", ".gradle", ".csproj", ".sln", ".vcxproj", ".props",
    ".targets", ".uproject", ".uplugin", ".csv", ".tsv", ".graphql", ".proto",
}
_TEXT_NAMES = {"dockerfile", "makefile", "cmakelists.txt", "license", "notice", ".gitignore", ".gitattributes", ".editorconfig"}
_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "bower_components", "vendor", "venv",
    ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
    "build", "dist", "target", "coverage", ".next", ".nuxt", "bin", "obj",
    "binaries", "intermediate", "deriveddatacache", "saved", "runtime-releases",
    "site-packages", ".tox", ".nox", ".idea",
    ".ssh", ".aws", ".gnupg", ".gcloud", "secrets", "credentials",
}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".sqlite", ".sqlite3", ".db"}
_SENSITIVE_NAMES = {
    "auth.json", "auth.toml", "credentials", "credentials.json", "credentials.yaml",
    "credentials.yml", "secrets.json", "secrets.yaml", "secrets.yml", "id_rsa", "id_ed25519",
    ".netrc", ".npmrc", ".pypirc", "config.json.secret",
}
_ROLES = {"unknown", "user", "assistant", "tool", "system", "developer"}
_INTERNAL_TYPES = {"reasoning", "thinking", "redacted_thinking", "analysis", "reasoning-chunks"}
_CODEX_RECORDS = {"session_meta", "response_item", "event_msg", "turn_context", "compacted"}
_AG_RECORDS = {"user", "assistant", "tool", "message", "system", "developer"}
_DSH_RECORDS = {
    "session/header", "turn/start", "turn/end", "step/start", "step/end",
    "user/message", "developer/message", "system/message", "assistant/message",
    "assistant/attempt", "tool/call", "tool/result", "request/header",
    "request/context", "session/end-seed",
}
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9 ]*PRIVATE KEY)-----.*?(?:-----END (?:[A-Z0-9 ]*PRIVATE KEY)-----|\Z)", re.S)
_QUERY_SECRET = re.compile(r"(?i)([?&](?:sid|token|email_token|access_token|refresh_token|api[_-]?key|auth|authorization|password|secret)=)[^&#\s<>\"']+")
_TOKEN = re.compile(r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{25,})\b")
_ASSIGNMENT_SECRET = re.compile(r"(?i)((?:[\"']?(?:api[_-]?key|password|secret|access_token|refresh_token|authorization|token)[\"']?)\s*[=:]\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\r\n]+)")
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_URL_QUERY = re.compile(r"([?&]([^=&#\s<>\"']+)=)([^&#\s<>\"']*)")
_QUERY_KEYS = {"sid", "token", "email_token", "access_token", "refresh_token", "api_key", "apikey", "api-key", "auth", "authorization", "password", "secret"}
_REDACTION_MARKER = re.compile(r"\[REDACTED(?: PRIVATE KEY)?\]")
_MAX_MARKER_CHARS = len("[REDACTED PRIVATE KEY]")


class HistorySourceError(ValueError):
    """选择或预算无效；文件级不可解析则保存在扫描结果中。"""


class _Skip(Exception):
    def __init__(self, reason):
        self.reason = reason
        self.bytes_read = 0


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_encoded(value).encode("utf-8")).hexdigest()


def _named(value, names):
    return isinstance(value, str) and value in names


def _sanitize(text):
    # 延迟引入纯 clean 函数，避免模块组合时产生循环导入；不构造 Runtime。
    from .runtime import clean
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    def plain(value):
        value = _TOKEN.sub("[REDACTED]", value)
        value = _ASSIGNMENT_SECRET.sub(r"\1[REDACTED]", value)
        return clean(value)
    # URL 单独处理，避免基础 clean 把 token 后的非敏感 query 一并吞掉。
    result, position = [], 0
    for match in _URL.finditer(text):
        result.append(plain(text[position:match.start()]))
        url = _URL_QUERY.sub(lambda found: found[1] + "[REDACTED]"
                             if unquote_plus(found[2]).casefold() in _QUERY_KEYS else found[0], match[0])
        url = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", url, flags=re.I)
        result.append(_TOKEN.sub("[REDACTED]", url))
        position = match.end()
    result.append(plain(text[position:]))
    return "".join(result)


def _options(options):
    if options is not None and not isinstance(options, dict):
        raise HistorySourceError("history options must be an object")
    supplied = options or {}
    if set(supplied) - set(DEFAULT_OPTIONS):
        raise HistorySourceError("unsupported history scan option")
    result = {**DEFAULT_OPTIONS, **supplied}
    for name, ceiling in _OPTION_CEILINGS.items():
        value = result[name]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
            raise HistorySourceError(f"{name} must be an integer between 1 and {ceiling}")
    for name in ("include", "exclude"):
        patterns = result[name]
        if isinstance(patterns, str):
            patterns = [patterns]
        if (not isinstance(patterns, list) or len(patterns) > 100
                or any(not isinstance(item, str) or not item.strip() or len(item) > 500 for item in patterns)):
            raise HistorySourceError(f"{name} must contain bounded glob strings")
        if any(Path(item).is_absolute() or ".." in item.replace("\\", "/").split("/") for item in patterns):
            raise HistorySourceError("history globs must stay relative to their source root")
        result[name] = [item.replace("\\", "/") for item in patterns]
    return result


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _version(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _absolute_selection(value):
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise HistorySourceError("an explicit existing source path is required")
    path = Path(value).expanduser()
    if ".." in path.parts:
        raise HistorySourceError("history source path cannot contain traversal")
    path = Path(os.path.abspath(path))
    try:
        for part in (path, *path.parents):
            if _linked(part.lstat()):
                raise HistorySourceError("history source cannot pass through links or junctions")
        information = path.lstat()
    except OSError as exc:
        raise HistorySourceError("history source is missing or unreadable") from exc
    if not (stat.S_ISREG(information.st_mode) or stat.S_ISDIR(information.st_mode)):
        raise HistorySourceError("history source must be a regular file or directory")
    # Windows 8.3 用户目录别名与句柄返回的长路径必须使用同一规范形式。
    resolved = path.resolve(strict=True)
    for part in (resolved, *resolved.parents):
        if _linked(part.lstat()):
            raise HistorySourceError("history source cannot pass through links or junctions")
    return resolved


def _matches(locator, patterns):
    value = locator.casefold()
    for original in patterns:
        pattern = original.casefold()
        if fnmatch.fnmatchcase(value, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatchcase(value, pattern[3:]):
            return True
    return False


def _sensitive(path):
    name = path.name.casefold()
    return (name in _SENSITIVE_NAMES or name == ".env" or name.startswith(".env.")
            or name.endswith(".env") or path.suffix.casefold() in _SENSITIVE_SUFFIXES
            or name.startswith("credentials.") or name.startswith("auth.json.")
            or name.startswith("id_rsa.") or name.startswith("id_ed25519."))


def _kind(path, requested):
    if requested == "conversation":
        return "conversation" if path.suffix.casefold() in {".json", ".jsonl", ".md", ".txt", ".markdown"} else None
    suffix, name = path.suffix.casefold(), path.name.casefold()
    if suffix == ".jsonl":
        return "conversation"
    if suffix in _DOCUMENTS:
        return "document"
    if suffix in _CODE:
        return "code"
    if suffix in _CONFIGURATION or name in _TEXT_NAMES:
        return "configuration"
    return None


def _priority(path, kind):
    name = path.name.casefold()
    if name.startswith("readme") or name in {"agents.md", "agent.md"}:
        return 0
    if kind == "document":
        hints = ("summary", "decision", "handoff", "walkthrough", "implementation_plan", "复盘", "总结", "决策", "备忘")
        return 1 if any(hint in name for hint in hints) else 2
    return {"conversation": 2, "configuration": 3, "code": 4}[kind]


def _contains_chat_structure(value):
    """嵌套聊天或内部思考字段不能通过普通配置分类绕过消息白名单。"""
    stack, visited = [value], 0
    while stack:
        current = stack.pop()
        visited += 1
        if visited > 65536:
            raise _Skip("budget_json_structure")
        if isinstance(current, dict):
            if ("role" in current and "content" in current or "messages" in current
                    or _named(current.get("type"), _CODEX_RECORDS | _INTERNAL_TYPES)
                    or any(key in current for key in ("trajectory", "conversationHistory"))
                    or any(isinstance(current.get(key), (str, list, dict)) and bool(current.get(key))
                           for key in ("reasoning", "thinking", "reasoning_content", "thinking_content", "encrypted_content"))):
                return True
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return False


def _open_file(path, root, root_info):
    """POSIX 逐层 no-follow；Windows 在读取前验证实际句柄路径和版本。"""
    for ancestor in (path, *path.parents):
        if _linked(ancestor.lstat()):
            raise _Skip("link_or_junction")
    if _version(root.lstat())[:2] != _version(root_info)[:2]:
        raise _Skip("source_changed")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if os.name != "nt" and os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
        directory_flags = flags | os.O_NOFOLLOW | os.O_DIRECTORY
        current = os.open(root, directory_flags)
        try:
            if _version(os.fstat(current))[:2] != _version(root_info)[:2]:
                raise _Skip("source_changed")
            parts = path.relative_to(root).parts
            for part in parts[:-1]:
                following = os.open(part, directory_flags, dir_fd=current)
                os.close(current)
                current = following
            return os.open(parts[-1], flags | os.O_NOFOLLOW, dir_fd=current)
        finally:
            os.close(current)
    handle = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))
    try:
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            function = kernel.GetFinalPathNameByHandleW
            function.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
            function.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            length = function(msvcrt.get_osfhandle(handle), buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                raise _Skip("path_verification_failed")
            actual = buffer.value
            if actual.startswith("\\\\?\\UNC\\"):
                actual = "\\\\" + actual[8:]
            elif actual.startswith("\\\\?\\"):
                actual = actual[4:]
            if os.path.normcase(os.path.abspath(actual)) != os.path.normcase(str(path)):
                raise _Skip("path_changed")
        return handle
    except BaseException:
        os.close(handle)
        raise


def _read_file(path, root, root_info, max_bytes):
    before = path.lstat()
    if _linked(before) or not stat.S_ISREG(before.st_mode):
        raise _Skip("link_or_nonregular")
    if before.st_size > max_bytes:
        raise _Skip("budget_file_bytes")
    handle = _open_file(path, root, root_info)
    data = []
    try:
        opened = os.fstat(handle)
        # Windows lstat/fstat 对 ctime 的表达不同；只在各自方法内比较 ctime。
        if _linked(opened) or not stat.S_ISREG(opened.st_mode) or _version(opened)[:4] != _version(before)[:4]:
            raise _Skip("source_changed")
        remaining = before.st_size
        while remaining:
            piece = os.read(handle, min(remaining, 65536))
            if not piece:
                raise _Skip("source_changed")
            data.append(piece)
            remaining -= len(piece)
        if (_version(os.fstat(handle)) != _version(opened)
                or _version(path.lstat()) != _version(before)):
            raise _Skip("source_changed")
        return b"".join(data), {"size_bytes": before.st_size, "mtime_ns": before.st_mtime_ns,
                                "ctime_ns": before.st_ctime_ns, "device": before.st_dev, "inode": before.st_ino}
    except (_Skip, OSError) as exc:
        # 变化或读取失败也消耗总读取额度，不能通过失败无限绕过预算。
        exc.bytes_read = sum(len(piece) for piece in data)
        raise
    finally:
        os.close(handle)


def safe_segment_end(text, start, limit):
    """返回绝对片段终点，limit 是本段字符预算；不切开完整脱敏标记。

    预算不足以容纳起点处的完整标记，或 start 已在标记内部时明确拒绝。
    此函数不会通过超过 limit 保全标记，供扫描器及送审二次分段共用。
    """
    if (not isinstance(text, str) or isinstance(start, bool) or not isinstance(start, int)
            or isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
            or not 0 <= start <= len(text)):
        raise HistorySourceError("invalid text segment offset or budget")
    end = min(len(text), start + limit)
    for marker in _REDACTION_MARKER.finditer(
            text, max(0, start - _MAX_MARKER_CHARS + 1), min(len(text), end + _MAX_MARKER_CHARS)):
        if marker.start() < start < marker.end():
            raise HistorySourceError("segment offset falls inside a redaction marker")
        if marker.start() < end < marker.end():
            if marker.start() == start:
                raise HistorySourceError("chunk budget cannot preserve a complete redaction marker")
            end = marker.start()
            break
    return end


def _segments(text, chars, locator=None, *, max_chunks=65536):
    result, start, line, part = [], 0, 1, 0
    while start < len(text):
        if len(result) >= max_chunks:
            raise _Skip("budget_chunks")
        try:
            end = safe_segment_end(text, start, chars)
        except HistorySourceError:
            raise _Skip("budget_redaction_marker") from None
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline >= start + chars // 2:
                end = newline + 1
        body = text[start:end]
        span = {"line_start": line, "line_end": line + body.count("\n") - int(body.endswith("\n")), "part": part}
        if locator:
            span.update(locator)
        result.append({"index": len(result), "text": body, "locator": span})
        line += body.count("\n")
        start, part = end, part + 1
    return result


def _text_blocks(content):
    if isinstance(content, str):
        return content, 0
    if not isinstance(content, list):
        raise _Skip("unparsed_message_content")
    visible, excluded = [], 0
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise _Skip("unparsed_message_block")
        kind = block["type"]
        if kind in _INTERNAL_TYPES:
            excluded += 1
            continue
        field = "refusal" if kind == "refusal" else "text"
        if kind not in {"text", "input_text", "output_text", "refusal"} or not isinstance(block.get(field), str):
            raise _Skip("unparsed_message_block")
        visible.append(block[field])
    return "\n".join(visible), excluded


def _message(record, index, *, envelope_timestamp=None):
    if not isinstance(record, dict):
        raise _Skip("unparsed_message")
    if _named(record.get("channel"), _INTERNAL_TYPES) or _named(record.get("type"), _INTERNAL_TYPES):
        return None, 1
    role = record.get("role")
    if not _named(role, _ROLES):
        raise _Skip("unparsed_message_role")
    if "content" not in record:
        raise _Skip("unparsed_message_content")
    text, excluded = _text_blocks(record["content"])
    timestamp = record.get("timestamp")
    if timestamp is None:
        timestamp = record.get("created_at")
    timestamp_source = "message" if timestamp is not None else "record" if envelope_timestamp is not None else None
    if timestamp is None:
        timestamp = envelope_timestamp
    ident = record.get("id")
    if timestamp is not None and (not isinstance(timestamp, str) or len(timestamp) > 200):
        raise _Skip("unparsed_message_timestamp")
    if envelope_timestamp is not None and (not isinstance(envelope_timestamp, str) or len(envelope_timestamp) > 200):
        raise _Skip("unparsed_record_timestamp")
    if ident is not None and (not isinstance(ident, str) or len(ident) > 500):
        raise _Skip("unparsed_message_id")
    return {"text": _sanitize(text), "role": role, "timestamp": _sanitize(timestamp) if timestamp else None,
            "record_timestamp": _sanitize(envelope_timestamp) if envelope_timestamp else None,
            "timestamp_source": timestamp_source,
            "message_id": _sanitize(ident) if ident else None, "message_index": index}, excluded


def _conversation(text, suffix, chars, max_chunks):
    if suffix in {".md", ".txt", ".markdown"}:
        cleaned = _sanitize(text)
        chunks = _segments(cleaned, chars, {"role": "unknown", "timestamp": None}, max_chunks=max_chunks)
        return cleaned, chunks, "partial", {"format": "plain-text", "author_role": "unknown"}, cleaned
    try:
        if suffix == ".json":
            document = json.loads(text)
            if not isinstance(document, dict) or not isinstance(document.get("messages"), list):
                raise _Skip("unparsed_conversation_format")
            coverage = document.get("coverage", "partial")
            if not _named(coverage, {"full", "partial", "summary"}):
                raise _Skip("unparsed_conversation_coverage")
            records, format_name = document["messages"], "messages-json"
            conversation = document.get("conversation_id")
        else:
            records = []
            for line in text.splitlines():
                if line.strip():
                    if len(records) >= 65536:
                        raise _Skip("budget_message_records")
                    records.append(json.loads(line))
            coverage, conversation = "partial", None
            format_name = "messages-jsonl"
    except (ValueError, TypeError, RecursionError):
        raise _Skip("unparsed_conversation_json") from None
    if len(records) > 65536:
        raise _Skip("budget_message_records")
    if conversation is not None and (not isinstance(conversation, str) or len(conversation) > 500):
        raise _Skip("unparsed_conversation_id")
    messages, excluded, omitted = [], 0, 0
    styles = {"standard" if isinstance(row, dict) and "role" in row else "typed" for row in records}
    if len(styles) > 1:
        raise _Skip("unparsed_mixed_conversation_format")
    typed = "typed" in styles
    if typed and suffix == ".json":
        raise _Skip("unparsed_conversation_format")
    host = None
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise _Skip("unparsed_conversation_record")
        current = record
        if typed:
            kind = record.get("type")
            if kind is None and index == 0 and {"version", "id", "isSeeded"} <= set(record):
                kind = "session/header"
            if not isinstance(kind, str):
                raise _Skip("unparsed_host_record")
            if kind in _CODEX_RECORDS:
                if host in {"antigravity", "dsh"}:
                    raise _Skip("unparsed_mixed_host_format")
                host, format_name = "codex", "codex-public-jsonl"
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    raise _Skip("unparsed_host_payload")
                if kind == "session_meta":
                    candidate_id = payload.get("id")
                    if isinstance(candidate_id, str) and len(candidate_id) <= 500:
                        conversation = candidate_id
                    continue
                if kind != "response_item":
                    # 不检查、保存 event_msg/turn_context/compacted 的内部正文。
                    omitted += 1
                    continue
                item_type = payload.get("type")
                if not isinstance(item_type, str):
                    raise _Skip("unparsed_public_response_item")
                if item_type in _INTERNAL_TYPES:
                    excluded += 1
                    continue
                if item_type == "message":
                    current = payload
                elif item_type == "function_call_output" and isinstance(payload.get("output"), str):
                    current = {"role": "tool", "content": payload["output"]}
                    current.update({key: payload[key] for key in ("timestamp", "created_at", "id") if key in payload})
                elif item_type in {"function_call", "custom_tool_call"}:
                    field = "arguments" if item_type == "function_call" else "input"
                    if not isinstance(payload.get(field), str) or not isinstance(payload.get("name"), str):
                        raise _Skip("unparsed_public_tool_item")
                    current = {"role": "assistant", "content": "[tool_call " + payload["name"] + "]\n" + payload[field]}
                    current.update({key: payload[key] for key in ("timestamp", "created_at", "id") if key in payload})
                elif item_type == "custom_tool_call_output" and isinstance(payload.get("output"), str):
                    current = {"role": "tool", "content": payload["output"]}
                    current.update({key: payload[key] for key in ("timestamp", "created_at", "id") if key in payload})
                else:
                    raise _Skip("unparsed_public_response_item")
            elif kind in _DSH_RECORDS:
                if host in {"codex", "antigravity"}:
                    raise _Skip("unparsed_mixed_host_format")
                host, format_name = "dsh", "dsh-session-jsonl"
                if kind == "session/header":
                    header = record.get("data") if isinstance(record.get("data"), dict) else record
                    candidate_id = header.get("id")
                    if isinstance(candidate_id, str) and len(candidate_id) <= 500:
                        conversation = candidate_id
                    continue
                if kind in {"turn/start", "turn/end", "step/start", "step/end", "request/header", "request/context", "session/end-seed"}:
                    omitted += 1
                    continue
                if kind == "assistant/attempt":
                    excluded += 1
                    continue
                data = record.get("data")
                if not isinstance(data, dict):
                    raise _Skip("unparsed_host_payload")
                if kind == "user/message":
                    current = data
                elif kind in {"developer/message", "system/message"}:
                    current = data.get("message")
                    if not isinstance(current, dict):
                        raise _Skip("unparsed_host_message")
                elif kind == "assistant/message":
                    msg = data.get("message")
                    if not isinstance(msg, dict) or not isinstance(msg.get("content"), (str, list)):
                        raise _Skip("unparsed_host_message")
                    if isinstance(msg["content"], list):
                        filtered_blocks = []
                        for blk in msg["content"]:
                            if isinstance(blk, dict) and blk.get("type") in {"tool-call", "tool-addition", "tool-removal"}:
                                omitted += 1
                                continue
                            filtered_blocks.append(blk)
                        current = {**msg, "content": filtered_blocks}
                    else:
                        current = msg
                elif kind == "tool/call":
                    if not isinstance(data.get("name"), str) or not isinstance(data.get("arguments"), str):
                        raise _Skip("unparsed_public_tool_item")
                    current = {"role": "assistant", "content": "[tool_call " + data["name"] + "]\n" + data["arguments"]}
                    if isinstance(data.get("callId"), str):
                        current["id"] = data["callId"]
                elif kind == "tool/result":
                    msg = data.get("message")
                    if not isinstance(msg, dict) or "content" not in msg:
                        raise _Skip("unparsed_host_message")
                    current = {"role": "tool", "content": msg["content"]}
                    if isinstance(msg.get("toolCallId"), str):
                        current["id"] = msg["toolCallId"]
            elif kind in _AG_RECORDS:
                if host in {"codex", "dsh"}:
                    raise _Skip("unparsed_mixed_host_format")
                # 只接受明确消息包装，不猜 steps/trajectory 等宿主内部格式。
                host, format_name = "antigravity", "public-message-jsonl"
                current = record.get("message")
                if not isinstance(current, dict):
                    raise _Skip("unparsed_host_message")
                if "role" not in current and kind in _ROLES:
                    current = {**current, "role": kind}
            elif kind in _INTERNAL_TYPES:
                excluded += 1
                continue
            else:
                raise _Skip("unparsed_host_record")
        envelope_time = record.get("timestamp", record.get("created_at")) if typed else None
        if envelope_time is None and typed and host == "dsh" and isinstance(record.get("time"), (int, float)) and not isinstance(record.get("time"), bool):
            envelope_time = str(record["time"])
        message, hidden = _message(current, index, envelope_timestamp=envelope_time)
        excluded += hidden
        if message is not None and message["text"]:
            messages.append(message)
    if not messages:
        raise _Skip("no_observable_messages")
    if excluded or omitted or typed:
        coverage = "summary" if coverage == "summary" else "partial"
    chunks, sections = [], []
    for message in messages:
        locator = {key: message[key] for key in ("role", "timestamp", "record_timestamp", "timestamp_source", "message_id", "message_index")}
        pieces = _segments(message["text"], chars, locator, max_chunks=max_chunks - len(chunks))
        if len(chunks) + len(pieces) > max_chunks:
            raise _Skip("budget_chunks")
        for piece in pieces:
            piece["index"] = len(chunks)
            chunks.append(piece)
        sections.append(f"[{message['role']}]\n" + message["text"])
    metadata = {"format": format_name, "observable_messages": len(messages),
                "excluded_internal_records": excluded, "omitted_nonmessage_records": omitted}
    if conversation:
        metadata["conversation_id"] = _sanitize(conversation)
    return "\n\n".join(sections), chunks, coverage, metadata, messages


def _project_configuration(raw):
    """Exclude only our MCP tables; preserve user tables and multiline values."""
    from .codex_connect import _statements, _key, SERVER, PLUGIN
    from .runtime import ContractError
    result, section, removed = [], (), False
    try:
        for _, _, text in _statements(raw):
            stripped = text.strip()
            if stripped.startswith("["):
                match = re.fullmatch(r"\[(.*?)\]\s*(?:#[^\n]*)?", stripped)
                if not match:
                    return raw, False
                section = _key(match[1].strip())
            owned = any(section[:len(prefix)] == prefix for prefix in (SERVER, PLUGIN))
            if not owned:
                result.append(text)
            else:
                removed = True
    except (ContractError, ValueError):
        return raw, False
    return "".join(result), removed


def scan_sources(workspace, extra_sources=None, options=None, *, prioritize_paths=None, only_paths=None):
    """预览明确选择的项目和额外来源；所有来源共用预算，不持久写入。

    extra_sources 为 [{path: 明确路径, kind: conversation|artifacts}]；kind
    可写为 mode。conversation 仅解析公开文本，不保证完整宿主聊天覆盖。
    返回来源的 content/chunks 已脱敏；完整文件超限时留待处理，不裁剪正文。
    """
    configuration = _options(options)
    selected = _absolute_selection(workspace)
    if not selected.is_dir():
        raise HistorySourceError("workspace must be an existing directory")
    if extra_sources is None:
        extra_sources = []
    if not isinstance(extra_sources, list) or len(extra_sources) > 16:
        raise HistorySourceError("extra_sources must contain at most 16 explicitly selected roots")
    roots = []
    for extra in extra_sources:
        if not isinstance(extra, dict) or set(extra) - {"path", "kind", "mode"}:
            raise HistorySourceError("invalid extra source selection")
        requested = extra.get("kind", extra.get("mode"))
        if not _named(requested, {"conversation", "artifacts"}) or "path" not in extra:
            raise HistorySourceError("extra source requires path and conversation or artifacts kind")
        if "kind" in extra and "mode" in extra and extra["kind"] != extra["mode"]:
            raise HistorySourceError("extra source kind and mode disagree")
        roots.append((_absolute_selection(extra["path"]), requested))
    # 显式对话选择优先于项目内同一个 JSON 文件的普通配置分类。
    roots.sort(key=lambda pair: 0 if pair[1] == "conversation" else 1)
    roots.append((selected, "project"))
    result = {"workspace": str(selected), "sources": [], "skipped": [], "pending": [], "incomplete": False,
              "stats": {"entries_seen": 0, "files_considered": 0, "files_read": 0,
                        "files_attempted": 0, "files_included": 0, "bytes_read": 0, "chunks": 0, "unlisted_entries": 0},
              "options": configuration}
    candidates, seen = [], set()
    selected_paths = None if only_paths is None else {os.path.normcase(str(path)) for path in only_paths}

    def note(path, reason, *, pending=False):
        record = {"path": str(path), "reason": reason}
        if reason == "budget_entries":
            record.update(coverage="partial", remaining_entries="unknown")
        result["skipped"].append(record)
        if pending:
            result["pending"].append(record.copy())
            result["incomplete"] = True

    for selection, requested in roots:
        root = selection if selection.is_dir() else selection.parent
        root_info = root.lstat()
        stack = [(selection, False)]
        while stack:
            current, counted = stack.pop()
            if not counted:
                if result["stats"]["entries_seen"] >= configuration["max_entries"]:
                    note(current, "budget_entries", pending=True)
                    result["stats"]["unlisted_entries"] += 1
                    continue
                result["stats"]["entries_seen"] += 1
            try:
                information = current.lstat()
            except OSError:
                note(current, "unreadable", pending=True)
                continue
            if _linked(information):
                note(current, "link_or_junction", pending=True)
                continue
            relative = current.relative_to(root).as_posix()
            if current != root and (_matches(relative, configuration["exclude"])
                                    or _matches(relative + "/", configuration["exclude"])):
                note(current, "excluded_by_user")
                continue
            if stat.S_ISDIR(information.st_mode):
                parts = tuple(part.casefold() for part in current.parts)
                excluded = (current != selected and current.name.casefold() in _EXCLUDED_DIRS
                            or any(parts[index:index + 2] in {(".agents", "plugins"), (".codex", "plugins")}
                                   for index in range(len(parts) - 1))
                            or (current / "release.json").is_file() and (current / "agent_learning").is_dir())
                if excluded:
                    note(current, "excluded_directory")
                    continue
                children = []
                try:
                    with os.scandir(current) as iterator:
                        for entry in iterator:
                            if result["stats"]["entries_seen"] >= configuration["max_entries"]:
                                note(current, "budget_entries", pending=True)
                                result["stats"]["unlisted_entries"] += 1
                                break
                            result["stats"]["entries_seen"] += 1
                            children.append(Path(entry.path))
                except OSError:
                    note(current, "unreadable_directory", pending=True)
                    continue
                stack.extend((child, True) for child in sorted(
                    children, key=lambda path: (path.name.casefold(), path.name), reverse=True))
                continue
            if not stat.S_ISREG(information.st_mode):
                note(current, "nonregular_file", pending=True)
                continue
            identity = os.path.normcase(str(current))
            if identity in seen:
                note(current, "already_selected")
                continue
            seen.add(identity)
            result["stats"]["files_considered"] += 1
            if selected_paths is not None and identity not in selected_paths:
                note(current, "outside_remaining_scope")
                continue
            if _sensitive(current):
                note(current, "sensitive_file")
                continue
            if configuration["include"] and not _matches(relative, configuration["include"]):
                note(current, "not_included_by_user")
                continue
            kind = _kind(current, requested)
            if kind is None:
                note(current, "unparsed_file_type", pending=True)
                continue
            candidates.append((_priority(current, kind), str(current).casefold(), current, root, root_info, kind, requested))

    priority_paths = {os.path.normcase(str(path)) for path in prioritize_paths or []}
    for priority, _, path, root, root_info, kind, requested in sorted(candidates, key=lambda row: (
            os.path.normcase(str(row[2])) not in priority_paths, row[0], row[1], str(row[2]))):
        stats = result["stats"]
        if stats["files_attempted"] >= configuration["max_files"]:
            note(path, "budget_files", pending=True)
            continue
        remaining = configuration["max_total_bytes"] - stats["bytes_read"]
        try:
            size = path.lstat().st_size
            if size > configuration["max_file_bytes"]:
                raise _Skip("budget_file_bytes")
            if size > remaining:
                raise _Skip("budget_total_bytes")
            stats["files_attempted"] += 1
            data, read_stat = _read_file(path, root, root_info, min(configuration["max_file_bytes"], remaining))
            stats["files_read"] += 1
            stats["bytes_read"] += len(data)
            if b"\x00" in data:
                raise _Skip("unparsed_binary")
            try:
                raw = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise _Skip("unparsed_encoding") from None
            if not raw:
                raise _Skip("empty_source")
            relative_path = path.relative_to(root).as_posix().casefold()
            if relative_path == ".codex/environments/agent-learning.toml" and raw.startswith(
                    "# Agent Learning managed project action; update through codex-action."):
                raise _Skip("plugin_generated_content")
            if relative_path == ".codex/environments/agent-learning.action.json":
                try:
                    managed = json.loads(raw)
                except ValueError:
                    managed = None
                if isinstance(managed, dict) and managed.get("kind") == "agent-learning-codex-action" and managed.get("schema_version") == 1:
                    raise _Skip("plugin_generated_content")
            omitted_plugin = False
            if relative_path == ".codex/config.toml":
                raw, omitted_plugin = _project_configuration(raw)
                if omitted_plugin and not raw.strip():
                    raise _Skip("plugin_generated_content")
            if kind != "conversation" and path.suffix.casefold() in {".json", ".ipynb"}:
                # 已批准项目内的聊天 JSON 自动进入白名单解析，不能回退配置全文。
                try:
                    structure = json.loads(raw)
                except (ValueError, RecursionError):
                    raise _Skip("unparsed_json_structure") from None
                if isinstance(structure, dict) and "messages" in structure:
                    kind = "conversation"
                elif (_contains_chat_structure(structure)
                      or re.match(r"(?i)(?:transcript|rollout)(?:[._-]|$)", path.name)):
                    raise _Skip("unparsed_conversation_format")
            max_chunks = configuration["max_chunks"] - stats["chunks"]
            if kind == "conversation":
                content, chunks, coverage, metadata, normalized = _conversation(
                    raw, path.suffix.casefold(), configuration["chunk_chars"], max_chunks)
            else:
                content = _sanitize(raw)
                chunks = _segments(content, configuration["chunk_chars"], max_chunks=max_chunks)
                coverage, normalized = "full", content
                metadata = {"author_role": "unknown", "format": path.suffix.casefold() or path.name.casefold(),
                            "agent_artifact_hint": kind == "document" and priority == 1,
                            "selection_kind": requested}
                if omitted_plugin:
                    coverage = "partial"
                    metadata["excluded_plugin_configuration"] = True
            metadata["read_stat"] = read_stat
            if len(chunks) > max_chunks:
                raise _Skip("budget_chunks")
            source = {"source_key": _hash({"path": os.path.normcase(str(path))}), "path": str(path),
                      "root": str(root), "relative_path": path.relative_to(root).as_posix(), "kind": kind,
                      "coverage": coverage, "priority": _priority(path, kind), "content_hash": _hash(
                          {"kind": kind, "coverage": coverage, "material": normalized}),
                      "content": content, "chunks": chunks, "size_bytes": len(data),
                      "parser_version": PARSER_VERSION, "metadata": metadata}
            result["sources"].append(source)
            stats["files_included"] += 1
            stats["chunks"] += len(chunks)
        except _Skip as exc:
            stats["bytes_read"] += exc.bytes_read
            note(path, exc.reason, pending=exc.reason not in {"empty_source", "plugin_generated_content"})
        except (OSError, ValueError) as exc:
            stats["bytes_read"] += getattr(exc, "bytes_read", 0)
            note(path, "unreadable_or_changed", pending=True)
    result["stats"].update(skipped=len(result["skipped"]), pending=len(result["pending"]), sources=len(result["sources"]))
    result["sources"].sort(key=lambda source: (source["priority"], source["path"].casefold(), source["path"]))
    return result
