"""Synthetic Runtime regressions for per-file notices and bounded candidate slots."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning.runtime import Runtime, encode
from agent_learning.work_files import normalize_file_target
from agent_learning.work_notice_delivery import FileNoticeGate


class WorkNoticeEdgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="work-edges-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "checkout"
        self.workspace.mkdir()
        self.original = {name: name + " original\n" for name in ("alpha.py", "beta.py", "gamma.py")}
        for name, content in self.original.items():
            self.change(name, content)
        self.original_times = {name: ((self.workspace / name).stat().st_atime_ns,
                                      (self.workspace / name).stat().st_mtime_ns) for name in self.original}
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": False},
            "scheduler": {"enabled": False}, "hindsight": {"enabled": False},
            "work_memory": {"max_context_chars": 1800}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.conversation = "reader"
        self.task = self.r.task_update(self.p, self.conversation, "edgefocus per-file notices", {"mode": "analysis"})
        self.handle = self.r.register_context(self.p, self.conversation)["context_handle"]
        self.now = time.time() + 1
        self.sequence = 0

    def change(self, name, content):
        (self.workspace / name).write_text(content, encoding="utf-8")

    def restore(self, name):
        # The file observer compares stat metadata, not semantic content. A
        # recovery fixture must restore the recorded size AND mtime explicitly.
        self.change(name, self.original[name])
        os.utime(self.workspace / name, ns=self.original_times[name])

    def source(self, text=None, *, paths=(), kind="Stop", public_message_id=None):
        self.sequence += 1
        text = text or "edgefocus: preserve file-specific revision evidence %d." % self.sequence
        if kind == "PublicMessageSegment":
            payload = {"text": text, "memory_type": "public_summary", "title": "edgefocus segment",
                "public_message_id": public_message_id, "segment": self.sequence, "segments": 4,
                "message_chars": 4800, "completion": "complete"}
        else:
            payload = {"last_assistant_message" if kind == "Stop" else "prompt": text}
            if public_message_id:
                payload["public_message_id"] = public_message_id
        with self.r.transaction():
            event, _ = self.r._event(self.p, "writer-%d" % self.sequence, kind, payload, outcome="reported")
            for name in paths:
                link = normalize_file_target(str(self.workspace / name), self.workspace)
                self.r.db.execute("INSERT INTO work_file_links VALUES(?,?,?,?,?,?,?)", (
                    self.p, event["id"], link["checkout_key"], link["relative_path"], link["path_key"],
                    "read_target", encode(link["version"])))
            memory = self.r._work_capture_event(self.p, event["id"], self.workspace, raw=payload)
        return {"event": event, "memory_id": memory["memory_id"]}

    def context(self, seconds=0):
        with patch("agent_learning.work_notice_delivery.time.time", return_value=self.now + seconds):
            return self.r._context(self.p, self.conversation, query="edgefocus", context_handle=self.handle,
                                   workspace=self.workspace)

    @staticmethod
    def cards(text):
        return [json.loads(line[len("[工作历史] "):]) for line in text.splitlines()
                if line.startswith("[工作历史] ")]

    def item(self, source):
        row = self.r.db.execute("SELECT * FROM work_memories WHERE project=? AND id=?",
                               (self.p, source["memory_id"])).fetchone()
        return self.r._work_item(self.p, row, workspace=self.workspace)

    def expose(self, source):
        self.r.work_record_exposure(self.p, self.conversation, [self.item(source)], method="mcp")

    def gate(self):
        return FileNoticeGate(self.r, self.p, self.conversation, None, "synthetic-scope", "persistent")

    def test_one_changed_file_and_one_stable_file_share_a_real_cooldown(self):
        source = self.source(paths=("alpha.py", "beta.py"))
        self.assertIn(source["memory_id"], self.context())
        self.change("alpha.py", "alpha revised once\n")
        self.assertIn(source["memory_id"], self.context())
        self.change("alpha.py", "alpha revised twice\n")
        self.assertNotIn(source["memory_id"], self.context(1))
        self.assertIn(source["memory_id"], self.context(31))

    def test_stable_link_is_not_put_on_cooldown_before_its_first_change(self):
        source = self.source(paths=("alpha.py", "beta.py"))
        self.context()
        self.change("alpha.py", "alpha first change\n")
        self.assertIn(source["memory_id"], self.context())
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_notice_delivery WHERE path_key NOT LIKE 'version:%'").fetchone()[0], 1)
        self.change("beta.py", "beta first change\n")
        self.assertIn(source["memory_id"], self.context(1))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_notice_delivery WHERE path_key NOT LIKE 'version:%'").fetchone()[0], 2)
        self.change("alpha.py", "alpha second change\n")
        self.assertNotIn(source["memory_id"], self.context(2))

    def test_mixed_recovery_is_immediate_but_unchanged_recovered_link_is_not(self):
        source = self.source(paths=("alpha.py", "beta.py"))
        self.context()
        self.change("alpha.py", "alpha changed\n")
        self.change("beta.py", "beta changed\n")
        self.assertIn(source["memory_id"], self.context())
        self.restore("alpha.py")
        self.change("beta.py", "beta changed again\n")
        self.assertIn(source["memory_id"], self.context(1))
        self.change("beta.py", "beta changed a third time\n")
        self.assertNotIn(source["memory_id"], self.context(2))
        self.assertIn(source["memory_id"], self.context(31))

    def test_sixty_multifile_edits_do_not_repeat_sixty_warnings(self):
        source = self.source(paths=("alpha.py", "beta.py"))
        self.context()
        shown = []
        for index in range(60):
            self.change("alpha.py", "alpha revision %d\n" % index)
            if source["memory_id"] in self.context(index):
                shown.append(index)
        self.assertEqual(shown, [0, 30])

    def test_one_slot_keeps_an_unread_candidate_when_the_top_row_is_deferred(self):
        unread = self.source("edgefocus: retain the unrelated unpublished evidence.")
        shown = self.source("edgefocus: preserve the observed alpha invariant.", paths=("alpha.py",))
        self.r.config["work_memory"]["max_items"] = 1
        first = self.cards(self.context())
        self.assertEqual([card["memory_id"] for card in first], [shown["memory_id"]])
        self.change("alpha.py", "alpha changed\n")
        self.assertIn(shown["memory_id"], self.context())
        self.change("alpha.py", "alpha changed again\n")
        cards = self.cards(self.context(1))
        self.assertEqual([card["memory_id"] for card in cards], [unread["memory_id"]])
        self.assertNotIn("source_update", cards[0].get("reference_role", ""))

    def test_filtered_segments_do_not_consume_their_public_message_quota(self):
        other = self.source("edgefocus: independent public result.")
        kept = self.source("edgefocus: retained message segment.", kind="PublicMessageSegment", public_message_id="same-message")
        rejected = {self.source("edgefocus: deferred message segment %d." % index,
            kind="PublicMessageSegment", public_message_id="same-message")["memory_id"] for index in range(2)}
        rows = self.r._work_search_rows(self.p, "edgefocus", automatic=True, limit=2,
                                       _row_gate=lambda row: row["id"] not in rejected)
        self.assertEqual({row["id"] for row in rows}, {kept["memory_id"], other["memory_id"]})

    def test_request_fallback_applies_the_same_candidate_gate(self):
        kept = self.source("edgefocus currently uses local storage.", kind="UserPromptSubmit")
        rejected = self.source("edgefocus currently uses offline storage.", kind="UserPromptSubmit")
        visited = []
        def eligible(row):
            visited.append(row["id"])
            return row["id"] != rejected["memory_id"]
        rows = self.r._work_search_rows(self.p, "edgefocus", automatic=True, limit=1, _row_gate=eligible)
        self.assertEqual([row["id"] for row in rows], [kept["memory_id"]])
        self.assertIn(rejected["memory_id"], visited)

    def test_legacy_search_without_a_gate_does_not_expand_items(self):
        source = self.source()
        with patch.object(self.r, "_work_item", side_effect=AssertionError("unexpected item expansion")):
            rows = self.r._work_search_rows(self.p, "edgefocus", automatic=True, limit=1)
        self.assertEqual([row["id"] for row in rows], [source["memory_id"]])

    def test_candidate_filter_does_not_enlarge_the_sql_recall_pool(self):
        for index in range(25):
            self.source("edgefocus result evidence number %d." % index)
        queries, visited = [], []
        self.r.db.set_trace_callback(queries.append)
        self.addCleanup(lambda: self.r.db.set_trace_callback(None))
        def reject(row):
            visited.append(row["id"])
            return False
        rows = self.r._work_search_rows(self.p, "edgefocus", automatic=True, limit=1, _row_gate=reject)
        searches = [sql for sql in queries if sql.startswith("SELECT m.* FROM work_memories m")]
        self.assertEqual(rows, [])
        self.assertEqual(len(visited), 10)
        self.assertEqual(len(searches), 1)
        self.assertTrue(re.search(r"\bLIMIT 10\Z", searches[0]), searches[0])

    def test_candidate_filter_does_not_expand_the_final_item_limit(self):
        sources = [self.source("edgefocus public result %d." % index) for index in range(6)]
        rejected = {source["memory_id"] for source in sources[-2:]}
        rows = self.r._work_search_rows(self.p, "edgefocus", automatic=True, limit=2,
                                       _row_gate=lambda row: row["id"] not in rejected)
        self.assertEqual(len(rows), 2)
        self.assertFalse(rejected & {row["id"] for row in rows})
        self.assertEqual(len(self.r.work_context_items(self.p, self.conversation, "edgefocus",
            workspace=self.workspace, task_state=self.task, limit=2, _notice_gate=self.gate())), 2)

    def test_multifile_tiny_budget_does_not_record_or_acknowledge_a_warning(self):
        source = self.source(paths=("alpha.py", "beta.py"))
        self.context()
        exposure = self.r.db.execute("SELECT file_state FROM work_exposures WHERE memory_id=?",
                                    (source["memory_id"],)).fetchone()[0]
        self.change("alpha.py", "alpha changed\n")
        self.r.config["injection_max_chars"] = 128
        self.assertNotIn(source["memory_id"], self.context())
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_notice_delivery").fetchone()[0], 0)
        self.assertEqual(self.r.db.execute("SELECT file_state FROM work_exposures WHERE memory_id=?",
            (source["memory_id"],)).fetchone()[0], exposure)
        self.r.config["injection_max_chars"] = 2400
        self.assertIn(source["memory_id"], self.context(1))
        self.change("alpha.py", "alpha changed again\n")
        self.assertNotIn(source["memory_id"], self.context(2))

    def test_new_source_risk_bypasses_multifile_cooldown(self):
        source = self.source(paths=("alpha.py", "beta.py"))
        self.context()
        self.change("alpha.py", "alpha changed\n")
        self.context()
        self.change("alpha.py", "alpha changed again\n")
        self.r.db.execute("UPDATE events SET payload=? WHERE id=?",
            (encode({"last_assistant_message": "edgefocus source changed after exposure"}), source["event"]["id"]))
        cards = self.cards(self.context(1))
        warning = next(card for card in cards if card["memory_id"] == source["memory_id"])
        self.assertTrue(warning["source_changed"])
        self.assertEqual(warning["reference_role"], "source_update")
        self.assertNotIn("summary", warning)

    def test_coalesced_members_record_the_union_of_their_actual_file_changes(self):
        first = self.source(paths=("alpha.py", "beta.py"))
        second = self.source(paths=("alpha.py", "beta.py"))
        self.change("beta.py", "beta changed\n")
        self.expose(first)  # first has seen A-original / B-changed.
        self.change("beta.py", self.original["beta.py"])
        self.change("alpha.py", "alpha changed\n")
        self.expose(second)  # second has seen A-changed / B-original.
        self.change("beta.py", "beta changed\n")
        warnings = [card for card in self.cards(self.context()) if card.get("file_state_changed")]
        self.assertEqual(len(warnings), 1)
        identities = {warnings[0]["memory_id"], *[item["memory_id"] for item in warnings[0]["related_memories"]]}
        self.assertEqual(identities, {first["memory_id"], second["memory_id"]})
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_notice_delivery WHERE path_key NOT LIKE 'version:%'").fetchone()[0], 2)
        self.change("alpha.py", "alpha changed again\n")
        self.assertFalse(any(card.get("file_state_changed") for card in self.cards(self.context(1))))

    def test_final_validation_recomputes_changes_instead_of_trusting_stale_paths(self):
        source = self.source(paths=("alpha.py", "beta.py"))
        self.expose(source)
        gate = self.gate()
        self.change("alpha.py", "alpha changed\n")
        first = self.r.work_exposure_updates(self.p, self.conversation, workspace=self.workspace)
        with patch("agent_learning.work_notice_delivery.time.time", return_value=self.now):
            gate.record(first[0])
        self.r.work_record_exposure(self.p, self.conversation, first)
        self.change("alpha.py", "alpha changed again\n")
        stale = self.r.work_exposure_updates(self.p, self.conversation, workspace=self.workspace)
        stale[0]["_changed_file_paths"] = ["beta.py"]  # Simulate obsolete recall metadata.
        checked = self.r.work_validate_items(self.p, stale, workspace=self.workspace, _notice_gate=gate)
        with patch("agent_learning.work_notice_delivery.time.time", return_value=self.now + 1):
            self.assertTrue(gate.deferred(checked[0]))
        self.restore("alpha.py")
        restored = self.r.work_validate_items(self.p, stale, workspace=self.workspace, _notice_gate=gate)
        with patch("agent_learning.work_notice_delivery.time.time", return_value=self.now + 1):
            self.assertFalse(gate.deferred(restored[0]))

    def test_final_validation_does_not_hide_a_source_risk_that_arrived_after_recall(self):
        source = self.source(paths=("alpha.py", "beta.py"))
        self.expose(source)
        gate = self.gate()
        self.change("alpha.py", "alpha changed\n")
        first = self.r.work_exposure_updates(self.p, self.conversation, workspace=self.workspace)
        gate.record(first[0])
        self.r.work_record_exposure(self.p, self.conversation, first)
        self.change("alpha.py", "alpha changed again\n")
        recalled = self.r.work_exposure_updates(self.p, self.conversation, workspace=self.workspace)
        self.r.db.execute("UPDATE events SET payload=? WHERE id=?",
            (encode({"last_assistant_message": "edgefocus changed source"}), source["event"]["id"]))
        checked = self.r.work_validate_items(self.p, recalled, workspace=self.workspace, _notice_gate=gate)
        self.assertTrue(checked[0]["source_changed"])
        self.assertFalse(gate.deferred(checked[0]))

    def test_missing_exposure_does_not_borrow_another_records_cooldown(self):
        source = self.source(paths=("alpha.py",))
        self.expose(source)
        gate = self.gate()
        self.change("alpha.py", "alpha changed\n")
        updates = self.r.work_exposure_updates(self.p, self.conversation, workspace=self.workspace)
        gate.record(updates[0])
        self.r.db.execute("DELETE FROM work_exposures WHERE memory_id=?", (source["memory_id"],))
        checked = self.r.work_validate_items(self.p, updates, workspace=self.workspace, _notice_gate=gate)
        self.assertFalse(gate.deferred(checked[0]))

    def test_missing_file_observation_does_not_establish_a_cooldown(self):
        source = self.source(paths=("alpha.py",))
        self.expose(source)
        gate = self.gate()
        self.change("alpha.py", "alpha changed\n")
        updates = self.r.work_exposure_updates(self.p, self.conversation, workspace=self.workspace)
        gate.record(updates[0])
        self.r.db.execute("UPDATE work_exposures SET file_state=NULL WHERE memory_id=?", (source["memory_id"],))
        checked = self.r.work_validate_items(self.p, updates, workspace=self.workspace, _notice_gate=gate)
        self.assertFalse(gate.deferred(checked[0]))

    def test_newly_visible_file_is_not_hidden_by_a_cooling_associated_path(self):
        source = self.source(paths=("alpha.py", "beta.py"))
        self.expose(source)
        gate = self.gate()
        self.change("alpha.py", "alpha changed\n")
        updates = self.r.work_exposure_updates(self.p, self.conversation, workspace=self.workspace)
        gate.record(updates[0])
        self.r.work_record_exposure(self.p, self.conversation, updates)
        self.change("alpha.py", "alpha changed again\n")
        gamma = normalize_file_target(str(self.workspace / "gamma.py"), self.workspace)
        self.r.db.execute("INSERT INTO work_file_links VALUES(?,?,?,?,?,?,?)", (
            self.p, source["event"]["id"], gamma["checkout_key"], gamma["relative_path"],
            gamma["path_key"], "read_target", encode(gamma["version"])))
        item = gate.annotate(self.item(source))
        self.assertIn("gamma.py", item["_unseen_file_paths"])
        self.assertFalse(gate.deferred(item))

    def test_explicit_fourth_file_focus_survives_earlier_associated_file_cooldown(self):
        names = ("alpha.py", "beta.py", "gamma.py", "delta.py")
        self.change("delta.py", "delta original\n")
        source = self.source(paths=names)
        initial = self.item(source)
        visible = {file["relative_path"] for file in initial["files"]}
        hidden = (set(names) - visible).pop()
        changed = next(iter(visible))
        self.expose(source)
        gate = self.gate()
        self.change(changed, "first change\n")
        updates = self.r.work_exposure_updates(self.p, self.conversation, workspace=self.workspace)
        gate.record(updates[0])
        self.r.work_record_exposure(self.p, self.conversation, updates)
        self.change(changed, "second change\n")
        items = self.r.work_context_items(self.p, self.conversation, query="", workspace=self.workspace,
            raw={"tool_name": "Read", "tool_input": {"file_path": str(self.workspace / hidden)}},
            task_state=self.task, limit=1, file_only=True, _notice_gate=gate)
        self.assertEqual([item["memory_id"] for item in items], [source["memory_id"]])
        self.assertIn(hidden, {file["relative_path"] for file in items[0]["files"]})


if __name__ == "__main__":
    unittest.main()
