"""Find the running Codex backend from process identity, without launching tools.

Only the current process's ancestors are considered. No command lines, other
processes' environments, installation-directory scans, or account files are read.
Process discoveries are deliberately not cached: every caller checks the live
ancestry and the selected file again. Only native API bindings are cached.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import stat


_MAX_ANCESTORS = 16
_MAX_PATH_PARTS = 128
_BACKEND_ENV = "ALP_CODEX_HOST_BACKEND"
_MAX_SNAPSHOT_CHARS = 16384


@dataclass(frozen=True)
class _ProcessImage:
    pid: int
    parent_pid: int
    created: int
    image: str


@dataclass(frozen=True)
class _BackendIdentity:
    process: _ProcessImage
    path: str
    file: tuple


def _unavailable():
    exc = FileNotFoundError("host Codex executable unavailable")
    exc.launch_reason = "host_cli_unavailable"
    return exc


@lru_cache(maxsize=1)
def _windows_api():
    import ctypes
    from ctypes import wintypes

    class BasicInformation(ctypes.Structure):
        _fields_ = [("reserved", ctypes.c_void_p), ("peb", ctypes.c_void_p),
                    ("reserved2", ctypes.c_void_p * 2), ("pid", ctypes.c_size_t),
                    ("parent_pid", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    native = ctypes.WinDLL("ntdll")
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    native.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                                                wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    native.NtQueryInformationProcess.restype = wintypes.LONG
    return ctypes, wintypes, kernel, native, BasicInformation


def _process_image(pid):
    """Read image, parent PID and creation time from one process handle.

    PROCESS_QUERY_LIMITED_INFORMATION grants metadata access, without access to
    process memory. Keeping one handle binds these fields to one PID generation.
    Unsupported platforms fail back to the existing explicit hint/PATH contract.
    """
    if os.name != "nt" or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        ctypes, wintypes, kernel, native, BasicInformation = _windows_api()
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            basic, size = BasicInformation(), wintypes.ULONG()
            if (native.NtQueryInformationProcess(handle, 0, ctypes.byref(basic),
                                                ctypes.sizeof(basic), ctypes.byref(size)) != 0
                    or basic.pid != pid):
                return None
            created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(system), ctypes.byref(user)):
                return None
            # An exited process retained by another handle is not a live ancestor.
            if exited.dwHighDateTime or exited.dwLowDateTime:
                return None
            length = wintypes.DWORD(32768)
            image = ctypes.create_unicode_buffer(length.value)
            if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
                return None
            creation = (created.dwHighDateTime << 32) | created.dwLowDateTime
            if not creation or not image.value:
                return None
            return _ProcessImage(pid, int(basic.parent_pid), creation, image.value)
        finally:
            kernel.CloseHandle(handle)
    except (OSError, ValueError, OverflowError, AttributeError):
        return None


def _backend_image(image):
    """Recognize the desktop's native backend layout, never the Codex UI.

    The opaque release-directory name is taken from the live image; no release
    directory is guessed or enumerated. Unknown same-name images stay ineligible.
    """
    path = Path(image)
    parents = path.parents
    return (path.name.lower() == "codex.exe" and len(parents) >= 4
            and parents[1].name.lower() == "bin" and parents[2].name.lower() == "codex"
            and parents[3].name.lower() == "openai")


def _workspace_root():
    current = Path.cwd().resolve(strict=True)
    for parent in (current, *current.parents)[:_MAX_PATH_PARTS]:
        try:
            (parent / ".git").lstat()
        except FileNotFoundError:
            continue
        return parent
    return current


def _file_identity(path, workspace):
    """Check all path components before resolving the selected executable."""
    path = Path(path)
    if (not path.is_absolute() or path.drive.startswith("\\\\") or ".." in path.parts
            or len(path.parts) > _MAX_PATH_PARTS):
        raise _unavailable()
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise _unavailable()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise _unavailable()
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(workspace)
    except ValueError:
        pass
    else:
        raise _unavailable()
    return resolved, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _ancestor_backend_identity():
    try:
        workspace = _workspace_root()
        current = _process_image(os.getpid())
        if current is None or current.pid != os.getpid():
            return None
        chain, seen = [current], {current.pid}
        for _ in range(_MAX_ANCESTORS):
            parent_pid = current.parent_pid
            if parent_pid <= 0 or parent_pid in seen:
                return None
            parent = _process_image(parent_pid)
            if (parent is None or parent.pid != parent_pid or parent.created <= 0
                    or parent.created > current.created):
                # A newer parent PID belongs to another generation of process.
                return None
            chain.append(parent)
            seen.add(parent_pid)
            current = parent
            if not _backend_image(parent.image):
                continue
            try:
                selected = _file_identity(parent.image, workspace)
                # Reopen every member to reject exits/reparenting/PID reuse while
                # traversing the chain, then check file replacement as well.
                if any(_process_image(item.pid) != item for item in chain):
                    raise _unavailable()
                if _file_identity(parent.image, workspace) != selected:
                    raise _unavailable()
                return _BackendIdentity(parent, str(selected[0]), selected[1])
            except (OSError, ValueError) as exc:
                raise _unavailable() from exc
        return None
    except (OSError, ValueError) as exc:
        if getattr(exc, "launch_reason", None) == "host_cli_unavailable":
            raise
        # No reliable ancestry was established; preserve the plain CLI fallback.
        return None


def _ancestor_backend():
    identity = _ancestor_backend_identity()
    return identity.path if identity is not None else None


def codex_background_environment(environment, *, windows=None):
    """Carry one verified backend across a short-lived Hook's process boundary.

    Only administrator/scheduler dispatchers call this helper. An inherited
    explicit hint stays authoritative. A failed recognized backend is carried
    as an unavailable snapshot, so custom commands remain usable while standard
    commands cannot silently fall back to another CLI after the Hook exits.
    """
    result = dict(environment)
    windows = os.name == "nt" if windows is None else windows
    if not windows or result.get("CODEX_CLI_PATH") or result.get(_BACKEND_ENV):
        return result
    try:
        identity = _ancestor_backend_identity()
        if identity is None:
            return result
        process = identity.process
        value = json.dumps({"version": 1, "pid": process.pid, "parent_pid": process.parent_pid,
                            "created": process.created, "image": process.image, "file": identity.file},
                           ensure_ascii=True, separators=(",", ":"))
        if len(value) > _MAX_SNAPSHOT_CHARS:
            raise _unavailable()
        result[_BACKEND_ENV] = value
    except (OSError, ValueError):
        result[_BACKEND_ENV] = "unavailable"
    return result


def _inherited_backend(value):
    """Recheck live backend generation and file identity, without the dead Hook."""
    try:
        if len(value) > _MAX_SNAPSHOT_CHARS:
            raise _unavailable()
        snapshot = json.loads(value)
        if (not isinstance(snapshot, dict)
                or set(snapshot) != {"version", "pid", "parent_pid", "created", "image", "file"}
                or type(snapshot["version"]) is not int or snapshot["version"] != 1
                or any(type(snapshot[name]) is not int or snapshot[name] <= 0
                       for name in ("pid", "parent_pid", "created"))
                or not isinstance(snapshot["image"], str) or not _backend_image(snapshot["image"])
                or not isinstance(snapshot["file"], list) or len(snapshot["file"]) != 5
                or any(type(item) is not int for item in snapshot["file"])):
            raise _unavailable()
        process = _ProcessImage(snapshot["pid"], snapshot["parent_pid"], snapshot["created"], snapshot["image"])
        if _process_image(process.pid) != process:
            raise _unavailable()
        workspace = _workspace_root()
        selected = _file_identity(process.image, workspace)
        if (selected[1] != tuple(snapshot["file"]) or _process_image(process.pid) != process
                or _file_identity(process.image, workspace) != selected):
            raise _unavailable()
        return str(selected[0])
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise _unavailable() from exc


def codex_host_launcher(*, windows=None):
    """Return (executable, source), or (None, None) when no host was identified."""
    hint = os.environ.get("CODEX_CLI_PATH")
    if hint:
        # Preserve the existing explicit host-hint contract, independently of
        # implicit ancestry discovery and the current project's directory.
        try:
            path = Path(hint)
            if (not path.is_absolute() or path.name.lower() not in {"codex", "codex.exe"}
                    or not path.is_file()):
                raise _unavailable()
            return str(path), "host_cli"
        except (OSError, ValueError) as exc:
            raise _unavailable() from exc
    if windows is None:
        windows = os.name == "nt"
    if windows:
        inherited = os.environ.get(_BACKEND_ENV)
        if inherited:
            return _inherited_backend(inherited), "host_ancestor"
        path = _ancestor_backend()
        if path is not None:
            return path, "host_ancestor"
    return None, None
