---
name: learning-admin
description: "Review bounded Agent Learning evidence and propose knowledge candidates, governance or optional work summaries in a separately configured DSH administrator session."
---

# DSH knowledge administrator

Use this skill only in a session that has the separate administrator MCP interface. The default worker interface does not expose administrator tools, and plugin installation alone does not enable automatic model review.

- Process assigned incremental review packets (`review_packet`), comparing existing knowledge and candidates before proposing new knowledge.
- Preserve the task snapshot, source IDs, exact revisions, applicability, limitations and missing evidence. Never include private model reasoning.
- Distinguish `reported` statements, `call_completed` protocol callbacks, verified `tool_success` outcomes and user acceptance.
- Submit candidates with `submit_candidate` and propose lifecycle actions with `propose_governance`. Publication, revocation, restoration, counterexample dismissal and maturity promotion use trusted local CLI entrances.
- For `purpose=work_summary`, follow the bounded work-summary contract: `category="project"`, `validation_level="reported"`, `proposal=null`. Summaries are saved as `model_reported` working history and never publish formal knowledge.

See the exported [project README](../../README.md) for DSH executor routing (`current`, `fixed`, `default`) and budget boundaries.
