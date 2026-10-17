"""一次有限的管理员审核。快照与记账先落盘，模型调用在事务外进行。"""
from __future__ import annotations

import copy
import contextlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from .mcp import CANDIDATE, validate
from .runtime import ContractError, clean, digest, encode
from .review_packet import PACKET_FORMAT, REVIEW_PREFIX as _REVIEW_PREFIX, review_instruction


class ReviewStateChanged(ContractError):
    """A valid response lost its frozen local context; not an executor failure."""


@contextlib.contextmanager
def _review_state_guard():
    try:
        yield
    except ContractError as exc:
        raise ReviewStateChanged("administrator review context changed before commit") from exc


def _strict_schema(contract):
    """模型输出要求所有对象字段必填；公开 MCP 仍兼容旧候选。"""
    result = copy.deepcopy(contract)
    if isinstance(result, dict):
        if result.get("type") == "object":
            result["required"] = list(result.get("properties", {}))
            result["additionalProperties"] = False
        return {key: _strict_schema(value) for key, value in result.items()}
    if isinstance(result, list):
        return [_strict_schema(value) for value in result]
    return result


RESPONSE_SCHEMA = _strict_schema({
    "type": "object", "properties": {
        "decision": {"type": "string", "enum": ["candidate", "needs_evidence", "skip", "governance"]},
        "reason": {"type": "string"},
        "candidate": {"anyOf": [CANDIDATE, {"type": "null"}]},
        "missing_evidence": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 1000}},
        "proposal": {"anyOf": [{"type": "object", "properties": {
            "action": {"type": "string", "enum": ["publish", "update", "attach", "revoke", "dismiss_counterexample"]},
            "knowledge_id": {"type": ["string", "null"]},
            "expected_revision": {"type": ["integer", "null"], "minimum": 1}},
            "required": ["action", "knowledge_id", "expected_revision"], "additionalProperties": False}, {"type": "null"}]},
    }, "required": ["decision", "reason", "candidate"], "additionalProperties": False,
})

# 多条输出只用于用户显式启动的历史批次；日常审核与工作摘要保持单条契约。
HISTORY_RESPONSE_SCHEMA = copy.deepcopy(RESPONSE_SCHEMA)
HISTORY_RESPONSE_SCHEMA["properties"]["candidates"] = {
    "type": "array", "maxItems": 5, "items": _strict_schema(CANDIDATE)}
HISTORY_RESPONSE_SCHEMA["required"].append("candidates")


def _history_packet(runtime, project, problem_id, job_id):
    from .history_context import enrich_history_packet
    limit = runtime.admin_effective_config(project)["max_input_chars"]
    packet = runtime.review_packet(project, problem_id, max_chars=max(1000, limit - 800))
    return enrich_history_packet(runtime, project, packet, job_id, max_chars=limit)

_TOKEN_FIELDS = {"input_tokens", "output_tokens", "cached_input_tokens", "total_tokens"}
_TRUNCATION_NOTE = "原始证据已投影或截短；结论仅限可见记录，未核对完整原文。"


def _executor_semantics(provider_plan):
    # 身份边界由不可变调用计划另行核对，不能使换号或换会话绕过未知调用。
    result = {key: provider_plan.get(key) for key in ("provider", "model", "model_mode", "model_source", "command")}
    if provider_plan.get("dsh_profile") is not None:
        result["dsh_profile"] = provider_plan["dsh_profile"]
    return result


def _review_configuration(cfg, provider_plan):
    result = {key: value for key, value in cfg.items() if key not in
              {"executor", "routing_ready", "routing_reason", "antigravity_command", "dsh_command", "max_run_seconds"}}
    result.update(_executor_semantics(provider_plan))
    return result


def _fingerprint(packet):
    # 去掉传输身份而非业务参数；业务对象中的 id/version 等字段仍参与比较。
    from .event_capture import business_evidence_digest, normalize_business_payload, projected_evidence
    events, seen, actors, assignments = [], set(), {}, {}
    for event in packet["events"]:
        payload = normalize_business_payload(event.get("payload", {}))
        business_digest = event.get("business_digest")
        if not business_digest and not projected_evidence(payload) and event.get("type") != "missing":
            business_digest = business_evidence_digest(payload)
        if business_digest:
            # 比较全量业务证据；可见预览的相同首尾不能证明完整日志相同。
            payload = {"business_digest": business_digest}
        else:
            # 旧截短记录没有完整业务摘要，只能复用本事件，不能跨事件推定等价。
            payload = {"legacy_event_id": event.get("id"), "visible_payload": payload}
        actor = event.get("agent")
        actor_slot = "parent" if actor is None else actors.setdefault(actor, f"child-{len(actors)}")
        assignment = event.get("assignment_id")
        assignment_slot = None if assignment is None else assignments.setdefault(
            (actor_slot, assignment), f"assignment-{len(assignments)}")
        # 身份值可以变化，父子与派单的关系不能消失；当前失效状态也参与去重。
        item = {"type": event["type"], "outcome": event["outcome"], "payload": payload,
                "role": event.get("role", "observation"), "health": event.get("health", "valid"),
                "current_tool_success": event.get("current_tool_success", event["type"] == "PostToolUse"
                                                    and event["outcome"] == "succeeded"),
                "actor": actor_slot, "assignment": assignment_slot}
        identity = encode(item)
        if identity not in seen:
            events.append(item)
            seen.add(identity)
    task = packet.get("task") or {}
    problem = packet["problem"]
    summary = clean(problem.get("summary", ""))
    if packet.get("purpose") == "work_summary":
        # 阶段请求的传输摘要不是新增工作；变更write-version不能绕过未知调用阻止。
        summary = re.sub(r" \[[a-f0-9]{16}\]$", "", summary)
    identity = {"summary": summary,
                   "category": problem.get("category", "unknown"),
                   "goal": clean(task.get("goal", "")), "scope": clean(task.get("scope", {})),
                   "events": events, "evidence_truncated": bool(packet.get("evidence_truncated")),
                   "incomplete": bool(packet.get("incomplete")),
                   "feedback": {key: problem.get(key) for key in
                                ("knowledge_id", "knowledge_revision", "resolution_status")}}
    if packet.get("purpose") == "history_review":
        identity["history_context"] = packet.get("related_history", [])
    return digest(identity)


