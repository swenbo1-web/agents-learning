"""可检索的工作观察与文件索引；权威证据仍为已有 events/operations。"""
from __future__ import annotations

from contextlib import contextmanager
import json
import re
import time

from .work_files import checkout_key, checkout_root, file_links_from_payload, normalize_file_target, current_file_version


WORK_MEMORY_DEFAULTS = {"enabled": True, "automatic_context": True, "max_context_chars": 600,
                        "max_items": 3, "summary_enabled": False, "summary_min_events": 2,
                        "summary_max_events": 12}
WORK_INDEX_VERSION = 1


def validate_work_memory_config(value):
    from .runtime import ContractError
    if not isinstance(value, dict) or set(value) - set(WORK_MEMORY_DEFAULTS):
        raise ContractError("unsupported work_memory settings")
    result = dict(WORK_MEMORY_DEFAULTS, **value)
    for key in ("enabled", "automatic_context", "summary_enabled"):
        if not isinstance(result[key], bool):
            raise ContractError(f"work_memory.{key} must be boolean")
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
    native = payload.get("toolCall", {})
    return payload.get("tool_name", native.get("name", "")), payload.get("tool_input", native.get("args", {}))


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


def _task_key(task_state):
    state = task_state or {}
    if not isinstance(state, dict):
        return _json(state)
    return _json({key: state.get(key) for key in ("id", "revision", "write_version", "assignment_id")})


