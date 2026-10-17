"""Bounded adapter-owned failure observations; never retain provider error text."""
from __future__ import annotations


PHASES = frozenset({"arguments", "sdk_load", "profile", "runtime", "input", "stream", "response", "cleanup"})
REASONS = frozenset({
    "invalid_arguments", "sdk_unavailable", "sdk_incompatible", "dsh_profile_unavailable",
    "dsh_profile_changed", "dsh_profile_invalid", "qualified_model_required", "default_model_unavailable",
    "model_binding_invalid", "provider_unavailable", "readonly_host_configuration", "unexpected_host_capability",
    "model_compatibility_unavailable", "model_compatibility_invalid", "invalid_input", "input_limit_exceeded",
    "invalid_response_schema", "model_request_failed", "invalid_request_parameter", "unsupported_reasoning_effort",
    "provider_quota_exceeded", "invalid_credential", "model_timeout", "invalid_output_stream", "invalid_output_block",
    "invalid_output_text", "output_limit_exceeded", "output_block_mismatch", "tool_output_refused",
    "incomplete_model_response", "invalid_response_json", "invalid_response_object", "model_cleanup_failed",
})
PARAMETERS = frozenset({"thinking_budget", "thinking_level", "reasoning_effort"})
FLAGS = frozenset({"stream_invoked", "http_response_observed", "usage_observed"})
FIELDS = frozenset({"version", "phase", "reason_code", "http_status", "parameter_code", "retry_policy", *FLAGS})


def safe_diagnostic(value):
    """Reject untyped/extended diagnostics rather than copying arbitrary strings."""
    if (not isinstance(value, dict) or set(value) != FIELDS or type(value.get("version")) is not int
            or value["version"] != 1
            or any(not isinstance(value.get(key), str) for key in ("phase", "reason_code", "retry_policy"))
            or value.get("parameter_code") is not None and not isinstance(value.get("parameter_code"), str)
            or value.get("phase") not in PHASES
            or value.get("reason_code") not in REASONS
            or value.get("retry_policy") not in {"no_automatic_retry", "configuration_change"}
            or value.get("parameter_code") not in PARAMETERS | {None}
            or any(type(value.get(key)) is not bool for key in FLAGS)):
        return None
    status = value.get("http_status")
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        return None
    if value["http_response_observed"] != (status is not None):
        return None
    if value["retry_policy"] == "configuration_change" and not (
            (status == 400 or status is None and value["reason_code"] == "unsupported_reasoning_effort")
            and value["stream_invoked"] and value["phase"] == "stream"
            and value["reason_code"] in {"invalid_request_parameter", "unsupported_reasoning_effort"}
            and value["parameter_code"] in PARAMETERS):
        return None
    return dict(value)


def configuration_failure(value):
    diagnostic = safe_diagnostic(value)
    return bool(diagnostic and diagnostic["retry_policy"] == "configuration_change")
