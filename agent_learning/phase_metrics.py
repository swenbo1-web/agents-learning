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
    for key in ("input_tokens", "output_tokens", "total_tokens"):
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
