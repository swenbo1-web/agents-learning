"""可检索的工作观察与文件索引；权威证据仍为已有 events/operations。"""
from __future__ import annotations

from contextlib import contextmanager
import json
import re
import time
from .automatic_relevance import has_constraint as _constraint_request

from .work_files import checkout_key, checkout_root, file_links_from_payload, normalize_file_target, current_file_version


WORK_MEMORY_DEFAULTS = {"enabled": True, "automatic_context": True, "max_context_chars": 600,
                        "max_items": 3, "summary_enabled": False, "summary_strategy": "balanced", "summary_min_events": 2,
                        "summary_max_events": 12}
WORK_INDEX_VERSION = 2
WORK_READ_FILE_CHECK_LIMIT = 64


def _read_file_version(relative_path, workspace):
    """Bounded metadata observation; known absence differs from unreadable state."""
    try:
        root = checkout_root(workspace)
        target = normalize_file_target(relative_path, root, observe_version=False)
        if target is None:
            return {"status": "unavailable"}
        stat = (root / target["relative_path"]).stat()
        return {"status": "stat_observed", "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except FileNotFoundError:
        return {"status": "missing"}
    except (OSError, ValueError, RuntimeError):
        return {"status": "unavailable"}


def _read_file_check(runtime, project, memory_id, workspace):
    """A receipt checks all recorded associations, independently of three-file UI."""
    from .runtime import digest
    rows = runtime.db.execute(
        "SELECT f.event_id,f.checkout_key,f.path_key,f.relative_path,f.relation,f.version "
        "FROM work_memory_sources s JOIN work_file_links f "
        "ON f.project=s.project AND f.event_id=s.event_id "
        "WHERE s.project=? AND s.memory_id=? "
        "ORDER BY f.event_id,f.checkout_key,f.path_key,f.relation LIMIT ?",
        (project, memory_id, WORK_READ_FILE_CHECK_LIMIT + 1)).fetchall()
    if len(rows) > WORK_READ_FILE_CHECK_LIMIT:
        return {"complete": False, "reason": "file_check_limit"}
    if not rows:
        return {"complete": True, "version": digest([])}
    if workspace is None:
        return {"complete": False, "reason": "workspace_unavailable"}
    try:
        current_checkout = checkout_key(workspace)
    except (OSError, ValueError, RuntimeError):
        return {"complete": False, "reason": "workspace_unavailable"}
    versions, observations = {}, []
    for row in rows:
        if row["checkout_key"] != current_checkout:
            return {"complete": False, "reason": "checkout_unavailable"}
        path = row["relative_path"]
        if path not in versions:
            versions[path] = _read_file_version(path, workspace)
        current = versions[path]
        if current.get("status") not in {"stat_observed", "missing"}:
            return {"complete": False, "reason": "file_state_unavailable"}
        observations.append([*tuple(row), current])
    return {"complete": True, "version": digest(observations)}


def _read_metadata_preview(result, max_chars, *, paginated=False):
    """Reserve page space by shortening previews, never identity or live checks."""
    from .work_read_delivery import coverage_limits
    original_limits = coverage_limits(result.get("coverage"))
    # A fixed 128-character reserve left small pages almost entirely metadata.
    # Repeated pages need only the live identity/checks, not another index card.
    target = max_chars - 128
    if paginated:
        target = 512 if result.get("offset") else max(512, max_chars // 4)
    while len(_json(result)) > target and result["files"]:
        result["files"].pop()
        result["files_omitted"] = result["files_count"] - len(result["files"])
    if len(_json(result)) > target and len(result["source_event_refs"]) > 1:
        preview = dict(result, source_refs_omitted=len(result["source_event_refs"]) - 1,
                       source_event_refs=result["source_event_refs"][:1], source_refs_truncated=True)
        if len(_json(preview)) < len(_json(result)):
            result.update(preview)
    if len(_json(result)) > target and result.get("file_state"):
        result["file_state_omitted"] = len(result["file_state"])
        result["file_state"] = None
    if len(_json(result)) > target and result.get("coverage"):
        result["coverage"] = original_limits
        result["coverage_omitted"] = True
    if len(_json(result)) > target and result.get("candidate_reference_risk"):
        result["candidate_reference_risk"] = [{"blocked": True, "details_omitted": True}]
    # Index-only hints and presentation fields do not establish source validity.
    # The immutable source_version includes the full source/risk snapshots.
    for key in ("file_focus", "summary_only", "read_required", "permission_restored",
                "created", "task_binding_known", "type", "coverage"):
        if len(_json(result)) <= target:
            break
        if key == "coverage" and original_limits:
            continue
        if key in result:
            result.pop(key)
            result["metadata_omitted"] = True
    if len(_json(result)) > target and len(result.get("title", "")) > 80:
        result["title"] = result["title"][:80]
        result["title_truncated"] = True
    if len(_json(result)) > target and result["source_event_refs"]:
        preview = dict(result, source_refs_omitted=result.get("source_refs_omitted", 0) + len(result["source_event_refs"]),
                       source_event_refs=[], source_refs_truncated=True)
        if len(_json(preview)) < len(_json(result)):
            result.update(preview)
    if paginated and len(_json(result)) > target and "title" in result:
        result.pop("title")
        result.pop("title_truncated", None)
        result["metadata_omitted"] = True


def validate_work_memory_config(value):
    from .runtime import ContractError
    if not isinstance(value, dict) or set(value) - set(WORK_MEMORY_DEFAULTS):
        raise ContractError("unsupported work_memory settings")
    result = dict(WORK_MEMORY_DEFAULTS, **value)
    for key in ("enabled", "automatic_context", "summary_enabled"):
        if not isinstance(result[key], bool):
            raise ContractError(f"work_memory.{key} must be boolean")
    if not isinstance(result["summary_strategy"], str) or result["summary_strategy"] not in {"balanced", "all_windows"}:
        raise ContractError("work_memory.summary_strategy must be balanced or all_windows")
    for key, minimum, maximum in (("max_context_chars", 128, 4000), ("max_items", 1, 10),
                                  ("summary_min_events", 1, 40), ("summary_max_events", 1, 40)):
        if isinstance(result[key], bool) or not isinstance(result[key], int) or not minimum <= result[key] <= maximum:
            raise ContractError(f"work_memory.{key} must be {minimum}..{maximum}")
    if result["summary_min_events"] > result["summary_max_events"]:
        raise ContractError("work_memory summary minimum exceeds maximum")
    return result


def install_work_memory(runtime):
    from .runtime import ContractError
    if not runtime.db.in_transaction:
        raise ContractError("work memory migration requires an existing write transaction")
    for sql in (
        "CREATE TABLE IF NOT EXISTS work_memories(project TEXT NOT NULL,id TEXT NOT NULL,event_id TEXT,revision INTEGER NOT NULL,body TEXT NOT NULL,source_hash TEXT NOT NULL,workspace_key TEXT NOT NULL,conversation TEXT NOT NULL,task_id TEXT,task_revision INTEGER,agent TEXT,created REAL NOT NULL,PRIMARY KEY(project,id),UNIQUE(project,event_id))",
        "CREATE INDEX IF NOT EXISTS work_memories_scope ON work_memories(project,conversation,created)",
        "CREATE INDEX IF NOT EXISTS work_public_message ON work_memories(project,conversation,json_extract(body,'$.public_message_id')) WHERE json_valid(body)",
        "CREATE TABLE IF NOT EXISTS work_event_checkouts(project TEXT NOT NULL,event_id TEXT NOT NULL,checkout_key TEXT NOT NULL,PRIMARY KEY(project,event_id))",
        "CREATE TABLE IF NOT EXISTS work_memory_sources(project TEXT NOT NULL,memory_id TEXT NOT NULL,event_id TEXT NOT NULL,source_hash TEXT NOT NULL,source_version TEXT NOT NULL,PRIMARY KEY(project,memory_id,event_id))",
        "CREATE VIRTUAL TABLE IF NOT EXISTS work_memory_fts USING fts5(project UNINDEXED,id UNINDEXED,text)",
        "CREATE TABLE IF NOT EXISTS work_file_links(project TEXT NOT NULL,event_id TEXT NOT NULL,checkout_key TEXT NOT NULL,relative_path TEXT NOT NULL,path_key TEXT NOT NULL,relation TEXT NOT NULL,version TEXT NOT NULL,PRIMARY KEY(project,event_id,checkout_key,path_key,relation))",
        "CREATE INDEX IF NOT EXISTS work_files_target ON work_file_links(project,checkout_key,path_key,event_id)",
        "CREATE TABLE IF NOT EXISTS work_file_hints(project TEXT NOT NULL,event_id TEXT NOT NULL,checkout_key TEXT NOT NULL,path_key TEXT NOT NULL,context_task_key TEXT NOT NULL,session_epoch INTEGER NOT NULL,generation INTEGER NOT NULL,created REAL NOT NULL,PRIMARY KEY(project,event_id,checkout_key,path_key))",
        "CREATE TABLE IF NOT EXISTS work_exposures(project TEXT NOT NULL,conversation TEXT NOT NULL,agent TEXT NOT NULL,memory_id TEXT NOT NULL,revision INTEGER NOT NULL,source_version TEXT NOT NULL,context_epoch INTEGER NOT NULL,task_key TEXT NOT NULL,delivery TEXT NOT NULL,method TEXT NOT NULL,created REAL NOT NULL,file_state TEXT,PRIMARY KEY(project,conversation,agent,memory_id))",
    ):
        runtime.db.execute(sql)
    if "file_state" not in {row[1] for row in runtime.db.execute("PRAGMA table_info(work_exposures)")}:
        runtime.db.execute("ALTER TABLE work_exposures ADD COLUMN file_state TEXT")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS work_index_state(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL)")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS work_index_pending(project TEXT NOT NULL,memory_id TEXT NOT NULL,PRIMARY KEY(project,memory_id))")
    state = runtime.db.execute("SELECT version FROM work_index_state WHERE id=1").fetchone()
    if state is None or state[0] != WORK_INDEX_VERSION:
        # Enqueue identities only; large evidence bodies are rebuilt in small writer batches.
        runtime.db.execute("INSERT OR IGNORE INTO work_index_pending SELECT project,id FROM work_memories")
        runtime.db.execute("INSERT INTO work_index_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version",
                           (WORK_INDEX_VERSION,))
    from .work_recovery import install
    install(runtime)


def _config(runtime):
    return dict(WORK_MEMORY_DEFAULTS, **runtime.config.get("work_memory", {}))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _budget(value, low=512, high=20000):
    from .runtime import ContractError
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ContractError(f"max_chars must be {low}..{high}")
    return value


@contextmanager
def _snapshot(runtime):
    owned = not runtime.db.in_transaction
    if owned:
        runtime.db.execute("BEGIN")
    try:
        yield
    finally:
        if owned:
            runtime.db.rollback()


@contextmanager
def _write_snapshot(runtime):
    if runtime.db.in_transaction:
        yield
    else:
        with runtime.transaction():
            yield


def _tool(payload):
    native = payload.get("toolCall")
    native = native if isinstance(native, dict) else {}
    name = payload.get("tool_name", native.get("name", ""))
    return name if isinstance(name, str) else "", payload.get("tool_input", native.get("args", {}))


def _learning_tool(name):
    name = str(name).lower()
    return ("agent_learning" in name or "agent-learning" in name
            or name.startswith("mcp__learning__"))


def _verification(name, args):
    if any(term in name.lower() for term in ("test", "build", "compile", "verify", "validate")):
        return True
    command = args.get("command", args.get("cmd", args.get("CommandLine", ""))) if isinstance(args, dict) else ""
    return bool(isinstance(command, str) and re.match(
        r"\s*(?:pytest\b|python(?:\.exe|[0-9.]*)?\s+(?:-B\s+)?-m\s+(?:unittest|pytest)\b|"
        r"npm\s+(?:test|run\s+(?:test|build))\b|cargo\s+(?:test|build|check)\b|"
        r"dotnet\s+(?:test|build)\b|cmake\s+--build\b)", command, re.I))


def _correction(text):
    return bool(isinstance(text, str) and re.match(
        r"\s*(?:不对|不是|纠正|更正|我说的是|你理解错|实际上|correction\b|actually\b|that's wrong\b)", text, re.I))


def _metadata_only_write(body):
    """Recognize only known write acknowledgements, not arbitrary tool output."""
    if body.get("type") != "file_change":
        return False
    text = body.get("summary", "")
    try:
        response = json.loads(text)
    except (ValueError, TypeError):
        response = text
    if isinstance(response, str):
        # Known patch acknowledgements are strings on several hosts. Require
        # the whole visible result to contain only the success envelope and
        # changed paths: warnings, errors and substantive explanations stay.
        lines = response.strip().splitlines()
        if lines and lines[0] == "Exit code: 0":
            lines.pop(0)
            if lines and re.fullmatch(r"Wall time: \d+(?:\.\d+)? seconds?", lines[0]):
                lines.pop(0)
            if lines and lines[0] == "Output:":
                lines.pop(0)
        coverage = body.get("coverage", {})
        return bool(isinstance(coverage, dict) and len(lines) >= 2
                    and lines[0] == "Success. Updated the following files:"
                    and all(re.fullmatch(r"[ADM] [^\r\n]+", line) for line in lines[1:])
                    and not coverage.get("truncated") and coverage.get("full_original_available") is not False)
    if not isinstance(response, dict) or not response:
        return False
    if any(key in response and response[key] is not True for key in ("written", "success")):
        return False
    if "exit_code" in response and (type(response["exit_code"]) is not int or response["exit_code"] != 0):
        return False
    if any(key in response and (type(response[key]) is not int or response[key] < 0)
           for key in ("chars", "bytes", "size")):
        return False
    if any(key in response and (not isinstance(response[key], str)
                               or re.fullmatch(r"[0-9a-fA-F]{16,128}", response[key]) is None)
           for key in ("sha256", "hash")):
        return False
    if any(key in response and type(response[key]) is not bool for key in ("created", "updated")):
        return False
    metadata = {"path", "file_path", "written", "success", "exit_code", "chars", "bytes",
                "size", "sha256", "hash", "encoding", "created", "updated"}
    return (set(response) <= metadata and all(not isinstance(value, (dict, list)) for value in response.values())
            and (response.get("written") is True or response.get("success") is True))


def _write_receipt_query(plan):
    """Keep acknowledgements discoverable when the user explicitly asks for them."""
    text = plan["meaningful"].casefold().replace("\\", "/")
    marker_text = text
    # A filename such as receipt_notes.py or archive/回执.md is an identity,
    # not a request to prefer acknowledgement records.
    for anchor in plan["exact_anchors"]:
        marker_text = marker_text.replace(anchor, " ")
    if "回执" in marker_text or re.search(
            r"\b(?:receipts?|ack|acknowledg(?:e)?ments?)\b", marker_text):
        return True
    # Fault descriptions remain requests for explanatory evidence. An explicit
    # receipt marker above still permits "the successful receipt before failure".
    if re.search(r"失败|故障|错误|异常|崩溃|报错|原因|导致|损坏|死锁|回滚|丢失|"
                 r"\b(?:fail(?:ed|ure|ing)?|errors?|crash(?:ed|es)?|bugs?|why|cause[ds]?|"
                 r"exceptions?|broken|regressions?|corrupt(?:ed|ion)?|deadlock|timeout)\b", text):
        return False
    bare = re.sub(r"\b(?:show|find|list|latest|last|recent|the|me|please)\b|"
                  r"查看|查询|查找|最近|最新|一次|一个|的|查|看", " ", text)
    bare = re.sub(r"[\s。.!！?？,，;；:：`]+", "", bare)
    if bare in {"apply_patch", "patch", "补丁", "write_file", "writefile", "edit_file", "editfile"}:
        return True
    return bool(re.search(r"(?:apply_patch|patch|补丁|写入|write).{0,32}"
                          r"(?:成功|success(?:ful(?:ly)?)?).{0,24}(?:输出|回包|output|result)", text))


def _work_priority(body):
    kind = body.get("type")
    if kind == "user_correction" or kind == "user_request" and _constraint_request(body.get("summary", "")):
        return 4
    return {"public_summary": 3, "work_summary": 3, "verification": 2, "tool_failure": 2,
            "file_change": 1}.get(kind, 0)


def _work_preview(text, query, max_chars=80):
    """Select a bounded original-text excerpt; never synthesize a conclusion."""
    from .knowledge_index import normalized_query_plan
    if len(text) <= max_chars:
        return text
    plan = normalized_query_plan(query)
    terms = [word for word in plan["terms"] if len(word) > 1]
    lowered = text.casefold()
    positions = [(lowered.find(word.casefold()), len(word)) for word in terms if word.casefold() in lowered]
    if not positions:
        return text[:max_chars]
    position, _ = max(positions, key=lambda item: item[1])
    line_start = text.rfind("\n", 0, position) + 1
    start = line_start if position - line_start < 24 else max(0, position - 16)
    return ("…" if start else "") + text[start:start + max_chars - int(bool(start))]


def _task_key(task_state):
    state = task_state or {}
    if not isinstance(state, dict):
        return _json(state)
    return _json({key: state.get(key) for key in ("id", "revision", "write_version", "assignment_id")})


def _exposure_task_key(task_state):
    """Display identity follows the task contract, not a progress write fence."""
    state = task_state or {}
    if not isinstance(state, dict):
        return _json(state)
    return _json({key: state.get(key) for key in ("id", "revision", "assignment_id")})


def _same_exposure_task(previous_key, task_state):
    # Older receipts include write_version. Ignore only that display field;
    # permission checks and file hints keep the complete _task_key contract.
    try:
        previous = json.loads(previous_key)
    except (TypeError, ValueError):
        return False
    return isinstance(previous, dict) and _exposure_task_key(previous) == _exposure_task_key(task_state)


class WorkMemoryMixin:
    def _work_verification_source(self, project, event, payload, raw, name):
        """Classify a missing-input callback using only matching recorded attempts."""
        if not name or not event.get("operation"):
            return None
        for data in (payload, raw):
            native = data.get("toolCall")
            if "tool_input" in data or isinstance(native, dict) and "args" in native:
                return None
        rows = self.db.execute(
            "SELECT id,payload FROM events WHERE project=? AND operation=? AND type='PreToolUse' "
            "AND conversation=? AND agent IS ? AND task_id IS ? AND task_revision IS ? "
            "AND task_write_version IS ? AND execution_epoch IS ? AND assignment_id IS ? "
            "AND json_extract(payload,'$.host_epoch') IS ? "
            "AND rowid<(SELECT rowid FROM events WHERE id=?) ORDER BY rowid DESC LIMIT 3",
            (project, event["operation"], event["conversation"], event.get("agent"), event.get("task_id"),
             event.get("task_revision"), event.get("task_write_version"), event.get("execution_epoch"),
             event.get("assignment_id"), payload.get("host_epoch"), event["id"])).fetchall()
        if not rows or len(rows) >= 3:
            return None
        attempts = [_tool(json.loads(row["payload"])) for row in rows]
        if any(tool != name for tool, _ in attempts) or len({_json(args) for _, args in attempts}) != 1:
            return None
        return rows[0]["id"] if _verification(*attempts[0]) else None

    def _work_file_projection(self, project, event, workspace, *, raw=None, observe_version=True):
        """Bounded file observations made once at the original callback boundary."""
        from .runtime import clean
        payload = json.loads(event["payload"])
        raw = clean(raw) if isinstance(raw, dict) else payload
        name, args = _tool(raw)
        stored_name, _ = _tool(payload)
        if _learning_tool(name or stored_name):
            return []
        links = file_links_from_payload(name, args, workspace, observe_version=observe_version)
        if not links and event.get("operation"):
            links = [dict(row) for row in self.db.execute(
                "SELECT relative_path,path_key,checkout_key,relation,version FROM work_file_links l "
                "JOIN events e ON e.id=l.event_id AND e.project=l.project WHERE l.project=? AND e.operation=? "
                "AND l.checkout_key=? ORDER BY e.created LIMIT 16", (project, event["operation"], checkout_key(workspace)))]
            for link in links:
                link["version"] = (current_file_version(link["relative_path"], workspace) if observe_version
                                   else {"status": "not_observed", "content_hash": None})
        return links

    def _work_capture_event(self, project, event_id, workspace, *, raw=None, native_context_hint=False,
                            _file_projection=None):
        """调用者已保存事件和当前操作状态；不扩大历史读取范围。"""
        from .runtime import clean, clip, digest
        if not _config(self)["enabled"]:
            return None
        event = self.db.execute("SELECT * FROM events WHERE project=? AND id=?", (project, event_id)).fetchone()
        if not event or event["type"] not in {"PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit", "PublicMessageSegment"}:
            return None
        event = dict(event)
        current_checkout = checkout_key(workspace)
        recorded_checkout = self.db.execute("SELECT checkout_key FROM work_event_checkouts WHERE project=? AND event_id=?",
                                            (project, event_id)).fetchone()
        if recorded_checkout and recorded_checkout[0] != current_checkout:
            from .runtime import ContractError
            raise ContractError("work capture checkout changed")
        self.db.execute("INSERT OR IGNORE INTO work_event_checkouts VALUES(?,?,?)", (project, event_id, current_checkout))
        payload = json.loads(event["payload"])
        raw = clean(raw) if isinstance(raw, dict) else payload
        name, args = _tool(raw)
        stored_name, stored_args = _tool(payload)
        if _learning_tool(name or stored_name):
            return None
        if _file_projection is None:
            links = self._work_file_projection(project, event, workspace, raw=raw)
            file_projection_complete = True
        else:
            from .work_recovery import projection_links
            links = projection_links(event, current_checkout, _file_projection)
            file_projection_complete = _file_projection["complete"]
        for link in links:
            self.db.execute("INSERT OR IGNORE INTO work_file_links VALUES(?,?,?,?,?,?,?)",
                (project, event_id, link["checkout_key"], link["relative_path"], link["path_key"],
                 link["relation"], _json(link["version"])))
        # AG/DSH observe 模式的 Post 可能没有执行归属。新鲜回调可留下一条检索提示，
        # 当前任务只用于下一次查文件历史，不能补写原事件的 task_id 或升级其证据。
        if (native_context_hint and self.host in {"antigravity", "dsh"} and event["type"] == "PostToolUse"
                and event.get("task_id") is None and event.get("agent") is None and links):
            task = self.task(project, event["conversation"])
            trajectory_table = "antigravity_trajectories" if self.host == "antigravity" else "dsh_sessions"
            trajectory = self.db.execute(f"SELECT generation FROM {trajectory_table} WHERE project=? AND conversation=?",
                                         (project, event["conversation"])).fetchone()
            if task and trajectory:
                for link in links:
                    self.db.execute("INSERT OR IGNORE INTO work_file_hints VALUES(?,?,?,?,?,?,?,?)",
                        (project, event_id, link["checkout_key"], link["path_key"], _task_key(task),
                         self.session_epoch(), trajectory["generation"], time.time()))
        if event["type"] == "PreToolUse":
            return None
        kind = ""
        text = ""
        projection_truncated = False
        classification_source = None
        if event["type"] == "PostToolUse":
            if event["outcome"] in {"failed", "conflict"}:
                kind = "tool_failure"
            elif any(link["relation"] == "write_target" for link in links):
                kind = "file_change"
            elif _verification(stored_name or name, stored_args):
                kind = "verification"
            elif (classification_source := self._work_verification_source(project, event, payload, raw, stored_name or name)):
                kind = "verification"
            if not kind:
                return None
            response = payload.get("tool_response", payload.get("toolResult", payload.get("error", {})))
            response_text = _json(response)
            projection_truncated = len(response_text) > 700
            text = clip(response_text, 700)
            title = f"{stored_name or name or 'tool'}: {event['outcome']}"
        elif event["type"] == "PublicMessageSegment":
            text = payload.get("text", "")
            kind = payload.get("memory_type", "public_summary")
            title = payload.get("title", "公开工作说明")
        elif event["type"] == "Stop":
            text = payload.get("last_assistant_message", payload.get("message", ""))
            if not isinstance(text, str) or not text.strip():
                return None
            kind, title = "public_summary", "公开工作说明：" + text.strip().splitlines()[0][:85]
        else:
            text = payload.get("prompt", "")
            if not isinstance(text, str) or not text.strip():
                return None
            kind = "user_correction" if _correction(text) else "user_request"
            title = ("用户更正：" if kind == "user_correction" else "用户要求：") + text.strip().splitlines()[0][:85]
        if not isinstance(text, str) or not text.strip():
            return None
        public = kind in {"public_summary", "user_correction", "user_request"}
        completion = payload.get("completion", raw.get("completion", "complete"))
        coverage = payload.get("capture_coverage", raw.get("capture_coverage", {}))
        if public and event["type"] != "PublicMessageSegment":
            original = raw.get("last_assistant_message" if event["type"] == "Stop" else "prompt", text)
            if isinstance(original, str) and len(original) > 1400:
                self._work_capture_public_segments(project, event, workspace, clean(original), kind, title,
                                                   completion, coverage, raw)
        body = {"type": kind, "title": clip(title, 160), "summary": clip(text, 1400),
                "validation_level": "reported" if public else "tool_observed",
                "task_write_version": event.get("task_write_version"),
                "assignment_id": event.get("assignment_id"),
                "coverage": {"projection_only": True, "truncated": bool(payload.get("truncated"))
                             or projection_truncated or len(text) > 1400,
                             "full_original_available": not bool(payload.get("truncated"))}}
        if not file_projection_complete:
            body["coverage"]["file_projection_complete"] = False
        if classification_source:
            body["classification_source_event"] = classification_source
            source = self.db.execute("SELECT business_digest,payload FROM events WHERE project=? AND id=?",
                                     (project, classification_source)).fetchone()
            body["classification_input_digest"] = source["business_digest"] or digest([classification_source, source["payload"]])
        if kind == "verification":
            observed_input = _tool(json.loads(source["payload"]))[1] if classification_source else stored_args
            body["tool_input_preview"] = clip(_json(observed_input), 350)
        if public:
            body.update(completion=completion, execution_authority=False,
                        public_message_id=payload.get("public_message_id", event_id))
            body["coverage"]["host"] = coverage
            host_truncated = any(value.get("truncated") for value in coverage.values()
                                 if isinstance(value, dict)) if isinstance(coverage, dict) else False
            body["coverage"]["truncated"] |= bool(payload.get("message_truncated") or host_truncated
                or raw.get("last_assistant_message_truncated") or raw.get("prompt_truncated"))
            body["coverage"]["full_original_available"] &= not body["coverage"]["truncated"]
            if event["type"] == "PublicMessageSegment":
                body["coverage"].update(segment=payload["segment"], segments=payload["segments"],
                                         message_chars=payload["message_chars"])
        memory_id = digest([project, "work_event", event_id])[:32]
        source_hash = event.get("capture_digest") or digest(payload)
        present = self.db.execute("SELECT source_hash,body FROM work_memories WHERE project=? AND id=?",
                                  (project, memory_id)).fetchone()
        if present:
            existing_links = [dict(row) for row in self.db.execute(
                "SELECT relative_path FROM work_file_links WHERE project=? AND event_id=?", (project, event_id))]
            self._work_index(project, memory_id, json.loads(present["body"]), existing_links)
            return {"memory_id": memory_id, "indexed": False, "reindexed": True}
        self.db.execute("INSERT INTO work_memories VALUES(?,?,?,1,?,?,?,?,?,?,?,?)",
            (project, memory_id, event_id, _json(body), source_hash, checkout_key(workspace),
             event["conversation"], event.get("task_id"), event.get("task_revision"), event.get("agent"), event["created"]))
        self.db.execute("INSERT INTO work_memory_sources VALUES(?,?,?,?,?)", (project, memory_id, event_id,
                         source_hash, self.work_source_snapshot(project, event_id)["source_version"]))
        self._work_index(project, memory_id, body, links)
        return {"memory_id": memory_id, "indexed": True}

    def _work_capture_public_segments(self, project, event, workspace, text, kind, title, completion, coverage, raw):
        """Preserve bounded public message windows as evidence, without reading transcripts."""
        from .runtime import digest
        maximum, size = 65536, 1200
        visible = text[:maximum]
        total = (len(visible) + size - 1) // size
        visible_digest = digest(visible)
        for index, start in enumerate(range(0, len(visible), size)):
            payload = {"text": visible[start:start + size], "memory_type": kind,
                       "title": title[:100] + f" [{index + 1}/{total}]", "public_message_id": event["id"],
                       "segment": index, "segments": total, "completion": completion,
                       "capture_coverage": coverage, "message_chars": len(text),
                       "message_truncated": (len(text) > maximum or raw.get("last_assistant_message_truncated") is True
                                             or raw.get("prompt_truncated") is True),
                       "execution_authority": False}
            segment, fresh = self._event(project, event["conversation"], "PublicMessageSegment", payload,
                event_id=["public-message", event["id"], visible_digest, index], outcome="reported",
                task_id=event.get("task_id"), task_revision=event.get("task_revision"),
                task_write_version=event.get("task_write_version"), execution_epoch=event.get("execution_epoch"),
                agent_id=event.get("agent"), assignment_id=event.get("assignment_id"))
            if fresh:
                # A delayed rebuild is not a new user statement or delivery.
                # Keep the original observation time for ranking and stage windows.
                self.db.execute("UPDATE events SET created=? WHERE project=? AND id=?",
                                (event["created"], project, segment["id"]))
            self._work_capture_event(project, segment["id"], workspace)

    def _work_index(self, project, memory_id, body, links):
        from .knowledge_index import terms
        text = " ".join(terms(" ".join([body.get("title", ""), body.get("summary", ""),
                                        *(item.get("relative_path", "") for item in links)])))
        self.db.execute("DELETE FROM work_memory_fts WHERE project=? AND id=?", (project, memory_id))
        self.db.execute("INSERT INTO work_memory_fts VALUES(?,?,?)", (project, memory_id, text))
        self.db.execute("DELETE FROM work_index_pending WHERE project=? AND memory_id=?", (project, memory_id))

    def _work_repair_index(self, project, *, limit=16):
        """Refresh derived lexical terms, without recapturing or changing source facts."""
        if not _config(self)["enabled"]:
            return 0
        rows = self.db.execute("SELECT p.memory_id,m.body,m.event_id FROM work_index_pending p "
            "LEFT JOIN work_memories m ON m.project=p.project AND m.id=p.memory_id "
            "WHERE p.project=? ORDER BY p.memory_id LIMIT ?", (project, limit)).fetchall()
        for row in rows:
            if row["body"] is None:
                self.db.execute("DELETE FROM work_index_pending WHERE project=? AND memory_id=?", (project, row["memory_id"]))
                continue
            links = self.db.execute("SELECT f.relative_path FROM work_memory_sources s JOIN work_file_links f "
                "ON f.project=s.project AND f.event_id=s.event_id WHERE s.project=? AND s.memory_id=?",
                (project, row["memory_id"])).fetchall()
            self._work_index(project, row["memory_id"], json.loads(row["body"]), [dict(link) for link in links])
        return len(rows)

    def work_source_snapshot(self, project, event_id):
        from .runtime import digest
        event = self.db.execute("SELECT * FROM events WHERE project=? AND id=?", (project, event_id)).fetchone()
        if event is None:
            return {"event_id": event_id, "state": "missing", "source_version": digest([event_id, "missing"])}
        event = dict(event)
        operation = self.db.execute("SELECT * FROM operations WHERE project=? AND id=?",
            (project, event.get("operation"))).fetchone() if event.get("operation") else None
        current = dict(operation) if operation else {}
        state = "conflicted" if current.get("conflict") or current.get("state") == "conflict" else (
            "denied" if current.get("decision") == "denied" else current.get("observation", event["outcome"]))
        if event["type"] in {"Stop", "UserPromptSubmit", "PublicMessageSegment"}:
            state = "reported"
        signature = {key: current.get(key) for key in ("decision", "observation", "conflict", "state", "last_event")}
        return {"event_id": event_id, "capture_digest": event.get("capture_digest") or digest(json.loads(event["payload"])),
                "state": state, "source_version": digest([event["id"], event["payload"], signature])}

    def _work_item(self, project, row, *, workspace=None, file_paths=None, preview_query=None):
        from .runtime import digest
        row = dict(row)
        body = json.loads(row["body"])
        sources = self.db.execute("SELECT event_id,source_hash,source_version FROM work_memory_sources WHERE project=? AND memory_id=? ORDER BY event_id",
                                  (project, row["id"])).fetchall()
        snapshots = [self.work_source_snapshot(project, source["event_id"]) for source in sources]
        mismatch = any(snapshot.get("capture_digest") != source["source_hash"] for source, snapshot in zip(sources, snapshots))
        changed = any(snapshot["source_version"] != source["source_version"] for source, snapshot in zip(sources, snapshots))
        state = ("missing" if any(s["state"] == "missing" for s in snapshots) else "source_changed" if mismatch else
                 "conflicted" if any(s["state"] == "conflicted" for s in snapshots) else
                 "denied" if any(s["state"] == "denied" for s in snapshots) else
                 "source_changed" if row["event_id"] is None and changed else
                 snapshots[0]["state"] if row["event_id"] is not None and len(snapshots) == 1 else "model_reported")
        files = []
        for source in sources:
            for file in self.db.execute("SELECT * FROM work_file_links WHERE project=? AND event_id=? ORDER BY checkout_key,path_key,relation LIMIT 16",
                                        (project, source["event_id"])):
                item = {"relative_path": file["relative_path"], "checkout_key": file["checkout_key"],
                        "relation": file["relation"], "version": json.loads(file["version"])}
                if item not in files:
                    files.append(item)
        focus = {str(path) for path in (file_paths or [])[:3]}
        if focus:
            files.sort(key=lambda item: (item["relative_path"] not in focus, item["checkout_key"], item["relative_path"], item["relation"]))
        visible_files = files[:3]
        states = {}
        for item in visible_files:
            if workspace and checkout_key(workspace) == item["checkout_key"]:
                item["current_version"] = current_file_version(item["relative_path"], workspace)
                item["version_changed"] = item["version"] != item["current_version"]
                states[item["relative_path"]] = digest(item["current_version"])
        file_state = states or None
        from .candidate_references import lineage_risk
        reference_risks = [risk for source in sources
                           if (risk := lineage_risk(self, project, source["event_id"])) and risk.get("blocked", True)]
        source_identity = [row["revision"], row["body"], snapshots, reference_risks]
        # Capture mismatch can change health without changing the live snapshot.
        # Keep healthy legacy fingerprints stable; only append the missing risk.
        if mismatch:
            source_identity.append({"capture_state": state})
        return {"memory_id": row["id"], "revision": row["revision"],
                "source_version": digest(source_identity), "state": state,
                **({"automatic_eligible": False, "file_projection_incomplete": True}
                   if body.get("coverage", {}).get("file_projection_complete") is False else {}),
                **({"candidate_reference_risk": reference_risks, "automatic_eligible": False} if reference_risks else {}),
                "type": body["type"], "title": body["title"],
                "summary": (_work_preview(body["summary"], preview_query) if preview_query is not None else body["summary"][:240]),
                "completion": body.get("completion", "complete"), "execution_authority": False,
                "validation_level": body["validation_level"], "source_event_refs": [source["event_id"] for source in sources],
                "files": visible_files, "files_count": len(files), "coverage": body.get("coverage", {}),
                "created": row["created"], "summary_only": True, "read_required": True,
                "permission_restored": False, "source_changed": changed,
                "task_binding_known": row["task_id"] is not None,
                "file_state": file_state, "file_focus": sorted(focus)}

    def _work_search_rows(self, project, query, *, file_path=None, workspace=None, limit=5,
                          conversation=None, agent_id=None, task_state=None, exclude_event_id=None,
                          automatic=False, handoff=False):
        from .knowledge_index import normalized_query_plan, _MAX_QUERY_CHARS
        from .runtime import ContractError
        query_limit = _MAX_QUERY_CHARS if automatic else 8000
        if not isinstance(query, str) or len(query) > query_limit:
            raise ContractError(f"work query must be text of at most {query_limit} characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ContractError("work search limit must be 1..20")
        clauses, params = ["m.project=?"], [project]
        if handoff:
            if not conversation or not workspace or not isinstance(task_state, dict) or not task_state.get("id"):
                return []
            if agent_id and not task_state.get("assignment_id"):
                return []
            clauses.extend(["m.workspace_key=?", "json_extract(m.body,'$.type') IN ('public_summary','work_summary')",
                            "COALESCE(json_extract(m.body,'$.completion'),'complete')='complete'",
                            "COALESCE(json_extract(m.body,'$.coverage.segments'),1)=1",
                            "COALESCE(json_extract(m.body,'$.coverage.truncated'),0)=0",
                            "json_extract(m.body,'$.assignment_id') IS ?"])
            params.extend((checkout_key(workspace), task_state.get("assignment_id")))
        if exclude_event_id is not None:
            # Public-message segments retain the original event identity.
            # Exclude before LIMIT so the current prompt cannot crowd out history.
            clauses.append("m.event_id IS NOT ? AND COALESCE(json_extract(m.body,'$.public_message_id'),'')!=?")
            params.extend((exclude_event_id, exclude_event_id))
        joins = ""
        if file_path is not None:
            if workspace is None:
                raise ContractError("file search requires a workspace")
            target = normalize_file_target(file_path, workspace, observe_version=False)
            if target is None:
                raise ContractError("file search target must be inside workspace")
            clauses.append("EXISTS(SELECT 1 FROM work_memory_sources s JOIN work_file_links l ON l.project=s.project AND l.event_id=s.event_id WHERE s.project=m.project AND s.memory_id=m.id AND l.checkout_key=? AND l.path_key=?)")
            params.extend((target["checkout_key"], target["path_key"]))
        elif query.strip():
            selected = normalized_query_plan(query)["recall_terms"]
            if not selected:
                return []
            joins = " JOIN work_memory_fts f ON f.project=m.project AND f.id=m.id "
            clauses.append("work_memory_fts MATCH ?")
            params.append(" OR ".join('"' + word.replace('"', '""') + '"' for word in selected))
        if conversation is not None:
            clauses.append("m.conversation=? AND m.agent IS ?")
            params.extend((conversation, agent_id))
            if isinstance(task_state, dict) and task_state.get("id"):
                clauses.append("m.task_id=? AND m.task_revision=?")
                params.extend((task_state["id"], task_state.get("revision")))
            if isinstance(task_state, dict) and task_state.get("assignment_id"):
                clauses.append("json_extract(m.body,'$.assignment_id')=?")
                params.append(task_state["assignment_id"])
        ordering = " ORDER BY bm25(work_memory_fts),m.created DESC,m.id DESC" if joins else " ORDER BY m.created DESC,m.id DESC"
        select = "SELECT m.* FROM work_memories m" + joins + " WHERE " + " AND ".join(clauses)
        pool_limit = min(100 if automatic else 200, limit * 10)
        rows = self.db.execute(select + ordering + " LIMIT ?", (*params, pool_limit)).fetchall()
        plan = normalized_query_plan(query)
        receipt_query = _write_receipt_query(plan)
        if (not handoff and not receipt_query and len(rows) == pool_limit
                and any(_metadata_only_write(json.loads(row["body"])) for row in rows)):
            # A bounded second lane prevents a burst of write receipts from
            # hiding older results. It retains the same scope and query gates.
            other = self.db.execute(select + " AND json_extract(m.body,'$.type')!='file_change'" + ordering
                                    + " LIMIT ?", (*params, pool_limit)).fetchall()
            found = {row["id"] for row in rows}
            rows.extend(row for row in other if row["id"] not in found)
        if (query.strip() or automatic or file_path is not None) and not handoff:
            selected = plan["terms"][:64]
            from .knowledge_index import match_terms
            def score(row):
                body = json.loads(row["body"])
                values = set(match_terms(body["title"] + " " + body["summary"]))
                matches = sum(term in values for term in selected)
                exact = all(anchor in values for anchor in plan["exact_anchors"])
                # A file acknowledgement is still an audit record, but a
                # matching public explanation is a more useful answer. Exact
                # technical identities and explicit receipt requests retain
                # normal lexical ordering; no source or authority is changed.
                receipt = _metadata_only_write(body)
                content_role = receipt if receipt_query else not receipt
                return (exact, bool(matches), content_role, matches, _work_priority(body),
                        row["created"], row["id"])
            rows = sorted(rows, key=score, reverse=True)
        selected_rows, counts, requests = [], {}, []
        query_gate = automatic and query.strip() and file_path is None
        if query_gate:
            from .knowledge_index import _match_info
            plan = normalized_query_plan(query)
            if not plan["automatic_safe"]:
                return []
            weights = {word: 2 if re.fullmatch(r"[\u3400-\u9fff]+", word) and len(word) > 1 else 1
                       for word in plan["terms"]}
            denominator = sum(weights.values())
        for row in rows:
            body = json.loads(row["body"])
            if automatic and _metadata_only_write(body):
                continue
            if query_gate:
                if (plan["automatic_intent"]["action_only"] and body.get("type") == "user_request"
                        and not _constraint_request(body["summary"])):
                    continue
                # A user's correction may quote the very status it rejects.
                # Preserve that role without bypassing the original match gate.
                intent = {} if body.get("type") == "user_correction" else plan["automatic_intent"]
                comparable = _json({"title": body["title"], "content": body["summary"], "knowledge_kind": "",
                                    "validation_level": body.get("validation_level")})
                if not denominator or not _match_info(comparable, weights, denominator,
                                                       plan["meaningful"], _anchors=plan["exact_anchors"],
                                                       _intent=intent)["auto_eligible"]:
                    continue
                # Prefer recorded results over a previous short question about
                # the same subject. Keep explicit user constraints/corrections;
                # requests remain the fallback when no useful result exists.
                if body.get("type") == "user_request" and not _constraint_request(body["summary"]):
                    requests.append(row)
                    continue
            parent = body.get("public_message_id", row["id"])
            if counts.get(parent, 0) >= 2:
                continue
            counts[parent] = counts.get(parent, 0) + 1
            selected_rows.append(row)
            if len(selected_rows) == limit:
                break
        if automatic and not selected_rows:
            for row in requests:
                body = json.loads(row["body"])
                parent = body.get("public_message_id", row["id"])
                if counts.get(parent, 0) >= 2:
                    continue
                counts[parent] = counts.get(parent, 0) + 1
                selected_rows.append(row)
                if len(selected_rows) == limit:
                    break
        return selected_rows

    def work_search(self, project, query="", *, file_path=None, workspace=None, limit=5, max_chars=3000):
        _budget(max_chars)
        with _snapshot(self):
            output = {"status": "ok", "items": [], "summary_only": True, "knowledge_status": "working_history",
                      "truncated": False, "omitted_count": 0, "index_version": WORK_INDEX_VERSION}
            if not _config(self)["enabled"]:
                return dict(output, status="disabled")
            rows = self._work_search_rows(project, query, file_path=file_path, workspace=workspace, limit=limit)
            for row in rows:
                target = normalize_file_target(file_path, workspace, observe_version=False) if file_path and workspace else None
                card = self._work_item(project, row, workspace=workspace,
                                       file_paths=[target["relative_path"]] if target else None)
                # 默认检索只给索引；精确 stat、长摘要与完整来源在 read 中展开。
                card["files"] = [{key: item[key] for key in ("relative_path", "relation", "version_changed") if key in item}
                                 for item in card["files"]]
                card["summary"] = card["summary"][:120]
                card["title"] = card["title"][:100]
                card["source_event_refs"] = card["source_event_refs"][:3]
                card.pop("file_focus", None)
                candidate = dict(output, items=output["items"] + [card])
                if len(_json(candidate)) > max_chars:
                    card["summary"] = card["summary"][:60]
                    card["title"] = card["title"][:70]
                    card.pop("coverage", None)
                    candidate = dict(output, items=output["items"] + [card])
                if len(_json(candidate)) <= max_chars:
                    output["items"].append(card)
                else:
                    output["omitted_count"] += 1
            output["truncated"] = bool(output["omitted_count"])
            return output

    def work_read(self, project, memory_id, *, expected_revision=None, offset=0, max_chars=6000, section="summary", workspace=None,
                  _delivery=None, _delivery_scope=None, _reread=False):
        from .runtime import ContractError
        _budget(max_chars, 1024, 50000)
        if section not in {"summary", "evidence"}:
            raise ContractError("work section must be summary or evidence")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ContractError("offset must be a nonnegative integer")
        if expected_revision is not None and (isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int) or expected_revision < 1):
            raise ContractError("expected_revision must be a positive integer")
        with _snapshot(self):
            if not _config(self)["enabled"]:
                return {"status": "disabled", "items": []}
            row = self.db.execute("SELECT * FROM work_memories WHERE project=? AND id=?", (project, memory_id)).fetchone()
            if row is None:
                raise ContractError("unknown work memory")
            if expected_revision is not None and expected_revision != row["revision"]:
                raise ContractError("work revision changed; search again")
            result = self._work_item(project, row, workspace=workspace)
            result["file_check"] = _read_file_check(self, project, memory_id, workspace)
            if result["files_count"] > len(result["files"]):
                result["files_omitted"] = result["files_count"] - len(result["files"])
            if _delivery is not None and not _reread:
                receipt = _delivery.completed(result, _delivery_scope, section=section, offset=offset, max_chars=max_chars)
                if receipt is not None:
                    return receipt
            body = json.loads(row["body"])
            if section == "summary":
                text = body["summary"]
            else:
                text = self._work_evidence_text(project, result["source_event_refs"])
            if offset > len(text):
                raise ContractError("offset exceeds work section length")
            result.pop("summary", None)
            result.update(status="ok", section=section, section_length=len(text), offset=offset,
                          content="", next_offset=None, truncated=False)
            # Preserve a complete first-page response when its full details fit.
            # Bound the trial by the response budget, even for very long evidence.
            if offset == 0 and len(text) <= max_chars:
                result["content"] = text
                if len(_json(result)) <= max_chars:
                    return result
                result["content"] = ""
            # Previews may shrink; full source and bounded file checks remain.
            _read_metadata_preview(result, max_chars, paginated=True)
            if len(text) - offset <= max_chars:
                result.update(content=text[offset:], next_offset=None, truncated=False)
                # At large offsets, replacing next_offset's integer with null
                # can make the final page shorter than a preceding prefix.
                if len(_json(result)) <= max_chars:
                    return result
            low, high = 0, min(len(text) - offset, max_chars)
            while low < high:
                middle = (low + high + 1) // 2
                end = offset + middle
                result.update(content=text[offset:end], next_offset=end if end < len(text) else None,
                              truncated=end < len(text))
                if len(_json(result)) <= max_chars:
                    low = middle
                else:
                    high = middle - 1
            result.update(content=text[offset:offset + low], next_offset=offset + low if offset + low < len(text) else None,
                          truncated=offset + low < len(text))
            while len(_json(result)) > max_chars and result["content"]:
                low -= 1
                result.update(content=text[offset:offset + low], next_offset=offset + low, truncated=True)
            if len(_json(result)) > max_chars or (not low and offset < len(text)):
                raise ContractError("budget cannot fit work identity and page")
            return result

    def _work_evidence_text(self, project, refs):
        evidence = []
        for ref in refs:
            event = self.db.execute("SELECT id,type,outcome,payload,capture_digest FROM events WHERE project=? AND id=?",
                                    (project, ref)).fetchone()
            evidence.append(dict(event) if event else {"id": ref, "state": "missing"})
        return _json({"projection_only": True, "events": evidence})

    def work_record_exposure(self, project, conversation, items, agent_id=None, *, delivery="persistent",
                             task_state=None, compact=False, context_epoch=None, method="hook", **kwargs):
        from .runtime import ContractError
        if delivery not in {"persistent", "ephemeral"} or method not in {"hook", "mcp"}:
            raise ContractError("invalid work delivery or method")
        if kwargs.get("expected_epoch") is not None:
            self.assert_session_epoch(kwargs["expected_epoch"])
        if kwargs.get("context_handle"):
            self.access_context(project, kwargs["context_handle"])
        context_epoch = int(context_epoch or 0)
        now, count = time.time(), 0
        for item in items[:20]:
            if not isinstance(item, dict) or not all(key in item for key in ("memory_id", "revision", "source_version")):
                continue
            observed_files = item.get("file_state")
            file_state = None
            if isinstance(observed_files, dict):
                previous = self.db.execute("SELECT file_state FROM work_exposures WHERE project=? AND conversation=? AND agent=? AND memory_id=?",
                                           (project, conversation, agent_id or "", item["memory_id"])).fetchone()
                known = json.loads(previous["file_state"]) if previous and previous["file_state"] else {}
                if not isinstance(known, dict):
                    known = {}
                # 文件提示按具体文件确认，不因标题检索和文件检索选择不同子集而反复报警。
                for path, version in observed_files.items():
                    if isinstance(path, str) and isinstance(version, str):
                        known.pop(path, None)
                        known[path] = version
                file_state = _json(dict(list(known.items())[-32:]))
            self.db.execute("INSERT INTO work_exposures(project,conversation,agent,memory_id,revision,source_version,context_epoch,task_key,delivery,method,created,file_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project,conversation,agent,memory_id) DO UPDATE SET revision=excluded.revision,source_version=excluded.source_version,context_epoch=excluded.context_epoch,task_key=excluded.task_key,delivery=excluded.delivery,method=excluded.method,created=excluded.created,file_state=COALESCE(excluded.file_state,work_exposures.file_state)",
                (project, conversation, agent_id or "", item["memory_id"], item["revision"], item["source_version"],
                 context_epoch, _exposure_task_key(task_state), delivery, method, now, file_state))
            count += 1
        # 只保留展示台账；不改变工作记录或现有知识采用状态。
        self.db.execute("DELETE FROM work_exposures WHERE project=? AND conversation=? AND agent=? AND memory_id NOT IN (SELECT memory_id FROM work_exposures WHERE project=? AND conversation=? AND agent=? ORDER BY created DESC,memory_id DESC LIMIT 100)",
            (project, conversation, agent_id or "", project, conversation, agent_id or ""))
        return {"recorded": count, "delivery": delivery, "model_read_confirmed": False}

    def work_exposure_updates(self, project, conversation, agent_id=None, limit=3, *, workspace=None):
        result = []
        with _snapshot(self):
            rows = self.db.execute("SELECT m.*,x.source_version AS shown_version,x.file_state AS shown_file_state FROM work_exposures x JOIN work_memories m ON m.project=x.project AND m.id=x.memory_id WHERE x.project=? AND x.conversation=? AND x.agent=? ORDER BY x.created DESC LIMIT 100",
                                   (project, conversation, agent_id or "")).fetchall()
            for row in rows:
                old_files = json.loads(row["shown_file_state"]) if row["shown_file_state"] else {}
                item = self._work_item(project, row, workspace=workspace if old_files else None,
                                       file_paths=list(old_files))
                file_changed = any(path in old_files and old_files[path] != version
                                   for path, version in (item["file_state"] or {}).items())
                source_changed = item["source_version"] != row["shown_version"]
                if source_changed:
                    item["source_changed"] = True
                if file_changed:
                    item["file_state_changed"] = True
                if source_changed or file_changed:
                    result.append(item)
                    if len(result) >= limit:
                        break
        return result

    def work_context_items(self, project, conversation, query="", *, workspace=None, raw=None,
                           agent_id=None, limit=3, task_state=None, delivery="persistent", compact=False,
                           context_epoch=None, file_only=False, exclude_event_id=None, handoff_only=False,
                           allow_unfocused_history=True):
        if not _config(self)["enabled"] or not _config(self)["automatic_context"]:
            return []
        with _snapshot(self):
            name, args = _tool(raw or {})
            links = (file_links_from_payload(name, args, workspace, observe_version=False)
                     if workspace and not handoff_only else [])
            result = self.work_exposure_updates(project, conversation, agent_id, limit=limit, workspace=workspace)
            if not handoff_only and not links and not query.strip() and workspace and not file_only:
                # AG 下一次 invocation 无工具参数，仅使用本会话/agent近期明确目标。
                filters, parameters = "", [project, conversation, agent_id, checkout_key(workspace)]
                if isinstance(task_state, dict) and task_state.get("id"):
                    filters += " AND e.task_id=? AND e.task_revision=?"
                    parameters.extend((task_state["id"], task_state.get("revision")))
                if isinstance(task_state, dict) and task_state.get("assignment_id"):
                    filters += " AND e.assignment_id=?"
                    parameters.append(task_state["assignment_id"])
                recent = self.db.execute("SELECT l.* FROM work_file_links l JOIN events e ON e.project=l.project AND e.id=l.event_id WHERE l.project=? AND e.conversation=? AND e.agent IS ? AND l.checkout_key=?" + filters + " ORDER BY e.created DESC LIMIT 3",
                                         parameters).fetchall()
                links = [dict(row) for row in recent]
                if self.host in {"antigravity", "dsh"} and not agent_id:
                    trajectory_table = "antigravity_trajectories" if self.host == "antigravity" else "dsh_sessions"
                    trajectory = self.db.execute(f"SELECT generation FROM {trajectory_table} WHERE project=? AND conversation=?",
                                                 (project, conversation)).fetchone()
                    if trajectory and isinstance(task_state, dict) and task_state.get("id"):
                        hints = self.db.execute(
                            "SELECT l.* FROM work_file_hints h JOIN work_file_links l ON l.project=h.project "
                            "AND l.event_id=h.event_id AND l.checkout_key=h.checkout_key AND l.path_key=h.path_key "
                            "JOIN events e ON e.project=h.project AND e.id=h.event_id "
                            "WHERE h.project=? AND e.conversation=? AND e.agent IS NULL AND h.checkout_key=? "
                            "AND h.context_task_key=? AND h.session_epoch=? AND h.generation=? "
                            "ORDER BY h.created DESC LIMIT 3",
                            (project, conversation, checkout_key(workspace), _task_key(task_state),
                             self.session_epoch(), trajectory["generation"])).fetchall()
                        links = [dict(row, task_affinity="context_only") for row in hints] + links
            if file_only and not links:
                return result[:limit]
            rows = []
            for link in links[:3]:
                rows.extend(self._work_search_rows(project, query[:8000],
                    file_path=str(checkout_root(workspace) / link["relative_path"]), workspace=workspace, limit=limit,
                    exclude_event_id=exclude_event_id, automatic=True))
            if not links and not handoff_only and (query.strip() or allow_unfocused_history):
                # 缺少当前请求时不猜历史焦点。空查询只恢复本会话；跨会话
                # 资料必须通过实际问题召回或明确文件关联，不能仅按新近程度。
                rows = self._work_search_rows(project, query, limit=limit,
                    conversation=None if query.strip() else conversation, agent_id=agent_id, task_state=task_state,
                    exclude_event_id=exclude_event_id, automatic=bool(query.strip()))
            # A query that cannot represent every identity is not a blank query.
            # Its compact-only lane never infers files or falls back to other
            # conversations. Reserve one healthy current-task handoff so ordinary
            # matches cannot fill every slot before phase recovery is considered.
            # A usable query keeps its best match first; a one-item budget has
            # no extra slot to reserve for background.
            handoff_scopes = {}
            query_match_first = bool(query.strip() and rows and not handoff_only)
            if compact and not file_only and not (limit == 1 and query_match_first):
                handoffs = self._work_search_rows(project, "", workspace=workspace, limit=20,
                    conversation=conversation, agent_id=agent_id, task_state=task_state,
                    exclude_event_id=exclude_event_id, automatic=True, handoff=True)
                for row in sorted(handoffs, key=lambda row: (row["created"], row["id"]), reverse=True):
                    item = self._work_item(project, row, workspace=workspace)
                    if not self._work_handoff_usable(item):
                        continue
                    handoff_scopes[row["id"]] = {
                        "workspace_key": checkout_key(workspace), "conversation": conversation,
                        "agent": agent_id, "task_id": task_state["id"],
                        "task_revision": task_state.get("revision"),
                        "assignment_id": task_state.get("assignment_id"),
                    }
                    others = [other for other in rows if other["id"] != row["id"]]
                    if query_match_first and rows[0]["id"] != row["id"]:
                        rows = others[:1] + [row] + others[1:]
                    else:
                        rows = [row] + others
                    break
            seen = {item["memory_id"] for item in result}
            public_messages = set()
            for row in rows:
                body = json.loads(row["body"])
                parent = body.get("public_message_id")
                if _metadata_only_write(body) or compact and parent and parent in public_messages:
                    continue
                handoff_scope = handoff_scopes.get(row["id"])
                preview_query = None if handoff_scope else query
                item = self._work_item(project, row, workspace=workspace,
                                       file_paths=[link["relative_path"] for link in links[:3]], preview_query=preview_query)
                if item.get("file_projection_incomplete"):
                    continue
                item["_preview_query"] = preview_query
                if handoff_scope:
                    if not self._work_handoff_usable(item):
                        continue
                    from .work_handoff import select_handoff_preview
                    item["summary"] = select_handoff_preview(body.get("summary", ""))
                    item["_handoff_scope"] = handoff_scope
                    item["reference_role"] = "task_handoff"
                if any(link.get("task_affinity") == "context_only" for link in links):
                    item["task_affinity"] = "context_only"
                if item["memory_id"] in seen:
                    continue
                previous = self.db.execute("SELECT * FROM work_exposures WHERE project=? AND conversation=? AND agent=? AND memory_id=?",
                                            (project, conversation, agent_id or "", item["memory_id"])).fetchone()
                previous_files = json.loads(previous["file_state"]) if previous and previous["file_state"] else {}
                current_files = item["file_state"] or {}
                file_changed = any(path in previous_files and previous_files[path] != version
                                   for path, version in current_files.items())
                file_unseen = bool(current_files) and any(path not in previous_files for path in current_files)
                if file_changed:
                    item["file_state_changed"] = True
                if (previous and delivery == "persistent" and not compact and previous["source_version"] == item["source_version"]
                        and not file_changed and not file_unseen and previous["context_epoch"] == int(context_epoch or 0)
                        and _same_exposure_task(previous["task_key"], task_state)):
                    continue
                result.append(item)
                seen.add(item["memory_id"])
                if parent:
                    public_messages.add(parent)
                if len(result) >= limit:
                    break
            return result[:limit]

    @staticmethod
    def _work_handoff_usable(item):
        """Only healthy complete observations may restore a phase as background."""
        return (item.get("type") in {"public_summary", "work_summary"}
                and item.get("completion") == "complete"
                and not item.get("coverage", {}).get("truncated")
                and item.get("coverage", {}).get("segments", 1) == 1
                and item.get("state") in {"reported", "model_reported"}
                and not item.get("source_changed")
                and not item.get("candidate_reference_risk")
                and item.get("automatic_eligible", True)
                and not any(file.get("version_changed") is True for file in item.get("files", [])))

    def work_rebuild(self, project, workspace, *, cursor=None, limit=100):
        from .runtime import ContractError
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ContractError("work rebuild limit must be 1..1000")
        after = (0, "")
        phase = "events"
        if cursor not in (None, 0, ""):
            try:
                after = json.loads(cursor) if isinstance(cursor, str) else cursor
                if isinstance(after, dict):
                    if set(after) != {"phase", "after"} or after["phase"] != "summaries":
                        raise ValueError()
                    phase, after = "summaries", after["after"]
                if (not isinstance(after, (list, tuple)) or len(after) != 2 or isinstance(after[0], bool)
                        or not isinstance(after[0], (int, float)) or not isinstance(after[1], str)):
                    raise ValueError()
            except (TypeError, ValueError):
                raise ContractError("invalid work rebuild cursor")
        with _write_snapshot(self):
            paths = self.db.execute("SELECT path FROM project_paths WHERE project=?", (project,)).fetchall()
            if checkout_key(workspace) not in {checkout_key(row[0]) for row in paths}:
                raise ContractError("work rebuild workspace belongs to another project")
            rows = (self.db.execute("SELECT * FROM events WHERE project=? AND (created>? OR created=? AND id>?) ORDER BY created,id LIMIT ?",
                                   (project, after[0], after[0], after[1], limit + 1)).fetchall()
                    if phase == "events" else [])
            indexed, reindexed, skipped = 0, 0, 0
            for row in rows[:limit]:
                payload = json.loads(row["payload"])
                recorded_workspace = payload.get("cwd", payload.get("workspace"))
                recorded_checkout = self.db.execute("SELECT checkout_key FROM work_event_checkouts WHERE project=? AND event_id=?",
                                                     (project, row["id"])).fetchone()
                # 缺少旧checkout身份不猜；只重建本checkout可确认的已有事件。
                if (recorded_checkout and recorded_checkout[0] == checkout_key(workspace)
                        or recorded_workspace and checkout_key(recorded_workspace) == checkout_key(workspace)):
                    from .work_recovery import restore
                    result = restore(self, project, row["id"], workspace, reindex=True)
                    if result["status"] == "completed":
                        indexed += bool(result.get("indexed"))
                        reindexed += bool(result.get("reindexed"))
                else:
                    skipped += 1
            event_scanned = min(len(rows), limit)
            next_cursor = _json([rows[limit - 1]["created"], rows[limit - 1]["id"]]) if len(rows) > limit else None
            summary_scanned = 0
            if next_cursor is None:
                # 原有事件游标保持不变。事件页完成后进入摘要阶段，派生 FTS 丢失时
                # 仍能从既有工作摘要恢复；不重新调用模型或改写来源快照。
                summary_after = after if phase == "summaries" else (0, "")
                remaining = limit - event_scanned
                summaries = self.db.execute(
                    "SELECT * FROM work_memories WHERE project=? AND event_id IS NULL AND workspace_key=? "
                    "AND (created>? OR created=? AND id>?) ORDER BY created,id LIMIT ?",
                    (project, checkout_key(workspace), summary_after[0], summary_after[0],
                     summary_after[1], remaining + 1)).fetchall()
                for summary in summaries[:remaining]:
                    files = [dict(row) for row in self.db.execute(
                        "SELECT DISTINCT l.relative_path FROM work_memory_sources s JOIN work_file_links l "
                        "ON l.project=s.project AND l.event_id=s.event_id WHERE s.project=? AND s.memory_id=? "
                        "AND l.checkout_key=? ORDER BY l.relative_path LIMIT 640",
                        (project, summary["id"], checkout_key(workspace)))]
                    self._work_index(project, summary["id"], json.loads(summary["body"]), files)
                    reindexed += 1
                    summary_scanned += 1
                if len(summaries) > remaining:
                    last = summaries[remaining - 1] if remaining else None
                    summary_cursor = [last["created"], last["id"]] if last else list(summary_after)
                    next_cursor = _json({"phase": "summaries", "after": summary_cursor})
            return {"scanned": event_scanned + summary_scanned, "events_scanned": event_scanned,
                    "summaries_reindexed": summary_scanned, "indexed": indexed, "reindexed": reindexed,
                    "unknown_checkout_skipped": skipped, "next_cursor": next_cursor,
                    "files_scanned": False, "conversations_scanned": False, "automatic": False}

    def record_work_summary(self, project, summary_id, body, source_snapshots, conversation, task_id=None, task_revision=None):
        from .runtime import ContractError, clean, digest
        if (not isinstance(body, dict) or not isinstance(body.get("title"), str)
                or not 1 <= len(body["title"]) <= 160 or not isinstance(body.get("summary"), str)
                or not 1 <= len(body["summary"]) <= 4000 or not isinstance(source_snapshots, list)
                or not 1 <= len(source_snapshots) <= 40):
            raise ContractError("invalid work summary")
        with _write_snapshot(self):
            sources, rows = [], []
            for snapshot in source_snapshots:
                if not isinstance(snapshot, dict) or not isinstance(snapshot.get("event_id"), str):
                    raise ContractError("invalid work summary source")
                current = self.work_source_snapshot(project, snapshot["event_id"])
                if (current.get("capture_digest") != snapshot.get("capture_digest")
                        or snapshot.get("source_version") and current["source_version"] != snapshot["source_version"]):
                    raise ContractError("work summary source changed")
                source = self.db.execute("SELECT * FROM work_memories WHERE project=? AND event_id=?",
                                         (project, snapshot["event_id"])).fetchone()
                conversation_scope = body.get("scope") == "conversation" and task_id is None and task_revision is None
                if (source is None or source["conversation"] != conversation
                        or conversation_scope and source["agent"] is not None
                        or not conversation_scope and (source["task_id"] != task_id or source["task_revision"] != task_revision)):
                    raise ContractError("work summary source scope mismatch")
                rows.append(source)
                sources.append(current)
            if len({row["workspace_key"] for row in rows}) != 1 or len({row["agent"] for row in rows}) != 1:
                raise ContractError("work summary mixes checkout or agent identities")
            projected = clean({"type": "work_summary", "title": body["title"], "summary": body["summary"],
                "validation_level": "model_reported", "coverage": {"projection_only": True, "truncated": any(
                    json.loads(row["body"])["coverage"].get("truncated", False) for row in rows)},
                "task_write_version": body.get("task_write_version")})
            projected.update(scope="conversation" if body.get("scope") == "conversation" else "task",
                             execution_authority=False, completion="complete")
            projected["coverage"].update(body.get("coverage", {}))
            memory_id = digest([project, "work_summary", summary_id])[:32]
            fingerprint = digest([projected, sources])
            previous = self.db.execute("SELECT * FROM work_memories WHERE project=? AND id=?", (project, memory_id)).fetchone()
            if previous:
                if previous["source_hash"] != fingerprint:
                    raise ContractError("work summary identity reused with changed content")
                return dict(self._work_item(project, previous), id=memory_id, indexed=False)
            self.db.execute("INSERT INTO work_memories VALUES(?,?,NULL,1,?,?,?,?,?,?,?,?)",
                (project, memory_id, _json(projected), fingerprint, rows[0]["workspace_key"], conversation,
                 task_id, task_revision, rows[0]["agent"], time.time()))
            files = []
            for source in sources:
                self.db.execute("INSERT OR IGNORE INTO work_memory_sources VALUES(?,?,?,?,?)",
                    (project, memory_id, source["event_id"], source["capture_digest"], source["source_version"]))
                files.extend(dict(row) for row in self.db.execute("SELECT relative_path FROM work_file_links WHERE project=? AND event_id=?",
                                                                 (project, source["event_id"])))
            self._work_index(project, memory_id, projected, files)
            row = self.db.execute("SELECT * FROM work_memories WHERE project=? AND id=?", (project, memory_id)).fetchone()
            return dict(self._work_item(project, row), id=memory_id, indexed=True)

    def work_memory_status(self, project):
        with _snapshot(self):
            return {"settings": _config(self), "index_version": WORK_INDEX_VERSION,
                    "index_rebuild_pending": self.db.execute("SELECT COUNT(*) FROM work_index_pending WHERE project=?", (project,)).fetchone()[0],
                    "capture_repair_pending": self.db.execute("SELECT COUNT(*) FROM work_capture_pending WHERE project=?", (project,)).fetchone()[0],
                    "memories": self.db.execute("SELECT COUNT(*) FROM work_memories WHERE project=?", (project,)).fetchone()[0],
                    "file_relations": self.db.execute("SELECT COUNT(*) FROM work_file_links WHERE project=?", (project,)).fetchone()[0],
                    "model_summaries": self.db.execute("SELECT COUNT(*) FROM work_memories WHERE project=? AND event_id IS NULL", (project,)).fetchone()[0],
                    "coverage": {"observable_events_only": True, "history_backfill_automatic": False,
                                 "internal_thinking_captured": False, "project_permissions_restored": False}}

    def work_validate_items(self, project, items, workspace=None):
        """最终发送事务中刷新来源状态；不能用旧状态确认当前有效性。"""
        result = []
        for item in items[:20]:
            row = self.db.execute("SELECT * FROM work_memories WHERE project=? AND id=?", (project, item.get("memory_id"))).fetchone()
            if row is not None and row["revision"] == item.get("revision"):
                current = self._work_item(project, row, workspace=workspace, file_paths=item.get("file_focus"),
                                          preview_query=item.get("_preview_query"))
                if current.get("file_projection_incomplete"):
                    continue
                if item.get("reference_role") == "task_handoff":
                    scope = item.get("_handoff_scope")
                    body = json.loads(row["body"])
                    if (not isinstance(scope, dict) or not self._work_handoff_usable(current)
                            or any(row[field] != scope.get(field) for field in (
                                "workspace_key", "conversation", "agent", "task_id", "task_revision"))
                            or body.get("assignment_id") != scope.get("assignment_id")
                            or not workspace or checkout_key(workspace) != scope["workspace_key"]):
                        continue
                    current["reference_role"] = "task_handoff"
                    current["_handoff_scope"] = scope
                    from .work_handoff import select_handoff_preview
                    current["summary"] = select_handoff_preview(body.get("summary", ""))
                if item.get("task_affinity") == "context_only":
                    current["task_affinity"] = "context_only"
                if item.get("file_state_changed"):
                    current["file_state_changed"] = True
                result.append(current)
        return result
