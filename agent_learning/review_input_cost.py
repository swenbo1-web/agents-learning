"""Frozen visible-input character accounting, never provider token estimates.

Snapshots belong to ``_local_admission`` and are not review evidence. Creation
uses the same projection as the model instruction once, during batch selection.
Settings reads validate the small snapshot; they never re-render old packets.
"""
from __future__ import annotations


_MAX_PACKET_CHARS = 1_000_000
_MAX_NODES = 100_000
_MAX_DEPTH = 64
_MAX_COUNTER = 1_000_000_000
_COMPONENTS = ("instructions", "source_events", "task_context", "governance_target", "related_history",
               "related_knowledge", "related_candidates", "shared_context", "packet_metadata")
_SLOTS = {"events": "source_events", "task": "task_context", "target_knowledge": "governance_target",
          "related_history": "related_history", "related_knowledge": "related_knowledge",
          "related_candidates": "related_candidates", "review_transport": "shared_context"}
_REASONS = {"not_recorded", "invalid_snapshot", "packet_unavailable", "projection_limit"}


def _unknown(reason):
    return {"status": "unknown", "unit": "characters", "reason": reason}


def _bounded(value):
    def members(mapping):
        for key, child in mapping.items():
            # JSON keys also consume text. Inspect them before serializing,
            # without constructing a second list for a wide dictionary.
            yield key
            yield child

    pending, nodes, text_chars = [(iter((value,)), 0)], 0, 0
    while pending:
        values, depth = pending[-1]
        try:
            item = next(values)
        except StopIteration:
            pending.pop()
            continue
        nodes += 1
        if nodes > _MAX_NODES or depth > _MAX_DEPTH:
            return False
        if isinstance(item, dict):
            pending.append((members(item), depth + 1))
        elif isinstance(item, list):
            # Only iterators for the current path are held: a wide container
            # cannot allocate all its child tuples before the node guard runs.
            pending.append((iter(item), depth + 1))
        elif isinstance(item, str):
            text_chars += len(item)
            if text_chars > _MAX_PACKET_CHARS:
                return False
    return True


def _case_chars(case, counts):
    """Allocate exactly the rendered JSON value characters at known slots.

    Keys, commas, brackets and all non-classified metadata use packet_metadata.
    Business payloads are counted intact as events; same-named business fields
    never become a structural task, reference or another child case.
    """
    from .runtime import encode
    total = len(encode(case))
    allocated = 0
    for key, component in _SLOTS.items():
        if key in case:
            size = len(encode(case[key]))
            counts[component] += size
            allocated += size
    if case.get("purpose") == "phase_review" and isinstance(case.get("items"), list):
        # The enclosing list and key stay metadata. A well-formed frozen batch
        # has dictionary cases, but non-object values still reconcile exactly.
        for child in case["items"]:
            if isinstance(child, dict):
                size = _case_chars(child, counts)
            else:
                size = len(encode(child))
                counts["packet_metadata"] += size
            allocated += size
    counts["packet_metadata"] += total - allocated
    return total


def input_cost_snapshot(packet, *, outer_prefix="", schema=None):
    """Count this selected instruction, excluding host prompts and provider use.

    The optional JSON response schema is measured separately; adapters may send
    it outside the instruction or add wrappers. This is not a provider request
    total, a cache measurement, or evidence that a model request was launched.
    """
    from .review_packet import REVIEW_SUFFIX, _instruction_parts
    from .review_transport import TRANSPORT_PREFIX
    from .runtime import encode
    if not isinstance(packet, dict) or not isinstance(outer_prefix, str):
        return _unknown("projection_limit")
    try:
        if not _bounded(packet) or len(encode(packet)) > _MAX_PACKET_CHARS:
            return _unknown("projection_limit")
        prefix, projected, visible = _instruction_parts(packet)
        encoded = encode(visible)
        instruction = outer_prefix + prefix + encoded + REVIEW_SUFFIX
        counts = dict.fromkeys(_COMPONENTS, 0)
        counts["instructions"] = len(outer_prefix) + len(prefix) + len(REVIEW_SUFFIX)
        _case_chars(visible, counts)
        applied = visible is not projected
        saved = len(encode(projected)) - len(encoded) - len(TRANSPORT_PREFIX) if applied else 0
        schema_text = encode(schema) if schema is not None and _bounded(schema) else None
        if (len(instruction) > _MAX_PACKET_CHARS or saved < 0
                or schema_text is not None and len(schema_text) > _MAX_PACKET_CHARS):
            return _unknown("projection_limit")
        result = {"status": "recorded", "version": 1, "scope": "frozen_visible_instruction", "unit": "characters",
            "instruction_chars": len(instruction), "instruction_bytes": len(instruction.encode("utf-8")),
            "schema_chars": len(schema_text) if schema_text is not None else None,
            "schema_bytes": len(schema_text.encode("utf-8")) if schema_text is not None else None,
            "components": counts, "transport_applied": applied, "transport_saved_chars": saved,
            "provider_overhead_included": False}
        return validate_input_cost(result)
    except (ValueError, TypeError, RecursionError, OverflowError, UnicodeError):
        return _unknown("projection_limit")


def validate_input_cost(value):
    """Validate bounded server snapshot numbers without exposing stored extras."""
    if not isinstance(value, dict):
        return _unknown("invalid_snapshot")
    if value.get("status") == "unknown":
        reason = value.get("reason")
        return _unknown(reason if isinstance(reason, str) and reason in _REASONS else "invalid_snapshot")

    def count(number):
        return type(number) is int and 0 <= number <= _MAX_COUNTER

    components = value.get("components")
    chars, byte_count = value.get("instruction_chars"), value.get("instruction_bytes")
    schema_chars, schema_bytes = value.get("schema_chars"), value.get("schema_bytes")
    applied, saved = value.get("transport_applied"), value.get("transport_saved_chars")
    if (value.get("status") != "recorded" or type(value.get("version")) is not int or value["version"] != 1
            or value.get("scope") != "frozen_visible_instruction" or value.get("unit") != "characters"
            or value.get("provider_overhead_included") is not False or type(applied) is not bool
            or not count(chars) or not count(byte_count) or not chars <= byte_count <= chars * 4
            or not isinstance(components, dict) or set(components) != set(_COMPONENTS)
            or not all(count(number) for number in components.values()) or sum(components.values()) != chars
            or not count(saved) or (saved <= 0 if applied else saved != 0)
            or ((schema_chars is None) != (schema_bytes is None))
            or schema_chars is not None and (not count(schema_chars) or not count(schema_bytes)
                or not schema_chars <= schema_bytes <= schema_chars * 4)):
        return _unknown("invalid_snapshot")
    return {"status": "recorded", "version": 1, "scope": "frozen_visible_instruction", "unit": "characters",
        "instruction_chars": chars, "instruction_bytes": byte_count, "schema_chars": schema_chars,
        "schema_bytes": schema_bytes, "components": dict(components), "transport_applied": applied,
        "transport_saved_chars": saved, "provider_overhead_included": False}


def cost_from_packet(packet):
    """Old or unreadable packets stay unknown; no historical reconstruction."""
    if not isinstance(packet, dict):
        return _unknown("packet_unavailable")
    local = packet.get("_local_admission")
    if not isinstance(local, dict) or "input_cost" not in local:
        return _unknown("not_recorded")
    return validate_input_cost(local["input_cost"])
