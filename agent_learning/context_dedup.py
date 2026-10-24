"""Discardable delivery fingerprints, never reading, adoption or authority receipts."""
from __future__ import annotations

import json
import sqlite3
import time


POLICY_VERSION = 1
MAX_ENTRIES = 256
MAX_SOURCE_REFS = 20


def migrate(runtime):
    try:
        _migrate(runtime)
    except sqlite3.Error:
        # This cache is optional; normal source, notification and delivery
        # contracts remain authoritative if its local storage is unavailable.
        pass


def _migrate(runtime):
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS context_delivery_cache(
        project TEXT NOT NULL,conversation TEXT NOT NULL,agent TEXT NOT NULL,
        scope TEXT NOT NULL,policy INTEGER NOT NULL,channel TEXT NOT NULL,item_id TEXT NOT NULL,
        revision TEXT NOT NULL,source_version TEXT NOT NULL,material_key TEXT NOT NULL,
        text_key TEXT NOT NULL DEFAULT '',created REAL NOT NULL,
        PRIMARY KEY(project,scope,channel,item_id))""")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS context_delivery_material ON "
                       "context_delivery_cache(project,scope,material_key)")


def _text(value):
    return value.replace("\r\n", "\n") if isinstance(value, str) else value


def _refs(item):
    refs = set(item.get("source_event_refs", ()))
    refs.update(item.get("evidence_refs", ()))
    for source in item.get("sources", ()):
        refs.update(source.get("evidence_refs", ()))
        refs.update(event["event_id"] for event in source.get("evidence", ()) if event.get("event_id"))
    refs.update(event["event_id"] for event in item.get("evidence", ()) if event.get("event_id"))
    return sorted(refs)


class ContextDedup:
    """Only complete automatic cards in this exact input scope can suppress cards."""

    def __init__(self, runtime, project, conversation, agent_id, workspace, task_state,
                 context_epoch, delivery, environment):
        from .runtime import digest
        from .work_files import checkout_key
        self.r, self.project, self.conversation = runtime, project, conversation
        self.agent, self.workspace, self.delivery = agent_id or "", workspace, delivery
        state = task_state or {}
        self.scope = digest([POLICY_VERSION, conversation, self.agent,
            checkout_key(workspace) if workspace else None, runtime.host, runtime.host_instance,
            runtime.session_epoch(), runtime.host_epoch(), context_epoch,
            {key: state.get(key) for key in ("id", "revision", "assignment_id")}, environment])
        self.entries = []
        self.pending = []
        self.validity = {}
        # Ephemeral model inputs cannot borrow coverage from an earlier call.
        if delivery == "persistent":
            try:
                self.entries = [dict(row) for row in runtime.db.execute(
                    "SELECT channel,item_id,revision,source_version,material_key,text_key FROM "
                    "context_delivery_cache WHERE project=? AND scope=? AND policy=? "
                    "ORDER BY created DESC LIMIT ?", (project, self.scope, POLICY_VERSION, MAX_ENTRIES))]
            except sqlite3.Error:
                pass  # Derived cache failure must not block normal knowledge.

    def _sources(self, item):
        from .runtime import digest
        from .work_files import checkout_key, current_file_version
        refs = _refs(item)
        if not refs or len(refs) > MAX_SOURCE_REFS:
            return None
        snapshots = []
        for ref in refs:
            snapshot = self.r.work_source_snapshot(self.project, ref)
            if snapshot.get("state") in {"missing", "conflicted", "denied", "source_changed"}:
                return None
            snapshots.append(snapshot)
        files = []
        if self.workspace:
            marks = ",".join("?" for _ in refs)
            rows = self.r.db.execute("SELECT relative_path,relation,version FROM work_file_links "
                f"WHERE project=? AND checkout_key=? AND event_id IN ({marks}) "
                "ORDER BY event_id,relative_path,relation LIMIT 17",
                (self.project, checkout_key(self.workspace), *refs)).fetchall()
            if len(rows) > 16:
                return None
            files = [(row["relative_path"], row["relation"], row["version"],
                      current_file_version(row["relative_path"], self.workspace)) for row in rows]
        return digest([snapshots, files])

    def prepare(self, channel, item, card):
        try:
            return self._prepare(channel, item, card)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
            return None  # Unknown coverage is never a reason to suppress output.

    def _prepare(self, channel, item, card):
        from .runtime import digest
        if (item.get("source_changed") or item.get("file_state_changed")
                or item.get("source_files_changed") or item.get("candidate_reference_risk")
                or item.get("pending_counterexample_count")
                or any(file.get("version_changed") is True for file in item.get("files", ()))):
            return None
        if channel == "work":
            if item.get("state") not in {"reported", "model_reported", "succeeded"}:
                return None
            row = self.r.db.execute("SELECT body FROM work_memories WHERE project=? AND id=?",
                                    (self.project, item["memory_id"])).fetchone()
            if row is None:
                return None
            body = json.loads(row[0])
            full = _text(body.get("summary", ""))
            material = {key: body.get(key) for key in (
                "type", "title", "validation_level", "completion", "coverage", "assignment_id")}
            identity, revision, source_version = item["memory_id"], item["revision"], item["source_version"]
        else:
            if item.get("source_health") != "valid":
                return None
            body = item
            if channel == "candidate":
                row = self.r.db.execute("SELECT body FROM candidates WHERE project=? AND id=? AND status='candidate'",
                                        (self.project, item["candidate_id"])).fetchone()
                if row is None:
                    return None
                body = json.loads(row[0])
            full = _text(body.get("content", ""))
            material = {key: body.get(key) for key in (
                "title", "applicability", "limitations", "metadata", "category", "knowledge_kind",
                "validation_level", "knowledge_tier", "maturity", "current_tool_success", "problem_changed")}
            material["source_health"] = item.get("source_health")
            material["source_problems"] = [(source.get("problem_id"), source.get("problem_revision"))
                                           for source in item.get("sources", ())]
            identity = item["knowledge_id"] if channel == "formal" else item["candidate_id"]
            revision = item["revision"] if channel == "formal" else item["reference_version"]
            source_version = item.get("source_version", item.get("reference_version"))
        sources = self._sources(item)
        if sources is None:
            return None
        # Rendered excerpts and full material both matter. Equal previews cannot
        # collapse different bodies, conditions, negations or evidence grades.
        visible = {key: value for key, value in card.items() if key not in {
            "knowledge_id", "candidate_id", "memory_id", "revision", "reference_version", "matched_terms"}}
        comparison_revision = item.get("problem_revision") if channel == "candidate" else revision
        entry = {"channel": channel, "item_id": identity, "revision": str(revision),
            "source_version": str(source_version),
            "material_key": digest([channel, str(comparison_revision), material, full, sources, visible]), "text_key": ""}
        if channel == "formal" and (item.get("knowledge_kind") == "fact"
                and item.get("knowledge_tier", "source_reference") == "source_reference"
                and item.get("validation_level") == "reported"
                and _text(card.get("content_summary")) == full
                and all(card.get(key, {} if key == "metadata" else "") == item.get(key, {} if key == "metadata" else "")
                        for key in ("applicability", "limitations", "metadata"))):
            # A complete sourced fact plus its conditions can cover an identical
            # reported public statement; a title, prefix or verified solution cannot.
            entry["text_key"] = digest([full, sources, "reported"])
        if channel == "work" and (item.get("type") == "public_summary"
                and item.get("validation_level") == "reported"
                and item.get("completion") == "complete" and not item.get("coverage", {}).get("truncated")):
            entry["text_key"] = digest([full, sources, "reported"])
        return entry

    def _valid(self, entry):
        if entry.get("_current"):
            return True
        key = (entry["channel"], entry["item_id"], entry["revision"], entry["source_version"])
        if key in self.validity:
            return self.validity[key]
        from .runtime import ContractError
        valid = False
        try:
            if entry["channel"] == "formal":
                item = self.r.read(self.project, entry["item_id"])
                valid = (str(item["revision"]) == entry["revision"]
                    and str(item.get("source_version")) == entry["source_version"]
                    and item["source_health"] == "valid" and not item.get("pending_counterexample_count"))
            elif entry["channel"] == "candidate":
                from .candidate_references import assess, policy
                item = assess(self.r, self.project, entry["item_id"])
                valid = bool(policy(self.r, self.project)["enabled"] and item.get("eligible")
                             and item["reference_version"] == entry["revision"])
            else:
                row = self.r.db.execute("SELECT * FROM work_memories WHERE project=? AND id=?",
                                         (self.project, entry["item_id"])).fetchone()
                if row is not None:
                    item = self.r._work_item(self.project, row, workspace=self.workspace)
                    valid = (str(item["revision"]) == entry["revision"]
                             and str(item["source_version"]) == entry["source_version"])
        except (ContractError, sqlite3.Error, OSError, ValueError, TypeError, KeyError):
            pass
        self.validity[key] = valid
        return valid

    def repeated(self, entry):
        if entry is None:
            return False
        for old in self.entries:
            same = old["material_key"] == entry["material_key"]
            covered = (entry["channel"] == "work" and old["channel"] == "formal"
                       and bool(entry["text_key"]) and old["text_key"] == entry["text_key"])
            if (same or covered) and self._valid(old):
                return True
        return False

    def record(self, entry):
        if entry is None:
            return
        self.entries.insert(0, dict(entry, _current=True))
        self.entries = self.entries[:MAX_ENTRIES]
        if self.delivery != "persistent":
            return
        self.pending.append(entry)

    def flush(self):
        """Best-effort cache writes occur only after authoritative delivery commits."""
        if not self.pending or self.r.db.in_transaction:
            return
        previous_timeout = None
        try:
            previous_timeout = self.r.db.execute("PRAGMA busy_timeout").fetchone()[0]
            # Optional cache contention must not consume the host's Hook window.
            self.r.db.execute("PRAGMA busy_timeout=25")
            with self.r.transaction():
                for entry in self.pending:
                    self.r.db.execute("INSERT OR REPLACE INTO context_delivery_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (self.project, self.conversation, self.agent, self.scope, POLICY_VERSION,
                         entry["channel"], entry["item_id"], entry["revision"], entry["source_version"],
                         entry["material_key"], entry["text_key"], time.time()))
                self.r.db.execute("DELETE FROM context_delivery_cache WHERE project=? AND conversation=? AND agent=? "
                    "AND rowid NOT IN (SELECT rowid FROM context_delivery_cache WHERE project=? AND conversation=? "
                    "AND agent=? ORDER BY created DESC,rowid DESC LIMIT ?)",
                    (self.project, self.conversation, self.agent, self.project, self.conversation, self.agent, MAX_ENTRIES))
        except sqlite3.Error:
            pass
        finally:
            if previous_timeout is not None:
                try:
                    self.r.db.execute(f"PRAGMA busy_timeout={int(previous_timeout)}")
                except sqlite3.Error:
                    pass
