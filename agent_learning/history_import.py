"""显式、按次批准的历史吸收；不由 Hook 或普通管理员队列唤醒。"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import time
import uuid


ACCESS_STATES = {"materialized", "reviewing", "review_complete"}
_REVIEW_TERMINAL = {"candidate_ready", "proposal_ready", "skipped", "published"}


@contextmanager
def _read_snapshot(runtime):
    """Reuse an enclosing transaction; standalone progress reads share one view."""
    owned = not runtime.db.in_transaction
    if owned:
        runtime.db.execute("BEGIN")
    try:
        yield
        if owned:
            runtime.db.execute("COMMIT")
    except BaseException:
        if owned and runtime.db.in_transaction:
            runtime.db.execute("ROLLBACK")
        raise


def _review_queue(runtime, project, job_id):
    """Current-revision disposition; file order never depends on hashed unit IDs.

    A related source included in another packet remains unreviewed here. Only a
    decision/candidate for this exact problem revision settles its own unit.
    """
    rows = runtime.db.execute(
        "SELECT p.id AS problem_id,p.revision,s.position,u.unit_index,r.state AS review_state,"
        "r.revision AS review_revision,"
        "EXISTS(SELECT 1 FROM candidates c WHERE c.project=p.project AND c.problem=p.id "
        "AND c.problem_revision=p.revision) AS has_candidate,"
        "EXISTS(SELECT 1 FROM review_receipts d WHERE d.project=p.project AND d.problem=p.id "
        "AND d.revision=p.revision AND d.decision='needs_evidence') AS needs_evidence,"
        "(SELECT a.id FROM admin_calls a WHERE a.project=p.project AND a.problem=p.id "
        "AND a.revision=p.revision AND a.retry_allowed=0 AND a.state NOT IN ('not_started','deduplicated') "
        "AND NOT EXISTS(SELECT 1 FROM review_receipts d WHERE d.project=a.project AND d.call_id=a.id) "
        "ORDER BY a.created DESC,a.id DESC LIMIT 1) AS blocked_call_id "
        "FROM history_case_links l JOIN problems p ON p.project=l.project AND p.id=l.problem_id "
        "JOIN history_units u ON u.id=l.unit_id JOIN history_job_sources s "
        "ON s.job_id=l.job_id AND s.material_id=u.material_id "
        "LEFT JOIN review_states r ON r.project=p.project AND r.problem=p.id "
        "WHERE l.project=? AND l.job_id=? ORDER BY s.position,u.unit_index,p.id", (project, job_id)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        current = row["review_revision"] == row["revision"]
        if row["blocked_call_id"]:
            item["disposition"] = "blocked"
            call = runtime.db.execute("SELECT state FROM admin_calls WHERE id=?", (row["blocked_call_id"],)).fetchone()
            item["blocked_call_state"] = call[0] if call else "unknown"
        elif row["has_candidate"] or current and row["review_state"] in _REVIEW_TERMINAL:
            item["disposition"] = "completed"
        elif current and row["review_state"] == "awaiting_evidence" and row["needs_evidence"]:
            item["disposition"] = "awaiting_evidence"
        else:
            item["disposition"] = "ready"
        items.append(item)
    return items


def install_storage(runtime):
    from .runtime import ContractError
    if not runtime.db.in_transaction:
        raise ContractError("history migration requires an existing write transaction")
    statements = (
        "CREATE TABLE IF NOT EXISTS project_onboarding(project TEXT PRIMARY KEY,state TEXT NOT NULL,job_id TEXT,updated REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS history_jobs(id TEXT PRIMARY KEY,project TEXT NOT NULL,workspace TEXT NOT NULL,plan TEXT NOT NULL,signature TEXT NOT NULL,state TEXT NOT NULL,host_epoch INTEGER NOT NULL,review_enabled INTEGER NOT NULL DEFAULT 0,max_calls INTEGER NOT NULL DEFAULT 0,remaining_calls INTEGER NOT NULL DEFAULT 0,owner TEXT,lease_until REAL,created REAL NOT NULL,updated REAL NOT NULL,last_result TEXT NOT NULL DEFAULT '{}')",
        "CREATE INDEX IF NOT EXISTS history_jobs_project ON history_jobs(project,created)",
        "CREATE TABLE IF NOT EXISTS history_materials(id TEXT PRIMARY KEY,project TEXT NOT NULL,source_key TEXT NOT NULL,content_hash TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,UNIQUE(project,source_key,content_hash))",
        "CREATE TABLE IF NOT EXISTS history_job_sources(job_id TEXT NOT NULL,material_id TEXT NOT NULL,position INTEGER NOT NULL,materialized INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(job_id,material_id),FOREIGN KEY(job_id) REFERENCES history_jobs(id),FOREIGN KEY(material_id) REFERENCES history_materials(id))",
        "CREATE TABLE IF NOT EXISTS history_units(id TEXT PRIMARY KEY,project TEXT NOT NULL,material_id TEXT NOT NULL,unit_index INTEGER NOT NULL,body TEXT NOT NULL,problem_id TEXT,UNIQUE(project,material_id,unit_index),FOREIGN KEY(material_id) REFERENCES history_materials(id))",
        "CREATE TABLE IF NOT EXISTS history_case_links(project TEXT NOT NULL,problem_id TEXT NOT NULL,job_id TEXT NOT NULL,unit_id TEXT NOT NULL,PRIMARY KEY(project,problem_id,job_id),FOREIGN KEY(job_id) REFERENCES history_jobs(id),FOREIGN KEY(unit_id) REFERENCES history_units(id))",
        "CREATE INDEX IF NOT EXISTS history_cases_job ON history_case_links(project,job_id,problem_id)",
        "CREATE TABLE IF NOT EXISTS history_call_links(job_id TEXT NOT NULL,call_id TEXT NOT NULL UNIQUE,refunded INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(job_id,call_id),FOREIGN KEY(job_id) REFERENCES history_jobs(id))",
    )
    for statement in statements:
        runtime.db.execute(statement)


def _job(runtime, project, job_id):
    from .runtime import ContractError
    row = runtime.db.execute("SELECT * FROM history_jobs WHERE project=? AND id=?", (project, job_id)).fetchone()
    if row is None:
        raise ContractError("unknown history job")
    return dict(row)


def _summary(runtime, row):
    owned = not runtime.db.in_transaction
    with _read_snapshot(runtime):
        if owned:
            # Direct callers may have fetched job metadata before this snapshot.
            # Refresh it inside the view used by the counts and dispositions.
            row = _job(runtime, row["project"], row["id"])
        return _summary_snapshot(runtime, row)


def _summaries(runtime, rows):
    from .history_progress import progress_snapshots
    progress = progress_snapshots(runtime, rows)
    return [_summary_snapshot(runtime, row, progress[row["id"]]) for row in rows]


def _summary_snapshot(runtime, row, progress=None):
    from .history_progress import progress_snapshots
    plan = json.loads(row["plan"])
    progress = progress or progress_snapshots(runtime, [row])[row["id"]]
    counts = progress["counts"]
    dispositions = progress["dispositions"]
    selected_complete = counts["materialized_sources"] == counts["sources"]
    review_complete = selected_complete and dispositions["completed"] == progress["queue_total"]
    blocked = progress["blocked"]
    candidate_states = progress["candidate_states"]
    active = bool(row["owner"] and (row["lease_until"] or 0) > time.time())
    last_result = json.loads(row["last_result"])
    auto_reference = bool(last_result.get("auto_reference_authorized", False))
    result = {"job_id": row["id"], "state": row["state"], "workspace": row["workspace"], "counts": counts,
            "coverage": {"incomplete": bool(plan.get("incomplete")), "stats": plan.get("stats", {}),
                         "skipped_count": plan.get("skipped_count", 0), "pending_count": plan.get("pending_count", 0),
                         "archive": {"selected_sources": counts["sources"], "materialized_sources": counts["materialized_sources"],
                                     "selected_sources_complete": selected_complete, "scan_incomplete": bool(plan.get("incomplete")),
                                     "project_scan_complete": not bool(plan.get("incomplete"))},
                         "review": {"scope": "materialized_units", "total_cases": progress["queue_total"], "completed_cases": dispositions["completed"],
                                    "ready_cases": dispositions["ready"], "awaiting_evidence_cases": dispositions["awaiting_evidence"],
                                    "blocked_cases": dispositions["blocked"], "complete": review_complete},
                         "publication": {"candidates": counts["candidates"], "published_candidates": counts["published_candidates"],
                                         "awaiting_approval": candidate_states.get("candidate", 0) + candidate_states.get("deferred", 0),
                                         "rejected_candidates": candidate_states.get("rejected", 0), "automatic": auto_reference}},
            "remaining_calls": row["remaining_calls"], "review_enabled": bool(row["review_enabled"]),
            "created": row["created"], "updated": row["updated"], "last_result": json.loads(row["last_result"]),
            "automatic_resume": False, "publication_automatic": auto_reference,
            "run_active": active,
            "resume_required": row["state"] == "paused" or row["state"] in {"materializing", "reviewing"} and (row["lease_until"] or 0) <= time.time(),
            "resume": {"automatic": False, "explicit_run_required": row["state"] != "cancelled" and not active
                       and (not selected_complete or dispositions["ready"] > 0),
                       "review_ready_cases": dispositions["ready"], "awaiting_evidence_cases": dispositions["awaiting_evidence"],
                       "blocked_cases": dispositions["blocked"],
                       "requires_call_retry": any(item["state"] not in {"reserved", "launch_intent", "started"} for item in blocked),
                       "wait_for_active_calls": any(item["state"] in {"reserved", "launch_intent", "started"} for item in blocked),
                       "blocked_calls": blocked[:20], "blocked_calls_truncated": len(blocked) > 20},
            "epoch_current": row["host_epoch"] == runtime.host_epoch()}
    from .history_workflow import workflow_state
    result["workflow"] = workflow_state(row, result)
    result["changes"] = plan.get("changes")
    return result


def _unit_groups(source):
    """相邻片段组合成有限审核单元；单元身份不随执行预算变化。"""
    from .history_sources import safe_segment_end
    # 固定分组使已归档资料在配置变化后仍可复用；预算由审核包另行校验。
    limit = 4000
    chunks = source.get("chunks") or [{"index": 0, "text": source.get("content", ""), "locator": {}}]
    group, size = [], 0
    for chunk in chunks:
        text = chunk.get("text", "")
        if not isinstance(text, str) or not text.strip():
            continue
        offset, part = 0, 0
        while offset < len(text):
            end = safe_segment_end(text, offset, 1500)
            locator = dict(chunk.get("locator", {}), chunk_index=chunk.get("index"), char_offset=offset,
                           char_length=end - offset, char_scope="source_chunk")
            segment = dict(chunk, text=text[offset:end], part=part, locator=locator)
            if group and (size + len(segment["text"]) > limit or len(group) >= 8):
                yield group
                group, size = [], 0
            group.append(segment)
            size += len(segment["text"])
            offset, part = end, part + 1
    if group:
        yield group


class HistoryImportMixin:
    def history_prepare(self, project, workspace, extra_sources=None, scan_options=None, expected_epoch=None, mode="continue"):
        from .history_sources import scan_sources
        from .history_workflow import MODES, existing_work, head
        from .runtime import ContractError, clean, digest, encode
        if not isinstance(mode, str) or mode not in MODES:
            raise ContractError("invalid history preparation mode")
        epoch = self.assert_host_epoch(expected_epoch)
        workspace = str(Path(workspace).absolute())
        if self.project(workspace) != project:
            raise ContractError("history workspace belongs to another project")
        with self.transaction():
            self.assert_host_epoch(epoch)
            pending = existing_work(self, project, mode)
            if pending:
                return pending
            previous = head(self, project)
        if mode == "remaining":
            if not previous:
                raise ContractError("no previous history scan to continue")
            old_plan = json.loads(previous["plan"])
            # Only the original roots and selection are eligible. Budget-skipped
            # paths are prioritized, without treating unsupported files as work.
            if extra_sources is not None or scan_options is not None:
                raise ContractError("remaining scan reuses its frozen source selection")
            extra_sources = old_plan.get("extra_sources", [])
            scan_options = old_plan["options"]
            prioritize = [v["path"] for v in old_plan.get("pending", [])
                          if isinstance(v, dict) and v.get("reason") in {"budget_total_bytes", "budget_files", "budget_chunks"}]
            if not prioritize:
                raise ContractError("no resumable file budget omissions; select an explicit scope or adjust scan limits")
        else:
            prioritize = None
        snapshot = scan_sources(workspace, extra_sources=extra_sources, options=scan_options, prioritize_paths=prioritize,
                                only_paths=prioritize if mode == "remaining" else None)
        snapshot = clean(snapshot)
        workspace = snapshot["workspace"]
        sources = snapshot.get("sources", [])
        plan = {key: snapshot.get(key) for key in ("workspace", "options", "stats", "incomplete")}
        plan.update(extra_sources=clean(extra_sources or []), skipped_count=len(snapshot.get("skipped", [])),
                    pending_count=len(snapshot.get("pending", [])), skipped=snapshot.get("skipped", []),
                    pending=snapshot.get("pending", []))
        with self.transaction():
            self.assert_host_epoch(epoch)
            pending = existing_work(self, project, mode)
            if pending:
                return pending
            # Source identity is exact; similarity never carries a decision.
            known = {(r["source_key"], r["content_hash"]) for r in self.db.execute(
                "SELECT m.source_key,m.content_hash FROM history_materials m WHERE m.project=? AND EXISTS("
                "SELECT 1 FROM history_job_sources s WHERE s.material_id=m.id AND s.materialized=1)", (project,))}
            all_sources = sources
            changed = [s for s in sources if (s["source_key"], s["content_hash"]) not in known]
            changes = {"selected_sources": len(sources), "new_or_changed_sources": len(changed),
                       "reused_sources": len(sources) - len(changed), "mode": mode}
            current_head = head(self, project)
            if not changed and current_head and mode != "full":
                return dict(_summary(self, current_head), reused=True, preparation={"action": "no_changes", "scanned": True,
                            "models_started": False}, changes=changes)
            if known and mode != "full":
                sources = changed
                changes["context_only_sources"] = len(all_sources) - len(changed)
            plan["changes"] = changes
            signature = digest([project, workspace, plan, [(s["source_key"], s["content_hash"]) for s in sources]])
            existing = self.db.execute("SELECT * FROM history_jobs WHERE project=? AND signature=? AND state!='cancelled' ORDER BY created DESC LIMIT 1",
                                       (project, signature)).fetchone()
            if existing:
                return dict(_summary(self, existing), reused=True)
            ident, now = uuid.uuid4().hex, time.time()
            self.db.execute("INSERT INTO history_jobs(id,project,workspace,plan,signature,state,host_epoch,created,updated) VALUES(?,?,?,?,?,'prepared',?,?,?)",
                            (ident, project, workspace, encode(plan), signature, epoch, now, now))
            selected_keys = {(s["source_key"], s["content_hash"]) for s in sources}
            for position, source in enumerate(all_sources):
                material_id = digest([project, source["source_key"], source["content_hash"]])
                self.db.execute("INSERT OR IGNORE INTO history_materials VALUES(?,?,?,?,?,?)",
                                (material_id, project, source["source_key"], source["content_hash"], encode(source), now))
                context_only = (source["source_key"], source["content_hash"]) not in selected_keys
                self.db.execute("INSERT INTO history_job_sources(job_id,material_id,position,materialized) VALUES(?,?,?,?)",
                                (ident, material_id, position, int(context_only)))
            self.db.execute("INSERT INTO project_onboarding VALUES(?,'prepared',?,?) ON CONFLICT(project) DO UPDATE SET state='prepared',job_id=excluded.job_id,updated=excluded.updated",
                            (project, ident, now))
            return dict(_summary(self, _job(self, project, ident)), preparation={"action": "created", "scanned": True,
                        "models_started": False})

    def history_defer(self, project, job_id, reason, expected_epoch=None):
        from .history_workflow import defer_history
        return defer_history(self, project, job_id, reason, expected_epoch)

    def history_archive(self, project, workspace, *, extra_sources=None, scan_options=None, expected_epoch=None,
                        mode="continue", max_seconds=180):
        prepared = self.history_prepare(project, workspace, extra_sources=extra_sources, scan_options=scan_options,
                                        expected_epoch=expected_epoch, mode=mode)
        preparation = prepared.get("preparation") or {}
        may_materialize = (preparation.get("action") == "created" or
            preparation.get("next_action") == "continue_archive" and prepared["epoch_current"]
            and prepared["state"] != "paused")
        status = (self.history_run(project, prepared["job_id"], allow_review=False, max_calls=0,
                                  max_seconds=max_seconds, expected_epoch=expected_epoch) if may_materialize
                  else self.history_status(project, prepared["job_id"]))
        return dict(status, prepared_job_id=prepared["job_id"], reused_preparation=bool(prepared.get("reused")),
                    preparation=preparation, changes=prepared.get("changes"))

    def history_status(self, project, job_id=None):
        with _read_snapshot(self):
            row = self.db.execute("SELECT * FROM project_onboarding WHERE project=?", (project,)).fetchone()
            onboarding = {"state": row["state"], "job_id": row["job_id"]} if row else {"state": "unoffered", "job_id": None}
            from .history_workflow import head
            selected = _job(self, project, job_id) if job_id else head(self, project)
            return {"project_id": project, "onboarding": onboarding, "job": _summary(self, selected) if selected else None,
                    "manual_only": True, "automatic_resume": False}

    def history_jobs(self, project, limit=20):
        from .runtime import ContractError
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ContractError("history job limit must be 1..100")
        with _read_snapshot(self):
            rows = self.db.execute("SELECT * FROM history_jobs WHERE project=? ORDER BY created DESC,id LIMIT ?", (project, limit)).fetchall()
            return {"items": _summaries(self, rows), "limit": limit,
                    "total": self.db.execute("SELECT COUNT(*) FROM history_jobs WHERE project=?", (project,)).fetchone()[0]}

    def history_source_report(self, project, job_id, section="sources", offset=0, limit=20):
        from .runtime import ContractError
        if section not in {"sources", "pending", "skipped"}:
            raise ContractError("history source section must be sources, pending or skipped")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ContractError("history source offset must be nonnegative")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ContractError("history source limit must be 1..100")
        job = _job(self, project, job_id)
        if section == "sources":
            total = self.db.execute("SELECT COUNT(*) FROM history_job_sources WHERE job_id=?", (job_id,)).fetchone()[0]
            rows = self.db.execute("SELECT m.id,m.body,s.materialized FROM history_job_sources s JOIN history_materials m ON m.id=s.material_id WHERE s.job_id=? ORDER BY s.position LIMIT ? OFFSET ?", (job_id, limit, offset)).fetchall()
            items = []
            for row in rows:
                source = json.loads(row["body"])
                item = {key: source.get(key) for key in ("source_key", "path", "relative_path", "root", "kind", "coverage", "content_hash", "size_bytes", "parser_version", "metadata")}
                item.update(material_id=row["id"], materialized=bool(row["materialized"]),
                            content_chars=len(source.get("content", "")), chunks=len(source.get("chunks", [])))
                items.append(item)
        else:
            records = json.loads(job["plan"]).get(section, [])
            total, items = len(records), records[offset:offset + limit]
        return {"job_id": job_id, "section": section, "items": items, "offset": offset, "limit": limit,
                "total": total, "next_offset": offset + len(items) if offset + len(items) < total else None}

    def history_cases(self, project, job_id, offset=0, limit=20):
        """按具体任务定位案例；暂停任务可查 ID，但此入口不授予读取审核包的权限。"""
        from .runtime import ContractError
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ContractError("history case offset must be nonnegative")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ContractError("history case limit must be 1..100")
        owned = not self.db.in_transaction
        if owned:
            self.db.execute("BEGIN")
        try:
            job = _job(self, project, job_id)
            total = self.db.execute("SELECT COUNT(*) FROM history_case_links WHERE project=? AND job_id=?", (project, job_id)).fetchone()[0]
            rows = self.db.execute(
                "SELECT p.id,p.revision,p.status,p.summary,u.id AS unit_id,u.unit_index,u.material_id "
                "FROM history_case_links l JOIN problems p ON p.project=l.project AND p.id=l.problem_id "
                "JOIN history_units u ON u.id=l.unit_id JOIN history_job_sources s ON s.job_id=l.job_id AND s.material_id=u.material_id "
                "WHERE l.project=? AND l.job_id=? ORDER BY s.position,u.unit_index,p.id LIMIT ? OFFSET ?",
                (project, job_id, limit, offset)).fetchall()
            items = []
            for row in rows:
                review = self.db.execute("SELECT state,reason,revision FROM review_states WHERE project=? AND problem=?", (project, row["id"])).fetchone()
                candidates = self.db.execute("SELECT id,status,json_extract(body,'$.title') AS title,json_extract(body,'$.validation_level') AS validation_level "
                                             "FROM candidates WHERE project=? AND problem=? ORDER BY created DESC,id LIMIT 3", (project, row["id"])).fetchall()
                candidate_count = self.db.execute("SELECT COUNT(*) FROM candidates WHERE project=? AND problem=?", (project, row["id"])).fetchone()[0]
                try:
                    self.history_guard(project, row["id"], job_id)
                    allowed = True
                except ContractError:
                    allowed = False
                items.append({"problem_id": row["id"], "revision": row["revision"], "status": row["status"],
                              "summary": row["summary"][:240], "unit_id": row["unit_id"], "unit_index": row["unit_index"],
                              "material_id": row["material_id"], "review_allowed": allowed,
                              "review": {"state": review["state"], "revision": review["revision"], "reason": review["reason"][:240]} if review else None,
                              "candidate_count": candidate_count,
                              "candidates": [{"candidate_id": candidate["id"], "status": candidate["status"], "title": (candidate["title"] or "")[:160],
                                              "validation_level": candidate["validation_level"]} for candidate in candidates]})
            result = {"job_id": job_id, "job_state": job["state"], "job_updated": job["updated"], "items": items,
                      "offset": offset, "limit": limit, "total": total,
                      "next_offset": offset + len(items) if offset + len(items) < total else None}
            if owned:
                self.db.execute("COMMIT")
            return result
        except BaseException:
            if owned and self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def history_skip(self, project):
        with self.transaction():
            self.db.execute("INSERT INTO project_onboarding VALUES(?,'skipped',NULL,?) ON CONFLICT(project) DO UPDATE SET state='skipped',updated=excluded.updated", (project, time.time()))
        return self.history_status(project)

    def history_cancel(self, project, job_id, expected_epoch=None):
        with self.transaction():
            self.assert_host_epoch(expected_epoch)
            _job(self, project, job_id)
            self.db.execute("UPDATE history_jobs SET state='cancelled',owner=NULL,lease_until=NULL,review_enabled=0,updated=? WHERE project=? AND id=?",
                            (time.time(), project, job_id))
        return self.history_status(project, job_id)

    def history_pause(self, project, job_id, expected_epoch=None):
        from .runtime import ContractError, encode
        with self.transaction():
            self.assert_host_epoch(expected_epoch)
            row = _job(self, project, job_id)
            if row["state"] == "cancelled":
                raise ContractError("cancelled history job cannot be paused")
            self.db.execute("UPDATE history_jobs SET state='paused',owner=NULL,lease_until=NULL,review_enabled=0,updated=?,last_result=? WHERE project=? AND id=?",
                            (time.time(), encode({"reason": "user_paused"}), project, job_id))
        return self.history_status(project, job_id)

    def history_guard(self, project, problem_id, history_job_id=None, *, require_review=False, expected_epoch=None):
        from .runtime import ContractError
        links = self.db.execute("SELECT j.* FROM history_case_links l JOIN history_jobs j ON j.id=l.job_id WHERE l.project=? AND l.problem_id=? ORDER BY j.created DESC",
                                (project, problem_id)).fetchall()
        if not links:
            if history_job_id is not None:
                raise ContractError("case is outside the approved history job")
            return None
        epoch = self.assert_host_epoch(expected_epoch)
        rows = [row for row in links if history_job_id is None or row["id"] == history_job_id]
        for row in rows:
            if row["state"] not in ACCESS_STATES or row["host_epoch"] != epoch:
                continue
            if row["state"] == "reviewing" and (not row["owner"] or (row["lease_until"] or 0) <= time.time()):
                continue
            if require_review and (history_job_id is None or row["state"] != "reviewing" or not row["review_enabled"]
                                   or not row["owner"] or (row["lease_until"] or 0) <= time.time()):
                continue
            return dict(row)
        raise ContractError("history job is not currently approved; explicitly run or resume it")

    def history_reserve_call(self, project, job_id, problem_id, call_id, expected_epoch):
        from .runtime import ContractError
        if not self.db.in_transaction:
            raise ContractError("history call reservation requires a write transaction")
        row = self.history_guard(project, problem_id, job_id, require_review=True, expected_epoch=expected_epoch)
        if row["remaining_calls"] <= 0:
            raise ContractError("history job call budget exhausted")
        self.db.execute("UPDATE history_jobs SET remaining_calls=remaining_calls-1,updated=? WHERE project=? AND id=?", (time.time(), project, job_id))
        self.db.execute("INSERT INTO history_call_links(job_id,call_id) VALUES(?,?)", (job_id, call_id))

    def history_refund_call(self, job_id, call_id):
        with self._case_transaction():
            call = self.db.execute("SELECT state FROM admin_calls WHERE id=?", (call_id,)).fetchone()
            if call and call[0] == "not_started":
                changed = self.db.execute("UPDATE history_call_links SET refunded=1 WHERE job_id=? AND call_id=? AND refunded=0", (job_id, call_id)).rowcount
                if changed:
                    self.db.execute("UPDATE history_jobs SET remaining_calls=MIN(max_calls,remaining_calls+1) WHERE id=?", (job_id,))

    def _history_materialize(self, project, job_id, owner, epoch, deadline):
        from .runtime import ContractError, digest, encode
        rows = self.db.execute("SELECT m.* FROM history_materials m JOIN history_job_sources s ON s.material_id=m.id WHERE s.job_id=? AND s.materialized=0 ORDER BY s.position", (job_id,)).fetchall()
        for material in rows:
            if time.monotonic() >= deadline:
                return False
            source = json.loads(material["body"])
            for index, group in enumerate(_unit_groups(source)):
                if time.monotonic() >= deadline:
                    return False
                with self.transaction():
                    self.assert_host_epoch(epoch)
                    job = _job(self, project, job_id)
                    if job["owner"] != owner or job["state"] != "materializing" or (job["lease_until"] or 0) <= time.time():
                        raise ContractError("history run ownership changed")
                    unit_id = digest([project, material["id"], index])
                    previous = self.db.execute("SELECT problem_id FROM history_units WHERE id=?", (unit_id,)).fetchone()
                    if previous and previous[0]:
                        problem_id = previous[0]
                    else:
                        conversation = "history:" + material["id"]
                        refs = []
                        for part, chunk in enumerate(group):
                            payload = {"source": {"source_key": source["source_key"], "path": source["path"], "content_hash": source["content_hash"],
                                                  "kind": source["kind"], "coverage": source.get("coverage", "unknown"), "parser_version": source.get("parser_version")},
                                       "locator": chunk.get("locator", {}), "content": chunk["text"], "historical_statement": True,
                                       "unit_index": index, "content_is_excerpt": True,
                                       "limitations": "Historical public text; not an observed tool success or current execution authorization."}
                            if source.get("coverage") != "full":
                                payload["limitations"] += " Source coverage is partial or unknown; only the supplied public text is available."
                            event, _ = self._event(project, conversation, "HistoryImport", payload,
                                                   event_id=["history", unit_id, part], outcome="reported")
                            refs.append(event["id"])
                        excerpt = " ".join(" ".join(chunk["text"].split()) for chunk in group)[:400]
                        problem = self._problem(project, conversation,
                                                "Historical source " + source.get("relative_path", source["path"]) + " [" + source["content_hash"][:12] + "] unit " + str(index + 1) + ": " + excerpt,
                                                "project", refs)
                        problem_id = problem["id"]
                        self.db.execute("INSERT INTO history_units VALUES(?,?,?,?,?,?)", (unit_id, project, material["id"], index, encode({"parts": len(group)}), problem_id))
                    self.db.execute("INSERT OR IGNORE INTO history_case_links VALUES(?,?,?,?)", (project, problem_id, job_id, unit_id))
            with self.transaction():
                self.assert_host_epoch(epoch)
                job = _job(self, project, job_id)
                if job["owner"] != owner or job["state"] != "materializing":
                    raise ContractError("history run ownership changed")
                self.db.execute("UPDATE history_job_sources SET materialized=1 WHERE job_id=? AND material_id=?", (job_id, material["id"]))
        return True

    def history_run(self, project, job_id, allow_review=False, max_calls=10, max_seconds=600, expected_epoch=None,
                    *, auto_reference=False):
        from .admin import admin_once
        from .runtime import ContractError, encode
        if not isinstance(allow_review, bool):
            raise ContractError("allow_review must be a boolean")
        if not isinstance(auto_reference, bool) or auto_reference and not allow_review:
            raise ContractError("automatic history references require explicit model review")
        if auto_reference:
            from .admin_review_flow import publication_policy
            policy = publication_policy(self, project)
            if not policy["enabled"] or policy["mode"] != "tiered":
                raise ContractError("automatic history references require the tiered publication policy")
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or not 0 <= max_calls <= 20:
            raise ContractError("history max_calls must be 0..20")
        if isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) or not 1 <= max_seconds <= 600:
            raise ContractError("history max_seconds must be 1..600")
        epoch, owner = self.assert_host_epoch(expected_epoch), uuid.uuid4().hex
        deadline = time.monotonic() + max_seconds
        with self.transaction():
            self.assert_host_epoch(epoch)
            row = _job(self, project, job_id)
            if row["state"] == "cancelled":
                raise ContractError("cancelled history job requires a new explicit prepare")
            if row["owner"] and (row["lease_until"] or 0) > time.time():
                raise ContractError("history job already has an active run")
            from .history_workflow import assert_run_head
            assert_run_head(self, project, job_id)
            self.db.execute("UPDATE history_jobs SET state='materializing',host_epoch=?,review_enabled=?,max_calls=?,remaining_calls=?,owner=?,lease_until=?,updated=?,last_result=? WHERE project=? AND id=?",
                            (epoch, int(allow_review), max_calls, max_calls, owner, time.time() + max_seconds + 30, time.time(),
                             encode({"auto_reference_authorized": auto_reference}), project, job_id))
        results, state, reason = [], "materialized", "manual_review_available"
        run_queue = None
        try:
            if not self._history_materialize(project, job_id, owner, epoch, deadline):
                state, reason = "paused", "materialization_time_budget"
            elif allow_review:
                with self.transaction():
                    self.assert_host_epoch(epoch)
                    row = _job(self, project, job_id)
                    if row["owner"] != owner or row["state"] != "materializing":
                        raise ContractError("history run ownership changed")
                    self.db.execute("UPDATE history_jobs SET state='reviewing' WHERE id=?", (job_id,))
                from .admin_lifecycle import recover_calls
                # Recover abandoned reservations without launching or retrying any
                # model. Unknown calls stay isolated while untouched work proceeds.
                recover_calls(self, project)
                queued = _review_queue(self, project, job_id)
                run_queue = {key: sum(item["disposition"] == key for item in queued)
                             for key in ("completed", "ready", "awaiting_evidence", "blocked")}
                problems = [item for item in queued if item["disposition"] == "ready"]
                state, reason = "review_complete", "bounded_review_complete"
                for problem in problems:
                    if time.monotonic() >= deadline or _job(self, project, job_id)["remaining_calls"] <= 0:
                        state, reason = "paused", "review_budget_exhausted"
                        break
                    result = admin_once(self, project, problem["problem_id"], expected_epoch=epoch, _deadline=deadline,
                                        _history_job_id=job_id, _history_auto_reference=auto_reference)
                    results.append({"problem_id": problem["problem_id"], **{key: result[key] for key in
                                   ("status", "call_id", "candidate_id", "candidate_ids", "candidate_count", "proposal_id", "proposal_ids", "reused", "publication", "publications") if key in result}})
                    if result["status"] in {"failed", "timeout_unknown", "account_changed_unknown", "orphaned_unknown", "review_required", "waiting_for_review",
                                             "budget_exhausted", "token_budget_exhausted", "concurrency_limited", "not_started", "batch_time_exhausted",
                                             "executor_not_ready", "disabled"}:
                        state, reason = "paused", result["status"]
                        break
                if state == "review_complete":
                    review = _summary(self, _job(self, project, job_id))["coverage"]["review"]
                    if review["blocked_cases"]:
                        state, reason = "paused", "review_required"
                    elif review["awaiting_evidence_cases"]:
                        state, reason = "materialized", "needs_evidence"
                    elif not review["complete"]:
                        state, reason = "materialized", "pending_manual_review"
        except BaseException:
            with self.transaction():
                self.db.execute("UPDATE history_jobs SET state='paused',owner=NULL,lease_until=NULL,review_enabled=0,updated=?,last_result=? WHERE project=? AND id=? AND owner=?",
                                (time.time(), encode({"reason": "run_failed"}), project, job_id, owner))
            raise
        with self.transaction():
            # Cancel/epoch changes can race with a completed model; never overwrite the fence.
            if self.host_epoch() != epoch:
                state, reason = "paused", "host_epoch_changed"
            last_result = {"reason": reason, "reviews": results[:20], "reviews_count": len(results),
                           "auto_reference_authorized": auto_reference,
                           "reviews_truncated": len(results) > 20}
            if run_queue is not None:
                last_result["queue_at_start"] = run_queue
            self.db.execute("UPDATE history_jobs SET state=?,owner=NULL,lease_until=NULL,review_enabled=0,updated=?,last_result=? WHERE project=? AND id=? AND owner=?",
                            (state, time.time(), encode(last_result), project, job_id, owner))
            self.db.execute("UPDATE project_onboarding SET state='started',updated=? WHERE project=?", (time.time(), project))
        return self.history_status(project, job_id)
