"""Bounded, read-only stage batch measurements; never infer per-knowledge costs."""
from __future__ import annotations

import json
import math


_PACKET_LIMIT = 1_000_000
_RESULT_LIMIT = 60_000
_USAGE_LIMIT = 24_000
_CALL_LIMIT = 32


def _object(value, limit):
    if not isinstance(value, str) or len(value) > limit:
        return None
    try:
        body = json.loads(value)
    except (ValueError, TypeError):
        return None
    return body if isinstance(body, dict) else None


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _selection_diagnostics(packet, packet_chars):
    """Describe frozen selection, never reconstruct its historical trigger.

    Related-reference counts are occurrences across cases, not unique knowledge.
    This projection does not re-run triage, retrieval, readiness or model work.
    """
    if not isinstance(packet, dict) or not isinstance(packet.get("items"), list):
        return None
    items = packet["items"]
    if any(not isinstance(item, dict) for item in items):
        return None

    def count(key):
        value = packet.get(key)
        return value if type(value) is int and value >= 0 else None

    # ID previews are bounded; their length cannot replace an absent total.
    reused = count("reused_problem_count")
    omitted = count("omitted_problem_count")
    related = {}
    for key in ("related_knowledge", "related_candidates"):
        lists = [item.get(key) for item in items]
        related[key] = sum(map(len, lists)) if all(isinstance(value, list) for value in lists) else None
    phases = packet.get("phase_ids")
    snapshot = packet.get("_local_admission")
    trigger, selection_policy = "not_recorded", None
    if isinstance(snapshot, dict):
        version, reason = snapshot.get("version"), snapshot.get("trigger_reason")
        frequency, wait = snapshot.get("every_phases"), snapshot.get("max_wait_seconds")
        if (type(version) is int and version == 1 and isinstance(reason, str)
                and reason in {"phase_count", "max_wait", "continuation", "explicit_request"}
                and type(frequency) is int and 1 <= frequency <= 20
                and type(wait) is int and 0 <= wait <= 86400
                and (reason != "max_wait" or wait > 0)):
            trigger = reason
            selection_policy = {"every_phases": frequency, "max_wait_seconds": wait}
    return {"scope": "frozen_batch_selection", "trigger_reason": trigger,
        "policy_at_selection": selection_policy,
        "selection_reason": "visible_material_selected" if items else "local_reuse_only" if reused
            else "uncovered_material" if omitted else "no_visible_material",
        "packet_chars": packet_chars, "phase_count": len(phases) if isinstance(phases, list) else None,
        "selected_cases": len(items), "reused_cases": reused, "uncovered_cases": omitted,
        "related_knowledge_refs": related["related_knowledge"],
        "related_candidate_refs": related["related_candidates"]}


def _outcomes(result, state):
    items = result.get("items") if result else None
    if not isinstance(items, list):
        return (0, 0, 0) if state == "no_ready_material" else (None, None, None)
    candidates, published = set(), set()
    for item in items:
        if not isinstance(item, dict):
            continue
        identities = item.get("candidate_ids")
        if not isinstance(identities, list):
            identities = [item["candidate_id"]] if isinstance(item.get("candidate_id"), str) else []
        identities = [identity for identity in identities if isinstance(identity, str)]
        candidates.update(identities)
        publications = item.get("publications")
        if isinstance(publications, list):
            published.update(value["candidate_id"] for value in publications
                             if isinstance(value, dict) and value.get("status") == "published"
                             and isinstance(value.get("candidate_id"), str))
        elif isinstance(item.get("publication"), dict) and item["publication"].get("status") == "published":
            # Older single-candidate receipts do not carry a publications list.
            published.update(identities)
    return len(items), len(candidates), len(published)


