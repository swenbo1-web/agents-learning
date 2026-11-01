"""Visible file facts alone acknowledge a bounded change notice.

All sources and file changes are synthetic and confined to build/local.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning.runtime import Runtime, encode
from agent_learning.work_notice_delivery import coalesce
from tests import test_work_notice_edges1445 as edge_fixture


class WorkNoticeVisibilityTests(unittest.TestCase):
    change = edge_fixture.WorkNoticeEdgeTests.change
    source = edge_fixture.WorkNoticeEdgeTests.source
    item = edge_fixture.WorkNoticeEdgeTests.item
    expose = edge_fixture.WorkNoticeEdgeTests.expose
    cards = staticmethod(edge_fixture.WorkNoticeEdgeTests.cards)

    def setUp(self):
        area = Path(__file__).resolve().parents[1] / "build" / "local" / "accept1457" / "notice-visibility"
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
        environment.update({name: str(self.root / "tmp") for name in ("TMP", "TEMP", "TMPDIR")})
        isolated = patch.dict(os.environ, environment)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.workspace = self.root / "checkout"
        for name in ("alpha.py", "beta.py", "gamma.py"):
            self.change(name, name + " original\n")
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": False},
            "scheduler": {"enabled": False}, "hindsight": {"enabled": False},
            "work_memory": {"max_items": 4, "max_context_chars": 1800}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.conversation, self.sequence = "reader", 0
        self.r.task_update(self.p, self.conversation, "edgefocus visible file changes", {"mode": "analysis"})
        self.handle = self.r.register_context(self.p, self.conversation)["context_handle"]
        self.now = time.time() + 1

    def context(self, seconds=0):
        with patch("agent_learning.work_notice_delivery.time.time", return_value=self.now + seconds):
            return self.r._context(self.p, self.conversation, query="edgefocus", context_handle=self.handle,
                                   workspace=self.workspace, tool_only=True)

    def exposure(self, identity):
        return dict(self.r.db.execute("SELECT * FROM work_exposures WHERE project=? AND memory_id=?",
                                     (self.p, identity)).fetchone())

    def saved_delivery(self):
        return {name: [dict(row) for row in self.r.db.execute("SELECT * FROM " + name + " WHERE project=?",
                      (self.p,))] for name in ("work_exposures", "work_notice_delivery", "context_delivery_cache")}

    def restore_delivery(self, saved):
        for name, rows in saved.items():
            self.r.db.execute("DELETE FROM " + name + " WHERE project=?", (self.p,))
            for row in rows:
                self.r.db.execute("INSERT INTO " + name + "(" + ",".join(row) + ") VALUES(" +
                                  ",".join("?" for _ in row) + ")", tuple(row.values()))

    def budget_for_card(self, card):
        header = "工作历史索引（非正式解法/执行授权；仅相关且索引不足时用 work_memory_read）："
        return len(header) + 1 + len("[工作历史] " + encode(card)) + 1

    @staticmethod
    def notice(identity="one", **updates):
        return dict({"memory_id": identity, "revision": 1, "source_version": "source", "state": "reported",
            "validation_level": "reported", "file_state_changed": True,
            "file_state": {"alpha.py": "new-alpha", "beta.py": "stable-beta"},
            "_changed_file_paths": ["alpha.py"],
            "files": [{"relative_path": "alpha.py", "version_changed": True,
                       "current_version": {"status": "stat_observed"}},
                      {"relative_path": "beta.py", "version_changed": False,
                       "current_version": {"status": "stat_observed"}}]}, **updates)

    def test_same_changed_fact_merges_despite_different_stable_companions(self):
        first = self.source(paths=("alpha.py", "beta.py"))
        second = self.source(paths=("alpha.py", "gamma.py"))
        for source in (first, second):
            self.expose(source)
        self.change("alpha.py", "alpha changed once\n")
        cards = self.cards(self.context())
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual({file["path"] for file in card["files"]}, {"alpha.py"})
        self.assertEqual({card["memory_id"], *[member["memory_id"] for member in card["related_memories"]]},
                         {first["memory_id"], second["memory_id"]})
        for source, stable in ((first, "beta.py"), (second, "gamma.py")):
            state = json.loads(self.exposure(source["memory_id"])["file_state"])
            self.assertIn(stable, state)  # Preserve the earlier explicit display.
            self.assertEqual(state["alpha.py"], self.item(source)["file_state"]["alpha.py"])

    def test_coalescing_retains_distinct_version_state_and_evidence_grade(self):
        original = self.notice()
        for changes in ({"state": "unknown"}, {"validation_level": "tool_observed"},
                        {"file_state": {"alpha.py": "other-version", "beta.py": "stable-beta"}}):
            with self.subTest(changes=changes):
                self.assertEqual(len(coalesce([copy.deepcopy(original), self.notice("two", **changes)])), 2)

    def test_source_risks_and_restorations_do_not_merge_as_ordinary_file_facts(self):
        for changes in ({"source_changed": True}, {"state": "conflicted"}, {"state": "denied"},
                        {"state": "missing"}, {"candidate_reference_risk": ["risk"]},
                        {"published_knowledge_risk": ["risk"]}, {"pending_counterexample_count": 1},
                        {"completion": "partial"}, {"_unseen_file_paths": ["new.py"]},
                        {"files": [{"relative_path": "alpha.py", "version_changed": False}]}):
            with self.subTest(changes=changes):
                first, risk = self.notice(), self.notice("two", **changes)
                self.assertEqual(coalesce([first, risk]), [dict(first, _notice_members=[first]), risk])

    def test_budget_omitted_changed_path_keeps_previous_observation_and_can_notify_later(self):
        source = self.source(paths=("alpha.py", "beta.py"))
        self.expose(source)
        before = self.exposure(source["memory_id"])
        self.change("alpha.py", "alpha changed\n")
        self.change("beta.py", "beta changed\n")
        saved = self.saved_delivery()
        full = self.cards(self.context())[0]
        self.assertEqual(len(full["files"]), 2)
        limited = dict(full, files=full["files"][:1])
        self.restore_delivery(saved)
        self.r.config["work_memory"]["max_context_chars"] = self.budget_for_card(limited)
        card = self.cards(self.context())[0]
        self.assertEqual(card["files"], limited["files"])
        visible = card["files"][0]["path"]
        hidden = ({"alpha.py", "beta.py"} - {visible}).pop()
        after = json.loads(self.exposure(source["memory_id"])["file_state"])
        self.assertEqual(after[hidden], json.loads(before["file_state"])[hidden])
        self.assertEqual(after[visible], self.item(source)["file_state"][visible])
        later = self.cards(self.context(31))
        self.assertTrue(any(file["path"] == hidden for item in later for file in item.get("files", ())))

    def test_budget_with_no_changed_path_does_not_acknowledge_an_unseen_fact(self):
        source = self.source(paths=("alpha.py",))
        self.expose(source)
        before = self.exposure(source["memory_id"])
        self.change("alpha.py", "alpha changed\n")
        saved = self.saved_delivery()
        full = self.cards(self.context())[0]
        limited = {key: value for key, value in full.items() if key != "files"}
        self.restore_delivery(saved)
        self.r.config["work_memory"]["max_context_chars"] = self.budget_for_card(limited)
        self.assertEqual(self.cards(self.context()), [])
        self.assertEqual(self.exposure(source["memory_id"]), before)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_notice_delivery").fetchone()[0], 0)
        self.r.config["work_memory"]["max_context_chars"] = 1800
        self.assertIn(source["memory_id"], self.context(31))

    def test_long_changed_path_is_complete_when_it_fits(self):
        long_name = "long_" + "a" * 110 + ".py"
        self.change(long_name, "long original\n")
        source = self.source(paths=(long_name,))
        self.expose(source)
        self.change(long_name, "long changed\n")
        card = self.cards(self.context())[0]
        self.assertEqual(card["files"][0]["path"], long_name)
        self.assertEqual(self.cards(self.context(31)), [])

    def test_omitted_merged_member_keeps_its_exposure_until_actually_identified(self):
        sources = [self.source(paths=("alpha.py",)) for _ in range(4)]
        for source in sources:
            self.expose(source)
        before = {source["memory_id"]: self.exposure(source["memory_id"]) for source in sources}
        self.change("alpha.py", "alpha changed\n")
        saved = self.saved_delivery()
        full = self.cards(self.context())[0]
        self.assertEqual(len(full["related_memories"]), 3)
        limited = dict(full, related_memories=full["related_memories"][:1])
        self.restore_delivery(saved)
        self.r.config["work_memory"]["max_context_chars"] = self.budget_for_card(limited)
        card = self.cards(self.context())[0]
        visible = {card["memory_id"], *[member["memory_id"] for member in card.get("related_memories", [])]}
        self.assertEqual(len(visible), 2)
        for identity in set(before) - visible:
            self.assertEqual(self.exposure(identity), before[identity])
        for identity in visible:
            self.assertNotEqual(self.exposure(identity)["file_state"], before[identity]["file_state"])


if __name__ == "__main__":
    unittest.main()