def _usage(log_path, elapsed):
    """有界流式累计全部可读报告；预览裁剪不丢累计，缺失不写成零。"""
    records, totals, seen = [], {}, {}
    pending, failed = set(), set()
    anonymous_pending, anonymous_failed, started_turns = 0, 0, 0
    truncated, incomplete, count, scanned = False, False, 0, 0
    if log_path is None:
        return {"elapsed_seconds": round(elapsed, 3), "token_usage": [], "token_totals": {},
                "completeness": "unknown", "log_tail_truncated": False, "preview_truncated": False,
                "reported_turns": 0}
    try:
        with log_path.open("rb") as stream:
            while True:
                line = stream.readline(65537)
                if not line:
                    break
                scanned += len(line)
                if scanned > 64 * 1024 * 1024:
                    truncated = True
                    break
                if len(line) > 65536:
                    truncated = True
                    # 跳过整条超长记录，不把其余片段当成新的完整事件。
                    while line and not line.endswith(b"\n"):
                        line = stream.readline(65537)
                        scanned += len(line)
                        if scanned > 64 * 1024 * 1024:
                            break
                    if scanned > 64 * 1024 * 1024:
                        break
                    continue
                try:
                    event = json.loads(line)
                except (UnicodeError, ValueError):
                    incomplete |= line.lstrip().startswith(b"{")
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                identity = event.get("turn_id", event.get("id"))
                identity = identity if isinstance(identity, str) and identity else None
                if kind == "turn.started":
                    if identity is None:
                        anonymous_pending += 1
                        started_turns += 1
                    elif identity not in pending and identity not in seen:
                        pending.add(identity)
                        started_turns += 1
                    continue
                if kind in {"turn.failed", "turn.cancelled", "turn.canceled"}:
                    if identity is None:
                        anonymous_failed += 1
                        anonymous_pending = max(0, anonymous_pending - 1)
                    else:
                        # 同一turn的失败/取消与完成是终态矛盾；后到完成不能抹掉它。
                        failed.add(identity)
                        pending.discard(identity)
                    continue
                if kind != "turn.completed":
                    continue
                if identity is None:
                    anonymous_pending = max(0, anonymous_pending - 1)
                else:
                    pending.discard(identity)
                usage = event.get("usage")
                if not isinstance(usage, dict):
                    incomplete = True
                    continue
                numeric = {key: value for key, value in usage.items()
                           if key in _TOKEN_FIELDS and isinstance(value, int)
                           and not isinstance(value, bool) and value >= 0}
                incomplete |= not {"input_tokens", "output_tokens"} <= numeric.keys()
                incomplete |= any(key in _TOKEN_FIELDS and key not in numeric for key in usage)
                incomplete |= ("total_tokens" in numeric and {"input_tokens", "output_tokens"} <= numeric.keys()
                               and numeric["total_tokens"] != numeric["input_tokens"] + numeric["output_tokens"])
                if identity:
                    if identity in seen:
                        if seen[identity] != numeric:
                            incomplete = True
                            # 冲突报告保留已观察最大值；不重复累加同一turn。
                            old = seen[identity]
                            for key, value in numeric.items():
                                if value > old.get(key, 0):
                                    totals[key] = totals.get(key, 0) + value - old.get(key, 0)
                                    old[key] = value
                        continue
                    seen[identity] = dict(numeric)
                if numeric:
                    count += 1
                    for key, value in numeric.items():
                        totals[key] = totals.get(key, 0) + value
                    records.append(numeric)
                    if len(records) > 16:
                        records.pop(0)
                else:
                    incomplete = True
    except OSError:
        truncated = True
    unreported_turns = len(pending) + len(failed) + anonymous_pending + anonymous_failed
    incomplete |= unreported_turns > 0
    return {"elapsed_seconds": round(elapsed, 3), "token_usage": records, "token_totals": totals,
            "reported_turns": count, "preview_truncated": count > 16,
            "started_turns": started_turns, "unreported_turns": unreported_turns,
            "completeness": "complete" if count and not truncated and not incomplete else "partial" if totals else "unknown",
            "log_tail_truncated": truncated}


def _finish(runtime, call_id, state, log_path, started_at, provider_plan=None, *, execution=None):
    from .admin_lifecycle import UNKNOWN_STATES, finish_call
    from .admin_providers import antigravity_usage
    elapsed = time.monotonic() - started_at
    stream_provider = provider_plan["provider"] if provider_plan and provider_plan.get("provider") in {"antigravity", "dsh"} else None
    usage = (antigravity_usage(log_path, elapsed, provider=stream_provider) if stream_provider
             else _usage(log_path, elapsed))
    if provider_plan:
        usage["executor"] = {key: provider_plan.get(key) for key in
                             ("provider", "model", "model_source", "model_mode", "host", "host_scope", "policy_version", "executor_signature")}
    if execution is not None:
        # Only adapter-owned scalar observations survive temporary log cleanup.
        # Input submission does not establish model receipt or paid inference.
        usage["execution"] = dict(execution)
    summary = runtime.db.execute("SELECT source_digest FROM work_summary_links WHERE call_id=?", (call_id,)).fetchone()
    if summary:
        usage.update(purpose="work_summary", work_source_digest=summary["source_digest"])
    current = runtime.db.execute("SELECT state FROM admin_calls WHERE id=?", (call_id,)).fetchone()
    if state in UNKNOWN_STATES or (current and current[0] in UNKNOWN_STATES):
        usage["call_outcome_unknown"] = True
        usage["completeness"] = "partial" if usage["token_totals"] else "unknown"
    row = finish_call(runtime, call_id, state, usage=usage)
    return json.loads(row["usage"])


def _settings(cfg):
    command = cfg["command"]
    if (not isinstance(command, list) or not command
            or not all(isinstance(value, str) and value for value in command)):
        raise ContractError("admin.command must be an executable argument array")
    ranges = {"timeout_seconds": (1, 600), "max_calls_per_day": (0, 100000),
              "max_input_chars": (1000, 200000), "max_cases_per_batch": (1, 100),
              "max_calls_per_batch": (1, 20), "max_batch_seconds": (1, 600), "max_run_seconds": (1, 3600)}
    for key, (low, high) in ranges.items():
        value = cfg.get(key, 600) if key == "max_run_seconds" else cfg[key]
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ContractError(f"invalid admin.{key}")
    from .admin_usage import budget_settings
    class Settings:
        config = {"admin": cfg}
    budget_settings(Settings())
    return list(command), cfg["timeout_seconds"]


