---
name: learning-debug
description: "Read-only Agent Learning connection and session checks for Antigravity, including the current project, knowledge directory, administrator model, limits and blockers. Use for integration debugging."
---

# Agent Learning debug

Use this skill when the user asks Antigravity to check Agent Learning. Native skill discovery or a natural-language request can select it; do not invent a slash command or permanent composer button.

- Follow the advertised tool schema and call `learning_status(view="summary")` once. Extract `project_id`, `home`, `host_scope`, `storage_schema_version`, `version`, administrator routing, `project_usage` and `global_usage`. Only use an argument-free call for an older installation whose schema has no `view`; request `view="full"` only when this diagnosis needs omitted fields or the summary marks `strings_truncated`. Never treat a shortened path or model as its full identity. Do not dump the full result, knowledge contents, handles or access codes. Missing tools mean the integration is unavailable; do not change installation or settings.
- Pass only the genuine `context_handle` injected by this invocation's Hook to `session_context`. Without one, read status only and report this invocation's Hook binding as unverified/missing its handle; an argument-free call could reuse an old connection binding. Never search for a latest conversation, reuse an old handle or invent an identity. A project mismatch, stale handle or reconnect error ends the binding check.
- Briefly report MCP response, current Hook binding, project and knowledge directory, actual `admin_admission_enabled` switch, `admin_routing.executor.model` and mode, `project_usage.counted_attempts`, valid `completed_reviews`, `failed_attempts` and project `remaining_calls`; list `global_usage` totals/protection separately with `admission.blocking_scope`, the UTC accounting day and local reset time. If project fields are absent, say project usage is unavailable; never relabel global counts as project counts. Unreported token usage remains unknown, and the relevant `admin_routing.reason`. Missing observations remain waiting. Older installations without the effective switch must label `admin_enabled` as a policy preference with actual admission unavailable. An enabled policy or available CLI proves neither a running worker nor an always-running service.
- Tools responding proves MCP connectivity. Only successful binding of this invocation's actual handle establishes its Hook connection. Old events, existing knowledge and an empty knowledge base establish neither success nor failure.
- Make no task, feedback, configuration or knowledge changes; launch no reviewer, retry, historical import or installation. For detailed knowledge use [learning-browser](../learning-browser/SKILL.md). For candidate approval or administrator settings use [learning-review](../learning-review/SKILL.md) only when requested.

Respond in the user's language with a short status checklist and one useful next step. Create no debugging document.
