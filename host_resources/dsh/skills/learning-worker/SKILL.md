---
name: learning-worker
description: "Use Agent Learning project knowledge, work history, file associations and task state in DeepSeek Harness (DSH). Applies when the project memory tools are available."
---

# DSH worker knowledge protocol

Complete the current authorized task. Querying or adopting knowledge never expands its execution scope.

## Identity and task state

- Pass the current Hook's `context_handle` on every stateful call (`session_context`, `task_update`, `case_feedback`, `knowledge_use`, `work_memory_read`) and to `knowledge_search`, `knowledge_read` and `work_memory_search` to register actual exposure. Without a verified handle, use read-only project queries.
- For an existing task, read `session_context` and pass `expected_task_id` and `expected_write_version` to `task_update`. Use `new_task=true` only for a genuinely new task. Resume an inactive task explicitly; this does not restore execution permissions.
- Child agents (`subagent` / `subagent_fork` / `workflow`) receive fixed assignments bound to the parent task snapshot. Read `session_context` to inspect the assigned scope, and never overwrite the parent task.

## Retrieval, work history and progressive reading

- Check applicability, limitations, source health and revision before using an entry. Search on a new issue or actual error if the injected summary is insufficient.
- `knowledge_search` returns previews. `knowledge_read` expands the chosen ID with `next_offset` and `expected_revision` for further pages.
- `work_memory_search` lists recorded work observations and file history (filterable by `file_path`); `work_memory_read` requires the current `context_handle` and revision-bound pagination (`section="summary"|"evidence"`). Work observations are historical evidence, not published solutions or execution permissions.
- Record actual adoption with `knowledge_use`, supplying the exact knowledge revision, current task ID/revision/write version, a stable idempotency key and a genuine operation ID when available.

## Observable results and boundaries

- `PostToolUse` records observable tool outcomes, file relations and explicit errors. Background jobs (`run_in_background: true`) remain `running` until a verified final outcome is observed.
- Never include or claim access to internal model reasoning (`ReasoningBlock`).
- Use `case_feedback` for concise professional facts or counterexamples (`feedback_kind="counterexample"`, `knowledge_id`, `knowledge_revision`). Workers do not publish or revoke knowledge.
- Historical ingestion requires explicit user confirmation (`history-prepare --confirm` / `history-run --confirm`).

See the exported [project README](../../README.md) for installation and supported capabilities.
