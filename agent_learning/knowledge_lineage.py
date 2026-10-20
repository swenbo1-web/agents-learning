"""Bounded candidate provenance shared by publication and formal consumption.

Possible influence is an observation about sources, not proof of adoption. A
blocked public retelling cannot become independent evidence by being approved;
unrelated tool observations have no candidate lineage and remain separate.
"""
from __future__ import annotations

def source_state(view, project, refs, *, evaluation=None, ancestors=()):
    from .candidate_references import _Assessment, _lineage_risk, MAX_EVIDENCE
    if not view.db.execute("SELECT 1 FROM sqlite_master WHERE name='candidate_reference_lineage'").fetchone():
        return {"blocked": False, "sources": []}
    evaluation = evaluation if evaluation is not None else _Assessment()
    identities = list(dict.fromkeys(refs))
    if not identities:
        return {"blocked": False, "reason": None, "sources": []}
    # Unrelated observations do not consume the candidate graph budget. Older
    # legitimate candidates can cite more than 64 ordinary source events.
    marks = ",".join("?" for _ in identities)
    linked = view.db.execute(f"SELECT DISTINCT event_id FROM candidate_reference_lineage WHERE project=? "
                            f"AND event_id IN ({marks}) LIMIT ?", (project, *identities, MAX_EVIDENCE + 1)).fetchall()
    if len(linked) > MAX_EVIDENCE:
        return {"blocked": True, "reason": "candidate_lineage_budget_exceeded", "sources": []}
    linked = {row[0] for row in linked}
    sources = []
    for identity in identities:
        if identity not in linked:
            continue
        risk = _lineage_risk(view, project, identity, ancestors, evaluation)
        if risk:
            sources.append({"event_id": identity, **risk})
        if evaluation.exhausted:
            break
    blocked = evaluation.exhausted or any(item["blocked"] for item in sources)
    return {"blocked": blocked, "reason": "candidate_lineage_budget_exceeded" if evaluation.exhausted else
            "candidate_reference_source_blocked" if blocked else None, "sources": sources}


def published_reference_state(view, project, redirect, evaluation, ancestors):
    """Do not mistake an active redirect for healthy formal evidence.

    SQL checks ordinary authority without materializing every source event.
    Only actual candidate dependencies consume the shared traversal budget;
    accumulating independent sources must not turn healthy evidence invalid.
    """
    if not redirect or not redirect.get("active"):
        return {"blocked": True, "reason": "published_reference_inactive"}
    identity, revision = redirect["knowledge_id"], redirect["revision"]
    if view.db.execute("SELECT 1 FROM knowledge_feedback WHERE project=? AND knowledge_id=? AND revision=? "
            "AND kind='counterexample' AND disposition='unresolved' LIMIT 1", (project, identity, revision)).fetchone():
        return {"blocked": True, "reason": "published_reference_disputed"}
    key = (project, identity, revision)
    sources = "FROM knowledge_sources s WHERE s.project=? AND s.knowledge_id=? AND s.revision=?"
    if not view.db.execute("SELECT 1 " + sources + " LIMIT 1", key).fetchone():
        return {"blocked": True, "reason": "published_reference_source_unavailable"}
    if view.db.execute("SELECT 1 " + sources + " AND NOT EXISTS (SELECT 1 FROM problems p "
            "WHERE p.project=s.project AND p.id=s.problem_id AND p.revision=s.problem_revision) LIMIT 1", key).fetchone():
        return {"blocked": True, "reason": "published_reference_source_changed"}
    if view.db.execute("SELECT 1 " + sources + " AND CASE WHEN json_valid(s.evidence_refs) "
            "THEN json_type(s.evidence_refs) IS NOT 'array' OR json_array_length(s.evidence_refs)=0 "
            "ELSE 1 END LIMIT 1", key).fetchone():
        return {"blocked": True, "reason": "published_reference_source_unavailable"}
    references = "json_each(CASE WHEN json_valid(s.evidence_refs) THEN s.evidence_refs ELSE '[]' END) ref "
    authority = ("FROM knowledge_sources s JOIN " + references +
        "LEFT JOIN problem_events pe ON pe.problem=s.problem_id AND pe.event=ref.value "
        "LEFT JOIN events e ON e.project=s.project AND e.id=pe.event "
        "LEFT JOIN operations o ON o.project=e.project AND o.id=e.operation "
        "WHERE s.project=? AND s.knowledge_id=? AND s.revision=?")
    if view.db.execute("SELECT 1 " + authority + " AND (ref.type!='text' OR e.id IS NULL OR o.conflict "
            "OR o.decision='denied' OR e.outcome IN ('failed','conflict','denied')) LIMIT 1", key).fetchone():
        return {"blocked": True, "reason": "published_reference_source_unhealthy"}
    # The graph bound applies to linked observations, not the number of normal
    # source rows. DISTINCT also avoids charging repeated citations twice.
    from .candidate_references import MAX_EVIDENCE
    linked = view.db.execute("SELECT DISTINCT ref.value FROM knowledge_sources s JOIN " + references +
        "JOIN candidate_reference_lineage l "
        "ON l.project=s.project AND l.event_id=ref.value "
        "WHERE s.project=? AND s.knowledge_id=? AND s.revision=? LIMIT ?",
        (*key, MAX_EVIDENCE + 1)).fetchall()
    state = source_state(view, project, [row[0] for row in linked], evaluation=evaluation, ancestors=ancestors)
    if state["blocked"]:
        return state
    version = view.db.execute("SELECT version FROM knowledge_source_versions WHERE project=? AND knowledge_id=? AND revision=?",
                              (project, identity, revision)).fetchone()
    return {"blocked": False, "reason": None, "source_version": version[0] if version else 0}


