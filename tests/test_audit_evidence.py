"""整合审查的事件可信度、恢复关联与全量摘要回归；仅临时 SQLite。"""
import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import _fingerprint, admin_once
from agent_learning.event_capture import business_evidence_digest, prepare_event
from agent_learning.runtime import ContractError, Runtime, digest
from tests.test_module6_queue import fake_reviewer


class AuditEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-audit-evidence-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "state"
        self.work = Path(self.temp.name) / "project"
        self.work.mkdir()
        self.r = Runtime(self.home, config={})
        self.addCleanup(lambda: self.r.close())
        self.p = self.r.project(self.work)
        self.r.task_update(self.p, "chat", "Review the same tool observation")

    def hook(self, kind, chat="chat", **values):
        return self.r.handle_hook(dict(hook_event_name=kind, session_id=chat, cwd=str(self.work), **values))

    def tool(self, ident, code, inputs, chat="chat", output=""):
        values = dict(tool_use_id=ident, tool_name="Bash", tool_input=inputs)
        self.hook("PreToolUse", chat, **values)
        self.hook("PostToolUse", chat, **values, tool_response=dict(exit_code=code, output=output))
        return self.r.db.execute("SELECT * FROM events WHERE type='PostToolUse' ORDER BY rowid DESC LIMIT 1").fetchone()

    def test_changed_input_or_tool_keeps_evidence_and_invalidates_success(self):
        for field, value in (("tool_input", {"command": "other"}), ("tool_name", "Read")):
            with self.subTest(field=field):
                chat = field
                self.r.task_update(self.p, chat, "Review the same tool observation")
                event = self.tool(field, 0, {"command": "build"}, chat)
                problem = self.r.feedback(self.p, chat, "Tool success source", event_refs=[event["id"]])
                body = dict(problem_id=problem["id"], problem_revision=problem["revision"],
                            title="build evidence " + field, content="The recorded build completed",
                            applicability="This recorded command", limitations="No product acceptance claim",
                            category="operation", validation_level="tool_success", evidence_refs=[event["id"]])
                candidate = self.r.submit_candidate(self.p, **body)
                published = self.r.publish(self.p, candidate["candidate_id"])
                values = dict(tool_use_id=field, tool_name="Bash", tool_input={"command": "build"},
                              tool_response={"exit_code": 0, "output": ""})
                values[field] = value
                self.hook("PostToolUse", chat, **values)
                operation = self.r.db.execute("SELECT * FROM operations WHERE conversation=?", (chat,)).fetchone()
                self.assertEqual((operation["state"], operation["conflict"]), ("conflict", 1))
                self.assertEqual(self.r.read(self.p, published["knowledge_id"])["source_health"], "conflicted")
                self.assertFalse(self.r.read(self.p, published["knowledge_id"])["current_tool_success"])
                count = self.r.db.execute("SELECT COUNT(*) FROM events WHERE conversation=? AND type='PostToolUse'", (chat,)).fetchone()[0]
                self.assertEqual(count, 2)
                self.hook("PostToolUse", chat, **values)
                self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE conversation=? AND type='PostToolUse'", (chat,)).fetchone()[0], count)
                problem = self.r.db.execute("SELECT revision FROM problems WHERE id=?", (problem["id"],)).fetchone()
                with self.assertRaises(ContractError):
                    self.r.submit_candidate(self.p, **dict(body, title="new invalid claim", problem_revision=problem[0]))

    def test_post_only_operation_detects_input_conflict(self):
        values = dict(tool_use_id="orphan", tool_name="Bash", tool_response={"exit_code": 0})
        self.hook("PostToolUse", **values, tool_input={"command": "build"})
        self.hook("PostToolUse", **values, tool_input={"command": "pwd"})
        self.assertEqual(self.r.db.execute("SELECT state FROM operations").fetchone()[0], "conflict")

    def test_legacy_identical_replay_stays_idempotent_but_changed_input_is_saved(self):
        event = self.tool("legacy", 0, {"command": "build"})
        key = ["chat", None, "PostToolUse", "legacy", digest({"exit_code": 0, "output": ""})]
        legacy_id = digest([self.p, [self.r.host_epoch(), key]])
        self.r.db.execute("UPDATE events SET id=? WHERE id=?", (legacy_id, event["id"]))
        values = dict(tool_use_id="legacy", tool_name="Bash", tool_response={"exit_code": 0, "output": ""})
        self.hook("PostToolUse", **values, tool_input={"command": "build"})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='PostToolUse'").fetchone()[0], 1)
        self.hook("PostToolUse", **values, tool_input={"command": "other"})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='PostToolUse'").fetchone()[0], 2)
        self.assertEqual(self.r.db.execute("SELECT state FROM operations").fetchone()[0], "conflict")

    def test_legacy_complete_replay_without_capture_digest_is_idempotent(self):
        event = self.tool("legacy", 0, {"command": "build"})
        key = ["chat", None, "PostToolUse", "legacy", digest({"exit_code": 0, "output": ""})]
        legacy_id = digest([self.p, [self.r.host_epoch(), key]])
        self.r.db.execute("UPDATE events SET id=?,capture_digest=NULL WHERE id=?", (legacy_id, event["id"]))
        self.hook("PostToolUse", tool_use_id="legacy", tool_name="Bash", tool_input={"command": "build"},
                  tool_response={"exit_code": 0, "output": ""})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='PostToolUse'").fetchone()[0], 1)
        self.hook("PostToolUse", tool_use_id="legacy", tool_name="Bash", tool_input={"command": "changed"},
                  tool_response={"exit_code": 0, "output": ""})
        self.assertEqual(self.r.db.execute("SELECT state FROM operations").fetchone()[0], "conflict")

    def test_unrelated_command_and_shared_cwd_do_not_mask_actual_recovery(self):
        inputs = dict(command="build", cwd=str(self.work))
        self.tool("fail", 1, inputs)
        problem = self.r.problems(self.p)[0]
        self.tool("unrelated", 0, dict(command="pwd", cwd=str(self.work)))
        self.assertEqual(self.r.problems(self.p)[0]["revision"], problem["revision"])
        self.tool("retry", 0, inputs)
        current = self.r.problems(self.p)[0]
        self.assertGreater(current["revision"], problem["revision"])
        commands = [json.loads(row[0])["tool_input"]["command"] for row in self.r.db.execute(
            "SELECT e.payload FROM problem_events pe JOIN events e ON e.id=pe.event "
            "WHERE pe.problem=? AND pe.role='potential_recovery'", (problem["id"],))]
        self.assertEqual(commands, ["build"])

    def test_legacy_unrelated_recovery_does_not_prevent_related_new_evidence(self):
        self.tool("fail", 1, dict(command="build"))
        problem = self.r.problems(self.p)[0]
        unrelated = self.tool("unrelated", 0, dict(command="pwd"))
        with self.r.transaction():
            self.r._attach_case_evidence(problem["id"], [(unrelated["id"], "potential_recovery")])
        revision = self.r.problems(self.p)[0]["revision"]
        self.tool("retry", 0, dict(command="build"))
        self.assertGreater(self.r.problems(self.p)[0]["revision"], revision)

    def test_conflicted_prior_recovery_cannot_mask_new_valid_success(self):
        self.tool("fail", 1, dict(command="build"))
        problem = self.r.problems(self.p)[0]
        self.tool("first-recovery", 0, dict(command="build"))
        self.hook("PostToolUse", tool_use_id="first-recovery", tool_name="Bash",
                  tool_input={"command": "build"}, tool_response={"exit_code": 1})
        revision = self.r.db.execute("SELECT revision FROM problems WHERE id=?", (problem["id"],)).fetchone()[0]
        recovered = self.tool("actual-recovery", 0, dict(command="build"))
        self.assertGreater(self.r.db.execute("SELECT revision FROM problems WHERE id=?", (problem["id"],)).fetchone()[0], revision)
        self.assertTrue(self.r.db.execute("SELECT 1 FROM problem_events WHERE problem=? AND event=? "
                                        "AND role='potential_recovery'", (problem["id"], recovered["id"])).fetchone())

    def test_full_business_digest_distinguishes_hidden_log_and_ignores_transport(self):
        first = dict(turn_id="one", tool_use_id="one", metadata={"tool": ""}, tool_response={
            "exit_code": 1, "output": "head " + "x" * 10000 + "cause-a" + "x" * 10000 + " tail"})
        changed = dict(first, turn_id="two", tool_use_id="two", metadata={})
        self.assertEqual(business_evidence_digest(first), business_evidence_digest(changed))
        second = dict(changed, tool_response={**first["tool_response"],
                                              "output": first["tool_response"]["output"].replace("cause-a", "cause-b")})
        self.assertEqual(prepare_event(first["tool_response"], 128)[0], prepare_event(second["tool_response"], 128)[0])
        self.assertNotEqual(business_evidence_digest(first), business_evidence_digest(second))
        self.assertNotEqual(business_evidence_digest({"tool_input": {"id": "a"}}),
                            business_evidence_digest({"tool_input": {"id": "b"}}))

    def test_hidden_log_difference_is_reviewed_instead_of_deduplicated(self):
        self.r.config["admin"].update(enabled=True, command=["memory-reviewer"], max_calls_per_day=100)
        packets = []
        for chat, middle in (("first", "cause-a"), ("second", "cause-b")):
            self.r.task_update(self.p, chat, "Review the same tool observation")
            self.tool(chat, 1, {"command": "build"}, chat, "head " + "x" * 10000 + middle + "x" * 10000 + " tail")
            problem = next(item for item in self.r.problems(self.p) if item["conversation"] == chat)
            packets.append(self.r.review_packet(self.p, problem["id"]))
            with patch("agent_learning.admin.subprocess.Popen", fake_reviewer()):
                self.assertEqual(admin_once(self.r, self.p, problem["id"])["status"], "skip")
        self.assertNotEqual(_fingerprint(packets[0]), _fingerprint(packets[1]))
        states = [row[0] for row in self.r.db.execute("SELECT state FROM admin_calls ORDER BY rowid")]
        self.assertEqual(states, ["skip", "skip"])

    def legacy_schema(self):
        self.r.db.execute("ALTER TABLE events DROP COLUMN business_digest")
        self.r.db.execute("PRAGMA user_version=10")
        self.r.close()

    def test_schema10_upgrade_preserves_full_and_unknown_clipped_evidence(self):
        self.r.config["event_max_chars"] = 128
        full = self.r.record_event(self.p, "chat", "Feedback", {"summary": "Full observation"})
        clipped = self.r.record_event(self.p, "chat", "Feedback", {"summary": "x" * 10000})
        self.legacy_schema()
        self.r = Runtime(self.home, config={})
        rows = {row["id"]: dict(row) for row in self.r.db.execute("SELECT * FROM events")}
        self.assertEqual(rows[full["id"]]["business_digest"], full["business_digest"])
        self.assertIsNone(rows[clipped["id"]]["business_digest"])
        self.assertEqual(rows[clipped["id"]]["capture_digest"], clipped["capture_digest"])
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 11)
        self.r.close()
        self.r = Runtime(self.home, config={})
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 11)

    def test_schema11_migration_rolls_back_on_digest_failure(self):
        self.r.record_event(self.p, "chat", "Feedback", {"summary": "Full observation"})
        self.legacy_schema()
        with patch("agent_learning.runtime.business_evidence_digest", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                Runtime(self.home, config={})
        with contextlib.closing(sqlite3.connect(self.home / "runtime.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertNotIn("business_digest", {row[1] for row in db.execute("PRAGMA table_info(events)")})
        self.r = Runtime(self.home, config={})
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 11)


if __name__ == "__main__":
    unittest.main()