def _select_review(runtime, project, problem_id, scan_limit, timeout, expected_epoch, history_job_id=None):
    from .admin_decisions import find_receipt, persist_receipt, receipt_for_problem, review_signature
    from .admin_lifecycle import claim_call, recover_calls
    from .admin_queue import (eligible_query, executor_query, remember_check, scan_signature,
                              executor_backoff, executor_wait_reason, REVIEW_ORDER)
    from .admin_providers import executor_availability
    cfg = runtime.admin_effective_config(project)
    if history_job_id is None and not cfg["enabled"]:
        return {"status": "disabled", "scanned_count": 0}
    epoch = runtime.assert_host_epoch(expected_epoch)
    recover_calls(runtime, project)
    signature = scan_signature(runtime, project)
    preflight_signature = scan_signature(runtime, project, include_candidates=False)
    if history_job_id is not None:
        runtime.history_guard(project, problem_id, history_job_id, require_review=True, expected_epoch=epoch)
    summary_enabled = runtime.work_summary_enabled(project)
    query, args = eligible_query(signature, problem_id, preflight_signature=preflight_signature, history_job_id=history_job_id,
                                summary_enabled=summary_enabled, summary_host_scope=runtime._session_scope())
    raw_query, raw_args = query, args
    if history_job_id is None:
        query, args = executor_query(runtime, project, query, args)
    rows = runtime.db.execute(query + REVIEW_ORDER + "LIMIT ?",
                              (project, *args, 1 if problem_id is not None else scan_limit)).fetchall()
    skipped, routing_skipped, scanned = [], [], 0
    if not rows:
        if history_job_id is None and runtime.db.execute(raw_query + "LIMIT 1", (project, *raw_args)).fetchone():
            from .admin_queue import unrouted_reason
            return {"status": "executor_not_ready", "reason": unrouted_reason(runtime, project, raw_query, raw_args,
                    "administrator_current_model_unavailable"), "scanned_count": 0}
        query, args = eligible_query(signature, None, include_cached=True, history_job_id=history_job_id,
                                    summary_enabled=summary_enabled, summary_host_scope=runtime._session_scope())
        blocked = runtime.db.execute(query + "AND EXISTS(SELECT 1 FROM admin_preflights f WHERE "
                                     "f.project=p.project AND f.problem=p.id AND f.revision=p.revision "
                                     "AND f.signature=? AND f.task_updated IS t.updated) ORDER BY p.created,p.id LIMIT 10",
                                     (project, *args, preflight_signature)).fetchall() if problem_id is None else []
        return {"status": "needs_evidence" if blocked else "idle", "scanned_count": 0,
                **({"problem_id": blocked[0]["id"], "skipped_problem_ids": [r["id"] for r in blocked],
                    "reason": "material needs a complete packet or a larger input budget; no invocation reserved"} if blocked else {})}
    for selected in rows:
        scanned += 1
        summary_context = runtime.work_summary_select(project, selected["id"])
        if summary_context and summary_context.get("eligible") is False:
            if problem_id is not None:
                return {"status": "needs_evidence", "problem_id": selected["id"],
                        "purpose": "work_summary", "reason": summary_context["reason"], "scanned_count": scanned}
            continue
        source_conversation = None if history_job_id is not None else selected["conversation"]
        try:
            provider_plan = runtime.resolve_admin_executor(project, source_conversation, require_enabled=history_job_id is None)
            availability = executor_availability(provider_plan)
        except ValueError as exc:
            routing_skipped.append({"problem_id": selected["id"], "reason": str(exc).split(":", 1)[0]})
            continue
        if not availability["cli_available"]:
            routing_skipped.append({"problem_id": selected["id"], "reason": availability["reason"]})
            continue
        backoff = executor_backoff(runtime, project, provider_plan)
        if backoff:
            routing_skipped.append({"problem_id": selected["id"], "reason": executor_wait_reason(backoff)})
            continue
        review_cfg = _review_configuration(cfg, provider_plan)
        if summary_context:
            review_cfg.update(review_purpose="work_summary", summary_instruction_version=summary_context["instruction_version"])
        packet = (_history_packet(runtime, project, selected["id"], history_job_id) if history_job_id else
                  runtime.work_summary_packet(project, selected["id"], max_chars=cfg["max_input_chars"] - 800) if summary_context else
                  runtime.review_packet(project, selected["id"]))
        if history_job_id:
            review_cfg.update(review_purpose="history_review", history_instruction_version=1)
        if summary_context:
            packet["purpose"] = "work_summary"
        instruction = review_instruction(packet)
        if summary_context:
            from .work_summaries import SUMMARY_PREFIX
            instruction = SUMMARY_PREFIX + instruction
        evidence = _fingerprint(packet)
        review = review_signature(packet, review_cfg, evidence)
        reason = ("packet exceeds configured input limit; no invocation reserved" if packet["incomplete"]
                  else "input exceeds configured limit; no invocation reserved"
                  if len(instruction) > cfg["max_input_chars"] else None)
        if reason:
            # 显式诊断也持久化回执，但不改变自动扫描的位置，便于后续批次绕过阻塞项。
            persist_receipt(runtime, project, packet, evidence, review, None,
                            {"decision": "preflight_blocked", "reason": reason})
            if problem_id is not None:
                return {"status": "needs_evidence", "problem_id": selected["id"], "reason": reason, "scanned_count": scanned}
            runtime.db.execute("INSERT INTO admin_preflights VALUES(?,?,?,?,?,?,?) ON CONFLICT(project,problem) "
                               "DO UPDATE SET revision=excluded.revision,signature=excluded.signature,"
                               "task_updated=excluded.task_updated,reason=excluded.reason,updated=excluded.updated",
                               (project, selected["id"], selected["revision"], preflight_signature,
                                selected["source_task_updated"], reason, time.time()))
            skipped.append(selected["id"])
            continue
        runtime.db.execute("DELETE FROM admin_preflights WHERE project=? AND problem=?", (project, selected["id"]))
        current = receipt_for_problem(runtime, project, selected["id"], selected["revision"], review)
        if current and current["decision"] != "preflight_blocked":
            remember_check(runtime, project, selected, signature, blocked_call=current["duplicate_of"])
            if problem_id is not None:
                response = current["response"]
                return {"status": current["decision"], "reason": current["reason"], "reused": True,
                        "call_id": current["call_id"], "scanned_count": scanned,
                        **{k: response[k] for k in ("candidate_id", "candidate_ids", "candidate_count", "proposal_id", "proposal_ids", "missing_evidence") if k in response}}
            continue
        previous = find_receipt(runtime, project, review)
        if previous:
            call_id = str(uuid.uuid4())
            runtime.db.execute("INSERT INTO admin_calls(id,project,problem,revision,day,state,created,finished,fingerprint,host_epoch) "
                               "VALUES(?,?,?,?,?,'deduplicated',?,?,?,?)",
                               (call_id, project, selected["id"], selected["revision"], time.strftime("%Y-%m-%d", time.gmtime()),
                                time.time(), time.time(), review, epoch))
            result = {"status": "skip", "decision": "skip", "reason": "identical evidence and review context already reviewed; not independent verification",
                      "problem_id": selected["id"], "duplicate_of": previous["call_id"],
                      "previous_decision": previous["decision"], "scanned_count": scanned,
                      "fingerprint": evidence, "review_signature": review,
                      "previous_missing_evidence": previous["response"].get("missing_evidence", [])}
            for key in ("candidate_id", "candidate_ids", "proposal_id", "proposal_ids"):
                if key in previous["response"]:
                    result["previous_" + key] = previous["response"][key]
            persist_receipt(runtime, project, packet, evidence, review, call_id, result, duplicate_of=previous["call_id"])
            remember_check(runtime, project, selected, signature, blocked_call=previous["call_id"])
            return dict(result, **({"skipped_problem_ids": skipped} if skipped else {}))
        # 未知/失败不是可复用决定；等价新案例也需显式核对，不绕过失败再花一次额度。
        unknown = runtime.db.execute("SELECT id,state FROM admin_calls WHERE project=? AND fingerprint=? "
                                     "AND state NOT IN ('not_started','deduplicated') AND retry_allowed=0 "
                                     "AND NOT EXISTS(SELECT 1 FROM review_receipts r WHERE r.project=admin_calls.project "
                                     "AND r.call_id=admin_calls.id) ORDER BY created DESC LIMIT 1", (project, review)).fetchone()
        if unknown:
            remember_check(runtime, project, selected, signature, blocked_call=unknown["id"])
            return {"status": "review_required", "problem_id": selected["id"], "previous_call_id": unknown["id"],
                    "previous_state": unknown["state"], "scanned_count": scanned}
        claim = claim_call(runtime, project, selected["id"], selected["revision"], review, epoch, timeout)
        if claim["status"] != "reserved":
            return dict(claim, scanned_count=scanned)
        runtime.record_admin_execution(project, claim["call_id"], provider_plan, require_enabled=history_job_id is None)
        if summary_context:
            runtime.db.execute("UPDATE work_summary_links SET call_id=?,updated=? WHERE project=? AND problem_id=? AND state='pending'",
                               (claim["call_id"], time.time(), project, selected["id"]))
        if history_job_id is not None:
            runtime.history_reserve_call(project, history_job_id, selected["id"], claim["call_id"], epoch)
        return {"status": "reserved", "packet": packet, "selected": dict(selected), "instruction": instruction,
                "evidence_signature": evidence, "review_signature": review, "scan_signature": signature,
                "scanned_count": scanned, "skipped_problem_ids": skipped, "history_job_id": history_job_id,
                "provider_plan": provider_plan, "summary_context": summary_context, **claim}
    return {"status": "executor_not_ready" if routing_skipped else "needs_evidence" if skipped else "scan_complete", "scanned_count": scanned,
            **({"reason": routing_skipped[0]["reason"], "routing_skipped": routing_skipped[:10]} if routing_skipped else {}),
            **({"problem_id": skipped[0], "skipped_problem_ids": skipped,
                "reason": "bounded scan blocked; no invocation reserved"} if skipped else {})}


