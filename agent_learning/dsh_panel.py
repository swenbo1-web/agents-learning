"""Project-bound DSH panel requests with displayed versions and durable receipts."""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import time

from .admin_control import apply_control, control_summary, readonly_runtime
from .admin_presets import PRESETS
from .panel_control import EXPECTED_FIELDS, _project_for_workspace, same_workspace
from .runtime import ContractError


def bound_project(runtime, workspace, bound_workspace):
    if not same_workspace(workspace, bound_workspace):
        raise ContractError("workspace is not bound to this Agent Learning installation")
    project = runtime.project(bound_workspace)
    if _project_for_workspace(runtime, workspace) != project:
        raise ContractError("workspace belongs to a different logical project")
    return project


def _snapshot(runtime, project, state):
    policy = state["policy"]
    return {"project_id": project, "host_scope": runtime._session_scope(),
            "expected": {"expected_policy_version": policy["policy_version"],
                         "expected_project_budget_version": state["project_budget_policy"]["version"],
                         "expected_settings_version": state["settings_version"],
                         "expected_session_epoch": state["session_epoch"],
                         "expected_governance_epoch": state["governance_epoch"]},
            "model_mode": policy["model_mode"], "model": policy["model"],
            "project_limits": state["project_budget_policy"]["values"],
            "phase_review": state.get("phase_review"),
            "diagnostic": state["diagnostic"]}


def snapshot(runtime, project):
    return _snapshot(runtime, project, control_summary(runtime, project))


def status_payload(runtime, project):
    """Only fields consumed by the native panel, from one common control state."""
    from .admin_workload import workload_summary
    state = control_summary(runtime, project)
    workload = workload_summary(runtime.db, project, history_stages=False)

    def count(table, where=""):
        return runtime.db.execute("SELECT COUNT(*) FROM " + table + " WHERE project=?" + where,
                                  (project,)).fetchone()[0]

    onboarding = runtime.db.execute("SELECT state FROM project_onboarding WHERE project=?", (project,)).fetchone()
    return {"project_id": project, "home": str(runtime.home), "host_scope": runtime._session_scope(),
            "counts": {"active": count("active"), "pending_problems": count("problems", " AND status='pending'"),
                       "candidates": count("candidates"),
                       "pending_review_cases": workload["stages"]["pending_review"],
                       "awaiting_evidence_cases": workload["stages"]["awaiting_evidence"]
                           + workload["stages"]["local_deferred"],
                       "awaiting_approval_cases": workload["stages"]["awaiting_approval"],
                       "historical_pending_units": workload["history_pending_units"]},
            "work_memory": {"memories": count("work_memories"), "file_relations": count("work_file_links")},
            "admin_admission_enabled": state["enabled"], "project_usage": state["project_usage"],
            "global_usage": state["global_usage"], "project_budget_policy": state["project_budget_policy"],
            "global_budget_policy": state["global_budget_policy"], "admission": state["admission"],
            "history_import": {"onboarding": {"state": onboarding[0] if onboarding else "unoffered"}},
            "panel_snapshot": _snapshot(runtime, project, state)}


def read_status(home, workspace, bound_workspace, *, expected_project=None):
    """Native status GET cannot initialize storage, register paths or advance work."""
    if not same_workspace(workspace, bound_workspace):
        raise ContractError("workspace is not bound to this Agent Learning installation")
    default_home = (Path(os.environ["LOCALAPPDATA"]) / "AgentLearning"
                    if os.environ.get("LOCALAPPDATA") else Path.home() / ".local/share/agent-learning")
    home = Path(home or os.environ.get("ALP_HOME") or default_home).resolve()
    database = home / "runtime.sqlite3"
    if not database.is_file():
        return {"registered": False}
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1,
                                isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        deadline, checks = time.monotonic() + 2, [0]

        def budget():
            checks[0] += 1
            return checks[0] > 4000 or time.monotonic() > deadline

        db.set_progress_handler(budget, 1000)
        db.execute("BEGIN")
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] != 25:
                raise ContractError("panel requires database schema 25; run an explicit upgrade first")
            runtime = readonly_runtime(database, db, "dsh", "default")
            project = _project_for_workspace(runtime, bound_workspace)
            if _project_for_workspace(runtime, workspace) != project:
                raise ContractError("workspace belongs to a different logical project")
            # An ID derived from a path hash is not registration. A trusted
            # configured alias may reuse an already registered project, while
            # ordinary paths must retain their registered path association.
            path = os.path.normcase(str(Path(bound_workspace).resolve()))
            known = db.execute("SELECT path,project FROM project_paths WHERE project=?", (project,))
            registered = any(path == row["path"] or path.startswith(row["path"].rstrip(os.sep) + os.sep)
                             for row in known)
            alias = any(value == project and (path == key or path.startswith(key.rstrip(os.sep) + os.sep))
                        for key, value in ((os.path.normcase(str(Path(key).resolve())), value)
                                           for key, value in runtime.config["project_aliases"].items()))
            if not registered and not alias:
                return {"registered": False}
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone():
                return {"registered": False}
            if expected_project is not None and expected_project != project:
                raise ContractError("panel project changed; refresh before continuing")
            if not db.execute("SELECT 1 FROM host_scopes WHERE scope='dsh:default'").fetchone():
                raise ContractError("host_scope_uninitialized")
            return {"data": status_payload(runtime, project)}
        finally:
            if db.in_transaction:
                db.execute("ROLLBACK")


def apply_preset(runtime, project, payload):
    preset = payload.get("admin_preset")
    view = payload.get("panel_snapshot")
    if preset not in {"off", *PRESETS} or payload.get("confirm") is not True:
        raise ContractError("administrator preset requires explicit confirmation")
    if (not isinstance(view, dict) or view.get("project_id") != project
            or view.get("host_scope") != runtime._session_scope()
            or not isinstance(view.get("expected"), dict) or set(view["expected"]) != EXPECTED_FIELDS
            or any(type(value) is not int or value < 0 for value in view["expected"].values())
            or view.get("model_mode") not in {"first", "current", "default", "fixed"}
            or not isinstance(view.get("model"), str)):
        raise ContractError("panel settings changed or unavailable; refresh before saving")
    mode = view["model_mode"]
    from .phase_review import policy as phase_policy
    from .admin_control import request_receipt
    if (preset != "off" and phase_policy(runtime, project)["mode"] == "phase"
            and not request_receipt(runtime, project, payload.get("request_id"))["found"]):
        raise ContractError("stage review uses token budgets; open administrator settings to change it")
    if preset != "off" and view["expected"]["expected_policy_version"] == 0 and mode == "default":
        mode = "first"
    limits = PRESETS.get(preset)
    if limits is None:
        values = view.get("project_limits")
        keys = next(iter(PRESETS.values()))
        if not isinstance(values, dict) or any(key not in values for key in keys):
            raise ContractError("panel budget unavailable; refresh before saving")
        limits = {key: values[key] for key in keys}
    body = {"request_id": payload.get("request_id"), **view["expected"],
            "enabled": preset != "off", "model_mode": mode, "model": view["model"] if mode == "fixed" else "",
            "project_limits": dict(limits), "start": preset != "off"}
    return {"preset": preset, "applied": apply_control(runtime, project, body)}
