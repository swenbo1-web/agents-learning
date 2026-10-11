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
from .assignments import AssignmentMixin
from .task_control import TaskControlMixin
from .execution import ExecutionMixin
from .knowledge_store import KnowledgeStoreMixin
from .knowledge_index import KnowledgeIndexMixin
from .knowledge_context import KnowledgeContextMixin
from .context_storage import ContextStorageMixin
from .case_store import CaseStoreMixin
from .event_capture import (CaseCaptureMixin, prepare_event, tool_outcome, operation_input_conflict,
                            business_evidence_digest, projected_evidence)
from .review_packet import ReviewPacketMixin
from .knowledge_releases import KnowledgeReleaseMixin
from .knowledge_sync import KnowledgeSyncMixin
from .publication_state import PublicationStateMixin
from .scheduler import SCHEDULER_DEFAULTS, SchedulerMixin, validate_scheduler_config
from .learning_evaluation import LearningEvaluationMixin

CATEGORIES = {"operation", "professional", "project", "mixed", "unknown"}
TASK_STATUSES = {"active", "delivered", "cancelled", "interrupted", "paused"}
VALIDATION_LEVELS = {"reported", "tool_success"}
KNOWLEDGE_KINDS = {"fact", "solution", "pattern"}
METADATA_KEYS = {"tool", "tool_version", "error_code", "environment", "verification"}
DEFAULTS = {
    "injection_max_chars": 2400, "event_max_chars": 6000,
    "enforce_task_policy": False, "capture_deliveries": True, "allowed_tools": [], "project_aliases": {},
    "admin": {"enabled": False, "command": ["codex"], "max_calls_per_day": 5,
              "max_input_chars": 16000, "timeout_seconds": 120, "auto_publish": False,
              "max_cases_per_batch": 20, "max_calls_per_batch": 2, "max_batch_seconds": 180,
              "max_concurrent_calls": 1, "max_reported_tokens_per_day": None,
              "token_reservation_per_call": 4000},
    "scheduler": SCHEDULER_DEFAULTS,
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
    return digest({**{k: body.get(k, {}) if k == "metadata" else body[k] for k in
                    ("title", "content", "category", "applicability", "limitations", "validation_level", "metadata")},
                   "knowledge_kind": body.get("knowledge_kind", "solution")})


def knowledge_filters(value):
    """检索字段独立于工具 metadata，分类与知识用途都可精确筛选。"""
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - METADATA_KEYS - {"category", "knowledge_kind"}:
        raise ContractError("unsupported knowledge filters")
    if any(not isinstance(v, str) or not v.strip() or len(v) > 500 for v in value.values()):
        raise ContractError("knowledge filters must contain nonempty string fields")
    result = {k: clean(v).strip() for k, v in value.items()}
    if result.get("category", "unknown") not in CATEGORIES or result.get("knowledge_kind", "solution") not in KNOWLEDGE_KINDS:
        raise ContractError("invalid knowledge category or kind filter")
    return result




SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0,
 revocation_epoch INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS project_paths(path TEXT PRIMARY KEY,project TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_projects(conversation TEXT PRIMARY KEY,project TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS host_state(id INTEGER PRIMARY KEY CHECK(id=1),epoch INTEGER NOT NULL);
INSERT OR IGNORE INTO host_state VALUES(1,0);
CREATE TABLE IF NOT EXISTS host_notifications(id TEXT PRIMARY KEY,epoch INTEGER NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS context_access(handle TEXT PRIMARY KEY,project TEXT NOT NULL,
 conversation TEXT NOT NULL,agent TEXT NOT NULL,host_epoch INTEGER NOT NULL,created REAL NOT NULL,
 UNIQUE(project,conversation,agent,host_epoch));
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, project TEXT NOT NULL, conversation TEXT NOT NULL,
 revision INTEGER NOT NULL, goal TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL,
 updated REAL NOT NULL, UNIQUE(project,conversation));
CREATE TABLE IF NOT EXISTS task_snapshots(project TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,
 conversation TEXT NOT NULL,goal TEXT NOT NULL,scope TEXT NOT NULL,status TEXT NOT NULL,updated REAL NOT NULL,
 PRIMARY KEY(project,id,revision));
CREATE TABLE IF NOT EXISTS contexts(project TEXT NOT NULL, conversation TEXT NOT NULL,
 epoch INTEGER NOT NULL DEFAULT 0, injected TEXT NOT NULL DEFAULT '[]', last_query TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(project,conversation));
CREATE TABLE IF NOT EXISTS subagent_contexts(project TEXT NOT NULL,conversation TEXT NOT NULL,agent TEXT NOT NULL,
 epoch INTEGER NOT NULL DEFAULT 0,injected TEXT NOT NULL DEFAULT '[]',last_query TEXT NOT NULL DEFAULT '',
 shown TEXT NOT NULL DEFAULT '[]',PRIMARY KEY(project,conversation,agent));
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
CREATE TABLE IF NOT EXISTS task_states(project TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,
 write_version INTEGER NOT NULL,conversation TEXT NOT NULL,status TEXT NOT NULL,execution_epoch INTEGER NOT NULL,
 updated REAL NOT NULL,PRIMARY KEY(project,id,write_version));
CREATE TABLE IF NOT EXISTS assignments(id TEXT PRIMARY KEY,project TEXT NOT NULL,conversation TEXT NOT NULL,
 agent TEXT NOT NULL,context_handle TEXT NOT NULL UNIQUE,task_id TEXT,task_revision INTEGER,task_write_version INTEGER,
 execution_epoch INTEGER,host_epoch INTEGER NOT NULL,scope TEXT NOT NULL,created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS assignments_agent ON assignments(project,conversation,agent,host_epoch,created);
CREATE TABLE IF NOT EXISTS execution_policies(project TEXT NOT NULL,conversation TEXT NOT NULL,task_id TEXT NOT NULL,
 task_revision INTEGER NOT NULL,execution_epoch INTEGER NOT NULL,host_epoch INTEGER NOT NULL,rules TEXT NOT NULL,
 created REAL NOT NULL,PRIMARY KEY(project,conversation));
"""


class Runtime(TaskControlMixin, ExecutionMixin, AssignmentMixin, KnowledgeContextMixin,
              ContextStorageMixin, CaseStoreMixin, CaseCaptureMixin, ReviewPacketMixin,
              KnowledgeStoreMixin, KnowledgeIndexMixin, KnowledgeReleaseMixin,
              KnowledgeSyncMixin, PublicationStateMixin, SchedulerMixin, LearningEvaluationMixin):
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
            if key in {"admin", "hindsight", "scheduler"} and isinstance(value, dict):
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
        validate_scheduler_config(self.config["scheduler"])
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
        if version not in set(range(12)):
            self.db.close()
            raise ContractError("unsupported runtime database version")
        try:
            self.db.executescript(SCHEMA)
            if version < 2:
                self._migrate()
            if version < 3:
                with self.transaction():
                    if self.db.execute("PRAGMA user_version").fetchone()[0] < 3:
                        for table in ("operations", "admin_calls"):
                            existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                            if "host_epoch" not in existing:
                                self.db.execute(f"ALTER TABLE {table} ADD COLUMN host_epoch INTEGER NOT NULL DEFAULT 0")
                        self.db.execute("PRAGMA user_version=3")
            if version < 4:
                self._migrate_execution()
            if version < 5:
                self._migrate_knowledge_store()
            self._migrate_context_storage()
            self._migrate_case_store()
            self._migrate_admin_storage()
            self._migrate_publication_storage()
            self._migrate_module8_storage()
            self._migrate_audit_storage()
        except BaseException:
            self.db.close()
            raise

    def _migrate_admin_storage(self):
        from .admin_lifecycle import install_lifecycle
        from .admin_decisions import install_decisions
        from .admin_governance import install_governance
        from .admin_queue import install_queue
        with self.transaction():
            install_lifecycle(self)
            install_decisions(self)
            install_governance(self)
            install_queue(self)
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 8:
                self.db.execute("PRAGMA user_version=8")

    def _migrate_publication_storage(self):
        from .knowledge_lifecycle import install_lifecycle
        from .knowledge_releases import install_releases
        from .knowledge_sync import install_sync
        from .publication_state import install_publication_triggers
        with self.transaction():
            install_lifecycle(self)
            install_releases(self)
            install_sync(self)
            install_publication_triggers(self)
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 9:
                self.db.execute("PRAGMA user_version=9")

    def _migrate_module8_storage(self):
        """调度、调用计量与采用回执共享迁移事务；重开不降低 schema。"""
        from .admin_usage import install_budget
        with self.transaction():
            install_budget(self)
            self._migrate_scheduler_storage()
            self._migrate_learning_evaluation()
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 10:
                self.db.execute("PRAGMA user_version=10")

    def _migrate_audit_storage(self):
        """全量业务摘要独立保存；旧投影不补造不可恢复的内容。"""
        with self.transaction():
            if self.db.execute("PRAGMA user_version").fetchone()[0] >= 11:
                return
            existing = {row[1] for row in self.db.execute("PRAGMA table_info(events)")}
            if "business_digest" not in existing:
                self.db.execute("ALTER TABLE events ADD COLUMN business_digest TEXT")
            for row in self.db.execute("SELECT id,payload FROM events WHERE business_digest IS NULL").fetchall():
                payload = json.loads(row["payload"])
                if not projected_evidence(payload):
                    self.db.execute("UPDATE events SET business_digest=? WHERE id=?",
                                    (business_evidence_digest(payload), row["id"]))
            self.db.execute("PRAGMA user_version=11")

    def _migrate_execution(self):
        """只增加控制字段；旧来源缺少事件时点版本时保持未知。"""
        with self.transaction():
            if self.db.execute("PRAGMA user_version").fetchone()[0] >= 4:
                return
            additions = {
                "tasks": {"write_version": "INTEGER NOT NULL DEFAULT 1", "execution_epoch": "INTEGER NOT NULL DEFAULT 1"},
                "events": {"task_write_version": "INTEGER", "execution_epoch": "INTEGER", "assignment_id": "TEXT", "task_status": "TEXT", "capture_digest": "TEXT"},
                "operations": {"task_write_version": "INTEGER", "execution_epoch": "INTEGER", "assignment_id": "TEXT",
                               "decision": "TEXT NOT NULL DEFAULT 'unobserved'", "observation": "TEXT NOT NULL DEFAULT 'unknown'",
                               "conflict": "INTEGER NOT NULL DEFAULT 0", "tool_name": "TEXT"},
            }
            for table, columns in additions.items():
                existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                for column, declaration in columns.items():
                    if column not in existing:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            self.db.execute("UPDATE operations SET decision=CASE WHEN state='denied' THEN 'denied' ELSE 'unobserved' END,"
                            "observation=CASE WHEN state='denied' THEN 'unknown' ELSE state END")
            for row in self.db.execute("SELECT * FROM tasks").fetchall():
                self._write_task_state(dict(row))
            # 老句柄不能在升级后被重新归属到此刻的新任务；父身份仍可恢复。
            self.db.execute("PRAGMA user_version=4")

    def _write_task_state(self, task):
        self.db.execute("INSERT OR IGNORE INTO task_states VALUES(?,?,?,?,?,?,?,?)",
                        (task["project"], task["id"], task["revision"], task["write_version"], task["conversation"],
                         task["status"], task["execution_epoch"], task["updated"]))

    def _execution_fence(self, project, conversation, reason, pause=False):
        task = self.task(project, conversation)
        if not task:
            return None
        status = "paused" if pause and task["status"] == "active" else task["status"]
        self.db.execute("UPDATE tasks SET write_version=write_version+1,execution_epoch=execution_epoch+1,"
                        "status=?,updated=? WHERE project=? AND conversation=?", (status, time.time(), project, conversation))
        updated = self.task(project, conversation)
        self._write_task_state(updated)
        self._event(project, conversation, "ExecutionFence", {"reason": reason, "status": status,
                    "write_version": updated["write_version"], "execution_epoch": updated["execution_epoch"]},
                    task_id=updated["id"], task_revision=updated["revision"])
        return updated

    def _migrate(self):
        """原地升级并保留原记录；并发启动时在锁内重新检查列。"""
        with self.transaction():
            if self.db.execute("PRAGMA user_version").fetchone()[0] >= 2:
                return
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
        aliases = {}
        for key, value in self.config["project_aliases"].items():
            normalized = os.path.normcase(str(Path(key).resolve()))
            if normalized in aliases and aliases[normalized] != value:
                raise ContractError("project alias collision after normalization")
            aliases[normalized] = value
        alias = next((v for k, v in sorted(aliases.items(), key=lambda pair: len(pair[0]), reverse=True)
                      if path == k or path.startswith(k.rstrip(os.sep) + os.sep)), None)
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

    def host_epoch(self):
        return self.db.execute("SELECT epoch FROM host_state WHERE id=1").fetchone()[0]

    def assert_host_epoch(self, expected_epoch):
        epoch = self.host_epoch()
        if expected_epoch is not None and expected_epoch != epoch:
            raise ContractError("reconnect_required: account context changed")
        return epoch

    def _register_context(self, project, conversation_id, agent_id=None):
        self.bind_session(project, conversation_id)
        if agent_id is not None and (not isinstance(agent_id, str) or not agent_id.strip()):
            raise ContractError("agent_id must be a nonempty string")
        agent, epoch = agent_id or "", self.host_epoch()
        row = self.db.execute("SELECT handle FROM context_access WHERE project=? AND conversation=? "
                              "AND agent=? AND host_epoch=?", (project, conversation_id, agent, epoch)).fetchone()
        handle = row[0] if row else uuid.uuid4().hex
        if not row:
            self.db.execute("INSERT INTO context_access VALUES(?,?,?,?,?,?)",
                            (handle, project, conversation_id, agent, epoch, time.time()))
        if agent_id and not row:
            self._bind_assignment(project, conversation_id, agent_id, handle)
        return {"context_handle": handle, "project_id": project, "conversation_id": conversation_id,
                "agent_id": agent_id, "host_epoch": epoch}

    def register_context(self, project, conversation_id, agent_id=None):
        """仅 Hook/受信本地入口登记；MCP 不提供任意会话凭据查询或登记。"""
        with self.transaction():
            return self._register_context(project, conversation_id, agent_id)

    def access_context(self, project, context_handle, conversation_id=None, expected_epoch=None):
        epoch = self.assert_host_epoch(expected_epoch)
        if not isinstance(context_handle, str) or not context_handle:
            raise ContractError("session_context_required: use the handle supplied by the current hook")
        row = self.db.execute("SELECT * FROM context_access WHERE handle=?", (context_handle,)).fetchone()
        if not row:
            raise ContractError("context_mismatch: unknown context handle")
        if row["host_epoch"] != epoch:
            raise ContractError("reconnect_required: context handle expired")
        if row["project"] != project or (conversation_id is not None and conversation_id != row["conversation"]):
            raise ContractError("context_mismatch: project or conversation does not match")
        return {"context_handle": context_handle, "project_id": project, "conversation_id": row["conversation"],
                "agent_id": row["agent"] or None, "host_epoch": epoch}

    def session_context(self, project, context_handle=None, expected_epoch=None):
        # 压缩恢复只读已验证的身份；不能从“最新会话”猜本次归属。
        with self.transaction():
            epoch = self.assert_host_epoch(expected_epoch)
            if context_handle is None:
                return {"project_id": project, "host_epoch": epoch, "context_bound": False, "rebind_required": True}
            result = self.access_context(project, context_handle, expected_epoch=epoch)
            parent = self.task(project, result["conversation_id"])
            assignment = self.assignment_context(project, result["conversation_id"], result["agent_id"], context_handle) if result["agent_id"] else None
            task = self.task_snapshot(project, assignment["task_id"], assignment["task_revision"],
                                      assignment["task_write_version"]) if assignment and assignment["task_id"] else None
            return dict(result, context_bound=True, rebind_required=False, task=task if result["agent_id"] else parent,
                        assignment=assignment, parent_task=parent if result["agent_id"] else None,
                        execution=self.execution_status(project, result["conversation_id"]),
                        task_access="parent_read_only" if result["agent_id"] else "read_write")

    def account_changed(self, notification_id=None):
        """宿主/操作者通知换号；不读取凭证，不按登录账户拆分项目知识。"""
        if notification_id is not None and (not isinstance(notification_id, str)
                or not notification_id.strip() or len(notification_id) > 200):
            raise ContractError("notification_id must be a nonempty string of at most 200 characters")
        with self.transaction():
            if notification_id and self.db.execute("SELECT 1 FROM host_notifications WHERE id=?", (notification_id,)).fetchone():
                return {"host_epoch": self.host_epoch(), "changed": False, "knowledge_scope": "shared_project"}
            self.db.execute("UPDATE host_state SET epoch=epoch+1 WHERE id=1")
            epoch = self.host_epoch()
            if notification_id:
                self.db.execute("INSERT INTO host_notifications VALUES(?,?,?)", (notification_id, epoch, time.time()))
            self.db.execute("UPDATE operations SET state='unknown' WHERE state IN ('started','running')")
            for task in self.db.execute("SELECT project,conversation FROM tasks").fetchall():
                self._execution_fence(task["project"], task["conversation"], "account_changed")
            # 未知调用仍占原额度；禁止旧执行器回写或自动重试。
            self.db.execute("UPDATE admin_calls SET state='account_changed_unknown',finished=?,lease_until=NULL "
                            "WHERE state IN ('reserved','launch_intent','started')", (time.time(),))
            for table in ("contexts", "subagent_contexts"):
                self.db.execute(f"UPDATE {table} SET epoch=epoch+1,injected='[]',last_query='',retrieval_signature='',retrieval_refs='[]'")
            return {"host_epoch": epoch, "changed": True, "knowledge_scope": "shared_project",
                    "reconnect_required": True}

    def task(self, project, conversation):
        row = self.db.execute("SELECT * FROM tasks WHERE project=? AND conversation=?",
                              (project, conversation)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["scope"] = json.loads(item["scope"])
        return item

    def _event(self, project, conversation, kind, payload, event_id=None, task_id=None,
               task_revision=None, agent_id=None, operation_id=None, outcome="unknown",
               task_write_version=None, execution_epoch=None, assignment_id=None, task_status=None, capture_digest=None):
        text, payload_digest = prepare_event(payload, self.config["event_max_chars"])
        capture_digest = capture_digest or payload_digest
        input_digest = digest(clean(payload["tool_input"])) if "tool_input" in payload else None
        ident = digest([project, event_id]) if event_id else str(uuid.uuid4())
        old = self.db.execute("SELECT * FROM events WHERE id=?", (ident,)).fetchone()
        if old:
            if (old["payload"] != text or old["type"] != kind or old["conversation"] != conversation
                    or old["capture_digest"] and old["capture_digest"] != capture_digest):
                raise ContractError("event id reused with different content")
            return dict(old), False
        if task_id:
            current = self.task(project, conversation)
            if task_write_version is None and current and (current["id"], current["revision"]) == (task_id, task_revision):
                task_write_version, execution_epoch = current["write_version"], current["execution_epoch"]
                task_status = current["status"] if task_status is None else task_status
            if task_status is None and task_write_version is not None:
                state = self.db.execute("SELECT status FROM task_states WHERE project=? AND id=? AND write_version=?",
                                        (project, task_id, task_write_version)).fetchone()
                task_status = state[0] if state else None
        self.db.execute("INSERT INTO events(id,project,conversation,task_id,task_revision,agent,operation,type,outcome,payload,created,"
                        "task_write_version,execution_epoch,assignment_id,task_status,capture_digest,tool_input_digest,business_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ident, project, conversation, task_id, task_revision, agent_id,
                         operation_id, kind, outcome, text, time.time(), task_write_version, execution_epoch, assignment_id,
                         task_status, capture_digest, input_digest, business_evidence_digest(payload)))
        return dict(self.db.execute("SELECT * FROM events WHERE id=?", (ident,)).fetchone()), True

    def record_event(self, project, conversation, kind, payload, **kwargs):
        with self.transaction():
            return self._event(project, conversation, kind, payload, **kwargs)[0]





    def submit_candidate(self, project, expected_epoch=None, **body):
        required = {"problem_id", "problem_revision", "title", "content", "category", "applicability",
                    "limitations", "evidence_refs", "validation_level"}
        if not required <= set(body) or set(body) - required - {"metadata", "knowledge_kind"}:
            raise ContractError("candidate fields do not match schema")
        if (not isinstance(body["category"], str) or body["category"] not in CATEGORIES
                or not isinstance(body["validation_level"], str) or body["validation_level"] not in VALIDATION_LEVELS):
            raise ContractError("invalid candidate category or evidence level")
        if (not isinstance(body["problem_revision"], int) or isinstance(body["problem_revision"], bool)
                or body["problem_revision"] < 1):
            raise ContractError("problem_revision must be a positive integer")
        if any(not isinstance(body[key], str) or not body[key].strip() for key in
               ("title", "content", "applicability", "limitations")):
            raise ContractError("candidate text fields must be nonempty strings")
        if (not isinstance(body["problem_id"], str) or not body["problem_id"].strip()
                or not isinstance(body["evidence_refs"], list) or not body["evidence_refs"]
                or not all(isinstance(ref, str) and ref.strip() for ref in body["evidence_refs"])):
            raise ContractError("at least one source event is required")
        body = clean(body)
        body["metadata"] = metadata(body.get("metadata"))
        body["evidence_refs"] = sorted(set(body["evidence_refs"]))
        body["knowledge_kind"] = body.get("knowledge_kind", "solution")
        if not isinstance(body["knowledge_kind"], str) or body["knowledge_kind"] not in KNOWLEDGE_KINDS:
            raise ContractError("invalid knowledge kind")
        if len(encode(body)) > 30000:
            raise ContractError("candidate is too large")
        identity = digest([project, body])
        ident = identity[:32]
        with self._case_transaction():
            self.assert_host_epoch(expected_epoch)
            previous = self.db.execute("SELECT c.id,c.status,c.body FROM candidate_identity i JOIN candidates c "
                                       "ON i.project=c.project AND i.candidate_id=c.id WHERE i.project=? AND i.fingerprint=?",
                                       (project, identity)).fetchone()
            if previous:
                if previous["body"] != encode(body):
                    raise ContractError("candidate identity does not match its body; inspect storage")
                # 已完成候选按原身份重放；旧来源变化不制造新候选绕过发布回执。
                if previous["status"] == "published":
                    return {"candidate_id": previous["id"], "status": previous["status"]}
            self._validate_sources(project, body)
            if previous:
                return {"candidate_id": previous["id"], "status": previous["status"]}
            self.db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?)",
                            (ident, project, body["problem_id"], body["problem_revision"], encode(body), "candidate", time.time()))
            self.db.execute("INSERT INTO candidate_identity VALUES(?,?,?)", (project, identity, ident))
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
            operation = self.db.execute("SELECT conflict,decision FROM operations WHERE project=? AND id=?",
                                        (project, event["operation"])).fetchone() if event["operation"] else None
            successful |= (event["type"] == "PostToolUse" and event["outcome"] == "succeeded"
                           and not (operation and (operation["conflict"] or operation["decision"] == "denied")))
        if body["validation_level"] == "tool_success" and not successful:
            raise ContractError("no observed completed tool success")




    def _metric(self, project, name, value=1):
        self.db.execute("INSERT INTO metrics VALUES(?,?,?) ON CONFLICT(project,name) DO UPDATE SET value=value+excluded.value",
                        (project, name, value))

    def status(self, project):
        from .admin_usage import admin_budget_status
        counts = {}
        for table in ("events", "tasks", "problems", "candidates", "active", "outbox", "knowledge_sources", "publication_receipts",
                      "review_receipts", "governance_proposals", "knowledge_actions", "knowledge_releases", "knowledge_sync_dirty"):
            counts[table] = self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE project=?", (project,)).fetchone()[0]
        counts["pending_problems"] = self.db.execute("SELECT COUNT(*) FROM problems WHERE project=? AND status='pending'", (project,)).fetchone()[0]
        observed = {r[0]: r[1] for r in self.db.execute("SELECT type,COUNT(*) FROM events WHERE project=? GROUP BY type", (project,))}
        outbox_states = {r[0]: r[1] for r in self.db.execute("SELECT state,COUNT(*) FROM outbox WHERE project=? GROUP BY state", (project,))}
        try:
            budget = admin_budget_status(self)
        except ContractError:
            budget = {"admission_status": "invalid_configuration"}
        return {"project_id": project, "home": str(self.home), "host_epoch": self.host_epoch(),
                "knowledge_scope": "shared_project", "account_detection": "host_notification_required", "counts": counts,
                "storage_schema_version": self.db.execute("PRAGMA user_version").fetchone()[0],
                "index_version": self.db.execute("SELECT version FROM knowledge_index_state WHERE id=1").fetchone()[0],
                "observed_events": observed, "outbox_states": outbox_states,
                "admin_enabled": bool(self.config["admin"]["enabled"]),
                "admin_budget": budget, "scheduler": self.scheduler_status(project),
                "learning_effects": self.learning_effects(project),
                "hindsight_enabled": bool(self.config["hindsight"]["enabled"]),
                "knowledge_release_head": self._release_head(project),
                "metrics": {r[0]: r[1] for r in self.db.execute("SELECT name,value FROM metrics WHERE project=?", (project,))},
                "admin_states": {r[0]: r[1] for r in self.db.execute("SELECT state,COUNT(*) FROM admin_calls WHERE project=? GROUP BY state", (project,))},
                "review_states": {r[0]: r[1] for r in self.db.execute("SELECT state,COUNT(*) FROM review_states WHERE project=? GROUP BY state", (project,))},
                "governance_states": {r[0]: r[1] for r in self.db.execute("SELECT state,COUNT(*) FROM governance_proposals WHERE project=? GROUP BY state", (project,))},
                "coverage": "configured local hooks only; unidentified agents remain unknown"}

    @staticmethod
    def outcome(response):
        return tool_outcome(response)

    def handle_hook(self, raw):
        kind = raw.get("hook_event_name")
        supported = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "Interrupt", "SubagentStart", "SubagentStop"}
        if kind not in supported:
            return {"systemMessage": "Agent Learning: 未覆盖的事件，未解释其状态。"}
        if not raw.get("session_id") or not raw.get("cwd"):
            raise ContractError("hook requires session_id and cwd")
        project, conversation = self.project(raw["cwd"]), str(raw["session_id"])
        tool_id, turn = raw.get("tool_use_id"), raw.get("turn_id")
        agent_id = raw.get("agent_id")
        if agent_id is not None and (not isinstance(agent_id, str) or not agent_id.strip()):
            raise ContractError("invalid hook agent_id")
        operation = digest([conversation, agent_id, tool_id] if agent_id else [conversation, tool_id]) if tool_id else None
        outcome = self.outcome(raw.get("tool_response")) if kind == "PostToolUse" else "unknown"
        # 没有宿主标识时不按内容哈希去重，两个真实的相同动作仍是两个事件。
        event_key = ([conversation, agent_id, kind, tool_id, digest(raw.get("tool_response"))] if tool_id else
                     [conversation, agent_id, kind, turn, digest(raw.get("last_assistant_message") if kind == "Stop" else raw.get("prompt"))]
                     if turn and kind in {"UserPromptSubmit", "Stop"} else None)
        payload = {k: raw[k] for k in ("tool_name", "tool_input", "tool_response", "prompt", "source",
                                     "last_assistant_message", "agent_type", "turn_id") if k in raw}
        payload["agent_identity_known"] = bool(raw.get("agent_id"))
        capture_digest = digest(clean(payload))
        legacy_event_key = event_key
        if kind == "PostToolUse" and tool_id:
            # 工具响应相同而输入/工具名不同仍是独立证据，由 operation 统一判冲突。
            event_key = [conversation, agent_id, kind, tool_id, capture_digest]
        access = None
        with self.transaction():
            self.bind_session(project, conversation)
            epoch = self.host_epoch()
            if kind == "PostToolUse" and event_key:
                replay = self.db.execute("SELECT capture_digest FROM events WHERE id=?", (digest([project, [epoch, event_key]]),)).fetchone()
                if replay and replay[0] is not None:
                    if replay[0] != capture_digest:
                        raise ContractError("event id reused with different content")
                    return {}
                legacy_replay = self.db.execute("SELECT capture_digest,payload FROM events WHERE id=?",
                    (digest([project, [epoch, legacy_event_key]]),)).fetchone()
                if legacy_replay and legacy_replay[0] == capture_digest:
                    return {}
                if legacy_replay and legacy_replay[0] is None:
                    legacy_payload = json.loads(legacy_replay["payload"])
                    if not projected_evidence(legacy_payload):
                        observable = {key: value for key, value in legacy_payload.items() if key in (
                            "tool_name", "tool_input", "tool_response", "prompt", "source",
                            "last_assistant_message", "agent_type", "turn_id")}
                        observable["agent_identity_known"] = legacy_payload.get("agent_identity_known", bool(agent_id))
                        if digest(clean(observable)) == capture_digest:
                            return {}
            if kind in {"SessionStart", "UserPromptSubmit"} or kind == "SubagentStart" and agent_id:
                access = self._register_context(project, conversation, agent_id)
            payload["host_epoch"] = epoch
            # 状态判断与记录共享快照，避免并发晚到事件降级终止状态。
            task = self.task(project, conversation)
            binding = self.db.execute("SELECT * FROM operations WHERE project=? AND id=?", (project, operation)).fetchone() if operation else None
            obsolete_operation = binding and binding["host_epoch"] != epoch
            assignment = self.assignment_context(project, conversation, agent_id) if agent_id else None
            assigned = self.hook_binding(project, conversation, agent_id) if agent_id else None
            # 只有 Pre 能绑定父任务；缺少 Pre 的 Post 不能按“此刻任务”猜历史归属。
            source = ({"id": binding["task_id"], "revision": binding["task_revision"],
                       "write_version": binding["task_write_version"], "execution_epoch": binding["execution_epoch"],
                       "assignment_id": binding["assignment_id"]} if binding else assigned if agent_id else
                      task if kind != "PostToolUse" else None)
            task_id, task_revision = (source["id"], source["revision"]) if source else (None, None)
            task_write_version = source.get("write_version") if source else None
            execution_epoch = source.get("execution_epoch") if source else None
            assignment_id = source.get("assignment_id") if source else None
            denied, deny_reason = False, ""
            decision = binding["decision"] if binding else "unobserved"
            if kind == "PreToolUse":
                recovery_tools = {"mcp__agent_learning__" + name for name in (
                    "knowledge_search", "knowledge_read", "session_context", "task_update", "case_feedback", "knowledge_use", "learning_status")}
                if self.config["enforce_task_policy"] and raw.get("tool_name") not in recovery_tools:
                    verdict = self.check_execution(project, conversation, task, raw, assignment)
                    denied, deny_reason = verdict["decision"] == "denied", verdict["reason"]
                    if agent_id and not assignment:
                        denied, deny_reason = True, "assignment_required"
                if binding and binding["decision"] == "denied":
                    denied, deny_reason = True, "operation_previously_denied"
                if obsolete_operation:
                    denied, deny_reason = True, "account_context_changed"
                if binding and self.config["enforce_task_policy"] and raw.get("tool_name") not in recovery_tools:
                    prior_pre = self.db.execute("SELECT capture_digest FROM events WHERE project=? AND operation=? AND type='PreToolUse' "
                                                "ORDER BY created,id LIMIT 1", (project, operation)).fetchone()
                    same_round = task and (binding["task_id"], binding["task_revision"], binding["execution_epoch"]) == (
                        task["id"], task["revision"], task["execution_epoch"])
                    if not same_round:
                        denied, deny_reason = True, "operation_execution_epoch_stale"
                    elif binding["state"] not in {"started", "running"}:
                        denied, deny_reason = True, "operation_already_terminal_or_unknown"
                    elif not prior_pre or prior_pre[0] != capture_digest:
                        denied, deny_reason = True, "operation_payload_mismatch"
                decision = "denied" if denied else "allowed" if self.config["enforce_task_policy"] else "observed"
                payload["control_decision"] = {"decision": decision, "reason": deny_reason or "observed"}
                if denied:
                    outcome = "denied"
            observed_outcome = outcome
            conflict = bool(binding and binding["conflict"])
            if binding and kind == "PostToolUse" and not obsolete_operation:
                terminals = {"succeeded", "failed", "call_completed"}
                conflict |= binding["decision"] == "denied" and outcome != "unknown"
                conflict |= binding["observation"] in terminals and outcome in terminals and binding["observation"] != outcome
                conflict |= bool(binding["tool_name"] and raw.get("tool_name") != binding["tool_name"])
                conflict |= operation_input_conflict(self, project, operation, raw)
                if conflict:
                    payload["observed_outcome"] = outcome
                    payload["operation_conflict"] = True
                    outcome = "conflict"
            if obsolete_operation:
                outcome = "unknown"
                payload["account_context_changed"] = True
            identity = [epoch, event_key, decision, task["execution_epoch"] if task else None] if event_key and kind == "PreToolUse" else [epoch, event_key] if event_key else None
            event, fresh = self._event(project, conversation, kind, payload, event_id=identity,
                                       task_id=task_id, task_revision=task_revision,
                                       agent_id=agent_id, operation_id=operation, outcome=outcome,
                                       task_write_version=task_write_version, execution_epoch=execution_epoch, assignment_id=assignment_id,
                                       capture_digest=capture_digest)
            # 重播 Pre 的新拒绝作为独立事件保存，不改写原许可和已观察结果。
            if operation and (kind == "PostToolUse" or kind == "PreToolUse" and not binding):
                state = "denied" if denied else "started" if kind == "PreToolUse" else outcome
                old_state = binding["state"] if binding else None
                observation = binding["observation"] if binding else "unknown"
                if kind == "PostToolUse":
                    observation = "unknown" if obsolete_operation else "conflict" if conflict else observed_outcome
                    if binding and binding["observation"] in {"succeeded", "failed", "call_completed"} and observation in {"started", "running", "unknown"}:
                        observation = binding["observation"]
                if obsolete_operation:
                    state = "unknown"
                elif conflict:
                    state = "conflict"
                elif old_state in {"succeeded", "failed", "call_completed"} and state in {"started", "running", "unknown"}:
                    state = old_state
                self.db.execute("INSERT INTO operations(project,id,conversation,task_id,task_revision,agent,state,last_event,host_epoch,"
                                "task_write_version,execution_epoch,assignment_id,decision,observation,conflict,tool_name) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project,id) "
                                "DO UPDATE SET state=excluded.state,last_event=excluded.last_event,decision=excluded.decision,"
                                "observation=excluded.observation,conflict=excluded.conflict",
                                (project, operation, conversation, task_id, task_revision,
                                 agent_id, state, event["id"], epoch, task_write_version, execution_epoch, assignment_id,
                                 decision, observation, int(conflict), raw.get("tool_name")))
            if fresh and kind == "PostToolUse":
                self._capture_tool_case(project, conversation, agent_id, event, outcome, raw,
                                        {"id": task_id, "revision": task_revision}, source, epoch, operation)
            if kind == "Interrupt":
                self.db.execute("UPDATE operations SET state='unknown' WHERE project=? AND conversation=? "
                                "AND (? IS NULL OR agent=?) AND state IN ('running','started')", (project, conversation, agent_id, agent_id))
                if fresh and not agent_id:
                    self._execution_fence(project, conversation, "interrupted", pause=True)
            if fresh and kind == "UserPromptSubmit" and not agent_id and str(raw.get("prompt", "")).strip().lower().strip("。.!！ ") not in {
                    "继续", "继续吧", "接着", "continue", "go on"}:
                self._execution_fence(project, conversation, "new_user_prompt")
            if kind == "SessionStart" and raw.get("source") == "clear" and not agent_id:
                self._execution_fence(project, conversation, "context_cleared", pause=True)
            if kind == "Stop" and not agent_id and fresh:
                self._capture_delivery(project, conversation, task, event, raw)
        schedule_warning = None
        if fresh and kind in {"Stop", "SessionStart"} and self.config["scheduler"]["enabled"]:
            token = None
            try:
                if self.scheduler_wake(project):
                    token = self.claim_scheduler_worker()
                    if token:
                        self._launch_scheduler(raw["cwd"], token)
            except (OSError, ValueError, TypeError, sqlite3.Error):
                if token:
                    try:
                        self.release_scheduler_worker(token)
                    except sqlite3.Error:
                        pass
                try:
                    self._metric(project, "scheduler_schedule_failed")
                except sqlite3.Error:
                    pass
                schedule_warning = "Agent Learning: 事件已保存，后台调度未确认启动；待办保留。"
        if denied:
            return {"hookSpecificOutput": {"hookEventName": kind, "permissionDecision": "deny",
                    "permissionDecisionReason": "Agent Learning: 当前任务状态或执行范围不允许此调用：" + deny_reason}}
        if kind == "PreToolUse":
            text = self._context(project, conversation, agent_id=agent_id, expected_epoch=epoch, tool_only=True)
            if text:
                return {"hookSpecificOutput": {"hookEventName": kind, "additionalContext":
                        text}}
        if kind in {"UserPromptSubmit", "SessionStart", "SubagentStart"}:
            if kind == "SubagentStart" and not agent_id:
                return {"hookSpecificOutput": {"hookEventName": kind, "additionalContext":
                        "Agent Learning: 子 Agent 身份未知，未分配写入句柄；继承当前任务边界，可按项目检索知识，不猜测会话归属。"}}
            query = raw.get("prompt", "")
            text = self._context(project, conversation, query, compact=kind == "SessionStart" and raw.get("source") in {"compact", "clear"},
                                 agent_id=agent_id, context_handle=access["context_handle"], expected_epoch=access["host_epoch"])
            if text:
                result = {"hookSpecificOutput": {"hookEventName": kind, "additionalContext": text[:self.config["injection_max_chars"]]}}
                if schedule_warning:
                    result["systemMessage"] = schedule_warning
                return result
        if kind == "Stop" and fresh and self.config["admin"]["enabled"] and not self.config["scheduler"]["enabled"]:
            # Hook只触发独立短批次，模型分析不占用同步Hook，也不依赖Hook生命周期。
            token = None
            try:
                from .admin import _settings
                from .admin_queue import claim_dispatch, release_dispatch
                _settings(self.config["admin"])
                token = claim_dispatch(self, project)
                if token:
                    self._launch_admin(raw["cwd"], token)
            except (OSError, ValueError, TypeError, sqlite3.Error):
                if token:
                    try:
                        release_dispatch(self, project, token)
                    except sqlite3.Error:
                        pass
                try:
                    self._metric(project, "admin_schedule_failed")
                except sqlite3.Error:
                    pass
                return {"systemMessage": "Agent Learning: event_recorded; admin_schedule_failed，事件已保存，后台整理未确认启动。"}
        return {"systemMessage": schedule_warning} if schedule_warning else {}

    def _launch_admin(self, workspace, dispatch_token=None):
        self._launch_background(workspace, "admin-batch",
                                ["--dispatch-token", dispatch_token] if dispatch_token else [])

    def _launch_scheduler(self, workspace, worker_token):
        self._launch_background(workspace, "scheduler-run", ["--all", "--worker-token", worker_token])

    def _launch_background(self, workspace, command, arguments):
        import subprocess
        import sys
        options = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS}
        env = dict(os.environ, ALP_HOME=str(self.home), ALP_DISABLED="1")
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
        log_path = self.home / "background.log"
        try:
            if log_path.exists() and log_path.stat().st_size > 1024 * 1024:
                log_path.replace(self.home / "background.previous.log")
        except OSError:
            # Windows 活动写句柄可能禁止重命名，轮换失败不能阻断已授权派发。
            try:
                self._metric(self.project(workspace), "background_log_rotation_deferred")
            except (OSError, sqlite3.Error):
                pass
        with log_path.open("ab") as log:
            subprocess.Popen([sys.executable, "-m", "agent_learning.cli", "--home", str(self.home), "--workspace",
                              str(workspace), command, *arguments], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, env=env, **options)
