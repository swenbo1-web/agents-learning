"""Automatic context coverage is conservative, scoped and discardable.

All records are synthetic; assertions use Runtime context and MCP output.
Legacy duplicate IDs are constructed only inside isolated SQLite fixtures.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning import candidate_references
from agent_learning.mcp import Server
from agent_learning.runtime import Runtime, encode, knowledge_fingerprint
from agent_learning.work_files import normalize_file_target


class ContextDedupTests(unittest.TestCase):
    CONTENT = "dedupfocus: preserve exact ledger ordering."

    def setUp(self):
        area = Path(__file__).resolve().parents[1] / "build" / "local" / "context-dedup-tests"
        area.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="case-", dir=area)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("tmp", "home", "codex", "runtime-releases", "checkout"):
            (self.root / name).mkdir()
        config = self.root / "config.json"
        config.write_text("{}", encoding="utf-8")
        environment = {"PYTHONDONTWRITEBYTECODE": "1", "ALP_HOME": str(self.root / "home"),
            "ALP_CONFIG": str(config), "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"),
            "CODEX_HOME": str(self.root / "codex")}
        environment.update({name: str(self.root / "tmp") for name in ("TMP", "TEMP", "TMPDIR")})
        isolated = patch.dict(os.environ, environment)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.workspace = self.root / "checkout"
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": False},
            "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.conversation = "reader"
        self.r.task_update(self.p, self.conversation, "dedupfocus ledger ordering", {"mode": "analysis"})
        self.handle = self.r.register_context(self.p, self.conversation)["context_handle"]
        self.sequence = 0

    def context(self, query="dedupfocus", **kwargs):
        return self.r._context(self.p, self.conversation, query=query,
            context_handle=self.handle, workspace=self.workspace, **kwargs)

    @staticmethod
    def formal_cards(text):
        return [json.loads(line) for line in text.splitlines()
                if line.startswith('{"') and "knowledge_id" in json.loads(line)]

    @staticmethod
    def work_cards(text):
        return [json.loads(line[len("[工作历史] "):]) for line in text.splitlines()
                if line.startswith("[工作历史] ")]

    def source(self, content=None, *, capture=True, file=False, observed=False):
        self.sequence += 1
        conversation = self.r.host_conversation("source-" + str(self.sequence))
        task = self.r.task_update(self.p, conversation, "dedupfocus source", {"mode": "analysis"})
        payload = ({"tool_name": "run_public_tests", "tool_response": {"exit_code": 0, "stdout": content or self.CONTENT}}
                   if observed else {"last_assistant_message": content or self.CONTENT})
        with self.r.transaction():
            event, _ = self.r._event(self.p, conversation, "PostToolUse" if observed else "Stop", payload,
                task_id=task["id"], task_revision=task["revision"], outcome="succeeded" if observed else "reported")
            if file:
                path = self.workspace / "ledger.py"
                path.write_text("ledger = 1\n", encoding="utf-8")
                link = normalize_file_target(str(path), self.workspace)
                self.r.db.execute("INSERT INTO work_file_links VALUES(?,?,?,?,?,?,?)", (
                    self.p, event["id"], link["checkout_key"], link["relative_path"],
                    link["path_key"], "read_target", encode(link["version"])))
            memory = self.r._work_capture_event(self.p, event["id"], self.workspace, raw=payload) if capture else None
        return {"event": event, "memory": memory, "conversation": conversation}

    def candidate(self, source=None, *, content=None, kind="fact", title="dedupfocus recorded fact",
                  applicability="Recorded ledger environment", limitations="Reported; verify current evidence"):
        source = source or self.source(content)
        problem = self.r.feedback(self.p, source["conversation"], "dedupfocus recorded finding",
            category="project", event_refs=[source["event"]["id"]])
        candidate = self.r.submit_candidate(self.p, problem_id=problem["id"],
            problem_revision=problem["revision"], title=title, content=content or self.CONTENT,
            category="project", knowledge_kind=kind, applicability=applicability,
            limitations=limitations, validation_level="reported", evidence_refs=[source["event"]["id"]])
        return candidate, source

    def published(self, source=None, **kwargs):
        candidate, source = self.candidate(source, **kwargs)
        return self.r.publish(self.p, candidate["candidate_id"]), source

    def clone_legacy(self, published, *, revision=None, **changes):
        """Reproduce preexisting duplicate IDs without changing live databases."""
        original = self.r.db.execute("SELECT * FROM knowledge WHERE project=? AND id=? AND revision=?",
            (self.p, published["knowledge_id"], published["revision"])).fetchone()
        body = dict(json.loads(original["body"]), **changes)
        identity = published["knowledge_id"] + "-duplicate-" + str(self.sequence)
        self.sequence += 1
        revision = published["revision"] if revision is None else revision
        with self.r.transaction():
            self.r.db.execute("INSERT INTO knowledge VALUES(?,?,?,?,?)", (self.p, identity, revision, encode(body), time.time()))
            self.r.db.execute("INSERT INTO active VALUES(?,?,?)", (self.p, identity, revision))
            self.r.db.execute("INSERT INTO knowledge_fingerprints VALUES(?,?,?,?)",
                (self.p, identity, revision, knowledge_fingerprint(body)))
            for row in self.r.db.execute("SELECT * FROM knowledge_sources WHERE project=? AND knowledge_id=? AND revision=?",
                    (self.p, published["knowledge_id"], published["revision"])).fetchall():
                self.r.db.execute("INSERT INTO knowledge_sources VALUES(?,?,?,?,?,?,?,?)", (
                    self.p, identity, revision, row["candidate_id"], row["problem_id"], row["problem_revision"],
                    row["evidence_refs"], row["created"]))
            self.r._index_knowledge(self.p, identity, revision, body)
            self.r.db.execute("UPDATE projects SET generation=generation+1 WHERE id=?", (self.p,))
        return {"knowledge_id": identity, "revision": revision}

    def enable_candidates(self):
        state = candidate_references.policy(self.r, self.p)
        candidate_references.configure(self.r, self.p, True, state["version"])

    def clone_candidate_legacy(self, candidate, **changes):
        row = self.r.db.execute("SELECT * FROM candidates WHERE project=? AND id=?",
            (self.p, candidate["candidate_id"])).fetchone()
        identity = candidate["candidate_id"] + "-duplicate-" + str(self.sequence)
        self.sequence += 1
        body = dict(json.loads(row["body"]), **changes)
        self.r.db.execute("INSERT INTO candidates(id,project,problem,problem_revision,body,status,created) "
            "VALUES(?,?,?,?,?,?,?)", (identity, self.p, row["problem"], row["problem_revision"],
            encode(body), "candidate", time.time()))
        return identity

    def antigravity(self):
        self.r = Runtime(self.root / "ag-home", host="antigravity", config={
            "admin": {"enabled": False}, "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.conversation = self.r.host_conversation("reader")
        self.r.task_update(self.p, self.conversation, "dedupfocus ledger ordering", {"mode": "analysis"})
        self.handle = self.r.register_context(self.p, self.conversation)["context_handle"]

    def test_persistent_formal_body_is_not_repeated_without_compaction(self):
        formal, _ = self.published()
        self.assertIn(formal["knowledge_id"], self.context())
        self.assertEqual(self.formal_cards(self.context()), [])
        self.assertEqual(self.formal_cards(self.context("continue")), [])

    def test_real_compaction_revalidates_and_restores_formal_body(self):
        formal, _ = self.published()
        self.context()
        restored = self.formal_cards(self.context(compact=True))
        self.assertEqual([card["knowledge_id"] for card in restored], [formal["knowledge_id"]])

    def test_manual_search_preview_is_not_automatic_body_coverage(self):
        formal, _ = self.published()
        preview = Server(self.r, self.p, context_handle=self.handle, workspace=self.workspace).call(
            "knowledge_search", {"query": "dedupfocus"})
        self.assertTrue(preview["items"])
        self.assertIn(formal["knowledge_id"], self.context())

    def test_manual_read_page_is_not_automatic_body_coverage(self):
        formal, _ = self.published()
        page = Server(self.r, self.p, context_handle=self.handle, workspace=self.workspace).call(
            "knowledge_read", {"knowledge_id": formal["knowledge_id"]})
        self.assertEqual(page["content"], self.CONTENT)
        self.assertIn(formal["knowledge_id"], self.context())

    def test_same_source_complete_duplicate_formal_ids_use_one_card(self):
        formal, _ = self.published()
        duplicate = self.clone_legacy(formal)
        cards = self.formal_cards(self.context())
        self.assertEqual(len(cards), 1)
        self.assertIn(cards[0]["knowledge_id"], {formal["knowledge_id"], duplicate["knowledge_id"]})

    def test_different_full_body_is_not_collapsed_when_preview_prefix_matches(self):
        content = "dedupfocus " + "recorded behavior " * 35 + " must allow replay."
        formal, _ = self.published(content=content)
        duplicate = self.clone_legacy(formal, content=content.replace("must allow replay", "must not allow replay"))
        cards = self.formal_cards(self.context())
        self.assertEqual({card["knowledge_id"] for card in cards}, {formal["knowledge_id"], duplicate["knowledge_id"]})

    def test_changed_applicability_is_not_collapsed(self):
        formal, _ = self.published()
        duplicate = self.clone_legacy(formal, applicability="Only the isolated archive environment")
        self.assertEqual({card["knowledge_id"] for card in self.formal_cards(self.context())},
            {formal["knowledge_id"], duplicate["knowledge_id"]})

    def test_changed_limitation_negation_is_not_collapsed(self):
        formal, _ = self.published(limitations="Replay is allowed after validation")
        duplicate = self.clone_legacy(formal, limitations="Replay is not allowed after validation")
        self.assertEqual({card["knowledge_id"] for card in self.formal_cards(self.context())},
            {formal["knowledge_id"], duplicate["knowledge_id"]})

    def test_changed_metadata_is_not_collapsed(self):
        formal, _ = self.published()
        duplicate = self.clone_legacy(formal, metadata={"tool": "compiler"})
        self.assertEqual({card["knowledge_id"] for card in self.formal_cards(self.context())},
            {formal["knowledge_id"], duplicate["knowledge_id"]})

    def test_different_revision_is_not_collapsed_across_ids(self):
        formal, _ = self.published()
        duplicate = self.clone_legacy(formal, revision=2)
        self.assertEqual({card["knowledge_id"] for card in self.formal_cards(self.context())},
            {formal["knowledge_id"], duplicate["knowledge_id"]})

    def test_different_evidence_level_is_not_collapsed(self):
        source = self.source(observed=True)
        formal, _ = self.published(source)
        duplicate = self.clone_legacy(formal, validation_level="tool_success")
        self.assertTrue(self.r.read(self.p, formal["knowledge_id"])["current_tool_success"])
        self.assertEqual({card["knowledge_id"] for card in self.formal_cards(self.context())},
            {formal["knowledge_id"], duplicate["knowledge_id"]})

    def test_same_body_from_distinct_source_events_is_not_collapsed(self):
        formal, _ = self.published()
        other = self.source()
        duplicate = self.clone_legacy(formal, evidence_refs=[other["event"]["id"]])
        origin = self.r.db.execute("SELECT body FROM knowledge WHERE project=? AND id=? AND revision=1",
            (self.p, formal["knowledge_id"])).fetchone()
        problem = json.loads(origin[0])["problem_id"]
        self.r.db.execute("INSERT INTO problem_events(problem,event) VALUES(?,?)", (problem, other["event"]["id"]))
        self.r.db.execute("UPDATE knowledge_sources SET evidence_refs=? WHERE project=? AND knowledge_id=?",
            (encode([other["event"]["id"]]), self.p, duplicate["knowledge_id"]))
        self.assertEqual(self.r.read(self.p, duplicate["knowledge_id"])["source_health"], "valid")
        self.assertEqual({card["knowledge_id"] for card in self.formal_cards(self.context())},
            {formal["knowledge_id"], duplicate["knowledge_id"]})

    def test_previously_injected_full_fact_covers_exact_later_work_body(self):
        formal, source = self.published()
        self.r.config["work_memory"]["automatic_context"] = False
        self.assertIn(formal["knowledge_id"], self.context())
        self.r.config["work_memory"]["automatic_context"] = True
        text = self.context()
        self.assertEqual(self.formal_cards(text), [])
        self.assertNotIn(source["memory"]["memory_id"], text)

    def test_formal_coverage_does_not_swallow_extra_work_facts(self):
        source = self.source(self.CONTENT + " Additional dedupfocus invariant: retain the audit receipt.")
        formal, _ = self.published(source)
        self.r.config["work_memory"]["automatic_context"] = False
        self.context()
        self.r.config["work_memory"]["automatic_context"] = True
        text = self.context()
        self.assertNotIn(formal["knowledge_id"], text)
        self.assertIn(source["memory"]["memory_id"], text)

    def test_solution_is_not_treated_as_reported_fact_coverage(self):
        _, source = self.published(kind="solution")
        self.r.config["work_memory"]["automatic_context"] = False
        self.context()
        self.r.config["work_memory"]["automatic_context"] = True
        self.assertIn(source["memory"]["memory_id"], self.context())

    def test_truncated_formal_summary_does_not_cover_complete_work_body(self):
        content = "dedupfocus " + "complete evidence requires careful review " * 18
        _, source = self.published(content=content)
        self.r.config["work_memory"]["automatic_context"] = False
        self.context()
        self.r.config["work_memory"]["automatic_context"] = True
        self.assertIn(source["memory"]["memory_id"], self.context())

    def test_old_candidate_title_card_does_not_cover_later_work_body(self):
        candidate, source = self.candidate()
        self.enable_candidates()
        self.r.config["work_memory"]["automatic_context"] = False
        self.assertIn(candidate["candidate_id"], self.context())
        self.r.config["work_memory"]["automatic_context"] = True
        self.assertIn(source["memory"]["memory_id"], self.context())

    def test_same_source_same_complete_candidate_body_is_not_repeated_under_another_id(self):
        candidate, _ = self.candidate()
        self.enable_candidates()
        self.r.config["work_memory"]["automatic_context"] = False
        self.assertIn(candidate["candidate_id"], self.context())
        duplicate = self.clone_candidate_legacy(candidate)
        assessment = candidate_references.assess(self.r, self.p, duplicate)
        self.assertTrue(assessment["eligible"], assessment.get("reason"))
        self.assertNotIn(duplicate, self.context())

    def test_candidate_equal_visible_title_and_summary_cannot_hide_different_complete_body(self):
        content = "dedupfocus: " + "recorded observations " * 25 + " replay is allowed."
        # Different full titles preserve the existing same-title governance
        # barrier; both titles and content previews render identically.
        title = "dedupfocus recorded comparison " * 3
        candidate, _ = self.candidate(content=content, title=title + "alpha")
        self.enable_candidates()
        self.r.config["work_memory"]["automatic_context"] = False
        self.assertIn(candidate["candidate_id"], self.context())
        duplicate = self.clone_candidate_legacy(candidate, title=title + "beta",
            content=content.replace("replay is allowed", "replay is not allowed"))
        assessment = candidate_references.assess(self.r, self.p, duplicate)
        self.assertTrue(assessment["eligible"], assessment.get("reason"))
        self.assertIn(duplicate, self.context())

    def test_revocation_of_dedup_survivor_does_not_hide_remaining_active_duplicate(self):
        formal, _ = self.published()
        duplicate = self.clone_legacy(formal)
        first = self.formal_cards(self.context())
        self.assertEqual(len(first), 1)
        survivor = first[0]["knowledge_id"]
        remaining = ({formal["knowledge_id"], duplicate["knowledge_id"]} - {survivor}).pop()
        self.r.revoke(self.p, survivor, expected_revision=1)
        text = self.context()
        self.assertIn("已撤销", text)
        self.assertIn(survivor, text)
        self.assertIn(remaining, {card["knowledge_id"] for card in self.formal_cards(text)})

    def test_work_file_change_notice_is_not_swallowed_by_cached_formal_fact(self):
        source = self.source(file=True)
        first = self.context()
        self.assertIn(source["memory"]["memory_id"], first)
        self.published(source)
        self.context()
        (self.workspace / "ledger.py").write_text("ledger = 'new longer value'\n", encoding="utf-8")
        text = self.context()
        changed = next(card for card in self.work_cards(text) if card["memory_id"] == source["memory"]["memory_id"])
        self.assertTrue(changed["file_state_changed"])

    def test_new_formal_delivery_does_not_swallow_same_source_work_file_warning(self):
        source = self.source(file=True)
        self.assertIn(source["memory"]["memory_id"], self.context())
        formal, _ = self.published(source)
        (self.workspace / "ledger.py").write_text("ledger = 'changed after the captured source'\n", encoding="utf-8")
        text = self.context()
        self.assertIn(formal["knowledge_id"], {card["knowledge_id"] for card in self.formal_cards(text)})
        changed = next(card for card in self.work_cards(text)
            if card["memory_id"] == source["memory"]["memory_id"])
        self.assertTrue(changed["file_state_changed"])
        self.assertEqual(changed["reference_role"], "source_update")
        self.assertNotIn("summary", changed)

    def test_revocation_after_selection_cannot_replay_the_same_work_claim(self):
        formal, source = self.published()
        self.r.config["work_memory"]["automatic_context"] = False
        self.context()
        self.r.config["work_memory"]["automatic_context"] = True
        original = self.r.work_validate_items
        def revoked(project, items, workspace=None, **kwargs):
            self.r.revoke(self.p, formal["knowledge_id"], expected_revision=formal["revision"])
            return original(project, items, workspace=workspace, **kwargs)
        with patch.object(self.r, "work_validate_items", side_effect=revoked):
            text = self.context()
        self.assertNotIn(source["memory"]["memory_id"], text)
        retained = self.r.work_read(self.p, source["memory"]["memory_id"], workspace=self.workspace)
        self.assertEqual(retained["content"], self.CONTENT)
        self.assertTrue(retained["published_knowledge_risk"])

    def test_missing_derived_cache_does_not_block_normal_automatic_injection(self):
        formal, _ = self.published()
        self.r.db.execute("DROP TABLE context_delivery_cache")
        text = self.context()
        self.assertIn(formal["knowledge_id"], text)
        self.assertEqual(len(self.formal_cards(text)), 1)

    def test_derived_cache_write_failure_does_not_rollback_formal_delivery(self):
        formal, _ = self.published()
        self.r.db.execute("CREATE TRIGGER synthetic_cache_failure BEFORE INSERT ON context_delivery_cache "
            "BEGIN SELECT RAISE(FAIL,'synthetic coverage write unavailable'); END")
        text = self.context()
        self.assertIn(formal["knowledge_id"], text)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_exposures WHERE knowledge_id=?",
            (formal["knowledge_id"],)).fetchone()[0], 1)

    def test_cache_rollback_cannot_undo_committed_formal_delivery_or_revocation_notice(self):
        old, _ = self.published()
        self.r.config["work_memory"]["automatic_context"] = False
        self.assertIn(old["knowledge_id"], self.context())
        self.r.revoke(self.p, old["knowledge_id"], expected_revision=old["revision"])
        new, _ = self.published(content="dedupfocus: retain revised audit ordering.",
            title="dedupfocus revised audit fact")
        self.r.db.execute("CREATE TRIGGER synthetic_cache_rollback BEFORE INSERT ON context_delivery_cache "
            "BEGIN SELECT RAISE(ROLLBACK,'synthetic cache transaction rollback'); END")
        query = "dedupfocus revised audit"
        text = self.context(query)
        self.assertIn(new["knowledge_id"], text)
        self.assertIn(old["knowledge_id"], text)
        self.assertIn("已撤销", text)
        context = self.r.db.execute("SELECT shown,last_query FROM contexts WHERE project=? AND conversation=?",
            (self.p, self.conversation)).fetchone()
        shown = json.loads(context["shown"])
        self.assertIn(f"{new['knowledge_id']}@{new['revision']}", shown)
        self.assertNotIn(f"{old['knowledge_id']}@{old['revision']}", shown)
        self.assertEqual(context["last_query"], query)
        exposed = {row[0] for row in self.r.db.execute("SELECT knowledge_id FROM context_exposures "
            "WHERE project=? AND conversation=?", (self.p, self.conversation))}
        self.assertIn(new["knowledge_id"], exposed)
        self.assertNotIn(old["knowledge_id"], exposed)
        self.assertNotIn("已撤销", self.context(query))

    def test_derived_delivery_cache_is_bounded_to_256_entries_per_conversation(self):
        formal, _ = self.published()
        self.r.config["work_memory"]["automatic_context"] = False
        self.assertIn(formal["knowledge_id"], self.context())
        columns = [row[1] for row in self.r.db.execute("PRAGMA table_info(context_delivery_cache)")]
        original = dict(self.r.db.execute("SELECT * FROM context_delivery_cache LIMIT 1").fetchone())
        for index in range(255):
            seeded = dict(original, scope=f"old-scope-{index}", item_id=f"old-item-{index}",
                material_key=f"old-material-{index}", created=float(index))
            self.r.db.execute("INSERT INTO context_delivery_cache(" + ",".join(columns) + ") VALUES(" +
                ",".join("?" for _ in columns) + ")", tuple(seeded[name] for name in columns))
        updated, _ = self.published(content="dedupfocus: retain a second distinct audit receipt.",
            title="dedupfocus second audit fact")
        self.assertIn(updated["knowledge_id"], self.context())
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM context_delivery_cache "
            "WHERE project=? AND conversation=? AND agent=''", (self.p, self.conversation)).fetchone()[0], 256)
        self.assertTrue(self.r.db.execute("SELECT 1 FROM context_delivery_cache WHERE project=? AND item_id=?",
            (self.p, updated["knowledge_id"])).fetchone())

    def test_new_task_does_not_inherit_previous_automatic_delivery_coverage(self):
        formal, _ = self.published()
        self.r.config["work_memory"]["automatic_context"] = False
        self.assertIn(formal["knowledge_id"], self.context())
        self.assertEqual(self.formal_cards(self.context()), [])
        self.r.task_update(self.p, self.conversation, "dedupfocus independent next task", new_task=True)
        self.assertIn(formal["knowledge_id"], self.context())

    def test_work_optional_rollback_does_not_leave_phantom_coverage(self):
        source = self.source()
        original = self.r.work_record_exposure
        def failed(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError("synthetic work exposure unavailable")
        with patch.object(self.r, "work_record_exposure", side_effect=failed):
            first = self.context()
        self.assertNotIn(source["memory"]["memory_id"], first)
        self.assertIn(source["memory"]["memory_id"], self.context())

    def test_antigravity_ephemeral_resends_current_formal_card_each_invocation(self):
        self.antigravity()
        formal, _ = self.published()
        for _ in range(2):
            self.assertIn(formal["knowledge_id"], self.context(delivery="ephemeral"))

    def test_ephemeral_file_context_does_not_borrow_prior_persistent_formal_coverage(self):
        self.antigravity()
        source = self.source(file=True)
        formal, _ = self.published(source)
        self.r.config["work_memory"]["automatic_context"] = False
        self.assertIn(formal["knowledge_id"], self.context(delivery="persistent"))
        self.r.config["work_memory"]["automatic_context"] = True
        text = self.context(delivery="ephemeral", tool_only=True,
            raw={"tool_name": "Read", "tool_input": {"path": "ledger.py"}})
        self.assertEqual(self.formal_cards(text), [])
        self.assertIn(source["memory"]["memory_id"], text)


if __name__ == "__main__":
    unittest.main()
