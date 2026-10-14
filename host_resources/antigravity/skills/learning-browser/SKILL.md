---
name: learning-browser
description: "Open and use the local read-only visual knowledge browser (127.0.0.1) to search published knowledge, inspect applicability, limitations, source health, revisions, event timelines and administrator queues."
---

# Visual knowledge browser

The local knowledge browser listens only on `127.0.0.1` and reads a consistent read-only SQLite snapshot for the selected project. It makes no model or embedding calls, never mutates the database and does not record model exposures.

- Verify the current project, host and knowledge directory. Obtain the actual Python interpreter and fixed runtime entry from this installation's `runtime-release.json` or active MCP configuration; preserve its `ALP_CONFIG`. Do not assume the terminal cwd, development checkout or default data directory.
- Run `<PYTHON> -B <RUNTIME>/scripts/learning.py --workspace <PROJECT> --home <KNOWLEDGE_HOME> --host antigravity browser --open` with those verified values. Keep the temporary local service running; hide background process windows on Windows.
- Let `--open` open the launch URL directly. Do not echo or persist output containing access tokens. The fragment is cleared on page load and the token stays in process and page memory only.
- For candidate approval or administrator settings, use `learning-review`. Read-only viewing does not add `--review` or `--admin-control`.
