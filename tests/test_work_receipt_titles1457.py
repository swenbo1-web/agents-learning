"""Known file-tool labels are display projections, never rewritten evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.runtime import Runtime
from agent_learning.work_memory import _work_display_title


class WorkReceiptTitleTests(unittest.TestCase):
    def setUp(self):
        area = Path(__file__).resolve().parents[1] / "build" / "local" / "receipt1457"
        area.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="case-", dir=area)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("tmp", "home", "codex", "runtime-releases", "checkout"):
            (self.root / name).mkdir()
        config = self.root / "config.json"
        config.write_text("{}", encoding="utf-8")
        environment = {"ALP_HOME": str(self.root / "home"), "ALP_CONFIG": str(config),
            "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"), "CODEX_HOME": str(self.root / "codex"),
            "PYTHONDONTWRITEBYTECODE": "1"}
        environment.update({key: str(self.root / "tmp") for key in ("TMP", "TEMP", "TMPDIR")})
        isolated = patch.dict(os.environ, environment)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.workspace = self.root / "checkout"
        (self.workspace / "ledger.py").write_text("ledger = 1\n", encoding="utf-8")
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": False},
            "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.sequence = 0

    def source(self, *, tool="functions.apply_patch", outcome="unknown", text=None, paths=None, runtime=None):
        self.sequence += 1
        runtime = runtime or self.r
        paths = paths or ["ledger.py"]
        payload = {"tool_name": tool, "tool_input": {"path": paths[0],
            "patch": "*** Begin Patch\n" + "".join("*** Update File: " + path + "\n@@\n-a\n+b\n" for path in paths)
                + "*** End Patch"},
            "tool_response": text if text is not None else "Success. Updated the following files:\nM ledger.py\n"}
        with runtime.transaction():
            event, _ = runtime._event(self.p, runtime.host_conversation("writer-%d" % self.sequence),
                "PostToolUse", payload, outcome=outcome)
            memory = runtime._work_capture_event(self.p, event["id"], self.workspace, raw=payload)
        row = runtime.db.execute("SELECT * FROM work_memories WHERE project=? AND id=?",
            (self.p, memory["memory_id"])).fetchone()
        return row, event

    def test_old_generic_title_is_projected_without_rewriting_source_or_fingerprint(self):
        row, event = self.source()
        original_body = row["body"]
        with patch("agent_learning.work_memory._work_display_title", side_effect=lambda body, files: body["title"]):
            old = self.r._work_item(self.p, row, workspace=self.workspace)
        item = self.r._work_item(self.p, row, workspace=self.workspace)
        self.assertEqual(item["title"], "文件操作记录：ledger.py")
        self.assertEqual(item["source_version"], old["source_version"])
        for key in ("summary", "state", "validation_level", "read_required", "source_event_refs", "coverage"):
            self.assertEqual(item[key], old[key])
        self.assertEqual(json.loads(original_body)["title"], "functions.apply_patch: unknown")
        self.assertEqual(self.r.db.execute("SELECT body FROM work_memories WHERE id=?", (row["id"],)).fetchone()[0], original_body)
        self.assertEqual(self.r.db.execute("SELECT payload FROM events WHERE id=?", (event["id"],)).fetchone()[0], event["payload"])

    def test_tool_state_and_evidence_grade_are_not_promoted_by_a_success_receipt(self):
        for outcome in ("unknown", "running", "reported", "call_completed", "succeeded"):
            with self.subTest(outcome=outcome):
                row, _ = self.source(outcome=outcome)
                item = self.r._work_item(self.p, row, workspace=self.workspace)
                self.assertEqual(item["title"], "文件操作记录：ledger.py")
                self.assertEqual(item["state"], outcome)
                self.assertEqual(item["validation_level"], "tool_observed")
                self.assertTrue(item["read_required"])
                self.assertFalse(item["execution_authority"])

    def test_meaningful_tail_and_warning_are_not_discarded_from_the_preview(self):
        text = "Success. Updated the following files:\nM ledger.py\nWARNING: E_ORDER remains unverified."
        row, _ = self.source(text=text)
        item = self.r._work_item(self.p, row, workspace=self.workspace)
        self.assertEqual(item["title"], "文件操作记录：ledger.py")
        self.assertIn("E_ORDER", item["summary"])
        self.assertIn("WARNING", item["summary"])

    def test_tool_fault_is_not_relabelled_as_a_file_change(self):
        row, _ = self.source(outcome="failed", text="E_PATCH_CONTEXT: update rejected")
        item = self.r._work_item(self.p, row, workspace=self.workspace)
        self.assertEqual(item["title"], "functions.apply_patch: failed")
        self.assertEqual(item["type"], "tool_failure")
        self.assertEqual(item["state"], "failed")
        self.assertIn("E_PATCH_CONTEXT", item["summary"])

    def test_search_retains_tool_name_matching_and_manual_read_retains_source_content(self):
        row, _ = self.source()
        item = self.r.work_search(self.p, "apply_patch", max_chars=10000)["items"][0]
        self.assertEqual(item["memory_id"], row["id"])
        self.assertEqual(item["title"], "文件操作记录：ledger.py")
        read = self.r.work_read(self.p, row["id"], workspace=self.workspace, max_chars=10000)
        self.assertIn("Success. Updated the following files:", read["content"])
        self.assertEqual(read["content"], json.loads(row["body"])["summary"])
        self.assertEqual(read["validation_level"], "tool_observed")

    def test_display_words_do_not_create_an_unrelated_automatic_match(self):
        row, _ = self.source(text="unrelated output")
        self.assertIn("文件操作记录", self.r._work_item(self.p, row)["title"])
        self.assertEqual(self.r.work_search(self.p, "文件操作记录")["items"], [])
        self.assertEqual(self.r.work_context_items(self.p, "reader", query="文件操作记录",
            workspace=self.workspace), [])

    def test_multi_file_focus_describes_the_selected_target_without_claiming_a_new_revision(self):
        (self.workspace / "zebra.py").write_text("pass\n", encoding="utf-8")
        row, _ = self.source(paths=["ledger.py", "zebra.py"])
        item = self.r._work_item(self.p, row, workspace=self.workspace, file_paths=["zebra.py"])
        self.assertEqual(item["title"], "文件操作记录：zebra.py（另1个文件）")
        self.assertEqual(item["revision"], 1)
        self.assertEqual(item["files_count"], 2)

    def test_unrecognized_custom_and_public_titles_remain_unchanged(self):
        files = [{"relative_path": "ledger.py", "relation": "write_target"}]
        for kind, title in (("public_summary", "apply_patch: unknown"),
                            ("work_summary", "apply_patch: unknown"),
                            ("file_change", "Explain ordering: unknown"),
                            ("file_change", "apply_patch: unknown\nWARNING: unsafe"),
                            ("file_change", "unrelated_tool: unknown")):
            with self.subTest(kind=kind, title=title):
                body = {"type": kind, "title": title, "summary": "meaningful output"}
                self.assertEqual(_work_display_title(body, files), title)

    def test_no_target_and_read_association_do_not_invent_a_written_file(self):
        body = {"type": "file_change", "title": "apply_patch: unknown", "summary": "output"}
        for files in ([], [{"relative_path": "ledger.py", "relation": "read_target"}]):
            self.assertEqual(_work_display_title(body, files), body["title"])

    def test_long_target_title_is_bounded_and_counts_unique_paths(self):
        body = {"type": "file_change", "title": "apply_patch: unknown", "summary": "output"}
        path = "目录/" * 100 + "ledger.py"
        files = [{"relative_path": path, "relation": "write_target"}] * 2
        title = _work_display_title(body, files)
        self.assertEqual(len(title), 160)
        self.assertNotIn("另", title)

    def test_display_projection_is_shared_by_all_three_hosts_without_new_knowledge(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                runtime = Runtime(self.root / "home", config={"admin": {"enabled": False},
                    "scheduler": {"enabled": False}, "hindsight": {"enabled": False}}, host=host)
                try:
                    row, _ = self.source(runtime=runtime)
                    item = runtime._work_item(self.p, row, workspace=self.workspace)
                    self.assertEqual(item["title"], "文件操作记录：ledger.py")
                    self.assertEqual(item["state"], "unknown")
                    self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
                finally:
                    runtime.close()


if __name__ == "__main__":
    unittest.main()
