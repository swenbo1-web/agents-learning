"""上下文展示台账与来源变化投影；迁移保留旧会话的真实展示记录。"""
from __future__ import annotations

import json
import time


def _dirty_sql(affected):
    """只提升受影响正文修订的版本，不把其他项目或未关联事件算成变化。"""
    return f"""
        INSERT INTO knowledge_source_versions(project,knowledge_id,revision,version)
        SELECT project,knowledge_id,revision,1 FROM ({affected}) WHERE 1
        ON CONFLICT(project,knowledge_id,revision) DO UPDATE SET version=version+1;
        UPDATE projects SET source_epoch=source_epoch+1
        WHERE id IN (SELECT project FROM ({affected}));
    """


def _evidence_affected(project, event_predicate):
    # 无法还原来源关系的历史知识仍由正文 evidence_refs 提供证据，不能漏通知。
    return f"""
        SELECT DISTINCT s.project,s.knowledge_id,s.revision FROM events e
        JOIN problem_events pe ON pe.event=e.id
        JOIN knowledge_sources s ON s.project=e.project AND s.problem_id=pe.problem
        WHERE e.project={project} AND {event_predicate}
          AND EXISTS(SELECT 1 FROM json_each(s.evidence_refs) ref WHERE ref.value=e.id)
        UNION
        SELECT k.project,k.id AS knowledge_id,k.revision FROM knowledge k
        WHERE k.project={project}
          AND NOT EXISTS(SELECT 1 FROM knowledge_sources s WHERE s.project=k.project
                         AND s.knowledge_id=k.id AND s.revision=k.revision)
          AND EXISTS(SELECT 1 FROM json_each(k.body,'$.evidence_refs') ref
                     JOIN events e ON e.id=ref.value AND e.project=k.project
                     WHERE {event_predicate})
    """


def _event_identity_affected(prefix):
    # DELETE 触发时 events 已没有 OLD 行，直接比较证据引用。
    return f"""
        SELECT DISTINCT s.project,s.knowledge_id,s.revision FROM problem_events pe
        JOIN knowledge_sources s ON s.problem_id=pe.problem AND s.project={prefix}.project
        WHERE pe.event={prefix}.id
          AND EXISTS(SELECT 1 FROM json_each(s.evidence_refs) ref WHERE ref.value={prefix}.id)
        UNION
        SELECT k.project,k.id AS knowledge_id,k.revision FROM knowledge k
        WHERE k.project={prefix}.project
          AND NOT EXISTS(SELECT 1 FROM knowledge_sources s WHERE s.project=k.project
                         AND s.knowledge_id=k.id AND s.revision=k.revision)
          AND EXISTS(SELECT 1 FROM json_each(k.body,'$.evidence_refs') ref WHERE ref.value={prefix}.id)
    """


def _problem_link_affected(prefix):
    return f"""
        SELECT DISTINCT s.project,s.knowledge_id,s.revision FROM knowledge_sources s
        WHERE s.problem_id={prefix}.problem
          AND EXISTS(SELECT 1 FROM json_each(s.evidence_refs) ref WHERE ref.value={prefix}.event)
        UNION
        SELECT k.project,k.id AS knowledge_id,k.revision FROM knowledge k
        WHERE json_extract(k.body,'$.problem_id')={prefix}.problem
          AND NOT EXISTS(SELECT 1 FROM knowledge_sources s WHERE s.project=k.project
                         AND s.knowledge_id=k.id AND s.revision=k.revision)
          AND EXISTS(SELECT 1 FROM json_each(k.body,'$.evidence_refs') ref WHERE ref.value={prefix}.event)
    """


