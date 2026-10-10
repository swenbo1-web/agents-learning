"""SQLite 是记录和发布清单的权威；外部记忆服务只是可选索引。

只保证本服务及已覆盖 Hook 入口的约束，不推断自然语言授权或业务验收。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

CATEGORIES = {"operation", "professional", "project", "mixed", "unknown"}
METADATA_KEYS = {"tool", "tool_version", "error_code", "environment", "verification"}
DEFAULTS = {
    "injection_max_chars": 2400, "event_max_chars": 6000,
    "enforce_task_policy": False, "capture_deliveries": True, "allowed_tools": [], "project_aliases": {},
    "admin": {"enabled": False, "command": ["codex"], "max_calls_per_day": 5,
              "max_input_chars": 16000, "timeout_seconds": 120, "auto_publish": False},
    "hindsight": {"enabled": False, "url": "http://127.0.0.1:8888", "bank_id": "",
                  "api_key_env": "HINDSIGHT_API_KEY", "strategy": "alp_chunks",
                  "timeout_seconds": 10},
}


class ContractError(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def clean(value):
    """减少常见凭证进入记录；不能代替专门的数据脱敏策略。"""
    if isinstance(value, dict):
        return {k: "[REDACTED]" if re.search(r"(?i)(password|secret|api.?key|authorization|token)$", k)
                else clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
        value = re.sub(r"(?i)((?:api[_-]?key|password|secret|access_token)\s*[=:]\s*)[^\s,;]+",
                       r"\1[REDACTED]", value)
    return value


def clip(text, limit):
    """保留首尾，错误原因常在日志末尾；字符预算不等于 token 预算。"""
    if len(text) <= limit:
        return text
    marker = "\n...[truncated]...\n"
    if limit <= len(marker):
        return text[:max(0, limit)]
    head = (limit - len(marker)) // 2
    return text[:head] + marker + text[-(limit - len(marker) - head):]


def metadata(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - METADATA_KEYS or any(
            not isinstance(v, str) or len(v) > 500 for v in value.values()):
        raise ContractError("metadata must contain supported string fields")
    return {k: clean(v).strip() for k, v in value.items() if v.strip()}


def knowledge_fingerprint(body):
    # 来源不同不应制造重复知识；适用条件不同必须保留为不同条目。
    return digest({k: body.get(k, {}) if k == "metadata" else body[k] for k in
                   ("title", "content", "category", "applicability", "limitations", "validation_level", "metadata")})


def project_event(value, text_limit):
    if isinstance(value, str):
        return clip(value, text_limit)
    if isinstance(value, list):
        items = [project_event(v, text_limit) for v in value[:20]]
        return items + ([{"truncated_items": len(value) - 20}] if len(value) > 20 else [])
    if isinstance(value, dict):
        # 返回码、工具身份等结构字段优先；正文首尾裁剪，不转换为不可解析的大字符串。
        keys = list(value)
        priorities = {"tool_name", "exit_code", "exitCode", "session_id", "isError", "success", "tool_response", "tool_input"}
        keys.sort(key=lambda key: key not in priorities)
        result = {k: project_event(value[k], text_limit) for k in keys[:30]}
        if len(keys) > 30:
            result["truncated_fields"] = len(keys) - 30
        return result
    return value


def terms(text):
    # 中文二元片段用于本地 FTS；查询长度和词数受限，不扫描项目文档。
    words = re.findall(r"[a-z0-9_./:-]{2,}|[\u3400-\u9fff]+", text.lower())
    result = []
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            result.extend(word[i:i + 2] for i in range(max(1, len(word) - 1)))
        else:
            result.append(word)
    return list(dict.fromkeys(result))


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0,
 revocation_epoch INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS project_paths(path TEXT PRIMARY KEY,project TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_projects(conversation TEXT PRIMARY KEY,project TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, project TEXT NOT NULL, conversation TEXT NOT NULL,
 revision INTEGER NOT NULL, goal TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL,
 updated REAL NOT NULL, UNIQUE(project,conversation));
CREATE TABLE IF NOT EXISTS task_snapshots(project TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,
 conversation TEXT NOT NULL,goal TEXT NOT NULL,scope TEXT NOT NULL,status TEXT NOT NULL,updated REAL NOT NULL,
 PRIMARY KEY(project,id,revision));
CREATE TABLE IF NOT EXISTS contexts(project TEXT NOT NULL, conversation TEXT NOT NULL,
 epoch INTEGER NOT NULL DEFAULT 0, injected TEXT NOT NULL DEFAULT '[]', last_query TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(project,conversation));
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, project TEXT NOT NULL, conversation TEXT NOT NULL,
 task_id TEXT, task_revision INTEGER, agent TEXT, operation TEXT, type TEXT NOT NULL,
 outcome TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS events_scope ON events(project,conversation,created);
CREATE TABLE IF NOT EXISTS operations(project TEXT NOT NULL, id TEXT NOT NULL, conversation TEXT NOT NULL,
 task_id TEXT, task_revision INTEGER, agent TEXT, state TEXT NOT NULL, last_event TEXT NOT NULL,
 PRIMARY KEY(project,id));
CREATE TABLE IF NOT EXISTS problems(id TEXT PRIMARY KEY, project TEXT NOT NULL, conversation TEXT NOT NULL,
 task_id TEXT, task_revision INTEGER, revision INTEGER NOT NULL, summary TEXT NOT NULL,
 category TEXT NOT NULL, status TEXT NOT NULL, knowledge_id TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS problem_events(problem TEXT NOT NULL, event TEXT NOT NULL,
 PRIMARY KEY(problem,event));
CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY, project TEXT NOT NULL, problem TEXT NOT NULL,
 problem_revision INTEGER NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS knowledge(project TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,
 body TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project,id,revision));
CREATE TABLE IF NOT EXISTS knowledge_fingerprints(project TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,
 fingerprint TEXT NOT NULL,PRIMARY KEY(project,id,revision));
CREATE INDEX IF NOT EXISTS knowledge_duplicates ON knowledge_fingerprints(project,fingerprint);
CREATE TABLE IF NOT EXISTS active(project TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,
 PRIMARY KEY(project,id));
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(project UNINDEXED,id UNINDEXED,
 revision UNINDEXED,text);
CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,project TEXT NOT NULL,kind TEXT NOT NULL,
 payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
 next_try REAL NOT NULL DEFAULT 0,lease TEXT,lease_until REAL NOT NULL DEFAULT 0,error TEXT);
CREATE TABLE IF NOT EXISTS admin_calls(id TEXT PRIMARY KEY,project TEXT NOT NULL,problem TEXT NOT NULL,
 revision INTEGER NOT NULL,day TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS problems_pending ON problems(project,status,created);
CREATE INDEX IF NOT EXISTS operations_task ON operations(project,conversation,task_id,task_revision,state);
CREATE INDEX IF NOT EXISTS calls_problem ON admin_calls(project,problem,revision);
CREATE TABLE IF NOT EXISTS metrics(project TEXT NOT NULL,name TEXT NOT NULL,value INTEGER NOT NULL,
 PRIMARY KEY(project,name));
CREATE TABLE IF NOT EXISTS deliveries(project TEXT NOT NULL,task_id TEXT NOT NULL,revision INTEGER NOT NULL,
 problem_id TEXT NOT NULL,PRIMARY KEY(project,task_id,revision));
"""


