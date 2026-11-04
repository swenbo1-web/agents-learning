"""Conservative local deferral of transient plugin status, never a review verdict.

Unknown text, tools, partial capture and independent cases retain normal review.
No model or external service is used, and source evidence is not modified.
"""
from __future__ import annotations

import json
import re
import unicodedata


_STAGE_SUMMARY = re.compile(r"\A阶段公开记录：(.*) \[[a-f0-9]{32}\] part [0-9]+\Z", re.S)


def _comparison_delivery(problem, events, latest_delivery_id):
    """Read a complete public delivery from the already bounded evidence page.

    Segments are a lossless projection of one public message, not new success
    evidence. An omitted/newer or ambiguous message must never revive an older
    conclusion just because that earlier text would be useful for retrieval.
    """
    from .public_observations import observed_public

    deliveries = [event for event in events if event.get("role") != "history_related" and (
        event.get("type") == "Stop" or event.get("type") == "PublicMessageSegment"
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("memory_type") == "public_summary")]
    if latest_delivery_id is not None:
        chosen = [event for event in deliveries if event.get("id") == latest_delivery_id]
        if not chosen:
            # A parent Stop preview may have yielded to its complete segments.
            chosen = [event for event in deliveries if event.get("type") == "PublicMessageSegment"
                      and event["payload"].get("public_message_id") == latest_delivery_id]
        if not chosen:
            return None, "delivery_omitted"
    else:
        chosen = deliveries
    if not chosen:
        return None, "no_delivery"
    latest = max(chosen, key=lambda event: (event.get("created") or 0, event.get("id", "")))
    parent = latest.get("id") if latest["type"] == "Stop" else latest["payload"].get("public_message_id")
    segments = [event for event in deliveries if event.get("type") == "PublicMessageSegment"
                and event["payload"].get("public_message_id") == parent]

    def eligible(event):
        return (event.get("health", "valid") == "valid" and not event.get("agent")
                and not event.get("assignment_id") and event.get("outcome") in {"reported", "unknown"}
                and all(event.get(key) in (None, problem.get(key)) for key in
                        ("project", "conversation", "task_id", "task_revision") if problem.get(key) is not None))

    def complete(event):
        return eligible(event) and observed_public(event["type"], event.get("payload"), require_complete=True)

    if not segments:
        if latest["type"] != "Stop" or not complete(latest):
            return None, "incomplete_delivery"
        return latest["payload"]["last_assistant_message"], "complete_public_delivery"
    # Event ids are opaque: native callbacks use digests, local legacy events
    # may use UUIDs. Their exact identity, not a particular spelling, binds parts.
    if not isinstance(parent, str) or not parent or len(parent) > 200:
        return None, "incomplete_delivery"
    first = segments[0]["payload"]
    count, chars = first.get("segments"), first.get("message_chars")
    if (type(count) is not int or not 1 <= count <= 64 or len(segments) != count
            or type(chars) is not int or chars < 1):
        return None, "incomplete_delivery"
    if chars > 2048:
        return None, "delivery_too_long"
    pieces = {}
    identities = ("project", "conversation", "task_id", "task_revision", "task_write_version",
                  "execution_epoch", "agent", "assignment_id")
    identity = tuple(segments[0].get(key) for key in identities)
    for event in segments:
        body = event["payload"]
        index = body.get("segment")
        if (not complete(event) or type(index) is not int or not 0 <= index < count or index in pieces
                or body.get("segments") != count or type(body.get("segments")) is not int
                or body.get("message_chars") != chars or type(body.get("message_chars")) is not int
                or body.get("capture_coverage", {}) != first.get("capture_coverage", {})
                or body.get("execution_authority") is not False
                or tuple(event.get(key) for key in identities) != identity):
            return None, "incomplete_delivery"
        pieces[index] = body["text"]
    text = "".join(pieces[index] for index in range(count))
    if len(text) != chars:
        return None, "incomplete_delivery"
    # The existing producer binds every part id to the complete visible text.
    # Same parent/count/length alone cannot exclude mixing equal-length versions.
    from .runtime import digest
    project = problem.get("project")
    text_digest = digest(text)
    if not project or any(event.get("id") != digest([project, ["public-message", parent, text_digest,
                                                             event["payload"]["segment"]]]) for event in segments):
        return None, "incomplete_delivery"
    # If both forms are visible they must agree, unless the parent explicitly
    # records only a truncated projection. Never repair a conflicting source.
    parents = [event for event in deliveries if event.get("type") == "Stop" and event.get("id") == parent]
    if any(not complete(event) or event["payload"]["last_assistant_message"] != text for event in parents
           if observed_public("Stop", event.get("payload"), require_complete=True)):
        return None, "incomplete_delivery"
    if any(not eligible(event) or not observed_public("Stop", event.get("payload"))
           or event["payload"].get("completion", "complete") != "complete"
           or event["payload"].get("capture_coverage", {}) != first.get("capture_coverage", {})
           or any(event.get(key) is not None and segments[0].get(key) is not None
                  and event[key] != segments[0][key] for key in identities) for event in parents):
        return None, "incomplete_delivery"
    return text, "complete_public_segments"


