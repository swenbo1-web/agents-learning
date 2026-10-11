"""稳定运行副本的发布身份；源码入口没有 release.json 时保持兼容。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from . import __version__


PROTOCOL_VERSION = "7"
MANIFEST_NAME = "release.json"
RELEASE_DIRECTORY = re.compile(r"^\d+\.\d+\.\d+-[0-9a-f]{64}$")


class ReleaseError(ValueError):
    """发布内容或协议不一致，入口必须停止使用此副本。"""


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _files(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file() and path != root / MANIFEST_NAME and path.suffix != ".pyc"
            and "__pycache__" not in path.parts}


def manifest_for(root):
    body = {"version": __version__, "protocol_version": PROTOCOL_VERSION, "files": _files(Path(root))}
    body["content_digest"] = hashlib.sha256(_canonical(body)).hexdigest()
    body["release_id"] = __version__ + "-" + body["content_digest"]
    return body


def validate_release_root(root):
    """逐文件核对固定发布副本；不会输出材料内容或异常详情。"""
    root = Path(root).resolve()
    try:
        manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ReleaseError("release manifest is invalid")
        expected = manifest_for(root)
        if manifest != expected:
            raise ReleaseError("release identity, protocol or content does not match")
        if RELEASE_DIRECTORY.fullmatch(root.name) and root.name != manifest["release_id"]:
            raise ReleaseError("release directory does not match its pinned identity")
        return manifest
    except ReleaseError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise ReleaseError("release manifest could not be verified") from None


def validate_entry(entry_path):
    """源码可直接运行，固定发布副本必须完整校验后再接入 Runtime。"""
    root = Path(entry_path).resolve().parents[1]
    if not (root / MANIFEST_NAME).exists():
        if RELEASE_DIRECTORY.fullmatch(root.name):
            raise ReleaseError("fixed runtime release manifest is missing")
        return None
    return validate_release_root(root)
