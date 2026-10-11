"""知识成熟度认证及不可变发布批次；不创建调度任务，也不改变历史正文。"""
from __future__ import annotations

import contextlib
import json
import time
import uuid


MATURITY_RULE_VERSION = 1
RELEASE_FORMAT = 1


def install_releases(runtime):
    """只增表；由 Runtime 统一推进 schema 版本，旧知识默认 candidate。"""
    with runtime._case_transaction():
        statements = [
            """CREATE TABLE IF NOT EXISTS knowledge_maturity(
                project TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('candidate','stable')),
                source_version INTEGER NOT NULL,certification TEXT NOT NULL,
                reason TEXT NOT NULL,created REAL NOT NULL,
                PRIMARY KEY(project,id,revision),
                FOREIGN KEY(project,id,revision) REFERENCES knowledge(project,id,revision))""",
            """CREATE TABLE IF NOT EXISTS knowledge_maturity_receipts(
                project TEXT NOT NULL,request_id TEXT NOT NULL,request_digest TEXT NOT NULL,
                result TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project,request_id))""",
            """CREATE TABLE IF NOT EXISTS knowledge_releases(
                project TEXT NOT NULL,id TEXT NOT NULL,manifest TEXT NOT NULL,
                content_hash TEXT NOT NULL,state TEXT NOT NULL
                CHECK(state IN ('prepared','committed','unchanged')),
                parent_release TEXT,reason TEXT NOT NULL,created REAL NOT NULL,
                result TEXT,completed REAL,PRIMARY KEY(project,id),
                FOREIGN KEY(project) REFERENCES projects(id))""",
            """CREATE TABLE IF NOT EXISTS knowledge_release_heads(
                project TEXT PRIMARY KEY,id TEXT NOT NULL,
                FOREIGN KEY(project,id) REFERENCES knowledge_releases(project,id))""",
            "CREATE INDEX IF NOT EXISTS knowledge_releases_history ON knowledge_releases(project,created,id)",
            """CREATE TRIGGER IF NOT EXISTS knowledge_release_immutable
                BEFORE UPDATE ON knowledge_releases
                WHEN OLD.state IN ('committed','unchanged')
                BEGIN SELECT RAISE(ABORT,'completed knowledge release is immutable'); END""",
            """CREATE TRIGGER IF NOT EXISTS knowledge_release_no_delete
                BEFORE DELETE ON knowledge_releases
                WHEN OLD.state IN ('committed','unchanged')
                BEGIN SELECT RAISE(ABORT,'completed knowledge release is immutable'); END""",
        ]
        for statement in statements:
            runtime.db.execute(statement)


def _positive(value, label, maximum=None):
    from .runtime import ContractError

    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or (maximum and value > maximum):
        raise ContractError(f"{label} must be a positive integer" + (f" at most {maximum}" if maximum else ""))
    return value


def _text(value, label, maximum=2000, allow_empty=False):
    from .runtime import ContractError, clean

    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        raise ContractError(f"{label} must be a {'possibly empty ' if allow_empty else 'nonempty '}string at most {maximum} characters")
    return clean(value).strip()