def _accept_history_candidates(runtime, project, reserved, bodies, reason):
    """同一调用的候选全部成功或回滚；每条仍有独立来源与审批身份。"""
    from .admin_governance import propose_governance
    from .history_context import link_history_evidence
    packet, job_id = reserved["packet"], reserved["history_job_id"]
    prepared = []
    for body in bodies:
        validate(body, CANDIDATE)
        if (body["problem_id"] != packet["problem"]["id"]
                or body["problem_revision"] != packet["problem"]["revision"]):
            raise ContractError("administrator changed review identity or revision")
        if body["validation_level"] != "reported":
            raise ContractError("historical public text must remain reported")
        if packet.get("evidence_truncated"):
            body = dict(body, limitations=body["limitations"] + "\n" + _TRUNCATION_NOTE)
        prepared.append(body)
    candidate_ids, proposal_ids = [], []
    for body in prepared:
        link_history_evidence(runtime, project, packet, body["evidence_refs"], job_id)
        candidate = runtime.submit_candidate(project, expected_epoch=reserved["host_epoch"], **body)
        proposal = propose_governance(runtime, project, packet["problem"]["id"], packet["problem"]["revision"],
                                      "publish", candidate_id=candidate["candidate_id"], reason=reason,
                                      evidence_refs=body["evidence_refs"], expected_epoch=reserved["host_epoch"])
        if candidate["candidate_id"] not in candidate_ids:
            candidate_ids.append(candidate["candidate_id"])
            proposal_ids.append(proposal["proposal_id"])
    return {"candidate_id": candidate_ids[0], "candidate_ids": candidate_ids,
            "candidate_count": len(candidate_ids), "proposal_id": proposal_ids[0], "proposal_ids": proposal_ids}


