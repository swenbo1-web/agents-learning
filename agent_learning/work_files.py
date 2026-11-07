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


def _root_records(workspace, roots=None):
    if roots is not None:
        return roots
    root = checkout_root(workspace)
    return [{"path": str(root), "checkout_key": checkout_key(root), "primary": True}]


def _linked_path(path, root):
    """Reject links and Windows reparse points before following the target."""
    current = path
    while current != root:
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            return True
        try:
            if getattr(current.lstat(), "st_file_attributes", 0) & 0x400:
                return True
        except FileNotFoundError:
            pass
        if current == current.parent:
            return True
        current = current.parent
    return False


def _normalize_file_target(value, workspace, *, observe_version=True, roots=None):
    """Return a relation and controlled rejection reason; never read file content."""
    if (not isinstance(value, str) or not value.strip() or len(value) > 2000
            or any(ord(char) < 32 for char in value) or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value)):
        return None, "invalid_target"
    try:
        cwd = Path(workspace).resolve()
        candidate = Path(value.strip())
        if not candidate.is_absolute():
            candidate = cwd / candidate
        # Lexical containment first: a link from one authorized root into another
        # still cannot cross their checkout identities.
        candidate = Path(os.path.abspath(candidate))
        if os.name == "nt" and ":" in str(candidate)[len(candidate.drive):]:
            return None, "invalid_target"
        selected = None
        for record in _root_records(workspace, roots):
            root = Path(record["path"])
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            selected = record
            break
        if selected is None:
            return None, "outside_workspace"
        if relative == Path("."):
            return None, "invalid_target"
        from .history_sources import _sensitive
        if (_sensitive(candidate) or any(part.casefold() in {".git", ".ssh", ".aws", ".gnupg",
                                       ".gcloud", "secrets", "credentials"} for part in relative.parts)):
            return None, "sensitive_target"
        if _linked_path(candidate, root):
            return None, "symlink_target"
        resolved = candidate.resolve()
        relative = resolved.relative_to(root)
        if relative == Path("."):
            return None, "invalid_target"
        path = relative.as_posix()
        result = {"checkout_key": selected["checkout_key"], "relative_path": path,
                  "path_key": os.path.normcase(path), "version": {"status": "unknown"}}
        if observe_version:
            try:
                stat = resolved.stat()
                result["version"] = {"status": "stat_observed", "size": stat.st_size,
                                     "mtime_ns": stat.st_mtime_ns, "content_hash": None}
            except OSError:
                result["version"] = {"status": "unavailable", "content_hash": None}
        return result, None
    except (OSError, ValueError, RuntimeError):
        return None, "invalid_target"


def normalize_file_target(value, workspace, *, observe_version=True, roots=None):
    """Only the primary checkout and explicitly approved roots are eligible."""
    return _normalize_file_target(value, workspace, observe_version=observe_version, roots=roots)[0]


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


def file_links_from_payload(tool_name, payload, workspace, *, observe_version=True, roots=None, diagnostics=None):
    """保守解析支持的结构化目标及 apply_patch 标头，返回最多 16 个关系。"""
    name = str(tool_name or "").lower()
    args = payload if isinstance(payload, dict) else {}
    reasons = {}
    def reject(reason, count=1):
        reasons[reason] = reasons.get(reason, 0) + count
    def finish(output, total):
        if diagnostics is not None:
            diagnostics.update(target_count=total, accepted_count=len(output),
                               filtered_count=max(0, total - len(output)), reasons=reasons)
        return output
    roots = _root_records(workspace, roots)
    shell = any(word in name for word in ("bash", "shell", "pwsh", "powershell", "exec_command", "run_command", "terminal"))
    shell_base = None
    if shell:
        cwd = args.get("cwd", args.get("workdir", args.get("Cwd")))
        if cwd is not None:
            if not isinstance(cwd, str) or not cwd.strip() or "\0" in cwd:
                reject("unsupported_arguments")
                return finish([], 0)
            try:
                origin = Path(workspace).resolve()
                shell_base = Path(cwd)
                if not shell_base.is_absolute():
                    shell_base = origin / shell_base
                shell_base = Path(os.path.abspath(shell_base))
                base_root = next((Path(record["path"]) for record in roots
                                  if shell_base == Path(record["path"]) or Path(record["path"]) in shell_base.parents), None)
                if base_root is None:
                    reject("outside_workspace")
                    return finish([], 0)
                if _linked_path(shell_base, base_root):
                    reject("symlink_target")
                    return finish([], 0)
                shell_base = shell_base.resolve()
                if not any(shell_base == Path(record["path"]) or Path(record["path"]) in shell_base.parents
                           for record in roots):
                    reject("outside_workspace")
                    return finish([], 0)
            except (OSError, ValueError, RuntimeError):
                reject("unsupported_arguments")
                return finish([], 0)
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
        elif command is not None:
            reject("unsupported_arguments")
    if not targets:
        if not reasons:
            reject("no_structured_target")
        return finish([], 0)
    output, seen = [], set()
    for value, target_relation in targets[:256]:
        if shell_base is not None and not Path(value).is_absolute():
            value = str(shell_base / value)
        # Keep classifying every bounded target after the relation quota fills,
        # so a 21-header patch outside the workspace reports 21 rejections.
        item, reason = _normalize_file_target(value, workspace, observe_version=observe_version and len(output) < 16,
                                               roots=roots)
        identity = (item["checkout_key"], item["path_key"], target_relation) if item else None
        if item and identity not in seen and len(output) < 16:
            item["relation"] = target_relation
            output.append(item)
            seen.add(identity)
        else:
            reject(reason or ("duplicate_target" if identity in seen else "target_limit"))
    if len(targets) > 256:
        reject("target_limit", len(targets) - 256)
    return finish(output, len(targets))


