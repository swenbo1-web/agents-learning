"""Explicit local administrator controls; reads never initialize the runtime."""
from __future__ import annotations

from .phase_review import scope as phase_scope, status as phase_status

import json
import os
from pathlib import Path
import time


def install_control(runtime):
    from .runtime import ContractError
    if not runtime.db.in_transaction:
        raise ContractError("administrator control installation requires a transaction")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS admin_control_state(
        project TEXT NOT NULL,host_scope TEXT NOT NULL,enabled INTEGER NOT NULL,
        version INTEGER NOT NULL,updated REAL NOT NULL,PRIMARY KEY(project,host_scope))""")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS admin_control_receipts(
        project TEXT NOT NULL,host_scope TEXT NOT NULL,session_epoch INTEGER NOT NULL,
        request_id TEXT NOT NULL,request_digest TEXT NOT NULL,response TEXT NOT NULL,created REAL NOT NULL,
        PRIMARY KEY(project,host_scope,session_epoch,request_id))""")


def control_state(runtime, project):
    exists = runtime.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_control_state'").fetchone()
    row = runtime.db.execute("SELECT * FROM admin_control_state WHERE project=? AND host_scope=?",
        (project, runtime._session_scope())).fetchone() if exists else None
    return dict(row) if row else {"enabled": 1, "version": 0, "updated": None, "auto_publish": None}


def admission_enabled(runtime, project):
    return bool(control_state(runtime, project)["enabled"])


def readonly_runtime(database, db, host, instance):
    """Build a read view, without Runtime.__init__, migration, recovery or mkdir."""
    from .runtime import Runtime, DEFAULTS, ContractError, encode
    from .admin_routing import validate_admin_routing_config
    view = Runtime.__new__(Runtime)
    view.home = Path(database).parent
    view.host, view.host_instance, view.host_scope = host, instance, host + ":" + instance
    view.db = db
    view.config = json.loads(encode(DEFAULTS))
    path = Path(os.environ.get("ALP_CONFIG", view.home / "config.json"))
    supplied = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    if not isinstance(supplied, dict):
        raise ContractError("configuration must be an object")
    for key, value in supplied.items():
        if key in {"admin", "scheduler", "work_memory", "hindsight"} and isinstance(value, dict):
            view.config[key].update(value)
        else:
            view.config[key] = value
    view.config["admin"] = validate_admin_routing_config(view.config["admin"])
    return view


def _control_queue(runtime, project, *, include_history=True):
    from .admin_queue import eligible_query, scan_signatures
    from .admin_workload import workload_summary
    signature, preflight_signature = scan_signatures(runtime, project)
    query, args = eligible_query(signature,
        preflight_signature=preflight_signature,
        summary_enabled=runtime.work_summary_enabled(project),
        summary_host_scope=runtime._session_scope(), phase_scope=phase_scope(runtime, project))
    eligible = runtime.db.execute("SELECT COUNT(*) FROM (" + query + ")", (project, *args)).fetchone()[0]
    pending = runtime.db.execute("SELECT COUNT(*) FROM problems WHERE project=? AND status='pending'", (project,)).fetchone()[0]
    history = runtime.db.execute("SELECT COUNT(DISTINCT problem_id) FROM history_case_links WHERE project=?", (project,)).fetchone()[0]
    awaiting = runtime.db.execute("SELECT COUNT(*) FROM review_states r JOIN problems p ON p.project=r.project AND p.id=r.problem "
        "WHERE r.project=? AND r.revision=p.revision AND r.state='awaiting_evidence'", (project,)).fetchone()[0]
    blocked = runtime.db.execute("SELECT COUNT(*) FROM admin_calls a WHERE a.project=? AND a.retry_allowed=0 "
        "AND a.state IN ('failed','timeout_unknown','account_changed_unknown','orphaned_unknown') "
        "AND NOT EXISTS(SELECT 1 FROM review_receipts r WHERE r.project=a.project AND r.call_id=a.id)", (project,)).fetchone()[0]
    return {"pending": pending, "eligible": eligible, "awaiting_evidence": awaiting,
            "blocked_calls": blocked, "history_excluded": history,
            "workload": workload_summary(runtime.db, project, history_stages=include_history)}


