"""Bounded contextual tool evidence for an existing stage's public cases.

Selection is a local association, never proof that a conclusion follows from a
tool. Original events and outcomes stay authoritative. The durable selection
receipt describes this optional scope; it does not claim a full tool audit.
"""
from __future__ import annotations

import json
import re
import sqlite3


VERSION = 1
SCAN_LIMIT = 128
SCAN_CHARS = 196608
PUBLIC_CHARS = 16000
MAX_TOOLS = 3
TOOL_CHARS = 6000
_SUMMARY = re.compile(r"^阶段公开记录：.* \[([a-f0-9]{32})\] part [0-9]+$", re.S)
_WORDS = re.compile(r"(?<![a-z0-9_])[a-z_][a-z0-9_.:/\\+-]{2,119}", re.I)
_CHINESE = re.compile(r"[\u3400-\u9fff]{4,}")
_IGNORE = frozenset("the and for with this that from have were been will would could should project knowledge "
    "agent learning codex antigravity dsh check checked checking status success succeeded failed failure error "
    "result results output input tool tools command commands test tests testing read write file files python "
    "powershell windows reported verified complete completed true false none null unknown exit code local "
    "return source sources install installed installation update updated upgrade upgraded version runtime "
    "process processes connection connected normal healthy passed git main json http https utf8 utf-8 "
    "uses used using use was are has had not yes you your our administrator admin query review "
    "evidence record records review reviewing prompt message assistant public returncode redacted truncated".split())
_ZH_IGNORE = ("当前项目", "检查通过", "连接正常", "状态正常", "已经完成", "执行成功", "工具调用", "运行正常")


def _text(value):
    """Only already stored, sanitized public data, with bounded traversal."""
    if isinstance(value, str):
        return value[:TOOL_CHARS * 2]
    if isinstance(value, dict):
        return " ".join(_text(child) for child in list(value.values())[:30])[:TOOL_CHARS * 2]
    if isinstance(value, list):
        return " ".join(_text(child) for child in value[:20])[:TOOL_CHARS * 2]
    return ""


def _anchors(text):
    # Technical names/paths are more selective than generic status vocabulary.
    # Do not use timestamps, numeric counts or Chinese single-character overlap.
    words = list(dict.fromkeys(word.rstrip(".:/\\+-").casefold() for word in _WORDS.findall(text)
                              if word.rstrip(".:/\\+-").casefold() not in _IGNORE))
    chinese = []
    for phrase in _CHINESE.findall(text):
        for value in _ZH_IGNORE:
            phrase = phrase.replace(value, " ")
        for part in phrase.split():
            chinese.extend(part[index:index+4] for index in range(min(64, len(part)-3)))
    return set((words + chinese)[:256]) - {""}


def _partial(value):
    if isinstance(value, dict):
        if value.get("completion", "complete") != "complete" or value.get("full_original_available") is False:
            return True
        if value.get("capture_coverage") is not None and not isinstance(value["capture_coverage"], dict):
            return True
        return any(_partial(child) for child in value.values())
    return isinstance(value, list) and any(_partial(child) for child in value)


def _budget_preview(runtime, project, problem, phase, packet=None):
    """Measure bounded envelope fields, not thousands of imaginary spaces.

    This is a size-only preview, never a stored phase or citable source. Optional
    tools leave room for the bounded diagnostics of a later multi-stage batch.
    """
    from .phase_review import batch_packet, item_char_budget
    maximum = runtime.admin_effective_config(project)["max_input_chars"]
    parent = dict(problem, id="0" * 36, revision=2, task_id=None, task_revision=None,
                  summary="阶段集中审核 [" + "0" * 32 + "]", category="mixed",
                  knowledge_id=None, knowledge_revision=None)
    phase_ids = [phase["id"]] + ["0" * 36] * 19
    identities = ["0" * 36] * 32
    empty = batch_packet(parent, phase_ids, [], phase["conversation"], maximum,
                         omitted=identities, reused=identities, continuation=True)
    limit = item_char_budget(maximum, empty)
    items = []
    if packet is not None:
        item = dict(packet)
        item["phase_tool_context"] = {"policy_version": VERSION, "scope": "selected_related_tools_only",
            "stage_status": "delivered", "selected_count": MAX_TOOLS, "omitted_matching": SCAN_LIMIT,
            "scan_limited": True, "selection_unavailable": False,
            "excluded": {key: SCAN_LIMIT for key in ("existing_case", "partial_source", "budget", "duplicate")},
            "max_tools_per_stage": MAX_TOOLS, "tool_payload_budget": TOOL_CHARS}
        items.append(item)
    return batch_packet(parent, phase_ids, items, phase["conversation"], limit,
                        omitted=identities, reused=identities, continuation=True), limit


