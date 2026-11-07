"""Optional derived, bounded file-filter observations. No command or file text."""
from __future__ import annotations

import json
import sqlite3

REASONS = frozenset({"outside_workspace", "unsupported_arguments", "no_structured_target",
                     "sensitive_target", "invalid_target", "symlink_target", "target_limit",
                     "duplicate_target", "unsupported_tool"})


def install(runtime):
    # An additive derived table leaves schema-25 positional inserts untouched.
    runtime.db.execute("CREATE TABLE IF NOT EXISTS work_file_observations("
        "project TEXT NOT NULL,host TEXT NOT NULL,conversation TEXT NOT NULL,event_id TEXT NOT NULL,"
        "created REAL NOT NULL,target_count INTEGER NOT NULL,accepted_count INTEGER NOT NULL,"
        "filtered_count INTEGER NOT NULL,reasons TEXT NOT NULL,"
        "PRIMARY KEY(project,host,conversation,event_id))")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS work_file_observation_scope "
        "ON work_file_observations(project,host,conversation,created DESC)")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS work_file_observation_recent "
        "ON work_file_observations(project,created DESC)")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS work_file_observation_host "
        "ON work_file_observations(project,host,created DESC)")


def observe_event(runtime, project, event, workspace, *, raw=None):
    """Freeze the first callback's metadata, including when work memory is off."""
    from .work_memory import _tool, _learning_tool
    from .work_files import file_links_from_payload
    from .workspace_links import file_workspace_roots
    event = dict(event)
    if event.get("type") not in {"PreToolUse", "PostToolUse"} or not workspace:
        return None
    host = runtime.host
    identity = (project, host, event["conversation"], event["id"])
    if runtime.db.execute("SELECT 1 FROM work_file_observations WHERE project=? AND host=? "
                          "AND conversation=? AND event_id=?", identity).fetchone():
        return None
    payload = raw if isinstance(raw, dict) else json.loads(event["payload"])
    name, args = _tool(payload)
    if _learning_tool(name):
        return None
    details = {}
    file_links_from_payload(name, args, workspace, roots=file_workspace_roots(runtime, project, workspace),
                            observe_version=False, diagnostics=details)
    runtime.db.execute("INSERT OR IGNORE INTO work_file_observations VALUES(?,?,?,?,?,?,?,?,?)",
        (*identity, event["created"], details["target_count"], details["accepted_count"],
         details["filtered_count"], json.dumps(details["reasons"], sort_keys=True)))
    return details


def summary(runtime, project, *, host=None, conversation=None, limit=40):
    """Read a recent indexed window; never scan authority events or file links."""
    limit = max(1, min(100, int(limit)))
    result = {"available": True, "events": 0, "target_count": 0, "accepted_count": 0, "filtered_count": 0,
              "reasons": {}, "truncated": False}
    clauses, params = ["project=?"], [project]
    if host is not None:
        clauses.append("host=?")
        params.append(host)
    if conversation is not None:
        clauses.append("conversation=?")
        params.append(conversation)
    try:
        rows = runtime.db.execute("SELECT target_count,accepted_count,filtered_count,reasons "
            "FROM work_file_observations WHERE " + " AND ".join(clauses)
            + " ORDER BY created DESC LIMIT ?", (*params, limit + 1)).fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error).lower():
            raise
        return dict(result, available=False)
    result["truncated"] = len(rows) > limit
    for row in rows[:limit]:
        result["events"] += 1
        for field in ("target_count", "accepted_count", "filtered_count"):
            result[field] += max(0, min(200000, int(row[field])))
        try:
            reasons = json.loads(row["reasons"])
        except (ValueError, TypeError):
            continue
        if isinstance(reasons, dict):
            for reason, count in reasons.items():
                if reason in REASONS and type(count) is int and count > 0:
                    result["reasons"][reason] = result["reasons"].get(reason, 0) + min(count, 200000)
    return result