def _affected(project, seed, include_seed=False):
    # The reverse traversal has a hard node limit. An oversized dependency graph
    # dirties this project's formal entries conservatively, never silently drops
    # the unseen tail or follows another project's IDs.
    first = "SELECT id FROM origins" if include_seed else (
        "SELECT d.downstream FROM candidate_lineage_dependencies d JOIN origins root ON root.id=d.upstream "
        f"WHERE d.project={project}")
    return f"""WITH RECURSIVE origins(id) AS ({seed}), downstream(id) AS (
        {first} UNION SELECT d.downstream FROM candidate_lineage_dependencies d
        JOIN downstream parent ON parent.id=d.upstream WHERE d.project={project} LIMIT 513
    ) SELECT DISTINCT s.project,s.knowledge_id,s.revision FROM knowledge_sources s
      WHERE s.project={project} AND (s.candidate_id IN (SELECT id FROM downstream)
                                    OR (SELECT COUNT(*) FROM downstream)>512)"""


def _edges(project, condition):
    return ("INSERT OR IGNORE INTO candidate_lineage_dependencies(project,upstream,downstream) "
        "SELECT c.project,l.candidate_id,c.id FROM candidates c JOIN json_each(c.body,'$.evidence_refs') ref "
        "JOIN candidate_reference_lineage l ON l.project=c.project AND l.event_id=ref.value "
        f"WHERE c.project={project} AND {condition};")


