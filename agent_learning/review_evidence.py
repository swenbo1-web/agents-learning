"""Exact, case-scoped short references for model-facing review evidence.

Aliases are a presentation aid, never a source lookup or a permission grant.
The frozen packet and archived public model reply retain their original values.
"""
from __future__ import annotations

from copy import deepcopy


VERSION = 1
REFERENCE_FIELD = "evidence_ref"
REFERENCE_PREFIX = (
    "evidence_refs优先原样复制当前子案例events[].evidence_ref短引用，也兼容完整events[].id；"
    "不得截短、拼接或借用其他子案例引用。历史仅可引用本批实际可见的related_history证据，仍须含主资料。\n"
)


def _event_groups(case):
    """Visit only structural source slots, never IDs inside business payloads."""
    if isinstance(case.get("events"), list):
        yield case["events"]
    related = case.get("related_history")
    if case.get("purpose") == "history_review" and isinstance(related, list):
        for source in related:
            if isinstance(source, dict) and isinstance(source.get("events"), list):
                yield source["events"]


def _cases(packet, *, include_parent=False):
    if packet.get("purpose") != "phase_review":
        yield packet
    else:
        if include_parent:
            yield packet
        if isinstance(packet.get("items"), list):
            yield from (case for case in packet["items"] if isinstance(case, dict))


def source_packet(packet):
    """Remove only structural presentation fields, never business payload data.

    Authoritative packets normally contain no aliases and return unchanged.
    This also keeps evidence/dependency comparisons stable for model previews.
    """
    if not isinstance(packet, dict):
        return packet
    if not any(REFERENCE_FIELD in event for case in _cases(packet, include_parent=True)
               for group in _event_groups(case) for event in group if isinstance(event, dict)):
        return packet
    result = deepcopy(packet)
    for case in _cases(result, include_parent=True):
        for group in _event_groups(case):
            for event in group:
                if isinstance(event, dict):
                    event.pop(REFERENCE_FIELD, None)
    return result


def aliases(case):
    """Map aliases only to whole, actually visible source event identities."""
    from .runtime import digest

    if not isinstance(case, dict):
        return {}
    problem = case.get("problem")
    if (not isinstance(problem, dict) or not isinstance(problem.get("id"), str)
            or type(problem.get("revision")) is not int or problem["revision"] < 1):
        return {}
    identities = sorted({event["id"] for group in _event_groups(case) for event in group
                         if isinstance(event, dict) and isinstance(event.get("id"), str) and event["id"]})
    if not identities:
        return {}
    # The domain includes the complete frozen case, not just an ordinal: a
    # sibling or changed packet cannot accidentally reuse its "first event".
    domain = source_packet(case)
    prefix = "ev-" + digest([VERSION, domain])[:12] + "-"
    return {prefix + str(index): identity for index, identity in enumerate(identities, 1)}


def canonical_refs(case, refs):
    """Unknown spellings stay unknown for the ordinary source checks to reject."""
    mapping = aliases(case)
    return [mapping.get(ref, ref) for ref in refs]


def project_packet(packet):
    """Add short references to a model projection without touching raw IDs."""
    if not isinstance(packet, dict):
        return packet
    result = deepcopy(packet)
    for case in _cases(result):
        reverse = {identity: alias for alias, identity in aliases(case).items()}
        for group in _event_groups(case):
            for event in group:
                if isinstance(event, dict) and isinstance(event.get("id"), str) and event["id"] in reverse:
                    event[REFERENCE_FIELD] = reverse[event["id"]]
    return result
