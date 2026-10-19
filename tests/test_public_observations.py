"""Public statements are reported provenance, never tool success or acceptance."""
from pathlib import Path
import tempfile
import unittest

from agent_learning.admin_publication import publication_gate
from agent_learning.antigravity import AntigravityAdapter
from agent_learning.dsh import DshAdapter
from agent_learning.public_observations import observed_public, reference_public
from agent_learning.runtime import Runtime


class PublicObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="public-observation-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "workspace"
        self.work.mkdir()

    def runtime(self, host="codex"):
        r = Runtime(self.root / (host + "-home"), config={}, host=host)
        self.addCleanup(r.close)
        return r, r.project(self.work)

    def gate(self, r, p, kind, payload, outcome="unknown"):
        event = r.record_event(p, "source", kind, payload, outcome=outcome)
        # Explicitly emulate an old immutable event, including its frozen
        # unknown outcome. The gate must not migrate it or its fingerprints.
        if outcome == "unknown":
            r.db.execute("UPDATE events SET outcome='unknown' WHERE id=?", (event["id"],))
        problem = r._problem(p, "source", "A sourced project observation", "project", [event["id"]])
        body = {"title": "Source bound project fact", "content": "This project uses SQLite",
                "category": "project", "knowledge_kind": "fact", "validation_level": "reported",
                "applicability": "This source version only", "limitations": "Statement, not verified execution",
                "evidence_refs": [event["id"]]}
        before = tuple(r.db.execute("SELECT payload,outcome,capture_digest,business_digest FROM events WHERE id=?",
                                    (event["id"],)).fetchone())
        reason = publication_gate(r, p, {"problem": problem}, body,
                                  {"action": "publish"}, mode="tiered")
        after = tuple(r.db.execute("SELECT payload,outcome,capture_digest,business_digest FROM events WHERE id=?",
                                   (event["id"],)).fetchone())
        self.assertEqual(before, after, "compatibility must not rewrite original source or digest")
        return reason

    def test_legacy_complete_native_public_fields_are_read_compatible(self):
        r, p = self.runtime()
        for kind, payload in (("UserPromptSubmit", {"prompt": "Project store uses SQLite"}),
                              ("Stop", {"last_assistant_message": "Project store uses SQLite"}),
                              ("PublicMessageSegment", {"text": "Project store uses SQLite", "completion": "complete"})):
            with self.subTest(kind=kind):
                self.assertIsNone(self.gate(r, p, kind, payload))

    def test_partial_truncated_empty_and_wrong_host_identity_do_not_gain_compatibility(self):
        r, p = self.runtime()
        base = {"last_assistant_message": "Project store uses SQLite"}
        for change in ({"completion": "partial"}, {"truncated": True},
                       {"last_assistant_message_truncated": True}, {"last_assistant_message": " "},
                       {"capture_coverage": {"finalModelOutput": {"truncated": True}}},
                       {"capture_coverage": {"finalModelOutput": {"full_original_available": False}}},
                       {"capture_coverage": {"truncated": True}},
                       {"capture_coverage": {"trajectory_identity": "ambiguous"}},
                       {"capture_coverage": {"workspace_association": "unknown_multi_workspace"}}):
            with self.subTest(change=change):
                self.assertIsNotNone(self.gate(r, p, "Stop", dict(base, **change)))

    def test_reported_custom_events_and_unknown_tools_are_not_public_provenance(self):
        r, p = self.runtime()
        for kind, payload, outcome in (("TaskUpdate", {"summary": "SQLite"}, "reported"),
                                       ("PostToolUse", {"tool_response": {"output": "SQLite"}}, "unknown"),
                                       ("PreToolUse", {"prompt": "SQLite"}, "reported"),
                                       ("PhaseRequestContext", {"prompt": "SQLite", "derived_context": True}, "reported")):
            with self.subTest(kind=kind):
                self.assertIsNotNone(self.gate(r, p, kind, payload, outcome))
        self.assertIsNone(self.gate(r, p, "Feedback", {"summary": "Project store uses SQLite"}, "reported"))
        self.assertIsNotNone(self.gate(r, p, "Feedback", {"summary": "Project store uses SQLite"}, "unknown"))

    def test_three_native_capture_paths_report_public_text_without_tool_verification(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                r, p = self.runtime(host)
                if host == "codex":
                    for kind, field in (("UserPromptSubmit", "prompt"), ("Stop", "last_assistant_message")):
                        r.handle_hook({"hook_event_name": kind, "session_id": "chat", "cwd": str(self.work),
                                       "turn_id": "1", field: "This project uses SQLite"})
                elif host == "dsh":
                    adapter = DshAdapter(r, self.work)
                    adapter.handle("UserPromptSubmit", {"sessionId": "chat", "turn": 1, "prompt": "This project uses SQLite"})
                    adapter.handle("Stop", {"sessionId": "chat", "turn": 1, "lastAssistantMessage": "This project uses SQLite"})
                else:
                    adapter = AntigravityAdapter(r, self.work)
                    common = {"conversationId": "chat", "workspacePaths": [str(self.work)]}
                    adapter.handle("PreInvocation", dict(common, invocationNum=0, initialNumSteps=0,
                                                         lastUserInput="This project uses SQLite"))
                    adapter.handle("Stop", dict(common, executionNum=0, fullyIdle=True, terminationReason="model_stop",
                                                finalModelOutput="This project uses SQLite"))
                rows = r.db.execute("SELECT type,outcome FROM events WHERE project=? AND type IN ('UserPromptSubmit','Stop')",
                                    (p,)).fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual({row["outcome"] for row in rows}, {"reported"})
                self.assertEqual(r.db.execute("SELECT count(*) FROM active").fetchone()[0], 0)
                self.assertEqual(r.db.execute("SELECT count(*) FROM admin_calls").fetchone()[0], 0)

    def test_partial_statement_is_reported_but_not_reference_eligible(self):
        payload = {"last_assistant_message": "A public partial observation", "completion": "partial"}
        self.assertTrue(observed_public("Stop", payload))
        self.assertFalse(reference_public("Stop", payload, "reported"))
        self.assertFalse(observed_public("PostToolUse", {"prompt": "not a user message"}))


if __name__ == "__main__":
    unittest.main()
