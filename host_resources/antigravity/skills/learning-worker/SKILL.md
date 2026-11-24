---
name: learning-worker
description: "Track substantial tasks, consult project knowledge as needed and record results in a connected Antigravity project with a current Hook binding."
---

# Antigravity project work

Proceed with the user's task. Use Agent Learning when the current project is connected and this invocation has a verified Hook `context_handle`. Without a binding, continue the task and use the read-only project entry for an explicit memory query or diagnostic.

## Task state and delivery

- Use the current Hook handle for conversation-bound calls. Register substantial new work with `task_update(new_task=true)` before the first write, execution or delegation. Continue an active task in place or use `resume=true` to resume the same inactive work after checking the current request. Answer ordinary questions directly.
- For any write to an existing task, including replacement, pass `expected_task_id` and `expected_write_version` from the latest task receipt. Use `session_context` when these are missing or stale. Check current state before retrying an uncertain write. Host rebinding supplies a new handle on `reconnect_required`. Children read the assigned scope and report their own evidence.
- Register the actual goal and scope before executing a changed request. PreInvocation can supply context and may omit the prompt. Prior events and delegations keep their original task attribution.
- Mark an actual completed deliverable with `task_update(status="delivered")` before replying. With stage review enabled, the parent Stop seals that delivery. PostInvocation supplies partial public history.

## Read and record what matters

- Use the available context first. Search published knowledge or work history when it will help the current decision. Check applicability, revision and source health. Expand relevant indexes only when needed. Reuse complete versions within the current context and keep the returned section, revision and cursor on subsequent pages.
- Use `case_feedback` for key facts missing from captured events, with real `event_refs` and the task identity. Record actual adoption with `knowledge_use` for the exact knowledge revision and task version. Keep the same idempotency key and parameters on a retry. Distinguish a callback, confirmed tool success and user acceptance. Keep asynchronous results pending until verified.

## Host boundaries

- Execution authority comes from the user and host. Observe installations preserve Antigravity's permission policy. Enforce requests host review on passing checks and denies rejected actions. If Runtime is unavailable in observe mode, continue the authorized task and note any pending sync as needed.
- Treat public transcript recovery as evidence for its verified invocation and task. Ambiguous coverage stays unknown. History ingestion, publishing and administrator setup use their dedicated entries.

See the exported [project README](../../README.md) for host capabilities and detailed tool contracts.