def _accept_response(runtime, project, reserved, response):
    from .admin_decisions import persist_receipt, review_signature
    from .admin_governance import propose_governance
    from .admin_lifecycle import validate_call
    from .admin_queue import remember_check
    call_id, packet = reserved["call_id"], reserved["packet"]
    history_job_id = reserved.get("history_job_id")
    allowed = {"decision", "reason", "candidate", "missing_evidence", "proposal"}
    if history_job_id:
        allowed.add("candidates")
    if (not isinstance(response, dict) or not {"decision", "reason", "candidate"} <= set(response)
            or set(response) - allowed or response["decision"] not in {"candidate", "needs_evidence", "skip", "governance"}
            or not isinstance(response["reason"], str)):
        raise ContractError("invalid administrator response")
    missing = response.get("missing_evidence", [])
    if not isinstance(missing, list) or len(missing) > 8 or not all(isinstance(v, str) and len(v) <= 1000 for v in missing):
        raise ContractError("invalid missing evidence requirements")
    candidates = response.get("candidates", [])
    if (not isinstance(candidates, list) or len(candidates) > 5
            or candidates and (not history_job_id or response["decision"] != "candidate"
                               or response["candidate"] is not None or response.get("proposal") is not None)):
        raise ContractError("invalid history candidate batch")
    if history_job_id and len(encode(response).encode("utf-8")) > 40000:
        raise ContractError("history response exceeds total output budget")
    with _review_state_guard():
        validate_call(runtime, call_id, reserved["owner"], reserved["host_epoch"])
        runtime.assert_admin_executor_snapshot(project, reserved["provider_plan"], reserved["provider_plan"].get("conversation"),
                                               require_enabled=reserved.get("history_job_id") is None)
        if reserved.get("history_job_id"):
            runtime.history_guard(project, packet["problem"]["id"], reserved["history_job_id"],
                                  require_review=True, expected_epoch=reserved["host_epoch"])
        summary_context = runtime.work_summary_guard(project, packet["problem"]["id"])
        if bool(summary_context) != bool(reserved.get("summary_context")):
            raise ContractError("administrator review purpose changed")
        if summary_context and summary_context["source_digest"] != reserved["summary_context"]["source_digest"]:
            raise ContractError("work summary source identity changed")
        revision = runtime.db.execute("SELECT revision FROM problems WHERE project=? AND id=?",
                                      (project, packet["problem"]["id"])).fetchone()
        if not revision or revision[0] != packet["problem"]["revision"]:
            raise ContractError("reviewed problem changed")
        current_packet = (_history_packet(runtime, project, packet["problem"]["id"], history_job_id) if history_job_id else
                          runtime.work_summary_packet(project, packet["problem"]["id"],
                              max_chars=runtime.admin_effective_config(project)["max_input_chars"] - 800) if summary_context else
                          runtime.review_packet(project, packet["problem"]["id"]))
        if summary_context:
            current_packet["purpose"] = "work_summary"
        review_cfg = _review_configuration(runtime.admin_effective_config(project), reserved["provider_plan"])
        if history_job_id:
            review_cfg.update(review_purpose="history_review", history_instruction_version=1)
        if summary_context:
            review_cfg.update(review_purpose="work_summary", summary_instruction_version=summary_context["instruction_version"])
        if review_signature(current_packet, review_cfg, _fingerprint(current_packet)) != reserved["review_signature"]:
            raise ContractError("review evidence or comparison context changed")
    result = {"status": response["decision"], "reason": clean(response["reason"])[:1000], "call_id": call_id,
              "problem_id": packet["problem"]["id"], "missing_evidence": missing}
    if summary_context:
        result["purpose"] = "work_summary"
    body, proposal = response["candidate"], response.get("proposal")
    if summary_context and (proposal is not None or response["decision"] == "governance"):
        raise ContractError("work summary cannot propose knowledge governance")
    if candidates:
        result.update(_accept_history_candidates(runtime, project, reserved, candidates, result["reason"]))
    elif response["decision"] == "candidate":
        validate(body, CANDIDATE)
        if body["problem_id"] != packet["problem"]["id"] or body["problem_revision"] != packet["problem"]["revision"]:
            raise ContractError("administrator changed review identity or revision")
        if history_job_id:
            from .history_context import link_history_evidence
            if body["validation_level"] != "reported":
                raise ContractError("historical public text must remain reported")
            link_history_evidence(runtime, project, packet, body["evidence_refs"], history_job_id)
        elif set(body["evidence_refs"]) - {e["id"] for e in packet["events"]}:
            raise ContractError("candidate references evidence outside the review packet")
        if packet.get("evidence_truncated"):
            body = dict(body, limitations=body["limitations"] + "\n" + _TRUNCATION_NOTE)
        if summary_context:
            result.update(runtime.work_summary_accept(project, packet["problem"]["id"], body, call_id))
        else:
            result.update(runtime.submit_candidate(project, expected_epoch=reserved["host_epoch"], **body))
            proposal = proposal or {"action": "publish", "knowledge_id": None, "expected_revision": None}
    elif body is not None:
        raise ContractError("non-candidate response must use candidate=null")
    if summary_context and response["decision"] == "skip":
        result.update(runtime.work_summary_skip(project, packet["problem"]["id"], call_id))
    elif summary_context and response["decision"] == "needs_evidence":
        result.update(runtime.work_summary_needs_evidence(project, packet["problem"]["id"], call_id))
    if proposal is not None:
        if (not isinstance(proposal, dict) or set(proposal) != {"action", "knowledge_id", "expected_revision"}
                or proposal["action"] not in {"publish", "update", "attach", "revoke", "dismiss_counterexample"}):
            raise ContractError("invalid administrator governance proposal")
        candidate_action = proposal["action"] in {"publish", "update", "attach"}
        if candidate_action != (response["decision"] == "candidate") or (not candidate_action and response["decision"] != "governance"):
            raise ContractError("governance action does not match decision")
        if proposal["action"] != "publish":
            visible = packet.get("related_knowledge", []) + ([packet["target_knowledge"]] if packet.get("target_knowledge") else [])
            if not any(k.get("knowledge_id", k.get("id")) == proposal["knowledge_id"]
                       and k.get("revision") == proposal["expected_revision"] for k in visible):
                raise ContractError("governance target is outside the visible review context")
        refs = body["evidence_refs"] if body else [e["id"] for e in packet["events"] if e["health"] == "valid"]
        result.update(propose_governance(runtime, project, packet["problem"]["id"], packet["problem"]["revision"],
                                        proposal["action"], candidate_id=result.get("candidate_id"),
                                        knowledge_id=proposal["knowledge_id"], expected_revision=proposal["expected_revision"],
                                        reason=result["reason"], evidence_refs=refs, expected_epoch=reserved["host_epoch"]))
    elif response["decision"] == "governance":
        raise ContractError("governance decision requires a proposal")
    if history_job_id and response["decision"] == "candidate" and not candidates:
        result.update(candidate_ids=[result["candidate_id"]], candidate_count=1,
                      proposal_ids=[result["proposal_id"]] if result.get("proposal_id") else [])
    if (response["decision"] == "candidate" and runtime.admin_effective_config(project)["auto_publish"]
            and not reserved.get("history_job_id") and not summary_context):
        prob_id = packet["problem"]["id"]
        bound_kid = packet["problem"].get("knowledge_id")
        target_kid = proposal.get("knowledge_id")
        same_lineage_update = False
        target_operation_solution = False
        if proposal["action"] in {"update", "attach"} and target_kid:
            target_row = runtime.db.execute(
                "SELECT k.body FROM active a JOIN knowledge k ON k.project=a.project AND k.id=a.id "
                "AND k.revision=a.revision WHERE a.project=? AND a.id=? AND a.revision=?",
                (project, target_kid, proposal["expected_revision"]),
            ).fetchone()
            target_body = json.loads(target_row[0]) if target_row else {}
            target_operation_solution = (target_body.get("category") == "operation"
                                         and target_body.get("knowledge_kind", "solution") == "solution")
            has_unresolved_counterexample = runtime.db.execute(
                "SELECT 1 FROM knowledge_feedback WHERE project=? AND knowledge_id=? AND disposition='unresolved' LIMIT 1",
                (project, target_kid),
            ).fetchone()
            if not has_unresolved_counterexample:
                # 1) 同源案例多轮演进升级 (v1 -> v2)
                same_problem_source = (
                    runtime.db.execute(
                        "SELECT 1 FROM knowledge_sources WHERE project=? AND knowledge_id=? AND problem_id=? LIMIT 1",
                        (project, target_kid, prob_id),
                    ).fetchone() is not None
                )
                # 2) 日后跨会话修改同一目标源码文件 (write_target) 的迭代升级
                has_file_links = runtime.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_file_links'"
                ).fetchone()
                same_file_target = bool(
                    not same_problem_source
                    and has_file_links
                    and runtime.db.execute(
                        "SELECT 1 FROM problem_events pe "
                        "JOIN work_file_links wfl ON wfl.project=? AND wfl.event_id=pe.event "
                        "JOIN work_file_links kwfl ON kwfl.project=wfl.project AND kwfl.checkout_key=wfl.checkout_key AND kwfl.path_key=wfl.path_key "
                        "JOIN problem_events kpe ON kpe.event=kwfl.event_id "
                        "JOIN knowledge_sources ks ON ks.project=kwfl.project AND ks.problem_id=kpe.problem "
                        "WHERE pe.problem=? AND ks.knowledge_id=? AND wfl.relation='write_target' AND kwfl.relation='write_target' LIMIT 1",
                        (project, prob_id, target_kid),
                    ).fetchone()
                )
                same_lineage_update = bool(same_problem_source or same_file_target)
        # 振荡回退边界检查：若本案例在最后一次成功工具验证之后又发生了新的失败/冲突事件，禁止抢发半成品
        latest_fail_seq = runtime.db.execute(
            "SELECT COALESCE(MAX(e.rowid),0) FROM problem_events pe JOIN events e ON e.id=pe.event "
            "WHERE pe.problem=? AND e.project=? AND (pe.role IN ('failure','conflict') OR e.outcome IN ('failed','conflict'))",
            (prob_id, project),
        ).fetchone()[0]
        latest_succ_seq = runtime.db.execute(
            "SELECT COALESCE(MAX(e.rowid),0) FROM problem_events pe JOIN events e ON e.id=pe.event "
            "LEFT JOIN operations o ON o.project=e.project AND o.id=e.operation "
            "WHERE pe.problem=? AND e.project=? AND e.type='PostToolUse' AND e.outcome='succeeded' "
            "AND COALESCE(o.conflict,0)=0 AND COALESCE(o.decision,'')!='denied'",
            (prob_id, project),
        ).fetchone()[0]
        # The accepted candidate must cite the recovery evidence itself.  A newer
        # success elsewhere in the packet cannot validate a stale quoted success.
        cited_success_seq = 0
        for ref in body["evidence_refs"]:
            observed = runtime.db.execute(
                "SELECT e.rowid FROM problem_events pe JOIN events e ON e.id=pe.event "
                "LEFT JOIN operations o ON o.project=e.project AND o.id=e.operation "
                "WHERE pe.problem=? AND e.project=? AND e.id=? AND e.type='PostToolUse' "
                "AND e.outcome='succeeded' AND COALESCE(o.conflict,0)=0 AND COALESCE(o.decision,'')!='denied'",
                (prob_id, project, ref),
            ).fetchone()
            if observed:
                cited_success_seq = max(cited_success_seq, observed[0])
        latest_write_seq = runtime.db.execute(
            "SELECT COALESCE(MAX(e.rowid),0) FROM problem_events pe JOIN events e ON e.id=pe.event "
            "WHERE pe.problem=? AND e.project=? AND e.type='PostToolUse' "
            "AND EXISTS(SELECT 1 FROM work_file_links wfl WHERE wfl.project=e.project "
            "AND wfl.event_id=e.id AND wfl.relation='write_target')",
            (prob_id, project),
        ).fetchone()[0]
        regressed = bool(latest_fail_seq > latest_succ_seq)
        stale_citation = (not cited_success_seq or cited_success_seq <= latest_fail_seq
                          or cited_success_seq < latest_write_seq)
        reason = ("evidence truncated" if packet.get("evidence_truncated") or packet.get("incomplete") else
                  "requires trusted tool-success evidence" if body["validation_level"] != "tool_success" else
                  "latest observation is unresolved failure" if regressed else
                  "requires current cited tool-success evidence" if stale_citation else
                  "requires operation solution" if body["category"] != "operation" or body.get("knowledge_kind", "solution") != "solution" else
                  "target knowledge requires trusted governance" if target_kid and not target_operation_solution else
                  "existing knowledge requires trusted governance" if not (
                      (proposal["action"] == "publish" and not bound_kid) or same_lineage_update
                  ) else None)
        if reason:
            result["publication"] = {"status": "not_published", "reason": reason}
        else:
            from .admin_governance import execute_governance
            result["publication"] = execute_governance(runtime, project, result["proposal_id"],
                                                       expected_epoch=reserved["host_epoch"])["publication"]
    result["status"] = response["decision"]
    persist_receipt(runtime, project, packet, reserved["evidence_signature"], reserved["review_signature"],
                    call_id, dict(response, **result, decision=response["decision"]))
    if summary_context and response["decision"] == "candidate":
        runtime.db.execute("UPDATE review_states SET state='work_summary_ready' WHERE project=? AND problem=? AND revision=?",
                           (project, packet["problem"]["id"], packet["problem"]["revision"]))
    from .admin_queue import scan_signature
    remember_check(runtime, project, reserved["selected"], scan_signature(runtime, project))
    with _review_state_guard():
        # 取包和写决定可能跨过租约截止点，最终提交前再校验一次。
        validate_call(runtime, call_id, reserved["owner"], reserved["host_epoch"])
        runtime.assert_admin_executor_snapshot(project, reserved["provider_plan"], reserved["provider_plan"].get("conversation"),
                                               require_enabled=reserved.get("history_job_id") is None)
        if reserved.get("history_job_id"):
            runtime.history_guard(project, packet["problem"]["id"], reserved["history_job_id"],
                                  require_review=True, expected_epoch=reserved["host_epoch"])
        if summary_context:
            runtime.work_summary_guard(project, packet["problem"]["id"],
                                       accepted_call_id=call_id if response["decision"] in {"candidate", "skip", "needs_evidence"} else None)
    return result


