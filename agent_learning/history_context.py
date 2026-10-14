"""Bounded cross-document context from one explicitly active history import.

Only frozen SQLite records are read. Related excerpts remain separate from the
primary case and become source links only when the reviewer actually cites
them. Merely offering a source never marks that source as reviewed.
"""
from __future__ import annotations

import contextlib
from copy import deepcopy
import json
import re
import uuid


MAX_SCAN = 128
MAX_SOURCES = 2
MAX_EVENTS = 4
MAX_RELATED_CHARS = 5000
_ENGLISH = re.compile(r"[a-z][a-z0-9_]{1,63}", re.I)
_CHINESE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_WRAPPER = re.compile(r"^Historical source .*? \[[a-f0-9]{12}\] unit [0-9]+:\s*", re.S)
_STOP = {
    "historical", "history", "source", "unit", "project", "document", "documents",
    "documentation", "file", "files", "code", "notes", "note", "summary", "readme",
    "the", "this", "that", "these", "those", "and", "with", "from", "for", "into",
    "are", "was", "were", "has", "have", "had", "can", "will", "should", "must",
    "use", "used", "uses", "using", "only", "not", "but", "its", "our", "their",
    "return", "const", "void", "class", "public", "private", "function", "true", "false",
    "历史", "来源", "资料", "项目", "文档", "说明", "记录", "摘要", "代码",
    "进行", "使用", "可以", "需要", "这个", "当前", "相关", "我们", "一个",
}
_LIMITATIONS = (
    "Related archived excerpts only; not an independent review, observed tool "
    "success, current implementation verification, or execution authorization."
)


def _tokens(summary):
    """Technical words and Chinese bigrams, with import scaffolding removed."""
    text = _WRAPPER.sub("", summary, count=1).lower()
    text = re.sub(r"\bc\+\+", "cplusplus", text)
    text = re.sub(r"\bc#", "csharp", text)
    words = {word for word in _ENGLISH.findall(text)
             if word not in _STOP and not re.fullmatch(r"[a-f0-9]{12,}", word)}
    for run in _CHINESE.findall(text):
        words.update(run[index:index + 2] for index in range(len(run) - 1)
                     if run[index:index + 2] not in _STOP)
    return words


def _weight(word):
    return 3 if any(character.isdigit() for character in word) or "_" in word else (
        2 if _CHINESE.fullmatch(word) else 1)


@contextlib.contextmanager
def _transaction(runtime, *, write=False):
    owned = not runtime.db.in_transaction
    if owned:
        runtime.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
        if owned:
            runtime.db.execute("COMMIT")
    except BaseException:
        if owned and runtime.db.in_transaction:
            runtime.db.execute("ROLLBACK")
        raise


def _primary(runtime, project, packet, job_id):
    from .runtime import ContractError

    if not isinstance(job_id, str) or not job_id or not isinstance(packet, dict):
        raise ContractError("history context requires an explicit active job and packet")
    problem = packet.get("problem")
    if not isinstance(problem, dict) or not isinstance(problem.get("id"), str):
        raise ContractError("history context requires a primary problem")
    runtime.history_guard(project, problem["id"], job_id, require_review=True)
    row = runtime.db.execute(
        "SELECT p.*,u.id AS unit_id,u.material_id,u.body AS unit_body,m.source_key,m.content_hash "
        "FROM history_case_links l JOIN problems p ON p.project=l.project AND p.id=l.problem_id "
        "JOIN history_units u ON u.id=l.unit_id AND u.project=l.project AND u.problem_id=p.id "
        "JOIN history_job_sources s ON s.job_id=l.job_id AND s.material_id=u.material_id "
        "JOIN history_materials m ON m.id=u.material_id AND m.project=l.project "
        "WHERE l.project=? AND l.job_id=? AND p.id=? AND s.materialized=1",
        (project, job_id, problem["id"])).fetchone()
    if (not row or row["revision"] != problem.get("revision") or row["task_id"] is not None
            or row["task_revision"] is not None or packet.get("task") is not None):
        raise ContractError("primary history context snapshot changed or has a task binding")
    return dict(row)


