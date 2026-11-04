"""Bounded public-message grouping; source events and review authority stay intact."""
from __future__ import annotations

import json


_FIELDS = ("id", "project", "conversation", "task_id", "task_revision", "task_write_version",
           "execution_epoch", "agent", "assignment_id", "type", "outcome", "created")
_MAX_PAYLOAD = 6000
_MAX_MESSAGES = 16


def _event(row):
    event = {key: row[key] for key in _FIELDS}
    try:
        payload = json.loads(row["public_payload"] or "null")
    except (TypeError, ValueError, RecursionError):
        payload = None
    event["payload"] = payload if isinstance(payload, dict) else {}
    event["health"] = ("conflicted" if row["operation_conflict"] else
                       "denied" if row["operation_decision"] == "denied" else "valid")
    return event


def _groups(events):
    groups = {}
    for event in events:
        body = event.get("payload")
        if (event.get("type") != "PublicMessageSegment" or not isinstance(body, dict)
                or body.get("memory_type") != "public_summary"):
            continue
        parent = body.get("public_message_id")
        # Invalid legacy identities still produce an explicit partial marker.
        key = parent if isinstance(parent, str) and 0 < len(parent) <= 200 else None
        groups.setdefault(key, []).append(event)
    return groups


def parent_events(runtime, project, events, *, max_messages=_MAX_MESSAGES):
    """Read only exact parent identities, once per bounded case/selection window.

    Parents are validation context, never newly added case evidence. An absent,
    oversized or conflicting parent cannot prove a complete public message.
    """
    parents = [key for key in _groups(events) if key is not None][:max_messages]
    if not parents:
        return []
    fields = ",".join("e." + key for key in _FIELDS)
    rows = []
    for offset in range(0, len(parents), 64):
        page = parents[offset:offset + 64]
        rows.extend(runtime.db.execute("SELECT " + fields + ",substr(e.payload,1,?) public_payload,"
            "o.conflict operation_conflict,o.decision operation_decision FROM events e "
            "LEFT JOIN operations o ON o.project=e.project AND o.id=e.operation "
            "WHERE e.project=? AND e.type='Stop' AND e.id IN (" + ",".join("?" for _ in page) + ")",
            (_MAX_PAYLOAD + 1, project, *page)).fetchall())
    return [_event(row) for row in rows if len(row["public_payload"]) <= _MAX_PAYLOAD]


def chunk_sources(runtime, project, task, refs):
    """Keep a proven complete <=4000-char/6-event message in one case.

    Other events retain the old bounded chunking behavior. Grouping never joins
    different source messages, repairs missing parts or rewrites source data.
    """
    from .phase_local_triage import _comparison_delivery
    events = [_event(row) for row in refs if row["type"] == "PublicMessageSegment"]
    sizes = {row["id"]: row["chars"] for row in refs}
    possible = {identity: group for identity, group in _groups(events).items()
                if identity is not None and len(group) <= 6
                and sum(sizes[event["id"]] for event in group) <= 4000}
    parents = parent_events(runtime, project, [event for group in possible.values() for event in group],
                            max_messages=2000)
    parents_by_id = {event["id"]: event for event in parents}
    problem = {"project": project, "conversation": task["conversation"], "task_id": task["id"],
               "task_revision": task["revision"]}
    atomic = {}
    for identity, group in possible.items():
        parent = [parents_by_id[identity]] if identity in parents_by_id else []
        if not parent:
            continue
        _, reason = _comparison_delivery(problem, [*group, *parent], group[0]["id"], max_chars=4000)
        if reason == "complete_public_segments":
            ids = [event["id"] for event in group]
            atomic.update((event_id, ids) for event_id in ids)
    chunks, chunk, chars, emitted = [], [], 0, set()
    for ref in refs:
        if ref["id"] in emitted:
            continue
        unit = atomic.get(ref["id"], [ref["id"]])
        size = sum(sizes[event_id] for event_id in unit)
        if chunk and (len(chunk) + len(unit) > 6 or chars + size > 4000):
            chunks.append(chunk)
            chunk, chars = [], 0
        chunk.extend(unit)
        emitted.update(unit)
        chars += size
    if chunk:
        chunks.append(chunk)
    return chunks


def coverage(problem, events, parents=()):
    """Describe whole-message semantics separately from case-reference coverage.

    Long or partial messages can still be reviewed as visible partial evidence;
    they never become a complete statement merely because each ref is present.
    """
    from .phase_local_triage import _comparison_delivery, _STAGE_SUMMARY
    if not _STAGE_SUMMARY.fullmatch(problem.get("summary", "")):
        return None
    groups = _groups(events)
    if not groups:
        return None
    result = []
    parents_by_id = {event["id"]: event for event in parents}
    for identity, group in list(groups.items())[:_MAX_MESSAGES]:
        parent = [parents_by_id[identity]] if identity in parents_by_id else []
        if identity is None or not parent:
            reason = "parent_unavailable"
        else:
            _, reason = _comparison_delivery(problem, [*group, *parent], group[0]["id"], max_chars=4000)
        complete = reason == "complete_public_segments"
        expected = group[0]["payload"].get("segments")
        result.append({"public_message_id": identity, "visible_segments": len(group),
            "total_segments": expected if type(expected) is int and 1 <= expected <= 64 else None,
            "complete": complete, "reason": reason})
    complete = len(groups) <= _MAX_MESSAGES and all(item["complete"] for item in result)
    return {"scope": "visible_public_messages", "complete": complete, "read_required": not complete,
            "messages": result, "additional_messages": max(0, len(groups) - _MAX_MESSAGES)}
