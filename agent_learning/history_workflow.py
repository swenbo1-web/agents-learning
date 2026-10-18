"""Project-wide history continuation and explicit deferred work.

The onboarding pointer selects work, not source access or execution authority.
Account, host, frozen evidence and request guards remain independent.
"""
from __future__ import annotations

import json
import time


MODES = {"continue", "incremental", "remaining", "full"}


def active_request(runtime, project):
    return runtime.db.execute("SELECT q.job_id FROM history_review_requests q JOIN host_scopes h "
        "ON h.scope=q.host_scope AND h.epoch=q.session_epoch WHERE q.project=? "
        "AND json_extract(q.body,'$.expected_governance_epoch')=? "
        "AND q.state IN ('reserved','running') AND q.lease_until>? ORDER BY q.created LIMIT 1",
        (project, runtime.host_epoch(), time.time())).fetchone()


def head(runtime, project):
    pointer = runtime.db.execute("SELECT job_id FROM project_onboarding WHERE project=?", (project,)).fetchone()
    if pointer and pointer[0]:
        row = runtime.db.execute("SELECT * FROM history_jobs WHERE project=? AND id=?", (project, pointer[0])).fetchone()
        if row:
            return dict(row)
    row = runtime.db.execute("SELECT * FROM history_jobs WHERE project=? ORDER BY created DESC,id LIMIT 1", (project,)).fetchone()
    return dict(row) if row else None


def workflow_state(row, summary):
    review = summary["coverage"]["review"]
    result = json.loads(row["last_result"])
    deferred = bool(result.get("workflow_deferred"))
    first_pass_complete = (summary["coverage"]["archive"]["selected_sources_complete"]
                           and not review["ready_cases"] and not review["blocked_cases"])
    unfinished = (row["state"] != "cancelled" and not deferred
                  and (row["state"] == "prepared" or not first_pass_complete or summary["run_active"]))
    return {"deferred": deferred, "first_pass_complete": first_pass_complete,
            "blocks_new_batch": unfinished,
            "next_action": "wait" if summary["run_active"] else "reauthorize" if not summary["epoch_current"]
                else "continue_review" if unfinished and summary["coverage"]["archive"]["selected_sources_complete"]
                else "continue_archive" if unfinished else "detect_changes",
            "awaiting_approval_blocks": False, "awaiting_evidence_blocks": False}


def existing_work(runtime, project, mode):
    from .history_import import _summary
    from .runtime import ContractError
    request = active_request(runtime, project)
    row = (dict(runtime.db.execute("SELECT * FROM history_jobs WHERE project=? AND id=?",
            (project, request[0])).fetchone()) if request else head(runtime, project))
    if not row:
        return None
    summary = _summary(runtime, row)
    if summary["workflow"]["blocks_new_batch"] or request:
        if mode != "continue":
            raise ContractError("unfinished history batch " + row["id"]
                                + "; continue it or explicitly defer it before scanning another batch")
        return dict(summary, reused=True, preparation={"action": "continue_existing", "scanned": False,
                    "models_started": False, "next_action": "wait" if request else summary["workflow"]["next_action"]})
    return None


def defer_history(runtime, project, job_id, reason, expected_epoch=None):
    from .runtime import ContractError, clean, encode
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ContractError("history deferral requires an explicit reason")
    with runtime._case_transaction():
        runtime.assert_host_epoch(expected_epoch)
        row = runtime.db.execute("SELECT * FROM history_jobs WHERE project=? AND id=?", (project, job_id)).fetchone()
        if row is None or row["state"] == "cancelled":
            raise ContractError("history batch is unavailable")
        if row["owner"] and (row["lease_until"] or 0) > time.time():
            raise ContractError("wait for the active history worker before deferring this batch")
        request = active_request(runtime, project)
        if request and request[0] == job_id:
            raise ContractError("wait for the active history request before deferring this batch")
        result = json.loads(row["last_result"])
        result.update(workflow_deferred=True, workflow_reason=clean(reason), workflow_deferred_at=time.time())
        runtime.db.execute("UPDATE history_jobs SET last_result=?,updated=? WHERE project=? AND id=?",
                           (encode(result), time.time(), project, job_id))
    return {"status": "history_deferred", "job_id": job_id, "models_started": False,
            "knowledge_published": False, "source_access_changed": False}


def assert_run_head(runtime, project, job_id):
    from .runtime import ContractError, encode
    request = active_request(runtime, project)
    if request and request[0] != job_id:
        raise ContractError("this project already has an active history review request")
    current = head(runtime, project)
    if current and current["id"] != job_id:
        from .history_import import _summary
        if _summary(runtime, current)["workflow"]["blocks_new_batch"]:
            raise ContractError("another unfinished history batch owns this project; continue or explicitly defer it")
    # A confirmed run can reclaim a deferred batch after other main work ends.
    row = runtime.db.execute("SELECT last_result FROM history_jobs WHERE project=? AND id=?", (project, job_id)).fetchone()
    if row:
        result = json.loads(row[0])
        if result.pop("workflow_deferred", False):
            runtime.db.execute("UPDATE history_jobs SET last_result=? WHERE project=? AND id=?", (encode(result), project, job_id))
    runtime.db.execute("INSERT INTO project_onboarding VALUES(?,'started',?,?) ON CONFLICT(project) "
        "DO UPDATE SET state='started',job_id=excluded.job_id,updated=excluded.updated", (project, job_id, time.time()))
