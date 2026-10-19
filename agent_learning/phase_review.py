"""Frozen task-delivery batches. Capture, frequency and model spending are separate."""
from __future__ import annotations

import json
import time

DEFAULT_POLICY = {"mode": "legacy", "every_phases": 1, "max_input_tokens": 40000,
                  "max_output_tokens": 4000, "daily_tokens": 1000000}
PHASE_PREFIX = (
    "本次是阶段集中审核。items各自属于独立任务和来源，不得互相借用成功证据。"
    "逐项返回一个结果，保持problem_id和problem_revision；每项可提出最多5条独立候选。"
    "公开项目事实保留reported；流水不强行提炼知识。重复返回skip，缺证返回needs_evidence。"
    "不得以新条目绕过已有知识反例、撤销或修订。只审核可见范围，未覆盖材料仍待后续。\n"
)


def response_schema():
    from copy import deepcopy
    from .admin import RESPONSE_SCHEMA, HISTORY_RESPONSE_SCHEMA
    result = deepcopy(RESPONSE_SCHEMA)
    item = deepcopy(HISTORY_RESPONSE_SCHEMA)
    item["properties"]["problem_id"] = {"type": "string", "minLength": 1}
    item["required"].append("problem_id")
    result["properties"]["items"] = {"type": "array", "minItems": 1, "maxItems": 8, "items": item}
    result["required"].append("items")
    return result


def install(runtime):
    runtime.db.execute("CREATE TABLE IF NOT EXISTS phase_review_policies("
        "project TEXT NOT NULL,host_scope TEXT NOT NULL,version INTEGER NOT NULL,body TEXT NOT NULL,"
        "updated REAL NOT NULL,PRIMARY KEY(project,host_scope))")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS review_phases("
        "id TEXT PRIMARY KEY,project TEXT NOT NULL,host_scope TEXT NOT NULL,conversation TEXT NOT NULL,"
        "task_id TEXT NOT NULL,task_revision INTEGER NOT NULL,delivery_version INTEGER NOT NULL,"
        "start_time REAL NOT NULL,session_epoch INTEGER NOT NULL,governance_epoch INTEGER NOT NULL,"
        "state TEXT NOT NULL,event_id TEXT,created REAL NOT NULL,omitted_sources INTEGER NOT NULL DEFAULT 0,"
        "UNIQUE(project,host_scope,task_id,delivery_version))")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS review_phase_queue ON review_phases(project,host_scope,state,created)")
    if "omitted_sources" not in {row[1] for row in runtime.db.execute("PRAGMA table_info(review_phases)")}:
        runtime.db.execute("ALTER TABLE review_phases ADD COLUMN omitted_sources INTEGER NOT NULL DEFAULT 0")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS phase_review_batches("
        "project TEXT NOT NULL,problem_id TEXT PRIMARY KEY,host_scope TEXT NOT NULL,"
        "session_epoch INTEGER NOT NULL,governance_epoch INTEGER NOT NULL,policy_version INTEGER NOT NULL,"
        "phase_ids TEXT NOT NULL,packet TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,result TEXT)")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS phase_review_members("
        "project TEXT NOT NULL,batch_id TEXT NOT NULL,problem_id TEXT NOT NULL,revision INTEGER NOT NULL,"
        "evidence_signature TEXT NOT NULL,state TEXT NOT NULL,context_signature TEXT NOT NULL,PRIMARY KEY(batch_id,problem_id))")
    if "context_signature" not in {row[1] for row in runtime.db.execute("PRAGMA table_info(phase_review_members)")}:
        runtime.db.execute("ALTER TABLE phase_review_members ADD COLUMN context_signature TEXT NOT NULL DEFAULT ''")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS phase_review_queue ON phase_review_batches(project,host_scope,state)")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS phase_member_evidence ON phase_review_members(project,evidence_signature)")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS phase_scan_progress(phase_id TEXT PRIMARY KEY,"
        "signature TEXT NOT NULL,cursor TEXT,deferred INTEGER NOT NULL DEFAULT 0,exhausted INTEGER NOT NULL DEFAULT 0)")


def policy(runtime, project):
    exists = runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name='phase_review_policies'").fetchone()
    row = runtime.db.execute("SELECT * FROM phase_review_policies WHERE project=? AND host_scope=?",
                            (project, runtime._session_scope())).fetchone() if exists else None
    result = {**DEFAULT_POLICY, **(json.loads(row["body"]) if row else {}),
              "version": row["version"] if row else 0, "configured": bool(row)}
    if runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name='admin_project_budget_policies'").fetchone():
        budget = runtime.db.execute("SELECT body FROM admin_project_budget_policies WHERE project=?", (project,)).fetchone()
        maximum = json.loads(budget[0]).get("max_reported_tokens_per_day") if budget else None
        if maximum is not None:
            result["daily_tokens"] = maximum
    return result


