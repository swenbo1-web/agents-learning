"""Read-only presentation of execution observations, never recovery authority."""
from __future__ import annotations


TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "conflicted", "unknown"})
_LOCAL_FAILURES = frozenset({"executor_capability_failed", "executor_preparation_failed",
    "executor_initialization_failed", "executor_execution_failed", "executor_output_failed",
    "executor_nonzero_exit", "executor_response_invalid", "executor_decision_uncommitted",
    "executor_timeout", "review_context_changed"})


def provider_failure_diagnostic(usage):
    """An earlier provider error cannot explain a later local reply rejection."""
    from .admin_diagnostics import safe_diagnostic
    usage = usage if isinstance(usage, dict) else {}
    execution = usage.get("execution")
    execution = execution if isinstance(execution, dict) else {}
    phase = execution.get("phase")
    if execution.get("response_collected") is True or isinstance(phase, str) and phase in {
            "response_validation", "decision_commit"}:
        return None
    return safe_diagnostic(execution.get("provider_diagnostic")) or safe_diagnostic(usage.get("provider_diagnostic"))


def review_failure_reason(usage):
    diagnostic = provider_failure_diagnostic(usage)
    if diagnostic:
        return diagnostic["reason_code"]
    execution = usage.get("execution") if isinstance(usage, dict) else None
    execution = execution if isinstance(execution, dict) else {}
    reason = execution.get("reason_code")
    if isinstance(reason, str) and reason in _LOCAL_FAILURES:
        return reason
    returncode = execution.get("returncode")
    return ("reviewer_process_failed" if type(returncode) is int and returncode != 0
            else "historical_failure_details_unavailable")


def execution_observations(usage):
    """Missing fields in old attempts remain unknown; legacy terminal means differ."""
    usage = usage if isinstance(usage, dict) else {}
    execution = usage.get("execution")
    execution = execution if isinstance(execution, dict) else {}
    observed = usage.get("observations")
    observed = observed if isinstance(observed, dict) else {}
    source = execution if "protocol_terminal_observed" in execution else observed
    terminal = source.get("protocol_terminal_observed")
    terminal = terminal if type(terminal) is bool else None
    status = source.get("protocol_terminal_status")
    status = status if isinstance(status, str) and status in TERMINAL_STATUSES else "unknown"
    if terminal is not True:
        if status != "unknown":
            terminal = None
        status = "unknown"
    response = execution.get("response_collected")
    response = response if type(response) is bool else None
    returncode = execution.get("returncode")
    returncode = returncode if type(returncode) is int and -(2**31) <= returncode < 2**31 else None
    return {"protocol_terminal_observed": terminal, "protocol_terminal_status": status,
            "response_collected": response, "returncode": returncode}