def comparison_queries(problem, task, events, *, latest_delivery_id=None):
    """Keep generated labels out of recall, without inventing a business topic.

    A fallback is only a complete short public delivery plus the unchanged
    original goal (except an exact continuation with no business subject).
    Negation, versions and paths stay visible to the comparator;
    these queries never classify a project report as routine or grant release.
    """
    summary = problem.get("summary", "")
    goal = (task or {}).get("goal", "")
    match = _STAGE_SUMMARY.fullmatch(summary) if isinstance(summary, str) else None
    if match is None:
        return {"primary": summary + " " + goal, "fallback": None, "fallback_reason": "not_stage"}
    primary = goal or match[1]
    if len(primary) > 1024:
        return {"primary": primary, "fallback": None, "fallback_reason": "goal_too_long"}
    text, reason = _comparison_delivery(problem, events, latest_delivery_id)
    if text is None:
        return {"primary": primary, "fallback": None, "fallback_reason": reason}
    if len(text) > 2048:
        return {"primary": primary, "fallback": None, "fallback_reason": "delivery_too_long"}
    if _text(text) == _text(primary):
        return {"primary": primary, "fallback": None, "fallback_reason": "same_material"}
    context = "" if _text(primary) in {"继续", "继续推进", "继续执行", "continue", "proceed"} else primary
    return {"primary": primary, "fallback": (context + "\n" if context else "") + text,
            "fallback_reason": reason}


def _text(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold()).strip("。.!！?？")


def _request(value):
    text = _text(value)
    return bool(len(text) <= 160 and re.fullmatch(
        r"(?:(?:请|帮我|麻烦|只读))*?(?:检查|查看|核对|确认)(?:一下)?(?:当前项目的?|本项目的?)?"
        r"(?:agent[-_]?learning(?:插件)?)(?:的)?(?:连接|状态|连接状态|管理员状态)"
        r"|(?:please)?(?:check|verify|show)(?:the)?agent[-_]?learning(?:plugin)?(?:connection|status|connectionstatus|administratorstatus)", text))


