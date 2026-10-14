---
name: learning-review
description: "Open the current project's local visual knowledge review console, inspect candidates and evidence, and let the user approve publication, reject or defer selected entries. Applies to knowledge decisions, not task execution approvals."
---

# Visual knowledge review

Open the console for the selected Antigravity project. The user previews candidates and chooses approve-and-publish, reject or defer in the page. Opening the console does not approve any candidate. Do not publish all candidates on the user's behalf or call a model to prepare them again.

## Use the installed runtime

- Verify the intended absolute project path, host and knowledge directory against this installation's bindings. Read `python` and `runtime_directory` from its `runtime-release.json`, or the effective MCP configuration's fixed entry. Use that runtime's `scripts/learning.py` rather than the shell working directory, a source checkout or another project's installation. Preserve any custom `ALP_CONFIG` setting.
- With the verified paths, run:

  ```text
  <PYTHON> -B <RUNTIME>/scripts/learning.py --workspace <PROJECT_ABSOLUTE_PATH> --home <KNOWLEDGE_HOME> --host antigravity browser --review --open --language en
  ```

  Use `--language zh-CN` for Simplified Chinese. First check this entry's `browser --help` for `--review`; an older installed runtime must be updated instead of silently using another checkout or bypassing review.
- Keep the temporary loopback service running in a managed terminal or process session. Hide the window when starting a Windows background process. Keep the access token out of project documents and persistent logs.

## Let the user decide

- Present complete candidate content, applicability, limitations, evidence level, sources and publication checks before the user selects one entry or a fixed batch. Page actions are performed by local code and do not invoke a model.
- Historical facts retain their `reported` evidence level after publication. Publication does not assert a successful current test or user acceptance. A changed source, stale candidate or version conflict requires refreshed details and a new selection; do not bypass the checks.
- Report only the actual per-item receipts. Opening a page, clicking an action or a successful tool exit does not establish publication. Knowledge review grants no tool-execution permission, starts no history import or administrator model and does not revoke published knowledge.
- For read-only browsing, use `learning-browser` without review mode.