def _source_rows(runtime, project, primary, job_id):
    return runtime.db.execute(
        "SELECT p.id,p.revision,p.summary,p.task_id,p.task_revision,u.id AS unit_id,"
        "u.material_id,u.unit_index,u.body AS unit_body,m.source_key,m.content_hash,s.position "
        "FROM history_case_links l JOIN problems p ON p.project=l.project AND p.id=l.problem_id "
        "JOIN history_units u ON u.id=l.unit_id AND u.project=l.project AND u.problem_id=p.id "
        "JOIN history_job_sources s ON s.job_id=l.job_id AND s.material_id=u.material_id "
        "JOIN history_materials m ON m.id=u.material_id AND m.project=l.project "
        "WHERE l.project=? AND l.job_id=? AND p.id<>? AND u.material_id<>? AND s.materialized=1 "
        "AND p.task_id IS NULL AND p.task_revision IS NULL "
        "ORDER BY s.position,u.unit_index,p.id LIMIT ?",
        (project, job_id, primary["id"], primary["material_id"], MAX_SCAN)).fetchall()


def _archive_event(runtime, project, source, row):
    """Require a real event of this frozen unit, not an arbitrary linked event."""
    from .runtime import ContractError, digest

    try:
        parts = json.loads(source["unit_body"])["parts"]
        if isinstance(parts, bool) or not isinstance(parts, int) or not 1 <= parts <= 8:
            raise ValueError
        identities = {digest([project, ["history", source["unit_id"], part]]) for part in range(parts)}
        payload = json.loads(row["payload"])
        recorded = payload["source"]
        valid = (
            row["id"] in identities and row["project"] == project
            and row["conversation"] == "history:" + source["material_id"]
            and row["type"] == "HistoryImport" and row["outcome"] == "reported"
            and all(row[name] is None for name in ("task_id", "task_revision", "agent", "operation", "assignment_id"))
            and payload.get("historical_statement") is True
            and recorded["source_key"] == source["source_key"]
            and recorded["content_hash"] == source["content_hash"]
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ContractError("history context event is not from its frozen source unit")
    event_row = dict(row)
    event_row.update(linked_id=row["id"], operation_conflict=None, operation_decision=None)
    return runtime._review_event(event_row)


def _events(runtime, project, source):
    rows = runtime.db.execute(
        "SELECT e.*,p.role FROM problem_events p JOIN events e ON e.id=p.event "
        "WHERE p.problem=? AND p.role<>'history_related' ORDER BY e.created,e.id LIMIT ?",
        (source["id"], MAX_EVENTS)).fetchall()
    total = runtime.db.execute(
        "SELECT COUNT(*) FROM problem_events WHERE problem=? AND role<>'history_related'",
        (source["id"],)).fetchone()[0]
    return [_archive_event(runtime, project, source, row) for row in rows], total


def _related_item(source, events, total, matched, score):
    omitted = total - len(events)
    return {"problem_id": source["id"], "revision": source["revision"],
            "unit_id": source["unit_id"], "material_id": source["material_id"],
            "source_key": source["source_key"], "content_hash": source["content_hash"],
            "matched_terms": matched[:12], "score": round(score, 6),
            "event_count": total, "events": events,
            "limitations": _LIMITATIONS + (
                f" {omitted} source events are outside this bounded context." if omitted else "")}


def enrich_history_packet(runtime, project, packet, job_id, *, max_chars=None):
    """Add at most two sources/four whole events without trimming the packet."""
    from .review_packet import review_instruction
    from .runtime import ContractError, encode

    limit = runtime.config["admin"]["max_input_chars"] if max_chars is None else max_chars
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1000 <= limit <= 200000:
        raise ContractError("history context budget must be an integer from 1000 to 200000")
    if not isinstance(packet, dict):
        raise ContractError("history context requires a packet object")
    result = deepcopy(packet)
    result["purpose"] = "history_review"
    result.pop("related_history", None)
    with _transaction(runtime):
        primary = _primary(runtime, project, result, job_id)
        query = _tokens(primary["summary"])
        if not query or len(review_instruction(result)) >= limit:
            return result
        denominator = sum(_weight(word) for word in query)
        ranked = []
        for row in _source_rows(runtime, project, primary, job_id):
            shared = query & _tokens(row["summary"])
            if shared:
                score = sum(_weight(word) for word in shared) / denominator
                ranked.append((score, len(shared), dict(row), sorted(shared)))
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]["position"],
                                      item[2]["unit_index"], item[2]["id"]))
        selected, available, materials = [], {}, set()

        def fits(items):
            return (len(encode(items)) <= MAX_RELATED_CHARS
                    and len(review_instruction(dict(result, related_history=items))) <= limit)

        # First offer one full event per material; use any remaining space for
        # further events only after giving another relevant document a chance.
        for score, _, source, matched in ranked:
            if source["material_id"] in materials:
                continue
            try:
                events, total = _events(runtime, project, source)
            except ContractError:
                continue
            if not events:
                continue
            item = _related_item(source, events[:1], total, matched, score)
            if not fits(selected + [item]):
                continue
            selected.append(item)
            materials.add(source["material_id"])
            available[source["id"]] = (source, events, total, matched, score)
            if len(selected) >= MAX_SOURCES:
                break
        for index, item in enumerate(list(selected)):
            source, events, total, matched, score = available[item["problem_id"]]
            for event in events[1:]:
                if sum(len(value["events"]) for value in selected) >= MAX_EVENTS:
                    break
                proposal = _related_item(source, selected[index]["events"] + [event], total, matched, score)
                expanded = selected[:index] + [proposal] + selected[index + 1:]
                if fits(expanded):
                    selected = expanded
                else:
                    break
        if selected:
            result["related_history"] = selected
    return result