def configure(runtime, project, values, expected_version):
    from .runtime import ContractError, encode
    if not runtime.db.in_transaction:
        with runtime.transaction():
            return configure(runtime, project, values, expected_version)
    if not isinstance(values, dict) or set(values) - set(DEFAULT_POLICY):
        raise ContractError("invalid phase review settings")
    current = policy(runtime, project)
    if type(expected_version) is not int or current["version"] != expected_version:
        raise ContractError("phase review settings changed; refresh before saving")
    merged = {**{key: current[key] for key in DEFAULT_POLICY}, **values}
    if merged["mode"] not in {"legacy", "phase"}:
        raise ContractError("invalid phase review mode")
    for key, low, high in (("every_phases", 1, 20), ("max_input_tokens", 16000, 1000000),
                           ("max_output_tokens", 256, 40000), ("daily_tokens", 0, 1000000000)):
        if type(merged[key]) is not int or not low <= merged[key] <= high:
            raise ContractError("invalid phase review " + key)
    if current["configured"] and all(merged[key] == current[key] for key in DEFAULT_POLICY):
        return current
    if runtime.db.execute("SELECT 1 FROM admin_calls WHERE project=? AND state IN ('reserved','launch_intent','started')",
                          (project,)).fetchone():
        raise ContractError("wait for the current administrator call before changing phase settings")
    runtime.db.execute("INSERT INTO phase_review_policies VALUES(?,?,?,?,?) ON CONFLICT(project,host_scope) "
        "DO UPDATE SET version=excluded.version,body=excluded.body,updated=excluded.updated",
        (project, runtime._session_scope(), current["version"] + 1, encode(merged), time.time()))
    if merged["mode"] == "phase":
        from .admin_usage import configure_project_budget, project_budget_policy
        configure_project_budget(runtime, project, {"max_reported_tokens_per_day": merged["daily_tokens"]},
                                 expected_version=project_budget_policy(runtime, project)["version"])
    for row in runtime.db.execute("SELECT problem_id,phase_ids FROM phase_review_batches b WHERE project=? "
        "AND host_scope=? AND state='pending' AND NOT EXISTS(SELECT 1 FROM admin_calls a WHERE a.problem=b.problem_id "
        "AND a.state NOT IN ('not_started','deduplicated'))", (project, runtime._session_scope())).fetchall():
        runtime.db.execute("UPDATE phase_review_batches SET state='cancelled' WHERE problem_id=?", (row[0],))
        runtime.db.execute("UPDATE problems SET status='superseded' WHERE id=?", (row[0],))
        for identity in json.loads(row[1]):
            runtime.db.execute("UPDATE review_phases SET state='sealed' WHERE id=? AND state='queued'", (identity,))
    return policy(runtime, project)


def scope(runtime, project):
    return runtime._session_scope() if policy(runtime, project)["mode"] == "phase" else None


def instruction(packet):
    from .review_packet import review_instruction
    return PHASE_PREFIX + review_instruction(packet)


def retry(runtime, project, problem_id):
    """Only the explicit retry entry may reopen an unresolved frozen batch."""
    from .runtime import ContractError
    row = runtime.db.execute("SELECT * FROM phase_review_batches WHERE project=? AND problem_id=?",
                             (project, problem_id)).fetchone()
    if not row:
        return
    cfg = policy(runtime, project)
    if (row["host_scope"] != runtime._session_scope() or row["session_epoch"] != runtime.session_epoch()
            or row["governance_epoch"] != runtime.host_epoch() or row["policy_version"] != cfg["version"]
            or cfg["mode"] != "phase" or row["state"] not in {"pending", "blocked"}):
        raise ContractError("phase retry context changed; original unknown result is retained")
    runtime.db.execute("UPDATE phase_review_batches SET state='pending' WHERE problem_id=?", (problem_id,))


def arm(runtime, project, conversation, previous, task):
    """Only an actual active -> delivered transition creates a phase, never a fence."""
    from .runtime import digest
    if (scope(runtime, project) is None or not previous or previous["status"] != "active"
            or task["status"] != "delivered" or previous["id"] != task["id"]):
        return
    start = runtime.db.execute("SELECT updated FROM task_states WHERE project=? AND id=? AND revision=? "
        "AND status='active' ORDER BY write_version DESC LIMIT 1",
        (project, previous["id"], previous["revision"])).fetchone()
    runtime.db.execute("INSERT OR IGNORE INTO review_phases VALUES(?,?,?,?,?,?,?,?,?,?,'awaiting_stop',NULL,?,0)",
        (digest([project, runtime._session_scope(), task["id"], task["write_version"]])[:32], project,
         runtime._session_scope(), conversation, task["id"], task["revision"], task["write_version"],
         start[0] if start else previous["updated"], runtime.session_epoch(), runtime.host_epoch(), time.time()))


