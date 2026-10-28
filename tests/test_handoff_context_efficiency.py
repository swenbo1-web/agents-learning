"""Current-task excerpts improve information without changing restoration scope."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.mcp import Server
from agent_learning.runtime import Runtime, encode
from agent_learning.work_files import normalize_file_target


class HandoffContextEfficiencyTests(unittest.TestCase):
    def setUp(self):
        area = Path(__file__).resolve().parents[1] / "build/local/handoff-context-tests"
        area.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=area, prefix="case-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ("tmp", "home", "runtime-releases", "codex", "checkout"):
            (self.root / name).mkdir()
        config = self.root / "config.json"
        config.write_text("{}", encoding="utf-8")
        env = {"PYTHONDONTWRITEBYTECODE": "1", "ALP_HOME": str(self.root / "home"),
               "ALP_CONFIG": str(config), "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"),
               "CODEX_HOME": str(self.root / "codex")}
        env.update({key: str(self.root / "tmp") for key in ("TMP", "TEMP", "TMPDIR")})
        isolated = patch.dict(os.environ, env)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.workspace = self.root / "checkout"
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": False},
            "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "reader", "preserve ledger revision ordering", {"mode": "analysis"})
        self.handle = self.r.register_context(self.p, "reader")["context_handle"]

    def record(self, text, conversation="reader"):
        task = self.r.task(self.p, conversation)
        if task is None:
            task = self.r.task_update(self.p, conversation, "different ledger task", {"mode": "analysis"})
        payload = {"last_assistant_message": text, "completion": "complete"}
        with self.r.transaction():
            event, _ = self.r._event(self.p, conversation, "Stop", payload, outcome="reported",
                task_id=task["id"], task_revision=task["revision"], task_write_version=task["write_version"])
            memory = self.r._work_capture_event(self.p, event["id"], self.workspace, raw=payload)
        return memory["memory_id"], event["id"]

    def context(self, **kwargs):
        return self.r._context(self.p, "reader", query="compiler " + "x " * 40000 + " E_CRITICAL",
            context_handle=self.handle, workspace=self.workspace, **kwargs)

    @staticmethod
    def cards(output):
        return [json.loads(line.split("] ", 1)[1]) for line in output.splitlines()
                if line.startswith("[工作历史] ")]

    def test_late_constraint_survives_long_completion_prefix_in_same_budget(self):
        constraint = "Pending: do not publish ledger revision 37 until concurrency checks pass."
        text = "Stage complete; observed tests passed.\n\n" * 12 + constraint
        memory, _ = self.record(text)
        output = self.context(compact=True)
        card = self.cards(output)[0]
        self.assertEqual(card["memory_id"], memory)
        self.assertIn(constraint, card["summary"])
        self.assertEqual(card["state"], "reported")
        self.assertEqual(card["validation_level"], "reported")
        self.assertTrue(card["read_required"])
        self.assertLessEqual(len(output), self.r.config["injection_max_chars"])
        self.assertLessEqual(len(output[output.index("工作历史索引"):]), 600)

    def test_chinese_condition_and_number_remain_a_complete_quote(self):
        constraint = "待验证：账本修订37未通过并发检查前，禁止发布或清除回滚快照。"
        self.record("本阶段已完成。\n\n" * 40 + constraint)
        card = self.cards(self.context(compact=True))[0]
        self.assertIn(constraint, card["summary"])

    def test_correction_is_not_hidden_behind_earlier_success(self):
        correction = "Correction: ledger concurrency tests did not pass; do not publish."
        self.record("Stage completed. Tests passed.\n\n" * 12 + correction)
        card = self.cards(self.context(compact=True))[0]
        self.assertIn(correction, card["summary"])

    def test_excerpt_does_not_create_a_complete_read_receipt(self):
        text = "Stage complete.\n\n" * 16 + "Pending: preserve ledger revision 37."
        memory, _ = self.record(text)
        self.context(compact=True)
        server = Server(self.r, self.p, context_handle=self.handle, workspace=self.workspace)
        self.addCleanup(server.close)
        result = server.call("work_memory_read", {"memory_id": memory, "section": "summary", "max_chars": 10000})
        self.assertEqual(result["content"], text)
        self.assertFalse(result["truncated"])
        self.assertNotIn("already_read", result)

    def test_source_change_during_final_check_never_sends_selected_constraint(self):
        constraint = "Pending: do not publish ledger revision 37."
        _, source = self.record("Stage complete.\n\n" * 12 + constraint)
        validate = self.r.work_validate_items
        def changed(project, items, workspace=None, **kwargs):
            self.r.db.execute("UPDATE events SET capture_digest=? WHERE project=? AND id=?",
                              ("changed-capture", self.p, source))
            return validate(project, items, workspace=workspace, **kwargs)
        with patch.object(self.r, "work_validate_items", side_effect=changed):
            output = self.context(compact=True)
        self.assertNotIn(constraint, output)
        self.assertEqual(self.cards(output), [])

    def test_other_conversation_constraint_is_not_borrowed(self):
        own, _ = self.record("Pending: preserve the current ledger revision 37.")
        self.record("Pending: foreign ledger revision 99 must replace revision 37.", conversation="other")
        card = self.cards(self.context(compact=True))[0]
        self.assertEqual(card["memory_id"], own)
        self.assertNotIn("99", card.get("summary", ""))

    def test_file_change_during_final_check_never_sends_old_constraint(self):
        path = self.workspace / "ledger.py"
        path.write_text("revision = 37\n", encoding="utf-8")
        constraint = "Pending: do not publish ledger revision 37."
        memory, source = self.record(constraint)
        link = normalize_file_target(str(path), self.workspace)
        self.r.db.execute("INSERT INTO work_file_links VALUES(?,?,?,?,?,?,?)", (
            self.p, source, link["checkout_key"], link["relative_path"], link["path_key"],
            "read_target", encode(link["version"])))
        validate = self.r.work_validate_items
        def changed(project, items, workspace=None, **kwargs):
            path.write_text("revision = 38\nchanged_after_selection = True\n", encoding="utf-8")
            return validate(project, items, workspace=workspace, **kwargs)
        with patch.object(self.r, "work_validate_items", side_effect=changed):
            output = self.context(compact=True)
        self.assertNotIn(constraint, output)
        self.assertEqual(self.cards(output), [])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_exposures WHERE project=? AND memory_id=?",
            (self.p, memory)).fetchone()[0], 0)

    def test_tiny_budget_does_not_record_an_unsent_excerpt(self):
        memory, _ = self.record("Pending: preserve ledger revision 37 until verified.")
        self.r.config["injection_max_chars"] = 128
        output = self.context(compact=True)
        self.assertLessEqual(len(output), 128)
        self.assertEqual(self.cards(output), [])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_exposures WHERE project=? AND memory_id=?",
            (self.p, memory)).fetchone()[0], 0)

    def test_plain_progress_does_not_resend_the_improved_excerpt(self):
        memory, _ = self.record("Pending: preserve ledger revision 37 until verified.")
        self.assertIn(memory, self.context(compact=True))
        task = self.r.task(self.p, "reader")
        self.r.task_update(self.p, "reader", task["goal"], status="delivered")
        output = self.r._context(self.p, "reader", query="ledger", context_handle=self.handle,
                                 workspace=self.workspace)
        self.assertNotIn(memory, output)


if __name__ == "__main__":
    unittest.main()
