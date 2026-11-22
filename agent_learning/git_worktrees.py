"""Verify the on-disk identity of standard linked Git worktrees."""
from __future__ import annotations

from configparser import ConfigParser, Error as ConfigError
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

from .install_update import safe_path
from .release import filesystem_path


_MAX_GIT_POINTER = 64 * 1024
_MAX_GIT_CONFIG = 256 * 1024
_HEAD = re.compile(rb"(?:ref: refs/[^\r\n]+|[0-9a-f]{40}|[0-9a-f]{64})")
_CONFIG_SECTION = re.compile(r'(\[[A-Za-z0-9.-]+(?:[ \t]+"(?:[^"\\]|\\.)*")?\])[ \t]*(?:[;#].*)?')


@dataclass(frozen=True)
class LinkedWorktree:
    checkout: Path
    primary: Path
    git_dir: Path
    git_dir_identity: tuple[int, int]


def _read_regular(path: Path, limit: int) -> bytes:
    path = filesystem_path(safe_path(path))
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Git metadata is not a regular file")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Git metadata is too large")
    return data


def _read_line(path: Path, prefix: bytes = b"") -> bytes:
    line = _read_regular(path, _MAX_GIT_POINTER).rstrip(b"\r\n")
    if not line.startswith(prefix):
        raise ValueError("Git metadata has an invalid prefix")
    line = line[len(prefix):]
    if not line or any(byte < 32 for byte in line):
        raise ValueError("Git metadata must contain one value")
    return line


def _metadata_path(base: Path, data: bytes) -> Path:
    value = Path(os.fsdecode(data))
    if value.anchor and not value.is_absolute():
        raise ValueError("Git pointer has an ambiguous root")
    candidate = base / value
    if ".." in candidate.parts:
        # A relative Git pointer can use '..'. Validate each directory it
        # traverses before collapsing the path so a link cannot change meaning.
        current = Path(candidate.anchor)
        for part in candidate.parts[1:]:
            if part == "..":
                if current == current.parent or not filesystem_path(safe_path(current)).is_dir():
                    raise ValueError("Git pointer has an invalid parent")
                current = current.parent
            else:
                current /= part
        candidate = current
    return safe_path(candidate)


def _checkout_configuration(path: Path) -> None:
    """Accept ordinary local config and reject checkout indirection."""
    data = _read_regular(path, _MAX_GIT_CONFIG).decode("utf-8-sig")
    lines = []
    for line in data.splitlines():
        line = line.lstrip()
        if line.startswith("["):
            # Git allows assignments after a section header on the same line.
            # Reject that form because ConfigParser would silently drop them.
            section = _CONFIG_SECTION.fullmatch(line)
            if section is None:
                raise ValueError("Unsupported Git config section")
            line = section[1]
        elif line.rstrip().endswith("\\") and not line.startswith(("#", ";")):
            raise ValueError("Unsupported multiline Git configuration")
        lines.append(line)
    config = ConfigParser(delimiters=("=",), inline_comment_prefixes=("#", ";"),
                          interpolation=None, allow_no_value=True, strict=False,
                          default_section="\0")
    config.read_string("\n".join(lines))
    for section in config.sections():
        kind = section.strip().casefold()
        if re.match(r"include(?:if)?(?:[.\s]|$)", kind):
            raise ValueError("Git configuration includes are not checked for primary checkout identity")
        if kind == "core":
            values = config[section]
            bare = values.get("bare", "false")
            if bare and bare.startswith('"') and bare.endswith('"'):
                bare = bare[1:-1]
            if "worktree" in values or bare is None or bare.casefold() not in {"false", "no", "off", "0", ""}:
                raise ValueError("Git metadata does not describe an ordinary checkout")


def _nearest_checkout(workspace: Path) -> Path | None:
    for root in (workspace, *workspace.parents):
        try:
            info = filesystem_path(root / ".git").lstat()
        except FileNotFoundError:
            if root.name.lower() == "local" and root.parent.name.lower() == "build":
                break
            continue
        return root if stat.S_ISREG(info.st_mode) else None
    return None


def linked_worktree_identity(workspace: str | Path) -> LinkedWorktree | None:
    """Return a registered linked checkout and its primary checkout, if proven."""
    try:
        workspace = Path(workspace)
        if not workspace.is_absolute():
            return None
        workspace = safe_path(workspace)
        if not filesystem_path(workspace).is_dir():
            return None
        checkout = _nearest_checkout(workspace)
        if checkout is None:
            return None
        marker = checkout / ".git"
        registration = _metadata_path(checkout, _read_line(marker, b"gitdir: "))
        common = registration.parent.parent
        if registration.parent.name != "worktrees" or common.name != ".git":
            return None
        if (common.parent == checkout or not filesystem_path(common).is_dir()
                or _metadata_path(registration, _read_line(registration / "commondir")) != common
                or _metadata_path(registration, _read_line(registration / "gitdir")) != marker):
            return None
        if (not _HEAD.fullmatch(_read_line(registration / "HEAD"))
                or not _HEAD.fullmatch(_read_line(common / "HEAD"))
                or not filesystem_path(safe_path(common / "objects")).is_dir()):
            return None
        _checkout_configuration(common / "config")
        for git_dir in (common, registration):
            try:
                _checkout_configuration(git_dir / "config.worktree")
            except FileNotFoundError:
                pass
        info = filesystem_path(registration).stat()
        return LinkedWorktree(checkout=checkout, primary=common.parent, git_dir=registration,
                              git_dir_identity=(info.st_dev, info.st_ino))
    except (ConfigError, OSError, TypeError, ValueError, RuntimeError):
        return None
