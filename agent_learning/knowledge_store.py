"""知识正文、发布回执和来源关系的持久化。

正文修订是历史记录；active 与全文索引是可以重建的当前发布投影。
已完成的发布请求只回放回执，撤销后的重新启用必须显式恢复。
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time


def problem_feedback_target(runtime, project, problem):
    """Separate explicit feedback targets from the short-lived publication backfill.

    Publication lineage lives in knowledge_sources.  The 8f54757 writer also filled
    the feedback target columns for ordinary source cases; recognize only that
    evidenced legacy shape, preserving actual feedback and counterexample targets.
    Unknown legacy bindings remain conservative explicit targets.
    """
    knowledge_id, revision = problem["knowledge_id"], problem["knowledge_revision"]
    counterexample = runtime.db.execute(
        "SELECT knowledge_id,revision FROM knowledge_feedback WHERE project=? AND problem_id=? "
        "ORDER BY created,knowledge_id,revision LIMIT 1",
        (project, problem["id"]),
    ).fetchone()
    if counterexample:
        return counterexample["knowledge_id"], counterexample["revision"]
    feedback = runtime.db.execute(
        "SELECT json_extract(e.payload,'$.knowledge_id') AS knowledge_id, "
        "json_extract(e.payload,'$.knowledge_revision') AS revision FROM problem_events pe JOIN events e ON e.id=pe.event "
        "WHERE pe.problem=? AND e.project=? AND e.type='Feedback' "
        "AND json_extract(e.payload,'$.knowledge_id') IS NOT NULL ORDER BY e.rowid LIMIT 1",
        (problem["id"], project),
    ).fetchone()
    if feedback:
        # The old backfill also advanced explicit observation columns when the
        # same ID was updated.  The original event keeps its exact target intact.
        return feedback["knowledge_id"], feedback["revision"]
    if not knowledge_id:
        return None, None
    derived = runtime.db.execute(
        "SELECT 1 FROM knowledge_sources WHERE project=? AND problem_id=? "
        "AND knowledge_id=? AND revision=? LIMIT 1",
        (project, problem["id"], knowledge_id, revision),
    ).fetchone()
    return (None, None) if derived else (knowledge_id, revision)


class KnowledgeStoreMixin:
    def _migrate_knowledge_store(self):
        """在同一写事务内清洗旧正文并重建派生数据，不猜测历史发布目标。"""
        from .runtime import clean, digest, encode, knowledge_fingerprint, metadata
        with self.transaction():
            if self.db.execute("PRAGMA user_version").fetchone()[0] >= 5:
                return
            # 旧表 id 全局唯一；复合唯一键让来源同时约束项目归属。
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS candidates_project_id ON candidates(project,id)")
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS problems_project_id ON problems(project,id)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS publication_receipts(
                project TEXT NOT NULL,candidate_id TEXT NOT NULL,requested_id TEXT,
                expected_revision INTEGER,response TEXT NOT NULL,created REAL NOT NULL,
                PRIMARY KEY(project,candidate_id),
                FOREIGN KEY(project,candidate_id) REFERENCES candidates(project,id))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS knowledge_sources(
                project TEXT NOT NULL,knowledge_id TEXT NOT NULL,revision INTEGER NOT NULL,
                candidate_id TEXT NOT NULL,problem_id TEXT NOT NULL,problem_revision INTEGER NOT NULL,
                evidence_refs TEXT NOT NULL,created REAL NOT NULL,
                PRIMARY KEY(project,knowledge_id,revision,candidate_id),
                FOREIGN KEY(project,knowledge_id,revision) REFERENCES knowledge(project,id,revision),
                FOREIGN KEY(project,candidate_id) REFERENCES candidates(project,id),
                FOREIGN KEY(project,problem_id) REFERENCES problems(project,id))""")
            self.db.execute("CREATE INDEX IF NOT EXISTS knowledge_sources_candidate ON knowledge_sources(project,candidate_id)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS candidate_identity(
                project TEXT NOT NULL,fingerprint TEXT NOT NULL,candidate_id TEXT NOT NULL,
                PRIMARY KEY(project,fingerprint),
                FOREIGN KEY(project,candidate_id) REFERENCES candidates(project,id))""")
            for table in ("contexts", "subagent_contexts"):
                columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                if "source_states" not in columns:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN source_states TEXT NOT NULL DEFAULT '{{}}'")
            for table in ("candidates", "knowledge"):
                for row in self.db.execute(f"SELECT rowid,body FROM {table}").fetchall():
                    body = clean(json.loads(row["body"]))
                    body["metadata"] = metadata(body.get("metadata"))
                    body.setdefault("knowledge_kind", "solution")
                    body["evidence_refs"] = sorted(set(body["evidence_refs"]))
                    self.db.execute(f"UPDATE {table} SET body=? WHERE rowid=?", (encode(body), row["rowid"]))
            self.db.execute("DELETE FROM knowledge_fingerprints")
            for row in self.db.execute("SELECT project,id,revision,body FROM knowledge").fetchall():
                self.db.execute("INSERT INTO knowledge_fingerprints VALUES(?,?,?,?)",
                                (row["project"], row["id"], row["revision"], knowledge_fingerprint(json.loads(row["body"]))))
            for candidate in self.db.execute("SELECT * FROM candidates WHERE status='published'").fetchall():
                matches = self.db.execute("SELECT id,revision,created FROM knowledge WHERE project=? AND body=? ORDER BY revision,created",
                                          (candidate["project"], candidate["body"])).fetchall()
                body = json.loads(candidate["body"])
                recovered = matches[0] if len({row["id"] for row in matches}) == 1 else None
                deduplicated = False
                if not matches:
                    # 旧版去重没有保存第二个来源；全内容指纹唯一指向一个修订才可恢复。
                    equivalent = self.db.execute("SELECT id,revision FROM knowledge_fingerprints WHERE project=? AND fingerprint=?",
                                                 (candidate["project"], knowledge_fingerprint(body))).fetchall()
                    if len(equivalent) == 1:
                        recovered, deduplicated = equivalent[0], True
                if recovered is not None:
                    response = {"knowledge_id": recovered["id"], "revision": recovered["revision"], "status": "published"}
                    if deduplicated:
                        response["deduplicated"] = True
                    # 损坏的历史来源保留回执保护，但不能凭迁移伪造关联。
                    if self.db.execute("SELECT 1 FROM problems WHERE project=? AND id=?",
                                       (candidate["project"], body["problem_id"])).fetchone():
                        self._link_knowledge_source(candidate["project"], recovered["id"], recovered["revision"], candidate, body)
                else:
                    response = {"status": "legacy_unresolved", "candidate_id": candidate["id"]}
                self.db.execute("INSERT OR IGNORE INTO publication_receipts VALUES(?,?,?,?,?,?)",
                                (candidate["project"], candidate["id"], None, None, encode(response), candidate["created"]))
            self.db.execute("DELETE FROM candidate_identity")
            for candidate in self.db.execute("SELECT c.* FROM candidates c LEFT JOIN publication_receipts r "
                                             "ON c.project=r.project AND c.id=r.candidate_id "
                                             "ORDER BY (c.status='published' AND r.candidate_id IS NOT NULL) DESC,c.created,c.id").fetchall():
                # 补默认字段后正文摘要会变化；身份别名保留历史ID，避免重提绕过撤销和回执。
                self.db.execute("INSERT OR IGNORE INTO candidate_identity VALUES(?,?,?)",
                                (candidate["project"], digest([candidate["project"], json.loads(candidate["body"])]), candidate["id"]))
            self._rebuild_knowledge_index()
            self.db.execute("DELETE FROM cache")
            self.db.execute("PRAGMA user_version=5")

    def _link_knowledge_source(self, project, knowledge_id, revision, candidate, body):
        from .runtime import encode
        return self.db.execute("INSERT OR IGNORE INTO knowledge_sources VALUES(?,?,?,?,?,?,?,?)",
                               (project, knowledge_id, revision, candidate["id"], body["problem_id"],
                                body["problem_revision"], encode(body["evidence_refs"]), time.time())).rowcount

    def _finish_publication(self, project, candidate, body, knowledge_id, revision, requested_id,
                            expected_revision, response, changed, before_state):
        from .runtime import encode
        from .knowledge_lifecycle import record_action
        linked = self._link_knowledge_source(project, knowledge_id, revision, candidate, body)
        self.db.execute("UPDATE candidates SET status='published' WHERE project=? AND id=?", (project, candidate["id"]))
        # A publication creates source lineage, never a retrospective feedback
        # target or a mutation of the evidence packet that produced this candidate.
        self.db.execute("UPDATE problems SET status='reviewed' WHERE project=? AND id=?",
                        (project, body["problem_id"]))
        self.db.execute("UPDATE review_states SET state='published',updated=? WHERE project=? AND problem=? AND revision=?",
                        (time.time(), project, body["problem_id"], body["problem_revision"]))
        if changed or linked:
            self.db.execute("UPDATE projects SET generation=generation+1 WHERE id=?", (project,))
        self.db.execute("INSERT INTO publication_receipts VALUES(?,?,?,?,?,?)",
                        (project, candidate["id"], requested_id, expected_revision, encode(response), time.time()))
        record_action(self, project, "publication:" + candidate["id"],
                      {"candidate_id": candidate["id"], "requested_id": requested_id, "expected_revision": expected_revision},
                      "attach" if not changed else "update" if before_state["latest_revision"] else "publish",
                      knowledge_id, revision, response, actor="publisher", reason="validated candidate publication",
                      evidence_refs=body["evidence_refs"], before_state=before_state)
        published_body = self.db.execute("SELECT body FROM knowledge WHERE project=? AND id=? AND revision=?",
                                         (project, knowledge_id, revision)).fetchone()[0]
        self._queue_sync(project, knowledge_id, revision, json.loads(published_body))
        return response

    def publish(self, project, candidate_id, expected_generation=None, knowledge_id=None, expected_revision=None,
                expected_epoch=None, *, _review_approval=False):
        from .runtime import ContractError, clean, digest, encode, knowledge_fingerprint, metadata
        from .knowledge_lifecycle import lifecycle_state, publication_lineage_state
        from .knowledge_equivalence import fingerprint_query, same_presentation_claim
        with self._case_transaction():
            self.assert_host_epoch(expected_epoch)
            candidate = self.db.execute("SELECT * FROM candidates WHERE id=? AND project=?", (candidate_id, project)).fetchone()
            if not candidate:
                raise ContractError("unknown candidate")
            receipt = self.db.execute("SELECT * FROM publication_receipts WHERE project=? AND candidate_id=?",
                                      (project, candidate_id)).fetchone()
            if receipt:
                response = json.loads(receipt["response"])
                if response.get("status") == "legacy_unresolved":
                    raise ContractError("legacy publication target is unresolved; review a new candidate")
                if knowledge_id is not None and (knowledge_id != receipt["requested_id"]
                                                  or expected_revision != receipt["expected_revision"]
                                                  or not isinstance(expected_revision, int)
                                                  or isinstance(expected_revision, bool)):
                    raise ContractError("candidate already published with another request")
                if knowledge_id is None and expected_revision is not None:
                    raise ContractError("expected_revision requires a knowledge_id")
                return response
            # 发布标记缺少回执可能表示数据受损，不把它当成新的请求。
            if candidate["status"] == "published":
                raise ContractError("published candidate receipt is missing; inspect storage")
            if candidate["status"] not in {"candidate", "deferred"}:
                raise ContractError("candidate is rejected or not available for publication")
            body = clean(json.loads(candidate["body"]))
            body["metadata"] = metadata(body.get("metadata"))
            body.setdefault("knowledge_kind", "solution")
            from .history_response_recovery import result_access
            recovered = result_access(self, project, body["problem_id"], candidate_id=candidate_id,
                                      expected_epoch=expected_epoch)
            if recovered is not None:
                pass
            elif _review_approval:
                from .candidate_review import history_publication_guard
                history_publication_guard(self, project, body["problem_id"], expected_epoch=expected_epoch,
                                          candidate_id=candidate_id)
            else:
                self.history_guard(project, body["problem_id"], expected_epoch=expected_epoch,
                                   _local_action="publish", _local_candidate=candidate_id)
            self._validate_sources(project, body)
            lineage = publication_lineage_state(self, project, body, knowledge_id, candidate_id)
            if lineage["issues"]:
                raise ContractError(lineage["issues"][0]["message"])
            version = self.db.execute("SELECT generation FROM projects WHERE id=?", (project,)).fetchone()
            if not version:
                raise ContractError("unknown project")
            if expected_generation is not None and version[0] != expected_generation:
                raise ContractError("publication manifest changed")
            requested_id = knowledge_id
            fingerprint = knowledge_fingerprint(body)
            marks, fingerprints = fingerprint_query(body)
            duplicates = self.db.execute("SELECT a.id,a.revision,k.body FROM knowledge_fingerprints f JOIN active a "
                                         "ON a.project=f.project AND a.id=f.id AND a.revision=f.revision JOIN knowledge k "
                                         "ON k.project=a.project AND k.id=a.id AND k.revision=a.revision "
                                         f"WHERE f.project=? AND f.fingerprint IN ({marks}) ORDER BY a.id LIMIT 65",
                                         (project, *fingerprints)).fetchall()
            if len(duplicates) > 64:
                raise ContractError("too many equivalent active knowledge records; explicit comparison required")

            def format_attachment_guard(target_body):
                if knowledge_fingerprint(target_body) == fingerprint:
                    return
                from .candidate_references import held_equivalent
                # Explicit visual approval may release a deferred candidate,
                # exactly as for an unchanged body. Unresolved concerns and
                # upstream lineage checks still apply in either path.
                held = held_equivalent(self, project, body, include_held=not _review_approval)
                if held:
                    raise ContractError(held)
                # Explicit attachment must not bypass the protections that a
                # fresh publication would apply to this same stored claim.
                state = publication_lineage_state(self, project, body, candidate_id=candidate_id)
                if state["issues"]:
                    raise ContractError(state["issues"][0]["message"])
            if knowledge_id is not None:
                if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
                    raise ContractError("knowledge updates require the expected active revision")
                current = self.db.execute("SELECT k.* FROM knowledge k JOIN active a ON k.project=a.project AND "
                                          "k.id=a.id AND k.revision=a.revision WHERE k.project=? AND k.id=?",
                                          (project, knowledge_id)).fetchone()
                if not current or current["revision"] != expected_revision:
                    raise ContractError("knowledge revision changed or is revoked")
                if any(row["id"] != knowledge_id for row in duplicates):
                    raise ContractError("equivalent knowledge already has another active ID")
                current_body = json.loads(current["body"])
                if same_presentation_claim(body, current_body):
                    format_attachment_guard(current_body)
                    revision = current["revision"]
                    response = {"knowledge_id": knowledge_id, "revision": revision, "status": "published", "deduplicated": True}
                    if knowledge_fingerprint(current_body) != fingerprint:
                        response["format_equivalent"] = True
                    return self._finish_publication(project, candidate, body, knowledge_id, revision, requested_id,
                                                    expected_revision, response, False, lifecycle_state(self, project, knowledge_id))
            elif expected_revision is not None:
                raise ContractError("expected_revision requires a knowledge_id")
            elif duplicates:
                duplicate = duplicates[0]
                duplicate_body = json.loads(duplicate["body"])
                format_attachment_guard(duplicate_body)
                response = {"knowledge_id": duplicate["id"], "revision": duplicate["revision"], "status": "published", "deduplicated": True}
                if knowledge_fingerprint(duplicate_body) != fingerprint:
                    response["format_equivalent"] = True
                return self._finish_publication(project, candidate, body, duplicate["id"], duplicate["revision"], None,
                                                None, response, False, lifecycle_state(self, project, duplicate["id"]))
            else:
                knowledge_id = digest([project, "candidate", candidate_id])[:32]
                if self.db.execute("SELECT 1 FROM knowledge WHERE project=? AND id=?", (project, knowledge_id)).fetchone():
                    raise ContractError("knowledge ID already has history; restore or explicitly update it")
            before_state = lifecycle_state(self, project, knowledge_id)
            revision = self.db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM knowledge WHERE project=? AND id=?",
                                       (project, knowledge_id)).fetchone()[0]
            self.db.execute("INSERT INTO knowledge VALUES(?,?,?,?,?)", (project, knowledge_id, revision, encode(body), time.time()))
            self.db.execute("INSERT INTO knowledge_fingerprints VALUES(?,?,?,?)", (project, knowledge_id, revision, fingerprint))
            self.db.execute("INSERT INTO active VALUES(?,?,?) ON CONFLICT(project,id) DO UPDATE SET revision=excluded.revision",
                            (project, knowledge_id, revision))
            self._index_knowledge(project, knowledge_id, revision, body)
            response = {"knowledge_id": knowledge_id, "revision": revision, "status": "published"}
            return self._finish_publication(project, candidate, body, knowledge_id, revision, requested_id,
                                            expected_revision, response, True, before_state)

    def revoke(self, project, knowledge_id, expected_revision=None, *, request_id=None, reason="explicit revoke",
               actor="operator", expected_generation=None, expected_epoch=None):
        from .runtime import ContractError
        from .knowledge_lifecycle import action_request, lifecycle_state, record_action, request_identity

        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
            raise ContractError("revoke requires a positive expected active revision")
        request_id, reason, actor, refs = request_identity(request_id, reason, actor)
        request = {"action": "revoke", "knowledge_id": knowledge_id, "expected_revision": expected_revision,
                   "expected_generation": expected_generation, "reason": reason, "actor": actor, "evidence_refs": refs}
        with self._case_transaction():
            self.assert_host_epoch(expected_epoch)
            replay = action_request(self, project, request_id, request)
            if replay is not None:
                return replay
            before = lifecycle_state(self, project, knowledge_id)
            if before["active_revision"] != expected_revision:
                raise ContractError("knowledge active revision changed or is revoked")
            manifest = self.db.execute("SELECT generation FROM projects WHERE id=?", (project,)).fetchone()
            if not manifest or (expected_generation is not None and manifest[0] != expected_generation):
                raise ContractError("publication manifest changed")
            changed = self.db.execute("DELETE FROM active WHERE project=? AND id=?", (project, knowledge_id)).rowcount
            if changed:
                self._remove_knowledge_index(project, knowledge_id)
                self.db.execute("UPDATE projects SET generation=generation+1,revocation_epoch=revocation_epoch+1 WHERE id=?", (project,))
            response = {"knowledge_id": knowledge_id, "revision": expected_revision, "revoked": bool(changed),
                        "request_id": request_id, "changed": bool(changed)}
            record_action(self, project, request_id, request, "revoke", knowledge_id, expected_revision, response,
                          actor=actor, reason=reason, evidence_refs=refs, before_state=before)
            row = self.db.execute("SELECT body FROM knowledge WHERE project=? AND id=? AND revision=?",
                                  (project, knowledge_id, expected_revision)).fetchone()
            self._queue_sync(project, knowledge_id, expected_revision, json.loads(row[0]))
            return response

    def restore(self, project, knowledge_id, expected_revision, expected_generation=None, *, request_id=None,
                reason="explicit restore", actor="operator", expected_epoch=None, evidence_refs=None):
        from .runtime import ContractError
        from .knowledge_equivalence import fingerprint_query, same_presentation_claim
        from .knowledge_lifecycle import action_request, lifecycle_state, record_action, request_identity, validate_restore_support

        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
            raise ContractError("restore requires the expected latest revision")
        request_id, reason, actor, refs = request_identity(request_id, reason, actor, evidence_refs)
        request = {"action": "restore", "knowledge_id": knowledge_id, "expected_revision": expected_revision,
                   "expected_generation": expected_generation, "reason": reason, "actor": actor, "evidence_refs": refs}
        with self._case_transaction():
            self.assert_host_epoch(expected_epoch)
            replay = action_request(self, project, request_id, request)
            if replay is not None:
                return replay
            before = lifecycle_state(self, project, knowledge_id)
            if self.db.execute("SELECT 1 FROM active WHERE project=? AND id=?", (project, knowledge_id)).fetchone():
                raise ContractError("knowledge is already active")
            row = self.db.execute("SELECT * FROM knowledge WHERE project=? AND id=? ORDER BY revision DESC LIMIT 1",
                                  (project, knowledge_id)).fetchone()
            if not row or row["revision"] != expected_revision:
                raise ContractError("knowledge latest revision changed or is missing")
            manifest = self.db.execute("SELECT generation FROM projects WHERE id=?", (project,)).fetchone()
            if not manifest or (expected_generation is not None and manifest[0] != expected_generation):
                raise ContractError("publication manifest changed")
            body = json.loads(row["body"])
            marks, fingerprints = fingerprint_query(body)
            equivalent = self.db.execute("SELECT a.id FROM knowledge_fingerprints f JOIN active a ON a.project=f.project "
                                         f"AND a.id=f.id AND a.revision=f.revision WHERE f.project=? AND f.fingerprint IN ({marks}) LIMIT 1",
                                         (project, *fingerprints)).fetchone()
            if equivalent:
                raise ContractError("equivalent knowledge already has another active ID")
            sources = self.db.execute("SELECT s.*,c.body FROM knowledge_sources s JOIN candidates c "
                                      "ON s.project=c.project AND s.candidate_id=c.id "
                                      "WHERE s.project=? AND s.knowledge_id=? AND s.revision=? ORDER BY s.created,s.candidate_id",
                                      (project, knowledge_id, expected_revision)).fetchall()
            valid_source = None
            for source in sources:
                source_body = json.loads(source["body"])
                if not same_presentation_claim(source_body, body):
                    continue
                # 来源不能降低目标知识原先声明的证据级别。
                source_body["validation_level"] = body["validation_level"]
                try:
                    self._validate_sources(project, source_body)
                except ContractError:
                    continue
                valid_source = source["candidate_id"]
                break
            if not valid_source:
                raise ContractError("no current valid source can restore this knowledge")
            validate_restore_support(self, project, knowledge_id, expected_revision, reason, refs, body["validation_level"])
            self.db.execute("INSERT INTO active VALUES(?,?,?)", (project, knowledge_id, expected_revision))
            self._index_knowledge(project, knowledge_id, expected_revision, body)
            # 撤销曾关闭的反例重新进入待治理状态；已修订和已驳回处置保持历史依据。
            reopened = self.db.execute("UPDATE knowledge_feedback SET disposition='unresolved',disposition_reason=NULL,"
                                      "disposition_receipt=NULL WHERE project=? AND knowledge_id=? AND revision=? "
                                      "AND kind='counterexample' AND disposition='revoked'",
                                      (project, knowledge_id, expected_revision)).rowcount
            self.db.execute("UPDATE projects SET generation=generation+1 WHERE id=?", (project,))
            response = {"knowledge_id": knowledge_id, "revision": expected_revision, "status": "restored",
                        "source_candidate_id": valid_source, "request_id": request_id, "changed": True,
                        "reopened_counterexamples": reopened}
            record_action(self, project, request_id, request, "restore", knowledge_id, expected_revision, response,
                          actor=actor, reason=reason, evidence_refs=refs, before_state=before)
            self._queue_sync(project, knowledge_id, expected_revision, body)
            return response

    def _source_evidence(self, project, refs, problem_id=None, *, _lineage_evaluation=None):
        from .knowledge_lineage import source_state
        evidence = []
        for ref in dict.fromkeys(refs):
            row = self.db.execute("SELECT e.type,e.outcome,e.created,e.rowid AS sequence,e.operation,o.conflict,o.decision "
                                  "FROM events e LEFT JOIN operations o ON o.project=e.project AND o.id=e.operation "
                                  "WHERE e.project=? AND e.id=?", (project, ref)).fetchone()
            linked = (problem_id is None or self.db.execute("SELECT 1 FROM problem_events WHERE problem=? AND event=?",
                                                           (problem_id, ref)).fetchone())
            if not row or not linked:
                evidence.append({"event_id": ref, "outcome": "missing", "health": "missing", "current_tool_success": False})
                continue
            health = "conflicted" if row["conflict"] else "denied" if row["decision"] == "denied" else "valid"
            lineage = source_state(self, project, [ref], evaluation=_lineage_evaluation)
            if lineage["blocked"]:
                health = "conflicted"
            evidence.append({"event_id": ref, "type": row["type"], "outcome": row["outcome"], "created": row["created"], "sequence": row["sequence"],
                             "health": health, "current_tool_success": health == "valid" and row["type"] == "PostToolUse"
                             and row["outcome"] == "succeeded"})
            if lineage["sources"] or lineage["blocked"]:
                evidence[-1]["candidate_lineage"] = lineage
        return evidence

    @staticmethod
    def _source_health(evidence):
        # 出现矛盾比单纯缺失更需要管理员复审；不据此自动撤销正文。
        states = {item["health"] for item in evidence}
        return next((value for value in ("conflicted", "denied", "missing") if value in states), "valid" if states else "missing")

    def _public(self, project, row, include_maturity=True):
        from .runtime import clean, metadata
        from .admin_publication import knowledge_tier
        body = clean(json.loads(row["body"]))
        sources = []
        combined = {}
        from .candidate_references import _Assessment
        lineage_evaluation = _Assessment()
        for source in self.db.execute("SELECT * FROM knowledge_sources WHERE project=? AND knowledge_id=? AND revision=? "
                                      "ORDER BY created,candidate_id", (project, row["id"], row["revision"])).fetchall():
            evidence = self._source_evidence(project, json.loads(source["evidence_refs"]), source["problem_id"],
                                             _lineage_evaluation=lineage_evaluation)
            problem = self.db.execute("SELECT revision FROM problems WHERE project=? AND id=?", (project, source["problem_id"])).fetchone()
            sources.append({"candidate_id": source["candidate_id"], "problem_id": source["problem_id"],
                            "problem_revision": source["problem_revision"], "problem_changed": not problem or problem[0] != source["problem_revision"],
                            "health": self._source_health(evidence), "current_tool_success": any(item["current_tool_success"] for item in evidence),
                            "evidence": evidence})
            for item in evidence:
                combined[item["event_id"]] = item
        if not sources:
            # 迁移无法恢复某个旧来源时仍可展示历史证据，但不会凭空生成 source。
            combined = {item["event_id"]: item for item in self._source_evidence(project, body["evidence_refs"], body.get("problem_id"),
                                                                               _lineage_evaluation=lineage_evaluation)}
        evidence = list(combined.values())
        source_version = self.db.execute("SELECT version FROM knowledge_source_versions WHERE project=? AND knowledge_id=? AND revision=?",
                                         (project, row["id"], row["revision"])).fetchone()
        result = {"knowledge_id": row["id"], "revision": row["revision"],
                **{k: body[k] for k in ("title", "content", "category", "applicability", "limitations", "validation_level")},
                "knowledge_kind": body.get("knowledge_kind", "solution"), "metadata": metadata(body.get("metadata")),
                "knowledge_tier": knowledge_tier(body),
                "sources": sources, "source_health": self._source_health(evidence),
                "current_tool_success": any(item["current_tool_success"] for item in evidence), "evidence": evidence,
                "source_version": source_version[0] if source_version else 0,
                "problem_changed": any(source["problem_changed"] for source in sources),
                **self._knowledge_feedback_state(project, row["id"], row["revision"])}
        from .source_caveats import summarize_sources
        result["source_caveat"] = summarize_sources(sources or [{"health": result["source_health"], "evidence": evidence}])
        if include_maturity:
            state = self.knowledge_state_snapshot(project, row["id"], row["revision"], _public_data=result)
            result.update(maturity=state["maturity"], lifecycle_version=state["lifecycle_version"])
        return result

    def read(self, project, knowledge_id):
        from .runtime import ContractError
        # 正文和来源读取共享快照，避免公共输出跨修订拼接。
        owned = not self.db.in_transaction
        if owned:
            self.db.execute("BEGIN")
        try:
            row = self.db.execute("SELECT k.* FROM knowledge k JOIN active a ON k.project=a.project AND k.id=a.id "
                                  "AND k.revision=a.revision WHERE k.project=? AND k.id=?", (project, knowledge_id)).fetchone()
            if not row:
                raise ContractError("knowledge is not currently published")
            result = self._public(project, row)
            if owned:
                self.db.execute("COMMIT")
            return result
        except BaseException:
            if owned:
                self.db.execute("ROLLBACK")
            raise

    def backup(self, destination):
        """按需使用在线备份接口；只清理本次独占创建的目标。"""
        from .runtime import ContractError
        if self.db.in_transaction:
            raise ContractError("backup must run outside a transaction")
        target = Path(destination).expanduser().resolve()
        source = (self.home / "runtime.sqlite3").resolve()
        reserved = {source, *(Path(str(source) + suffix) for suffix in ("-wal", "-shm", "-journal"))}
        if target in reserved or target.exists():
            raise ContractError("backup destination must be a new file outside the live database")
        target.parent.mkdir(parents=True, exist_ok=True)
        owned = False
        connection = None
        try:
            with target.open("xb"):
                owned = True
            connection = sqlite3.connect(target)
            self.db.backup(connection)
            connection.close()
            connection = None
        except BaseException:
            if connection is not None:
                connection.close()
            if owned:
                target.unlink(missing_ok=True)
            raise
        return {"status": "backed_up", "path": str(target)}

    def doctor(self):
        """人工维护时检查最小持久化约束，不加入普通任务热路径。"""
        from .runtime import clean, digest, knowledge_fingerprint
        from .knowledge_index import INDEX_VERSION, _index_text
        owned = not self.db.in_transaction
        if owned:
            self.db.execute("BEGIN")
        try:
            quick = [row[0] for row in self.db.execute("PRAGMA quick_check").fetchall()]
            active = self.db.execute("SELECT COUNT(*) FROM active a LEFT JOIN knowledge k ON a.project=k.project AND a.id=k.id "
                                     "AND a.revision=k.revision WHERE k.id IS NULL").fetchone()[0]
            fingerprints = 0
            for row in self.db.execute("SELECT k.*,f.fingerprint FROM knowledge k LEFT JOIN knowledge_fingerprints f "
                                       "ON k.project=f.project AND k.id=f.id AND k.revision=f.revision").fetchall():
                try:
                    fingerprints += int(row["fingerprint"] != knowledge_fingerprint(json.loads(row["body"])))
                except (ValueError, TypeError, KeyError):
                    fingerprints += 1
            fingerprints += self.db.execute("SELECT COUNT(*) FROM knowledge_fingerprints f LEFT JOIN knowledge k "
                                            "ON k.project=f.project AND k.id=f.id AND k.revision=f.revision WHERE k.id IS NULL").fetchone()[0]
            index = self.db.execute("SELECT COUNT(*) FROM knowledge_fts f LEFT JOIN active a ON f.project=a.project "
                                    "AND f.id=a.id AND CAST(f.revision AS INTEGER)=a.revision WHERE a.id IS NULL").fetchone()[0]
            index += self.db.execute("SELECT COUNT(*) FROM (SELECT a.project,a.id,a.revision,COUNT(f.id) n FROM active a "
                                     "LEFT JOIN knowledge_fts f ON a.project=f.project AND a.id=f.id "
                                     "AND a.revision=CAST(f.revision AS INTEGER) GROUP BY a.project,a.id,a.revision HAVING n<>1)").fetchone()[0]
            for row in self.db.execute("SELECT f.text,k.body FROM knowledge_fts f JOIN knowledge k ON f.project=k.project "
                                       "AND f.id=k.id AND CAST(f.revision AS INTEGER)=k.revision").fetchall():
                try:
                    index += int(row["text"] != _index_text(clean(json.loads(row["body"]))))
                except (ValueError, TypeError, AttributeError):
                    index += 1
            index_state = self.db.execute("SELECT version FROM knowledge_index_state WHERE id=1").fetchone()
            index += int(not index_state or index_state[0] != INDEX_VERSION)
            mapping_exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_index_entries'").fetchone()
            if mapping_exists:
                index += self.db.execute("SELECT COUNT(*) FROM knowledge_index_entries m LEFT JOIN knowledge_fts f "
                                         "ON f.rowid=m.fts_rowid LEFT JOIN active a ON m.project=a.project AND m.id=a.id "
                                         "AND m.revision=a.revision WHERE f.rowid IS NULL OR a.id IS NULL "
                                         "OR f.project<>m.project OR f.id<>m.id OR CAST(f.revision AS INTEGER)<>m.revision").fetchone()[0]
                index += self.db.execute("SELECT COUNT(*) FROM knowledge_fts f LEFT JOIN knowledge_index_entries m "
                                         "ON f.rowid=m.fts_rowid WHERE m.fts_rowid IS NULL").fetchone()[0]
            else:
                index += 1
            sources = self.db.execute("SELECT COUNT(*) FROM knowledge_sources s LEFT JOIN knowledge k "
                                      "ON s.project=k.project AND s.knowledge_id=k.id AND s.revision=k.revision "
                                      "LEFT JOIN candidates c ON s.project=c.project AND s.candidate_id=c.id "
                                      "LEFT JOIN problems p ON s.project=p.project AND s.problem_id=p.id "
                                      "WHERE k.id IS NULL OR c.id IS NULL OR p.id IS NULL").fetchone()[0]
            receipts = 0
            for receipt in self.db.execute("SELECT r.*,c.id existing FROM publication_receipts r LEFT JOIN candidates c "
                                           "ON r.project=c.project AND r.candidate_id=c.id").fetchall():
                try:
                    response = json.loads(receipt["response"])
                    if response.get("status") == "legacy_unresolved":
                        receipts += 1
                    elif not receipt["existing"] or not self.db.execute("SELECT 1 FROM knowledge WHERE project=? AND id=? AND revision=?",
                            (receipt["project"], response["knowledge_id"], response["revision"])).fetchone():
                        receipts += 1
                except (ValueError, KeyError, TypeError):
                    receipts += 1
            receipts += self.db.execute("SELECT COUNT(*) FROM candidates c LEFT JOIN publication_receipts r "
                                        "ON c.project=r.project AND c.id=r.candidate_id WHERE c.status='published' AND r.candidate_id IS NULL").fetchone()[0]
            identities = 0
            represented = set()
            for identity in self.db.execute("SELECT i.*,c.body FROM candidate_identity i LEFT JOIN candidates c "
                                            "ON i.project=c.project AND i.candidate_id=c.id").fetchall():
                try:
                    matches = identity["body"] is not None and identity["fingerprint"] == digest([identity["project"], json.loads(identity["body"])])
                except (ValueError, TypeError):
                    matches = False
                if matches:
                    represented.add((identity["project"], identity["fingerprint"]))
                else:
                    identities += 1
            expected_identities = set()
            for candidate in self.db.execute("SELECT project,body FROM candidates").fetchall():
                try:
                    expected_identities.add((candidate["project"], digest([candidate["project"], json.loads(candidate["body"])])))
                except (ValueError, TypeError):
                    identities += 1
            identities += len(expected_identities - represented)
            checks = {"active": active, "fingerprints": fingerprints, "index": index, "sources": sources,
                      "receipts": receipts, "candidate_identity": identities}
            result = {"status": "ok" if quick == ["ok"] and not any(checks.values()) else "issues", "quick_check": quick, "checks": checks}
            if owned:
                self.db.execute("COMMIT")
            return result
        except BaseException:
            if owned:
                self.db.execute("ROLLBACK")
            raise
