"""Bounded, source-linked publication checks for original work statements.

Published governance must not be bypassed by replaying the same statement as
work history. Other statements citing the same event remain independent.
"""
from __future__ import annotations

import json
import re


MAX_REFERENCES = 64
MAX_PUBLICATIONS = 64
_BOUNDARY = re.compile(r"[\n。！？；;.!?]")
_CONCLUSION = re.compile(r"^(?:#{1,6}\s+)?(?:\*\*)?(?:结论|处理结论|解决结论|conclusion|decision|resolution)"
    r"(?:\*\*)?\s*(?:[:：](?:\*\*)?[ \t]*|\n)", re.I | re.M)
_SENTENCE = re.compile(r"[^\n]+?(?:[。！？]|[.!?](?=\s|$))")
_FOLLOWING_LABEL = re.compile(r"(?:\*\*)?(?:限制|适用条件|证据|验证|更正|limitations|conditions|evidence|verification|correction)"
                              r"(?:\*\*)?\s*[:：]", re.I)


def _materials(content):
    yield content
    from .knowledge_excerpt import conclusion_range
    # Only one explicitly scoped source conclusion can provide an additional
    # literal statement. Never infer it from a clipped preview or arbitrary prose.
    if conclusion_range(content) is None:
        return
    header = _CONCLUSION.search(content)
    sentence = _SENTENCE.match(content, header.end()) if header else None
    if sentence:
        for text in (content[header.start():sentence.end()].strip(), sentence[0].strip()):
            if len(text) >= 16:
                yield text


def _same_material(summary, content):
    if not isinstance(summary, str) or not isinstance(content, str):
        return False
    summary, content = summary.replace("\r\n", "\n").strip(), content.replace("\r\n", "\n").strip()
    if not summary or not content:
        return False
    if summary == content:
        return True
    # Only a complete original statement/block can establish this relation.
    # A word or a clipped preview is never enough to quarantine another claim.
    if len(content) < 16:
        return False
    for statement in _materials(content):
        if summary == statement:
            return True
        start = summary.find(statement)
        while start >= 0:
            end = start + len(statement)
            before, suffix = summary[:start].rstrip(" \t"), summary[end:]
            after = suffix.lstrip(" \t")
            if (not before or _BOUNDARY.fullmatch(before[-1])) and (
                    not after or _BOUNDARY.match(after) or suffix[:1] in {" ", "\t"}
                    or (_BOUNDARY.fullmatch(statement[-1]) and _FOLLOWING_LABEL.match(after))):
                return True
            start = summary.find(statement, start + 1)
    return False


def assess(runtime, project, summary, source_refs):
    """Use existing reverse indexes; never scan all published knowledge.

    The limit is on related publications, independently of how many match the
    text. Over-limit comparison is explicitly unknown and cannot grant advice.
    Original events, knowledge and work records are not modified.
    """
    refs = list(dict.fromkeys(source_refs))
    if not refs:
        return []
    if len(refs) > MAX_REFERENCES:
        return [{"blocked": True, "reason": "publication_comparison_limit"}]
    marks = ",".join("?" for _ in refs)
    rows = runtime.db.execute(
        "WITH related AS (SELECT DISTINCT s.project,s.knowledge_id,s.revision "
        "FROM problem_events pe INDEXED BY problem_events_event "
        "CROSS JOIN knowledge_sources s INDEXED BY knowledge_sources_problem ON s.problem_id=pe.problem AND s.project=? "
        f"WHERE pe.event IN ({marks}) AND EXISTS(SELECT 1 FROM json_each("
        "CASE WHEN json_valid(s.evidence_refs) THEN s.evidence_refs ELSE '[]' END) ref WHERE ref.value=pe.event) LIMIT ?) "
        "SELECT k.id,k.revision,CASE WHEN a.revision IS NOT k.revision OR EXISTS(SELECT 1 FROM knowledge_feedback f "
        "WHERE f.project=k.project AND f.knowledge_id=k.id AND f.revision=k.revision "
        "AND f.kind='counterexample' AND f.disposition='unresolved') THEN k.body END AS body,"
        "a.revision AS active_revision,EXISTS(SELECT 1 FROM knowledge_feedback f "
        "WHERE f.project=k.project AND f.knowledge_id=k.id AND f.revision=k.revision "
        "AND f.kind='counterexample' AND f.disposition='unresolved') AS disputed "
        "FROM related s JOIN knowledge k ON k.project=s.project AND k.id=s.knowledge_id AND k.revision=s.revision "
        "LEFT JOIN active a ON a.project=k.project AND a.id=k.id",
        (project, *refs, MAX_PUBLICATIONS + 1)).fetchall()
    if len(rows) > MAX_PUBLICATIONS:
        return [{"blocked": True, "reason": "publication_comparison_limit"}]
    result = []
    for row in rows:
        if row["active_revision"] == row["revision"] and not row["disputed"]:
            continue
        try:
            content = json.loads(row["body"]).get("content")
        except (ValueError, AttributeError):
            return [{"blocked": True, "reason": "publication_material_unavailable"}]
        if not isinstance(content, str):
            return [{"blocked": True, "reason": "publication_material_unavailable"}]
        if not _same_material(summary, content):
            continue
        reason = ("disputed" if row["disputed"] else "revoked" if row["active_revision"] is None else "superseded")
        result.append({"knowledge_id": row["id"], "revision": row["revision"], "reason": reason, "blocked": True})
    return sorted(result, key=lambda risk: (risk["knowledge_id"], risk["revision"]))
