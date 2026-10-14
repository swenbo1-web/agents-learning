"""Frozen cross-document history context; no external model or real database."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_learning.history_context import enrich_history_packet, link_history_evidence
from agent_learning.review_packet import review_instruction
from agent_learning.runtime import ContractError, Runtime, encode


class HistoryContextTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "build" / "local" / "history-context-tests"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="run-", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.docs = self.workspace / "Docs"
        self.docs.mkdir(parents=True)
        for name, text in {
            "main.md": "SQLite WAL transaction rollback governs journal recovery.\n",
            "related-a.md": "SQLite WAL journal recovery preserves transaction rollback.\n",
            "related-b.md": "SQLite transaction rollback requires explicit journal recovery checks.\n",
            "unrelated.md": "Orange orchard sunshine and annual apple harvest.\n",
        }.items():
            (self.docs / name).write_text(text, encoding="utf-8")
        self.r = Runtime(self.root / "home", config={})
        self.addCleanup(self.r.close)
        self.r.config["admin"]["max_input_chars"] = 20000
        self.project = self.r.project(self.workspace)
        self.job = self.materialize()
        self.primary = self.case_for("Docs/main.md")
        self.packet = self.r.review_packet(self.project, self.primary["id"])

    def activate(self, job):
        self.r.db.execute("UPDATE history_jobs SET state='reviewing',review_enabled=1,owner='isolated-test',lease_until=? WHERE id=?",
                          (time.time() + 120, job))

    def materialize(self):
        job = self.r.history_prepare(self.project, self.workspace)["job_id"]
        self.r.history_run(self.project, job)
        self.activate(job)
        return job

    def case_for(self, relative, job=None):
        rows = self.r.db.execute(
            "SELECT p.*,m.body AS material_body,u.unit_index FROM history_case_links l "
            "JOIN problems p ON p.id=l.problem_id AND p.project=l.project "
            "JOIN history_units u ON u.id=l.unit_id JOIN history_materials m ON m.id=u.material_id "
            "WHERE l.job_id=? ORDER BY u.unit_index", (job or self.job,)).fetchall()
        return next(row for row in rows
                    if json.loads(row["material_body"])["relative_path"].replace("\\", "/") == relative)

    def enriched(self, **kwargs):
        return enrich_history_packet(self.r, self.project, self.packet, self.job, **kwargs)

    @staticmethod
    def related_refs(packet):
        return [event["id"] for item in packet.get("related_history", []) for event in item["events"]]

    def refs(self, packet):
        return [packet["events"][0]["id"], *self.related_refs(packet)]

    def role_count(self):
        return self.r.db.execute("SELECT COUNT(*) FROM problem_events WHERE problem=? AND role='history_related'",
                                 (self.primary["id"],)).fetchone()[0]

    def test_enrichment_is_read_only_preserves_primary_and_ranks_meaningful_sources(self):
        original = deepcopy(self.packet)
        before = self.r.db.total_changes
        with patch("agent_learning.admin.subprocess.Popen") as model:
            packet = self.enriched()
        model.assert_not_called()
        self.assertEqual(before, self.r.db.total_changes)
        self.assertEqual(original, self.packet)
        self.assertEqual(packet["events"], original["events"])
        self.assertEqual(packet["evidence_count"], original["evidence_count"])
        self.assertEqual(packet["purpose"], "history_review")
        related = packet["related_history"]
        self.assertEqual(len(related), 2)
        self.assertEqual(len({item["material_id"] for item in related}), 2)
        self.assertTrue(all("sqlite" in item["matched_terms"] for item in related))
        self.assertTrue(all("unrelated" not in item["source_key"] for item in related))
        self.assertLessEqual(len(encode(related)), 5000)
        self.assertLessEqual(len(self.related_refs(packet)), 4)
        self.assertLessEqual(len(review_instruction(packet)), 20000)
        for item in related:
            self.assertIn("not an independent review", item["limitations"])
            for event in item["events"]:
                raw = self.r.db.execute("SELECT * FROM events WHERE id=?", (event["id"],)).fetchone()
                self.assertEqual(event["payload"], json.loads(raw["payload"]))
                self.assertEqual(event["type"], "HistoryImport")
                self.assertEqual(event["outcome"], "reported")
                self.assertIsNone(raw["task_id"])
        self.assertEqual(packet, self.enriched())

    def test_budget_never_trims_primary_or_cuts_an_event(self):
        base = dict(self.packet, purpose="history_review")
        exact = len(review_instruction(base))
        packet = self.enriched(max_chars=exact)
        self.assertNotIn("related_history", packet)
        self.assertEqual(packet["events"], self.packet["events"])
        self.assertEqual(len(review_instruction(packet)), exact)
        limited = self.enriched(max_chars=exact + 700)
        self.assertLessEqual(len(review_instruction(limited)), exact + 700)
        self.assertEqual(limited["events"], self.packet["events"])
        for item in limited.get("related_history", []):
            for event in item["events"]:
                payload = self.r.db.execute("SELECT payload FROM events WHERE id=?", (event["id"],)).fetchone()[0]
                self.assertEqual(event["payload"], json.loads(payload))

    def test_only_frozen_records_are_used_after_files_disappear(self):
        for path in self.docs.iterdir():
            path.unlink()
        with patch.object(Path, "open", side_effect=AssertionError("project files must not be opened")):
            packet = self.enriched()
        self.assertEqual(len(packet["related_history"]), 2)

    def test_guard_rejects_pause_expiry_and_other_job(self):
        self.r.history_pause(self.project, self.job)
        with self.assertRaises(ContractError):
            self.enriched()
        self.activate(self.job)
        self.r.db.execute("UPDATE history_jobs SET lease_until=0 WHERE id=?", (self.job,))
        with self.assertRaises(ContractError):
            self.enriched()
        self.activate(self.job)
        with self.assertRaises(ContractError):
            enrich_history_packet(self.r, self.project, self.packet, "not-the-approved-job")

    def test_unrelated_language_words_and_single_chinese_characters_do_not_match(self):
        for name, text in {"main.md": "甲乙丙丁", "related-a.md": "甲戊己庚", "related-b.md": "辛壬癸子", "unrelated.md": "丑寅卯辰"}.items():
            (self.docs / name).write_text(text, encoding="utf-8")
        job = self.materialize()
        primary = self.case_for("Docs/main.md", job)
        packet = self.r.review_packet(self.project, primary["id"])
        enriched = enrich_history_packet(self.r, self.project, packet, job)
        self.assertNotIn("related_history", enriched)
        self.assertEqual(enriched["purpose"], "history_review")
        (self.docs / "main.md").write_text("守门员点球扑救方向约束", encoding="utf-8")
        (self.docs / "related-a.md").write_text("点球扑救方向受限时保存目标", encoding="utf-8")
        job = self.materialize()
        primary = self.case_for("Docs/main.md", job)
        packet = self.r.review_packet(self.project, primary["id"])
        enriched = enrich_history_packet(self.r, self.project, packet, job)
        self.assertEqual(len(enriched["related_history"]), 1)
        self.assertIn("点球", enriched["related_history"][0]["matched_terms"])

    def test_cited_links_are_idempotent_and_do_not_review_related_cases(self):
        packet = self.enriched()
        before = [tuple(row) for row in self.r.db.execute("SELECT id,revision,status FROM problems ORDER BY id")]
        result = link_history_evidence(self.r, self.project, packet, self.refs(packet), self.job)
        self.assertEqual(set(result["linked_event_ids"]), set(self.related_refs(packet)))
        self.assertEqual(self.role_count(), len(self.related_refs(packet)))
        self.assertEqual(before, [tuple(row) for row in self.r.db.execute("SELECT id,revision,status FROM problems ORDER BY id")])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts").fetchone()[0], 0)
        repeated = link_history_evidence(self.r, self.project, packet, self.refs(packet), self.job)
        self.assertEqual(repeated["linked_event_ids"], [])
        body = dict(problem_id=self.primary["id"], problem_revision=self.primary["revision"],
                    title="SQLite history fact", content="The related archived documents discuss WAL rollback.",
                    category="project", knowledge_kind="fact", applicability="Frozen archive version only",
                    limitations="Reported text; not current execution verification", evidence_refs=self.refs(packet),
                    validation_level="reported")
        result = self.r.submit_candidate(self.project, **body)
        self.assertEqual(result["status"], "candidate")

    def test_uncited_related_events_are_not_linked_and_primary_only_still_works(self):
        packet = self.enriched()
        first = self.related_refs(packet)[0]
        result = link_history_evidence(self.r, self.project, packet, [packet["events"][0]["id"], first], self.job)
        self.assertEqual(result["linked_event_ids"], [first])
        self.assertEqual(self.role_count(), 1)
        result = link_history_evidence(self.r, self.project, packet, [packet["events"][0]["id"]], self.job)
        self.assertEqual(result["related_event_ids"], [])

    def test_unknown_and_related_only_refs_are_rejected_before_writes(self):
        packet = self.enriched()
        for refs in (self.related_refs(packet), [*self.refs(packet), "unseen-event"]):
            with self.subTest(refs=refs), self.assertRaises(ContractError):
                link_history_evidence(self.r, self.project, packet, refs, self.job)
            self.assertEqual(self.role_count(), 0)

    def test_source_and_primary_snapshot_changes_reject_all_links(self):
        packet = self.enriched()
        ref = self.related_refs(packet)[0]
        original = self.r.db.execute("SELECT payload FROM events WHERE id=?", (ref,)).fetchone()[0]
        payload = json.loads(original)
        payload["content"] += " changed after model input"
        self.r.db.execute("UPDATE events SET payload=? WHERE id=?", (encode(payload), ref))
        with self.assertRaises(ContractError):
            link_history_evidence(self.r, self.project, packet, self.refs(packet), self.job)
        self.assertEqual(self.role_count(), 0)
        self.r.db.execute("UPDATE events SET payload=? WHERE id=?", (original, ref))
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (self.primary["id"],))
        with self.assertRaises(ContractError):
            link_history_evidence(self.r, self.project, packet, self.refs(packet), self.job)
        self.assertEqual(self.role_count(), 0)

    def test_old_related_material_cannot_cross_into_a_new_job(self):
        packet = self.enriched()
        for name in ("related-a.md", "related-b.md"):
            path = self.docs / name
            path.write_text(path.read_text(encoding="utf-8") + "New archive version.", encoding="utf-8")
        new_job = self.materialize()
        self.assertNotEqual(new_job, self.job)
        with self.assertRaises(ContractError):
            link_history_evidence(self.r, self.project, packet, self.refs(packet), new_job)
        self.assertEqual(self.role_count(), 0)

    def test_sqlite_failure_rolls_back_links_even_inside_an_outer_transaction(self):
        packet = self.enriched()
        self.assertGreaterEqual(len(self.related_refs(packet)), 2)
        self.r.db.execute("""CREATE TRIGGER synthetic_link_failure BEFORE INSERT ON problem_events
            WHEN NEW.role='history_related' AND (SELECT COUNT(*) FROM problem_events
            WHERE problem=NEW.problem AND role='history_related')>=1
            BEGIN SELECT RAISE(ABORT,'synthetic second link failure'); END""")
        with self.r.transaction():
            with self.assertRaises(sqlite3.IntegrityError):
                link_history_evidence(self.r, self.project, packet, self.refs(packet), self.job)
            self.assertEqual(self.role_count(), 0)
        self.assertEqual(self.role_count(), 0)

    def test_duplicate_materials_and_tampered_unit_identity_are_not_accepted(self):
        packet = self.enriched()
        tampered = deepcopy(packet)
        tampered["related_history"][1] = deepcopy(tampered["related_history"][0])
        with self.assertRaises(ContractError):
            link_history_evidence(self.r, self.project, tampered, self.refs(tampered), self.job)
        self.assertEqual(self.role_count(), 0)
        source = packet["related_history"][0]
        self.r.db.execute("UPDATE history_units SET body=? WHERE id=?", (encode({"parts": 0}), source["unit_id"]))
        with self.assertRaises(ContractError):
            link_history_evidence(self.r, self.project, packet, self.refs(packet), self.job)
        self.assertEqual(self.role_count(), 0)

    def test_scan_is_bounded_and_oversized_events_are_not_silently_cut(self):
        for index in range(132):
            (self.docs / (f"extra-{index:03}.md")).write_text("Orchard sunshine orange apple harvest.", encoding="utf-8")
        job = self.materialize()
        primary = self.case_for("Docs/main.md", job)
        packet = self.r.review_packet(self.project, primary["id"])
        from agent_learning import history_context
        original = history_context._tokens
        with patch.object(history_context, "_tokens", wraps=original) as tokenize:
            result = enrich_history_packet(self.r, self.project, packet, job)
        self.assertLessEqual(tokenize.call_count, 129)
        self.assertEqual(result["events"], packet["events"])
        # The bounded scan may intentionally omit later relevant sources; it
        # never converts those omissions into a completed review.
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_receipts").fetchone()[0], 0)
        related = self.enriched()["related_history"][0]
        event = related["events"][0]
        payload = deepcopy(event["payload"])
        payload["content"] = "Visible archived text " * 500
        self.r.db.execute("UPDATE events SET payload=? WHERE id=?", (encode(payload), event["id"]))
        limited = self.enriched()
        self.assertNotIn(event["id"], self.related_refs(limited))
        self.assertEqual(json.loads(self.r.db.execute("SELECT payload FROM events WHERE id=?", (event["id"],)).fetchone()[0]), payload)

    def test_malformed_budget_packet_and_task_bound_archives_fail_closed(self):
        for budget in (True, 999, 200001, "20000"):
            with self.assertRaises(ContractError):
                self.enriched(max_chars=budget)
        with self.assertRaises(ContractError):
            enrich_history_packet(self.r, self.project, [], self.job)
        with self.assertRaises(ContractError):
            link_history_evidence(self.r, self.project, {}, ["event"], self.job)
        self.r.db.execute("UPDATE problems SET task_id='forged-task' WHERE id=?", (self.primary["id"],))
        with self.assertRaises(ContractError):
            self.enriched()


if __name__ == "__main__":
    unittest.main()