def seal(runtime, project, conversation, event_id):
    from .runtime import encode
    if scope(runtime, project) is None:
        return
    task = runtime.task(project, conversation)
    if not task or task["status"] != "delivered":
        return
    row = runtime.db.execute("SELECT * FROM review_phases WHERE project=? AND host_scope=? AND conversation=? "
        "AND task_id=? AND task_revision=? AND state='awaiting_stop' AND session_epoch=? AND governance_epoch=? "
        "ORDER BY delivery_version DESC LIMIT 1", (project, runtime._session_scope(), conversation, task["id"],
        task["revision"], runtime.session_epoch(), runtime.host_epoch())).fetchone()
    if not row:
        return
    # Bound selection to the real stage. Old delivered tasks cannot generate new phases.
    stop = runtime.db.execute("SELECT created FROM events WHERE project=? AND id=?", (project, event_id)).fetchone()
    if not stop:
        return
    from .work_summaries import _PUBLIC_PREVIEW_FILTER
    # Segments are captured AFTER their parent Stop. Use the original message
    # timestamp and prefer complete segments to the duplicated/truncated preview.
    sources = (" FROM events e LEFT JOIN work_memories m ON m.project=e.project AND m.event_id=e.id "
        "LEFT JOIN events origin ON e.type='PublicMessageSegment' AND origin.project=e.project "
        "AND origin.id=json_extract(e.payload,'$.public_message_id') "
        "WHERE e.project=? AND e.conversation=? AND e.task_id=? AND e.task_revision=? AND e.agent IS NULL "
        "AND COALESCE(origin.created,e.created)>=? AND COALESCE(origin.created,e.created)<=? "
        "AND e.type IN ('UserPromptSubmit','PublicMessageSegment','Stop') " + _PUBLIC_PREVIEW_FILTER)
    params = (project, conversation, task["id"], task["revision"], row["start_time"], stop[0])
    refs = runtime.db.execute("SELECT e.id,length(e.payload) AS chars" + sources +
        " ORDER BY COALESCE(origin.created,e.created),e.created,e.rowid LIMIT 2000", params).fetchall()
    chunks, chunk, chars = [], [], 0
    for ref in refs:
        if chunk and (len(chunk) >= 6 or chars + ref["chars"] > 4000):
            chunks.append(chunk)
            chunk, chars = [], 0
        chunk.append(ref["id"])
        chars += ref["chars"]
    if chunk:
        chunks.append(chunk)
    for index, chunk in enumerate(chunks):
        runtime._problem(project, conversation, "阶段公开记录：" + task["goal"][:240] + " [" + row["id"] + "] part " + str(index),
                         "mixed", chunk, task_binding=task)
    total = runtime.db.execute("SELECT COUNT(*)" + sources, params).fetchone()[0]
    runtime.db.execute("UPDATE review_phases SET state='sealed',event_id=?,omitted_sources=? WHERE id=?",
                       (event_id, max(0, total-len(refs)), row["id"]))
    prepare(runtime, project)


def status(runtime, project):
    cfg = policy(runtime, project)
    exists = runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name='review_phases'").fetchone()
    current = (project, runtime._session_scope(), runtime.session_epoch(), runtime.host_epoch())
    rows = runtime.db.execute("SELECT CASE state WHEN 'continuing' THEN 'partial' ELSE state END s,COUNT(*) n "
        "FROM review_phases WHERE project=? AND host_scope=? AND session_epoch=? AND governance_epoch=? GROUP BY s", current).fetchall() if exists else []
    old = runtime.db.execute("SELECT COUNT(*) FROM review_phases WHERE project=? AND host_scope=? "
        "AND (session_epoch!=? OR governance_epoch!=?)", current).fetchone()[0] if exists else 0
    source_limit = runtime.db.execute("SELECT COALESCE(SUM(omitted_sources),0) FROM review_phases WHERE project=? "
        "AND host_scope=? AND session_epoch=? AND governance_epoch=?", current).fetchone()[0] if exists else 0
    recent = runtime.db.execute("SELECT problem_id,state,substr(result,1,60001) FROM phase_review_batches WHERE project=? "
        "AND host_scope=? ORDER BY created DESC LIMIT 3", (project, runtime._session_scope())).fetchall() if exists else []
    return {"policy": cfg, "host_scope": runtime._session_scope(), "phases": {r[0]: r[1] for r in rows},
            "historical_boundary_phases": old, "source_scan_omitted": source_limit,
            "token_budget_is_estimated": True, "historical_imports_included": False,
            "recent_batches": [{"id": row[0], "state": row[1],
                "coverage_complete": _result(row[2]).get("coverage_complete"),
                "items": [{"problem_id": item.get("problem_id"), "status": item.get("status"),
                    "reason": item.get("reason", "")[:250], "candidate_ids": item.get("candidate_ids", [])}
                    for item in _result(row[2]).get("items", [])[:8] if isinstance(item, dict)]} for row in recent]}


def _result(value):
    try:
        body = json.loads(value or "{}")
        return body if isinstance(body, dict) and isinstance(body.get("items", []), list) else {}
    except (ValueError, TypeError):
        return {}


def source_conversation(runtime, project, problem_id, fallback):
    if not runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name='phase_review_batches'").fetchone():
        return fallback
    row = runtime.db.execute("SELECT packet FROM phase_review_batches WHERE project=? AND problem_id=?",
                             (project, problem_id)).fetchone()
    return json.loads(row[0]).get("executor_conversation") if row else fallback


