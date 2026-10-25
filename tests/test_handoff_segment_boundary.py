"""Whole-message conditions cannot be lost through public segmentation and compaction."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from agent_learning.runtime import Runtime
from agent_learning.work_handoff import select_handoff_preview


class HandoffSegmentBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-handoff-boundary-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={"project_aliases": {str(self.workspace): "1419-handoff-probe"}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.task = self.r.task_update(self.p, "chat", "preserve conditional delivery", {"mode": "analysis"})
        self.handle = self.r.register_context(self.p, "chat")["context_handle"]

    def record(self, text, key):
        raw = {"last_assistant_message": text, "completion": "complete"}
        with self.r.transaction():
            event, _ = self.r._event(self.p, "chat", "Stop", raw, event_id=key, outcome="reported",
                task_id=self.task["id"], task_revision=self.task["revision"], task_write_version=self.task["write_version"])
            self.r._work_capture_event(self.p, event["id"], self.workspace, raw=raw)
        return event

    @staticmethod
    def conditional_message():
        prefix = "Completed report. " * 8 + "\n\nOnly if all validation passes:\n- "
        prefix += ("context " * 200)[:1199 - len(prefix)] + "\n"
        assert len(prefix) == 1200
        return prefix + "- Next step: publish artifacts.\n- " + "background " * 30

    def context(self):
        query = " ".join(f"ERR_{index}" for index in range(129))
        text = self.r._context(self.p, "chat", query=query, compact=True,
            context_handle=self.handle, workspace=self.workspace)
        return [json.loads(line[len("[工作历史] "):]) for line in text.splitlines() if line.startswith("[工作历史] ")]

    def test_split_condition_never_becomes_an_unconditional_task_handoff(self):
        text = self.conditional_message()
        self.assertIn("Only if all validation passes:", select_handoff_preview(text))
        self.record(text, ["segment-probe", 4])
        segments = [json.loads(row[0]) for row in self.r.db.execute(
            "SELECT body FROM work_memories WHERE json_extract(body,'$.coverage.segments')>1")]
        self.assertEqual(len(segments), 2)
        self.assertTrue(any("publish artifacts" in segment["summary"] for segment in segments))
        self.assertEqual(self.context(), [])
        # Ordinary explicit retrieval still preserves every segment for reading.
        self.assertTrue(self.r.work_search(self.p, "publish artifacts", workspace=self.workspace)["items"])

    def test_many_recent_segments_do_not_hide_an_older_complete_handoff(self):
        old = self.record("Only if validation passes:\n- Next step: publish artifacts.", ["older", 1])
        with self.r.transaction():
            self.r.db.execute("UPDATE work_memories SET created=1 WHERE event_id=?", (old["id"],))
        for index in range(11):
            self.record(self.conditional_message(), ["recent-segments", index])
        cards = self.context()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["reference_role"], "task_handoff")
        self.assertIn("Only if validation passes:", cards[0]["summary"])

    def test_final_validation_rejects_a_previously_selected_isolated_segment(self):
        self.record(self.conditional_message(), ["segment-probe", 4])
        row = self.r.db.execute("SELECT * FROM work_memories WHERE json_extract(body,'$.coverage.segment')=1").fetchone()
        item = self.r._work_item(self.p, row, workspace=self.workspace)
        item.update(reference_role="task_handoff", _handoff_scope={"workspace_key": row["workspace_key"],
            "conversation": "chat", "agent": None, "task_id": self.task["id"],
            "task_revision": self.task["revision"], "assignment_id": None})
        self.assertEqual(self.r.work_validate_items(self.p, [item], workspace=self.workspace), [])


if __name__ == "__main__":
    unittest.main()
