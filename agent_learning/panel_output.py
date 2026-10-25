"""Bound model-visible card results without changing the private UI contract."""
from __future__ import annotations

import json
import math


MAX_MODEL_CHARS = 3000
_USAGE = ("budget_mode", "call_limit_enforced", "counted_attempts", "completed_reviews",
          "failed_attempts", "active_attempts", "reported_tokens", "usage_unknown_attempts",
          "remaining_calls", "remaining_reported_tokens")


def _fields(value, names):
    if not isinstance(value, dict):
        return {}
    result = {}
    for name in names:
        if name not in value:
            continue
        item = value[name]
        if item is None or isinstance(item, bool):
            result[name] = item
        elif isinstance(item, (int, float)):
            result[name] = item if abs(item) <= 2**63 - 1 and math.isfinite(item) else None
        elif isinstance(item, str):
            result[name] = "".join(char for char in " ".join(item.split())
                                   if ord(char) >= 32 and ord(char) != 127)[:160]
    return result


def model_panel_result(result):
    """Whitelist facts for the model; capability-bearing UI data stays private.

    This does not trim PanelControl, local HTTP or native host responses. Missing
    or unreported metrics stay absent/null, rather than becoming zero usage.
    """
    public = _fields(result, ("visible", "reason", "opened", "preset"))
    summary = result.get("summary")
    if isinstance(summary, dict):
        small = _fields(summary, ("project_id", "project_name", "host_scope", "admin_admission_enabled",
                                 "model_mode", "model", "admin_phase", "reason", "budget_scope"))
        small["counts"] = _fields(summary.get("counts"), ("active", "work_memory", "pending_review_cases",
            "awaiting_evidence_cases", "awaiting_approval_cases", "historical_pending_units", "candidates"))
        small["administrator_readiness"] = _fields(summary.get("administrator_readiness"),
            ("provider", "launcher_available", "capability_checked", "capability_ready", "authentication_checked"))
        for name in ("project_usage", "global_usage"):
            if name in summary:
                small[name] = _fields(summary[name], _USAGE)
        small["admission"] = _fields(summary.get("admission"), ("admission_status", "blocking_scope"))
        phase = summary.get("phase_review")
        if isinstance(phase, dict):
            small["phase_review"] = _fields(phase, ("blocked_phase_count", "waiting_phase_count",
                "executor_waiting_batch_count", "local_seal_pending_count", "historical_imports_included"))
            small["phase_review"]["policy"] = _fields(phase.get("policy"), ("mode", "every_phases"))
        public["summary"] = small
    history = result.get("history")
    if isinstance(history, dict):
        public["history"] = _fields(history, ("job_id", "state", "archived_cases", "reviewed_cases",
                                              "published_knowledge", "review_requested"))
        preparation = _fields(history.get("preparation"), ("action",))
        if preparation:
            public["history"]["preparation"] = preparation
    diagnostics = result.get("diagnostics")
    if isinstance(diagnostics, dict):
        public["diagnostics"] = _fields(diagnostics, ("version",))
        public["diagnostics"]["connection"] = _fields(diagnostics.get("connection"),
            ("ui_bridge", "mcp", "hook", "context_bound"))
        public["diagnostics"]["installation"] = _fields(diagnostics.get("installation"),
            ("state", "version", "protocol", "running_version", "running_protocol", "protocol_matches",
             "running_runtime_matches_installation", "latest_release_checked"))
        public["diagnostics"]["administrator"] = _fields(diagnostics.get("administrator"),
            ("enabled", "model", "phase", "reason", "next_due", "blocking_scope"))
    # Optional sections cannot inflate a chat reply through large UI snapshots.
    small = public.get("summary", {})
    for owner, name in ((public, "diagnostics"), (small, "global_usage"),
                        (small, "counts"), (small, "phase_review"), (small, "administrator_readiness"),
                        (public, "history"), (small, "project_usage")):
        if len(json.dumps(public, ensure_ascii=False, separators=(",", ":"))) <= MAX_MODEL_CHARS:
            break
        if name in owner:
            owner.pop(name)
            public["details_omitted"] = True
    # Quotes and backslashes expand when encoded. Limit the actual JSON, rather
    # than assuming a character cap on values is also a cap on the response.
    if len(json.dumps(public, ensure_ascii=False, separators=(",", ":"))) > MAX_MODEL_CHARS:
        for owner in (public, small, small.get("admission", {})):
            for name, item in owner.items():
                if isinstance(item, str):
                    owner[name] = item[:64]
        public["details_omitted"] = True
    return public
