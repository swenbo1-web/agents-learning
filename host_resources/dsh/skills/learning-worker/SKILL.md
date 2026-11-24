---
name: learning-worker
description: "Track substantial tasks, consult project knowledge as needed and record results in a connected DeepSeek Harness (DSH) project with a current Hook binding."
---

# DSH project work

Proceed with the user's task. Use Agent Learning when the current project is connected and this invocation has a verified Hook `context_handle`. Without a binding, continue the task and use the read-only project entry for an explicit memory query or diagnostic.

## Task state and delivery

- Use the current Hook handle for conversation-bound calls. Register substantial new work with `task_update(new_task=true)` before the first write, execution or delegation. Continue an active task in place or use `resume=true` to resume the same inactive work after checking the request. Answer ordinary questions directly.
- For a write to an existing task, including replacement, pass `expected_task_id` and `expected_write_version` from the latest task receipt. Use `session_context` when these are missing or stale. Prior events and delegations keep their original task attribution.
- `subagent`, `subagent_fork` and `workflow` children use their fixed assignment to read the parent scope and report their own evidence.
- Mark an actual completed deliverable with `task_update(status="delivered")` before replying. With stage review enabled, the parent delivery is counted once after sealing.

## Read and record what matters

- Use available context first. Search published knowledge or work history when it will help the decision. Check applicability, revision and source health. Expand relevant indexes only when needed. Reuse complete versions within the current context and keep the returned section, revision and cursor on later pages.
- Keep background jobs `running` until there is a verified final outcome. Use `case_feedback` for key facts missing from captured events and for counterexamples to the exact knowledge revision. Record actual adoption with `knowledge_use` for the exact knowledge revision and task version. Keep the same idempotency key and parameters on a retry. Use observable outcomes and public evidence.
- Execution authority comes from the user and host. If Runtime is unavailable in observe mode, continue the authorized task and note any pending sync as needed. Stop the affected operation on an enforcement denial. Publishing, revocation and explicit historical ingestion use their dedicated entries.

See the exported [project README](../../README.md) for host capabilities and detailed tool contracts.
