---
name: learning-panel
description: "Open the installed Agent Learning control panel in Antigravity App to inspect this project's knowledge and administrator status and use local visual controls."
---

# Agent Learning control panel

Use the panel for local status, administrator frequency, and links to knowledge and approval. It opens beside the conversation in Antigravity App 2.22+; it is not a custom button in the chat input toolbar.

- Check this project's installed `runtime-release.json`: `target_host` must be `antigravity`, its fixed workspace must match the current single project folder, and its `sidecars/control/sidecar.json` must declare an auxiliary pane. Preserve its runtime and knowledge directory bindings.
- The App discovers UI Sidecars globally. Project core installation alone does not register a sidebar. Verify that the separate `agent-learning-ui` UI-only plugin explicitly registers this installation and that `agent-learning-ui/control` is enabled and running, using the host's available customization tools or the user's visible Extensions menu. Do not scan other projects, register projects, or change host configuration just to open the panel. If missing, explain the README `install-ui` step and use the local browser fallback.
- When verified, provide `[Agent Learning](sidecar://agent-learning-ui/control/)`, or direct the user to the current conversation's **⋮ → Extensions → Agent Learning**. An older `agent-learning/control` link is valid only if that specific Sidecar is confirmed running and bound to this project. Let the user open it. Subsequent button operations do not require a new model turn.
- The **Check connection** button verifies the installation, directories and administrator budget. A native UI pipe alone cannot prove this conversation's Hook or MCP connection: use [learning-debug](../learning-debug/SKILL.md) for that evidence; do not describe all core features as verified merely because the panel opened.
- In Antigravity CLI, older App versions, an unsupported IDE surface, or a project that lacks this installation, explain the unavailable native panel and use [learning-browser](../learning-browser/SKILL.md) when the user wants the knowledge page. For approval or administrator settings use [learning-review](../learning-review/SKILL.md).

Opening the panel enables no administrator and scans no history. Frequency changes use platform allowance, this project's budget and the separate `ALP_HOME` safeguard; the user confirms them in the page. History archival does not review or publish knowledge. Do not click confirmations or change settings merely to open the panel.
