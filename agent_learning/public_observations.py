"""Classify observable public statements without treating them as verification.

Legacy native hooks saved real public text with outcome=unknown.  Compatibility
is evaluated at the read gate; neither original events nor frozen hashes change.
"""
from __future__ import annotations


_PUBLIC_FIELDS = {
    "UserPromptSubmit": "prompt",
    "Stop": "last_assistant_message",
    "PublicMessageSegment": "text",
    "PhaseRequestContext": "prompt",
}


def observed_public(kind, payload, *, require_complete=False):
    """True means a supplied public statement exists, never that it is correct."""
    if not isinstance(payload, dict) or kind not in _PUBLIC_FIELDS:
        return False
    text = payload.get(_PUBLIC_FIELDS[kind])
    if not isinstance(text, str) or not text.strip():
        return False
    coverage = payload.get("capture_coverage", {})
    if not isinstance(coverage, dict) or len(coverage) > 16:
        return False
    if coverage.get("workspace_association", "installed_workspace") != "installed_workspace":
        return False
    if coverage.get("trajectory_identity", "current") != "current":
        return False
    if not require_complete:
        return True
    if payload.get("completion", "complete") != "complete":
        return False
    if coverage.get("truncated") or coverage.get("full_original_available") is False:
        return False
    if any(payload.get(key) for key in ("truncated", "message_truncated", "prompt_truncated",
                                       "last_assistant_message_truncated")):
        return False
    for item in coverage.values():
        if isinstance(item, dict) and (item.get("truncated") or item.get("available") is False
                                      or item.get("full_original_available") is False):
            return False
    return True


def reference_public(kind, payload, outcome):
    """Bounded legacy compatibility for complete public evidence, not tools.

Derived request context remains useful to the reviewer but cannot independently
authorize automatic reference publication. Explicit Feedback keeps its original
reported-only contract; arbitrary event types do not become public evidence.
"""
    if not isinstance(payload, dict):
        return False
    if kind == "Feedback":
        return (outcome == "reported" and isinstance(payload.get("summary"), str)
                and bool(payload["summary"].strip()))
    return (kind in {"UserPromptSubmit", "Stop", "PublicMessageSegment"}
            and outcome in {"reported", "unknown"}
            and observed_public(kind, payload, require_complete=True))
