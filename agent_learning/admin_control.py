"""Explicit local administrator controls; reads never initialize the runtime."""
from __future__ import annotations

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
    return dict(row) if row else {"enabled": 1, "version": 0, "updated": None}


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


def control_status(runtime, project):
    from .admin_queue import eligible_query, scan_signature
    from .admin_usage import admin_budget_policy, admin_budget_status
    routing = runtime.admin_routing_status(project)
    policy, state = routing["policy"], control_state(runtime, project)
    query, args = eligible_query(scan_signature(runtime, project),
        preflight_signature=scan_signature(runtime, project, include_candidates=False),
        summary_enabled=bool(runtime.config["work_memory"]["enabled"] and runtime.config["work_memory"]["summary_enabled"]),
        summary_host_scope=runtime._session_scope())
    eligible = runtime.db.execute("SELECT COUNT(*) FROM (" + query + ")", (project, *args)).fetchone()[0]
    pending = runtime.db.execute("SELECT COUNT(*) FROM problems WHERE project=? AND status='pending'", (project,)).fetchone()[0]
    history = runtime.db.execute("SELECT COUNT(DISTINCT problem_id) FROM history_case_links WHERE project=?", (project,)).fetchone()[0]
    awaiting = runtime.db.execute("SELECT COUNT(*) FROM review_states r JOIN problems p ON p.project=r.project AND p.id=r.problem "
        "WHERE r.project=? AND r.revision=p.revision AND r.state='awaiting_evidence'", (project,)).fetchone()[0]
    blocked = runtime.db.execute("SELECT COUNT(*) FROM admin_calls a WHERE a.project=? AND a.retry_allowed=0 "
        "AND a.state IN ('failed','timeout_unknown','account_changed_unknown','orphaned_unknown') "
        "AND NOT EXISTS(SELECT 1 FROM review_receipts r WHERE r.project=a.project AND r.call_id=a.id)", (project,)).fetchone()[0]
    active = runtime.db.execute("SELECT COUNT(*) FROM admin_calls WHERE project=? AND state IN ('reserved','launch_intent','started')", (project,)).fetchone()[0]
    dispatch = runtime.db.execute("SELECT 1 FROM admin_dispatch WHERE project=? AND host_epoch=? AND lease_until>?", (project, runtime.host_epoch(), time.time())).fetchone()
    enabled = bool(policy["enabled"] and state["enabled"])
    return {"project_id": project, "host_scope": runtime._session_scope(), "session_epoch": runtime.session_epoch(),
        "governance_epoch": runtime.host_epoch(), "settings_version": state["version"], "policy": policy,
        "enabled": enabled, "routing": routing, "budget_policy": admin_budget_policy(runtime),
        "usage": admin_budget_status(runtime),
        "queue": {"pending": pending, "eligible": eligible, "awaiting_evidence": awaiting, "blocked_calls": blocked, "history_excluded": history},
        "runner": {"running": bool(active or dispatch), "state": "stopping" if not enabled and active else "running" if active or dispatch else "idle"},
        "limitation": "Explicitly enabled, bounded local worker; a later supported event can resume pending daily work. Historical imports require their own approval."}


def request_receipt(runtime, project, request_id):
    from .runtime import ContractError
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
        raise ContractError("invalid administrator request identity")
    row = runtime.db.execute("SELECT response FROM admin_control_receipts WHERE project=? AND host_scope=? AND session_epoch=? AND request_id=?",
        (project, runtime._session_scope(), runtime.session_epoch(), request_id)).fetchone()
    return {"found": bool(row), **({"response": json.loads(row[0])} if row else {})}


def apply_control(runtime, project, body, *, wake=False):
    """Commit settings and an idempotent receipt before a single launch attempt."""
    from .runtime import ContractError, digest, encode
    from .admin_usage import admin_budget_policy, configure_admin_budget
    required = {"request_id", "expected_policy_version", "expected_budget_version", "expected_settings_version",
                "expected_session_epoch", "expected_governance_epoch"}
    configured = {"enabled", "model_mode", "model", "limits", "start"}
    if not isinstance(body, dict) or set(body) != required | (set() if wake else configured):
        raise ContractError("invalid administrator control fields")
    request_id = body["request_id"]
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
        raise ContractError("invalid administrator request identity")
    for key in required - {"request_id"}:
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
        policy, state, budget = runtime.admin_policy(project), control_state(runtime, project), admin_budget_policy(runtime)
        if (policy["policy_version"] != body["expected_policy_version"] or budget["version"] != body["expected_budget_version"]
                or state["version"] != body["expected_settings_version"]):
            raise ContractError("administrator settings changed; refresh before saving")
        if not wake:
            if not isinstance(body["enabled"], bool) or not isinstance(body["start"], bool):
                raise ContractError("administrator enable and start flags must be booleans")
            if not isinstance(body["limits"], dict) or set(body["limits"]) - {"max_calls_per_day", "max_calls_per_batch", "max_batch_seconds", "max_run_seconds"}:
                raise ContractError("invalid visual administrator budget fields")
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
            if body["limits"]:
                configure_admin_budget(runtime, body["limits"], expected_version=budget["version"], expected_epoch=runtime.session_epoch())
            if bool(state["enabled"]) != body["enabled"] or not state["version"]:
                runtime.db.execute("INSERT INTO admin_control_state VALUES(?,?,?,?,?) ON CONFLICT(project,host_scope) DO UPDATE SET "
                    "enabled=excluded.enabled,version=excluded.version,updated=excluded.updated",
                    (project, runtime._session_scope(), int(body["enabled"]), state["version"] + 1, time.time()))
        response = {"request_id": request_id, "status": "configured", "settings": control_status(runtime, project),
                    "launch": {"status": "unconfirmed" if start else "not_requested"}}
        runtime.db.execute("INSERT INTO admin_control_receipts VALUES(?,?,?,?,?,?,?)", (*identity, fingerprint, encode(response), time.time()))
    if start:
        launch = wake_admin(runtime, project, expected_session_epoch=body["expected_session_epoch"],
                            expected_governance_epoch=body["expected_governance_epoch"])
        response.update(status=launch["status"], launch=launch)
        response["settings"] = control_status(runtime, project)
        with runtime.transaction():
            runtime.db.execute("UPDATE admin_control_receipts SET response=? WHERE project=? AND host_scope=? AND session_epoch=? AND request_id=?",
                               (encode(response), *identity))
    return response


def wake_admin(runtime, project, *, expected_session_epoch=None, expected_governance_epoch=None):
    from .admin_queue import claim_dispatch, release_dispatch
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
        return {"status": "already_running"}
    try:
        token = claim_dispatch(runtime, project, expected_epoch=expected_governance_epoch,
                               _phase_guard=lambda: runtime.assert_session_epoch(expected_session_epoch))
    except ContractError:
        return {"status": "waiting", "reason": "reconnect_required"}
    if token is None:
        return {"status": "waiting", "reason": "no_ready_work_or_executor_or_budget"}
    path = runtime.db.execute("SELECT path FROM project_paths WHERE project=? ORDER BY length(path),path LIMIT 1", (project,)).fetchone()
    try:
        runtime.assert_session_epoch(expected_session_epoch)
        runtime.assert_host_epoch(expected_governance_epoch)
        if not path or not Path(path[0]).is_dir():
            raise OSError("project workspace unavailable")
        runtime._launch_admin(path[0], token)
    except (OSError, ValueError):
        release_dispatch(runtime, project, token)
        return {"status": "waiting", "reason": "background_launch_failed"}
    return {"status": "accepted"}
