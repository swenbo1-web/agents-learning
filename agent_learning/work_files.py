"""明确工具字段的文件关系投影；不执行命令、不读取文件正文。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex


def checkout_root(workspace):
    """最近 Git 根目录标记；worktree 的 .git 文件保持自己的 checkout 身份。"""
    origin = Path(workspace).resolve()
    for current in (origin, *origin.parents):
        try:
            if (current / ".git").exists():
                return current
            if current.name.lower() == "local" and current.parent.name.lower() == "build":
                break
        except OSError:
            break
    return origin


def checkout_key(workspace):
    root = os.path.normcase(str(checkout_root(workspace)))
    return hashlib.sha256(root.encode("utf-8")).hexdigest()


def normalize_file_target(value, workspace, *, observe_version=True):
    """仅接收当前 checkout 内目标；链接不能越过根目录。mtime 不是内容摘要。"""
    if (not isinstance(value, str) or not value.strip() or len(value) > 2000
            or "\0" in value or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value)):
        return None
    try:
        cwd = Path(workspace).resolve()
        root = checkout_root(cwd)
        candidate = Path(value.strip())
        if not candidate.is_absolute():
            candidate = cwd / candidate
        resolved = candidate.resolve()
        relative = resolved.relative_to(root)
        if relative == Path("."):
            return None
        from .history_sources import _sensitive
        if (_sensitive(resolved) or any(part.casefold() in {".git", ".ssh", ".aws", ".gnupg",
                                       ".gcloud", "secrets", "credentials"} for part in relative.parts)):
            return None
        path = relative.as_posix()
        result = {"checkout_key": checkout_key(root), "relative_path": path,
                  "path_key": os.path.normcase(path), "version": {"status": "unknown"}}
        if observe_version:
            try:
                stat = resolved.stat()
                result["version"] = {"status": "stat_observed", "size": stat.st_size,
                                     "mtime_ns": stat.st_mtime_ns, "content_hash": None}
            except OSError:
                result["version"] = {"status": "unavailable", "content_hash": None}
        return result
    except (OSError, ValueError, RuntimeError):
        return None


def _single_read_command(command):
    # 只支持可明确解释的单文件读取；任何管道、插值、脚本和多命令均不猜。
    if (not isinstance(command, str) or len(command) > 4000
            or any(mark in command for mark in (";", "|", "&", "<", ">", "`", "$", "\n", "\r"))):
        return None
    try:
        parts = [item.strip("\"'") for item in shlex.split(command, posix=False)]
    except ValueError:
        return None
    if len(parts) == 2 and parts[0].lower() in {"cat", "type"} and not parts[1].startswith("-"):
        return parts[1]
    if (len(parts) == 3 and parts[0].lower() == "get-content"
            and parts[1].lower() in {"-literalpath", "-path"}):
        return parts[2]
    return None


def file_links_from_payload(tool_name, payload, workspace, *, observe_version=True):
    """保守解析支持的结构化目标及 apply_patch 标头，返回最多 16 个关系。"""
    name = str(tool_name or "").lower()
    args = payload if isinstance(payload, dict) else {}
    shell = any(word in name for word in ("bash", "shell", "pwsh", "powershell", "exec_command", "run_command", "terminal"))
    shell_base = None
    if shell:
        cwd = args.get("cwd", args.get("workdir", args.get("Cwd")))
        if cwd is not None:
            if not isinstance(cwd, str) or not cwd.strip() or "\0" in cwd:
                return []
            try:
                origin = Path(workspace).resolve()
                shell_base = Path(cwd)
                if not shell_base.is_absolute():
                    shell_base = origin / shell_base
                shell_base = shell_base.resolve()
                shell_base.relative_to(checkout_root(origin))
            except (OSError, ValueError, RuntimeError):
                return []
    write = any(word in name for word in ("write", "edit", "patch", "replace", "create_file"))
    read = any(word in name for word in ("read", "view_file", "get_content", "cat"))
    relation = "write_target" if write else "read_target" if read else "observed_target"
    targets = []
    # workspace/cwd/directory 自身不是文件关系，也不递归搜索任意业务对象。
    for key in ("path", "file_path", "filepath", "file", "filename", "AbsolutePath", "TargetFile",
                "target_file", "FilePath"):
        if isinstance(args.get(key), str):
            targets.append((args[key], relation))
    if "apply_patch" in name:
        patch = payload if isinstance(payload, str) else next((args[key] for key in
            ("patch", "input", "text", "command") if isinstance(args.get(key), str)), "")
        for match in re.finditer(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$",
                                 patch[:200000], re.MULTILINE):
            targets.append((match.group(1) or match.group(2), "write_target"))
    if shell:
        command = args.get("command", args.get("cmd", args.get("CommandLine")))
        path = _single_read_command(command)
        if path:
            targets.append((path, "read_target"))
    output, seen = [], set()
    for value, target_relation in targets[:32]:
        if shell_base is not None and not Path(value).is_absolute():
            value = str(shell_base / value)
        item = normalize_file_target(value, workspace, observe_version=observe_version)
        if item and (item["path_key"], target_relation) not in seen:
            item["relation"] = target_relation
            output.append(item)
            seen.add((item["path_key"], target_relation))
        if len(output) == 16:
            break
    return output


def current_file_version(relative_path, workspace):
    item = normalize_file_target(relative_path, checkout_root(workspace))
    return item["version"] if item else {"status": "outside_workspace", "content_hash": None}


def knowledge_file_observation(runtime, project, item, workspace):
    """Bounded metadata snapshot for display and its exact delivery receipt."""
    from .runtime import digest
    if not workspace:
        return None
    refs = list(dict.fromkeys(ev["event_id"] for ev in item.get("evidence", [])
                             if isinstance(ev, dict) and ev.get("event_id")))[:20]
    if not refs:
        return None
    marks = ",".join("?" for _ in refs)
    rows = runtime.db.execute(
        f"SELECT relative_path,version FROM work_file_links WHERE project=? AND checkout_key=? "
        f"AND event_id IN ({marks}) ORDER BY event_id,relative_path LIMIT 16",
        (project, checkout_key(workspace), *refs)).fetchall()
    snapshots, changed = [], False
    for row in rows:
        try:
            recorded = json.loads(row["version"])
        except (ValueError, TypeError):
            continue
        if not isinstance(recorded, dict) or recorded.get("status") != "stat_observed":
            continue
        current = current_file_version(row["relative_path"], workspace)
        snapshots.append([row["relative_path"], recorded, current])
        if (current.get("status") != "stat_observed" or current.get("size") != recorded.get("size")
                or current.get("mtime_ns") != recorded.get("mtime_ns")):
            changed = True
    return ({"checkout": checkout_key(workspace), "version": digest(snapshots), "changed": changed}
            if snapshots else None)


def knowledge_file_state(runtime, project, item, workspace):
    """Compare recorded size/mtime metadata, without claiming content validation."""
    observation = knowledge_file_observation(runtime, project, item, workspace)
    return {"source_files_changed": True} if observation and observation["changed"] else {}