class WorkMemoryMixin:
    def _work_capture_event(self, project, event_id, workspace, *, raw=None, native_context_hint=False):
        """调用者已保存事件和当前操作状态；不扩大历史读取范围。"""
        from .runtime import clean, clip, digest
        if not _config(self)["enabled"]:
            return None
        event = self.db.execute("SELECT * FROM events WHERE project=? AND id=?", (project, event_id)).fetchone()
        if not event or event["type"] not in {"PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit"}:
            return None
        event = dict(event)
        self.db.execute("INSERT OR IGNORE INTO work_event_checkouts VALUES(?,?,?)",
                        (project, event_id, checkout_key(workspace)))
        payload = json.loads(event["payload"])
        raw = clean(raw) if isinstance(raw, dict) else payload
        name, args = _tool(raw)
        stored_name, stored_args = _tool(payload)
        if _learning_tool(name or stored_name):
            return None
        links = file_links_from_payload(name, args, workspace)
        # PostToolUse 有时无输入，用同一 operation 的前置明确目标补关系。
        if not links and event.get("operation"):
            links = [dict(row) for row in self.db.execute(
                "SELECT relative_path,path_key,checkout_key,relation,version FROM work_file_links l "
                "JOIN events e ON e.id=l.event_id AND e.project=l.project WHERE l.project=? AND e.operation=? "
                "AND l.checkout_key=? ORDER BY e.created LIMIT 16", (project, event["operation"], checkout_key(workspace)))]
            for link in links:
                link["version"] = current_file_version(link["relative_path"], workspace)
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
        if event["type"] == "PostToolUse":
            if event["outcome"] in {"failed", "conflict"}:
                kind = "tool_failure"
            elif any(link["relation"] == "write_target" for link in links):
                kind = "file_change"
            elif _verification(stored_name or name, stored_args):
                kind = "verification"
            if not kind:
                return None
            response = payload.get("tool_response", payload.get("toolResult", payload.get("error", {})))
            text = clip(_json(response), 700)
            title = f"{stored_name or name or 'tool'}: {event['outcome']}"
        elif event["type"] == "Stop":
            text = payload.get("last_assistant_message", payload.get("message", ""))
            if not isinstance(text, str) or not text.strip():
                return None
            kind, title = "public_summary", "公开工作说明"
        else:
            text = payload.get("prompt", "")
            if not _correction(text):
                return None
            kind, title = "user_correction", "用户更正"
        body = {"type": kind, "title": clip(title, 160), "summary": clip(text, 1400),
                "validation_level": "reported" if kind in {"public_summary", "user_correction"} else "tool_observed",
                "task_write_version": event.get("task_write_version"),
                "assignment_id": event.get("assignment_id"),
                "coverage": {"projection_only": True, "truncated": bool(payload.get("truncated"))
                             or len(text) > 1400, "full_original_available": not bool(payload.get("truncated"))}}
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

    def _work_index(self, project, memory_id, body, links):
        from .knowledge_index import terms
        text = " ".join(terms(" ".join([body.get("title", ""), body.get("summary", ""),
                                        *(item.get("relative_path", "") for item in links)])))
        self.db.execute("DELETE FROM work_memory_fts WHERE project=? AND id=?", (project, memory_id))
        self.db.execute("INSERT INTO work_memory_fts VALUES(?,?,?)", (project, memory_id, text))

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
        if event["type"] in {"Stop", "UserPromptSubmit"}:
            state = "reported"
        signature = {key: current.get(key) for key in ("decision", "observation", "conflict", "state", "last_event")}
        return {"event_id": event_id, "capture_digest": event.get("capture_digest") or digest(json.loads(event["payload"])),
                "state": state, "source_version": digest([event["id"], event["payload"], signature])}

    def _work_item(self, project, row, *, workspace=None, file_paths=None):
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
        return {"memory_id": row["id"], "revision": row["revision"],
                "source_version": digest([row["revision"], row["body"], snapshots]), "state": state,
                "type": body["type"], "title": body["title"], "summary": body["summary"][:240],
                "validation_level": body["validation_level"], "source_event_refs": [source["event_id"] for source in sources],
                "files": visible_files, "files_count": len(files), "coverage": body.get("coverage", {}),
                "created": row["created"], "summary_only": True, "read_required": True,
                "permission_restored": False, "source_changed": changed,
                "task_binding_known": row["task_id"] is not None,
                "file_state": file_state, "file_focus": sorted(focus)}

    def _work_search_rows(self, project, query, *, file_path=None, workspace=None, limit=5,
                          conversation=None, agent_id=None, task_state=None):
        from .knowledge_index import normalized_query_plan
        from .runtime import ContractError
        if not isinstance(query, str) or len(query) > 8000:
            raise ContractError("work query must be text of at most 8000 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ContractError("work search limit must be 1..20")
        clauses, params = ["m.project=?"], [project]
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
            selected = normalized_query_plan(query)["terms"][:64]
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
        rows = self.db.execute("SELECT m.* FROM work_memories m" + joins + " WHERE " + " AND ".join(clauses)
                               + " ORDER BY m.created DESC,m.id DESC LIMIT ?", (*params, min(100, limit * 5))).fetchall()
        if query.strip():
            selected = normalized_query_plan(query)["terms"][:64]
            from .knowledge_index import terms
            def score(row):
                body = json.loads(row["body"])
                values = set(terms(body["title"] + " " + body["summary"]))
                return (sum(term in values for term in selected), row["created"], row["id"])
            rows = sorted(rows, key=score, reverse=True)
        return rows[:limit]

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

    def work_read(self, project, memory_id, *, expected_revision=None, offset=0, max_chars=6000, section="summary"):
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
            result = self._work_item(project, row)
            body = json.loads(row["body"])
            if section == "summary":
                text = body["summary"]
            else:
                evidence = []
                for ref in result["source_event_refs"]:
                    event = self.db.execute("SELECT id,type,outcome,payload,capture_digest FROM events WHERE project=? AND id=?", (project, ref)).fetchone()
                    evidence.append(dict(event) if event else {"id": ref, "state": "missing"})
                text = _json({"projection_only": True, "events": evidence})
            if offset > len(text):
                raise ContractError("offset exceeds work section length")
            result.pop("summary", None)
            result.update(status="ok", section=section, section_length=len(text), offset=offset, content="", next_offset=None)
            # metadata 非正文也必须可容纳，不裁断 JSON 身份或证据状态。
            while len(_json(result)) > max_chars - 128 and result["files"]:
                result["files"].pop()
            if len(_json(result)) > max_chars - 128:
                result["source_event_refs"] = result["source_event_refs"][:1]
                result["source_refs_truncated"] = True
            low, high = 0, len(text) - offset
            while low < high:
                middle = (low + high + 1) // 2
                result.update(content=text[offset:offset + middle], next_offset=offset + middle if offset + middle < len(text) else None)
                if len(_json(result)) <= max_chars:
                    low = middle
                else:
                    high = middle - 1
            result.update(content=text[offset:offset + low], next_offset=offset + low if offset + low < len(text) else None)
            result["truncated"] = result["next_offset"] is not None
            while len(_json(result)) > max_chars and result["content"]:
                low -= 1
                result.update(content=text[offset:offset + low], next_offset=offset + low, truncated=True)
            if len(_json(result)) > max_chars or (not low and offset < len(text)):
                raise ContractError("budget cannot fit work identity and page")
            return result

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
                 context_epoch, _task_key(task_state), delivery, method, now, file_state))
            count += 1
        # 只保留展示台账；不改变工作记录或现有知识采用状态。
        self.db.execute("DELETE FROM work_exposures WHERE project=? AND conversation=? AND agent=? AND memory_id NOT IN (SELECT memory_id FROM work_exposures WHERE project=? AND conversation=? AND agent=? ORDER BY created DESC,memory_id DESC LIMIT 100)",
            (project, conversation, agent_id or "", project, conversation, agent_id or ""))
        return {"recorded": count, "delivery": delivery, "model_read_confirmed": False}

    def work_exposure_updates(self, project, conversation, agent_id=None, limit=3):
        result = []
        with _snapshot(self):
            rows = self.db.execute("SELECT m.*,x.source_version AS shown_version FROM work_exposures x JOIN work_memories m ON m.project=x.project AND m.id=x.memory_id WHERE x.project=? AND x.conversation=? AND x.agent=? ORDER BY x.created DESC LIMIT 100",
                                   (project, conversation, agent_id or "")).fetchall()
            for row in rows:
                item = self._work_item(project, row)
                if item["source_version"] != row["shown_version"]:
                    item["source_changed"] = True
                    result.append(item)
                    if len(result) >= limit:
                        break
        return result

    def work_context_items(self, project, conversation, query="", *, workspace=None, raw=None,
                           agent_id=None, limit=3, task_state=None, delivery="persistent", compact=False,
                           context_epoch=None, file_only=False):
        if not _config(self)["enabled"] or not _config(self)["automatic_context"]:
            return []
        with _snapshot(self):
            name, args = _tool(raw or {})
            links = file_links_from_payload(name, args, workspace, observe_version=False) if workspace else []
            result = self.work_exposure_updates(project, conversation, agent_id, limit=limit)
            if not links and not query.strip() and workspace and not file_only:
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
                rows.extend(self._work_search_rows(project, query,
                    file_path=str(checkout_root(workspace) / link["relative_path"]), workspace=workspace, limit=limit))
            if not links:
                # 查询有关键词时跨会话相关召回，无关键词仅当前会话。
                rows = self._work_search_rows(project, query, limit=limit,
                    conversation=None if query.strip() else conversation, agent_id=agent_id, task_state=task_state)
            seen = {item["memory_id"] for item in result}
            for row in rows:
                item = self._work_item(project, row, workspace=workspace,
                                       file_paths=[link["relative_path"] for link in links[:3]])
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
                        and previous["task_key"] == _task_key(task_state)):
                    continue
                result.append(item)
                seen.add(item["memory_id"])
                if len(result) >= limit:
                    break
            return result[:limit]

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
                    result = self._work_capture_event(project, row["id"], workspace)
                    indexed += bool(result and result.get("indexed"))
                    reindexed += bool(result and result.get("reindexed"))
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
                if (source is None or source["conversation"] != conversation or source["task_id"] != task_id
                        or source["task_revision"] != task_revision):
                    raise ContractError("work summary source scope mismatch")
                rows.append(source)
                sources.append(current)
            if len({row["workspace_key"] for row in rows}) != 1 or len({row["agent"] for row in rows}) != 1:
                raise ContractError("work summary mixes checkout or agent identities")
            projected = clean({"type": "work_summary", "title": body["title"], "summary": body["summary"],
                "validation_level": "model_reported", "coverage": {"projection_only": True, "truncated": any(
                    json.loads(row["body"])["coverage"].get("truncated", False) for row in rows)},
                "task_write_version": body.get("task_write_version")})
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
                current = self._work_item(project, row, workspace=workspace, file_paths=item.get("file_focus"))
                if item.get("task_affinity") == "context_only":
                    current["task_affinity"] = "context_only"
                if item.get("file_state_changed"):
                    current["file_state_changed"] = True
                result.append(current)
        return result