def _case_source(runtime, project, phase):
    stop = runtime.db.execute("SELECT created FROM events WHERE id=?", (phase["event_id"],)).fetchone()[0]
    sql = (" FROM problems p WHERE p.project=? AND p.conversation=? AND p.task_id=? AND p.task_revision=? "
        "AND NOT EXISTS(SELECT 1 FROM work_summary_links w WHERE w.problem_id=p.id) "
        "AND NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.problem_id=p.id) "
        "AND EXISTS(SELECT 1 FROM problem_events pe JOIN events e ON e.id=pe.event "
        "LEFT JOIN events origin ON e.type='PublicMessageSegment' AND origin.project=e.project "
        "AND origin.id=json_extract(e.payload,'$.public_message_id') WHERE pe.problem=p.id "
        "AND COALESCE(origin.created,e.created)>=? AND COALESCE(origin.created,e.created)<=?) ")
    return sql, (project, phase["conversation"], phase["task_id"], phase["task_revision"], phase["start_time"], stop)


def _scan_progress(runtime, project, phase, cfg):
    from .runtime import digest
    sql, args = _case_source(runtime, project, phase)
    # Review outcomes remove eligible rows, but do not move this source version.
    # Real evidence/context revisions and changed input settings restart a pass.
    facts = runtime.db.execute("SELECT COUNT(*),COALESCE(SUM(p.revision),0),"
        "COALESCE(SUM((SELECT revision FROM admin_case_epochs c WHERE c.project=p.project AND c.problem=p.id)),0)" + sql, args).fetchone()
    knowledge = runtime.db.execute("SELECT generation,source_epoch FROM projects WHERE id=?", (project,)).fetchone()
    candidates = runtime.db.execute("SELECT candidates FROM admin_queue_epochs WHERE project=?", (project,)).fetchone()
    signature = digest([list(facts), list(knowledge) if knowledge else None, candidates[0] if candidates else 0,
                        cfg["version"], runtime.admin_effective_config(project)["max_input_chars"],
                        runtime.admin_policy(project)["policy_version"]])
    row = runtime.db.execute("SELECT * FROM phase_scan_progress WHERE phase_id=?", (phase["id"],)).fetchone()
    changed = not row or row["signature"] != signature
    if changed:
        runtime.db.execute("INSERT INTO phase_scan_progress VALUES(?,?,NULL,0,0) ON CONFLICT(phase_id) "
            "DO UPDATE SET signature=excluded.signature,cursor=NULL,deferred=0,exhausted=0", (phase["id"], signature))
        row = runtime.db.execute("SELECT * FROM phase_scan_progress WHERE phase_id=?", (phase["id"],)).fetchone()
    return dict(row), changed


