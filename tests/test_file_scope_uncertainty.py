"""Unknown checkout observations never masquerade as confirmed file changes."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_learning import candidate_references
from agent_learning.mcp import Server
from agent_learning.runtime import Runtime, encode
from agent_learning.work_files import compare_file_versions, normalize_file_target
from tests import test_context_dedup as dedup_fixtures


class FileScopeUncertaintyTests(unittest.TestCase):
    def setUp(self):
        self.f = dedup_fixtures.ContextDedupTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.r.config["injection_max_chars"] = 6000
        self.f.r.config["work_memory"]["max_context_chars"] = 1600

    def source(self, path, content):
        f = self.f
        source = f.source(content, capture=False)
        from agent_learning.workspace_links import file_workspace_roots
        link = normalize_file_target(str(path), f.workspace, roots=file_workspace_roots(f.r, f.p, f.workspace))
        self.assertIsNotNone(link)
        with f.r.transaction():
            f.r.db.execute("INSERT INTO work_file_links VALUES(?,?,?,?,?,?,?)", (
                f.p, source["event"]["id"], link["checkout_key"], link["relative_path"],
                link["path_key"], "read_target", encode(link["version"])))
            source["memory"] = f.r._work_capture_event(f.p, source["event"]["id"], f.workspace)
        return source

    def linked(self):
        f = self.f
        linked = f.root / "linked"
        linked.mkdir()
        path = linked / "ledger.py"
        path.write_text("original = 1\n", encoding="utf-8")
        settings = json.loads(encode(f.r.config))
        settings["linked_workspaces"] = {f.p: [str(linked)]}
        f.r.close()
        f.r = Runtime(f.root / "home", config=settings)
        self.addCleanup(f.r.close)
        return linked, path

    def test_comparison_separates_verified_drift_unknown_scope_and_unavailable(self):
        before = {"status": "stat_observed", "size": 10, "mtime_ns": 100, "content_hash": None}
        same = compare_file_versions(before, dict(before))
        self.assertEqual(same, {"changed": False, "verified": True, "unverified": False, "blocked": False})
        self.assertTrue(compare_file_versions(before, dict(before, size=11))["changed"])
        for status in ("outside_workspace", "outside_scope", "unknown"):
            self.assertEqual(compare_file_versions(before, {"status": status}),
                {"changed": False, "verified": False, "unverified": True, "blocked": False})
            self.assertFalse(candidate_references._environment_blocked({"applicability": {"status": "unknown"},
                "files": [{"recorded": before, "current": {"status": status}}]}))
        unavailable = compare_file_versions(before, {"status": "unavailable"})
        self.assertFalse(unavailable["changed"])
        self.assertTrue(unavailable["blocked"])
        self.assertTrue(unavailable["unverified"])

    def test_never_linked_foreign_checkout_keeps_formal_and_candidate_as_unverified(self):
        f = self.f
        path = f.workspace / "ledger.py"
        path.write_text("unchanged\n", encoding="utf-8")
        source = self.source(path, f.CONTENT)
        formal, _ = f.published(source)
        alternative = self.source(path, "dedupfocus: retain an unpublished ledger alternative.")
        candidate, _ = f.candidate(alternative, title="dedupfocus unpublished alternative")
        f.enable_candidates()
        foreign = f.root / "foreign"
        foreign.mkdir()
        (foreign / "ledger.py").write_text("different unrelated checkout\n", encoding="utf-8")
        f.workspace = foreign
        text = f.context()
        cards = f.formal_cards(text)
        item = next(card for card in cards if card["knowledge_id"] == formal["knowledge_id"])
        self.assertTrue(item["source_files_unverified"])
        self.assertNotIn("source_files_changed", item)
        self.assertIn(candidate["candidate_id"], text)
        candidate_card = next(json.loads(line[line.index("{"):]) for line in text.splitlines() if line.startswith("[待定参考"))
        self.assertTrue(candidate_card["source_files_unverified"])
        self.assertFalse(candidate_card["execution_authority"])
        again = f.context("continue")
        self.assertEqual(f.formal_cards(again), [])
        self.assertNotIn(candidate["candidate_id"], again)

    def test_public_bounded_read_and_search_keep_unknown_qualifier(self):
        f = self.f
        source = f.source(file=True)
        formal, _ = f.published(source)
        foreign = f.root / "foreign"
        foreign.mkdir()
        server = Server(f.r, f.p, context_handle=f.handle, workspace=foreign)
        for budget in (1024, 1200):
            page = server.call("knowledge_read", {"knowledge_id": formal["knowledge_id"], "max_chars": budget})
            self.assertTrue(page["source_files_unverified"])
            self.assertNotIn("source_files_changed", page)
            self.assertLessEqual(len(encode(page)), budget)
        result = server.call("knowledge_search", {"query": "dedupfocus", "max_chars": 1600})
        self.assertTrue(result["items"][0]["source_files_unverified"])

    def test_unknown_formal_cannot_claim_complete_work_coverage_and_risk_survives_small_cards(self):
        f = self.f
        source = f.source(file=True)
        formal, _ = f.published(source)
        foreign = f.root / "foreign"
        foreign.mkdir()
        f.workspace = foreign
        f.r.config["work_memory"]["automatic_context"] = False
        self.assertIn(formal["knowledge_id"], f.context())
        f.r.config["work_memory"]["automatic_context"] = True
        self.assertIn(source["memory"]["memory_id"], f.context("continue"))
        for budget in (128, 450, 750, 2400):
            f.r.config["injection_max_chars"] = budget
            text = f.context(compact=True)
            self.assertLessEqual(len(text), budget)
            for card in f.formal_cards(text):
                self.assertTrue(card["source_files_unverified"])
                self.assertNotIn("source_files_changed", card)

    def test_delivered_stat_scope_loss_warns_once_without_inspecting_revoked_root(self):
        f = self.f
        linked, path = self.linked()
        formal, _ = f.published(self.source(path, f.CONTENT))
        f.r.config["work_memory"]["automatic_context"] = False
        self.assertIn(formal["knowledge_id"], f.context())
        f.r.config["linked_workspaces"] = {}
        original = Path.stat
        def guarded(target, *args, **kwargs):
            if target == linked or linked in target.parents:
                raise AssertionError("revoked linked root was inspected")
            return original(target, *args, **kwargs)
        with patch.object(Path, "stat", guarded):
            text = f.context("continue")
            again = f.context("continue")
        self.assertIn("当前无法核实", text)
        self.assertNotIn("关联文件已变化", text)
        self.assertNotIn("当前无法核实", again)
        self.assertEqual(f.formal_cards(text), [])
        self.assertEqual(f.formal_cards(again), [])

    def test_scope_loss_notice_waits_for_complete_budget_and_legacy_snapshot_is_conservative(self):
        f = self.f
        _, path = self.linked()
        formal, _ = f.published(self.source(path, f.CONTENT))
        f.r.config["work_memory"]["automatic_context"] = False
        f.context()
        row = f.r.db.execute("SELECT state FROM context_exposures WHERE knowledge_id=?", (formal["knowledge_id"],)).fetchone()
        state = json.loads(row[0])
        state["_files"].pop("files")  # 1.4.61-style snapshot has no additive verified/scope metadata.
        f.r.db.execute("UPDATE context_exposures SET state=? WHERE knowledge_id=?", (encode(state), formal["knowledge_id"]))
        f.r.config["linked_workspaces"] = {}
        f.r.config["injection_max_chars"] = 128
        self.assertNotIn("当前无法核实", f.context("continue"))
        f.r.config["injection_max_chars"] = 6000
        self.assertIn("当前无法核实", f.context("continue"))
        self.assertNotIn("当前无法核实", f.context("continue"))

    def test_candidate_scope_loss_retains_an_explicit_unknown_notice(self):
        f = self.f
        _, path = self.linked()
        candidate, _ = f.candidate(self.source(path, f.CONTENT))
        f.enable_candidates()
        item = candidate_references.search(f.r, f.p, "dedupfocus", workspace=f.workspace)[0]
        candidate_references.record_exposure(f.r, f.p, f.handle, item, workspace=f.workspace)
        f.r.config["linked_workspaces"] = {}
        notes = candidate_references.notifications(f.r, f.p, f.handle, workspace=f.workspace)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["reason"], "reference_files_unverified")
        self.assertIn("当前无法核实", notes[0]["text"])
        self.assertEqual(candidate_references.ack_notifications(f.r, f.p, f.handle, notes), 0)
        self.assertEqual(candidate_references.ack_notifications(f.r, f.p, f.handle, notes, workspace=f.workspace), 1)
        self.assertEqual(candidate_references.notifications(f.r, f.p, f.handle, workspace=f.workspace), [])
        current = candidate_references.search(f.r, f.p, "dedupfocus", workspace=f.workspace)[0]
        page = candidate_references.read(f.r, f.p, f.handle, candidate["candidate_id"], current["reference_version"], workspace=f.workspace, max_chars=512)
        self.assertTrue(page["source_files_unverified"])
        self.assertLessEqual(len(encode(page)), 512)

    def test_undelivered_formal_scope_loss_survives_restored_scope_and_same_stat(self):
        f = self.f
        linked, path = self.linked()
        formal, _ = f.published(self.source(path, f.CONTENT))
        f.r.config["work_memory"]["automatic_context"] = False
        f.context()
        f.r.config["linked_workspaces"] = {}
        f.r.config["injection_max_chars"] = 128
        f.context("continue")
        saved = json.loads(f.r.db.execute("SELECT state FROM context_exposures WHERE knowledge_id=?",
                                         (formal["knowledge_id"],)).fetchone()[0])["_files"]
        self.assertIs(saved["pending_unverified"], True)
        f.r.config["linked_workspaces"] = {f.p: [str(linked)]}
        f.r.config["injection_max_chars"] = 6000
        Server(f.r, f.p, context_handle=f.handle, workspace=f.workspace).call("knowledge_read", {
            "knowledge_id": formal["knowledge_id"]})
        restored = f.context("continue")
        self.assertIn("曾观察到", restored)
        self.assertIn("当前已恢复可核验", restored)
        self.assertNotIn("当前无法核实", restored)
        self.assertNotIn("关联文件已变化", restored)
        self.assertNotIn("曾观察到", f.context("continue"))
        # A newer actual stat change must keep its stronger warning.
        f.r.config["linked_workspaces"] = {}
        f.r.config["injection_max_chars"] = 128
        f.context("continue")
        path.write_text("a genuinely different source stat\n", encoding="utf-8")
        f.r.config["linked_workspaces"] = {f.p: [str(linked)]}
        f.r.config["injection_max_chars"] = 6000
        self.assertIn("关联文件已变化", f.context("continue"))

    def test_authorized_file_unavailability_remains_a_risk_without_claiming_drift(self):
        f = self.f
        path = f.workspace / "ledger.py"
        path.write_text("original\n", encoding="utf-8")
        source = self.source(path, f.CONTENT)
        formal, _ = f.published(source)
        alternative = self.source(path, "dedupfocus: unpublished ledger alternate.")
        candidate, _ = f.candidate(alternative, title="dedupfocus alternate unavailable")
        f.enable_candidates()
        f.context()
        path.unlink()
        text = f.context("continue")
        self.assertIn("当前无法核实", text)
        self.assertNotIn("关联文件已变化", text)
        found = candidate_references.search(f.r, f.p, "dedupfocus", workspace=f.workspace)
        self.assertNotIn(candidate["candidate_id"], {item["candidate_id"] for item in found})
        page = Server(f.r, f.p, workspace=f.workspace).call("knowledge_read", {"knowledge_id": formal["knowledge_id"]})
        self.assertTrue(page["source_files_unverified"])
        self.assertNotIn("source_files_changed", page)