class ContextStorageMixin:
    def _migrate_context_storage(self):
        """schema 6 与索引版本分别检查，重开数据库不重复迁移展示记录。"""
        from .knowledge_index import INDEX_VERSION
        from .runtime import encode

        with self.transaction():
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 6:
                additions = {
                    "projects": {"source_epoch": "INTEGER NOT NULL DEFAULT 0"},
                    "contexts": {"retrieval_signature": "TEXT NOT NULL DEFAULT ''",
                                 "retrieval_refs": "TEXT NOT NULL DEFAULT '[]'"},
                    "subagent_contexts": {"retrieval_signature": "TEXT NOT NULL DEFAULT ''",
                                          "retrieval_refs": "TEXT NOT NULL DEFAULT '[]'"},
                }
                for table, columns in additions.items():
                    existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                    for name, declaration in columns.items():
                        if name not in existing:
                            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                self.db.execute("""CREATE TABLE IF NOT EXISTS context_exposures(
                    project TEXT NOT NULL,conversation TEXT NOT NULL,agent TEXT NOT NULL DEFAULT '',
                    knowledge_id TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL DEFAULT '{}',
                    source_version INTEGER NOT NULL DEFAULT -1,updated REAL NOT NULL,
                    PRIMARY KEY(project,conversation,agent,knowledge_id,revision))""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS knowledge_source_versions(
                    project TEXT NOT NULL,knowledge_id TEXT NOT NULL,revision INTEGER NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(project,knowledge_id,revision))""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS context_faults(
                    project TEXT NOT NULL,conversation TEXT NOT NULL,agent TEXT NOT NULL DEFAULT '',
                    signature TEXT NOT NULL,task_id TEXT,task_revision INTEGER,task_write_version INTEGER,
                    host_epoch INTEGER NOT NULL,query TEXT NOT NULL,tool TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',created REAL NOT NULL,
                    results TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(project,conversation,agent,signature))""")
                self.db.execute("CREATE INDEX IF NOT EXISTS context_exposures_source "
                                "ON context_exposures(project,knowledge_id,revision)")
                self.db.execute("CREATE INDEX IF NOT EXISTS context_faults_pending "
                                "ON context_faults(project,conversation,agent,state,created)")
                self.db.execute("CREATE INDEX IF NOT EXISTS knowledge_sources_problem "
                                "ON knowledge_sources(project,problem_id)")
                self.db.execute("CREATE INDEX IF NOT EXISTS events_operation "
                                "ON events(project,operation)")
                self.db.execute("CREATE INDEX IF NOT EXISTS problem_events_event "
                                "ON problem_events(event,problem)")
                self.db.execute("INSERT OR IGNORE INTO knowledge_source_versions "
                                "SELECT project,id,revision,0 FROM knowledge")
                now = time.time()
                for table in ("contexts", "subagent_contexts"):
                    for row in self.db.execute(f"SELECT * FROM {table}").fetchall():
                        # 旧 source_states 可为对象或有序键值对列表；都保留其旧基线。
                        states = dict(json.loads(row["source_states"]))
                        identities = dict.fromkeys(json.loads(row["shown"]) + json.loads(row["injected"]))
                        agent = row["agent"] if table == "subagent_contexts" else ""
                        for identity in identities:
                            if not isinstance(identity, str) or "@" not in identity:
                                continue
                            ident, revision_text = identity.rsplit("@", 1)
                            try:
                                revision = int(revision_text)
                            except ValueError:
                                continue
                            if not ident or revision < 1:
                                continue
                            self.db.execute("INSERT OR IGNORE INTO context_exposures VALUES(?,?,?,?,?,?,?,?)",
                                            (row["project"], row["conversation"], agent, ident, revision,
                                             encode(states.get(identity, {})), -1, now))
                self._install_context_source_triggers()
                self.db.execute("PRAGMA user_version=6")
            # 开发期/旧导出副本同版数据库可缺少有界失败预览列，补列不改已有队列。
            fault_columns = {row[1] for row in self.db.execute("PRAGMA table_info(context_faults)")}
            if "results" not in fault_columns:
                self.db.execute("ALTER TABLE context_faults ADD COLUMN results TEXT NOT NULL DEFAULT '[]'")
            # 索引是独立派生版本；schema 已是 6 也不能沿用旧分词投影。
            exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                     "AND name='knowledge_index_state'").fetchone()
            state = self.db.execute("SELECT version FROM knowledge_index_state WHERE id=1").fetchone() if exists else None
            if not state or state[0] != INDEX_VERSION:
                self._rebuild_knowledge_index()
            # Optional derived delivery cache; existing audit rows never prove
            # coverage and are deliberately not imported into this cache.
            from .context_dedup import migrate
            migrate(self)

    def _install_context_source_triggers(self):
        """来源变化由 SQLite 同事务记版本，调用方无需扫描所有会话。"""
        triggers = {
            "knowledge_insert": (
                "AFTER INSERT ON knowledge", "",
                "INSERT OR IGNORE INTO knowledge_source_versions VALUES(NEW.project,NEW.id,NEW.revision,0);"),
            "problem_revision": (
                "AFTER UPDATE OF revision ON problems", "WHEN OLD.revision IS NOT NEW.revision",
                _dirty_sql("SELECT DISTINCT project,knowledge_id,revision FROM knowledge_sources "
                           "WHERE project=NEW.project AND problem_id=NEW.id")),
            "operation_health": (
                "AFTER UPDATE OF conflict,decision ON operations",
                "WHEN OLD.conflict IS NOT NEW.conflict OR OLD.decision IS NOT NEW.decision",
                _dirty_sql(_evidence_affected("NEW.project", "e.operation=NEW.id"))),
            "event_outcome": (
                "AFTER UPDATE OF outcome ON events", "WHEN OLD.outcome IS NOT NEW.outcome",
                _dirty_sql(_event_identity_affected("NEW"))),
            "event_delete": (
                "AFTER DELETE ON events", "", _dirty_sql(_event_identity_affected("OLD"))),
            "problem_event_insert": (
                "AFTER INSERT ON problem_events", "", _dirty_sql(_problem_link_affected("NEW"))),
            "problem_event_delete": (
                "AFTER DELETE ON problem_events", "", _dirty_sql(_problem_link_affected("OLD"))),
            "source_insert": (
                "AFTER INSERT ON knowledge_sources", "",
                _dirty_sql("SELECT NEW.project AS project,NEW.knowledge_id AS knowledge_id,NEW.revision AS revision")),
            "source_delete": (
                "AFTER DELETE ON knowledge_sources", "",
                _dirty_sql("SELECT OLD.project AS project,OLD.knowledge_id AS knowledge_id,OLD.revision AS revision")),
        }
        for name, (event, condition, statements) in triggers.items():
            self.db.execute(f"CREATE TRIGGER IF NOT EXISTS context_source_{name} {event} {condition} "
                            f"BEGIN {statements} END")
