"""Bounded file-warning coalescing; source risks never enter this cache."""
from __future__ import annotations

import json
import math
import sqlite3
import time

DEFER_SECONDS = 30


def install(runtime):
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS work_notice_delivery(
        project TEXT NOT NULL,conversation TEXT NOT NULL,agent TEXT NOT NULL,
        scope TEXT NOT NULL,path_key TEXT NOT NULL,created REAL NOT NULL,
        PRIMARY KEY(project,scope,path_key))""")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS work_notice_owner ON "
                       "work_notice_delivery(project,conversation,agent,created)")


def mark_file_transition(item, previous_files):
    """Compare the current observation with files actually shown to this owner."""
    item["_changed_file_paths"] = []
    item.pop("_unseen_file_paths", None)
    current = item.get("file_state")
    if not isinstance(previous_files, dict) or not previous_files or not isinstance(current, dict):
        return item  # Missing observations cannot establish a restoration.
    changed = sorted(path for path, version in current.items()
                     if path in previous_files and previous_files[path] != version)
    item["_changed_file_paths"] = changed
    item["_unseen_file_paths"] = sorted(path for path in current if path not in previous_files)
    if changed:
        item["file_state_changed"] = True
    else:
        item.pop("file_state_changed", None)
    return item


def file_notice(item):
    return (item.get("file_state_changed") is True and not item.get("source_changed")
            and item.get("state") in {"unknown", "reported", "model_reported", "succeeded", "call_completed"}
            and item.get("completion", "complete") == "complete"
            and not item.get("candidate_reference_risk") and not item.get("published_knowledge_risk") and not item.get("pending_counterexample_count")
            and ("_changed_file_paths" not in item or bool(item["_changed_file_paths"]))
            and not item.get("_unseen_file_paths")
            and not any(file.get("version_changed") is False for file in item.get("files", ())
                        if isinstance(file, dict) and ("_changed_file_paths" not in item
                            or file.get("relative_path") in item["_changed_file_paths"]))
            and isinstance(item.get("file_state"), dict) and bool(item["file_state"]))


def coalesce(items):
    """Only identical current file observations share a warning; keep every ID."""
    result, groups = [], {}
    for item in items:
        if not file_notice(item):
            result.append(item)
            continue
        key = (tuple(sorted(item["file_state"].items())), item.get("state"), item.get("validation_level"))
        old = groups.get(key)
        if old is not None and len(old["_notice_members"]) < 4:
            old["_notice_members"].append(item)
        else:
            copy = dict(item, _notice_members=[item])
            groups[key] = copy
            result.append(copy)
    return result


class FileNoticeGate:
    def __init__(self, runtime, project, conversation, agent, scope, delivery):
        self.r, self.project, self.conversation, self.agent = runtime, project, conversation, agent or ""
        self.scope, self.delivery = scope, delivery
        self._index_scope_computed, self._index_scope_value = False, None

    def _index_scope(self):
        """File-index frequency is not proof of persistent model coverage."""
        if not self._index_scope_computed:
            from .runtime import digest
            self._index_scope_computed = True
            generation = None
            if self.r.host in {"antigravity", "dsh"}:
                table = "antigravity_trajectories" if self.r.host == "antigravity" else "dsh_sessions"
                row = self.r.db.execute(f"SELECT generation,ambiguous FROM {table} WHERE project=? AND conversation=?",
                                       (self.project, self.conversation)).fetchone()
                if row is None or row["ambiguous"] or type(row["generation"]) is not int or row["generation"] < 1:
                    return None
                generation = row["generation"]
            self._index_scope_value = digest([self.scope, "file-index-v1", generation])
        return self._index_scope_value

    def _index_key(self, item, card):
        from .runtime import digest
        if (not protocol_only_file_summary(item) or item.get("state") not in {"call_completed", "succeeded", "reported", "model_reported"}
                or item.get("completion", "complete") != "complete" or not card.get("files")
                or card.get("read_required") is not True or card.get("reference_role") == "source_update"
                or item.get("source_changed") or item.get("file_state_changed") or item.get("source_files_changed")
                or item.get("candidate_reference_risk") or item.get("published_knowledge_risk")
                or item.get("pending_counterexample_count") or item.get("file_projection_incomplete")
                or item.get("coverage", {}).get("truncated") or not item.get("source_event_refs")
                or not item.get("source_version") or not item.get("file_state")):
            return None
        files = item.get("files", [])
        if not files or any(file.get("current_version", {}).get("status") != "stat_observed" for file in files):
            return None
        return "index:" + digest([item["memory_id"], item["revision"], item["source_version"], item["file_state"], card])

    def index_repeated(self, item, card):
        """Suppress only the same protocol-only file index in a verified boundary.

        Formal/candidate material, substantive file results and source-change
        warnings never use this cache. Ephemeral inputs still need knowledge.
        """
        try:
            key = self._index_key(item, card)
            scope = self._index_scope() if key else None
            if scope is None:
                return False
            row = self.r.db.execute("SELECT created FROM work_notice_delivery WHERE project=? AND scope=? AND path_key=?",
                                   (self.project, scope, key)).fetchone()
            return bool(row and type(row[0]) in (int, float) and math.isfinite(row[0]) and row[0] <= time.time())
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            return False

    def record_index(self, item, card):
        """Called inside the delivery savepoint only after the whole card fits."""
        try:
            key = self._index_key(item, card)
            scope = self._index_scope() if key else None
            if scope is not None:
                self._record_keys([key], scope)
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            pass

    def annotate(self, item):
        """Use the trusted context owner, never an identity from a recall item."""
        previous = self.r.db.execute("SELECT source_version,file_state FROM work_exposures "
            "WHERE project=? AND conversation=? AND agent=? AND memory_id=?",
            (self.project, self.conversation, self.agent, item["memory_id"])).fetchone()
        if previous is not None:
            if previous["source_version"] != item["source_version"]:
                item["source_changed"] = True
        mark_file_transition(item, json.loads(previous["file_state"]) if previous and previous["file_state"] else {})
        return item

    def _keys(self, item):
        from .runtime import digest
        if not file_notice(item):
            return []
        # A stable associated file has not received its own warning. Merged
        # cards retain every member's actual transition, including disjoint ones.
        paths = set()
        for member in item.get("_notice_members", [item]):
            if file_notice(member):
                paths.update(path for path in member.get("_changed_file_paths", member["file_state"])
                             if path in member["file_state"])
        return [digest(path) for path in sorted(paths)]

    def deferred(self, item):
        keys = self._keys(item)
        if not keys or self.delivery != "persistent":
            return False
        now = time.time()
        try:
            for key in keys:
                row = self.r.db.execute("SELECT created FROM work_notice_delivery WHERE project=? AND scope=? AND path_key=?",
                                        (self.project, self.scope, key)).fetchone()
                if (row is None or type(row[0]) not in (int, float) or not math.isfinite(row[0])
                        or not 0 <= now - row[0] < DEFER_SECONDS):
                    return False
            return True
        except sqlite3.Error:
            return False

    def record(self, item):
        if self.delivery != "persistent":
            return
        keys = self._keys(item)
        if not keys:
            return
        try:
            self._record_keys(keys, self.scope)
        except sqlite3.Error:
            pass  # Optional cache failure keeps normal warnings available.

    def _record_keys(self, keys, scope):
        for key in keys:
            self.r.db.execute("INSERT OR REPLACE INTO work_notice_delivery VALUES(?,?,?,?,?,?)",
                             (self.project, self.conversation, self.agent, scope, key, time.time()))
        self.r.db.execute("DELETE FROM work_notice_delivery WHERE project=? AND conversation=? AND agent=? "
                          "AND rowid NOT IN (SELECT rowid FROM work_notice_delivery WHERE project=? "
                          "AND conversation=? AND agent=? ORDER BY created DESC,rowid DESC LIMIT 256)",
                          (self.project, self.conversation, self.agent, self.project, self.conversation, self.agent))


def protocol_only_file_summary(item):
    """Trim an empty protocol envelope from an automatic index, not its source."""
    if item.get("type") != "file_change":
        return False
    try:
        value = json.loads(item.get("summary", ""))
        return (isinstance(value, dict) and set(value) == {"error", "isError"}
                and value["error"] in ("", None) and value["isError"] is False)
    except (ValueError, TypeError):
        return False