class KnowledgeReleaseMixin:
    def _release_read_transaction(self):
        return contextlib.nullcontext() if self.db.in_transaction else self._release_snapshot_transaction()

    @contextlib.contextmanager
    def _release_snapshot_transaction(self):
        self.db.execute("BEGIN")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _release_project(self, project):
        from .runtime import ContractError

        if not self.db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone():
            raise ContractError("unknown project")

    def _release_head(self, project):
        row = self.db.execute("SELECT id FROM knowledge_release_heads WHERE project=?", (project,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _maturity_certification(snapshot):
        from .runtime import digest

        # 生命周期版本包含晋升自身；认证不能因刚写入晋升回执而自我失效。
        return digest({"rule": MATURITY_RULE_VERSION, **{key: snapshot.get(key) for key in (
            "project_id", "knowledge_id", "revision", "body", "source_version", "active",
            "source_health", "current_tool_success", "problem_changed", "pending_counterexample_count",
            "revoked_counterexample_count", "source_digest", "feedback_digest")}})

    def _maturity_state_from_snapshot(self, project, knowledge_id, revision, snapshot):
        """纯读认证判定，供共享快照调用；不递归读取快照或执行资格检查。"""
        if not snapshot.get("active") or snapshot.get("source_health") != "valid" or snapshot.get("problem_changed"):
            return "candidate"
        if snapshot.get("pending_counterexample_count", 0) or snapshot.get("revoked_counterexample_count", 0):
            return "candidate"
        row = self.db.execute("SELECT * FROM knowledge_maturity WHERE project=? AND id=? AND revision=?",
                              (project, knowledge_id, revision)).fetchone()
        if row and row["state"] == "stable" and row["certification"] == self._maturity_certification(snapshot):
            return "stable"
        return "candidate"

    def _release_snapshot(self, project, knowledge_id, revision):
        """使用共享无时间戳快照；兼容仅安装本模块的离线迁移检查。"""
        from .runtime import ContractError, digest

        snapshot_method = getattr(self, "knowledge_state_snapshot", None)
        if snapshot_method is not None:
            snapshot = snapshot_method(project, knowledge_id, revision)
        else:
            row = self.db.execute("SELECT * FROM knowledge WHERE project=? AND id=? AND revision=?",
                                  (project, knowledge_id, revision)).fetchone()
            if not row:
                raise ContractError("unknown knowledge revision")
            public = self._public(project, row)
            revoked = self.db.execute("SELECT COUNT(*) FROM knowledge_feedback WHERE project=? AND knowledge_id=? "
                "AND revision=? AND kind='counterexample' AND disposition='revoked'", (project, knowledge_id, revision)).fetchone()[0]
            feedback = [dict(item) for item in self.db.execute("SELECT * FROM knowledge_feedback WHERE project=? "
                "AND knowledge_id=? AND revision=? ORDER BY problem_id", (project, knowledge_id, revision))]
            snapshot = {"project_id": project, "knowledge_id": knowledge_id, "revision": revision,
                "body": json.loads(row["body"]), "source_version": public["source_version"], "lifecycle_version": 0,
                "active": bool(self.db.execute("SELECT 1 FROM active WHERE project=? AND id=? AND revision=?",
                                              (project, knowledge_id, revision)).fetchone()),
                **{key: public[key] for key in ("source_health", "current_tool_success", "problem_changed",
                                                "pending_counterexample_count")},
                "revoked_counterexample_count": revoked, "source_count": len(public["sources"]),
                "sources": public["sources"][:20], "source_digest": digest(public["sources"]),
                "feedback_digest": digest(feedback)}
        snapshot = dict(snapshot)
        snapshot["maturity"] = self._maturity_state_from_snapshot(project, knowledge_id, revision, snapshot)
        return snapshot

    def knowledge_maturity(self, project, knowledge_id, expected_revision):
        from .runtime import ContractError, knowledge_fingerprint

        _positive(expected_revision, "expected_revision")
        with self._release_read_transaction():
            self._release_project(project)
            snapshot = self._release_snapshot(project, knowledge_id, expected_revision)
            body, reasons, independent = snapshot["body"], [], set()
            if not snapshot["active"]:
                reasons.append("knowledge_revision_is_not_active")
            if body.get("category") != "operation" or body.get("knowledge_kind", "solution") != "solution":
                reasons.append("specialized_validation_rule_required")
            if body.get("validation_level") != "tool_success":
                reasons.append("observed_tool_success_required")
            if snapshot["source_health"] != "valid":
                reasons.append("source_health_is_not_valid")
            if snapshot["problem_changed"]:
                reasons.append("source_problem_changed")
            if snapshot["pending_counterexample_count"] or snapshot.get("revoked_counterexample_count", 0):
                reasons.append("counterexample_disposition_requires_review")
            rows = self.db.execute("SELECT s.*,c.body AS candidate_body,p.task_id,p.conversation FROM knowledge_sources s "
                "JOIN candidates c ON c.project=s.project AND c.id=s.candidate_id "
                "LEFT JOIN problems p ON p.project=s.project AND p.id=s.problem_id "
                "WHERE s.project=? AND s.knowledge_id=? AND s.revision=? ORDER BY s.candidate_id",
                (project, knowledge_id, expected_revision)).fetchall()
            invalid_sources = 0
            for row in rows:
                source_body = json.loads(row["candidate_body"])
                evidence = self._source_evidence(project, json.loads(row["evidence_refs"]), row["problem_id"])
                try:
                    if knowledge_fingerprint(source_body) != knowledge_fingerprint(body):
                        raise ContractError("source body differs")
                    source_body["validation_level"] = "tool_success"
                    self._validate_sources(project, source_body)
                    if self._source_health(evidence) != "valid" or not any(e["current_tool_success"] for e in evidence):
                        raise ContractError("source is not an observed current success")
                    identity = ("task", row["task_id"]) if row["task_id"] else ("conversation", row["conversation"])
                    if not identity[1]:
                        raise ContractError("source independent identity is missing")
                    independent.add(identity)
                except ContractError:
                    invalid_sources += 1
            if invalid_sources:
                reasons.append("supporting_source_contract_invalid")
            if len(independent) < 2:
                reasons.append("two_independent_tasks_required")
            reasons = list(dict.fromkeys(reasons))
            return {"knowledge_id": knowledge_id, "revision": expected_revision,
                "maturity": snapshot["maturity"], "effective_state": snapshot["maturity"],
                "eligible": not reasons, "reasons": reasons, "independent_validation_count": len(independent),
                "source_version": snapshot["source_version"], "rule_version": MATURITY_RULE_VERSION}

    def promote_knowledge(self, project, knowledge_id, expected_revision, request_id=None,
                          reason="explicit promotion", expected_epoch=None):
        from .runtime import ContractError, digest, encode

        _positive(expected_revision, "expected_revision")
        reason = _text(reason, "reason")
        explicit_request = request_id is not None
        request_id = uuid.uuid4().hex if request_id is None else _text(request_id, "request_id", 128)
        request_digest = digest([project, knowledge_id, expected_revision, reason])
        with self._case_transaction():
            self.assert_host_epoch(expected_epoch)
            self._release_project(project)
            previous = self.db.execute("SELECT * FROM knowledge_maturity_receipts WHERE project=? AND request_id=?",
                                       (project, request_id)).fetchone()
            if previous:
                if previous["request_digest"] != request_digest:
                    raise ContractError("maturity request_id already belongs to another request")
                return json.loads(previous["result"])
            evaluation = self.knowledge_maturity(project, knowledge_id, expected_revision)
            if not evaluation["eligible"]:
                raise ContractError("knowledge cannot become stable: " + ", ".join(evaluation["reasons"]))
            if not explicit_request and evaluation["maturity"] == "stable":
                return {"knowledge_id": knowledge_id, "revision": expected_revision, "maturity": "stable",
                    "state": "unchanged", "changed": False,
                    "independent_validation_count": evaluation["independent_validation_count"]}
            snapshot = self._release_snapshot(project, knowledge_id, expected_revision)
            before_state = None
            lifecycle = None
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_actions'").fetchone():
                from . import knowledge_lifecycle as lifecycle
                request = {"action": "promote", "knowledge_id": knowledge_id,
                           "expected_revision": expected_revision, "reason": reason}
                replay = lifecycle.action_request(self, project, request_id, request)
                if replay is not None:
                    return replay
                before_state = lifecycle.lifecycle_state(self, project, knowledge_id)
            changed = evaluation["maturity"] != "stable"
            if changed:
                self.db.execute("INSERT INTO knowledge_maturity VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(project,id,revision) DO UPDATE SET state=excluded.state,source_version=excluded.source_version,"
                    "certification=excluded.certification,reason=excluded.reason,created=excluded.created",
                    (project, knowledge_id, expected_revision, "stable", snapshot["source_version"],
                     self._maturity_certification(snapshot), reason, time.time()))
                self.db.execute("UPDATE projects SET generation=generation+1 WHERE id=?", (project,))
            result = {"knowledge_id": knowledge_id, "revision": expected_revision, "maturity": "stable",
                "state": "promoted" if changed else "unchanged", "changed": changed, "request_id": request_id,
                "independent_validation_count": evaluation["independent_validation_count"]}
            self.db.execute("INSERT INTO knowledge_maturity_receipts VALUES(?,?,?,?,?)",
                            (project, request_id, request_digest, encode(result), time.time()))
            if lifecycle is not None:
                lifecycle.record_action(self, project, request_id, request, "promote", knowledge_id,
                    expected_revision, result, actor="trusted_operator", reason=reason,
                    evidence_refs=[], before_state=before_state)
            return result

    def _release_items(self, project, max_items):
        from .runtime import ContractError, digest

        _positive(max_items, "max_items", 5000)
        rows = self.db.execute("SELECT id,revision FROM active WHERE project=? ORDER BY id LIMIT ?",
                              (project, max_items + 1)).fetchall()
        if len(rows) > max_items:
            raise ContractError("active knowledge exceeds release max_items; no partial release was prepared")
        items = []
        for row in rows:
            snapshot = self._release_snapshot(project, row["id"], row["revision"])
            # 历史正文已由 knowledge 表保存；批次只保存状态与摘要，避免复制全文和证据。
            compact = {key: value for key, value in snapshot.items() if key not in {"body", "sources"}}
            compact.update({"body_digest": digest(snapshot["body"]),
                            "title": snapshot["body"].get("title", "")[:200],
                            "category": snapshot["body"].get("category", "unknown"),
                            "knowledge_kind": snapshot["body"].get("knowledge_kind", "solution")})
            items.append({"knowledge_id": row["id"], "revision": row["revision"],
                          "snapshot": compact, "snapshot_digest": digest(snapshot)})
        return items

    def _release_diff(self, project, parent, items):
        row = self.db.execute("SELECT manifest FROM knowledge_releases WHERE project=? AND id=?", (project, parent)).fetchone() if parent else None
        prior = {item["knowledge_id"]: item["snapshot_digest"] for item in json.loads(row[0])["items"]} if row else {}
        current = {item["knowledge_id"]: item["snapshot_digest"] for item in items}
        changes = {"added": sorted(current.keys() - prior.keys()), "removed": sorted(prior.keys() - current.keys()),
                   "updated": sorted(ident for ident in current.keys() & prior.keys() if current[ident] != prior[ident])}
        return {**changes, "counts": {name: len(ids) for name, ids in changes.items()}}

    @staticmethod
    def _release_diff_preview(diff):
        return {"counts": diff.get("counts", {}),
                **{name: diff.get(name, [])[:5] for name in ("added", "updated", "removed")},
                "ids_truncated": any(len(diff.get(name, [])) > 5 for name in ("added", "updated", "removed"))}

    def prepare_knowledge_release(self, project, reason="", max_items=500, expected_epoch=None):
        from .runtime import digest, encode

        reason = _text(reason, "reason", allow_empty=True)
        with self._case_transaction():
            self.assert_host_epoch(expected_epoch)
            self._release_project(project)
            parent = self._release_head(project)
            items = self._release_items(project, max_items)
            content_hash = digest(items)
            head = self.db.execute("SELECT content_hash FROM knowledge_releases WHERE project=? AND id=?",
                                   (project, parent)).fetchone() if parent else None
            if (not parent and not items) or (head and head[0] == content_hash):
                return {"state": "unchanged", "changed": False, "release_id": parent,
                        "parent_release": parent, "item_count": len(items), "content_hash": content_hash,
                        "diff_counts": {"added": 0, "updated": 0, "removed": 0}}
            release_id = digest([project, parent, content_hash, RELEASE_FORMAT])[:32]
            manifest = {"format": RELEASE_FORMAT, "project_id": project, "parent_release": parent,
                        "content_hash": content_hash, "items": items,
                        "observed_full_state_digest": content_hash, "diff": self._release_diff(project, parent, items)}
            self.db.execute("INSERT OR IGNORE INTO knowledge_releases VALUES(?,?,?,?,?,?,?,?,?,?)",
                (project, release_id, encode(manifest), content_hash, "prepared", parent, reason, time.time(), None, None))
            return {"state": "prepared", "changed": True, "release_id": release_id,
                    "parent_release": parent, "item_count": len(items), "content_hash": content_hash,
                    "diff_counts": manifest["diff"]["counts"]}

    def commit_knowledge_release(self, project, release_id, expected_epoch=None):
        from .runtime import ContractError, digest, encode

        _text(release_id, "release_id", 128)
        with self._case_transaction():
            self.assert_host_epoch(expected_epoch)
            self._release_project(project)
            row = self.db.execute("SELECT * FROM knowledge_releases WHERE project=? AND id=?", (project, release_id)).fetchone()
            if not row:
                raise ContractError("unknown knowledge release")
            if row["state"] in {"committed", "unchanged"}:
                return json.loads(row["result"])
            if self._release_head(project) != row["parent_release"]:
                raise ContractError("knowledge release parent changed; prepare a new release")
            manifest = json.loads(row["manifest"])
            current = self._release_items(project, max(1, len(manifest["items"])))
            if current != manifest["items"] or digest(current) != row["content_hash"]:
                raise ContractError("knowledge release inputs changed; prepare a new release")
            # 单事务复查全部正文、来源、成熟度及生命周期之后才推进批次指针。
            result = {"release_id": release_id, "state": "committed", "changed": True,
                      "parent_release": row["parent_release"], "item_count": len(current),
                      "content_hash": row["content_hash"], "diff_counts": manifest["diff"]["counts"]}
            self.db.execute("UPDATE knowledge_releases SET state='committed',result=?,completed=? WHERE project=? AND id=?",
                            (encode(result), time.time(), project, release_id))
            self.db.execute("INSERT INTO knowledge_release_heads VALUES(?,?) "
                            "ON CONFLICT(project) DO UPDATE SET id=excluded.id", (project, release_id))
            return result

    def daily_knowledge_release(self, project, reason="Daily Release", max_items=500, expected_epoch=None):
        """显式增量发布入口，无变更直接跳过；准备后变化需调用者重新准备。"""
        prepared = self.prepare_knowledge_release(project, reason=reason, max_items=max_items, expected_epoch=expected_epoch)
        if prepared["state"] == "unchanged":
            return prepared
        return self.commit_knowledge_release(project, prepared["release_id"], expected_epoch=expected_epoch)

    @staticmethod
    def _release_summary(row):
        manifest = json.loads(row["manifest"])
        return {"release_id": row["id"], "state": row["state"], "parent_release": row["parent_release"],
            "content_hash": row["content_hash"], "item_count": len(manifest["items"]),
            "created": row["created"], "completed": row["completed"], "reason": row["reason"][:200],
            "rollback_of": manifest.get("rollback_of"), "diff_counts": manifest.get("diff", {}).get("counts", {})}

    def list_knowledge_releases(self, project, limit=20, offset=0, max_chars=12000):
        from .runtime import ContractError, encode

        _positive(limit, "limit", 100)
        _positive(max_chars, "max_chars", 50000)
        if max_chars < 2048 or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ContractError("offset must be nonnegative and max_chars at least 2048")
        with self._release_read_transaction():
            self._release_project(project)
            rows = self.db.execute("SELECT * FROM knowledge_releases WHERE project=? ORDER BY created DESC,id LIMIT ? OFFSET ?",
                                  (project, limit + 1, offset)).fetchall()
            result = {"head_release": self._release_head(project), "items": [], "next_offset": None}
            for row in rows[:limit]:
                candidate = self._release_summary(row)
                if len(encode({**result, "items": result["items"] + [candidate], "next_offset": offset + limit})) > max_chars:
                    break
                result["items"].append(candidate)
            if len(rows) > len(result["items"]):
                result["next_offset"] = offset + len(result["items"])
            return result

    def read_knowledge_release(self, project, release_id, limit=20, offset=0, max_chars=12000):
        from .runtime import ContractError, encode

        _positive(limit, "limit", 100)
        _positive(max_chars, "max_chars", 50000)
        if max_chars < 2048 or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ContractError("offset must be nonnegative and max_chars at least 2048")
        with self._release_read_transaction():
            self._release_project(project)
            row = self.db.execute("SELECT * FROM knowledge_releases WHERE project=? AND id=?", (project, release_id)).fetchone()
            if not row:
                raise ContractError("unknown knowledge release")
            manifest = json.loads(row["manifest"])
            result = {**self._release_summary(row), "diff": self._release_diff_preview(manifest.get("diff", {})),
                      "items": [], "next_offset": None}
            for item in manifest["items"][offset:offset + limit]:
                snapshot = item["snapshot"]
                preview = {"knowledge_id": item["knowledge_id"], "revision": item["revision"],
                    "snapshot_digest": item["snapshot_digest"], "maturity": snapshot["maturity"],
                    "source_version": snapshot["source_version"], "lifecycle_version": snapshot.get("lifecycle_version", 0),
                    "source_health": snapshot["source_health"], "active": snapshot["active"],
                    "pending_counterexample_count": snapshot["pending_counterexample_count"]}
                if len(encode({**result, "items": result["items"] + [preview], "next_offset": offset + limit})) > max_chars:
                    break
                result["items"].append(preview)
            if offset + len(result["items"]) < len(manifest["items"]):
                result["next_offset"] = offset + len(result["items"])
            return result

    def rollback_knowledge_release(self, project, release_id, expected_head=None, request_id=None,
                                   reason="explicit safe rollback", expected_epoch=None):
        from .runtime import ContractError, digest, encode

        release_id = _text(release_id, "release_id", 128)
        expected_head = _text(expected_head, "expected_head", 128)
        request_id = uuid.uuid4().hex if request_id is None else _text(request_id, "request_id", 128)
        reason = _text(reason, "reason")
        with self._case_transaction():
            self.assert_host_epoch(expected_epoch)
            self._release_project(project)
            rollback_id = digest([project, "rollback", request_id])[:32]
            previous = self.db.execute("SELECT * FROM knowledge_releases WHERE project=? AND id=?", (project, rollback_id)).fetchone()
            if previous:
                manifest = json.loads(previous["manifest"])
                if (manifest.get("rollback_of"), manifest.get("expected_head"), previous["reason"]) != (release_id, expected_head, reason):
                    raise ContractError("rollback request_id already belongs to another request")
                return json.loads(previous["result"])
            parent = self._release_head(project)
            if expected_head is None or parent != expected_head:
                raise ContractError("rollback requires the expected current release head")
            target = self.db.execute("SELECT * FROM knowledge_releases WHERE project=? AND id=? AND state='committed'",
                                     (project, release_id)).fetchone()
            if not target:
                raise ContractError("rollback target must be a committed release")
            old_manifest = json.loads(target["manifest"])
            for item in old_manifest["items"]:
                snapshot = self._release_snapshot(project, item["knowledge_id"], item["revision"])
                if not snapshot["active"] or snapshot["source_health"] != "valid" or snapshot["problem_changed"] \
                        or snapshot["pending_counterexample_count"] or snapshot.get("revoked_counterexample_count", 0):
                    raise ContractError("rollback target has revoked, replaced, changed, or counterexample knowledge")
            full_items = self._release_items(project, 5000)
            target_ids = {item["knowledge_id"] for item in old_manifest["items"]}
            items = [item for item in full_items if item["knowledge_id"] in target_ids]
            content_hash = digest(items)
            head = self.db.execute("SELECT content_hash FROM knowledge_releases WHERE project=? AND id=?",
                                   (project, parent)).fetchone()
            changed = not head or head[0] != content_hash
            state = "committed" if changed else "unchanged"
            manifest = {"format": RELEASE_FORMAT, "project_id": project, "parent_release": parent,
                "content_hash": content_hash, "items": items, "rollback_of": release_id, "expected_head": expected_head,
                "observed_full_state_digest": digest(full_items), "diff": self._release_diff(project, parent, items)}
            result = {"release_id": rollback_id if changed else parent, "receipt_id": rollback_id,
                "state": state, "changed": changed, "parent_release": parent, "rollback_of": release_id,
                "item_count": len(items), "content_hash": content_hash, "active_knowledge_changed": False,
                "diff_counts": manifest["diff"]["counts"]}
            self.db.execute("INSERT INTO knowledge_releases VALUES(?,?,?,?,?,?,?,?,?,?)",
                (project, rollback_id, encode(manifest), content_hash, state, parent, reason, time.time(), encode(result), time.time()))
            if changed:
                self.db.execute("INSERT INTO knowledge_release_heads VALUES(?,?) ON CONFLICT(project) DO UPDATE SET id=excluded.id",
                                (project, rollback_id))
            return result
