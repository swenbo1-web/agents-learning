"""Evidence facts shared by pending and published source assessments.

Recovery is evaluated within one cited source case. It never grants publication,
clears a counterexample, or lets success from another source repair a failure.
"""


def hard_source_issue(event):
    if (event.get("operation_conflict") or event.get("operation_decision") == "denied"
            or event.get("outcome") in {"conflict", "denied"}):
        return "source_operation_conflict"
    if event.get("role") in {"conflict", "feedback"}:
        return "source_unresolved_failure"
    return None


def tool_success(event):
    return (event.get("type") == "PostToolUse" and event.get("outcome") == "succeeded"
            and not event.get("operation_conflict") and event.get("operation_decision") != "denied")


def unresolved_failure(failed_sequence, succeeded_sequence):
    return bool(failed_sequence and failed_sequence >= succeeded_sequence)


def hard_source_sql():
    """Fixed internal aliases used by batched published-source authority reads."""
    return ("COALESCE(o.conflict,0)!=0 OR o.decision='denied' "
            "OR e.outcome IN ('conflict','denied') OR pe.role IN ('conflict','feedback')")


def tool_success_sql():
    return ("e.type='PostToolUse' AND e.outcome='succeeded' "
            "AND COALESCE(o.conflict,0)=0 AND COALESCE(o.decision,'')!='denied'")
