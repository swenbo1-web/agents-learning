---
name: learning-browser
description: "Open and use the local read-only visual knowledge browser (127.0.0.1) to search published knowledge, inspect applicability, limitations, source health, revisions, event timelines and administrator queues."
---

# Visual knowledge browser

The local knowledge browser listens only on `127.0.0.1` and reads a consistent read-only SQLite snapshot for the selected project. It makes no model or embedding calls, never mutates the database and does not record model exposures.

- Launch from a terminal with `python -B scripts/learning.py --workspace "<PROJECT_PATH>" browser --open` (pass the matching `--home PATH` when using a custom data directory).
- In DSH, you can also invoke the native `learning_browser(action="start"|"status"|"stop", open=true)` tool or the `/learning-browser` slash command.
- The temporary access token is passed via URL fragment (`#token=...`) and cleared from the address bar immediately on load, remaining in page memory only.