def control_status(runtime, project, *, include_history=True, details=True):
    """Settings can be read without a scan of cases, events or archived batches."""
    from .admin_queue import dispatch_diagnostic
    from .admin_usage import admin_admission_status
    from .admin_presets import PRESETS
    from .admin_recovery import recovery_status
    from .admin_review_flow import publication_policy, review_activity
    from .candidate_references import policy as reference_policy
    routing = runtime.admin_routing_status(project)
    policy, state = routing["policy"], control_state(runtime, project)
    diagnostic = dispatch_diagnostic(runtime, project, check_queue=details)
    running = diagnostic["phase"] in {"worker_claimed", "model_reserved", "model_launching", "model_started"}
    enabled = bool(policy["enabled"] and state["enabled"])
    admission = admin_admission_status(runtime, project)
    project_usage, global_usage = admission["project_usage"], admission["global_usage"]
    result = {"project_id": project, "host_scope": runtime._session_scope(), "session_epoch": runtime.session_epoch(),
        "governance_epoch": runtime.host_epoch(), "settings_version": state["version"], "policy": policy,
        "enabled": enabled, "routing": routing, "budget_policy": global_usage["budget_policy"],
        "project_budget_policy": project_usage["budget_policy"], "global_budget_policy": global_usage["budget_policy"],
        "project_usage": project_usage, "global_usage": global_usage, "admission": admission,
        "presets": {key: dict(values) for key, values in PRESETS.items()},
        "work_summary": runtime.work_summary_policy(project), "diagnostic": diagnostic,
        "publication_policy": publication_policy(runtime, project),
        "phase_review": phase_status(runtime, project),
        "candidate_reference_policy": reference_policy(runtime, project),
        "effective_run_seconds": min(runtime.admin_effective_config(project)["max_run_seconds"], runtime.config["scheduler"]["max_run_seconds"]) if runtime.config["scheduler"]["enabled"] else runtime.admin_effective_config(project)["max_run_seconds"],
        "usage": global_usage,
        "runner": {"running": running, "state": "stopping" if not enabled and running else diagnostic["phase"],
                   "reason": diagnostic["reason"], "next_due": diagnostic.get("next_due")},
        "limitation": "Explicitly enabled, bounded local worker; a later supported event can resume pending daily work. Historical imports require their own approval."}
    if details:
        result.update(queue=_control_queue(runtime, project, include_history=include_history),
                      recovery=recovery_status(runtime, project, include_history=include_history),
                      review_activity=review_activity(runtime, project))
    else:
        result["details_separate"] = True
    return result


def control_detail(runtime, project, section):
    """One independently budgeted read section, bound to the settings snapshot."""
    from .admin_usage import project_budget_policy
    from .runtime import ContractError
    if section == "queue":
        data = _control_queue(runtime, project, include_history=False)
    elif section == "recovery":
        from .admin_recovery import recovery_status
        data = recovery_status(runtime, project, include_history=False)
    elif section == "activity":
        from .admin_review_flow import review_activity
        data = review_activity(runtime, project)
    else:
        raise ContractError("invalid administrator detail section")
    return {"project_id": project, "host_scope": runtime._session_scope(), "section": section,
            "versions": [runtime.admin_policy(project)["policy_version"], project_budget_policy(runtime, project)["version"],
                         control_state(runtime, project)["version"], runtime.session_epoch(), runtime.host_epoch()],
            "captured_at": time.time(), "data": data}


def request_receipt(runtime, project, request_id):
    from .runtime import ContractError
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
        raise ContractError("invalid administrator request identity")
    row = runtime.db.execute("SELECT response FROM admin_control_receipts WHERE project=? AND host_scope=? AND session_epoch=? AND request_id=?",
        (project, runtime._session_scope(), runtime.session_epoch(), request_id)).fetchone()
    return {"found": bool(row), **({"response": json.loads(row[0])} if row else {})}