def install(view):
    """Install a rebuildable reverse index and transactional invalidation hooks."""
    from .context_storage import _dirty_sql
    if not view.db.in_transaction:
        raise RuntimeError("knowledge lineage installation requires a transaction")
    view.db.execute("CREATE TABLE IF NOT EXISTS candidate_lineage_dependencies("
        "project TEXT NOT NULL,upstream TEXT NOT NULL,downstream TEXT NOT NULL,PRIMARY KEY(project,upstream,downstream))")
    view.db.execute("CREATE INDEX IF NOT EXISTS candidate_lineage_downstream ON candidate_lineage_dependencies(project,downstream)")
    view.db.execute("CREATE TABLE IF NOT EXISTS knowledge_lineage_projection_state(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL)")
    state = view.db.execute("SELECT version FROM knowledge_lineage_projection_state WHERE id=1").fetchone()
    if not state or state[0] != 1:
        view.db.execute("DELETE FROM candidate_lineage_dependencies")
        view.db.execute("INSERT OR IGNORE INTO candidate_lineage_dependencies "
            "SELECT c.project,l.candidate_id,c.id FROM candidates c JOIN json_each(c.body,'$.evidence_refs') ref "
            "JOIN candidate_reference_lineage l ON l.project=c.project AND l.event_id=ref.value")
        for statement in _dirty_sql("SELECT DISTINCT s.project,s.knowledge_id,s.revision FROM knowledge_sources s "
                "JOIN candidate_lineage_dependencies d ON d.project=s.project AND d.downstream=s.candidate_id").split(";"):
            if statement.strip():
                view.db.execute(statement)
        view.db.execute("INSERT OR REPLACE INTO knowledge_lineage_projection_state VALUES(1,1)")

    def trigger(name, event, project, seed, *, condition="", before="", after="", include_seed=False):
        view.db.execute(f"CREATE TRIGGER IF NOT EXISTS knowledge_lineage_{name} {event} {condition} BEGIN "
                        + before + _dirty_sql(_affected(project, seed, include_seed)) + after + " END")

    trigger("candidate_insert", "AFTER INSERT ON candidates", "NEW.project", "SELECT NEW.id",
            before=_edges("NEW.project", "c.id=NEW.id"))
    trigger("candidate_update", "AFTER UPDATE OF status,body,problem_revision ON candidates", "NEW.project", "SELECT NEW.id",
            condition="WHEN OLD.status IS NOT NEW.status OR OLD.body IS NOT NEW.body OR OLD.problem_revision IS NOT NEW.problem_revision",
            before="DELETE FROM candidate_lineage_dependencies WHERE project=NEW.project AND downstream=NEW.id;"
                   + _edges("NEW.project", "c.id=NEW.id"))
    trigger("candidate_delete", "BEFORE DELETE ON candidates", "OLD.project", "SELECT OLD.id",
            after="DELETE FROM candidate_lineage_dependencies WHERE project=OLD.project AND downstream=OLD.id;")
    for action, prefix in (("INSERT", "NEW"), ("UPDATE OF state,summary", "NEW"), ("DELETE", "OLD")):
        trigger("concern_" + action.split()[0].lower(), f"AFTER {action} ON candidate_reference_feedback",
                f"{prefix}.project", f"SELECT {prefix}.candidate_id")
    for action, prefix in (("INSERT", "NEW"), ("DELETE", "OLD")):
        downstream = ("SELECT c.id FROM candidates c JOIN json_each(c.body,'$.evidence_refs') ref "
                      f"WHERE c.project={prefix}.project AND ref.value={prefix}.event_id")
        trigger("edge_" + action.lower(), f"AFTER {action} ON candidate_reference_lineage", f"{prefix}.project", downstream,
            before=f"DELETE FROM candidate_lineage_dependencies WHERE project={prefix}.project AND downstream IN ({downstream});"
                   + _edges(f"{prefix}.project", f"c.id IN ({downstream})"), include_seed=True)
    trigger("edge_relation", "AFTER UPDATE OF relation ON candidate_reference_lineage", "NEW.project",
            "SELECT c.id FROM candidates c JOIN json_each(c.body,'$.evidence_refs') ref "
            "WHERE c.project=NEW.project AND ref.value=NEW.event_id",
            condition="WHEN OLD.relation IS NOT NEW.relation", include_seed=True)
    trigger("problem_revision", "AFTER UPDATE OF revision ON problems", "NEW.project",
            "SELECT id FROM candidates WHERE project=NEW.project AND problem=NEW.id",
            condition="WHEN OLD.revision IS NOT NEW.revision")
    for action, prefix in (("INSERT", "NEW"), ("DELETE", "OLD")):
        trigger("source_" + action.lower(), f"AFTER {action} ON knowledge_sources", f"{prefix}.project",
                f"SELECT candidate_id FROM knowledge_sources WHERE project={prefix}.project AND knowledge_id={prefix}.knowledge_id "
                f"UNION SELECT {prefix}.candidate_id")
    for action, prefix in (("INSERT", "NEW"), ("UPDATE OF revision", "NEW"), ("DELETE", "OLD")):
        trigger("active_" + action.split()[0].lower(), f"AFTER {action} ON active", f"{prefix}.project",
                f"SELECT candidate_id FROM knowledge_sources WHERE project={prefix}.project AND knowledge_id={prefix}.id")
    for action, prefix in (("INSERT", "NEW"), ("UPDATE OF disposition,disposition_reason,disposition_receipt", "NEW"), ("DELETE", "OLD")):
        trigger("formal_feedback_" + action.split()[0].lower(), f"AFTER {action} ON knowledge_feedback", f"{prefix}.project",
                f"SELECT candidate_id FROM knowledge_sources WHERE project={prefix}.project AND knowledge_id={prefix}.knowledge_id")
    trigger("operation", "AFTER UPDATE OF conflict,decision ON operations", "NEW.project",
            "SELECT c.id FROM candidates c JOIN json_each(c.body,'$.evidence_refs') ref JOIN events e ON e.id=ref.value "
            "AND e.project=c.project WHERE c.project=NEW.project AND e.operation=NEW.id",
            condition="WHEN OLD.conflict IS NOT NEW.conflict OR OLD.decision IS NOT NEW.decision")
    for action, prefix in (("UPDATE OF outcome", "NEW"), ("DELETE", "OLD")):
        trigger("event_" + action.split()[0].lower(), f"AFTER {action} ON events", f"{prefix}.project",
                "SELECT c.id FROM candidates c JOIN json_each(c.body,'$.evidence_refs') ref "
                f"WHERE c.project={prefix}.project AND ref.value={prefix}.id")
    for action, prefix in (("INSERT", "NEW"), ("DELETE", "OLD")):
        # problem_events has no project column; candidate IDs are globally unique.
        project = f"(SELECT project FROM problems WHERE id={prefix}.problem)"
        trigger("problem_link_" + action.lower(), f"AFTER {action} ON problem_events", project,
                f"SELECT id FROM candidates WHERE project={project} AND problem={prefix}.problem")
    for action, prefix in (("INSERT", "NEW"), ("UPDATE OF enabled,signature,governance_epoch", "NEW"), ("DELETE", "OLD")):
        trigger("history_grant_" + action.split()[0].lower(), f"AFTER {action} ON candidate_reference_history", f"{prefix}.project",
                f"SELECT c.id FROM candidates c JOIN history_case_links l ON l.project=c.project AND l.problem_id=c.problem "
                f"WHERE c.project={prefix}.project AND l.job_id={prefix}.job_id")
    trigger("history_state", "AFTER UPDATE OF state,signature ON history_jobs", "NEW.project",
            "SELECT c.id FROM candidates c JOIN history_case_links l ON l.project=c.project AND l.problem_id=c.problem "
            "WHERE c.project=NEW.project AND l.job_id=NEW.id",
            condition="WHEN OLD.state IS NOT NEW.state OR OLD.signature IS NOT NEW.signature")
    for action, prefix in (("INSERT", "NEW"), ("DELETE", "OLD")):
        trigger("history_link_" + action.lower(), f"AFTER {action} ON history_case_links", f"{prefix}.project",
                f"SELECT id FROM candidates WHERE project={prefix}.project AND problem={prefix}.problem_id")
    # Historical reference grants are tied to the shared governance epoch. An
    # epoch change invalidates those derivations without interpreting it as an
    # automatic revocation or rewriting the original grant/history evidence.
    affected = ("SELECT DISTINCT s.project,s.knowledge_id,s.revision FROM knowledge_sources s "
                "JOIN candidate_lineage_dependencies d ON d.project=s.project AND d.downstream=s.candidate_id")
    view.db.execute("CREATE TRIGGER IF NOT EXISTS knowledge_lineage_governance_epoch AFTER UPDATE OF epoch ON host_state "
                    "WHEN OLD.epoch IS NOT NEW.epoch BEGIN " + _dirty_sql(affected) + " END")