def current_file_version(relative_path, workspace, *, file_checkout=None, roots=None):
    records = _root_records(workspace, roots)
    key = file_checkout or checkout_key(workspace)
    record = next((record for record in records if record["checkout_key"] == key), None)
    if record is None:
        return {"status": "outside_workspace", "content_hash": None}
    item = normalize_file_target(str(Path(record["path"]) / relative_path), record["path"], roots=[record])
    # A target rejected inside a known root remains a local availability risk.
    # It must not be confused with a checkout we were never allowed to inspect.
    return item["version"] if item else {"status": "unavailable", "content_hash": None}


def compare_file_versions(recorded, current):
    """Separate observed metadata drift, unknown scope and local unavailability."""
    recorded = recorded if isinstance(recorded, dict) else {}
    current = current if isinstance(current, dict) else {}
    def observed(value):
        return value.get("status") == "stat_observed" and all(
            type(value.get(key)) is int and value[key] >= 0 for key in ("size", "mtime_ns"))
    verified = observed(recorded) and observed(current)
    changed = verified and any(recorded[key] != current[key] for key in ("size", "mtime_ns"))
    return {"changed": bool(changed), "verified": verified, "unverified": not verified,
            "blocked": current.get("status") == "unavailable"}


def file_observation_state(observation):
    """Only additive, model-visible qualifiers; neither flag verifies content."""
    return {**({"source_files_changed": True} if observation and observation.get("changed") else {}),
            **({"source_files_unverified": True} if observation and observation.get("unverified") else {})}


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
    from .workspace_links import file_workspace_roots
    roots = file_workspace_roots(runtime, project, workspace)
    rows = runtime.db.execute(
        f"SELECT checkout_key,relative_path,version FROM work_file_links WHERE project=? "
        f"AND event_id IN ({marks}) ORDER BY event_id,relative_path LIMIT 16",
        (project, *refs)).fetchall()
    snapshots, files, changed, unverified, blocked = [], [], False, False, False
    for row in rows:
        try:
            recorded = json.loads(row["version"])
        except (ValueError, TypeError):
            continue
        if not isinstance(recorded, dict) or recorded.get("status") != "stat_observed":
            continue
        current = current_file_version(row["relative_path"], workspace, file_checkout=row["checkout_key"], roots=roots)
        snapshots.append([row["checkout_key"], row["relative_path"], recorded, current])
        comparison = compare_file_versions(recorded, current)
        changed |= comparison["changed"]
        unverified |= comparison["unverified"]
        blocked |= comparison["blocked"]
        files.append({"checkout": row["checkout_key"], "path": row["relative_path"],
                      "verified": comparison["verified"],
                      "scope": current.get("status") not in {"outside_workspace", "outside_scope", "unknown"}})
    return ({"checkout": checkout_key(workspace), "version": digest(snapshots), "changed": changed,
             "unverified": unverified, "blocked": blocked, "files": files}
            if snapshots else None)


def knowledge_file_state(runtime, project, item, workspace):
    """Compare recorded size/mtime metadata, without claiming content validation."""
    observation = knowledge_file_observation(runtime, project, item, workspace)
    return file_observation_state(observation)
