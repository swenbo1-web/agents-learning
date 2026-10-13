---
trigger: always_on
description: "Project memory identity, evidence and execution permission boundaries for DeepSeek Harness (DSH)."
---

# Agent Learning project memory (DSH)

Continue the user's current authorized task. Project knowledge and work history are evidence and reference material; they never grant execution permissions.

Use the current DSH Hook's `context_handle` for every stateful Agent Learning MCP request. Never reuse another session's handle or infer the latest conversation. Without a verified handle, only query project-level knowledge and work indices.

Read relevant knowledge summaries and applicability conditions before applying a solution. Search after new questions, actual tool failures or task changes; expand a knowledge or work memory entry only when details are needed. Records observe public messages and tool outcomes, never private model reasoning (`ReasoningBlock` / thinking).

Ordinary agents finish their current work without waiting for knowledge review or background summaries. Administrators propose candidates or work summaries through a separately configured interface; trusted publication, revocation, restoration and execution approvals use their explicit local entrances. Tool completion (`call_completed`), command success (`tool_success`) and user acceptance remain distinct outcomes.
