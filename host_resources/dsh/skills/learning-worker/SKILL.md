---
name: learning-worker
description: "Use Agent Learning project knowledge, work history, file associations and task state in DeepSeek Harness (DSH). Applies when the project memory tools are available."
---

# DSH worker knowledge protocol

Complete the current authorized task. Querying or adopting knowledge never expands its execution scope.

## Identity and task state

- Pass the current Hook's `context_handle` on every stateful call (`session_context`, `task_update`, `case_feedback`, `knowledge_use`, `work_memory_read`) and to `knowledge_search`, `knowledge_read` and `work_memory_search` to register actual exposure. Without a verified handle, use read-only project queries.
- For an existing task, read `session_context` and pass `expected_task_id` and `expected_write_version` to every `task_update`, including replacement with `new_task=true`. Start a new task only for genuinely new work. Resume an inactive task explicitly; this does not restore execution permissions.
- Child agents (`subagent` / `subagent_fork` / `workflow`) receive fixed assignments bound to the parent task snapshot. Read `session_context` to inspect the assigned scope, and never overwrite the parent task.

## Retrieval, work history and progressive reading

- Check applicability, limitations, source health and revision before using an entry. Search on a new issue or actual error if the injected summary is insufficient.
- `knowledge_search` returns previews. `knowledge_read` expands the chosen ID with `next_offset` and `expected_revision` for further pages.
- `work_memory_search` lists recorded work observations and file history (filterable by `file_path`). `read_required` means the index is not the full record; expand only relevant records whose index is insufficient. `work_memory_read` requires the current `context_handle`; continue pages with the same `section`, `expected_revision` and `next_offset`, rather than restarting with a larger budget. Reuse a complete returned version; `reread=true` starts a fresh presentation round, and normal continuation pages do not need that flag again. `file_check.complete=false` means file state was not fully checked, not that it stayed unchanged. Work observations are historical evidence, not published solutions or execution permissions.
- Record actual adoption with `knowledge_use`, supplying the exact knowledge revision, current task ID/revision/write version, a stable idempotency key and a genuine operation ID when available.

## Observable results and boundaries

- `PostToolUse` records observable tool outcomes, file relations and explicit errors. Background jobs (`run_in_background: true`) remain `running` until a verified final outcome is observed.
- Never include or claim access to internal model reasoning (`ReasoningBlock`).
- With stage review enabled, report a genuinely completed task or stage using `task_update(status="delivered")` with the current task ID/write version before the final public response. Runtime counts the parent delivery once and batches observable sources. Ordinary replies, unfinished tools and child stops do not count. Do not call the administrator or invent tasks to trigger review; unmarked public work remains searchable.
- Use `case_feedback` for concise professional facts or counterexamples (`feedback_kind="counterexample"`, `knowledge_id`, `knowledge_revision`). Workers do not publish or revoke knowledge.
- Historical ingestion requires explicit user confirmation (`history-prepare --confirm` / `history-run --confirm`).

See the exported [project README](../../README.md) for installation and supported capabilities.