def admin_once(runtime, project, problem_id=None, *, expected_epoch=None, _deadline=None, _scan_limit=10, _history_job_id=None):
    from .admin_lifecycle import mark_launch_intent, mark_started
    from .admin_providers import build_execution, collect_antigravity_stream, read_response, resolve_command, launch_error, executor_capability
    from .admin_queue import record_executor_failure, clear_executor_failure, record_dispatch_diagnostic
    epoch = runtime.assert_host_epoch(expected_epoch)
    cfg = runtime.admin_effective_config(project)
    if not cfg["enabled"] and _history_job_id is None:
        return {"status": "disabled", "scanned_count": 0}
    if _history_job_id is not None:
        runtime.history_guard(project, problem_id, _history_job_id, require_review=True, expected_epoch=epoch)
    _, timeout = _settings(cfg)
    timeout = min(timeout, cfg.get("max_run_seconds", 600))
    if cfg["max_calls_per_day"] == 0:
        return {"status": "budget_exhausted", "scanned_count": 0}
    if _deadline is not None:
        remaining = int(_deadline - time.monotonic())
        if remaining < 1:
            return {"status": "batch_time_exhausted", "scanned_count": 0}
        timeout = min(timeout, remaining)
    with runtime.transaction():
        reserved = _select_review(runtime, project, problem_id, _scan_limit, timeout, epoch, _history_job_id)
    if reserved["status"] != "reserved":
        return reserved
    call_id, process, started_at = reserved["call_id"], None, time.monotonic()
    provider_plan = reserved["provider_plan"]
    context = {"scanned_count": reserved["scanned_count"],
               **({"skipped_problem_ids": reserved["skipped_problem_ids"]} if reserved["skipped_problem_ids"] else {})}
    log_path, temporary, output_thread, output_state = None, None, None, None
    execution_state = {"phase": "capability", "input_submission_attempted": False,
                       "terminal_observed": False}
    try:
        capability = executor_capability(provider_plan)
        if not capability["ready"]:
            record_executor_failure(runtime, project, provider_plan, capability["reason"])
            usage = _finish(runtime, call_id, "not_started", None, started_at, provider_plan)
            return {"status": "not_started", "reason": capability["reason"], "call_id": call_id, "usage": usage, **context}
        execution_state["phase"] = "build_execution"
        temporary = tempfile.TemporaryDirectory(prefix="agent-learning-review-")
        temp = temporary.name
        directory = Path(temp)
        schema_path, output, log_path = directory / "response.schema.json", directory / "result.json", directory / "events.log"
        schema_path.write_text(encode(HISTORY_RESPONSE_SCHEMA if _history_job_id else RESPONSE_SCHEMA), encoding="utf-8")
        execution = build_execution(provider_plan["command"], provider_plan["provider"], provider_plan.get("model", ""),
                                    directory, schema_path, output, reserved["instruction"], timeout,
                                    selection_mode=provider_plan.get("model_mode", "default"),
                                    dsh_profile=provider_plan.get("dsh_profile"))
        if execution.get("command_resolved"):
            args = execution["args"]
        else:
            try:
                resolved = resolve_command(provider_plan["command"])
            except (OSError, ValueError) as exc:
                detail = launch_error(exc)
                record_executor_failure(runtime, project, provider_plan, detail["reason"], detail)
                usage = _finish(runtime, call_id, "not_started", None, started_at, provider_plan)
                return {"status": "not_started", "call_id": call_id, "usage": usage, **detail, **context}
            args = resolved["args"] + execution["args"][len(provider_plan["command"]):]
        env = dict(os.environ, ALP_DISABLED="1", ALP_HOME=str(runtime.home))
        if provider_plan.get("provider") != "dsh" or provider_plan.get("dsh_profile") is None:
            # A DSH host configuration can contain provider authentication. It
            # belongs only to our restricted SDK runner, never another launcher.
            env.pop("ALP_DSH_REVIEW_PROFILE", None)
        if _deadline is not None:
            remaining = int(_deadline - time.monotonic())
            if remaining < 1:
                usage = _finish(runtime, call_id, "not_started", None, started_at, provider_plan)
                return {"status": "batch_time_exhausted", "call_id": call_id, "usage": usage, **context}
            timeout = min(timeout, remaining)
        with log_path.open("wb") as log:
            with runtime.transaction():
                with _review_state_guard():
                    runtime.assert_admin_executor_snapshot(project, provider_plan, provider_plan.get("conversation"),
                                                           require_enabled=_history_job_id is None)
                    if reserved.get("summary_context"):
                        runtime.work_summary_guard(project, reserved["packet"]["problem"]["id"])
                    if _history_job_id is None and not runtime.admin_effective_config(project)["enabled"]:
                        usage = _finish(runtime, call_id, "not_started", log_path, started_at, provider_plan)
                        return {"status": "disabled", "call_id": call_id, "usage": usage, **context}
                    mark_launch_intent(runtime, call_id, reserved["owner"], reserved["host_epoch"])
            if _history_job_id is not None:
                with runtime.transaction():
                    with _review_state_guard():
                        runtime.history_guard(project, problem_id, _history_job_id, require_review=True, expected_epoch=epoch)
            try:
                stream_mode = provider_plan["provider"] in {"antigravity", "dsh"}
                process = subprocess.Popen(args, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE if stream_mode else log,
                                           stderr=subprocess.DEVNULL if stream_mode else log, env=env, cwd=directory,
                                           **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            except OSError as exc:
                # Popen明确失败才释放未启动额度；崩溃窗口由持久intent按未知恢复。
                usage = _finish(runtime, call_id, "not_started", log_path, started_at, provider_plan)
                detail = launch_error(exc)
                record_executor_failure(runtime, project, provider_plan, detail["reason"], detail)
                return {"status": "not_started", "call_id": call_id, "usage": usage, **detail, **context}
            with _review_state_guard():
                mark_started(runtime, call_id, reserved["owner"], getattr(process, "pid", None))
            record_dispatch_diagnostic(runtime, project, "model_started")
            execution_state["phase"] = "initialization" if stream_mode else "execution"
            if stream_mode:
                stdout_stream, process.stdout = process.stdout, None
                output_thread, output_state = collect_antigravity_stream(stdout_stream, log_path, process.kill,
                                                                         provider_plan.get("model", ""))
            try:
                process_deadline = time.monotonic() + timeout
                if output_thread:
                    if not output_state["initialized"].wait(timeout=timeout):
                        raise subprocess.TimeoutExpired(args, timeout)
                    if not output_state["init_safe"] or output_state["error"]:
                        raise ContractError("administrator did not confirm a tool-free initialization")
                    output_state["mark_input_sent"]()
                execution_state.update(phase="execution", input_submission_attempted=True)
                process.communicate(execution["stdin_data"], timeout=max(0.001, process_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                if output_thread:
                    output_thread.join(timeout=5)
                execution_state["reason_code"] = "executor_timeout"
                if output_state is not None:
                    execution_state["terminal_observed"] = bool(output_state.get("terminal"))
                record_executor_failure(runtime, project, provider_plan, "executor_timeout", execution_state)
                usage = _finish(runtime, call_id, "timeout_unknown", log_path, started_at, provider_plan,
                                execution=execution_state)
                return {"status": runtime.db.execute("SELECT state FROM admin_calls WHERE id=?", (call_id,)).fetchone()[0],
                        "call_id": call_id, "usage": usage, **context}
            if output_thread:
                execution_state["phase"] = "output_collection"
                output_thread.join(timeout=5)
                execution_state["terminal_observed"] = bool(output_state.get("terminal"))
                if output_thread.is_alive() or output_state["error"]:
                    raise ContractError("administrator output could not be collected completely")
        execution_state["phase"] = "process_exit"
        if isinstance(process.returncode, int) and not isinstance(process.returncode, bool):
            execution_state["returncode"] = process.returncode
        if process.returncode != 0:
            raise ContractError("administrator produced no usable response")
        execution_state["phase"] = "response_validation"
        response = read_response(provider_plan["provider"], output, log_path)
        execution_state.update(phase="decision_commit", terminal_observed=True)
        with runtime.transaction():
            result = _accept_response(runtime, project, reserved, response)
            result["usage"] = _finish(runtime, call_id, response["decision"], log_path, started_at, provider_plan,
                                      execution=execution_state)
            actual = runtime.db.execute("SELECT state FROM admin_calls WHERE id=?", (call_id,)).fetchone()[0]
            if actual != response["decision"]:
                # 同一事务回滚候选、治理提议、回执和错误终态；异常路径补真实未知结果。
                raise ContractError("administrator decision could not commit with its call state")
            # A spawned process is not a healthy executor. Reset accumulated
            # failures only with the committed, validated administrator decision.
            clear_executor_failure(runtime, project, provider_plan)
        return dict(result, **context)
    except (OSError, ValueError, TypeError, ContractError) as exc:
        execution_state["reason_code"] = {
            "capability": "executor_capability_failed", "build_execution": "executor_preparation_failed",
            "initialization": "executor_initialization_failed", "execution": "executor_execution_failed",
            "output_collection": "executor_output_failed", "process_exit": "executor_nonzero_exit",
            "response_validation": "executor_response_invalid", "decision_commit": "executor_decision_uncommitted",
        }[execution_state["phase"]]
        execution_state["error_type"] = type(exc).__name__
        if output_state is not None:
            execution_state["terminal_observed"] = bool(output_state.get("terminal"))
        if process is not None and isinstance(process.returncode, int) and not isinstance(process.returncode, bool):
            execution_state["returncode"] = process.returncode
        if isinstance(exc, ReviewStateChanged):
            execution_state["reason_code"] = "review_context_changed"
        else:
            from .admin_diagnostics import safe_diagnostic, configuration_failure
            diagnostic = safe_diagnostic(output_state.get("diagnostic")) if output_state is not None else None
            failure_reason = execution_state["reason_code"]
            if diagnostic:
                execution_state["provider_diagnostic"] = diagnostic
                execution_state["reason_code"] = diagnostic["reason_code"]
                failure_reason = ("configuration_change_required" if configuration_failure(diagnostic)
                                  else diagnostic["reason_code"])
            record_executor_failure(runtime, project, provider_plan, failure_reason, execution_state)
        usage = _finish(runtime, call_id, "not_started" if process is None else "failed", log_path, started_at, provider_plan,
                        execution=execution_state)
        state = runtime.db.execute("SELECT state FROM admin_calls WHERE id=?", (call_id,)).fetchone()[0]
        return {"status": state, "error_type": type(exc).__name__, "call_id": call_id, "usage": usage, **context}
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if output_thread:
            output_thread.join(timeout=5)
        stdin_stream = getattr(process, "stdin", None) if process is not None else None
        if stdin_stream is not None:
            try:
                stdin_stream.close()
            except OSError:
                pass
        stdout_stream = getattr(process, "stdout", None) if process is not None else None
        if stdout_stream is not None:
            try:
                stdout_stream.close()
            except OSError:
                pass
        if temporary is not None:
            temporary.cleanup()
        if _history_job_id is not None:
            runtime.history_refund_call(_history_job_id, call_id)


_ADMIN_STOP_STATES = {
    "disabled", "idle", "budget_exhausted", "token_budget_exhausted", "concurrency_limited",
    "batch_time_exhausted", "waiting_for_review", "not_started", "executor_not_ready",
    "failed", "timeout_unknown", "account_changed_unknown", "orphaned_unknown", "review_required",
}


def admin_batch(runtime, project, *, dispatch_token=None, expected_epoch=None, _deadline=None,
                _release_dispatch=True):
    from .admin_queue import release_dispatch, claim_dispatch_worker
    cfg = runtime.admin_effective_config(project)
    results, scanned, calls = [], 0, 0
    started = time.monotonic()
    owned_dispatch = False
    try:
        epoch = runtime.assert_host_epoch(expected_epoch)
        if not cfg["enabled"]:
            return {"status": "disabled", "items": []}
        _settings(cfg)
        if dispatch_token:
            if _release_dispatch:
                if not claim_dispatch_worker(runtime, project, dispatch_token, cfg["max_batch_seconds"]):
                    # An expired unclaimed legacy batch can be cleaned up. A
                    # live or already claimed worker belongs to its real owner.
                    runtime.db.execute("DELETE FROM admin_dispatch WHERE project=? AND token=? "
                        "AND phase='reserved' AND lease_until<=?", (project, dispatch_token, time.time()))
                    return {"status": "dispatch_expired", "items": []}
                owned_dispatch = True
            row = runtime.db.execute("SELECT * FROM admin_dispatch WHERE project=? AND token=?", (project, dispatch_token)).fetchone()
            if not row or row["host_epoch"] != epoch or row["lease_until"] <= time.time():
                return {"status": "dispatch_expired", "items": []}
        deadline = started + min(cfg["max_batch_seconds"], cfg.get("max_run_seconds", 600))
        if _deadline is not None:
            if not isinstance(_deadline, (int, float)) or isinstance(_deadline, bool):
                raise ContractError("invalid administrator batch deadline")
            import math
            if not math.isfinite(_deadline):
                raise ContractError("invalid administrator batch deadline")
            deadline = min(deadline, _deadline)
        while True:
            runtime.assert_host_epoch(epoch)
            cfg = runtime.admin_effective_config(project)
            _settings(cfg)
            if not cfg["enabled"]:
                results.append({"status": "disabled", "scanned_count": 0})
                break
            deadline = min(deadline, started + cfg["max_batch_seconds"], started + cfg.get("max_run_seconds", 600))
            if scanned >= cfg["max_cases_per_batch"] or calls >= cfg["max_calls_per_batch"]:
                break
            result = admin_once(runtime, project, expected_epoch=epoch, _deadline=deadline,
                                _scan_limit=min(10, cfg["max_cases_per_batch"] - scanned))
            results.append(result)
            scanned += result.get("scanned_count", 0)
            calls += int("call_id" in result and "usage" in result
                         and result["status"] not in {"not_started", "batch_time_exhausted"})
            if result["status"] in _ADMIN_STOP_STATES:
                break
            if result.get("scanned_count", 0) == 0:
                break
        return {"status": "batch_complete", "items": results, "scanned_count": scanned, "invocations": calls,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "stop_reason": results[-1]["status"] if results and results[-1]["status"] in _ADMIN_STOP_STATES else
                               "case_limit" if scanned >= cfg["max_cases_per_batch"] else
                               "call_limit" if calls >= cfg["max_calls_per_batch"] else results[-1]["status"] if results else "idle"}
    finally:
        if owned_dispatch:
            release_dispatch(runtime, project, dispatch_token)


def admin_drain(runtime, project, *, dispatch_token=None, expected_epoch=None, max_seconds=None):
    """Drain authorized daily work in small batches within one finite dispatch lease."""
    import math
    from .admin_queue import claim_dispatch, release_dispatch, claim_dispatch_worker, dispatch_diagnostic
    from .admin_usage import admin_admission_status

    started = time.monotonic()
    token = dispatch_token
    owned_dispatch = False
    summaries, calls, scanned, batches = [], 0, 0, 0
    stop_reason = "idle"
    try:
        epoch = runtime.assert_host_epoch(expected_epoch)
        cfg = runtime.admin_effective_config(project)
        _settings(cfg)
        if max_seconds is not None and (isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
                                        or not math.isfinite(max_seconds) or not 0 < max_seconds <= 3600):
            raise ContractError("administrator drain allowance must be finite and at most 3600 seconds")
        seconds = min(cfg.get("max_run_seconds", 600), max_seconds if max_seconds is not None else 3600)
        if not cfg["enabled"]:
            return {"status": "disabled", "batches": 0, "invocations": 0, "items": []}
        if token is None:
            token = claim_dispatch(runtime, project, expected_epoch=epoch)
            if token is None:
                budget = admin_admission_status(runtime, project)
                diagnostic = dispatch_diagnostic(runtime, project)
                return {"status": budget["admission_status"] if budget["admission_status"] != "available" else "idle", "reason": diagnostic["reason"],
                        "batches": 0, "invocations": 0, "items": []}
        deadline = started + seconds
        if not claim_dispatch_worker(runtime, project, token, seconds):
            # Do not let a second worker remove the first worker's lease.
            token = None
            return {"status": "dispatch_expired", "batches": 0, "invocations": 0, "items": []}
        owned_dispatch = True
        while time.monotonic() < deadline:
            runtime.assert_host_epoch(epoch)
            cfg = runtime.admin_effective_config(project)
            _settings(cfg)
            if not cfg["enabled"]:
                stop_reason = "disabled"
                break
            deadline = min(deadline, started + cfg.get("max_run_seconds", 600))
            if time.monotonic() >= deadline:
                stop_reason = "run_time_exhausted"
                break
            batch = admin_batch(runtime, project, dispatch_token=token, expected_epoch=epoch,
                                _deadline=deadline, _release_dispatch=False)
            batches += 1
            calls += batch.get("invocations", 0)
            scanned += batch.get("scanned_count", 0)
            stop_reason = batch.get("stop_reason", batch["status"])
            summaries.append({key: batch[key] for key in
                              ("status", "stop_reason", "invocations", "scanned_count", "elapsed_seconds") if key in batch})
            summaries = summaries[-20:]
            # Continue only after a successful bounded batch with actual progress.
            # Unknown results, failed calls, missing evidence and empty scans stop here.
            if (batch["status"] != "batch_complete" or stop_reason not in {"call_limit", "case_limit"}
                    or not batch.get("scanned_count", 0)):
                break
        else:
            stop_reason = "run_time_exhausted"
        return {"status": "drain_complete", "batches": batches, "invocations": calls, "scanned_count": scanned,
                "elapsed_seconds": round(time.monotonic() - started, 3), "stop_reason": stop_reason,
                "items": summaries, "items_truncated": batches > len(summaries)}
    finally:
        if owned_dispatch:
            release_dispatch(runtime, project, token)
