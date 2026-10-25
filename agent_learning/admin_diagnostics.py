"""Bounded adapter-owned failure observations; never retain provider error text."""
from __future__ import annotations

import re


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
    "model_unavailable",
})
PARAMETERS = frozenset({"thinking_budget", "thinking_level", "reasoning_effort"})
FLAGS = frozenset({"stream_invoked", "http_response_observed", "usage_observed"})
FIELDS = frozenset({"version", "phase", "reason_code", "http_status", "parameter_code", "retry_policy", *FLAGS})

_CODEX_LINE_BYTES = 4096
_CODEX_CODES = {
    "model_not_found": "model_unavailable", "model_not_available": "model_unavailable",
    "unsupported_model": "model_unavailable", "invalid_api_key": "invalid_credential",
    "authentication_error": "invalid_credential", "invalid_authentication": "invalid_credential",
    "token_expired": "invalid_credential", "refresh_token_reused": "invalid_credential",
    "insufficient_quota": "provider_quota_exceeded", "quota_exceeded": "provider_quota_exceeded",
    "usage_limit_reached": "provider_quota_exceeded", "rate_limit_exceeded": "provider_quota_exceeded",
    "rate_limit_error": "provider_quota_exceeded", "invalid_json_schema": "invalid_response_schema",
    "invalid_response_schema": "invalid_response_schema", "invalid_output_schema": "invalid_response_schema",
    "invalid_argument": "invalid_arguments", "invalid_arguments": "invalid_arguments",
    "unrecognized_argument": "invalid_arguments", "unknown_feature": "invalid_arguments",
}
_MODEL = r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}"
_QUOTED_MODEL = rf"(?:'{_MODEL}'|`{_MODEL}`)"
_OPTION = r"[-A-Za-z0-9_ <>|=]{1,200}"
_CODEX_TEXT = tuple((reason, re.compile(pattern, re.IGNORECASE | re.ASCII)) for reason, pattern in (
    ("invalid_credential", r"Your authentication token has been (?:invalidated|expired)\. Please try signing in again\."),
    ("invalid_credential", r"Your access token could not be refreshed because your refresh token was already used\. Please log out and sign in again\."),
    ("invalid_credential", r"(?:You are not logged in\. Please run 'codex login'\.|Not logged in|Missing bearer or basic authentication in header)"),
    ("invalid_credential", r"Incorrect API key provided: [^\r\n]{1,512}\. You can find your API key at https://platform\.openai\.com/(?:account/)?api-keys\.?"),
    ("provider_quota_exceeded", r"You exceeded your current quota, please check your plan and billing details\.(?: For more information on this error, read the docs: https://platform\.openai\.com/docs/guides/error-codes/api-errors\.?)?"),
    ("provider_quota_exceeded", r"You've hit your usage limit\. (?:Please try again later|Try again at [A-Za-z0-9, :+/-]{1,80})\.?"),
    ("provider_quota_exceeded", r"Rate limit exceeded\. Please try again later\."),
    ("model_unavailable", rf"The (?:requested )?model {_QUOTED_MODEL} does not exist(?: or you do not have access to it)?\."),
    ("model_unavailable", rf"The (?:model {_QUOTED_MODEL}|{_QUOTED_MODEL} model) is not supported when using Codex with a ChatGPT account\."),
    ("invalid_response_schema", r"Invalid schema for response_format '[A-Za-z0-9_-]{1,100}': [^\r\n]{1,2048}"),
    ("invalid_response_schema", r"Invalid (?:JSON|output|response) schema: [^\r\n]{1,2048}"),
    ("invalid_arguments", rf"unexpected argument '{_OPTION}' found"),
    ("invalid_arguments", rf"a value is required for '{_OPTION}' but none was supplied"),
    ("invalid_arguments", rf"invalid value '[^'\r\n]{{1,200}}' for '{_OPTION}'(?:: [^\r\n]{{1,1000}})?"),
    ("invalid_arguments", r"the following required arguments were not provided:"),
    ("invalid_arguments", r"unrecognized subcommand '[A-Za-z0-9_-]{1,100}'"),
    ("invalid_arguments", r"Unknown feature: [A-Za-z0-9_-]{1,100}"),
))


def _codex_line(value):
    if isinstance(value, bytes):
        if len(value) > _CODEX_LINE_BYTES:
            return None
        try:
            value = value.decode("utf-8")
        except UnicodeError:
            return None
    if not isinstance(value, str) or len(value) > _CODEX_LINE_BYTES:
        return None
    try:
        if len(value.encode("utf-8")) > _CODEX_LINE_BYTES:
            return None
    except UnicodeError:
        return None
    value = value.removesuffix("\n").removesuffix("\r")
    return value if value and value.isprintable() else None


def _codex_diagnostic(reason, status=None):
    return {"version": 1, "phase": "stream", "reason_code": reason, "http_status": status,
            "parameter_code": None, "retry_policy": "no_automatic_retry", "stream_invoked": False,
            "http_response_observed": status is not None, "usage_observed": False}


def _codex_message_reason(message):
    message = _codex_line(message)
    if message is None:
        return None
    if message.startswith(("Error: ", "error: ")):
        message = message[7:]
    return next((reason for reason, pattern in _CODEX_TEXT if pattern.fullmatch(message)), None)


def codex_text_failure(line):
    """Recognize bounded, prefixed CLI errors; never retain or scan free text."""
    line = _codex_line(line)
    if line is None or not line.startswith(("Error: ", "error: ")):
        return None
    reason = _codex_message_reason(line)
    return _codex_diagnostic(reason) if reason else None


def codex_failure(event):
    """Map typed fields or fixed error templates, without retaining provider text."""
    if (not isinstance(event, dict) or not isinstance(event.get("type"), str)
            or event["type"] not in {"error", "turn.failed"}):
        return None
    error = event.get("error")
    sources = []
    if isinstance(error, dict):
        if isinstance(error.get("error"), dict):
            sources.append(error["error"])
        sources.append(error)
    sources.append(event)
    reason = next((_CODEX_CODES[value] for source in sources for key in ("code", "error_code", "type")
                   if isinstance(value := source.get(key), str) and value in _CODEX_CODES), None)
    status = next((value for source in sources for key in ("status_code", "http_status", "status")
                   if type(value := source.get(key)) is int and 100 <= value <= 599), None)
    if reason is None:
        reason = {401: "invalid_credential", 429: "provider_quota_exceeded"}.get(status)
    if reason is None:
        messages = [source.get("message") for source in sources]
        if isinstance(error, str):
            messages.insert(0, error)
        reason = next((value for message in messages if (value := _codex_message_reason(message))), None)
    return _codex_diagnostic(reason or "model_request_failed", status)


def merge_codex_diagnostic(current, incoming):
    """Keep the strongest independent observation; never combine error payloads."""
    def checked(value):
        value = safe_diagnostic(value)
        return value if value and value["retry_policy"] == "no_automatic_retry" and value["parameter_code"] is None else None

    current, incoming = checked(current), checked(incoming)
    if current is None:
        return incoming
    if incoming is None:
        return current
    def rank(value):
        return value["reason_code"] != "model_request_failed", value["http_status"] is not None
    return incoming if rank(incoming) > rank(current) else current


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
