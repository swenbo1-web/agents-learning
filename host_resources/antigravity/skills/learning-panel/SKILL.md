---
name: learning-panel
description: "Open the installed Agent Learning control panel in Antigravity App to inspect this project's knowledge and administrator status and use local visual controls."
---

# Agent Learning control panel

Use the panel for local status, administrator frequency, and links to knowledge and approval. It opens beside the conversation in Antigravity App 2.22+; it is not a custom button in the chat input toolbar.

- Check this project's installed `runtime-release.json`: `target_host` must be `antigravity`, its fixed workspace must match the current single project folder, and its `sidecars/control/sidecar.json` must declare an auxiliary pane. Preserve its runtime and knowledge directory bindings.
- Verify the plugin and `agent-learning/control` UI sidecar are enabled and running using the host's available customization tools or the user's visible Extensions menu. If their state is unknown, tell the user where to check; do not invent a running panel or silently change global host configuration.
- When verified, provide `[Agent Learning](sidecar://agent-learning/control/)`, or direct the user to the current conversation's **⋮ → Extensions → Agent Learning**. Let the user open it. The link opens the panel; subsequent button operations do not require a new model turn.
- In Antigravity CLI, older App versions, an unsupported IDE surface, or a project that lacks this installation, explain the unavailable native panel and use [learning-browser](../learning-browser/SKILL.md) when the user wants the knowledge page. For approval or administrator settings use [learning-review](../learning-review/SKILL.md).

Opening the panel enables no administrator and scans no history. Frequency changes use the platform model allowance and the daily budget shared by the entire `ALP_HOME`; the user confirms them in the page. History archival does not review or publish knowledge. Do not click confirmations or change settings merely to open the panel.
