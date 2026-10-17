"""Project-bound DSH panel requests with displayed versions and durable receipts."""
from __future__ import annotations

from .admin_control import apply_control, control_status
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


def snapshot(runtime, project):
    state = control_status(runtime, project)
    policy = state["policy"]
    return {"project_id": project, "host_scope": runtime._session_scope(),
            "expected": {"expected_policy_version": policy["policy_version"],
                         "expected_project_budget_version": state["project_budget_policy"]["version"],
                         "expected_settings_version": state["settings_version"],
                         "expected_session_epoch": state["session_epoch"],
                         "expected_governance_epoch": state["governance_epoch"]},
            "model_mode": policy["model_mode"], "model": policy["model"],
            "project_limits": state["project_budget_policy"]["values"],
            "diagnostic": state["diagnostic"]}


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
