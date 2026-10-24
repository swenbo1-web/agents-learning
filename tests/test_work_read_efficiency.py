"""Bounded work pages preserve text and live safety checks without repeated cards."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime, encode
from agent_learning.work_files import normalize_file_target


class WorkReadEfficiencyTests(unittest.TestCase):
    def setUp(self):
        local = Path(__file__).resolve().parents[1] / "build" / "local" / "work-read-efficiency"
        local.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=local)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.file = self.workspace / "marker.py"
        self.file.write_text("original", encoding="utf-8")
        self.r = Runtime(self.root / "home", config={})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "reader", "inspect marker", {"mode": "analysis"})
        self.handle = self.r.register_context(self.p, "reader")["context_handle"]
        self.server = Server(self.r, self.p, context_handle=self.handle, workspace=self.workspace)
        payload = {"tool_name": "write_file", "tool_input": {"path": "marker.py"},
                   "tool_response": {"exit_code": 0, "output": "evidence " + "x" * 5000}}
        with self.r.transaction():
            self.event, _ = self.r._event(self.p, "past", "PostToolUse", payload, outcome="succeeded")
            memory = self.r._work_capture_event(self.p, self.event["id"], self.workspace, raw=payload)
        self.memory_id = memory["memory_id"]

    def read(self, **kwargs):
        return self.r.work_read(self.p, self.memory_id, workspace=self.workspace, **kwargs)

    def set_body(self, **changes):
        with self.r.transaction():
            body = json.loads(self.r.db.execute("SELECT body FROM work_memories WHERE project=? AND id=?",
                (self.p, self.memory_id)).fetchone()[0])
            body.update(changes)
            self.r.db.execute("UPDATE work_memories SET body=? WHERE project=? AND id=?",
                (encode(body), self.p, self.memory_id))

    def pages(self, budget, section="evidence", *, mcp=False):
        pages, offset, revision = [], 0, None
        while True:
            args = {"max_chars": budget, "section": section, "offset": offset}
            if revision is not None:
                args["expected_revision"] = revision
            page = (self.server.call("work_memory_read", {"memory_id": self.memory_id, **args})
                    if mcp else self.read(**args))
            self.assertLessEqual(len(encode(page)), budget)
            self.assertEqual(page["offset"], offset)
            self.assertEqual(page["truncated"], page["next_offset"] is not None)
            self.assertFalse(page["execution_authority"])
            self.assertTrue(page["file_check"]["complete"])
            revision = page["revision"]
            pages.append(page)
            if page["next_offset"] is None:
                self.assertEqual(offset + len(page["content"]), page["section_length"])
                return pages
            self.assertTrue(page["content"])
            self.assertEqual(page["next_offset"], offset + len(page["content"]))
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
            self.assertLess(len(pages), 200, "pages must make useful progress")

    def test_small_pages_allocate_meaningful_content_and_keep_large_first_page(self):
        complete = self.read(max_chars=50000, section="evidence")
        self.assertIn("title", complete)
        self.assertIn("created", complete)
        self.assertEqual(complete["files"][0]["relative_path"], "marker.py")
        self.assertNotIn("metadata_omitted", complete)
        for budget in (1024, 2000, 5000, 10000):
            with self.subTest(budget=budget):
                pages = self.pages(budget)
                self.assertEqual("".join(page["content"] for page in pages), complete["content"])
                self.assertTrue(all(page["source_version"] == complete["source_version"] for page in pages))
                self.assertTrue(all(page["source_event_refs"] == [self.event["id"]] for page in pages))
                if budget == 1024:
                    self.assertLessEqual(len(pages), 20)
                    content_chars = sum(len(encode(page["content"])) - 2 for page in pages)
                    self.assertGreaterEqual(content_chars / sum(len(encode(page)) for page in pages), 0.25)
                if budget == 10000:
                    self.assertEqual(pages, [complete])

    def test_chinese_escapes_and_digit_boundaries_round_trip_in_both_sections(self):
        summary = ('中文😀 "quoted" \\path\n\t\r\b\f\x00 ' * 105) + "末尾"
        self.set_body(summary=summary, completion="partial",
                      coverage={"truncated": True, "projection_only": True})
        for section in ("summary", "evidence"):
            complete = self.read(max_chars=50000, section=section)
            for budget in (1024, 1025, 1099, 1279, 2000):
                with self.subTest(section=section, budget=budget):
                    pages = self.pages(budget, section)
                    self.assertEqual("".join(page["content"] for page in pages), complete["content"])
                    self.assertTrue(all(page["completion"] == "partial" for page in pages))
                    self.assertTrue(all(page["validation_level"] == "tool_observed" for page in pages))
        self.assertEqual(self.read(max_chars=50000)["content"], summary)

    def test_huge_previews_and_blocked_risk_shrink_without_changing_identity(self):
        with self.r.transaction():
            for index in range(8):
                event, _ = self.r._event(self.p, "past", "PostToolUse", {"index": index}, outcome="succeeded")
                snapshot = self.r.work_source_snapshot(self.p, event["id"])
                self.r.db.execute("INSERT INTO work_memory_sources VALUES(?,?,?,?,?)",
                    (self.p, self.memory_id, event["id"], snapshot["capture_digest"], snapshot["source_version"]))
                target = normalize_file_target("long-" + str(index) + "-" + "z" * 150 + ".py", self.workspace)
                self.r.db.execute("INSERT INTO work_file_links VALUES(?,?,?,?,?,?,?)",
                    (self.p, event["id"], target["checkout_key"], target["relative_path"],
                     target["path_key"], "write_target", encode(target["version"])))
        self.set_body(title="长标题" * 3000, coverage={"details": "coverage " * 3000})
        risk = {"blocked": True, "reason": "unresolved " * 3000}
        with patch("agent_learning.candidate_references.lineage_risk", return_value=risk):
            complete = self.read(max_chars=50000, section="evidence")
            pages = self.pages(1024)
        self.assertEqual("".join(page["content"] for page in pages), complete["content"])
        for page in pages:
            self.assertEqual(page["source_version"], complete["source_version"])
            self.assertEqual(page["files_omitted"], page["files_count"] - len(page["files"]))
            self.assertGreater(page["file_state_omitted"], 0)
            self.assertGreater(page["source_refs_omitted"], 0)
            self.assertTrue(page["candidate_reference_risk"][0]["blocked"])
            self.assertTrue(page["candidate_reference_risk"][0]["details_omitted"])
            self.assertFalse(page["automatic_eligible"])

    def test_every_continuation_rechecks_source_and_full_file_state(self):
        first = self.read(max_chars=1024, section="evidence")
        kwargs = {"max_chars": 1024, "section": "evidence", "offset": first["next_offset"],
                  "expected_revision": first["revision"]}
        with patch.object(self.r, "work_source_snapshot", wraps=self.r.work_source_snapshot) as observed:
            unchanged = self.read(**kwargs)
        self.assertEqual(observed.call_count, 1)
        self.assertEqual(unchanged["source_version"], first["source_version"])
        self.file.write_text("a substantially longer current file", encoding="utf-8")
        changed_file = self.read(**kwargs)
        self.assertNotEqual(changed_file["file_check"], first["file_check"])
        with patch("agent_learning.work_memory._read_file_version", return_value={"status": "unavailable"}):
            unreadable = self.read(**kwargs)
        self.assertFalse(unreadable["file_check"]["complete"])
        self.assertTrue(unreadable["content"])
        with self.r.transaction():
            self.r.db.execute("UPDATE events SET outcome='failed' WHERE id=?", (self.event["id"],))
        changed_source = self.read(**kwargs)
        self.assertEqual(changed_source["state"], "failed")
        self.assertNotEqual(changed_source["source_version"], first["source_version"])
        with self.r.transaction():
            self.r.db.execute("UPDATE events SET payload=? WHERE id=?", (encode({"output": "changed"}), self.event["id"]))
        changed_payload = self.read(max_chars=1024, section="evidence")
        self.assertTrue(changed_payload["source_changed"])
        with self.r.transaction():
            self.r.db.execute("DELETE FROM events WHERE id=?", (self.event["id"],))
        missing = self.read(max_chars=1024, section="evidence")
        self.assertEqual(missing["state"], "missing")
        self.assertTrue(missing["source_changed"])
        self.assertTrue(missing["content"])

    def test_offset_and_revision_boundaries_do_not_drop_or_repeat_text(self):
        complete = self.read(max_chars=50000)
        length = complete["section_length"]
        for offset in (0, 9, 10, 99, 100, length - 1, length):
            with self.subTest(offset=offset):
                page = self.read(max_chars=1024, offset=offset, expected_revision=complete["revision"])
                self.assertEqual(page["content"], complete["content"][offset:offset + len(page["content"])])
                self.assertLessEqual(len(encode(page)), 1024)
                if offset == length:
                    self.assertEqual(page["content"], "")
                    self.assertIsNone(page["next_offset"])
                    self.assertFalse(page["truncated"])
        for offset in (-1, True, length + 1):
            with self.assertRaises(ContractError):
                self.read(offset=offset)
        with self.assertRaisesRegex(ContractError, "revision changed"):
            self.read(expected_revision=complete["revision"] + 1)

    def test_mcp_small_pages_form_complete_receipt_without_model_read_claim(self):
        complete = self.read(max_chars=50000, section="evidence")
        pages = self.pages(1024, mcp=True)
        self.assertEqual("".join(page["content"] for page in pages), complete["content"])
        receipt = self.server.call("work_memory_read", {"memory_id": self.memory_id,
                                   "section": "evidence", "max_chars": 1024})
        self.assertTrue(receipt["section_previously_returned_complete"])
        self.assertFalse(receipt["model_read_confirmed"])
        self.assertNotIn("content", receipt)
        self.assertLessEqual(len(encode(receipt)), 1024)
        self.file.write_text("file changed after complete delivery", encoding="utf-8")
        changed = self.server.call("work_memory_read", {"memory_id": self.memory_id,
                                  "section": "evidence", "max_chars": 1024})
        self.assertIn("content", changed)
        self.assertNotEqual(changed["file_check"], receipt["file_check"])

    def test_exact_final_budget_at_large_offset_does_not_add_empty_tail_page(self):
        offset = 1_000_000
        self.set_body(summary="x" * offset + "尾" * 500)
        complete = self.read(max_chars=50000, offset=offset)
        exact_budget = len(encode(complete))
        self.assertGreaterEqual(exact_budget, 1024)
        exact = self.read(max_chars=exact_budget, offset=offset)
        self.assertEqual(exact, complete)
        self.assertIsNone(exact["next_offset"])
        smaller = self.read(max_chars=exact_budget - 1, offset=offset)
        self.assertTrue(smaller["content"])
        self.assertTrue(smaller["truncated"])
        self.assertLessEqual(len(encode(smaller)), exact_budget - 1)


if __name__ == "__main__":
    unittest.main()