def _source_snapshot(runtime, project, job_id, item):
    from .runtime import ContractError

    row = runtime.db.execute(
        "SELECT p.id,p.revision,p.task_id,p.task_revision,u.id AS unit_id,u.material_id,u.body AS unit_body,"
        "m.source_key,m.content_hash FROM history_case_links l "
        "JOIN problems p ON p.project=l.project AND p.id=l.problem_id "
        "JOIN history_units u ON u.id=l.unit_id AND u.project=l.project AND u.problem_id=p.id "
        "JOIN history_job_sources s ON s.job_id=l.job_id AND s.material_id=u.material_id "
        "JOIN history_materials m ON m.id=u.material_id AND m.project=l.project "
        "WHERE l.project=? AND l.job_id=? AND p.id=? AND s.materialized=1",
        (project, job_id, item.get("problem_id"))).fetchone()
    if (not row or row["task_id"] is not None or row["task_revision"] is not None
            or any(row[key] != item.get(key) for key in
                   ("revision", "unit_id", "material_id", "source_key", "content_hash"))):
        raise ContractError("related history source snapshot changed or is outside this job")
    return dict(row)


def link_history_evidence(runtime, project, packet, refs, job_id):
    """Atomically attach only cited, snapshot-matched related event references.

    This deliberately does not use _attach_case_evidence: these are separately
    identified archives, and attachment must not manufacture a new primary
    review revision or mark another source as reviewed.
    """
    from .runtime import ContractError, encode

    if (not isinstance(refs, list) or not refs or len(refs) > 512
            or not all(isinstance(ref, str) and ref for ref in refs)):
        raise ContractError("history evidence references must be a nonempty bounded ID list")
    if not isinstance(packet, dict) or packet.get("purpose") != "history_review":
        raise ContractError("history evidence requires an enriched history review packet")
    with _transaction(runtime, write=True):
        primary = _primary(runtime, project, packet, job_id)
        primary_events = packet.get("events") or []
        if not isinstance(primary_events, list) or not all(isinstance(event, dict) for event in primary_events):
            raise ContractError("invalid primary history event list")
        primary_ids = {event.get("id") for event in primary_events}
        requested = set(refs)
        if not requested & primary_ids:
            raise ContractError("history candidate must cite at least one primary event")
        # The full visible primary snapshot remains unchanged, even when only a
        # subset is cited. Never accept a related event masquerading as primary.
        for event in primary_events:
            row = runtime.db.execute(
                "SELECT e.*,p.role FROM problem_events p JOIN events e ON e.id=p.event "
                "WHERE p.problem=? AND p.event=? AND p.role<>'history_related'",
                (primary["id"], event.get("id"))).fetchone()
            if not row or _archive_event(runtime, project, primary, row) != event:
                raise ContractError("primary history event snapshot changed")
        related = packet.get("related_history", [])
        if (not isinstance(related, list) or len(related) > MAX_SOURCES
                or len(encode(related)) > MAX_RELATED_CHARS
                or not all(isinstance(item, dict) and isinstance(item.get("events"), list) for item in related)
                or sum(len(item["events"]) for item in related) > MAX_EVENTS):
            raise ContractError("related history context exceeds its source or event budget")
        visible, materials = {}, {primary["material_id"]}
        for item in related:
            source = _source_snapshot(runtime, project, job_id, item)
            if source["material_id"] in materials or not item["events"]:
                raise ContractError("related history must use distinct nonprimary source materials")
            materials.add(source["material_id"])
            total = runtime.db.execute(
                "SELECT COUNT(*) FROM problem_events WHERE problem=? AND role<>'history_related'",
                (source["id"],)).fetchone()[0]
            if total != item.get("event_count"):
                raise ContractError("related history evidence count changed")
            for event in item["events"]:
                if not isinstance(event, dict) or not isinstance(event.get("id"), str):
                    raise ContractError("invalid related history event")
                row = runtime.db.execute(
                    "SELECT e.*,p.role FROM problem_events p JOIN events e ON e.id=p.event "
                    "WHERE p.problem=? AND p.event=? AND p.role<>'history_related'",
                    (source["id"], event["id"])).fetchone()
                if not row or _archive_event(runtime, project, source, row) != event:
                    raise ContractError("related history event snapshot changed")
                if event["id"] in visible or event["id"] in primary_ids:
                    raise ContractError("duplicate related history event identity")
                visible[event["id"]] = event
        if requested - primary_ids - set(visible):
            raise ContractError("history evidence reference was not visible in this review packet")
        related_refs = sorted(requested & set(visible))
        # All references and snapshots are validated before the first write.
        # A savepoint also protects this operation if an outer caller catches a
        # later SQLite failure without rolling back its own transaction.
        savepoint = "history_related_" + uuid.uuid4().hex
        runtime.db.execute("SAVEPOINT " + savepoint)
        inserted = []
        try:
            for identity in related_refs:
                existing = runtime.db.execute(
                    "SELECT role FROM problem_events WHERE problem=? AND event=?",
                    (primary["id"], identity)).fetchone()
                if existing and existing[0] != "history_related":
                    raise ContractError("related event already has a primary evidence role")
                if not existing:
                    runtime.db.execute("INSERT INTO problem_events(problem,event,role) VALUES(?,?,'history_related')",
                                       (primary["id"], identity))
                    inserted.append(identity)
            runtime.db.execute("RELEASE SAVEPOINT " + savepoint)
        except BaseException:
            runtime.db.execute("ROLLBACK TO SAVEPOINT " + savepoint)
            runtime.db.execute("RELEASE SAVEPOINT " + savepoint)
            raise
    return {"problem_id": primary["id"], "linked_event_ids": inserted, "related_event_ids": related_refs}
