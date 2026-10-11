"""知识分发与批次共用的确定性快照；不复制原始日志。"""
from __future__ import annotations

import json


class PublicationStateMixin:
    def knowledge_state_snapshot(self, project, knowledge_id, revision, *, _public_data=None):
        from .runtime import ContractError, clean, digest
        row = self.db.execute("SELECT * FROM knowledge WHERE project=? AND id=? AND revision=?",
                              (project, knowledge_id, revision)).fetchone()
        if not row:
            raise ContractError("knowledge snapshot is absent")
        public = _public_data if _public_data is not None else self._public(project, row, include_maturity=False)
        active = self.db.execute("SELECT revision FROM active WHERE project=? AND id=?",
                                 (project, knowledge_id)).fetchone()
        lifecycle = self.db.execute("SELECT version FROM knowledge_lifecycle_versions WHERE project=? AND knowledge_id=?",
                                    (project, knowledge_id)).fetchone()
        # 全部来源参与指纹，输出只携带有限引用和健康摘要。
        sources = []
        for source in public["sources"]:
            sources.append({k: source[k] for k in ("candidate_id", "problem_id", "problem_revision",
                                                   "problem_changed", "health", "current_tool_success")})
            sources[-1]["evidence"] = [{k: item[k] for k in
                                        ("event_id", "health", "current_tool_success")}
                                       for item in source["evidence"]]
        feedback = [dict(item) for item in self.db.execute(
            "SELECT f.problem_id,p.revision AS problem_revision,f.disposition,f.disposition_receipt "
            "FROM knowledge_feedback f JOIN problems p ON p.project=f.project AND p.id=f.problem_id "
            "WHERE f.project=? AND f.knowledge_id=? AND f.revision=? ORDER BY f.problem_id",
            (project, knowledge_id, revision))]
        preview = [{**{k: v for k, v in source.items() if k != "evidence"},
                    "evidence_count": len(source["evidence"]),
                    "evidence_refs": [item["event_id"] for item in source["evidence"][:20]]}
                   for source in sources[:20]]
        snapshot = {"project_id": project, "knowledge_id": knowledge_id, "revision": revision,
                    "body": clean(json.loads(row["body"])), "active": bool(active and active[0] == revision),
                    "source_version": public["source_version"], "lifecycle_version": lifecycle[0] if lifecycle else 0,
                    "maturity": "candidate", "source_health": public["source_health"],
                    "current_tool_success": public["current_tool_success"], "problem_changed": public["problem_changed"],
                    "pending_counterexample_count": public["pending_counterexample_count"],
                    "revoked_counterexample_count": sum(item["disposition"] == "revoked" for item in feedback),
                    "source_count": len(sources), "sources": preview,
                    "source_digest": digest(sources), "feedback_digest": digest(feedback)}
        snapshot["maturity"] = self._maturity_state_from_snapshot(project, knowledge_id, revision, snapshot)
        return snapshot

    def on_lifecycle_change(self, project, knowledge_id, revision, action):
        # 正文生命周期变化后必须重新达到晋升门槛；不会沿用旧认证。
        self.db.execute("UPDATE context_exposures SET source_version=-1 WHERE project=? AND knowledge_id=? AND revision=?",
                        (project, knowledge_id, revision))
        if action == "promote":
            return
        self.db.execute("DELETE FROM knowledge_maturity WHERE project=? AND id=?",
                        (project, knowledge_id))

    def knowledge_actions(self, project, knowledge_id=None, limit=20, offset=0):
        from .runtime import ContractError, encode
        if (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
                or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
            raise ContractError("action page requires limit 1..100 and nonnegative offset")
        query = "FROM knowledge_actions WHERE project=?" + (" AND knowledge_id=?" if knowledge_id is not None else "")
        params = [project] + ([knowledge_id] if knowledge_id is not None else [])
        total = self.db.execute("SELECT COUNT(*) " + query, params).fetchone()[0]
        rows = self.db.execute("SELECT request_id,action,knowledge_id,revision,actor,reason,created " + query +
                               " ORDER BY created DESC,request_id LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["reason"] = item["reason"][:500]
            if len(encode(items + [item])) > 11000:
                break
            items.append(item)
        return {"items": items, "total": total, "offset": offset,
                "next_offset": offset + len(items) if offset + len(items) < total else None}

    def knowledge_action(self, project, request_id):
        from .runtime import ContractError, clip
        row = self.db.execute("SELECT request_id,action,knowledge_id,revision,actor,reason,evidence_refs,"
                              "before_state,after_state,response,created FROM knowledge_actions WHERE project=? AND request_id=?",
                              (project, request_id)).fetchone()
        if not row:
            raise ContractError("knowledge action is absent")
        result = dict(row)
        for key in ("evidence_refs", "before_state", "after_state", "response"):
            result[key] = json.loads(result[key])
        result["reason"] = clip(result["reason"], 2000)
        result["evidence_count"] = len(result["evidence_refs"])
        result["evidence_refs"] = result["evidence_refs"][:100]
        return result


def install_publication_triggers(runtime):
    """在已有来源版本投影上登记增量同步；旧来源触发器保持原契约。"""
    triggers = {
        "source_insert": ("AFTER INSERT ON knowledge_source_versions", "", "NEW.knowledge_id", "NEW.revision", "NEW.project"),
        "source_update": ("AFTER UPDATE OF version ON knowledge_source_versions", "WHEN OLD.version IS NOT NEW.version",
                          "NEW.knowledge_id", "NEW.revision", "NEW.project"),
        "active_insert": ("AFTER INSERT ON active", "", "NEW.id", "NEW.revision", "NEW.project"),
        "active_delete": ("AFTER DELETE ON active", "", "OLD.id", "OLD.revision", "OLD.project"),
        "active_update": ("AFTER UPDATE OF revision ON active", "WHEN OLD.revision IS NOT NEW.revision",
                          "NEW.id", "NEW.revision", "NEW.project"),
        "maturity_insert": ("AFTER INSERT ON knowledge_maturity", "", "NEW.id", "NEW.revision", "NEW.project"),
        "maturity_update": ("AFTER UPDATE ON knowledge_maturity", "", "NEW.id", "NEW.revision", "NEW.project"),
        "maturity_delete": ("AFTER DELETE ON knowledge_maturity", "", "OLD.id", "OLD.revision", "OLD.project"),
    }
    for name, (event, condition, ident, revision, project) in triggers.items():
        runtime.db.execute(f"CREATE TRIGGER IF NOT EXISTS publication_dirty_{name} {event} {condition} BEGIN "
                           f"INSERT INTO knowledge_sync_dirty(project,knowledge_id,revision) "
                           f"SELECT {project},{ident},{revision} WHERE NOT EXISTS(SELECT 1 FROM knowledge_sync_dirty "
                           f"WHERE project={project} AND knowledge_id={ident} AND revision={revision}); END")
    # 修订替换时，旧版也必须发布失效状态。
    runtime.db.execute("CREATE TRIGGER IF NOT EXISTS publication_dirty_old_revision AFTER UPDATE OF revision ON active "
                       "WHEN OLD.revision IS NOT NEW.revision BEGIN "
                       "INSERT INTO knowledge_sync_dirty(project,knowledge_id,revision) "
                       "SELECT OLD.project,OLD.id,OLD.revision WHERE NOT EXISTS(SELECT 1 FROM knowledge_sync_dirty "
                       "WHERE project=OLD.project AND knowledge_id=OLD.id AND revision=OLD.revision); END")
