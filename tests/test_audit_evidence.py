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

    def test_missing_pre_name_learns_first_post_and_invalidates_changed_name(self):
        values = dict(tool_use_id="unknown-name", tool_input={"file_path": "file-a"})
        self.hook("PreToolUse", **values)
        self.hook("PostToolUse", **values, tool_name="Read", tool_response={"exit_code": 0})
        event = self.r.db.execute("SELECT id FROM events WHERE type='PostToolUse'").fetchone()[0]
        problem = self.r.feedback(self.p, "chat", "Recorded success", category="operation", event_refs=[event])
        body = dict(problem_id=problem["id"], problem_revision=problem["revision"], title="Observed read",
                    content="The supplied operation completed", category="operation", applicability="This observation",
                    limitations="No product acceptance claim", evidence_refs=[event], validation_level="tool_success")
        candidate = self.r.submit_candidate(self.p, **body)
        published = self.r.publish(self.p, candidate["candidate_id"])
        self.hook("PostToolUse", **values, tool_name="Bash", tool_response={"exit_code": 0})
        operation = self.r.db.execute("SELECT state,conflict,tool_name FROM operations").fetchone()
        self.assertEqual(tuple(operation), ("conflict", 1, "Read"))
        current = self.r.read(self.p, published["knowledge_id"])
        self.assertEqual(current["source_health"], "conflicted")
        self.assertFalse(current["current_tool_success"])
        revision = self.r.db.execute("SELECT revision FROM problems WHERE id=?", (problem["id"],)).fetchone()[0]
        with self.assertRaises(ContractError):
            self.r.submit_candidate(self.p, **dict(body, problem_revision=revision, title="Invalid reused success"))

    def test_post_only_missing_name_learns_first_known_name_and_detects_change(self):
        values = dict(tool_use_id="post-only-name", tool_input={"file_path": "file-a"}, tool_response={"exit_code": 0})
        self.hook("PostToolUse", **values)
        self.hook("PostToolUse", **values, tool_name="Read")
        self.hook("PostToolUse", **values, tool_name="Bash")
        self.assertEqual(tuple(self.r.db.execute("SELECT state,conflict,tool_name FROM operations").fetchone()),
                         ("conflict", 1, "Read"))
        self.assertEqual([row[0] for row in self.r.db.execute("SELECT outcome FROM events WHERE type='PostToolUse' ORDER BY rowid")],
                         ["succeeded", "succeeded", "conflict"])

    def test_first_known_name_replays_without_duplicate_evidence(self):
        values = dict(tool_use_id="name-replay", tool_input={"file_path": "file-a"})
        self.hook("PreToolUse", **values)
        response = dict(values, tool_name="Read", tool_response={"exit_code": 0})
        self.hook("PostToolUse", **response)
        count = self.r.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.hook("PostToolUse", **response)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], count)
        self.assertEqual(tuple(self.r.db.execute("SELECT state,conflict,tool_name FROM operations").fetchone()),
                         ("succeeded", 0, "Read"))

    def test_missing_post_name_keeps_known_name_without_fabricating_conflict(self):
        values = dict(tool_use_id="missing-post-name", tool_input={"file_path": "file-a"})
        self.hook("PreToolUse", **values, tool_name="Read")
        self.hook("PostToolUse", **values, tool_response={"exit_code": 0})
        self.assertEqual(tuple(self.r.db.execute("SELECT state,conflict,tool_name FROM operations").fetchone()),
                         ("succeeded", 0, "Read"))
        self.hook("PostToolUse", **values, tool_name="Bash", tool_response={"exit_code": 0})
        self.assertEqual(tuple(self.r.db.execute("SELECT state,conflict,tool_name FROM operations").fetchone()),
                         ("conflict", 1, "Read"))

    def test_legacy_null_name_uses_complete_known_post_before_incoming_name(self):
        values = dict(tool_use_id="legacy-name", tool_input={"file_path": "file-a"})
        self.hook("PreToolUse", **values)
        response = dict(values, tool_name="Read", tool_response={"exit_code": 0})
        self.hook("PostToolUse", **response)
        self.r.db.execute("UPDATE operations SET tool_name=NULL")
        self.hook("PostToolUse", **response)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events WHERE type='PostToolUse'").fetchone()[0], 1)
        self.hook("PostToolUse", **values, tool_name="Bash", tool_response={"exit_code": 0})
        self.assertEqual(tuple(self.r.db.execute("SELECT state,conflict,tool_name FROM operations").fetchone()),
                         ("conflict", 1, "Read"))

    def test_legacy_null_name_preserves_already_observed_name_conflict(self):
        values = dict(tool_use_id="legacy-name-conflict", tool_input={"file_path": "file-a"})
        self.hook("PreToolUse", **values)
        task = self.r.task(self.p, "chat")
        operation = self.r.db.execute("SELECT id FROM operations").fetchone()[0]
        for name in ("Read", "Bash"):
            self.r.record_event(self.p, "chat", "PostToolUse",
                {"tool_name": name, "tool_input": values["tool_input"], "tool_response": {"exit_code": 0}},
                task_id=task["id"], task_revision=task["revision"], operation_id=operation, outcome="succeeded")
        self.hook("PostToolUse", **values, tool_response={"exit_code": 0})
        self.assertEqual(tuple(self.r.db.execute("SELECT state,conflict,tool_name FROM operations").fetchone()),
                         ("conflict", 1, "Read"))
        self.hook("PostToolUse", **values, tool_name="Read", tool_response={"exit_code": 0})
        self.assertEqual(self.r.db.execute("SELECT conflict FROM operations").fetchone()[0], 1)

    def test_identical_legacy_replay_converges_confirmed_name_conflict_once(self):
        for mode in ("current", "legacy_digest", "legacy_without_digest"):
            with self.subTest(mode=mode):
                self.r.config["event_max_chars"] = 6000
                self.r.task_update(self.p, mode, "Review the same tool observation")
                values = dict(tool_use_id=mode, tool_input={"file_path": "file-a"})
                response = dict(values, tool_name="Read", tool_response={"exit_code": 0})
                self.hook("PreToolUse", mode, **values)
                self.hook("PostToolUse", mode, **response)
                operation = dict(self.r.db.execute("SELECT * FROM operations WHERE conversation=?", (mode,)).fetchone())
                event = dict(self.r.db.execute("SELECT * FROM events WHERE conversation=? AND type='PostToolUse'", (mode,)).fetchone())
                if mode != "current":
                    old_key = [mode, None, "PostToolUse", mode, digest(response["tool_response"])]
                    old_id = digest([self.p, [self.r.host_epoch(), old_key]])
                    self.r.db.execute("UPDATE events SET id=? WHERE id=?", (old_id, event["id"]))
                    if mode == "legacy_without_digest":
                        self.r.db.execute("UPDATE events SET capture_digest=NULL WHERE id=?", (old_id,))
                    self.r.db.execute("UPDATE operations SET last_event=? WHERE id=?", (old_id, operation["id"]))
                    event = dict(self.r.db.execute("SELECT * FROM events WHERE id=?", (old_id,)).fetchone())
                task = self.r.task(self.p, mode)
                self.r.record_event(self.p, mode, "PostToolUse",
                    {"tool_name": "Bash", "tool_input": values["tool_input"], "tool_response": {"exit_code": 0}},
                    task_id=task["id"], task_revision=task["revision"], operation_id=operation["id"], outcome="succeeded")
                self.r.db.execute("UPDATE operations SET tool_name=NULL WHERE id=?", (operation["id"],))
                problem = self.r.feedback(self.p, mode, "Legacy tool success " + mode,
                                          category="operation", event_refs=[event["id"]])
                body = dict(problem_id=problem["id"], problem_revision=problem["revision"], title="Legacy " + mode,
                    content="The recorded operation completed", category="operation", applicability="This observation",
                    limitations="No product acceptance claim", evidence_refs=[event["id"]], validation_level="tool_success")
                candidate = self.r.submit_candidate(self.p, **body)
                published = self.r.publish(self.p, candidate["candidate_id"])
                before = self.r.read(self.p, published["knowledge_id"])
                self.assertTrue(before["current_tool_success"])
                count = self.r.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                if mode == "current":
                    self.r.config["event_max_chars"] = 128
                self.hook("PostToolUse", mode, **response)
                self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], count + 1)
                self.assertEqual(dict(self.r.db.execute("SELECT * FROM events WHERE id=?", (event["id"],)).fetchone()), event)
                derived = self.r.db.execute("SELECT * FROM events WHERE conversation=? AND outcome='conflict'", (mode,)).fetchone()
                self.assertTrue(json.loads(derived["payload"])["legacy_name_conflict"])
                self.assertEqual(tuple(self.r.db.execute("SELECT state,conflict,tool_name FROM operations WHERE id=?",
                                                       (operation["id"],)).fetchone()), ("conflict", 1, "Read"))
                current = self.r.read(self.p, published["knowledge_id"])
                self.assertEqual(current["source_health"], "conflicted")
                self.assertFalse(current["current_tool_success"])
                self.assertGreater(current["source_version"], before["source_version"])
                revision = self.r.db.execute("SELECT revision FROM problems WHERE id=?", (problem["id"],)).fetchone()[0]
                with self.assertRaises(ContractError):
                    self.r.submit_candidate(self.p, **dict(body, problem_revision=revision, title="Invalid reused " + mode))
                self.hook("PostToolUse", mode, **response)
                self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], count + 1)
                self.assertEqual(self.r.db.execute("SELECT revision FROM problems WHERE id=?", (problem["id"],)).fetchone()[0], revision)

    def test_clipped_legacy_name_is_unknown_but_new_full_name_is_frozen(self):
        self.r.config["event_max_chars"] = 128
        values = dict(tool_use_id="clipped-name", tool_input={"file_path": "file-a"})
        self.hook("PreToolUse", **values)
        original_name = "same-prefix-" + "x" * 1000
        self.hook("PostToolUse", **values, tool_name=original_name, tool_response={"exit_code": 0})
        self.assertTrue(json.loads(self.r.db.execute("SELECT payload FROM events WHERE type='PostToolUse'").fetchone()[0])["truncated"])
        self.assertEqual(self.r.db.execute("SELECT tool_name FROM operations").fetchone()[0], original_name)
        self.r.db.execute("UPDATE operations SET tool_name=NULL")
        self.hook("PostToolUse", **values, tool_name="Read", tool_response={"exit_code": 0})
        self.assertEqual(tuple(self.r.db.execute("SELECT state,conflict,tool_name FROM operations").fetchone()),
                         ("succeeded", 0, "Read"))
        self.hook("PostToolUse", **values, tool_name="Bash", tool_response={"exit_code": 0})
        self.assertEqual(self.r.db.execute("SELECT conflict FROM operations").fetchone()[0], 1)

    def test_late_old_account_name_does_not_change_frozen_name_or_create_conflict(self):
        values = dict(tool_use_id="old-account-name", tool_input={"file_path": "file-a"})
        self.hook("PreToolUse", **values)
        self.hook("PostToolUse", **values, tool_name="Read", tool_response={"exit_code": 0})
        self.r.account_changed("name-boundary")
        self.hook("PostToolUse", **values, tool_name="Bash", tool_response={"exit_code": 0})
        self.assertEqual(tuple(self.r.db.execute("SELECT state,conflict,tool_name FROM operations").fetchone()),
                         ("unknown", 0, "Read"))

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
