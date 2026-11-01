"""Bounded metadata for newly assembled context, never reading or token receipts."""
from __future__ import annotations

import json
import time
import uuid

MAX_OUTPUTS = 256


def install(runtime):
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS context_output_observations(
        id TEXT PRIMARY KEY,project TEXT NOT NULL,conversation TEXT NOT NULL,agent TEXT NOT NULL,
        host_scope TEXT NOT NULL,session_epoch INTEGER NOT NULL,scope TEXT NOT NULL,
        task_id TEXT NOT NULL,task_revision INTEGER NOT NULL,assignment_id TEXT NOT NULL,
        context_epoch INTEGER NOT NULL,delivery TEXT NOT NULL,output_chars INTEGER NOT NULL,
        output_digest TEXT NOT NULL,cards TEXT NOT NULL,created REAL NOT NULL)""")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS context_output_owner ON "
                       "context_output_observations(project,conversation,agent,created)")


def describe(output):
    """Parse only complete rendered JSON cards, without retaining their text."""
    from .runtime import digest
    if not isinstance(output, str) or not output:
        return None
    cards = []
    for line in output.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            value = json.loads(line[start:])
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        if value.get("memory_id") and line.startswith("[工作历史] "):
            channel, identity = "work", value["memory_id"]
            kind = value.get("reference_role")
            if kind == "source_update":
                kind = ("source_update" if value.get("source_changed") or value.get("published_knowledge_risk")
                        else "file_update")
            elif kind != "task_handoff":
                kind = "work_summary" if value.get("summary") else "work_index"
        elif value.get("knowledge_id") and start == 0:
            channel, identity, kind = "formal", value["knowledge_id"], "formal"
        elif value.get("candidate_id") and line.startswith("[待定参考"):
            channel, identity, kind = "candidate", value["candidate_id"], "candidate"
        else:
            continue
        card = {"channel": channel, "kind": kind, "item_id": identity,
                "revision": value.get("revision", value.get("reference_version")),
                "chars": len(line), "digest": digest(line),
                "member_count": 1 + len(value.get("related_memories", []))}
        for key in ("state", "validation_level", "read_required", "source_coverage", "source_health",
                    "knowledge_tier", "maturity", "current_tool_success"):
            if key in value:
                card[key] = value[key]
        cards.append(card)
    return {"output_chars": len(output), "output_digest": digest(output), "cards": cards}


def record(owner, observation):
    """Use the caller's optional short transaction; never participate in gating."""
    from .runtime import encode
    owner.r.db.execute("INSERT INTO context_output_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), owner.project, owner.conversation, owner.agent, owner.observed_host_scope,
         owner.observed_session_epoch, owner.scope, owner.observed_task_id, owner.observed_task_revision,
         owner.observed_assignment_id, owner.observed_context_epoch, owner.delivery,
         observation["output_chars"], observation["output_digest"], encode(observation["cards"]), time.time()))
    owner.r.db.execute("DELETE FROM context_output_observations WHERE project=? AND conversation=? AND agent=? "
        "AND rowid NOT IN (SELECT rowid FROM context_output_observations WHERE project=? AND conversation=? "
        "AND agent=? ORDER BY created DESC,rowid DESC LIMIT ?)",
        (owner.project, owner.conversation, owner.agent, owner.project, owner.conversation, owner.agent, MAX_OUTPUTS))