def prepare(runtime, project, *, force=False, _page_budget=3):
    """Called only in a writer transaction; viewing status never creates jobs."""
    from .admin import _fingerprint, _review_configuration, _executor_semantics
    from .admin_decisions import find_receipt, review_signature, dependency_signature
    from .admin_review_flow import evidence_readiness
    from .runtime import digest, encode
    cfg = policy(runtime, project)
    if cfg["mode"] != "phase":
        return
    for old in runtime.db.execute("SELECT b.* FROM phase_review_batches b WHERE b.project=? AND b.host_scope=? "
        "AND b.state='pending'", (project, runtime._session_scope())).fetchall():
        bad = runtime.db.execute("SELECT 1 FROM admin_calls WHERE problem=? AND state IN "
            "('failed','timeout_unknown','account_changed_unknown','orphaned_unknown') AND retry_allowed=0", (old["problem_id"],)).fetchone()
        if bad:
            runtime.db.execute("UPDATE phase_review_batches SET state='blocked' WHERE problem_id=?", (old["problem_id"],))
            continue
        active = runtime.db.execute("SELECT 1 FROM admin_calls WHERE problem=? AND state IN ('reserved','launch_intent','started')",
                                    (old["problem_id"],)).fetchone()
        changed = runtime.db.execute("SELECT 1 FROM phase_review_members m JOIN problems p ON p.id=m.problem_id "
            "WHERE m.batch_id=? AND m.revision!=p.revision", (old["problem_id"],)).fetchone()
        if not changed and not active:
            frozen = json.loads(old["packet"])
            try:
                live_cfg = _review_configuration(runtime.admin_effective_config(project),
                    runtime.resolve_admin_executor(project, frozen["executor_conversation"]))
                changed = any(review_signature(runtime.review_packet(project, item["problem"]["id"],
                        max_chars=frozen["item_max_chars"]), live_cfg,
                        _fingerprint(runtime.review_packet(project, item["problem"]["id"], max_chars=frozen["item_max_chars"])))
                    != review_signature(item, live_cfg, _fingerprint(item)) for item in frozen["items"])
                if not changed:
                    for duplicate in runtime.db.execute("SELECT * FROM phase_review_members WHERE batch_id=? "
                                                        "AND state='duplicate_pending'", (old["problem_id"],)).fetchall():
                        fresh = runtime.review_packet(project, duplicate["problem_id"], max_chars=frozen["item_max_chars"])
                        if dependency_signature(fresh, _fingerprint(fresh)) != duplicate["context_signature"]:
                            changed = True
                            break
            except ValueError:
                # A missing current executor is not permission to select another model.
                continue
        if changed and not active:
            runtime.db.execute("UPDATE phase_review_batches SET state='stale' WHERE problem_id=?", (old["problem_id"],))
            runtime.db.execute("UPDATE problems SET status='superseded' WHERE id=?", (old["problem_id"],))
            for identity in json.loads(old["phase_ids"]):
                runtime.db.execute("UPDATE review_phases SET state='sealed' WHERE id=? AND state='queued'", (identity,))
                runtime.db.execute("DELETE FROM phase_scan_progress WHERE phase_id=?", (identity,))
    phases = runtime.db.execute("SELECT * FROM review_phases WHERE project=? AND host_scope=? AND state IN ('sealed','partial','continuing') "
        "AND session_epoch=? AND governance_epoch=? ORDER BY (state='sealed') DESC,created,id LIMIT 100",
        (project, runtime._session_scope(), runtime.session_epoch(), runtime.host_epoch())).fetchall()
    groups, progress = {}, {}
    for phase in phases:
        scan, dirty = _scan_progress(runtime, project, phase, cfg)
        progress[phase["id"]] = scan
        if phase["state"] in {"partial", "continuing"}:
            if scan["exhausted"] and not dirty:
                state = "partial" if scan["deferred"] or phase["omitted_sources"] else "completed"
                runtime.db.execute("UPDATE review_phases SET state=? WHERE id=?", (state, phase["id"]))
                continue
            if dirty and phase["state"] == "partial":
                runtime.db.execute("UPDATE review_phases SET state='continuing' WHERE id=?", (phase["id"],))
                phase = dict(phase, state="continuing")
        try:
            plan = runtime.resolve_admin_executor(project, phase["conversation"])
        except ValueError:
            continue
        key = digest(_executor_semantics(plan))
        groups.setdefault(key, []).append(phase)
    phases = next((group for group in groups.values() if force or any(r["state"] == "continuing" for r in group)
                   or sum(r["state"] == "sealed" for r in group) >= cfg["every_phases"]), [])
    fresh = [row for row in phases if row["state"] == "sealed"]
    if not phases or len(fresh) < cfg["every_phases"] and not force and not any(r["state"] == "continuing" for r in phases):
        return
    if runtime.db.execute("SELECT 1 FROM phase_review_batches WHERE project=? AND host_scope=? AND state='pending' "
                          "AND session_epoch=? AND governance_epoch=?",
                          (project, runtime._session_scope(), runtime.session_epoch(), runtime.host_epoch())).fetchone():
        return
    phases = ([row for row in phases if row["state"] == "continuing"][:5] + fresh[:cfg["every_phases"]]
              + [row for row in phases if row["state"] == "partial"][:5]) if not force else phases
    phases = phases[:20]
    ids = [r["id"] for r in phases]
    ordinal = runtime.db.execute("SELECT COUNT(*) FROM phase_review_batches WHERE project=?", (project,)).fetchone()[0]
    batch_key = digest([project, runtime._session_scope(), ids, cfg["version"], ordinal])[:32]
    conversation = "phase-review:" + batch_key
    request, _ = runtime._event(project, conversation, "PhaseReviewRequested", {"phase_ids": ids},
                                event_id="phase-review:" + batch_key, outcome="reported")
    parent = runtime._problem(project, conversation, "阶段集中审核 [" + batch_key + "]", "mixed", [request["id"]])
    items, seen, omitted, reused, used = [], set(), [], [], 0
    duplicates, reused_ready, scanned_remainder, prior_deferred = [], set(), 0, 0
    scan_updates = {}
    try:
        review_cfg = _review_configuration(runtime.admin_effective_config(project),
                                            runtime.resolve_admin_executor(project, phases[-1]["conversation"]))
    except ValueError:
        review_cfg = None
    char_limit = runtime.admin_effective_config(project)["max_input_chars"] - 3000
    for phase in phases:
        source_sql, source_args = _case_source(runtime, project, phase)
        source_sql += (
            "AND NOT EXISTS(SELECT 1 FROM phase_review_members m WHERE m.project=p.project AND m.problem_id=p.id "
            "AND m.revision=p.revision AND m.state IN ('candidate','skip','governance')) ")
        scan = progress[phase["id"]]
        previous_key = json.loads(scan["cursor"]) if scan["cursor"] else [-1, 0, ""]
        rows = runtime.db.execute("WITH eligible AS (SELECT p.id,p.knowledge_id,p.created,CASE "
            "WHEN p.knowledge_id IS NOT NULL THEN 0 WHEN p.summary LIKE '阶段公开记录：%' THEN 1 ELSE 2 END priority" +
            source_sql + ") SELECT * FROM eligible WHERE (priority,created,id)>(?,?,?) "
            "ORDER BY priority,created,id LIMIT 101", (*source_args, *previous_key)).fetchall()
        more = len(rows) > 100
        rows = rows[:100]
        deferred = 0
        prior_deferred += scan["deferred"]
        last_key = previous_key
        for source_index, source in enumerate(rows):
            if used >= char_limit - 1000 or len(items) >= 8:
                omitted.extend(r["id"] for r in rows[source_index:] if r["id"] not in seen)
                more = True
                break
            current_key = [source["priority"], source["created"], source["id"]]
            before_current = last_key
            last_key = current_key
            if source["id"] in seen:
                continue
            seen.add(source["id"])
            blocked = runtime.db.execute("SELECT 1 FROM admin_calls a WHERE a.project=? AND a.problem=? AND retry_allowed=0 "
                "AND state NOT IN ('not_started','deduplicated') AND NOT EXISTS(SELECT 1 FROM review_receipts r "
                "WHERE r.project=a.project AND r.call_id=a.id)", (project, source["id"])).fetchone()
            if blocked:
                omitted.append(source["id"])
                deferred += 1
                continue
            packet = runtime.review_packet(project, source["id"], max_chars=max(1000, char_limit))
            if packet.get("incomplete") or evidence_readiness(packet):
                omitted.append(source["id"])
                deferred += 1
                continue
            signature = _fingerprint(packet)
            if review_cfg:
                from .admin_workload import hold_unchanged_review
                if hold_unchanged_review(runtime, project, packet, signature, review_cfg):
                    reused.append(source["id"])
                    omitted.append(source["id"])
                    deferred += 1
                    continue
            blocked = runtime.db.execute("SELECT 1 FROM phase_review_members m JOIN admin_calls a "
                "ON a.project=m.project AND a.problem=m.batch_id WHERE m.project=? AND m.evidence_signature=? "
                "AND a.retry_allowed=0 AND a.state NOT IN ('not_started','deduplicated') "
                "AND NOT EXISTS(SELECT 1 FROM review_receipts r WHERE r.project=a.project AND r.call_id=a.id)",
                (project, signature)).fetchone()
            if blocked:
                omitted.append(source["id"])
                deferred += 1
                continue
            previous = find_receipt(runtime, project, review_signature(packet, review_cfg, signature)) if review_cfg else None
            if previous:
                from .admin_decisions import persist_receipt, receipt_for_problem
                waiting = previous["decision"] == "needs_evidence"
                existing = receipt_for_problem(runtime, project, source["id"], packet["problem"]["revision"],
                                               review_signature(packet, review_cfg, signature))
                if previous["problem"] != source["id"] and not existing:
                    persist_receipt(runtime, project, packet, signature, review_signature(packet, review_cfg, signature), None,
                        {"decision": "needs_evidence" if waiting else "skip",
                         "reason": "Equivalent reviewed material; not independent verification",
                         "previous_decision": previous["decision"], "duplicate_of": previous["call_id"]},
                        duplicate_of=previous["call_id"])
                if waiting:
                    omitted.append(source["id"])
                    deferred += 1
                    reused.append(source["id"])
                    continue
                duplicates.append(packet)
                reused_ready.add(source["id"])
                reused.append(source["id"])
                continue
            if any(_fingerprint(item) == signature and dependency_signature(item, signature) == dependency_signature(packet, signature)
                   for item in items):
                reused.append(source["id"])
                duplicates.append(packet)
                continue  # Comparison reuse only, never independent verification.
            cost = len(encode(packet))
            if used + cost > char_limit or len(items) >= 8:
                if items:
                    last_key = before_current
                    more = True
                    break
                omitted.append(source["id"])
                deferred += 1
                continue
            # Test the actual wrapper, not just the sum of member characters.
            # An oversized member remains uncovered; it cannot pin a ready sibling.
            candidate_body = {"problem": parent, "task": None, "events": [], "related_knowledge": [],
                "related_candidates": [], "purpose": "phase_review", "executor_conversation": phases[-1]["conversation"],
                "item_max_chars": max(1000, char_limit), "phase_ids": ids, "items": items + [packet],
                "incomplete": False, "evidence_truncated": False, "omitted_problem_ids": [],
                "reused_problem_ids": [], "omitted_source_count": 0, "coverage_complete": False}
            from .admin_usage import estimate_reservation
            # Leave room for bounded diagnostic identifiers appended after selection.
            estimate = estimate_reservation(runtime, project, instruction(candidate_body) + " " * 3000, phase=True)
            if not estimate["admitted"]:
                if items:
                    last_key = before_current
                    more = True
                    break
                omitted.append(source["id"])
                deferred += 1
                continue
            items.append(packet)
            used += cost
        scan_updates[phase["id"]] = {"cursor": last_key, "more": more,
            "deferred": scan["deferred"] + deferred}
        scanned_remainder += int(more)
    body = {"problem": parent, "task": None, "events": [], "related_knowledge": [], "related_candidates": [],
            "purpose": "phase_review", "executor_conversation": phases[-1]["conversation"], "item_max_chars": max(1000, char_limit),
            "phase_ids": ids, "items": items, "incomplete": False,
            "evidence_truncated": False, "omitted_problem_ids": omitted[:32], "reused_problem_ids": reused[:32],
            "omitted_problem_count": len(omitted) + prior_deferred, "reused_problem_count": len(reused),
            "case_scan_continuation": bool(scanned_remainder),
            "omitted_source_count": sum(p["omitted_sources"] for p in phases),
            "coverage_complete": not omitted and not prior_deferred and not scanned_remainder and not any(p["omitted_sources"] for p in phases)}
    for identity, scan in scan_updates.items():
        runtime.db.execute("UPDATE phase_scan_progress SET cursor=?,deferred=?,exhausted=? WHERE phase_id=?",
            (encode(scan["cursor"]), scan["deferred"], int(not scan["more"]), identity))
    # The final diagnostics are bounded as well. No queued batch may be too big
    # for the very same admission function that will launch it.
    from .admin_usage import estimate_reservation
    while body["items"] and (len(instruction(body)) > runtime.admin_effective_config(project)["max_input_chars"]
                             or not estimate_reservation(runtime, project, instruction(body), phase=True)["admitted"]):
        removed = body["items"].pop()
        body["omitted_problem_ids"] = (body["omitted_problem_ids"] + [removed["problem"]["id"]])[:32]
        body["omitted_problem_count"] += 1
        body["coverage_complete"] = False
        for identity in ids:
            runtime.db.execute("DELETE FROM phase_scan_progress WHERE phase_id=?", (identity,))
    included_signatures = {_fingerprint(item) for item in body["items"]}
    supported_duplicates = []
    for item in duplicates:
        identity = item["problem"]["id"]
        if identity in reused_ready or _fingerprint(item) in included_signatures:
            supported_duplicates.append(item)
        else:
            body["omitted_problem_ids"] = (body["omitted_problem_ids"] + [identity])[:32]
            body["omitted_problem_count"] += 1
            body["coverage_complete"] = False
    duplicates = supported_duplicates
    runtime.db.execute("INSERT INTO phase_review_batches VALUES(?,?,?,?,?,?,?,?,'pending',?,NULL)",
        (project, parent["id"], runtime._session_scope(), runtime.session_epoch(), runtime.host_epoch(), cfg["version"],
         encode(ids), encode(body), time.time()))
    for item in items:
        p = item["problem"]
        signature = _fingerprint(item)
        runtime.db.execute("INSERT INTO phase_review_members VALUES(?,?,?,?,?,'pending',?)",
            (project, parent["id"], p["id"], p["revision"], signature, dependency_signature(item, signature)))
    for item in duplicates:
        p = item["problem"]
        signature = _fingerprint(item)
        runtime.db.execute("INSERT OR IGNORE INTO phase_review_members VALUES(?,?,?,?,?,?,?)",
            (project, parent["id"], p["id"], p["revision"], signature,
             'skip' if p["id"] in reused_ready else 'duplicate_pending', dependency_signature(item, signature)))
    for identity in ids:
        runtime.db.execute("UPDATE review_phases SET state='queued' WHERE id=?", (identity,))
    if not items:
        runtime.db.execute("UPDATE phase_review_members SET state='skip' WHERE batch_id=? AND state='duplicate_pending'",
                           (parent["id"],))
        runtime.db.execute("UPDATE problems SET status='deferred' WHERE id=?", (parent["id"],))
        runtime.db.execute("UPDATE phase_review_batches SET state='no_ready_material' WHERE problem_id=?", (parent["id"],))
        for identity in ids:
            state = ("continuing" if scan_updates[identity]["more"] else
                     "partial" if not body["coverage_complete"] else "completed")
            runtime.db.execute("UPDATE review_phases SET state=? WHERE id=?", (state, identity))
        if scanned_remainder and _page_budget > 1:
            prepare(runtime, project, _page_budget=_page_budget-1)


