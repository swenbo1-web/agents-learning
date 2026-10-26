"""Explicit, bounded status facts for debugging; full status stays backward compatible."""
from __future__ import annotations

import json
import math

from . import __version__


MAX_MODEL_CHARS = 3000
_USAGE_COUNTS = ("counted_attempts", "completed_reviews", "failed_attempts",
                 "result_unknown_attempts", "active_attempts", "usage_unknown_attempts",
                 "reported_tokens", "token_reservations", "remaining_calls",
                 "remaining_reported_tokens", "next_reset_at")


class StatusSummary(dict):
    """Text-only MCP projection, so structuredContent does not repeat the same facts."""


def _object(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    if (type(value) not in (int, float) or abs(value) > 2**63 - 1
            or not math.isfinite(value)):
        return None
    return value


def status_summary(status):
    """Project existing observations only; no database, configuration or permission changes.

    The budget includes the MCP text content envelope, including JSON escaping.
    Unknown counters are never replaced by zero. Long strings are visibly
    shortened and flagged; callers can explicitly request view=full for detail.
    """
    status = _object(status)
    truncated = False

    def text(value, limit=160):
        nonlocal truncated
        if not isinstance(value, str):
            return None
        safe = "".join(char for char in value if ord(char) >= 32 and ord(char) != 127)
        if safe != value or len(safe) > limit:
            truncated = True
        return safe if len(safe) <= limit else safe[:limit - 3] + "..."

    def fields(value, names):
        value = _object(value)
        return {name: (item if item is None or isinstance(item, bool) else
                       _number(item) if isinstance(item, (int, float)) else text(item))
                for name in names if name in value for item in (value[name],)}

    def usage(value):
        value = _object(value)
        result = fields(value, ("scope", "project_id", "utc_day", "admission_status",
                                "budget_mode", "call_limit_enforced"))
        result.update({name: _number(value[name]) for name in _USAGE_COUNTS if name in value})
        return result

    routing = _object(status.get("admin_routing"))
    public = StatusSummary(view="summary", version=__version__,
        **fields(status, ("project_id", "host_scope", "host_epoch", "storage_schema_version",
                          "knowledge_scope", "admin_enabled", "hindsight_enabled")))
    scope = status.get("host_scope")
    host = scope.split(":", 1)[0] if isinstance(scope, str) and ":" in scope else None
    public["host"] = host if host in {"codex", "antigravity", "dsh"} else None
    public["home"] = text(status.get("home"), 512)
    # An absent effective switch is unknown, never inferred from the policy.
    actual = status.get("admin_admission_enabled")
    public["admin_admission_enabled"] = actual if isinstance(actual, bool) else None
    public["admin_routing"] = fields(routing, ("routing_ready", "ready", "reason"))
    for name in ("policy", "executor"):
        if name in routing:
            public["admin_routing"][name] = fields(routing[name], ("enabled", "provider", "model_mode", "model"))
    dispatch = _object(status.get("admin_dispatch_diagnostic"))
    public["admin_dispatch_diagnostic"] = fields(dispatch,
        ("phase", "reason", "blocking_scope", "next_due", "blocked_review_scope"))
    if "queue_blocked_by_protected_calls" in dispatch:
        blocked = dispatch["queue_blocked_by_protected_calls"]
        public["admin_dispatch_diagnostic"]["queue_blocked_by_protected_calls"] = (
            blocked if isinstance(blocked, bool) else None)
    backlog = _object(dispatch.get("protected_phase_backlog"))
    if backlog:
        public["admin_dispatch_diagnostic"]["protected_phase_backlog"] = {
            key: value if type(value) is int and 0 <= value < 2**63 else None
            for key in ("batches", "phases") for value in (backlog.get(key),)}
    phase = _object(status.get("phase_review"))
    registration = _object(phase.get("latest_delivery_registration"))
    if (isinstance(registration.get("state"), str) and isinstance(registration.get("reason"), str)
            and registration.get("state") in {"not_registered", "pending"}
            and registration.get("reason") in {"task_missing", "task_not_delivered", "delivery_already_registered",
                                               "delivery_not_registered", "awaiting_capture_seal"}):
        public["latest_delivery_registration"] = fields(registration, ("state", "reason", "observed_at"))
    for name in ("project_usage", "global_usage"):
        # Missing project statistics must not silently inherit global figures.
        public[name] = usage(status.get(name))
    public["admission"] = fields(status.get("admission"), ("admission_status", "blocking_scope"))
    workload = _object(status.get("workload"))
    public["workload"] = {"scope": "project", **fields(workload, ("available",)),
        "stages": fields(workload.get("stages"),
                         ("pending_review", "awaiting_evidence", "awaiting_approval", "blocked"))}
    public["work_summary_policy"] = fields(status.get("work_summary_policy"), ("enabled",))
    memory = _object(status.get("work_memory"))
    public["work_memory"] = fields(memory, ("index_version", "index_rebuild_pending", "capture_repair_pending"))
    public["work_memory"]["settings"] = fields(memory.get("settings"), ("enabled", "automatic_context", "summary_enabled"))
    scheduler = _object(status.get("scheduler"))
    public["scheduler"] = fields(scheduler, ("enabled", "continuous_service_installed", "total_jobs", "sync_dirty_count"))
    if "worker" in scheduler:
        public["scheduler"]["worker"] = (None if scheduler["worker"] is None else
            fields(scheduler["worker"], ("state", "host_epoch", "lease_until")))
    jobs = scheduler.get("items")
    if isinstance(jobs, list):
        public["scheduler"]["items"] = [fields(job, ("kind", "state", "wait_reason", "next_due")) for job in jobs[:3]]
        if len(jobs) > 3:
            public["scheduler"]["items_omitted"] = True
    if "host_capture" in status:
        capture = _object(status["host_capture"])
        public["host_capture"] = fields(capture, ("state", "reason", "scan_limited"))
        for name in ("public_delivery", "public_response"):
            if name in capture:
                public["host_capture"][name] = (fields(capture[name], ("state", "value", "available", "reason"))
                    if isinstance(capture[name], dict) else text(capture[name]))
        if "input_window" in capture:
            public["host_capture"]["input_window"] = fields(capture["input_window"],
                ("state", "public_input", "available", "reason"))
        if "delivery_recovery_reason" in capture:
            from .antigravity_health import DELIVERY_RECOVERY_REASONS
            reason = capture["delivery_recovery_reason"]
            if isinstance(reason, str) and reason in DELIVERY_RECOVERY_REASONS:
                public["host_capture"]["delivery_recovery_reason"] = reason
    public["full_view"] = {"tool": "learning_status", "arguments": {"view": "full"}}
    if truncated:
        public["strings_truncated"] = True

    def size():
        body = json.dumps(public, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        envelope = json.dumps({"content": [{"type": "text", "text": body}], "isError": False},
                              ensure_ascii=False, separators=(",", ":"))
        # DSH renders this projection in JavaScript: non-BMP code points use
        # two UTF-16 units there. Count the final escaped envelope the same way.
        return len(envelope.encode("utf-16-le", errors="surrogatepass")) // 2

    # Extended coverage and workload details are optional; identity, actual
    # admission, routing and the two separate ledgers have priority.
    if size() > MAX_MODEL_CHARS:
        public.pop("workload", None)
        public["details_omitted"] = True
    if size() > MAX_MODEL_CHARS and "items" in public["scheduler"]:
        public["scheduler"].pop("items")
        public["scheduler"]["items_omitted"] = True
        public["details_omitted"] = True
    for limit in (80, 40, 16, 8):
        if size() <= MAX_MODEL_CHARS:
            break

        def shorten(value):
            for name, item in list(value.items()):
                if name == "full_view":
                    continue
                if isinstance(item, dict):
                    shorten(item)
                elif isinstance(item, str) and len(item) > limit:
                    value[name] = item[:limit - 3] + "..."
                    public["strings_truncated"] = True

        shorten(public)
        public["details_omitted"] = True
    if size() > MAX_MODEL_CHARS:
        # Even already-short, heavily escaped strings and 64-bit counters may
        # exhaust the envelope. Keep the effective gate, separate ledgers and
        # uncertainty facts; omitted diagnostics remain available through full.
        keep = ("view", "version", "project_id", "home", "host", "host_scope", "host_epoch",
                "storage_schema_version", "admin_enabled", "admin_admission_enabled", "admin_routing",
                "admission", "full_view", "strings_truncated")
        ledgers = {name: public[name] for name in ("project_usage", "global_usage")}
        public = StatusSummary({name: public[name] for name in keep if name in public})
        for name in ("project_usage", "global_usage"):
            # Use bounded values from the projection, not fresh long strings.
            public[name] = {key: value for key, value in ledgers[name].items() if key in {
                "scope", "project_id", "utc_day", "counted_attempts", "completed_reviews", "failed_attempts",
                "result_unknown_attempts", "active_attempts", "usage_unknown_attempts", "reported_tokens",
                "remaining_calls", "remaining_reported_tokens"}}
        public["details_omitted"] = True
        public["budget_fallback"] = True
    if size() > MAX_MODEL_CHARS:
        # Final hard stop on secondary fields. The remaining shape contains
        # only the fixed full-view pointer, two bounded ledgers and effective
        # admission facts; it cannot grow with paths or diagnostic collections.
        public = StatusSummary(view="summary", version=__version__,
            admin_enabled=public.get("admin_enabled"),
            admin_admission_enabled=public["admin_admission_enabled"],
            project_usage=public["project_usage"], global_usage=public["global_usage"],
            full_view=public["full_view"], details_omitted=True, budget_fallback=True)
    return public
