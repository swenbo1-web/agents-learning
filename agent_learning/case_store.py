"""案例证据与反馈回执；治理状态、潜在恢复和知识反例分别保存。"""
from __future__ import annotations

import contextlib
import json
import time


CASE_ROLES = {"observation", "attempt", "failure", "conflict", "potential_recovery", "delivery", "feedback"}
RESOLUTION_STATUSES = {"open", "recovery_observed"}


class CaseStoreMixin:
    def _case_transaction(self):
        # Hook 和反馈已持有外层事务；复用它，避免嵌套 BEGIN。
        return contextlib.nullcontext() if self.db.in_transaction else self.transaction()

    def _migrate_case_store(self):
        """schema 7 不改历史身份，也不猜旧 knowledge_id 指向哪一个修订。"""
        with self._case_transaction():
            previous_version = self.db.execute("PRAGMA user_version").fetchone()[0]
            for table, additions in {
                "problems": {"resolution_status": "TEXT NOT NULL DEFAULT 'open'", "knowledge_revision": "INTEGER"},
                "problem_events": {"role": "TEXT NOT NULL DEFAULT 'observation'"},
                "events": {"tool_input_digest": "TEXT"},
            }.items():
                existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                for name, declaration in additions.items():
                    if name not in existing:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            self.db.execute("""CREATE TABLE IF NOT EXISTS feedback_receipts(
                project TEXT NOT NULL,conversation TEXT NOT NULL,agent TEXT NOT NULL,
                host_epoch INTEGER NOT NULL,idempotency_key TEXT NOT NULL,request_digest TEXT NOT NULL,
                response TEXT NOT NULL,created REAL NOT NULL,
                PRIMARY KEY(project,conversation,agent,host_epoch,idempotency_key))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS delivery_rounds(
                project TEXT NOT NULL,task_id TEXT NOT NULL,revision INTEGER NOT NULL,
                write_version INTEGER NOT NULL,problem_id TEXT NOT NULL,event_id TEXT NOT NULL,created REAL NOT NULL,
                PRIMARY KEY(project,task_id,revision,write_version))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS knowledge_feedback(
                project TEXT NOT NULL,knowledge_id TEXT NOT NULL,revision INTEGER NOT NULL,
                problem_id TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind='counterexample'),
                created REAL NOT NULL,PRIMARY KEY(project,knowledge_id,revision,problem_id),
                FOREIGN KEY(project,knowledge_id,revision) REFERENCES knowledge(project,id,revision),
                FOREIGN KEY(project,problem_id) REFERENCES problems(project,id))""")
            self.db.execute("CREATE INDEX IF NOT EXISTS knowledge_feedback_problem ON knowledge_feedback(project,problem_id)")
            if previous_version < 7:
                # 只有事件与历史状态都明确指向同一次父任务交付才恢复轮次，旧缺证记录不猜。
                self.db.execute("INSERT OR IGNORE INTO delivery_rounds(project,task_id,revision,write_version,problem_id,event_id,created) "
                                "SELECT d.project,d.task_id,d.revision,e.task_write_version,d.problem_id,e.id,e.created "
                                "FROM deliveries d JOIN problem_events pe ON pe.problem=d.problem_id JOIN events e ON e.id=pe.event "
                                "JOIN task_states ts ON ts.project=d.project AND ts.id=d.task_id AND ts.revision=d.revision "
                                "AND ts.write_version=e.task_write_version WHERE e.project=d.project AND e.task_id=d.task_id "
                                "AND e.task_revision=d.revision AND e.type='Stop' AND e.agent IS NULL "
                                "AND e.task_status='delivered' AND ts.status='delivered' ORDER BY e.created,e.id")
            self._install_case_feedback_triggers()
            if previous_version < 7:
                self.db.execute("PRAGMA user_version=7")

    def _install_case_feedback_triggers(self):
        from .context_storage import _dirty_sql

        affected = ("SELECT DISTINCT project,knowledge_id,revision FROM knowledge_feedback "
                    "WHERE project=NEW.project AND problem_id=NEW.id")
        triggers = {
            "insert": ("AFTER INSERT ON knowledge_feedback", "", _dirty_sql(
                "SELECT NEW.project AS project,NEW.knowledge_id AS knowledge_id,NEW.revision AS revision")),
            "delete": ("AFTER DELETE ON knowledge_feedback", "", _dirty_sql(
                "SELECT OLD.project AS project,OLD.knowledge_id AS knowledge_id,OLD.revision AS revision")),
            "update": ("AFTER UPDATE ON knowledge_feedback",
                       "WHEN OLD.project IS NOT NEW.project OR OLD.knowledge_id IS NOT NEW.knowledge_id "
                       "OR OLD.revision IS NOT NEW.revision OR OLD.problem_id IS NOT NEW.problem_id OR OLD.kind IS NOT NEW.kind",
                       _dirty_sql("SELECT OLD.project AS project,OLD.knowledge_id AS knowledge_id,OLD.revision AS revision "
                                  "UNION SELECT NEW.project,NEW.knowledge_id,NEW.revision")),
            "problem": ("AFTER UPDATE OF status,revision,resolution_status ON problems",
                        "WHEN OLD.status IS NOT NEW.status OR OLD.revision IS NOT NEW.revision "
                        "OR OLD.resolution_status IS NOT NEW.resolution_status", _dirty_sql(affected)),
        }
        for name, (event, condition, statements) in triggers.items():
            self.db.execute(f"CREATE TRIGGER IF NOT EXISTS case_feedback_{name} {event} {condition} BEGIN {statements} END")

    def _attach_case_evidence(self, problem_id, evidence, resolution_status=None, expected_revision=None):
        """一批真实证据变化只提升一次素材版本；潜在成功不等于已经解决。"""
        from .runtime import ContractError

        if resolution_status is not None and (not isinstance(resolution_status, str) or resolution_status not in RESOLUTION_STATUSES):
            raise ContractError("invalid case resolution status")
        with self._case_transaction():
            problem = self.db.execute("SELECT * FROM problems WHERE id=?", (problem_id,)).fetchone()
            if not problem:
                raise ContractError("unknown problem")
            if expected_revision is not None and problem["revision"] != expected_revision:
                raise ContractError("problem changed; review new evidence")
            normalized = {}
            for event_id, role in evidence:
                if not isinstance(role, str) or role not in CASE_ROLES:
                    raise ContractError("invalid case evidence role")
                event = self.db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
                if not event or (event["project"], event["conversation"], event["task_id"], event["task_revision"]) != (
                        problem["project"], problem["conversation"], problem["task_id"], problem["task_revision"]):
                    raise ContractError("case evidence must belong to the same project, conversation and task revision")
                normalized[event_id] = role
            changed = False
            changed_roles = []
            for event_id, role in normalized.items():
                previous = self.db.execute("SELECT role FROM problem_events WHERE problem=? AND event=?",
                                           (problem_id, event_id)).fetchone()
                if previous and previous[0] == role:
                    continue
                self.db.execute("INSERT INTO problem_events(problem,event,role) VALUES(?,?,?) "
                                "ON CONFLICT(problem,event) DO UPDATE SET role=excluded.role", (problem_id, event_id, role))
                changed = True
                changed_roles.append(role)
            next_resolution = resolution_status or problem["resolution_status"]
            if resolution_status is None:
                if "potential_recovery" in changed_roles:
                    next_resolution = "recovery_observed"
                elif any(role in {"failure", "conflict", "feedback"} for role in changed_roles):
                    next_resolution = "open"
            if changed:
                self.db.execute("UPDATE problems SET revision=revision+1,status='pending',resolution_status=? WHERE id=?",
                                (next_resolution, problem_id))
                # 新事实使原反例处置需要复核；重放没有新增证据时不清除回执。
                self.db.execute("UPDATE knowledge_feedback SET disposition='unresolved',disposition_reason=NULL,"
                                "disposition_receipt=NULL WHERE project=? AND problem_id=? AND disposition!='unresolved'",
                                (problem["project"], problem_id))
            elif next_resolution != problem["resolution_status"]:
                self.db.execute("UPDATE problems SET resolution_status=? WHERE id=?", (next_resolution, problem_id))
            return dict(self.db.execute("SELECT * FROM problems WHERE id=?", (problem_id,)).fetchone())

    def _problem(self, project, conversation, summary, category, event_ids, knowledge_id=None, task_binding=None,
                 evidence_roles=None, knowledge_revision=None):
        from .runtime import clean, clip, digest

        summary = clip(clean(summary), 2000)
        task = task_binding if task_binding is not None else self.task(project, conversation)
        task_id, task_revision = (task["id"], task["revision"]) if task else (None, None)
        identity = [project, conversation, task_id, task_revision, category, summary, knowledge_id]
        if knowledge_revision is not None:
            identity.append(knowledge_revision)
        ident = digest(identity)[:32]
        roles = evidence_roles or {}
        with self._case_transaction():
            self.db.execute("INSERT OR IGNORE INTO problems(id,project,conversation,task_id,task_revision,revision,summary,"
                            "category,status,knowledge_id,created,resolution_status,knowledge_revision) "
                            "VALUES(?,?,?,?,?,0,?,?,'pending',?,?,'open',?)",
                            (ident, project, conversation, task_id, task_revision, summary, category,
                             knowledge_id, time.time(), knowledge_revision))
            evidence = []
            for event in event_ids:
                previous = self.db.execute("SELECT role FROM problem_events WHERE problem=? AND event=?", (ident, event)).fetchone()
                evidence.append((event, roles.get(event, previous[0] if previous else "observation")))
            return self._attach_case_evidence(ident, evidence)

    def feedback(self, project, conversation_id, summary, category="unknown", event_refs=None,
                 resolution="", knowledge_id=None, context_handle=None, expected_epoch=None,
                 expected_task_id=None, expected_task_revision=None, idempotency_key=None,
                 knowledge_revision=None, feedback_kind=None):
        from .runtime import CATEGORIES, ContractError, digest, encode

        if not isinstance(category, str) or category not in CATEGORIES or not isinstance(summary, str) or not summary.strip():
            raise ContractError("valid category and summary are required")
        if not isinstance(resolution, str):
            raise ContractError("resolution must be a string")
        if expected_task_id is not None and (not isinstance(expected_task_id, str) or not expected_task_id.strip()):
            raise ContractError("expected_task_id must be a nonempty string")
        if expected_task_revision is not None and (isinstance(expected_task_revision, bool)
                or not isinstance(expected_task_revision, int) or expected_task_revision < 1):
            raise ContractError("expected_task_revision must be a positive integer")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key.strip()
                                            or len(idempotency_key) > 200):
            raise ContractError("idempotency_key must be a nonempty string of at most 200 characters")
        if knowledge_id is not None and (not isinstance(knowledge_id, str) or not knowledge_id.strip()):
            raise ContractError("knowledge_id must be a nonempty string")
        if knowledge_revision is not None and (knowledge_id is None or isinstance(knowledge_revision, bool)
                or not isinstance(knowledge_revision, int) or knowledge_revision < 1):
            raise ContractError("knowledge_revision requires a knowledge_id and positive integer revision")
        if feedback_kind is not None and (not isinstance(feedback_kind, str) or feedback_kind not in {"observation", "counterexample"}):
            raise ContractError("invalid feedback kind")
        kind = feedback_kind or ("counterexample" if knowledge_id is not None else "observation")
        if kind == "counterexample" and knowledge_id is None:
            raise ContractError("counterexample requires knowledge_id")
        if event_refs is not None and (not isinstance(event_refs, list)
                or not all(isinstance(ref, str) and ref.strip() for ref in event_refs)):
            raise ContractError("event_refs must contain nonempty event ids")
        refs = list(event_refs or [])
        request_digest = digest({"summary": summary, "category": category, "event_refs": refs, "resolution": resolution,
                                 "knowledge_id": knowledge_id, "knowledge_revision": knowledge_revision,
                                 "feedback_kind": feedback_kind, "expected_task_id": expected_task_id,
                                 "expected_task_revision": expected_task_revision})
        with self._case_transaction():
            self.assert_session_epoch(expected_epoch)
            access = self.access_context(project, context_handle, conversation_id, expected_epoch) if context_handle is not None else None
            self.bind_session(project, conversation_id)
            agent = access["agent_id"] if access else None
            receipt_key = (project, conversation_id, agent or "", self.session_epoch(), idempotency_key)
            if idempotency_key is not None:
                receipt = self.db.execute("SELECT request_digest,response FROM feedback_receipts WHERE project=? AND conversation=? "
                                          "AND agent=? AND host_epoch=? AND idempotency_key=?", receipt_key).fetchone()
                if receipt:
                    if receipt["request_digest"] != request_digest:
                        raise ContractError("idempotency key reused with different feedback")
                    return json.loads(receipt["response"])
            task = self.task(project, conversation_id)
            assignment = None
            if agent:
                assignment = self.assignment_context(project, conversation_id, agent, context_handle)
                if not assignment or not assignment["task_id"]:
                    raise ContractError("assignment_required: child has no verified task assignment")
                task = self.task_snapshot(project, assignment["task_id"], assignment["task_revision"], assignment["task_write_version"])
            if access and not agent and task and (expected_task_id is None or expected_task_revision is None):
                raise ContractError("feedback_task_required: supply expected_task_id and expected_task_revision")
            if expected_task_id is not None and (not task or expected_task_id != task["id"]):
                raise ContractError("stale feedback task identity")
            if expected_task_revision is not None and (not task or expected_task_revision != task["revision"]):
                raise ContractError("stale feedback task revision")
            actual_revision = knowledge_revision
            if knowledge_id is not None:
                if actual_revision is None:
                    row = self.db.execute("SELECT revision FROM active WHERE project=? AND id=?", (project, knowledge_id)).fetchone()
                    actual_revision = row[0] if row else None
                if actual_revision is None or not self.db.execute("SELECT 1 FROM knowledge WHERE project=? AND id=? AND revision=?",
                                                                  (project, knowledge_id, actual_revision)).fetchone():
                    raise ContractError("knowledge reference must identify an existing project knowledge revision")
            for ref in refs:
                if not self.db.execute("SELECT 1 FROM events WHERE id=? AND project=? AND conversation=? "
                                       "AND task_id IS ? AND task_revision IS ?", (ref, project, conversation_id,
                                       task["id"] if task else None, task["revision"] if task else None)).fetchone():
                    raise ContractError("evidence must belong to the current project, conversation and task revision")
                if agent and not self.db.execute("SELECT 1 FROM events WHERE id=? AND agent=?", (ref, agent)).fetchone():
                    raise ContractError("evidence must belong to the current subagent")
            event, _ = self._event(project, conversation_id, "Feedback",
                                   {"summary": summary, "resolution": resolution, "knowledge_id": knowledge_id,
                                    "knowledge_revision": actual_revision, "feedback_kind": kind},
                                   task_id=task["id"] if task else None, task_revision=task["revision"] if task else None,
                                   agent_id=agent, outcome="reported", task_write_version=task.get("write_version") if task else None,
                                   execution_epoch=task.get("execution_epoch") if task else None,
                                   assignment_id=assignment["id"] if assignment else None)
            result = self._problem(project, conversation_id, summary, category, refs + [event["id"]], knowledge_id,
                                   task_binding=task, evidence_roles={event["id"]: "feedback"}, knowledge_revision=actual_revision)
            if kind == "counterexample":
                self.db.execute("INSERT OR IGNORE INTO knowledge_feedback(project,knowledge_id,revision,problem_id,kind,created) VALUES(?,?,?,?,?,?)",
                                (project, knowledge_id, actual_revision, result["id"], kind, time.time()))
            if idempotency_key is not None:
                self.db.execute("INSERT INTO feedback_receipts VALUES(?,?,?,?,?,?,?,?)",
                                (*receipt_key, request_digest, encode(result), time.time()))
            return result

    def problems(self, project, limit=20):
        return [dict(row) for row in self.db.execute("SELECT * FROM problems WHERE project=? AND status='pending' "
                                                   "ORDER BY created,id LIMIT ?", (project, max(1, min(limit, 100))))]

    def _knowledge_feedback_state(self, project, knowledge_id, revision):
        count = self.db.execute("SELECT COUNT(*) FROM knowledge_feedback f JOIN problems p "
                                "ON p.project=f.project AND p.id=f.problem_id WHERE f.project=? AND f.knowledge_id=? "
                                "AND f.revision=? AND f.kind='counterexample' AND f.disposition='unresolved'",
                                (project, knowledge_id, revision)).fetchone()[0]
        return {"pending_counterexample_count": count, "feedback_status": "pending_counterexample" if count else "none"}
