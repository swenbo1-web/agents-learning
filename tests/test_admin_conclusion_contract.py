"""Conclusion-first wording is a prompt hint, not new evidence or review policy."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning import review_packet as prompts
from agent_learning.admin import _fingerprint
from agent_learning.admin_decisions import find_receipt, persist_receipt, review_signature
from agent_learning.mcp import CANDIDATE, validate
from agent_learning.phase_review import instruction as phase_instruction
from agent_learning.runtime import Runtime
from agent_learning.work_summaries import SUMMARY_PREFIX


ROOT = Path(__file__).resolve().parents[1]


def packet(purpose=None, host="codex"):
    value = {"problem": {"id": "case", "revision": 1, "category": "project"},
             "events": [{"id": "source", "health": "valid", "outcome": "reported",
                         "payload": {"statement": "Historical statement, not verified."}}],
             "related_knowledge": [], "related_candidates": [], "host_scope": host + ":default"}
    if purpose:
        value["purpose"] = purpose
    return value


class AdminConclusionPromptTests(unittest.TestCase):
    def test_all_hosts_and_review_purposes_share_one_hint_outside_evidence(self):
        for host in ("codex", "antigravity", "dsh"):
            for purpose in (None, "history_review", "phase_review", "work_summary"):
                with self.subTest(host=host, purpose=purpose):
                    source = packet(purpose, host)
                    frozen = deepcopy(source)
                    rendered = phase_instruction(source) if purpose == "phase_review" else prompts.review_instruction(source)
                    if purpose == "work_summary":
                        rendered = SUMMARY_PREFIX + rendered
                        self.assertIn("validation_level必须为reported", rendered)
                        self.assertIn("proposal必须为null", rendered)
                    self.assertEqual(rendered.count(prompts.CONTENT_LAYOUT_PREFIX), 1)
                    self.assertLess(rendered.index(prompts.CONTENT_LAYOUT_PREFIX), rendered.index("<untrusted_evidence>"))
                    visible = rendered.split("<untrusted_evidence>", 1)[1].rsplit("</untrusted_evidence>", 1)[0]
                    self.assertEqual(json.loads(visible), source)
                    self.assertEqual(source, frozen, "format guidance must not rewrite the frozen sources")

class AdminConclusionCompatibilityTests(unittest.TestCase):
    def setUp(self):
        root = ROOT / "build/local/admin-conclusion-contract"
        root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="case-", dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()
        self.runtime = Runtime(Path(self.temporary.name) / "home", config={})
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.project(self.workspace)
        self.runtime.task_update(self.project, "chat", "Review supplied observations")
        self.problem = self.runtime.feedback(self.project, "chat", "Source-reported project convention", category="project")
        self.packet = self.runtime.review_packet(self.project, self.problem["id"])

    def test_format_hint_does_not_invalidate_old_decisions_or_start_a_call(self):
        cfg = self.runtime.config["admin"]
        evidence = _fingerprint(self.packet)
        with patch.object(prompts, "CONTENT_LAYOUT_PREFIX", ""):
            old_instruction = prompts.review_instruction(self.packet)
            old_signature = review_signature(self.packet, cfg, evidence)
        self.assertNotEqual(old_instruction, prompts.review_instruction(self.packet))
        self.assertEqual(old_signature, review_signature(self.packet, cfg, evidence))
        persist_receipt(self.runtime, self.project, self.packet, evidence, old_signature, None,
                        {"decision": "skip", "reason": "No new facts", "candidate": None})
        self.assertEqual(find_receipt(self.runtime, self.project, old_signature)["decision"], "skip")
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_legacy_content_without_headings_remains_valid_and_is_not_rewritten(self):
        body = {"problem_id": self.problem["id"], "problem_revision": self.problem["revision"],
                "title": "Historical convention", "content": "An existing source-reported statement without headings.",
                "category": "project", "knowledge_kind": "fact", "applicability": "This project only",
                "limitations": "No independent verification", "validation_level": "reported",
                "evidence_refs": [event["id"] for event in self.packet["events"]]}
        validate(body, CANDIDATE)
        result = self.runtime.submit_candidate(self.project, **body)
        stored = self.runtime.db.execute("SELECT body FROM candidates WHERE id=?", (result["candidate_id"],)).fetchone()[0]
        self.assertEqual(json.loads(stored)["content"], body["content"])
        self.assertEqual(json.loads(stored)["validation_level"], "reported")
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
