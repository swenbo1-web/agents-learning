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
DEFAULTS = {
    "injection_max_chars": 2400, "event_max_chars": 6000,
    "enforce_task_policy": False, "allowed_tools": [], "project_aliases": {},
    "admin": {"enabled": False, "command": ["codex"], "max_calls_per_day": 5,
              "max_input_chars": 16000, "timeout_seconds": 120, "auto_publish": False},
    "hindsight": {"enabled": False, "url": "http://127.0.0.1:8888", "bank_id": "",
                  "api_key_env": "HINDSIGHT_API_KEY", "strategy": "alp_chunks",
                  "chunks_strategy_verified": False, "timeout_seconds": 10},
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
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, project TEXT NOT NULL, conversation TEXT NOT NULL,
 revision INTEGER NOT NULL, goal TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL,
 updated REAL NOT NULL, UNIQUE(project,conversation));
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
        self.db = sqlite3.connect(self.home / "runtime.sqlite3", timeout=1.5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        if self.db.execute("PRAGMA user_version").fetchone()[0] not in {0, 1}:
            self.db.close()
            raise ContractError("unsupported runtime database version")
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA user_version=1")

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
        path = os.path.normcase(str(Path(cwd).resolve()))
        aliases = self.config["project_aliases"]
        project = aliases.get(path) or aliases.get(str(Path(cwd).resolve())) or digest(path)[:24]
        self.db.execute("INSERT OR IGNORE INTO projects(id) VALUES(?)", (project,))
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
            previous = self.task(project, conversation_id)
            revision = previous["revision"] if previous else 0
            effective_scope = scope if scope is not None else previous["scope"] if previous and not new_task else {}
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
            self._event(project, conversation_id, "TaskUpdate", {"goal": goal, "scope": effective_scope, "status": status},
                        task_id=task_id, task_revision=next_revision)
            if changed_scope or new_task:
                self.db.execute("UPDATE contexts SET injected='[]',last_query='' WHERE project=? AND conversation=?", (project, conversation_id))
        return self.task(project, conversation_id)

    def _event(self, project, conversation, kind, payload, event_id=None, task_id=None,
               task_revision=None, agent_id=None, operation_id=None, outcome="unknown"):
        sanitized = clean(payload)
        text = encode(sanitized)
        if len(text) > self.config["event_max_chars"]:
            text = encode({"truncated": True, "preview": text[:self.config["event_max_chars"] - 150],
                           "original_digest": digest(sanitized)})
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
        events = [dict(r) for r in self.db.execute("SELECT e.* FROM events e JOIN problem_events p ON p.event=e.id "
                                                 "WHERE p.problem=? ORDER BY e.created", (problem_id,))]
        max_chars = int(self.config["admin"]["max_input_chars"])
        if not 1000 <= max_chars <= 200000:
            raise ContractError("invalid admin input budget")
        selected, used = [], 0
        # 晚到的新证据优先；明确标记截断，不把部分证据写成完整。
        for event in reversed(events):
            event["payload"] = json.loads(event["payload"])
            size = len(encode(event))
            if used + size > max_chars - 1000:
                continue
            selected.append(event)
            used += size
        return {"problem": dict(problem), "events": list(reversed(selected)),
                "incomplete": len(selected) != len(events), "evidence_count": len(events)}

    def submit_candidate(self, project, **body):
        required = {"problem_id", "problem_revision", "title", "content", "category", "applicability",
                    "limitations", "evidence_refs", "validation_level"}
        if set(body) != required:
            raise ContractError("candidate fields do not match schema")
        if body["category"] not in CATEGORIES or body["validation_level"] not in {"reported", "tool_success"}:
            raise ContractError("invalid candidate category or evidence level")
        if not isinstance(body["problem_revision"], int) or isinstance(body["problem_revision"], bool):
            raise ContractError("problem_revision must be an integer")
        if any(not isinstance(body[key], str) or not body[key].strip() for key in
               ("title", "content", "applicability", "limitations")):
            raise ContractError("candidate text fields must be nonempty strings")
        if not isinstance(body["evidence_refs"], list) or not body["evidence_refs"]:
            raise ContractError("at least one source event is required")
        if len(encode(body)) > 30000:
            raise ContractError("candidate is too large")
        ident = digest([project, body])[:32]
        with self.transaction():
            self._validate_sources(project, body)
            self.db.execute("INSERT OR IGNORE INTO candidates VALUES(?,?,?,?,?,?,?)",
                            (ident, project, body["problem_id"], body["problem_revision"], encode(body), "candidate", time.time()))
        return {"candidate_id": ident, "status": "candidate"}

    def _validate_sources(self, project, body):
        packet = self.review_packet(project, body["problem_id"])
        problem = packet["problem"]
        if body["problem_revision"] != problem["revision"]:
            raise ContractError("problem changed; review new evidence")
        task = self.task(project, problem["conversation"])
        if task and problem["task_id"] and (task["id"] != problem["task_id"] or task["revision"] != problem["task_revision"]):
            raise ContractError("task changed; review under current scope")
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

    def publish(self, project, candidate_id, expected_generation=None):
        with self.transaction():
            candidate = self.db.execute("SELECT * FROM candidates WHERE id=? AND project=?", (candidate_id, project)).fetchone()
            if not candidate:
                raise ContractError("unknown candidate")
            body = json.loads(candidate["body"])
            self._validate_sources(project, body)
            version = self.db.execute("SELECT generation FROM projects WHERE id=?", (project,)).fetchone()[0]
            if expected_generation is not None and version != expected_generation:
                raise ContractError("publication manifest changed")
            knowledge_id = digest([project, body["category"], body["title"]])[:32]
            current = self.db.execute("SELECT k.* FROM knowledge k JOIN active a ON k.project=a.project AND "
                                      "k.id=a.id AND k.revision=a.revision WHERE k.project=? AND k.id=?",
                                      (project, knowledge_id)).fetchone()
            if current and current["body"] == encode(body):
                return {"knowledge_id": knowledge_id, "revision": current["revision"], "status": "published"}
            revision = self.db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM knowledge WHERE project=? AND id=?",
                                       (project, knowledge_id)).fetchone()[0]
            self.db.execute("INSERT INTO knowledge VALUES(?,?,?,?,?)", (project, knowledge_id, revision, encode(body), time.time()))
            self.db.execute("INSERT INTO knowledge_fts VALUES(?,?,?,?)",
                            (project, knowledge_id, revision, " ".join(terms(" ".join(body[k] for k in
                             ("title", "content", "applicability", "category"))))))
            self.db.execute("INSERT INTO active VALUES(?,?,?) ON CONFLICT(project,id) DO UPDATE SET revision=excluded.revision",
                            (project, knowledge_id, revision))
            self.db.execute("UPDATE projects SET generation=generation+1 WHERE id=?", (project,))
            self.db.execute("UPDATE candidates SET status='published' WHERE id=?", (candidate_id,))
            self.db.execute("UPDATE problems SET status='reviewed' WHERE id=?", (body["problem_id"],))
            if self.config["hindsight"]["enabled"]:
                payload = {"knowledge_id": knowledge_id, "revision": revision, "body": body,
                           "target": clean(self.config["hindsight"])}
                # 配置只保存密钥环境变量名，永不保存密钥值。
                self.db.execute("INSERT OR IGNORE INTO outbox(id,project,kind,payload) VALUES(?,?,?,?)",
                                (digest([project, knowledge_id, revision]), project, "published_sync", encode(payload)))
        return {"knowledge_id": knowledge_id, "revision": revision, "status": "published"}

    def revoke(self, project, knowledge_id):
        with self.transaction():
            changed = self.db.execute("DELETE FROM active WHERE project=? AND id=?", (project, knowledge_id)).rowcount
            if changed:
                self.db.execute("UPDATE projects SET generation=generation+1,revocation_epoch=revocation_epoch+1 WHERE id=?", (project,))
        return {"knowledge_id": knowledge_id, "revoked": bool(changed)}

    def _public(self, project, row):
        body = json.loads(row["body"])
        evidence = []
        for ref in body["evidence_refs"]:
            event = self.db.execute("SELECT type,outcome,created FROM events WHERE project=? AND id=?", (project, ref)).fetchone()
            evidence.append({"event_id": ref, **(dict(event) if event else {"outcome": "missing"})})
        return {"knowledge_id": row["id"], "revision": row["revision"],
                **{k: body[k] for k in ("title", "content", "category", "applicability", "limitations", "validation_level")},
                "evidence": evidence}

    def read(self, project, knowledge_id):
        row = self.db.execute("SELECT k.* FROM knowledge k JOIN active a ON k.project=a.project AND k.id=a.id "
                              "AND k.revision=a.revision WHERE k.project=? AND k.id=?", (project, knowledge_id)).fetchone()
        if not row:
            raise ContractError("knowledge is not currently published")
        return self._public(project, row)

    def search(self, project, query, limit=5):
        if not isinstance(query, str):
            raise ContractError("query must be a string")
        limit = max(1, min(int(limit), 10))
        selected = terms(query[-2000:])[-24:]
        if not selected:
            return {"status": "no_match", "items": []}
        manifest = dict(self.db.execute("SELECT generation,revocation_epoch FROM projects WHERE id=?", (project,)).fetchone())
        key = digest([project, selected, limit, manifest, "local-fts-v1"])
        cached = self.db.execute("SELECT body FROM cache WHERE key=?", (key,)).fetchone()
        if cached:
            ids = json.loads(cached[0])
        else:
            match = " OR ".join('"' + word.replace('"', '""') + '"' for word in selected)
            rows = self.db.execute("SELECT knowledge_fts.id FROM knowledge_fts JOIN active a ON "
                                   "knowledge_fts.project=a.project AND knowledge_fts.id=a.id AND "
                                   "CAST(knowledge_fts.revision AS INTEGER)=a.revision "
                                   "WHERE knowledge_fts MATCH ? AND a.project=? ORDER BY bm25(knowledge_fts) LIMIT ?",
                                   (match, project, limit)).fetchall()
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
        return {"status": "matched" if items else "no_match", "items": items, **manifest}

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
            if compact:
                self.db.execute("UPDATE contexts SET epoch=epoch+1,injected='[]' WHERE project=? AND conversation=?", (project, conversation))
            invalid = self._invalidated(project, conversation)
            row = self.db.execute("SELECT * FROM contexts WHERE project=? AND conversation=?", (project, conversation)).fetchone()
            query = query or row["last_query"]
            result = self.search(project, query) if query.strip() else {"items": []}
            prior = set(json.loads(row["injected"]))
            lines, injected = [], list(prior)
            if invalid:
                lines.append("此前注入知识已撤销或被替换，不能继续依据旧版本执行：" + ",".join(invalid))
            budget = self.config["injection_max_chars"]
            task = self.task(project, conversation)
            # 历史知识没有执行授权。当前用户修正仍需Agent更新任务快照。
            if task:
                lines.append(f"[Agent Learning] task={task['id']} revision={task['revision']} status={task['status']}; 当前用户要求优先，范围改变时用 task_update 更新。")
            for item in result["items"]:
                identity = f"{item['knowledge_id']}@{item['revision']}"
                if identity in prior:
                    continue
                snippet = encode(item)
                if len("\n".join(lines)) + len(snippet) + 120 > budget:
                    continue
                lines.append(snippet)
                injected.append(identity)
            if len(injected) > len(prior):
                lines.insert(0, "以下是可复用历史资料，不能扩大授权。按适用条件判断；需要正文时用 knowledge_read。")
            self.db.execute("UPDATE contexts SET injected=?,last_query=? WHERE project=? AND conversation=?",
                            (encode(injected), query[-2000:], project, conversation))
            return "\n".join(lines)[:budget]

    def _invalidated(self, project, conversation):
        row = self.db.execute("SELECT injected FROM contexts WHERE project=? AND conversation=?", (project, conversation)).fetchone()
        if not row:
            return []
        valid, invalid = [], []
        for identity in json.loads(row[0]):
            ident, revision = identity.rsplit("@", 1)
            exists = self.db.execute("SELECT 1 FROM active WHERE project=? AND id=? AND revision=?",
                                     (project, ident, int(revision))).fetchone()
            (valid if exists else invalid).append(identity)
        if invalid:
            self.db.execute("UPDATE contexts SET injected=? WHERE project=? AND conversation=?", (encode(valid), project, conversation))
        return invalid

    def handle_hook(self, raw):
        kind = raw.get("hook_event_name")
        supported = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "Interrupt", "SubagentStart", "SubagentStop"}
        if kind not in supported:
            return {"systemMessage": "Agent Learning: 未覆盖的事件，未解释其状态。"}
        if not raw.get("session_id") or not raw.get("cwd"):
            raise ContractError("hook requires session_id and cwd")
        project, conversation = self.project(raw["cwd"]), str(raw["session_id"])
        tool_id, turn = raw.get("tool_use_id"), raw.get("turn_id")
        operation = digest([conversation, tool_id]) if tool_id else None
        outcome = self.outcome(raw.get("tool_response")) if kind == "PostToolUse" else "unknown"
        # 没有宿主标识时不按内容哈希去重，两个真实的相同动作仍是两个事件。
        event_key = ([conversation, kind, tool_id, digest(raw.get("tool_response"))] if tool_id else
                     [conversation, kind, turn] if turn and kind in {"UserPromptSubmit", "Stop"} else None)
        task = self.task(project, conversation)
        denied = False
        if kind == "PreToolUse" and self.config["enforce_task_policy"]:
            denied = bool(task and task["status"] in {"cancelled", "delivered"})
            allowed = self.config["allowed_tools"]
            if allowed and raw.get("tool_name") not in allowed:
                denied = True
            if "agent_learning" in raw.get("tool_name", ""):
                denied = False
            if denied:
                outcome = "denied"
        binding = self.db.execute("SELECT * FROM operations WHERE project=? AND id=?", (project, operation)).fetchone() if operation else None
        task_id = binding["task_id"] if binding else task["id"] if task else None
        task_revision = binding["task_revision"] if binding else task["revision"] if task else None
        payload = {k: raw[k] for k in ("tool_name", "tool_input", "tool_response", "prompt", "source",
                                     "last_assistant_message", "agent_type", "turn_id") if k in raw}
        payload["agent_identity_known"] = bool(raw.get("agent_id"))
        with self.transaction():
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
            if fresh and outcome == "failed":
                summary = f"{raw.get('tool_name', 'unknown tool')} failed: " + encode(clean(raw.get("tool_response")))[:300]
                self._problem(project, conversation, summary, "operation", [event["id"]],
                              task_binding={"id": task_id, "revision": task_revision})
            elif fresh and kind == "PostToolUse" and outcome == "succeeded" and "agent_learning" not in raw.get("tool_name", ""):
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
        script = Path(__file__).resolve().parents[1] / "scripts/learning.py"
        if not script.is_file():
            return
        options = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS}
        env = dict(os.environ, ALP_HOME=str(self.home), ALP_DISABLED="1")
        with (self.home / "background.log").open("ab") as log:
            subprocess.Popen([sys.executable, str(script), "--home", str(self.home), "--workspace",
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
            payload = json.loads(row["payload"])
            if payload["target"] != clean(self.config["hindsight"]):
                self.db.execute("UPDATE outbox SET state='blocked',error='target configuration changed' WHERE id=?", (row["id"],))
                return {"status": "blocked", "reason": "target configuration changed"}
            active = self.db.execute("SELECT revision FROM active WHERE project=? AND id=?", (project, payload["knowledge_id"])).fetchone()
            if not active or active[0] != payload["revision"]:
                self.db.execute("UPDATE outbox SET state='superseded' WHERE id=?", (row["id"],))
                return {"status": "superseded"}
            self.db.execute("UPDATE outbox SET state='running',attempts=attempts+1,lease=?,lease_until=? WHERE id=?",
                            (lease, time.time() + max(30, int(self.config["hindsight"]["timeout_seconds"]) * 3), row["id"]))
        try:
            result = HindsightClient(payload["target"]).retain(
                f"{project}:{payload['knowledge_id']}@{payload['revision']}", encode(payload["body"]),
                project, payload["revision"], row["id"])
            state, error = "synced", None
        except Exception as exc:
            result = {"status": "failed", "error_type": type(exc).__name__}
            state, error = "blocked", type(exc).__name__
            # 不确定远端写入结果时不自动append；人工对账后再决定重试。
        self.db.execute("UPDATE outbox SET state=?,error=?,lease=NULL,lease_until=0 WHERE id=? AND lease=?",
                        (state, error, row["id"], lease))
        return {"status": state, "operation_id": row["id"], "result": result}