def apply_control(runtime, project, body, *, wake=False, details=True):
    """Commit settings and an idempotent receipt before a single launch attempt."""
    from .runtime import ContractError, digest, encode
    from .admin_usage import admin_budget_policy, configure_admin_budget, project_budget_policy, configure_project_budget
    required = {"request_id", "expected_policy_version", "expected_settings_version",
                "expected_session_epoch", "expected_governance_epoch"}
    configured = {"enabled", "model_mode", "model", "start"}
    versions = {"expected_budget_version", "expected_project_budget_version"}
    optional = {"summary_enabled", "auto_publish", "publication_mode", "limits", "project_limits", "phase_review", "expected_phase_version"} if not wake else set()
    mandatory = required | (set() if wake else configured)
    if (not isinstance(body, dict) or not mandatory <= set(body) or set(body) - (mandatory | versions | optional)
            or not versions.intersection(body)
            or not wake and not {"limits", "project_limits"}.intersection(body)
            or "limits" in body and "expected_budget_version" not in body
            or "project_limits" in body and "expected_project_budget_version" not in body
            or any(key in body and not isinstance(body[key], bool) for key in ("summary_enabled", "auto_publish"))):
        raise ContractError("invalid administrator control fields")
    if "publication_mode" in body and (not isinstance(body["publication_mode"], str)
                                      or body["publication_mode"] not in {"verified_operation_solutions", "tiered"}):
        raise ContractError("invalid automatic publication mode")
    request_id = body["request_id"]
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
        raise ContractError("invalid administrator request identity")
    for key in (required | versions.intersection(body)) - {"request_id"}:
        if isinstance(body[key], bool) or not isinstance(body[key], int) or body[key] < 0:
            raise ContractError("administrator versions must be nonnegative integers")
    fingerprint = digest(["wake" if wake else "configure", body])
    start = wake or body.get("start", False)
    with runtime.transaction():
        runtime.assert_session_epoch(body["expected_session_epoch"])
        runtime.assert_host_epoch(body["expected_governance_epoch"])
        identity = (project, runtime._session_scope(), runtime.session_epoch(), request_id)
        prior = runtime.db.execute("SELECT * FROM admin_control_receipts WHERE project=? AND host_scope=? AND session_epoch=? AND request_id=?", identity).fetchone()
        if prior:
            if prior["request_digest"] != fingerprint:
                raise ContractError("administrator request identity reused with different settings")
            return json.loads(prior["response"])
        policy, state = runtime.admin_policy(project), control_state(runtime, project)
        budget, project_budget = admin_budget_policy(runtime), project_budget_policy(runtime, project)
        check_global = "limits" in body or wake and "expected_project_budget_version" not in body
        check_project = "project_limits" in body or wake and "expected_project_budget_version" in body
        if (policy["policy_version"] != body["expected_policy_version"]
                or check_global and budget["version"] != body["expected_budget_version"]
                or check_project and project_budget["version"] != body["expected_project_budget_version"]
                or state["version"] != body["expected_settings_version"]):
            raise ContractError("administrator settings changed; refresh before saving")
        if not wake:
            if not isinstance(body["enabled"], bool) or not isinstance(body["start"], bool):
                raise ContractError("administrator enable and start flags must be booleans")
            # Native quick controls also use this path. Only the first explicit
            # enable receives the new default; an existing off choice stays off.
            first_enable = (body["enabled"] and not state["version"] and not policy["policy_version"]
                            and state.get("auto_publish") is None
                            and not runtime.config["admin"].get("auto_publish", False))
            automatic = body.get("auto_publish", True if first_enable else state.get("auto_publish"))
            publication_mode = body.get("publication_mode", "tiered" if first_enable and "auto_publish" not in body
                                        else state.get("publication_mode"))
            if ("limits" in body and (not isinstance(body["limits"], dict)
                    or set(body["limits"]) - {"max_calls_per_day", "max_calls_per_batch", "max_batch_seconds", "max_run_seconds"})):
                raise ContractError("invalid visual administrator budget fields")
            if "project_limits" in body:
                from .admin_usage import PROJECT_BUDGET_FIELDS, _validate_budget_values
                if not isinstance(body["project_limits"], dict) or set(body["project_limits"]) - PROJECT_BUDGET_FIELDS:
                    raise ContractError("invalid visual administrator project budget fields")
                _validate_budget_values(body["project_limits"])
            mode, model = body["model_mode"], body["model"]
            changed_model = mode != policy["model_mode"] or model != policy["model"]
            active = runtime.db.execute("SELECT 1 FROM admin_calls WHERE project=? AND state IN ('reserved','launch_intent','started') LIMIT 1", (project,)).fetchone()
            if changed_model and active:
                raise ContractError("wait for the current administrator call before switching models")
            # Pause admission independently: an already paid call keeps its frozen
            # policy and may commit. Re-enabling the same policy does not move it.
            if changed_model or body["enabled"] and not policy["enabled"]:
                runtime.admin_configure(project, policy["enabled"] or body["enabled"], policy["provider"], mode, model,
                    expected_policy_version=policy["policy_version"], expected_epoch=runtime.session_epoch())
            if body.get("limits"):
                configure_admin_budget(runtime, body["limits"], expected_version=budget["version"], expected_epoch=runtime.session_epoch())
            if body.get("project_limits"):
                configure_project_budget(runtime, project, body["project_limits"], expected_version=project_budget["version"],
                                         expected_epoch=runtime.session_epoch())
            phase_prior = phase_status(runtime, project)["policy"]
            if "phase_review" in body or first_enable and not runtime.config["admin"]["enabled"] and not phase_prior["configured"]:
                from .phase_review import configure as configure_phases
                configure_phases(runtime, project, body.get("phase_review", {"mode":"phase"}), body.get("expected_phase_version", 0))
            phase_changed = phase_status(runtime, project)["policy"]["version"] != phase_prior["version"]
            summary_prior = runtime.work_summary_policy(project)
            summary_changed = "summary_enabled" in body and (not summary_prior["configured"]
                or body["summary_enabled"] != summary_prior["enabled"])
            if "summary_enabled" in body:
                runtime.configure_work_summary(project, body["summary_enabled"])
            publication_changed = (automatic != state.get("auto_publish")
                                   or publication_mode != state.get("publication_mode"))
            if bool(state["enabled"]) != body["enabled"] or not state["version"] or summary_changed or publication_changed or phase_changed:
                runtime.db.execute("INSERT INTO admin_control_state(project,host_scope,enabled,version,updated,auto_publish,publication_mode) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(project,host_scope) DO UPDATE SET "
                    "enabled=excluded.enabled,version=excluded.version,updated=excluded.updated,auto_publish=excluded.auto_publish,"
                    "publication_mode=excluded.publication_mode",
                    (project, runtime._session_scope(), int(body["enabled"]), state["version"] + 1, time.time(),
                     int(automatic) if automatic is not None else None, publication_mode))
        response = {"request_id": request_id, "status": "configured", "settings": control_status(runtime, project, details=details),
                    "launch": {"status": "unconfirmed" if start else "not_requested"}}
        runtime.db.execute("INSERT INTO admin_control_receipts VALUES(?,?,?,?,?,?,?)", (*identity, fingerprint, encode(response), time.time()))
    if start:
        launch = wake_admin(runtime, project, expected_session_epoch=body["expected_session_epoch"],
                            expected_governance_epoch=body["expected_governance_epoch"])
        response.update(status=launch["status"], launch=launch)
        response["settings"] = control_status(runtime, project, details=details)
        with runtime.transaction():
            runtime.db.execute("UPDATE admin_control_receipts SET response=? WHERE project=? AND host_scope=? AND session_epoch=? AND request_id=?",
                               (encode(response), *identity))
    return response