def packet(runtime, project, problem_id):
    from .runtime import ContractError
    row = runtime.db.execute("SELECT * FROM phase_review_batches WHERE project=? AND problem_id=?",
                             (project, problem_id)).fetchone()
    if not row:
        return None
    cfg = policy(runtime, project)
    if (row["state"] != "pending" or row["host_scope"] != runtime._session_scope()
            or row["session_epoch"] != runtime.session_epoch() or row["governance_epoch"] != runtime.host_epoch()
            or row["policy_version"] != cfg["version"] or cfg["mode"] != "phase"):
        raise ContractError("phase review context changed")
    return json.loads(row["packet"])


def accept(runtime, project, reserved, response):
    from .admin import _accept_response, _fingerprint, _review_configuration
    from .mcp import validate
    from .admin_decisions import persist_receipt, review_signature, dependency_signature
    from .admin_lifecycle import validate_call
    from .runtime import ContractError, encode
    parent = reserved["packet"]
    validate(response, response_schema())
    live = packet(runtime, project, parent["problem"]["id"])
    validate_call(runtime, reserved["call_id"], reserved["owner"], reserved["host_epoch"])
    runtime.assert_admin_executor_snapshot(project, reserved["provider_plan"], reserved["provider_plan"].get("conversation"))
    current = runtime.db.execute("SELECT revision FROM problems WHERE project=? AND id=?", (project, parent["problem"]["id"])).fetchone()
    if live != parent or not current or current[0] != parent["problem"]["revision"] or len(encode(response).encode("utf-8")) > 60000:
        raise ContractError("phase review snapshot or output changed")
    expected = {item["problem"]["id"]: item for item in parent["items"]}
    values = response.get("items")
    if not isinstance(values, list) or len(values) != len(expected) or {v.get("problem_id") for v in values} != set(expected):
        raise ContractError("phase review must return exactly one result per visible case")
    results = []
    cfg = _review_configuration(runtime.admin_effective_config(project), reserved["provider_plan"])
    cfg["auto_publish"] = reserved["publication_enabled_at_reservation"]
    cfg["publication_mode"] = reserved["publication_mode_at_reservation"]
    # Compare every frozen case before publishing any sibling result. Internal writes
    # must not appear to be concurrent external evidence changes in later items.
    for item in expected.values():
        fresh = runtime.review_packet(project, item["problem"]["id"], max_chars=parent["item_max_chars"])
        if review_signature(fresh, cfg, _fingerprint(fresh)) != review_signature(item, cfg, _fingerprint(item)):
            raise ContractError("phase case evidence or comparison changed")
    duplicates = []
    for duplicate in runtime.db.execute("SELECT * FROM phase_review_members WHERE batch_id=? AND state='duplicate_pending'",
                                        (parent["problem"]["id"],)).fetchall():
        fresh = runtime.review_packet(project, duplicate["problem_id"], max_chars=parent["item_max_chars"])
        if (fresh["problem"]["revision"] != duplicate["revision"] or _fingerprint(fresh) != duplicate["evidence_signature"]
                or dependency_signature(fresh, duplicate["evidence_signature"]) != duplicate["context_signature"]):
            raise ContractError("duplicate phase evidence or comparison changed")
        duplicates.append((duplicate, fresh))
    source_results = {}
    for value in values:
        item = expected[value["problem_id"]]
        child = dict(reserved, packet=item, phase_context=None, multi_candidate_allowed=True, frozen_batch_verified=True,
                     evidence_signature=_fingerprint(item), review_signature=review_signature(item, cfg, _fingerprint(item)),
                     selected={"id": item["problem"]["id"], "revision": item["problem"]["revision"],
                               "source_task_updated": None})
        from .admin_workload import evidence_wait_signature
        child["evidence_wait_signature"] = evidence_wait_signature(item, cfg, child["evidence_signature"])
        result = _accept_response(runtime, project, child, {k: v for k, v in value.items() if k != "problem_id"})
        source_results[(child["evidence_signature"], dependency_signature(item, child["evidence_signature"]))] = result
        results.append(result)
        runtime.db.execute("UPDATE phase_review_members SET state=? WHERE batch_id=? AND problem_id=?",
                           (result["status"], parent["problem"]["id"], item["problem"]["id"]))
    for duplicate, fresh in duplicates:
        source_result = source_results[(duplicate["evidence_signature"], duplicate["context_signature"])]
        decision = "needs_evidence" if source_result["status"] == "needs_evidence" else "skip"
        persist_receipt(runtime, project, fresh, duplicate["evidence_signature"],
            review_signature(fresh, cfg, duplicate["evidence_signature"]), reserved["call_id"],
            {"decision": decision, "reason": "Equivalent reviewed material; not independent verification",
             "duplicate_of": reserved["call_id"]}, duplicate_of=reserved["call_id"])
        runtime.db.execute("UPDATE phase_review_members SET state=? WHERE batch_id=? AND problem_id=?",
                           (decision, parent["problem"]["id"], duplicate["problem_id"]))
    aggregate = ("candidate" if any(r["status"] == "candidate" for r in results) else
                 "needs_evidence" if any(r["status"] == "needs_evidence" for r in results) else
                 "governance" if any(r["status"] == "governance" for r in results) else "skip")
    response["decision"] = aggregate
    result = {"status": aggregate, "decision": aggregate,
              "reason": "phase batch completed; independent per-case receipts retained", "call_id": reserved["call_id"],
              "purpose": "phase_review", "items": results, "coverage_complete": parent["coverage_complete"],
              "omitted_problem_ids": parent["omitted_problem_ids"]}
    # Keep the single actual call authoritative; child results do not create paid calls.
    persist_receipt(runtime, project, parent, reserved["evidence_signature"], reserved["review_signature"],
                    reserved["call_id"], result)
    runtime.db.execute("UPDATE phase_review_batches SET state='completed',result=? WHERE problem_id=?",
                       (encode(result), parent["problem"]["id"]))
    for identity in parent["phase_ids"]:
        phase = runtime.db.execute("SELECT * FROM review_phases WHERE id=?", (identity,)).fetchone()
        source_sql, source_args = _case_source(runtime, project, phase)
        waiting = runtime.db.execute("SELECT 1" + source_sql +
            "AND EXISTS(SELECT 1 FROM phase_review_members m WHERE m.batch_id=? AND m.problem_id=p.id "
            "AND m.revision=p.revision AND m.state='needs_evidence') LIMIT 1",
            (*source_args, parent["problem"]["id"])).fetchone()
        if waiting:
            # A completed main review may still await proof. Keep that phase
            # dormant but wakeable when its real source revision changes.
            runtime.db.execute("UPDATE phase_scan_progress SET deferred=deferred+1 WHERE phase_id=?", (identity,))
        runtime.db.execute("UPDATE review_phases SET state=? WHERE id=?",
                           ("completed" if parent["coverage_complete"] and not waiting else "continuing", identity))
    prepare(runtime, project)
    return result
