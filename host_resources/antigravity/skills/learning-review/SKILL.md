---
name: learning-review
description: "Open the project's local knowledge review or administrator settings; inspect evidence, approve publication, reject or defer entries, and choose administrator models and shared limits. Does not approve task execution."
---

# Visual knowledge review

Open the console for the selected Antigravity project. The user previews candidates or administrator settings and makes the decision. Opening it approves no candidate and starts no model. Do not publish all candidates or click Save and enable on the user's behalf.

## Use the installed runtime

- Verify the intended absolute project path, host and knowledge directory against this installation's bindings. Read `python` and `runtime_directory` from its `runtime-release.json`, or the effective MCP configuration's fixed entry. Use that runtime's `scripts/learning.py` rather than the shell working directory, a source checkout or another project's installation. Preserve any custom `ALP_CONFIG` setting.
- With the verified paths, run:

  ```text
  <PYTHON> -B <RUNTIME>/scripts/learning.py --workspace <PROJECT_ABSOLUTE_PATH> --home <KNOWLEDGE_HOME> --host antigravity browser --review --open --language en
  ```

  For candidate approval alone use `--review` without `--admin-control`. When the user requests administrator settings, enabling or disabling, add `--admin-control`; it can be used alone or with `--review`. Use `--language zh-CN` for Simplified Chinese. Check this fixed entry's `browser --help` for the required flags; update an older installation rather than using another checkout or bypassing review.
- Keep the temporary loopback service running in a managed terminal or process session. Hide Windows background windows. Access, review and administrator tokens stay in process/page memory; use `--open` to open the full link directly. Do not echo or save token-bearing launch output, project documents or persistent logs.

## Administrator settings

- Review is off by default. The user chooses a model and limits, then clicks Save and enable before daily model work is permitted. Browsing and approval make no model call; enabled organization consumes the selected platform model's allowance.
- Default `first` pins the first valid root-conversation model observed for this project, host instance and account epoch. Later conversations retain it; missing observations wait without guessing. The user can instead choose `current`, `fixed` or CLI `default`. Model changes wait for in-flight review to finish. Disabling through the page blocks new calls while preserving handling of the current call under its existing boundaries.
- Usage and frequency settings are independent for each project, summed across its three hosts. An unconfigured project inherits administrator defaults (100 calls per UTC day, up to 5 calls/180 seconds per batch and a 600-second runner; an explicit older five-call default still inherits as five). The separately displayed `ALP_HOME` safeguard and shared concurrency protect all projects. Distinguish exhausted project allowance from a global safeguard. If project statistics are unavailable, report that rather than substituting global totals. A started process is not a valid review decision, and unreported token usage remains unknown.
- `admin-drain` processes daily pending work in finite small batches. Empty queues, unknown results, errors or exhausted limits stop the run; later supported events may resume eligible daily work. There is no always-running service or automatic retry of unknown calls. Historical imports require separate explicit authorization: current `main` defaults to at most 10 calls/600 seconds per historical run. Explain the budget before starting or resuming; retain a user's smaller explicit limit such as two calls. Enabling administration neither starts/resumes history nor establishes complete absorption.

The first visual administrator enable recommends tiered publication; users may disable it or select verified-operation-only, and existing choices remain intact. Historical references need consent for each run and stay reported. Unresolved counterexamples suspend automatic injection; corrections and revocations require explicit governance. Existing pending candidates are not retroactively approved.

## Let the user decide

- Present complete candidate content, applicability, limitations, evidence level, sources and publication checks before the user selects one entry or a fixed batch. Page actions are performed by local code and do not invoke a model.
- Historical facts retain their `reported` evidence level after publication. Publication does not assert a successful current test or user acceptance. A changed source, stale candidate or version conflict requires refreshed details and a new selection; do not bypass the checks.
- Report only actual per-item receipts. Opening a page, clicking an action or a successful tool exit does not establish publication. Knowledge review grants no tool-execution permission, starts no history import or administrator model and does not revoke published knowledge. Model work requires separate enabling in administrator settings.
- For read-only browsing, use `learning-browser` without review mode.