def wake_admin(runtime, project, *, expected_session_epoch=None, expected_governance_epoch=None):
    from .admin_queue import claim_dispatch, release_dispatch, dispatch_diagnostic, record_dispatch_diagnostic
    from .runtime import ContractError
    try:
        runtime.assert_session_epoch(expected_session_epoch)
        runtime.assert_host_epoch(expected_governance_epoch)
    except ContractError:
        return {"status": "waiting", "reason": "reconnect_required"}
    if not runtime.admin_effective_config(project)["enabled"]:
        return {"status": "waiting", "reason": "administrator_disabled"}
    existing = runtime.db.execute("SELECT 1 FROM admin_dispatch WHERE project=? AND host_epoch=? AND lease_until>?", (project, runtime.host_epoch(), time.time())).fetchone()
    if existing:
        diagnostic = dispatch_diagnostic(runtime, project)
        return {"status": "accepted" if diagnostic["phase"] == "worker_reserved" else "already_running", "reason": diagnostic["reason"]}
    try:
        from .phase_review import prepare as prepare_phases
        with runtime.transaction():
            prepare_phases(runtime, project, force=True)
        token = claim_dispatch(runtime, project, expected_epoch=expected_governance_epoch,
                               _phase_guard=lambda: runtime.assert_session_epoch(expected_session_epoch))
    except ContractError:
        return {"status": "waiting", "reason": "reconnect_required"}
    if token is None:
        return {"status": "waiting", "reason": dispatch_diagnostic(runtime, project)["reason"]}
    path = runtime.db.execute("SELECT path FROM project_paths WHERE project=? ORDER BY length(path),path LIMIT 1", (project,)).fetchone()
    try:
        runtime.assert_session_epoch(expected_session_epoch)
        runtime.assert_host_epoch(expected_governance_epoch)
        if not path or not Path(path[0]).is_dir():
            raise OSError("project workspace unavailable")
        runtime._launch_admin(path[0], token)
    except (OSError, ValueError) as exc:
        release_dispatch(runtime, project, token)
        from .admin_providers import launch_error
        detail = launch_error(exc)
        record_dispatch_diagnostic(runtime, project, "background_launch_failed", detail)
        return {"status": "waiting", "reason": "background_launch_failed"}
    return {"status": "accepted"}
