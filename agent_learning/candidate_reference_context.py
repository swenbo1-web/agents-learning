"""Optional candidate cards share the existing context budget and authority."""
from __future__ import annotations


def card(item, max_chars=400):
    from .runtime import clip, encode
    body = {"candidate_id": item["candidate_id"], "reference_version": item["reference_version"],
            "publication_state": "unpublished", "validation_level": item.get("validation_level", "reported"),
            "title": clip(item.get("title", ""), 36), "execution_authority": False, "read_required": True}
    note = "[待定参考 / unpublished; 非验证结论] "
    for key, value in (("limitations", clip(item.get("limitations", ""), 48)),
                       ("applicability", clip(item.get("applicability", ""), 48))):
        trial = {**body, key: value}
        if len(note + encode(trial)) <= max_chars:
            body = trial
    while len(note + encode(body)) > max_chars and len(body["title"]) > 8:
        body["title"] = clip(body["title"], max(8, len(body["title"]) // 2))
    return note + encode(body) if len(note + encode(body)) <= max_chars else None


def suppress_work(runtime, project, item, candidate_items=()):
    """Retain audit records; suppress only automatic repetition of risky sources."""
    from .candidate_references import lineage_risk
    if item.get("published_knowledge_risk"):
        return True
    references = set(item.get("source_event_refs", ()))
    repeated = {ref for candidate in candidate_items for ref in candidate.get("source_event_refs", ())}
    repeated.update(ref for item in candidate_items for source in item.get("sources", ())
                    for ref in source.get("evidence_refs", ()))
    repeated.update(event["event_id"] for item in candidate_items
                    for event in item.get("evidence", ()) if event.get("event_id"))
    repeated.update(event["event_id"] for item in candidate_items for source in item.get("sources", ())
                    for event in source.get("evidence", ()) if event.get("event_id"))
    if references and references <= repeated:
        return True
    return any(risk and risk.get("blocked", True) for ref in references
               for risk in [lineage_risk(runtime, project, ref)])
