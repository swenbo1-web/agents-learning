"""Event-driven wakeups for previously completed stage sources.

The queue is a derived projection: original cases, revisions and review receipts
remain authoritative. Only writes enqueue work; status reads never scan stages.
"""
from __future__ import annotations


def install(runtime):
    runtime.db.execute("CREATE TABLE IF NOT EXISTS phase_source_dirty("
        "phase_id TEXT NOT NULL,problem_id TEXT NOT NULL,revision INTEGER NOT NULL,"
        "PRIMARY KEY(phase_id,problem_id))")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS review_phase_source_task "
        "ON review_phases(project,conversation,task_id,task_revision,state)")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS phase_member_source_revision "
        "ON phase_review_members(project,problem_id,revision,state)")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS phase_source_projection("
        "id INTEGER PRIMARY KEY CHECK(id=1),seed_version INTEGER NOT NULL)")
    # _attach_case_evidence raises revision only after its evidence links are
    # saved. The trigger also covers a new independent case in an old window.
    # Queue by identity here; exclude delivery/summary projections only after
    # their surrounding transaction has finished recording those links.
    runtime.db.execute("CREATE TRIGGER IF NOT EXISTS phase_completed_source_change "
        "AFTER UPDATE OF revision ON problems WHEN NEW.revision!=OLD.revision BEGIN "
        "INSERT INTO phase_source_dirty(phase_id,problem_id,revision) "
        "SELECT p.id,NEW.id,NEW.revision FROM review_phases p "
        "WHERE p.project=NEW.project AND p.conversation=NEW.conversation "
        "AND p.task_id=NEW.task_id AND p.task_revision=NEW.task_revision AND p.state IN ('completed','queued') "
        "ON CONFLICT(phase_id,problem_id) DO UPDATE SET revision=excluded.revision; END")
    if not runtime.db.execute("SELECT 1 FROM phase_source_projection WHERE id=1").fetchone():
        if runtime.db.execute("PRAGMA user_version").fetchone()[0] < 25:
            # Migration discovery only. Original host/account epochs stay on
            # the phase, so the protocol fence cannot authorize old work.
            runtime.db.execute("INSERT OR IGNORE INTO phase_source_dirty(phase_id,problem_id,revision) "
                "SELECT p.id,c.id,c.revision FROM review_phases p JOIN problems c ON c.project=p.project "
                "AND c.conversation=p.conversation AND c.task_id=p.task_id AND c.task_revision=p.task_revision "
                "JOIN events stop ON stop.project=p.project AND stop.id=p.event_id WHERE p.state='completed' "
                "AND NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.problem_id=c.id) "
                "AND NOT EXISTS(SELECT 1 FROM work_summary_links w WHERE w.problem_id=c.id) "
                "AND NOT EXISTS(SELECT 1 FROM phase_review_members m WHERE m.project=c.project "
                "AND m.problem_id=c.id AND m.revision=c.revision AND m.state IN ('candidate','skip','governance')) "
                "AND EXISTS(SELECT 1 FROM problem_events pe JOIN events e ON e.id=pe.event "
                "LEFT JOIN events origin ON e.type='PublicMessageSegment' AND origin.project=e.project "
                "AND origin.id=json_extract(e.payload,'$.public_message_id') WHERE pe.problem=c.id "
                "AND COALESCE(origin.created,e.created)>=p.start_time "
                "AND COALESCE(origin.created,e.created)<=stop.created)")
        runtime.db.execute("INSERT INTO phase_source_projection VALUES(1,25)")


def prepare(runtime, project, *, limit=100):
    """Consume bounded source changes in the current host/account boundary."""
    from .phase_review import _case_source
    rows = runtime.db.execute("SELECT d.phase_id,d.problem_id FROM phase_source_dirty d "
        "JOIN review_phases p ON p.id=d.phase_id WHERE p.project=? AND p.host_scope=? "
        "AND p.session_epoch=? AND p.governance_epoch=? AND p.state!='queued' "
        "ORDER BY d.phase_id,d.problem_id LIMIT ?",
        (project, runtime._session_scope(), runtime.session_epoch(), runtime.host_epoch(), limit)).fetchall()
    reopened = set()
    for row in rows:
        phase = runtime.db.execute("SELECT * FROM review_phases WHERE id=?", (row["phase_id"],)).fetchone()
        if phase and phase["state"] == "completed":
            source_sql, args = _case_source(runtime, project, phase)
            if runtime.db.execute("SELECT 1" + source_sql + "AND p.id=? LIMIT 1",
                                  (*args, row["problem_id"])).fetchone() and not runtime.db.execute(
                    "SELECT 1 FROM phase_review_members m JOIN problems c ON c.project=m.project "
                    "AND c.id=m.problem_id AND c.revision=m.revision WHERE m.project=? AND m.problem_id=? "
                    "AND m.state IN ('candidate','skip','governance') LIMIT 1", (project, row["problem_id"])).fetchone():
                runtime.db.execute("UPDATE review_phases SET state='continuing' WHERE id=? AND state='completed'",
                                   (phase["id"],))
                runtime.db.execute("DELETE FROM phase_scan_progress WHERE phase_id=?", (phase["id"],))
                reopened.add(phase["id"])
        runtime.db.execute("DELETE FROM phase_source_dirty WHERE phase_id=? AND problem_id=?",
                           (row["phase_id"], row["problem_id"]))
    return {"scanned": len(rows), "reopened": len(reopened)}
