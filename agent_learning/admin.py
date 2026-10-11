"""一次有限的管理员审核。快照与记账先落盘，模型调用在事务外进行。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from .mcp import CANDIDATE, validate
from .runtime import ContractError, clean, digest, encode
from .review_packet import PACKET_FORMAT, REVIEW_PREFIX as _REVIEW_PREFIX, review_instruction


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

_TOKEN_FIELDS = {"input_tokens", "output_tokens", "cached_input_tokens", "total_tokens"}
_PROTOCOL_FIELDS = {"turn_id", "tool_use_id", "event_id", "conversation_id", "agent_id",
                    "session_id", "sessionId", "timestamp", "created", "updated"}
_TRUNCATION_NOTE = "原始证据已投影或截短；结论仅限可见记录，未核对完整原文。"
def _fingerprint(packet):
    # 去掉传输身份而非业务参数；业务对象中的 id/version 等字段仍参与比较。
    events, seen, actors, assignments = [], set(), {}, {}
    for event in packet["events"]:
        payload = clean(event.get("payload", {}))
        if isinstance(payload, dict):
            payload = {key: value for key, value in payload.items()
                       if key not in _PROTOCOL_FIELDS and key != "agent_identity_known"}
            # tool_input/tool_response 里的 ID 或时间可能是业务条件，不递归删除。
            if isinstance(payload.get("metadata"), dict):
                payload["metadata"] = {key: value for key, value in payload["metadata"].items() if value}
                if not payload["metadata"]:
                    payload.pop("metadata")
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
    return digest({"summary": clean(problem.get("summary", "")),
                   "category": problem.get("category", "unknown"),
                   "goal": clean(task.get("goal", "")), "scope": clean(task.get("scope", {})),
                   "events": events, "evidence_truncated": bool(packet.get("evidence_truncated")),
                   "incomplete": bool(packet.get("incomplete")),
                   "feedback": {key: problem.get(key) for key in
                                ("knowledge_id", "knowledge_revision", "resolution_status")}})


def _usage(log_path, elapsed):
    """有界流式累计全部可读报告；预览裁剪不丢累计，缺失不写成零。"""
    records, totals, seen = [], {}, {}
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
                if not isinstance(event, dict) or event.get("type") != "turn.completed":
                    continue
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
                identity = event.get("turn_id", event.get("id"))
                if isinstance(identity, str) and identity:
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
    return {"elapsed_seconds": round(elapsed, 3), "token_usage": records, "token_totals": totals,
            "reported_turns": count, "preview_truncated": count > 16,
            "completeness": "complete" if count and not truncated and not incomplete else "partial" if totals else "unknown",
            "log_tail_truncated": truncated}


def _finish(runtime, call_id, state, log_path, started_at):
    from .admin_lifecycle import finish_call
    usage = _usage(log_path, time.monotonic() - started_at)
    row = finish_call(runtime, call_id, state, usage=usage)
    return json.loads(row["usage"])


def _settings(cfg):
    command = cfg["command"]
    if (not isinstance(command, list) or not command
            or not all(isinstance(value, str) and value for value in command)):
        raise ContractError("admin.command must be an executable argument array")
    ranges = {"timeout_seconds": (1, 600), "max_calls_per_day": (0, 100000),
              "max_input_chars": (1000, 200000), "max_cases_per_batch": (1, 100),
              "max_calls_per_batch": (1, 20), "max_batch_seconds": (1, 600)}
    for key, (low, high) in ranges.items():
        value = cfg[key]
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ContractError(f"invalid admin.{key}")
    from .admin_usage import budget_settings
    class Settings:
        config = {"admin": cfg}
    budget_settings(Settings())
    return list(command), cfg["timeout_seconds"]


def _select_review(runtime, project, problem_id, scan_limit, timeout):
    from .admin_decisions import find_receipt, persist_receipt, receipt_for_problem, review_signature
    from .admin_lifecycle import claim_call, recover_calls
    from .admin_queue import eligible_query, remember_check, scan_signature
    cfg = runtime.config["admin"]
    recover_calls(runtime, project)
    signature = scan_signature(runtime, project)
    preflight_signature = scan_signature(runtime, project, include_candidates=False)
    query, args = eligible_query(signature, problem_id, preflight_signature=preflight_signature)
    rows = runtime.db.execute(query + "ORDER BY p.created,p.id LIMIT ?",
                              (project, *args, 1 if problem_id is not None else scan_limit)).fetchall()
    skipped, scanned = [], 0
    if not rows:
        query, args = eligible_query(signature, None, include_cached=True)
        blocked = runtime.db.execute(query + "AND EXISTS(SELECT 1 FROM admin_preflights f WHERE "
                                     "f.project=p.project AND f.problem=p.id AND f.revision=p.revision "
                                     "AND f.signature=? AND f.task_updated IS t.updated) ORDER BY p.created,p.id LIMIT 10",
                                     (project, *args, preflight_signature)).fetchall() if problem_id is None else []
        return {"status": "needs_evidence" if blocked else "idle", "scanned_count": 0,
                **({"problem_id": blocked[0]["id"], "skipped_problem_ids": [r["id"] for r in blocked],
                    "reason": "material needs a complete packet or a larger input budget; no invocation reserved"} if blocked else {})}
    for selected in rows:
        scanned += 1
        packet = runtime.review_packet(project, selected["id"])
        instruction = review_instruction(packet)
        evidence = _fingerprint(packet)
        review = review_signature(packet, cfg, evidence)
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
                        **{k: response[k] for k in ("candidate_id", "proposal_id", "missing_evidence") if k in response}}
            continue
        previous = find_receipt(runtime, project, review)
        if previous:
            call_id = str(uuid.uuid4())
            runtime.db.execute("INSERT INTO admin_calls(id,project,problem,revision,day,state,created,finished,fingerprint,host_epoch) "
                               "VALUES(?,?,?,?,?,'deduplicated',?,?,?,?)",
                               (call_id, project, selected["id"], selected["revision"], time.strftime("%Y-%m-%d", time.gmtime()),
                                time.time(), time.time(), review, runtime.host_epoch()))
            result = {"status": "skip", "decision": "skip", "reason": "identical evidence and review context already reviewed; not independent verification",
                      "problem_id": selected["id"], "duplicate_of": previous["call_id"],
                      "previous_decision": previous["decision"], "scanned_count": scanned,
                      "fingerprint": evidence, "review_signature": review,
                      "previous_missing_evidence": previous["response"].get("missing_evidence", [])}
            for key in ("candidate_id", "proposal_id"):
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
        claim = claim_call(runtime, project, selected["id"], selected["revision"], review, runtime.host_epoch(), timeout)
        if claim["status"] != "reserved":
            return dict(claim, scanned_count=scanned)
        return {"status": "reserved", "packet": packet, "selected": dict(selected), "instruction": instruction,
                "evidence_signature": evidence, "review_signature": review, "scan_signature": signature,
                "scanned_count": scanned, "skipped_problem_ids": skipped, **claim}
    return {"status": "needs_evidence" if skipped else "scan_complete", "scanned_count": scanned,
            **({"problem_id": skipped[0], "skipped_problem_ids": skipped,
                "reason": "bounded scan blocked; no invocation reserved"} if skipped else {})}


def _accept_response(runtime, project, reserved, response):
    from .admin_decisions import persist_receipt, review_signature
    from .admin_governance import propose_governance
    from .admin_lifecycle import validate_call
    from .admin_queue import remember_check
    call_id, packet = reserved["call_id"], reserved["packet"]
    allowed = {"decision", "reason", "candidate", "missing_evidence", "proposal"}
    if (not isinstance(response, dict) or not {"decision", "reason", "candidate"} <= set(response)
            or set(response) - allowed or response["decision"] not in {"candidate", "needs_evidence", "skip", "governance"}
            or not isinstance(response["reason"], str)):
        raise ContractError("invalid administrator response")
    missing = response.get("missing_evidence", [])
    if not isinstance(missing, list) or len(missing) > 8 or not all(isinstance(v, str) and len(v) <= 1000 for v in missing):
        raise ContractError("invalid missing evidence requirements")
    validate_call(runtime, call_id, reserved["owner"], reserved["host_epoch"])
    revision = runtime.db.execute("SELECT revision FROM problems WHERE project=? AND id=?",
                                  (project, packet["problem"]["id"])).fetchone()
    if not revision or revision[0] != packet["problem"]["revision"]:
        raise ContractError("reviewed problem changed")
    current_packet = runtime.review_packet(project, packet["problem"]["id"])
    if review_signature(current_packet, runtime.config["admin"], _fingerprint(current_packet)) != reserved["review_signature"]:
        raise ContractError("review evidence or comparison context changed")
    result = {"status": response["decision"], "reason": clean(response["reason"])[:1000], "call_id": call_id,
              "problem_id": packet["problem"]["id"], "missing_evidence": missing}
    body, proposal = response["candidate"], response.get("proposal")
    if response["decision"] == "candidate":
        validate(body, CANDIDATE)
        if body["problem_id"] != packet["problem"]["id"] or body["problem_revision"] != packet["problem"]["revision"]:
            raise ContractError("administrator changed review identity or revision")
        if set(body["evidence_refs"]) - {e["id"] for e in packet["events"]}:
            raise ContractError("candidate references evidence outside the review packet")
        if packet.get("evidence_truncated"):
            body = dict(body, limitations=body["limitations"] + "\n" + _TRUNCATION_NOTE)
        result.update(runtime.submit_candidate(project, expected_epoch=reserved["host_epoch"], **body))
        proposal = proposal or {"action": "publish", "knowledge_id": None, "expected_revision": None}
    elif body is not None:
        raise ContractError("non-candidate response must use candidate=null")
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
    if response["decision"] == "candidate" and runtime.config["admin"]["auto_publish"]:
        reason = ("evidence truncated" if packet.get("evidence_truncated") else
                  "requires trusted tool-success evidence" if body["validation_level"] != "tool_success" else
                  "requires operation solution" if body["category"] != "operation" or body.get("knowledge_kind", "solution") != "solution" else
                  "existing knowledge requires trusted governance" if proposal["action"] != "publish" or packet["problem"].get("knowledge_id") else None)
        if reason:
            result["publication"] = {"status": "not_published", "reason": reason}
        else:
            from .admin_governance import execute_governance
            result["publication"] = execute_governance(runtime, project, result["proposal_id"],
                                                       expected_epoch=reserved["host_epoch"])["publication"]
    result["status"] = response["decision"]
    persist_receipt(runtime, project, packet, reserved["evidence_signature"], reserved["review_signature"],
                    call_id, dict(response, **result, decision=response["decision"]))
    from .admin_queue import scan_signature
    remember_check(runtime, project, reserved["selected"], scan_signature(runtime, project))
    # 取包和写决定可能跨过租约截止点，最终提交前再校验一次。
    validate_call(runtime, call_id, reserved["owner"], reserved["host_epoch"])
    return result


def admin_once(runtime, project, problem_id=None, *, _deadline=None, _scan_limit=10):
    from .admin_lifecycle import mark_launch_intent, mark_started
    cfg = runtime.config["admin"]
    if not cfg["enabled"]:
        return {"status": "disabled", "scanned_count": 0}
    command, timeout = _settings(cfg)
    if cfg["max_calls_per_day"] == 0:
        return {"status": "budget_exhausted", "scanned_count": 0}
    if _deadline is not None:
        remaining = int(_deadline - time.monotonic())
        if remaining < 1:
            return {"status": "batch_time_exhausted", "scanned_count": 0}
        timeout = min(timeout, remaining)
    with runtime.transaction():
        reserved = _select_review(runtime, project, problem_id, _scan_limit, timeout)
    if reserved["status"] != "reserved":
        return reserved
    call_id, process, started_at = reserved["call_id"], None, time.monotonic()
    context = {"scanned_count": reserved["scanned_count"],
               **({"skipped_problem_ids": reserved["skipped_problem_ids"]} if reserved["skipped_problem_ids"] else {})}
    log_path, temporary = None, None
    try:
        temporary = tempfile.TemporaryDirectory(prefix="agent-learning-review-")
        temp = temporary.name
        directory = Path(temp)
        schema_path, output, log_path = directory / "response.schema.json", directory / "result.json", directory / "events.log"
        schema_path.write_text(encode(RESPONSE_SCHEMA), encoding="utf-8")
        args = command + ["exec", "--ignore-user-config", "--sandbox", "read-only", "--ephemeral",
                          "--skip-git-repo-check", "-C", str(directory), "--json", "--output-schema",
                          str(schema_path), "--output-last-message", str(output), "-"]
        env = dict(os.environ, ALP_DISABLED="1", ALP_HOME=str(runtime.home))
        if _deadline is not None:
            remaining = int(_deadline - time.monotonic())
            if remaining < 1:
                usage = _finish(runtime, call_id, "not_started", None, started_at)
                return {"status": "batch_time_exhausted", "call_id": call_id, "usage": usage, **context}
            timeout = min(timeout, remaining)
        with log_path.open("wb") as log:
            mark_launch_intent(runtime, call_id, reserved["owner"], reserved["host_epoch"])
            try:
                process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=log, stderr=log, env=env, cwd=directory,
                                           **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            except OSError:
                # Popen明确失败才释放未启动额度；崩溃窗口由持久intent按未知恢复。
                usage = _finish(runtime, call_id, "not_started", log_path, started_at)
                return {"status": "not_started", "call_id": call_id, "usage": usage, **context}
            mark_started(runtime, call_id, reserved["owner"], getattr(process, "pid", None))
            try:
                process.communicate(reserved["instruction"].encode("utf-8"), timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                usage = _finish(runtime, call_id, "timeout_unknown", log_path, started_at)
                return {"status": runtime.db.execute("SELECT state FROM admin_calls WHERE id=?", (call_id,)).fetchone()[0],
                        "call_id": call_id, "usage": usage, **context}
        if process.returncode != 0 or not output.is_file() or output.stat().st_size > 50000:
            raise ContractError("administrator produced no usable response")
        response = json.loads(output.read_text(encoding="utf-8-sig"))
        with runtime.transaction():
            result = _accept_response(runtime, project, reserved, response)
            result["usage"] = _finish(runtime, call_id, response["decision"], log_path, started_at)
            actual = runtime.db.execute("SELECT state FROM admin_calls WHERE id=?", (call_id,)).fetchone()[0]
            if actual != response["decision"]:
                # 同一事务回滚候选、治理提议、回执和错误终态；异常路径补真实未知结果。
                raise ContractError("administrator decision could not commit with its call state")
        return dict(result, **context)
    except (OSError, ValueError, TypeError, ContractError) as exc:
        usage = _finish(runtime, call_id, "not_started" if process is None else "failed", log_path, started_at)
        state = runtime.db.execute("SELECT state FROM admin_calls WHERE id=?", (call_id,)).fetchone()[0]
        return {"status": state, "error_type": type(exc).__name__, "call_id": call_id, "usage": usage, **context}
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if temporary is not None:
            temporary.cleanup()


def admin_batch(runtime, project, *, dispatch_token=None, _deadline=None):
    from .admin_queue import release_dispatch
    cfg = runtime.config["admin"]
    results, scanned, calls = [], 0, 0
    started = time.monotonic()
    try:
        if not cfg["enabled"]:
            return {"status": "disabled", "items": []}
        _settings(cfg)
        if dispatch_token:
            row = runtime.db.execute("SELECT * FROM admin_dispatch WHERE project=? AND token=?", (project, dispatch_token)).fetchone()
            if not row or row["host_epoch"] != runtime.host_epoch() or row["lease_until"] <= time.time():
                return {"status": "dispatch_expired", "items": []}
        epoch = runtime.host_epoch()
        deadline = started + cfg["max_batch_seconds"]
        if _deadline is not None:
            if not isinstance(_deadline, (int, float)) or isinstance(_deadline, bool):
                raise ContractError("invalid administrator batch deadline")
            import math
            if not math.isfinite(_deadline):
                raise ContractError("invalid administrator batch deadline")
            deadline = min(deadline, _deadline)
        while scanned < cfg["max_cases_per_batch"] and calls < cfg["max_calls_per_batch"]:
            runtime.assert_host_epoch(epoch)
            result = admin_once(runtime, project, _deadline=deadline,
                                _scan_limit=min(10, cfg["max_cases_per_batch"] - scanned))
            results.append(result)
            scanned += result.get("scanned_count", 0)
            calls += int("call_id" in result and "usage" in result
                         and result["status"] not in {"not_started", "batch_time_exhausted"})
            if result["status"] in {"disabled", "idle", "budget_exhausted", "token_budget_exhausted", "concurrency_limited",
                                    "batch_time_exhausted", "waiting_for_review", "not_started"}:
                break
            if result.get("scanned_count", 0) == 0:
                break
        return {"status": "batch_complete", "items": results, "scanned_count": scanned, "invocations": calls,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "stop_reason": "case_limit" if scanned >= cfg["max_cases_per_batch"] else
                               "call_limit" if calls >= cfg["max_calls_per_batch"] else results[-1]["status"] if results else "idle"}
    finally:
        release_dispatch(runtime, project, dispatch_token)
