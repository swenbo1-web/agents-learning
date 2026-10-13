---
name: learning-admin
description: "Review bounded Agent Learning evidence and propose knowledge candidates or governance in a separately configured administrator session."
---

# Antigravity knowledge administrator

Use this skill only in a session that actually has the separate administrator MCP interface. The default native plugin exposes worker tools. A plugin installation alone does not configure an administrator model or start automatic review.

Review historical cases only after the user's explicit archival run. Do not scan, start, expand or resume historical jobs. Preserve source locations, versions and coverage; historical statements remain reported. Historical review never auto-publishes.

- Process assigned incremental review packets, not whole conversations or the entire project. Compare existing knowledge and candidates before proposing new knowledge.
- Keep the actual task snapshot, source IDs, exact revisions, applicability, limitations and missing evidence. Historical permissions do not authorize project actions.
- Distinguish reported facts, tool callback completion, explicit tool success, professional verification and user acceptance. An Antigravity callback without an error does not establish a command's final success.
- Submit candidates with the assigned `problem_id`, `problem_revision` and real `evidence_refs`. Unknown results remain unknown; repeated citations of one event do not become independent evidence.
- Propose governance with exact target ID and `expected_revision`. Candidate creation or case review does not resolve a counterexample. Publication, revocation, restoration, counterexample dismissal and maturity changes use trusted local entrances.
- Use bounded pagination with the packet's expected revision. A partial packet cannot support conclusions requiring missing evidence.
- Respect configured usage, time and concurrency budgets. Duplicate evidence reuses existing decisions. Failed or unknown administrator calls do not become successful review and cannot be retried under a fresh identity to reset usage.
- Automatic administrator execution currently requires an explicitly configured compatible executor; the Codex CLI adapter is not implicitly converted into an Antigravity model call. Do not invent CLI flags or credentials.
- Ordinary workers do not wait for review. Do not execute project tasks, modify host configuration or publish knowledge from this administrator skill.

See the exported [project README](../../README.md) for executor setup and governance boundaries.