def _token_fits(runtime, project, packet, phase):
    from .admin_usage import estimate_reservation
    from .phase_review import instruction
    body, _ = _budget_preview(runtime, project, packet["problem"], phase, packet)
    visible = instruction(body)
    return (len(visible) <= runtime.admin_effective_config(project)["max_input_chars"]
            and estimate_reservation(runtime, project, visible, phase=True)["admitted"])


def _public_text(runtime, project, refs):
    result = []
    for identity in refs:
        row = runtime.db.execute("SELECT type,payload FROM events WHERE project=? AND id=?", (project, identity)).fetchone()
        if not row:
            continue
        body = json.loads(row["payload"])
        if not isinstance(body, dict):
            continue
        key = {"Stop": "last_assistant_message", "PublicMessageSegment": "text",
               "UserPromptSubmit": "prompt", "PhaseRequestContext": "prompt"}.get(row["type"])
        if key and isinstance(body.get(key), str):
            result.append((identity, body[key][:12000]))
    return result


def _manifest_id(phase_id):
    return ["phase_tool_evidence_selection", phase_id]


def packet_selection(runtime, project, problem):
    """Expose a bounded receipt without making it independently citable evidence."""
    from .runtime import digest
    match = _SUMMARY.fullmatch(problem.get("summary", ""))
    if not match:
        return None
    row = runtime.db.execute("SELECT substr(payload,1,12001) FROM events WHERE project=? AND id=? "
        "AND type='PhaseToolEvidenceSelection' AND conversation=? AND task_id=? AND task_revision=?",
        (project, digest([project, _manifest_id(match[1])]), problem["conversation"],
         problem["task_id"], problem["task_revision"])).fetchone()
    if not row:
        return None
    unavailable = {"policy_version": VERSION, "scope": "selected_related_tools_only", "stage_status": "delivered",
                   "selected_count": 0, "omitted_matching": 0, "scan_limited": False,
                   "selection_unavailable": True, "excluded": {}, "max_tools_per_stage": MAX_TOOLS,
                   "tool_payload_budget": 0}
    try:
        receipt = json.loads(row[0]) if len(row[0]) <= 12000 else None
    except (ValueError, TypeError):
        return unavailable
    if (not isinstance(receipt, dict) or receipt.get("policy_version") != VERSION
            or not isinstance(receipt.get("selected"), list) or len(receipt["selected"]) > MAX_TOOLS
            or any(not isinstance(item, dict) or not isinstance(item.get("problem_id"), str) for item in receipt["selected"])
            or not isinstance(receipt.get("first_problem_id"), str)
            or type(receipt.get("omitted_matching")) is not int or receipt["omitted_matching"] < 0
            or type(receipt.get("scan_limited")) is not bool or not isinstance(receipt.get("excluded"), dict)
            or type(receipt.get("tool_payload_budget")) is not int):
        return unavailable
    selected = [item for item in receipt["selected"] if item["problem_id"] == problem["id"]]
    if not selected and receipt["first_problem_id"] != problem["id"]:
        return None
    return {"policy_version": receipt["policy_version"], "scope": "selected_related_tools_only",
            "stage_status": "delivered",
            "selected_count": len(selected), "omitted_matching": receipt["omitted_matching"],
            "scan_limited": receipt["scan_limited"], "selection_unavailable": receipt.get("selection_unavailable", False),
            "excluded": receipt["excluded"],
            "max_tools_per_stage": MAX_TOOLS, "tool_payload_budget": receipt["tool_payload_budget"]}