def batch_metrics(runtime, project, batch_id):
    """Counts refer to the frozen batch, not all project cases or formal knowledge.

    Call rows are counted once even when one call produced many case receipts.
    A launch intent is an execution attempt, not proof of a paid provider request.
    Published counts describe successful candidate publication in this review;
    later manual approvals and unique knowledge identities are different metrics.
    """
    exists = runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name='phase_review_batches'").fetchone()
    if not exists:
        return None
    row = runtime.db.execute("SELECT state,created,substr(packet,1,?),substr(result,1,?) "
        "FROM phase_review_batches WHERE project=? AND host_scope=? AND problem_id=?",
        (_PACKET_LIMIT + 1, _RESULT_LIMIT + 1, project, runtime._session_scope(), batch_id)).fetchone()
    if row is None:
        return None
    packet, result = _object(row[2], _PACKET_LIMIT), _object(row[3], _RESULT_LIMIT)
    items = packet.get("items") if packet else None
    submitted = len(items) if isinstance(items, list) else None
    evidence = None
    if isinstance(items, list):
        evidence = len({event["id"] for item in items if isinstance(item, dict)
            for event in (item.get("events") if isinstance(item.get("events"), list) else [])
            if isinstance(event, dict) and isinstance(event.get("id"), str)})
    contexts = [item["phase_tool_context"] for item in (items if isinstance(items, list) else []) if isinstance(item, dict)
                and isinstance(item.get("phase_tool_context"), dict)
                and type(item["phase_tool_context"].get("selected_count")) is int
                and 0 <= item["phase_tool_context"]["selected_count"] <= 3]
    omitted_context = any(isinstance(item, dict) and item.get("phase_tool_context_omitted")
                          for item in (items if isinstance(items, list) else []))
    tool_context = {"selected_observations": sum(item.get("selected_count", 0) for item in contexts),
                    "selection_limited": bool(omitted_context or any(item.get("omitted_matching") or item.get("selection_unavailable")
                        or item.get("scan_limited") for item in contexts))} if contexts or omitted_context else None
    result_cases, candidates, published = _outcomes(result, row[0])
    calls = runtime.db.execute("SELECT id,state,created,started,launch_intent,finished,substr(usage,1,?) "
        "FROM admin_calls WHERE project=? AND problem=? AND state!='deduplicated' ORDER BY created,id LIMIT ?",
        (_USAGE_LIMIT + 1, project, batch_id, _CALL_LIMIT + 1)).fetchall()
    truncated = len(calls) > _CALL_LIMIT
    count_row = runtime.db.execute("SELECT COUNT(*),COALESCE(SUM(CASE WHEN state!='not_started' AND "
        "(launch_intent IS NOT NULL OR started IS NOT NULL) THEN 1 ELSE 0 END),0),"
        "MIN(created),MAX(finished),COALESCE(SUM(finished IS NULL),0) "
        "FROM admin_calls WHERE project=? AND problem=? AND state!='deduplicated'", (project, batch_id)).fetchone()
    call_count, elapsed, tokens = count_row[1], [], []
    for call in calls[:_CALL_LIMIT]:
        if call[1] == "not_started" or call[3] is None and call[4] is None:
            continue
        usage = _object(call[6], _USAGE_LIMIT)
        elapsed.append(usage.get("elapsed_seconds") if usage else None)
        try:
            from .admin_usage import normalize_usage
            tokens.append(normalize_usage(usage) if usage else None)
        except (ValueError, TypeError):
            tokens.append(None)
    complete = bool(tokens) and not truncated and all(value and value["completeness"] == "complete" for value in tokens)
    totals = {}
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
        values = [value["token_totals"].get(key) if value else None for value in tokens]
        if key == "total_tokens":
            values = [value["token_totals"].get(key,
                value["token_totals"]["input_tokens"] + value["token_totals"]["output_tokens"]
                if {"input_tokens", "output_tokens"} <= value["token_totals"].keys() else None)
                if value else None for value in tokens]
        totals[key] = sum(values) if values and not truncated and all(_number(value) for value in values) else None
    reported = any(value and value["token_totals"] for value in tokens)
    # A recovered decision can finish much later than its original paid process.
    # Preserve provider timing/usage while including that wait in batch timing.
    recovered, completed_at = 0, count_row[3]
    if runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name='admin_response_records'").fetchone():
        local = runtime.db.execute("SELECT COUNT(*),MAX(r.updated) FROM admin_response_records r "
            "JOIN admin_calls c ON c.project=r.project AND c.id=r.call_id WHERE r.project=? AND r.host_scope=? "
            "AND c.problem=? AND c.state='failed' AND r.state='accepted'",
            (project, runtime._session_scope(), batch_id)).fetchone()
        recovered = local[0]
        if recovered and _number(local[1]):
            completed_at = max(completed_at or 0, local[1])
    return {"submitted_cases": submitted, "evidence_records": evidence, "result_cases": result_cases,
        "selection_diagnostics": _selection_diagnostics(packet, len(row[2]) if packet else None),
        "tool_context": tool_context,
        "candidate_count": candidates, "published_count": published, "call_count": call_count,
        "call_records": count_row[0], "call_count_scope": "recorded_execution_attempts",
        "batch_elapsed_seconds": round(completed_at - row[1], 3)
            if count_row[0] and not count_row[4] and _number(completed_at) and completed_at >= row[1] else None,
        "local_recheck_count": recovered,
        "call_elapsed_seconds": round(sum(elapsed), 3)
            if elapsed and not truncated and all(_number(value) for value in elapsed) else None,
        "call_elapsed_scope": "execution_and_local_processing", "usage_scope": "host_reported_per_call",
        "usage_completeness": "complete" if complete else "partial" if reported else "unknown",
        **totals, "call_details_truncated": truncated,
        "material_counts_complete": packet is not None, "result_counts_complete": result is not None}