def _reply(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1600:
        return False
    # Full clauses must match; a familiar status word cannot hide new facts.
    clauses = [_text(clause) for clause in re.split(r"[。.!！?？;；\r\n]+", value) if _text(clause)]
    patterns = (
        r"(?:当前)?(?:agent[-_]?learning(?:插件)?的?)?连接(?:正常|已连接|检查通过)",
        r"(?:当前)?(?:agent[-_]?learning(?:插件)?的?)?状态正常",
        r"(?:当前)?管理员(?:已开启|已启用|未开启|未启用|已关闭|运行正常|空闲)",
        r"(?:当前)?无(?:在途|待处理|待审核)(?:管理员)?(?:调用|阶段|任务)",
        r"(?:当前)?(?:待处理|待审核)(?:阶段|任务|案例)[:：]?\d{1,8}(?:个|条)?",
        r"(?:本轮|本次)(?:仅为|为|仅)?只读(?:状态|连接)(?:检查|核对)(?:完成|通过)?",
        r"(?:agent[-_]?learning(?:plugin)?)?(?:connection|status)(?:is)?(?:ok|normal|healthy|connected)",
        r"(?:administrator|admin)(?:is)?(?:enabled|disabled|idle|running)",
        r"noactive(?:administrator|admin)?calls",
    )
    return bool(clauses) and all(any(re.fullmatch(pattern, clause) for pattern in patterns) for clause in clauses)


def _partial(value):
    if isinstance(value, dict):
        if any(value.get(key) is True for key in
               ("truncated", "message_truncated", "prompt_truncated", "last_assistant_message_truncated")):
            return True
        if "completion" in value and value["completion"] != "complete":
            return True
        return any(_partial(item) for item in value.values())
    return isinstance(value, list) and any(_partial(item) for item in value)


def _status_tool(host, name):
    """Use host contracts, never a suffix match on an arbitrary tool name."""
    if host not in {"codex", "antigravity", "dsh"} or not isinstance(name, str):
        return False
    names = {"mcp__agent_learning__" + tool for tool in ("learning_status", "session_context")}
    if host == "dsh":
        names.update(("learning_status", "session_context",
                      "agent_learning_learning_status", "agent_learning_session_context"))
    return name in names


def _public_request_digest(runtime, project, phase, kind, body):
    """A metadata receipt needs the complete request actually saved as evidence."""
    from .public_observations import observed_public
    from .runtime import digest
    prompt = body.get("prompt")
    if kind == "PhaseRequestContext":
        original = runtime.db.execute("SELECT payload,outcome FROM events WHERE project=? AND conversation=? "
            "AND id=? AND type='UserPromptSubmit' AND agent IS NULL",
            (project, phase["conversation"], body.get("original_event_id"))).fetchone()
        if original is None or original["outcome"] not in {"reported", "unknown"}:
            return None
        body = json.loads(original["payload"])
        if not isinstance(body, dict) or body.get("prompt") != prompt:
            return None
    if (body.get("host_epoch") != phase["session_epoch"]
            or not observed_public("UserPromptSubmit", body, require_complete=True)):
        return None
    return digest(prompt)


def _native_input_digest(runtime, phase, body):
    """Allow only a known complete native-input diagnostic, never unknown capture."""
    coverage = body.get("capture_coverage", {})
    public = coverage.get("lastUserInput", {}) if isinstance(coverage, dict) else {}
    if (runtime.host != "antigravity" or not isinstance(coverage, dict)
            or body.get("host_epoch") != phase["session_epoch"]
            or coverage.get("workspace_association") != "installed_workspace"
            or coverage.get("trajectory_identity") != "current"
            or not isinstance(public, dict) or public.get("source") != "native_hook"
            or public.get("available") is not True or public.get("full_original_available") is not True
            or public.get("truncated") is not False
            or any(type(body.get(key)) is not int or body[key] < minimum
                   for key, minimum in (("generation", 1), ("invocation_num", 0), ("initial_steps", 0)))):
        return None
    value = body.get("public_input_digest")
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


def is_routine(runtime, project, phase):
    """Recheck the whole original window, including late revisions, on each scan.

    Deliberately bounded: overflowing this window means normal review. A shell
    command cannot be inferred read-only from its name or a successful exit.
    """
    from .phase_review import _case_source
    if phase["omitted_sources"]:
        return False
    task = runtime.task_snapshot(project, phase["task_id"], phase["task_revision"], phase["delivery_version"])
    if not task or not _request(task["goal"]):
        return False
    sql, args = _case_source(runtime, project, phase)
    cases = runtime.db.execute("SELECT p.id,p.summary,p.knowledge_id,p.revision,"
        "(SELECT state FROM review_states r WHERE r.project=p.project AND r.problem=p.id AND r.revision=p.revision) review_state"
        + sql + "LIMIT 17", args).fetchall()
    if not cases or len(cases) > 16 or any(
            row["knowledge_id"] or row["revision"] != 1 or row["review_state"] == "awaiting_evidence"
            or not row["summary"].startswith("阶段公开记录：")
            or "[" + phase["id"] + "] part " not in row["summary"] for row in cases):
        return False
    stop = runtime.db.execute("SELECT created,rowid FROM events WHERE project=? AND id=?", (project, phase["event_id"])).fetchone()
    if not stop:
        return False
    from .phase_window import event_bounds
    first_write, first_sequence = event_bounds(runtime, project, phase)
    events = runtime.db.execute(
        "SELECT e.type,e.outcome,e.agent,e.operation,length(e.payload) chars,substr(e.payload,1,6001) payload "
        "FROM events e LEFT JOIN events origin ON origin.project=e.project AND origin.id=json_extract(e.payload,'$.public_message_id') "
        "AND e.type='PublicMessageSegment' WHERE e.project=? AND e.conversation=? "
        "AND ((e.task_id=? AND e.task_revision=?) OR e.task_id IS NULL) "
        "AND COALESCE(origin.created,e.created)>=? AND COALESCE(origin.created,e.created)<=? "
        "AND COALESCE(origin.rowid,e.rowid)>? AND COALESCE(origin.rowid,e.rowid)<=? "
        "AND (e.task_id IS NULL OR e.task_write_version BETWEEN ? AND ?) "
        "AND e.type NOT IN ('PhaseWindowStarted','PhaseToolEvidenceSelection') "
        "ORDER BY COALESCE(origin.created,e.created),e.created,e.rowid LIMIT 41",
        (project, phase["conversation"], phase["task_id"], phase["task_revision"], phase["start_time"], stop[0],
         first_sequence, stop[1], first_write, phase["delivery_version"])).fetchall()
    if len(events) > 40 or sum(row["chars"] for row in events) > 16000:
        return False
    requested = replied = False
    request_digests, native_inputs, status_operations = set(), [], {}
    for event in events:
        if event["chars"] > 6000 or event["agent"] or event["outcome"] in {"failed", "conflict", "denied"}:
            return False
        body = json.loads(event["payload"])
        if not isinstance(body, dict) or _partial(body):
            return False
        kind = event["type"]
        if kind in {"UserPromptSubmit", "PhaseRequestContext"}:
            prompt = body.get("prompt")
            if _request(prompt):
                requested = True
                identity = _public_request_digest(runtime, project, phase, kind, body)
                if identity:
                    request_digests.add(identity)
            elif _text(prompt) not in {"继续", "继续核对", "继续检查", "continue"}:
                return False
        elif kind == "AntigravityPublicInput":
            identity = _native_input_digest(runtime, phase, body)
            if identity is None:
                return False
            native_inputs.append(identity)
        elif kind == "Stop":
            if not _reply(body.get("last_assistant_message")):
                return False
            replied = True
        elif kind == "TaskUpdate":
            if not _request(body.get("goal")):
                return False
        elif kind == "ExecutionFence":
            if (body.get("reason"), body.get("status")) not in {
                    ("new_user_prompt", "active"), ("antigravity_execution_stopped", "delivered")}:
                return False
        elif kind == "AntigravityStop":
            coverage = body.get("capture_coverage", {})
            if (runtime.host != "antigravity" or body.get("fullyIdle") is not True
                    or body.get("terminationReason") != "model_stop" or body.get("error") != ""
                    or coverage.get("workspace_association") != "installed_workspace"
                    or coverage.get("trajectory_identity") != "current"
                    or not _reply(body.get("finalModelOutput"))):
                return False
        elif kind == "SessionStart":
            continue
        elif kind in {"PreToolUse", "PostToolUse"}:
            # Exact plugin status tools only. Unknown/custom/shell tools retain
            # normal review even when their output claims everything is fine.
            name = body.get("tool_name", "")
            if not _status_tool(runtime.host, name):
                return False
            terminals = {"succeeded", "call_completed"}
            if kind == "PostToolUse" and event["outcome"] not in terminals:
                return False
            if event["operation"]:
                operation = runtime.db.execute("SELECT state,conflict,decision,observation,conversation,task_id,"
                                               "task_revision,host_epoch,agent,assignment_id,tool_name "
                                               "FROM operations WHERE project=? AND id=?",
                                               (project, event["operation"])).fetchone()
                if (not operation or operation["conflict"] or operation["decision"] not in {"allowed", "observed"}
                        or operation["observation"] not in terminals or operation["state"] != operation["observation"]
                        or operation["agent"] or operation["assignment_id"] or operation["tool_name"] != name
                        or (operation["conversation"], operation["task_id"], operation["task_revision"], operation["host_epoch"])
                           != (phase["conversation"], phase["task_id"], phase["task_revision"], phase["session_epoch"])):
                    return False
                if kind == "PostToolUse":
                    response = body.get("tool_response")
                    if (event["outcome"] != operation["observation"] or not isinstance(response, dict)
                            or event["outcome"] == "call_completed" and response.get("session_id") is not None):
                        return False
                status_operations.setdefault(event["operation"], set()).add(kind)
            else:
                return False
        else:
            return False
    # A completed MCP call is sufficient only for this local transient-status
    # decision. Its stored observation remains call_completed, never success.
    return (requested and replied and all(identity in request_digests for identity in native_inputs)
            and all(kinds == {"PreToolUse", "PostToolUse"} for kinds in status_operations.values()))