def attach(runtime, project, phase, task, stop, cases):
    """Optional context cannot roll back an otherwise valid public delivery."""
    from .runtime import ContractError
    if not runtime.db.in_transaction:
        raise ContractError("stage tool selection requires a writer transaction")
    runtime.db.execute("SAVEPOINT phase_tool_context")
    try:
        _attach(runtime, project, phase, task, stop, cases)
    except (OSError, ValueError, TypeError, sqlite3.Error):
        runtime.db.execute("ROLLBACK TO phase_tool_context")
        runtime.db.execute("RELEASE phase_tool_context")
        runtime._metric(project, "phase_tool_context_unavailable")
        if cases:
            try:
                _record(runtime, project, phase, task, {
                    "policy_version": VERSION, "phase_id": phase["id"], "scope": "selected_related_tools_only",
                    "first_problem_id": cases[0][0]["id"], "selected": [], "omitted_matching": 0,
                    "excluded": {}, "scan_limited": False, "tool_payload_budget": 0,
                    "selection_unavailable": True})
            except (OSError, ValueError, TypeError, sqlite3.Error):
                pass  # The original capture metric still records the optional failure.
        return
    runtime.db.execute("RELEASE phase_tool_context")


def _attach(runtime, project, phase, task, stop, cases):
    """Attach to existing public cases before freezing; no model or new cases."""
    from .event_capture import projected_evidence
    if not cases:
        return
    public, public_chars, public_limited = [], 0, False
    for problem, refs in cases:
        for identity, text in reversed(_public_text(runtime, project, refs)):
            if public_chars + len(text) > PUBLIC_CHARS:
                public_limited = True
                continue
            public.append((problem["id"], identity, _anchors(text), text.casefold()))
            public_chars += len(text)
    if not any(anchors for _, _, anchors, _ in public):
        return
    # Native events must have a real current operation and task binding. A
    # legitimate stage may span several execution fences/write versions.
    stop_sequence = runtime.db.execute("SELECT rowid FROM events WHERE project=? AND id=?", (project, stop["id"])).fetchone()[0]
    from .phase_window import event_bounds
    first_write, first_sequence = event_bounds(runtime, project, phase)
    rows = runtime.db.execute("SELECT e.*,o.host_epoch operation_epoch FROM events e JOIN operations o "
        "ON o.project=e.project AND o.id=e.operation WHERE e.project=? AND e.conversation=? "
        "AND e.task_id=? AND e.task_revision=? AND e.agent IS NULL AND e.assignment_id IS NULL "
        "AND e.type='PostToolUse' AND e.created>=? AND e.created<=? AND e.rowid<=? AND o.host_epoch=? "
        "AND o.task_id=e.task_id AND o.task_revision=e.task_revision AND o.agent IS NULL "
        "AND o.assignment_id IS NULL AND e.rowid>? AND e.task_write_version>=? AND e.task_write_version<=? "
        "AND EXISTS(SELECT 1 FROM task_states s WHERE s.project=e.project AND s.id=e.task_id "
        "AND s.revision=e.task_revision AND s.write_version=e.task_write_version) "
        "ORDER BY e.created DESC,e.rowid DESC LIMIT ?", (project, phase["conversation"], task["id"], task["revision"],
        phase["start_time"], stop["created"], stop_sequence, phase["session_epoch"], first_sequence, first_write,
        phase["delivery_version"], SCAN_LIMIT + 1)).fetchall()
    if not rows:
        return
    excluded = {"existing_case": 0, "partial_source": 0, "budget": 0, "duplicate": 0}
    candidates, scanned, seen, operations, used = [], 0, set(), set(), 0
    scan_limited = len(rows) > SCAN_LIMIT or public_limited
    for event in rows[:SCAN_LIMIT]:
        scanned += len(event["payload"])
        if scanned > SCAN_CHARS:
            scan_limited = True
            break
        body = json.loads(event["payload"])
        if body.get("host_epoch") != phase["session_epoch"]:
            continue
        if event["operation"] in operations:
            excluded["duplicate"] += 1
            continue
        operations.add(event["operation"])
        name = body.get("tool_name", "")
        if not isinstance(name, str) or "agent_learning" in name or "agent-learning" in name:
            continue
        # Sharing a command wrapper is insufficient. At least one specific
        # public anchor must occur in the stored observable response itself.
        response = _text(body.get("tool_response"))
        output_terms = _anchors(response)
        matches = [(len(anchors & output_terms), problem_id, identity, sorted(anchors & output_terms))
                   for problem_id, identity, anchors, _ in public if anchors & output_terms]
        if not matches:
            continue
        _, problem_id, identity, anchors = max(matches, key=lambda item: (item[0], item[2]))
        if runtime.db.execute("SELECT 1 FROM problem_events pe JOIN events linked ON linked.id=pe.event "
                "JOIN problems p ON p.id=pe.problem WHERE linked.project=? AND linked.operation=? "
                "AND NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.problem_id=p.id) "
                "AND NOT EXISTS(SELECT 1 FROM work_summary_links w WHERE w.problem_id=p.id) LIMIT 1",
                (project, event["operation"])).fetchone():
            excluded["existing_case"] += 1
            continue
        if projected_evidence(body) or _partial(body):
            excluded["partial_source"] += 1
            continue
        # Full business digests retain differences beyond a visible preview.
        signature = (event["business_digest"] or event["id"], event["outcome"])
        if signature in seen:
            excluded["duplicate"] += 1
            continue
        seen.add(signature)
        candidates.append({"event": dict(event), "problem_id": problem_id, "public_event_id": identity,
                           "anchors": anchors[:8], "score": len(anchors)})
    # Payload cap and complete-request admission are independent. Avoid shrinking
    # evidence twice using character guesses; each full observation is checked below.
    budget = TOOL_CHARS
    selected = []
    for item in sorted(candidates, key=lambda item: (-item["score"], -item["event"]["created"], item["event"]["id"])):
        event = item["event"]
        if len(selected) >= MAX_TOOLS or used + len(event["payload"]) > budget:
            excluded["budget"] += 1
            continue
        # Do not attach an optional large event if it would prevent the public
        # case from being reviewed. Reserve space for the selection receipt.
        runtime.db.execute("SAVEPOINT phase_tool_fit")
        try:
            runtime._attach_case_evidence(item["problem_id"], [(event["id"], "observation")])
            problem = runtime.db.execute("SELECT * FROM problems WHERE project=? AND id=?",
                                         (project, item["problem_id"])).fetchone()
            _, item_limit = _budget_preview(runtime, project, dict(problem), phase)
            packet = runtime.review_packet(project, item["problem_id"], max_chars=item_limit)
            fits = (not packet.get("incomplete") and not packet.get("evidence_truncated")
                    and _token_fits(runtime, project, packet, phase))
        except BaseException:
            runtime.db.execute("ROLLBACK TO phase_tool_fit")
            runtime.db.execute("RELEASE phase_tool_fit")
            raise
        if not fits:
            runtime.db.execute("ROLLBACK TO phase_tool_fit")
            excluded["budget"] += 1
        else:
            selected.append({"problem_id": item["problem_id"], "event_id": event["id"],
                             "public_event_id": item["public_event_id"], "anchors": item["anchors"]})
            used += len(event["payload"])
        runtime.db.execute("RELEASE phase_tool_fit")
    receipt = {"policy_version": VERSION, "phase_id": phase["id"], "scope": "selected_related_tools_only",
               "first_problem_id": cases[0][0]["id"], "selected": selected,
               "omitted_matching": excluded["partial_source"] + excluded["budget"],
               "excluded": excluded, "scan_limited": scan_limited, "tool_payload_budget": budget}
    # Audit-only receipt. It is not a new case, a candidate source or authority.
    _record(runtime, project, phase, task, receipt)


def _record(runtime, project, phase, task, receipt):
    runtime._event(project, phase["conversation"], "PhaseToolEvidenceSelection", receipt,
        event_id=_manifest_id(phase["id"]), task_id=task["id"], task_revision=task["revision"],
        task_write_version=phase["delivery_version"], task_status="delivered")
