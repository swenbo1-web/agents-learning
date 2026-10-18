"""Snapshot-local history totals; shared cases are evaluated once per read.

These are presentation facts, never execution or publication permissions.
No result is cached across transactions or persisted as authoritative state.
"""
from __future__ import annotations

from collections import Counter, defaultdict


TERMINAL = {"candidate_ready", "proposal_ready", "skipped", "published"}
DECISIONS = {"candidate", "governance", "skip", "needs_evidence"}


def progress_snapshots(runtime, rows):
    if not rows:
        return {}
    db, project = runtime.db, rows[0]["project"]
    ids = [row["id"] for row in rows]
    if any(row["project"] != project for row in rows):
        raise ValueError("history progress must belong to one project")
    slots = ",".join("?" for _ in ids)
    links = defaultdict(list)
    valid = defaultdict(set)
    for row in db.execute(
            "SELECT l.job_id,l.problem_id,u.id AS unit,s.material_id AS material "
            "FROM history_case_links l LEFT JOIN history_units u ON u.id=l.unit_id "
            "LEFT JOIN history_job_sources s ON s.job_id=l.job_id AND s.material_id=u.material_id "
            f"WHERE l.project=? AND l.job_id IN ({slots})", (project, *ids)):
        links[row["job_id"]].append(row["problem_id"])
        if row["unit"] and row["material"]:
            valid[row["job_id"]].add(row["problem_id"])
    selected = {problem for members in links.values() for problem in members}
    problems = {}
    if selected:
        for row in db.execute(
                "SELECT p.id,p.revision,r.state,r.revision AS review_revision FROM problems p "
                "LEFT JOIN review_states r ON r.project=p.project AND r.problem=p.id WHERE p.project=?",
                (project,)):
            if row["id"] in selected:
                problems[row["id"]] = dict(row)
    candidates = defaultdict(Counter)
    current_candidates = set()
    for row in db.execute(
            "SELECT problem,problem_revision,status,COUNT(*) AS n FROM candidates WHERE project=? "
            "GROUP BY problem,problem_revision,status", (project,)):
        problem = problems.get(row["problem"])
        if problem:
            candidates[row["problem"]][row["status"]] += row["n"]
            if row["problem_revision"] == problem["revision"]:
                current_candidates.add(row["problem"])
    receipts, decisions, needs_evidence = set(), set(), set()
    for row in db.execute(
            "SELECT problem,revision,decision FROM review_receipts WHERE project=?", (project,)):
        problem = problems.get(row["problem"])
        if problem and problem["revision"] == row["revision"]:
            receipts.add(row["problem"])
            if row["decision"] in DECISIONS:
                decisions.add(row["problem"])
            if row["decision"] == "needs_evidence":
                needs_evidence.add(row["problem"])
    attempted, blocked = set(), {}
    for row in db.execute(
            "SELECT a.problem,a.revision,a.id,a.state,a.retry_allowed,"
            "EXISTS(SELECT 1 FROM review_receipts r WHERE r.project=a.project AND r.call_id=a.id) AS accepted "
            "FROM admin_calls a WHERE a.project=? AND a.state NOT IN ('not_started','deduplicated') "
            "ORDER BY a.created,a.id", (project,)):
        problem = problems.get(row["problem"])
        if problem and problem["revision"] == row["revision"]:
            attempted.add(row["problem"])
            if not row["retry_allowed"] and not row["accepted"]:
                blocked[row["problem"]] = {"problem_id": row["problem"], "call_id": row["id"], "state": row["state"]}
    sources = {row["job_id"]: (row["n"], row["done"] or 0) for row in db.execute(
        "SELECT job_id,COUNT(*) AS n,SUM(materialized=1) AS done FROM history_job_sources "
        f"WHERE job_id IN ({slots}) GROUP BY job_id", ids)}
    calls = {row["job_id"]: row["n"] for row in db.execute(
        "SELECT job_id,COUNT(*) AS n FROM history_call_links "
        f"WHERE job_id IN ({slots}) AND refunded=0 GROUP BY job_id", ids)}
    results = {}
    for identity in ids:
        members = links[identity]
        counts = Counter(cases=len(members), sources=sources.get(identity, (0, 0))[0],
                         materialized_sources=sources.get(identity, (0, 0))[1], calls=calls.get(identity, 0))
        dispositions, candidate_states, blocked_items = Counter(), Counter(), []
        for problem_id in members:
            problem = problems.get(problem_id)
            statuses = candidates[problem_id]
            counts["candidates"] += sum(statuses.values())
            counts["published_candidates"] += statuses["published"]
            counts["candidate_cases"] += bool(statuses)
            counts["published_cases"] += bool(statuses["published"])
            candidate_states.update(statuses)
            if not problem:
                continue
            current = problem["revision"] == problem["review_revision"]
            counts["completed_review_cases"] += current and problem["state"] in TERMINAL
            counts["reviewed_cases"] += problem_id in decisions
            counts["review_decision_cases"] += problem_id in decisions
            counts["awaiting_evidence_cases"] += current and problem["state"] == "awaiting_evidence"
            counts["untouched_cases"] += not statuses and problem_id not in receipts and problem_id not in attempted
            if problem_id not in valid[identity]:
                continue
            if problem_id in blocked:
                dispositions["blocked"] += 1
                blocked_items.append(blocked[problem_id])
            elif problem_id in current_candidates or current and problem["state"] in TERMINAL:
                dispositions["completed"] += 1
            elif current and problem["state"] == "awaiting_evidence" and problem_id in needs_evidence:
                dispositions["awaiting_evidence"] += 1
            else:
                dispositions["ready"] += 1
        for key in ("candidates", "published_candidates", "candidate_cases", "published_cases", "calls",
                    "completed_review_cases", "reviewed_cases", "review_decision_cases", "awaiting_evidence_cases", "untouched_cases"):
            counts.setdefault(key, 0)
        counts["pending_cases"] = max(0, counts["cases"] - counts["completed_review_cases"])
        results[identity] = {"counts": dict(counts), "dispositions": dispositions,
                             "candidate_states": candidate_states, "blocked": blocked_items,
                             "queue_total": sum(dispositions.values())}
    return results
