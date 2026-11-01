"""Exact, case-scoped short references for model-facing review evidence.

Aliases are a presentation aid, never a source lookup or a permission grant.
The frozen packet and archived public model reply retain their original values.
"""
from __future__ import annotations

from copy import deepcopy


VERSION = 1
REFERENCE_FIELD = "evidence_ref"
MODE_FIELD = "_evidence_reference_mode"
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
    if not any(MODE_FIELD in case or any(REFERENCE_FIELD in event
               for group in _event_groups(case) for event in group if isinstance(event, dict))
               for case in _cases(packet, include_parent=True)):
        return packet
    result = deepcopy(packet)
    for case in _cases(result, include_parent=True):
        case.pop(MODE_FIELD, None)
        for group in _event_groups(case):
            for event in group:
                if isinstance(event, dict):
                    event.pop(REFERENCE_FIELD, None)
    return result


def raw_packet(packet):
    """Freeze raw-ID rendering while packing real evidence before decoration."""
    result = deepcopy(source_packet(packet))
    if isinstance(result, dict):
        for case in _cases(result, include_parent=True):
            case[MODE_FIELD] = "raw"
    return result


def prefer_short_refs(packet, fits):
    """Enable aliases only when the complete final packet still fits unchanged.

    ``fits`` checks the final wrapper, including any caller-specific token cap.
    Neither candidate mutates the source or changes its evidence membership.
    """
    short = source_packet(packet)
    return short if fits(short) else raw_packet(packet)


def has_short_refs(packet):
    return (isinstance(packet, dict) and any(REFERENCE_FIELD in event
            for case in _cases(packet) for group in _event_groups(case)
            for event in group if isinstance(event, dict)))


def aliases(case):
    """Map aliases only to whole, actually visible source event identities."""
    from .runtime import digest

    if not isinstance(case, dict) or case.get(MODE_FIELD) == "raw":
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
    """Render frozen policy without touching raw IDs or exposing that policy.

    Alias projections are idempotent. Raw wire output intentionally omits the
    internal mode and must not be reused as an authoritative/frozen packet;
    acceptance and local rechecks always use the original frozen packet.
    """
    if not isinstance(packet, dict):
        return packet
    result = deepcopy(packet)
    for case in _cases(result):
        reverse = {identity: alias for alias, identity in aliases(case).items()}
        for group in _event_groups(case):
            for event in group:
                if isinstance(event, dict):
                    event.pop(REFERENCE_FIELD, None)
                if isinstance(event, dict) and isinstance(event.get("id"), str) and event["id"] in reverse:
                    event[REFERENCE_FIELD] = reverse[event["id"]]
    for case in _cases(result, include_parent=True):
        case.pop(MODE_FIELD, None)
    return result