class Runtime:
    def __init__(self, home=None, config=None):
        default_home = (Path(os.environ["LOCALAPPDATA"]) / "AgentLearning"
                        if os.environ.get("LOCALAPPDATA") else Path.home() / ".local/share/agent-learning")
        self.home = Path(home or os.environ.get("ALP_HOME") or default_home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        config_path = Path(os.environ.get("ALP_CONFIG", self.home / "config.json"))
        supplied = config if config is not None else (
            json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.is_file() else {})
        self.config = json.loads(encode(DEFAULTS))
        for key, value in supplied.items():
            if key in {"admin", "hindsight"} and isinstance(value, dict):
                self.config[key].update(value)
            else:
                self.config[key] = value
        for key in ("injection_max_chars", "event_max_chars"):
            if not isinstance(self.config[key], int) or not 128 <= self.config[key] <= 100000:
                raise ContractError(f"invalid {key}")
        for value in (self.config["enforce_task_policy"], self.config["capture_deliveries"],
                      self.config["admin"]["enabled"], self.config["admin"]["auto_publish"], self.config["hindsight"]["enabled"]):
            if not isinstance(value, bool):
                raise ContractError("enable flags must be booleans")
        if not isinstance(self.config["allowed_tools"], list) or not all(isinstance(v, str) for v in self.config["allowed_tools"]):
            raise ContractError("allowed_tools must be a string array")
        if not isinstance(self.config["project_aliases"], dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                                        for k, v in self.config["project_aliases"].items()):
            raise ContractError("project_aliases must map workspace paths to project IDs")
        self.db = sqlite3.connect(self.home / "runtime.sqlite3", timeout=1.5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1, 2}:
            self.db.close()
            raise ContractError("unsupported runtime database version")
        self.db.executescript(SCHEMA)
        if version < 2:
            self._migrate()

    def _migrate(self):
        """原地升级并保留原记录；并发启动时在锁内重新检查列。"""
        with self.transaction():
            for table, additions in {
                    "contexts": {"shown": "TEXT NOT NULL DEFAULT '[]'"},
                    "admin_calls": {"started": "REAL", "finished": "REAL", "usage": "TEXT", "fingerprint": "TEXT"}}.items():
                existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                for column, declaration in additions.items():
                    if column not in existing:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            self.db.execute("UPDATE contexts SET shown=injected WHERE shown='[]'")
            # 旧任务当前行可能已经被替换，优先从明确 TaskUpdate 事件恢复原快照。
            for row in self.db.execute("SELECT * FROM events WHERE type='TaskUpdate' ORDER BY created").fetchall():
                body = clean(json.loads(row["payload"]))
                if row["task_id"] and isinstance(body.get("goal"), str) and isinstance(body.get("scope"), dict):
                    self.db.execute("INSERT OR REPLACE INTO task_snapshots VALUES(?,?,?,?,?,?,?,?)",
                                    (row["project"], row["task_id"], row["task_revision"], row["conversation"],
                                     body["goal"], encode(body["scope"]), body.get("status", "unknown"), row["created"]))
            for row in self.db.execute("SELECT * FROM tasks").fetchall():
                goal, scope = clean(row["goal"]), encode(clean(json.loads(row["scope"])))
                self.db.execute("UPDATE tasks SET goal=?,scope=? WHERE id=?", (goal, scope, row["id"]))
                self.db.execute("INSERT OR REPLACE INTO task_snapshots VALUES(?,?,?,?,?,?,?,?)",
                                (row["project"], row["id"], row["revision"], row["conversation"], goal, scope, row["status"], row["updated"]))
            for row in self.db.execute("SELECT id,summary FROM problems").fetchall():
                self.db.execute("UPDATE problems SET summary=? WHERE id=?", (clean(row["summary"]), row["id"]))
            for table in ("candidates", "knowledge"):
                for row in self.db.execute(f"SELECT rowid,body FROM {table}").fetchall():
                    self.db.execute(f"UPDATE {table} SET body=? WHERE rowid=?", (encode(clean(json.loads(row["body"]))), row["rowid"]))
            for row in self.db.execute("SELECT id,payload FROM outbox").fetchall():
                self.db.execute("UPDATE outbox SET payload=? WHERE id=?", (encode(clean(json.loads(row["payload"]))), row["id"]))
            for row in self.db.execute("SELECT project,id,revision,body FROM knowledge").fetchall():
                self.db.execute("INSERT OR REPLACE INTO knowledge_fingerprints VALUES(?,?,?,?)",
                                (row["project"], row["id"], row["revision"], knowledge_fingerprint(clean(json.loads(row["body"])))))
            self.db.execute("PRAGMA user_version=2")

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def project(self, cwd):
        resolved = Path(cwd).resolve()
        path = os.path.normcase(str(resolved))
        aliases = self.config["project_aliases"]
        alias = next((v for k, v in sorted(aliases.items(), key=lambda pair: len(pair[0]), reverse=True)
                      if path == os.path.normcase(str(Path(k).resolve())) or
                      path.startswith(os.path.normcase(str(Path(k).resolve())) + os.sep)), None)
        known = self.db.execute("SELECT path,project FROM project_paths ORDER BY length(path) DESC").fetchall()
        registered = next((r for r in known if path == r["path"] or path.startswith(r["path"] + os.sep)), None)
        git_root = next((p for p in (resolved, *resolved.parents) if (p / ".git").exists()), None)
        root = git_root or resolved
        root_path = os.path.normcase(str(root))
        exact = next((r for r in known if r["path"] == root_path), None)
        project = alias or ((exact["project"] if exact else digest(root_path)[:24]) if git_root
                            else registered["project"] if registered else digest(root_path)[:24])
        self.db.execute("INSERT OR IGNORE INTO projects(id) VALUES(?)", (project,))
        self.db.execute("INSERT OR IGNORE INTO project_paths VALUES(?,?)", (root_path if not alias else path, project))
        return project

    def bind_session(self, project, conversation_id):
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ContractError("conversation_id is required")
        self.db.execute("INSERT OR IGNORE INTO session_projects VALUES(?,?)", (conversation_id, project))
        row = self.db.execute("SELECT project FROM session_projects WHERE conversation=?", (conversation_id,)).fetchone()
        if row[0] != project:
            raise ContractError("conversation is already bound to a different workspace")
        return project

    def task(self, project, conversation):
        row = self.db.execute("SELECT * FROM tasks WHERE project=? AND conversation=?",
                              (project, conversation)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["scope"] = json.loads(item["scope"])
        return item

    def task_update(self, project, conversation_id, goal, scope=None, expected_revision=None, status="active", new_task=False):
        if not conversation_id or not isinstance(goal, str) or not goal.strip():
            raise ContractError("conversation_id and goal are required")
        if status not in {"active", "delivered", "cancelled", "interrupted"}:
            raise ContractError("invalid task status")
        if scope is not None and not isinstance(scope, dict):
            raise ContractError("scope must be an object")
        with self.transaction():
            self.bind_session(project, conversation_id)
            previous = self.task(project, conversation_id)
            revision = previous["revision"] if previous else 0
            effective_scope = scope if scope is not None else previous["scope"] if previous and not new_task else {}
            goal, effective_scope = clean(goal), clean(effective_scope)
            if expected_revision is not None and revision != expected_revision:
                raise ContractError("stale task revision")
            if previous and not new_task and (previous["goal"], previous["scope"], previous["status"]) == (goal, effective_scope, status):
                return previous
            changed_scope = not previous or previous["goal"] != goal or previous["scope"] != effective_scope
            next_revision = 1 if new_task or not previous else revision + int(changed_scope)
            task_id = previous["id"] if previous and not new_task else str(uuid.uuid4())
            self.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project,conversation) "
                            "DO UPDATE SET id=excluded.id,revision=excluded.revision,goal=excluded.goal,scope=excluded.scope,"
                            "status=excluded.status,updated=excluded.updated",
                            (task_id, project, conversation_id, next_revision, goal, encode(effective_scope), status, time.time()))
            self.db.execute("INSERT OR REPLACE INTO task_snapshots VALUES(?,?,?,?,?,?,?,?)",
                            (project, task_id, next_revision, conversation_id, goal, encode(effective_scope), status, time.time()))
            self._event(project, conversation_id, "TaskUpdate", {"goal": goal, "scope": effective_scope, "status": status},
                        task_id=task_id, task_revision=next_revision)
            if changed_scope or new_task:
                self.db.execute("UPDATE contexts SET injected='[]',last_query='' WHERE project=? AND conversation=?", (project, conversation_id))
        return self.task(project, conversation_id)

    def task_snapshot(self, project, task_id, revision):
        row = self.db.execute("SELECT * FROM task_snapshots WHERE project=? AND id=? AND revision=?",
                              (project, task_id, revision)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["scope"] = json.loads(result["scope"])
        return clean(result)

    def _event(self, project, conversation, kind, payload, event_id=None, task_id=None,
               task_revision=None, agent_id=None, operation_id=None, outcome="unknown"):
        sanitized = clean(payload)
        text = encode(sanitized)
        if len(text) > self.config["event_max_chars"]:
            limit = self.config["event_max_chars"] // 4
            while True:
                projected = project_event(sanitized, limit)
                projected.update(truncated=True, original_digest=digest(sanitized))
                text = encode(projected)
                if len(text) <= self.config["event_max_chars"] or limit <= 32:
                    break
                limit //= 2
            if len(text) > self.config["event_max_chars"]:
                # 极小预算保留可观察状态。正常默认预算下保留命令与日志首尾。
                projected = {"truncated": True, "tool_name": clip(str(sanitized.get("tool_name", "")), 32)}
                response = sanitized.get("tool_response")
                if isinstance(response, dict):
                    projected["tool_response"] = {k: v for k, v in response.items()
                                                  if k in {"exit_code", "exitCode", "success", "isError"} and isinstance(v, (int, bool))}
                text = encode(projected)
        ident = digest([project, event_id]) if event_id else str(uuid.uuid4())
        old = self.db.execute("SELECT * FROM events WHERE id=?", (ident,)).fetchone()
        if old:
            if old["payload"] != text or old["type"] != kind or old["conversation"] != conversation:
                raise ContractError("event id reused with different content")
            return dict(old), False
        self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (ident, project, conversation, task_id, task_revision, agent_id,
                         operation_id, kind, outcome, text, time.time()))
        return dict(self.db.execute("SELECT * FROM events WHERE id=?", (ident,)).fetchone()), True

    def record_event(self, project, conversation, kind, payload, **kwargs):
        with self.transaction():
            return self._event(project, conversation, kind, payload, **kwargs)[0]

    def _problem(self, project, conversation, summary, category, event_ids, knowledge_id=None, task_binding=None):
        summary = clip(clean(summary), 2000)
        task = task_binding if task_binding is not None else self.task(project, conversation)
        task_id, revision = (task["id"], task["revision"]) if task else (None, None)
        ident = digest([project, conversation, task_id, revision, category, summary, knowledge_id])[:32]
        self.db.execute("INSERT OR IGNORE INTO problems VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (ident, project, conversation, task_id, revision, 0, summary, category,
                         "pending", knowledge_id, time.time()))
        changed = 0
        for event in event_ids:
            changed += self.db.execute("INSERT OR IGNORE INTO problem_events VALUES(?,?)", (ident, event)).rowcount
        if changed:
            self.db.execute("UPDATE problems SET revision=revision+1,status='pending' WHERE id=?", (ident,))
        return dict(self.db.execute("SELECT * FROM problems WHERE id=?", (ident,)).fetchone())

    def feedback(self, project, conversation_id, summary, category="unknown", event_refs=None,
                 resolution="", knowledge_id=None):
        if category not in CATEGORIES or not summary.strip():
            raise ContractError("valid category and summary are required")
        refs = list(event_refs or [])
        with self.transaction():
            self.bind_session(project, conversation_id)
            task = self.task(project, conversation_id)
            for ref in refs:
                if not self.db.execute("SELECT 1 FROM events WHERE id=? AND project=? AND conversation=? "
                                       "AND task_id IS ? AND task_revision IS ?",
                                       (ref, project, conversation_id, task["id"] if task else None,
                                        task["revision"] if task else None)).fetchone():
                    raise ContractError("evidence must belong to the current project, conversation and task revision")
            event, _ = self._event(project, conversation_id, "Feedback",
                                   {"summary": summary, "resolution": resolution, "knowledge_id": knowledge_id},
                                   task_id=task["id"] if task else None,
                                   task_revision=task["revision"] if task else None, outcome="reported")
            return self._problem(project, conversation_id, summary, category, refs + [event["id"]], knowledge_id)

    def problems(self, project, limit=20):
        return [dict(r) for r in self.db.execute("SELECT * FROM problems WHERE project=? AND status='pending' "
                                               "ORDER BY created LIMIT ?", (project, max(1, min(limit, 100))))]

    def review_packet(self, project, problem_id):
        problem = self.db.execute("SELECT * FROM problems WHERE id=? AND project=?", (problem_id, project)).fetchone()
        if not problem:
            raise ContractError("unknown problem")
        count = self.db.execute("SELECT COUNT(*) FROM problem_events WHERE problem=?", (problem_id,)).fetchone()[0]
        events = [dict(r) for r in self.db.execute("SELECT e.* FROM events e JOIN problem_events p ON p.event=e.id "
                                                 "WHERE p.problem=? ORDER BY e.created DESC LIMIT 40", (problem_id,))]
        max_chars = int(self.config["admin"]["max_input_chars"])
        if not 1000 <= max_chars <= 200000:
            raise ContractError("invalid admin input budget")
        selected, used = [], 0
        # 晚到的新证据优先；明确标记截断，不把部分证据写成完整。
        truncated = False
        for event in events:
            event["payload"] = json.loads(event["payload"])
            truncated |= bool(event["payload"].get("truncated"))
            size = len(encode(event))
            if used + size > max_chars - 2000:
                continue
            selected.append(event)
            used += size
        task = self.task_snapshot(project, problem["task_id"], problem["task_revision"]) if problem["task_id"] else None
        query = problem["summary"] + " " + (task["goal"] if task else "")
        related = self.search(project, query, 3)["items"]
        summaries = [{"knowledge_id": item["knowledge_id"], "revision": item["revision"],
                      "title": clip(item["title"], 120), "content": clip(item["content"], 350),
                      "applicability": clip(item["applicability"], 160), "metadata": item.get("metadata", {})} for item in related]
        candidate_rows = self.db.execute("SELECT id,body FROM candidates WHERE project=? AND problem<>? ORDER BY created DESC LIMIT 30",
                                         (project, problem_id)).fetchall()
        query_terms = set(terms(query))
        candidate_summaries = []
        for row in candidate_rows:
            body = clean(json.loads(row["body"]))
            if query_terms.intersection(terms(body["title"] + " " + body["content"])):
                candidate_summaries.append({"candidate_id": row["id"], "title": clip(body["title"], 120),
                                            "content": clip(body["content"], 350), "applicability": clip(body["applicability"], 160)})
                if len(candidate_summaries) == 3:
                    break
        packet = {"problem": clean(dict(problem)), "task": task, "events": list(reversed(selected)),
                  "incomplete": len(selected) != count, "evidence_truncated": truncated, "evidence_count": count,
                  "related_knowledge": summaries, "related_candidates": candidate_summaries}
        # 去重提示不能挤占真实证据；完整输入仍由管理员在调用前核预算。
        while len(encode(packet)) > max_chars - 500 and (packet["related_candidates"] or packet["related_knowledge"]):
            (packet["related_candidates"] or packet["related_knowledge"]).pop()
        return packet

    def submit_candidate(self, project, **body):
        required = {"problem_id", "problem_revision", "title", "content", "category", "applicability",
                    "limitations", "evidence_refs", "validation_level"}
        if not required <= set(body) or set(body) - required - {"metadata"}:
            raise ContractError("candidate fields do not match schema")
        if body["category"] not in CATEGORIES or body["validation_level"] not in {"reported", "tool_success"}:
            raise ContractError("invalid candidate category or evidence level")
        if not isinstance(body["problem_revision"], int) or isinstance(body["problem_revision"], bool):
            raise ContractError("problem_revision must be an integer")
        if any(not isinstance(body[key], str) or not body[key].strip() for key in
               ("title", "content", "applicability", "limitations")):
            raise ContractError("candidate text fields must be nonempty strings")
        if (not isinstance(body["problem_id"], str) or not isinstance(body["evidence_refs"], list)
                or not body["evidence_refs"] or not all(isinstance(ref, str) for ref in body["evidence_refs"])):
            raise ContractError("at least one source event is required")
        body = clean(body)
        body["metadata"] = metadata(body.get("metadata"))
        if len(encode(body)) > 30000:
            raise ContractError("candidate is too large")
        ident = digest([project, body])[:32]
        with self.transaction():
            self._validate_sources(project, body)
            self.db.execute("INSERT OR IGNORE INTO candidates VALUES(?,?,?,?,?,?,?)",
                            (ident, project, body["problem_id"], body["problem_revision"], encode(body), "candidate", time.time()))
        return {"candidate_id": ident, "status": "candidate"}

    def _validate_sources(self, project, body):
        problem = self.db.execute("SELECT * FROM problems WHERE project=? AND id=?", (project, body["problem_id"])).fetchone()
        if not problem:
            raise ContractError("unknown problem")
        if body["problem_revision"] != problem["revision"]:
            raise ContractError("problem changed; review new evidence")
        if problem["task_id"] and not self.task_snapshot(project, problem["task_id"], problem["task_revision"]):
            raise ContractError("source task snapshot is missing")
        successful = False
        for ref in body["evidence_refs"]:
            event = self.db.execute("SELECT e.* FROM events e JOIN problem_events p ON e.id=p.event "
                                    "WHERE e.id=? AND e.project=? AND p.problem=?", (ref, project, problem["id"])).fetchone()
            if not event:
                raise ContractError("source is absent or outside this problem")
            if event["task_id"] != problem["task_id"] or event["task_revision"] != problem["task_revision"]:
                raise ContractError("source belongs to another task revision")
            successful |= event["type"] == "PostToolUse" and event["outcome"] == "succeeded"
        if body["validation_level"] == "tool_success" and not successful:
            raise ContractError("no observed completed tool success")

    def _queue_sync(self, project, knowledge_id, revision, body):
        if not self.config["hindsight"]["enabled"]:
            return 0
        target = clean(self.config["hindsight"])
        payload = {"knowledge_id": knowledge_id, "revision": revision, "body": clean(body), "target": target}
        return self.db.execute("INSERT OR IGNORE INTO outbox(id,project,kind,payload) VALUES(?,?,?,?)",
                               (digest([project, knowledge_id, revision, self._target_identity(target)]),
                                project, "published_sync", encode(payload))).rowcount

    @staticmethod
    def _target_identity(target):
        return {k: target.get(k) for k in ("url", "bank_id", "api_key_env", "strategy")}

    def enqueue_sync(self, project):
        if not self.config["hindsight"]["enabled"]:
            return {"status": "disabled", "enqueued": 0}
        with self.transaction():
            rows = self.db.execute("SELECT k.* FROM knowledge k JOIN active a ON k.project=a.project AND k.id=a.id "
                                   "AND k.revision=a.revision WHERE k.project=?", (project,)).fetchall()
            count = sum(self._queue_sync(project, row["id"], row["revision"], json.loads(row["body"])) for row in rows)
        return {"status": "enqueued", "enqueued": count}

    def publish(self, project, candidate_id, expected_generation=None, knowledge_id=None, expected_revision=None):
        with self.transaction():
            candidate = self.db.execute("SELECT * FROM candidates WHERE id=? AND project=?", (candidate_id, project)).fetchone()
            if not candidate:
                raise ContractError("unknown candidate")
            body = clean(json.loads(candidate["body"]))
            body["metadata"] = metadata(body.get("metadata"))
            self._validate_sources(project, body)
            version = self.db.execute("SELECT generation FROM projects WHERE id=?", (project,)).fetchone()[0]
            if expected_generation is not None and version != expected_generation:
                raise ContractError("publication manifest changed")
            if knowledge_id is not None:
                if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
                    raise ContractError("knowledge updates require the expected active revision")
                row = self.db.execute("SELECT revision FROM active WHERE project=? AND id=?", (project, knowledge_id)).fetchone()
                if not row or row[0] != expected_revision:
                    raise ContractError("knowledge revision changed or is revoked")
            elif expected_revision is not None:
                raise ContractError("expected_revision requires a knowledge_id")
            else:
                duplicate = self.db.execute("SELECT a.id,a.revision,k.body FROM knowledge_fingerprints f JOIN active a "
                                            "ON a.project=f.project AND a.id=f.id AND a.revision=f.revision JOIN knowledge k "
                                            "ON k.project=a.project AND k.id=a.id AND k.revision=a.revision "
                                            "WHERE f.project=? AND f.fingerprint=? LIMIT 1",
                                            (project, knowledge_fingerprint(body))).fetchone()
                if duplicate:
                    self.db.execute("UPDATE candidates SET status='published' WHERE id=?", (candidate_id,))
                    self.db.execute("UPDATE problems SET status='reviewed' WHERE id=?", (body["problem_id"],))
                    self._queue_sync(project, duplicate["id"], duplicate["revision"], json.loads(duplicate["body"]))
                    return {"knowledge_id": duplicate["id"], "revision": duplicate["revision"], "status": "published", "deduplicated": True}
                knowledge_id = digest([project, "candidate", candidate_id])[:32]
            current = self.db.execute("SELECT k.* FROM knowledge k JOIN active a ON k.project=a.project AND "
                                      "k.id=a.id AND k.revision=a.revision WHERE k.project=? AND k.id=?",
                                      (project, knowledge_id)).fetchone()
            if current and current["body"] == encode(body):
                return {"knowledge_id": knowledge_id, "revision": current["revision"], "status": "published"}
            revision = self.db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM knowledge WHERE project=? AND id=?",
                                       (project, knowledge_id)).fetchone()[0]
            self.db.execute("INSERT INTO knowledge VALUES(?,?,?,?,?)", (project, knowledge_id, revision, encode(body), time.time()))
            self.db.execute("INSERT INTO knowledge_fingerprints VALUES(?,?,?,?)", (project, knowledge_id, revision, knowledge_fingerprint(body)))
            self.db.execute("INSERT INTO knowledge_fts VALUES(?,?,?,?)",
                            (project, knowledge_id, revision, " ".join(terms(" ".join(body[k] for k in
                             ("title", "content", "applicability", "category")) + " " + " ".join(body["metadata"].values())))))
            self.db.execute("INSERT INTO active VALUES(?,?,?) ON CONFLICT(project,id) DO UPDATE SET revision=excluded.revision",
                            (project, knowledge_id, revision))
            self.db.execute("UPDATE projects SET generation=generation+1 WHERE id=?", (project,))
            self.db.execute("UPDATE candidates SET status='published' WHERE id=?", (candidate_id,))
            self.db.execute("UPDATE problems SET status='reviewed' WHERE id=?", (body["problem_id"],))
            self._queue_sync(project, knowledge_id, revision, body)
        return {"knowledge_id": knowledge_id, "revision": revision, "status": "published"}

    def revoke(self, project, knowledge_id):
        with self.transaction():
            changed = self.db.execute("DELETE FROM active WHERE project=? AND id=?", (project, knowledge_id)).rowcount
            if changed:
                self.db.execute("UPDATE projects SET generation=generation+1,revocation_epoch=revocation_epoch+1 WHERE id=?", (project,))
        return {"knowledge_id": knowledge_id, "revoked": bool(changed)}

    def _public(self, project, row):
        body = clean(json.loads(row["body"]))
        evidence = []
        for ref in body["evidence_refs"]:
            event = self.db.execute("SELECT type,outcome,created FROM events WHERE project=? AND id=?", (project, ref)).fetchone()
            evidence.append({"event_id": ref, **(dict(event) if event else {"outcome": "missing"})})
        return {"knowledge_id": row["id"], "revision": row["revision"],
                **{k: body[k] for k in ("title", "content", "category", "applicability", "limitations", "validation_level")},
                "metadata": metadata(body.get("metadata")), "evidence": evidence}

    def read(self, project, knowledge_id):
        row = self.db.execute("SELECT k.* FROM knowledge k JOIN active a ON k.project=a.project AND k.id=a.id "
                              "AND k.revision=a.revision WHERE k.project=? AND k.id=?", (project, knowledge_id)).fetchone()
        if not row:
            raise ContractError("knowledge is not currently published")
        return self._public(project, row)

    def search(self, project, query, limit=5, filters=None):
        if not isinstance(query, str):
            raise ContractError("query must be a string")
        limit = max(1, min(int(limit), 10))
        filters = metadata(filters)
        selected = terms(query[-2000:])
        selected = selected[:12] + selected[-12:] if len(selected) > 24 else selected
        if not selected:
            return {"status": "no_match", "items": []}
        manifest = dict(self.db.execute("SELECT generation,revocation_epoch FROM projects WHERE id=?", (project,)).fetchone())
        key = digest([project, selected, limit, filters, manifest, "local-fts-v2"])
        cached = self.db.execute("SELECT body FROM cache WHERE key=?", (key,)).fetchone()
        if cached:
            ids = json.loads(cached[0])
        else:
            match = " OR ".join('"' + word.replace('"', '""') + '"' for word in selected)
            conditions = "".join(" AND json_extract(k.body,'$.metadata." + k + "')=?" for k in filters)
            rows = self.db.execute("SELECT knowledge_fts.id FROM knowledge_fts JOIN active a ON "
                                   "knowledge_fts.project=a.project AND knowledge_fts.id=a.id AND "
                                   "CAST(knowledge_fts.revision AS INTEGER)=a.revision JOIN knowledge k ON "
                                   "k.project=a.project AND k.id=a.id AND k.revision=a.revision "
                                   "WHERE knowledge_fts MATCH ? AND a.project=?" + conditions + " ORDER BY bm25(knowledge_fts) LIMIT ?",
                                   (match, project, *filters.values(), limit)).fetchall()
            ids = [r[0] for r in rows]
            self.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?)", (key, encode(ids)))
            self.db.execute("DELETE FROM cache WHERE rowid NOT IN (SELECT rowid FROM cache ORDER BY rowid DESC LIMIT 256)")
        # 缓存仅存ID；每次读取重新校验active，撤销后不会复用缓存正文。
        items = []
        for ident in ids:
            try:
                items.append(self.read(project, ident))
            except ContractError:
                pass
        self._metric(project, "search_queries")
        self._metric(project, "search_matches", int(bool(items)))
        return {"status": "matched" if items else "no_match", "items": items, **manifest}

    def _metric(self, project, name, value=1):
        self.db.execute("INSERT INTO metrics VALUES(?,?,?) ON CONFLICT(project,name) DO UPDATE SET value=value+excluded.value",
                        (project, name, value))

    def status(self, project):
        counts = {}
        for table in ("events", "tasks", "problems", "candidates", "active", "outbox"):
            counts[table] = self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE project=?", (project,)).fetchone()[0]
        counts["pending_problems"] = self.db.execute("SELECT COUNT(*) FROM problems WHERE project=? AND status='pending'", (project,)).fetchone()[0]
        observed = {r[0]: r[1] for r in self.db.execute("SELECT type,COUNT(*) FROM events WHERE project=? GROUP BY type", (project,))}
        outbox_states = {r[0]: r[1] for r in self.db.execute("SELECT state,COUNT(*) FROM outbox WHERE project=? GROUP BY state", (project,))}
        return {"project_id": project, "home": str(self.home), "counts": counts,
                "observed_events": observed, "outbox_states": outbox_states,
                "admin_enabled": bool(self.config["admin"]["enabled"]),
                "hindsight_enabled": bool(self.config["hindsight"]["enabled"]),
                "metrics": {r[0]: r[1] for r in self.db.execute("SELECT name,value FROM metrics WHERE project=?", (project,))},
                "admin_states": {r[0]: r[1] for r in self.db.execute("SELECT state,COUNT(*) FROM admin_calls WHERE project=? GROUP BY state", (project,))},
                "coverage": "configured local hooks only; unidentified agents remain unknown"}

    @staticmethod
    def outcome(response):
        if not isinstance(response, dict):
            return "unknown"
        exit_code = response.get("exit_code", response.get("exitCode"))
        if response.get("session_id") is not None and exit_code is None:
            return "running"
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            return "succeeded" if exit_code == 0 else "failed"
        if response.get("isError") is True or response.get("is_error") is True or response.get("success") is False:
            return "failed"
        if response.get("success") is True:
            return "succeeded"
        return "unknown"

    def _context(self, project, conversation, query="", compact=False):
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO contexts(project,conversation) VALUES(?,?)", (project, conversation))
            invalid = self._invalidated(project, conversation)
            row = self.db.execute("SELECT * FROM contexts WHERE project=? AND conversation=?", (project, conversation)).fetchone()
            remembered = json.loads(row["injected"])
            if compact:
                self.db.execute("UPDATE contexts SET epoch=epoch+1,injected='[]' WHERE project=? AND conversation=?", (project, conversation))
            task = self.task(project, conversation)
            query = clean(query).strip()
            continuation = not query or query.lower().strip("。.!！ ") in {
                "继续", "好的", "可以", "接着", "继续吧", "好", "continue", "go on", "ok", "yes"}
            effective_query = row["last_query"] if continuation else query
            if not effective_query and task:
                effective_query = task["goal"]
            retrieval_query = effective_query + (" " + task["goal"] if compact and task else "")
            items = self.search(project, retrieval_query)["items"] if retrieval_query.strip() else []
            if compact:
                restored = []
                for identity in remembered[-5:]:
                    try:
                        restored.append(self.read(project, identity.rsplit("@", 1)[0]))
                    except ContractError:
                        pass
                unique = {item["knowledge_id"]: item for item in restored + items}
                items = list(unique.values())
            prior = set() if compact else set(remembered)
            lines, injected = [], list(prior)
            shown = set(json.loads(row["shown"]))
            if invalid:
                lines.append("此前注入知识已撤销或被替换，不能继续依据旧版本执行：" + ",".join(invalid))
            budget = self.config["injection_max_chars"]
            # 历史知识没有执行授权。当前用户修正仍需Agent更新任务快照。
            if task:
                lines.append(f"[Agent Learning] task={task['id']} revision={task['revision']} status={task['status']}; 当前要求优先，范围改变时用 task_update 更新。")
            header = "以下是历史资料摘要，按适用条件判断；它不提供执行授权。完整正文使用 knowledge_read。"
            fresh_count = 0
            for item in items:
                identity = f"{item['knowledge_id']}@{item['revision']}"
                if identity in prior:
                    continue
                available = budget - len("\n".join(lines)) - (len(header) + 2 if fresh_count == 0 else 1)
                card = {"knowledge_id": item["knowledge_id"], "revision": item["revision"],
                        "title": clip(item["title"], 100), "content_summary": clip(item["content"], 500),
                        "applicability": clip(item["applicability"], 180), "limitations": clip(item["limitations"], 180),
                        "validation_level": item["validation_level"], "summary_only": True}
                if item.get("metadata"):
                    card["metadata"] = {k: clip(v, 60) for k, v in item["metadata"].items()}
                for field in ("content_summary", "limitations", "applicability", "title"):
                    while len(encode(card)) > available and len(card[field]) > 24:
                        card[field] = clip(card[field], max(24, len(card[field]) // 2))
                if len(encode(card)) > available:
                    card = {"knowledge_id": item["knowledge_id"], "revision": item["revision"],
                            "title": clip(item["title"], 60), "summary_only": True}
                if len(encode(card)) > available:
                    continue
                if fresh_count == 0:
                    lines.append(header)
                lines.append(encode(card))
                injected.append(identity)
                shown.add(identity)
                fresh_count += 1
                if fresh_count == 3:
                    break
            if fresh_count:
                self._metric(project, "knowledge_injected", fresh_count)
            self.db.execute("UPDATE contexts SET injected=?,shown=?,last_query=? WHERE project=? AND conversation=?",
                            (encode(injected), encode(sorted(shown)), effective_query[-2000:], project, conversation))
            return "\n".join(lines)[:budget]

    def _invalidated(self, project, conversation):
        row = self.db.execute("SELECT injected,shown FROM contexts WHERE project=? AND conversation=?", (project, conversation)).fetchone()
        if not row:
            return []
        valid, invalid = [], []
        identities = set(json.loads(row[0])) | set(json.loads(row[1]))
        max_invalid = max(1, (self.config["injection_max_chars"] - 120) // 40)
        for identity in sorted(identities):
            ident, revision = identity.rsplit("@", 1)
            exists = self.db.execute("SELECT 1 FROM active WHERE project=? AND id=? AND revision=?",
                                     (project, ident, int(revision))).fetchone()
            (invalid if not exists and len(invalid) < max_invalid else valid).append(identity)
        if invalid:
            invalid_set = set(invalid)
            self.db.execute("UPDATE contexts SET injected=?,shown=? WHERE project=? AND conversation=?",
                            (encode([i for i in json.loads(row[0]) if i not in invalid_set]),
                             encode([i for i in json.loads(row[1]) if i not in invalid_set]), project, conversation))
        return invalid

    def handle_hook(self, raw):
        kind = raw.get("hook_event_name")
        supported = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "Interrupt", "SubagentStart", "SubagentStop"}
        if kind not in supported:
            return {"systemMessage": "Agent Learning: 未覆盖的事件，未解释其状态。"}
        if not raw.get("session_id") or not raw.get("cwd"):
            raise ContractError("hook requires session_id and cwd")
        project, conversation = self.project(raw["cwd"]), str(raw["session_id"])
        self.bind_session(project, conversation)
        tool_id, turn = raw.get("tool_use_id"), raw.get("turn_id")
        operation = digest([conversation, tool_id]) if tool_id else None
        outcome = self.outcome(raw.get("tool_response")) if kind == "PostToolUse" else "unknown"
        # 没有宿主标识时不按内容哈希去重，两个真实的相同动作仍是两个事件。
        event_key = ([conversation, kind, tool_id, digest(raw.get("tool_response"))] if tool_id else
                     [conversation, kind, turn] if turn and kind in {"UserPromptSubmit", "Stop"} else None)
        payload = {k: raw[k] for k in ("tool_name", "tool_input", "tool_response", "prompt", "source",
                                     "last_assistant_message", "agent_type", "turn_id") if k in raw}
        payload["agent_identity_known"] = bool(raw.get("agent_id"))
        with self.transaction():
            # 状态判断与记录共享快照，避免并发晚到事件降级终止状态。
            task = self.task(project, conversation)
            denied = False
            if kind == "PreToolUse" and self.config["enforce_task_policy"]:
                denied = not task or task["status"] in {"cancelled", "delivered", "interrupted"}
                allowed = self.config["allowed_tools"]
                if allowed and raw.get("tool_name") not in allowed:
                    denied = True
                if raw.get("tool_name", "").startswith("mcp__agent_learning__"):
                    denied = False
                if denied:
                    outcome = "denied"
            binding = self.db.execute("SELECT * FROM operations WHERE project=? AND id=?", (project, operation)).fetchone() if operation else None
            task_id = binding["task_id"] if binding else task["id"] if task else None
            task_revision = binding["task_revision"] if binding else task["revision"] if task else None
            event, fresh = self._event(project, conversation, kind, payload, event_id=event_key,
                                       task_id=task_id, task_revision=task_revision,
                                       agent_id=raw.get("agent_id"), operation_id=operation, outcome=outcome)
            if operation and kind in {"PreToolUse", "PostToolUse"}:
                state = "denied" if denied else "started" if kind == "PreToolUse" else outcome
                old_state = binding["state"] if binding else None
                if old_state in {"succeeded", "failed"} and state in {"started", "running", "unknown"}:
                    state = old_state
                self.db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project,id) "
                                "DO UPDATE SET state=excluded.state,last_event=excluded.last_event",
                                (project, operation, conversation, task_id, task_revision,
                                 raw.get("agent_id"), state, event["id"]))
            if fresh and outcome == "failed" and not raw.get("tool_name", "").startswith("mcp__agent_learning__"):
                summary = f"{raw.get('tool_name', 'unknown tool')} failed: " + encode(clean(raw.get("tool_response")))[:300]
                self._problem(project, conversation, summary, "operation", [event["id"]],
                              task_binding={"id": task_id, "revision": task_revision})
            elif fresh and kind == "PostToolUse" and outcome == "succeeded" and not raw.get("tool_name", "").startswith("mcp__agent_learning__"):
                # 仅保留同工具的首次潜在后续成功；它不证明错误根因或修复成功。
                # 插件自查和unknown结果不会推进问题版本，避免学习自身触发循环。
                pending = self.db.execute(
                    "SELECT p.id FROM problems p WHERE p.project=? AND p.conversation=? "
                    "AND p.status='pending' AND p.task_id IS ? AND p.task_revision IS ? "
                    "AND EXISTS(SELECT 1 FROM problem_events pe JOIN events e ON e.id=pe.event "
                    "WHERE pe.problem=p.id AND e.outcome='failed' AND json_extract(e.payload,'$.tool_name')=?) "
                    "AND NOT EXISTS(SELECT 1 FROM problem_events pe JOIN events e ON e.id=pe.event "
                    "WHERE pe.problem=p.id AND e.type='PostToolUse' AND e.outcome='succeeded') "
                    "ORDER BY p.created DESC LIMIT 1", (project, conversation, task_id, task_revision, raw.get("tool_name"))).fetchall()
                for problem in pending:
                    self.db.execute("INSERT OR IGNORE INTO problem_events VALUES(?,?)", (problem[0], event["id"]))
                    self.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (problem[0],))
            if kind == "Interrupt":
                self.db.execute("UPDATE operations SET state='unknown' WHERE project=? AND conversation=? AND state IN ('running','started')",
                                (project, conversation))
            if (kind == "Stop" and fresh and self.config["capture_deliveries"] and task
                    and task["status"] == "delivered" and raw.get("last_assistant_message")):
                delivered = self.db.execute("SELECT 1 FROM deliveries WHERE project=? AND task_id=? AND revision=?",
                                            (project, task["id"], task["revision"])).fetchone()
                if not delivered:
                    refs = [event["id"]] + [r[0] for r in self.db.execute(
                        "SELECT id FROM events WHERE project=? AND conversation=? AND task_id=? AND task_revision=? "
                        "AND type='PostToolUse' AND outcome IN ('succeeded','failed') ORDER BY created DESC LIMIT 3",
                        (project, conversation, task["id"], task["revision"]))]
                    problem = self._problem(project, conversation, "已交付任务结果（待审核）：" + clip(task["goal"], 200),
                                            "mixed", refs)
                    self.db.execute("INSERT INTO deliveries VALUES(?,?,?,?)",
                                    (project, task["id"], task["revision"], problem["id"]))
        if denied:
            return {"hookSpecificOutput": {"hookEventName": kind, "permissionDecision": "deny",
                    "permissionDecisionReason": "Agent Learning: 当前任务状态或已配置工具范围不允许此调用。"}}
        if kind == "PreToolUse":
            with self.transaction():
                invalid = self._invalidated(project, conversation)
            if invalid:
                return {"hookSpecificOutput": {"hookEventName": kind, "additionalContext":
                        ("此前注入知识已撤销或被替换，请核对当前依据：" + ",".join(invalid))[:self.config["injection_max_chars"]]}}
        if kind in {"UserPromptSubmit", "SessionStart"}:
            query = raw.get("prompt", "")
            text = self._context(project, conversation, query, compact=kind == "SessionStart" and raw.get("source") in {"compact", "clear"})
            if kind == "SessionStart" and not text:
                text = f"Agent Learning 本地记录已启用；conversation_id={conversation}。任务范围明确后用 task_update 保存，知识按需检索，来源资料不提供额外授权。"
            if text:
                return {"hookSpecificOutput": {"hookEventName": kind, "additionalContext": text[:self.config["injection_max_chars"]]}}
        if kind == "Stop" and fresh and self.config["admin"]["enabled"]:
            # Hook只触发独立短批次，模型分析不占用同步Hook，也不依赖Hook生命周期。
            self._launch_admin(raw["cwd"])
        return {}

    def _launch_admin(self, workspace):
        import subprocess
        import sys
        options = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS}
        env = dict(os.environ, ALP_HOME=str(self.home), ALP_DISABLED="1")
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
        with (self.home / "background.log").open("ab") as log:
            subprocess.Popen([sys.executable, "-m", "agent_learning.cli", "--home", str(self.home), "--workspace",
                              str(workspace), "admin-once"], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, env=env, **options)

    def sync_once(self, project, retry=None):
        """仅重放知识同步；不重新执行任何用户工具命令。"""
        from .hindsight import HindsightClient
        if not self.config["hindsight"]["enabled"]:
            return {"status": "disabled"}
        lease = str(uuid.uuid4())
        with self.transaction():
            # 进程崩溃后远端可能已经写入，过期租约只进入待核对，不盲目重投。
            self.db.execute("UPDATE outbox SET state='blocked',error='expired lease; remote outcome unknown' "
                            "WHERE project=? AND state='running' AND lease_until<?", (project, time.time()))
            if retry:
                changed = self.db.execute("UPDATE outbox SET state='pending',error=NULL,next_try=0 WHERE project=? AND id=? "
                                          "AND state='blocked' AND attempts<3", (project, retry)).rowcount
                if not changed:
                    raise ContractError("no retryable knowledge operation; verify remote state before requesting retry")
            row = self.db.execute("SELECT * FROM outbox WHERE project=? AND attempts<3 AND next_try<=? "
                                  "AND state='pending' AND (? IS NULL OR id=?) ORDER BY rowid LIMIT 1",
                                  (project, time.time(), retry, retry)).fetchone()
            if not row:
                return {"status": "idle"}
            payload = clean(json.loads(row["payload"]))
            if self._target_identity(payload["target"]) != self._target_identity(clean(self.config["hindsight"])):
                self.db.execute("UPDATE outbox SET state='blocked',error='target configuration changed' WHERE id=?", (row["id"],))
                return {"status": "blocked", "reason": "target configuration changed"}
            active = self.db.execute("SELECT revision FROM active WHERE project=? AND id=?", (project, payload["knowledge_id"])).fetchone()
            if not active or active[0] != payload["revision"]:
                self.db.execute("UPDATE outbox SET state='superseded' WHERE id=?", (row["id"],))
                return {"status": "superseded"}
            self.db.execute("UPDATE outbox SET state='running',attempts=attempts+1,lease=?,lease_until=? WHERE id=?",
                            (lease, time.time() + max(30, int(self.config["hindsight"]["timeout_seconds"]) * 3), row["id"]))
        try:
            result = HindsightClient(clean(self.config["hindsight"])).retain(
                f"{project}:{payload['knowledge_id']}@{payload['revision']}", encode(payload["body"]),
                project, payload["revision"], row["id"])
            state, error = "synced", None
        except Exception as exc:
            from .hindsight import HindsightError
            diagnostic = {"error_type": type(exc).__name__, "write_outcome": "unknown"}
            if isinstance(exc, HindsightError):
                diagnostic.update(kind=exc.kind, status_code=exc.status_code, retryable=exc.retryable,
                                  write_outcome=exc.write_outcome)
            result = {"status": "failed", **diagnostic}
            state, error = "blocked", encode(diagnostic)
            # 不确定远端写入结果时不自动append；人工对账后再决定重试。
        changed = self.db.execute("UPDATE outbox SET state=?,error=?,lease=NULL,lease_until=0 WHERE id=? AND lease=?",
                                  (state, error, row["id"], lease)).rowcount
        if not changed:
            current = self.db.execute("SELECT state FROM outbox WHERE id=?", (row["id"],)).fetchone()
            return {"status": "lease_lost", "operation_id": row["id"], "current_state": current[0] if current else "missing"}
        return {"status": state, "operation_id": row["id"], "result": result}
