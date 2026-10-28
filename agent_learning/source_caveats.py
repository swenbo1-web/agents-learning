"""Observable source caveats; counts are publication sources, not validations."""
from .source_evidence import unresolved_failure


def summarize_sources(sources):
    groups = {}
    for position, source in enumerate(sources):
        key = source.get("candidate_id") or ("legacy", position)
        group = groups.setdefault(key, {"healthy": True, "evidence": []})
        group["healthy"] &= source.get("health") == "valid"
        group["evidence"].extend(source.get("evidence", ()))
    failures, support = 0, 0
    for group in groups.values():
        evidence = group["evidence"]
        failed = [event for event in evidence if event.get("outcome") == "failed"]
        failed_sequence = max((event.get("sequence", 0) for event in failed), default=0)
        recovered_sequence = max((event.get("sequence", 0) for event in evidence
                                 if event.get("health") == "valid" and event.get("current_tool_success")), default=0)
        ordered = all(type(event.get("sequence")) is int and event["sequence"] > 0 for event in failed)
        pending = bool(failed) and (not ordered or unresolved_failure(failed_sequence, recovered_sequence))
        failures += int(pending)
        support += int(group["healthy"] and not pending)
    return {"state": "mixed_support" if failures and support else "unresolved_source_failures" if failures else "none",
            "unresolved_source_count": failures, "supporting_source_count": support, "source_count": len(groups)}


def context_caveat(item):
    value = item.get("source_caveat")
    if (not isinstance(value, dict) or value.get("state") not in {"mixed_support", "unresolved_source_failures"}
            or type(value.get("unresolved_source_count")) is not int or value["unresolved_source_count"] <= 0):
        return None
    support = value.get("supporting_source_count", 0)
    return {"state": value["state"], "unresolved_source_count": value["unresolved_source_count"],
            "supporting_source_count": support if type(support) is int and support >= 0 else 0}
