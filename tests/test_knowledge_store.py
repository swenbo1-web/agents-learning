"""真实 SQLite 回归：发布重试不倒退正文，来源和维护操作可追溯。"""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.runtime import ContractError, Runtime, encode


class KnowledgeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "state"
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.home)
        self.p = self.r.project(self.workspace)
        self.r.task_update(self.p, "chat", "核对知识发布", {"mode": "analysis"})

    def tearDown(self):
        self.r.close()
        self.temp.cleanup()

    def hook(self, kind, **extra):
        return self.r.handle_hook({"hook_event_name": kind, "session_id": "chat", "cwd": str(self.workspace), **extra})

    def candidate(self, summary="来源A", content="original compiler guidance", level="reported", refs=None):
        problem = self.r.feedback(self.p, "chat", summary, category="operation", event_refs=refs)
        packet = self.r.review_packet(self.p, problem["id"])
        return self.r.submit_candidate(self.p, problem_id=problem["id"], problem_revision=problem["revision"],
            title="compiler solution", content=content, category="operation", applicability="compiler projects",
            limitations="record evidence only", evidence_refs=[event["id"] for event in packet["events"]],
            validation_level=level)["candidate_id"]

    def generation(self):
        return self.r.db.execute("SELECT generation FROM projects WHERE id=?", (self.p,)).fetchone()[0]

    def test_old_receipt_replay_cannot_rollback_or_restore(self):
        candidate = self.candidate()
        first = self.r.publish(self.p, candidate)
        updated = self.r.publish(self.p, self.candidate("来源B", "corrected compiler guidance"),
                                 knowledge_id=first["knowledge_id"], expected_revision=1)
        before = self.generation()
        self.assertEqual(self.r.publish(self.p, candidate, expected_generation=0), first)
        self.assertEqual(self.r.read(self.p, first["knowledge_id"])["revision"], updated["revision"])
        self.assertEqual(self.generation(), before)
        self.r.revoke(self.p, first["knowledge_id"])
        before = self.generation()
        self.assertEqual(self.r.publish(self.p, candidate), first)
        self.assertEqual(self.generation(), before)
        self.assertEqual(self.r.search(self.p, "compiler")["items"], [])

    def test_receipt_retry_survives_source_changes_but_checks_host_epoch(self):
        candidate = self.candidate()
        epoch = self.r.host_epoch()
        first = self.r.publish(self.p, candidate, expected_epoch=epoch)
        self.hook("PostToolUse", tool_use_id="later", tool_name="Bash", tool_response={"exit_code": 0})
        self.assertEqual(self.r.publish(self.p, candidate), first)
        self.r.account_changed("account-test")
        with self.assertRaises(ContractError):
            self.r.publish(self.p, candidate, expected_epoch=epoch)
        self.assertEqual(self.r.publish(self.p, candidate), first)

    def test_explicit_receipt_matches_original_request(self):
        first = self.r.publish(self.p, self.candidate())
        candidate = self.candidate("revision", "corrected compiler guidance")
        result = self.r.publish(self.p, candidate, knowledge_id=first["knowledge_id"], expected_revision=1)
        self.assertEqual(self.r.publish(self.p, candidate, knowledge_id=first["knowledge_id"], expected_revision=1), result)
        self.assertEqual(self.r.publish(self.p, candidate), result)
        with self.assertRaises(ContractError):
            self.r.publish(self.p, candidate, knowledge_id=first["knowledge_id"], expected_revision=2)
        with self.assertRaises(ContractError):
            self.r.publish(self.p, candidate, knowledge_id=first["knowledge_id"], expected_revision=True)

    def test_dedup_retains_multiple_cases_and_advances_source_generation(self):
        first = self.r.publish(self.p, self.candidate())
        before = self.generation()
        candidate = self.candidate("来源B")
        result = self.r.publish(self.p, candidate)
        self.assertEqual(result["knowledge_id"], first["knowledge_id"])
        self.assertTrue(result["deduplicated"])
        item = self.r.read(self.p, first["knowledge_id"])
        self.assertEqual(len(item["sources"]), 2)
        self.assertEqual(len(item["evidence"]), 2)
        self.assertIn(candidate, [source["candidate_id"] for source in item["sources"]])
        self.assertEqual(item["knowledge_kind"], "solution")
        self.assertEqual(self.generation(), before + 1)
        self.r.publish(self.p, candidate)
        self.assertEqual(self.generation(), before + 1)

    def test_explicit_same_conclusion_adds_source_without_body_revision(self):
        first = self.r.publish(self.p, self.candidate())
        result = self.r.publish(self.p, self.candidate("来源B"), knowledge_id=first["knowledge_id"], expected_revision=1)
        self.assertEqual(result["revision"], 1)
        self.assertEqual(len(self.r.read(self.p, first["knowledge_id"])["sources"]), 2)

    def test_explicit_collision_cannot_create_two_active_equivalent_ids(self):
        first = self.r.publish(self.p, self.candidate())
        second = self.r.publish(self.p, self.candidate("另一个来源", "other conclusion"))
        candidate = self.candidate("更新来源")
        with self.assertRaises(ContractError):
            self.r.publish(self.p, candidate, knowledge_id=second["knowledge_id"], expected_revision=1)
        self.assertEqual(self.r.read(self.p, first["knowledge_id"])["revision"], 1)
        self.assertEqual(self.r.read(self.p, second["knowledge_id"])["content"], "other conclusion")

    def test_restore_requires_latest_revision_and_current_valid_source(self):
        first = self.r.publish(self.p, self.candidate())
        self.r.revoke(self.p, first["knowledge_id"])
        before = self.generation()
        with self.assertRaises(ContractError):
            self.r.restore(self.p, first["knowledge_id"], 2)
        with self.assertRaises(ContractError):
            self.r.restore(self.p, first["knowledge_id"], 1, expected_generation=before - 1)
        restored = self.r.restore(self.p, first["knowledge_id"], 1, expected_generation=before)
        self.assertEqual(restored["status"], "restored")
        self.assertIn("source_candidate_id", restored)
        self.assertEqual(self.r.read(self.p, first["knowledge_id"])["revision"], 1)
        self.assertEqual(self.generation(), before + 1)
        with self.assertRaises(ContractError):
            self.r.restore(self.p, first["knowledge_id"], 1)

    def test_restore_uses_another_valid_source_when_original_case_changed(self):
        first_candidate = self.candidate()
        first = self.r.publish(self.p, first_candidate)
        second_candidate = self.candidate("来源B")
        self.r.publish(self.p, second_candidate)
        problem = self.r.db.execute("SELECT problem FROM candidates WHERE id=?", (first_candidate,)).fetchone()[0]
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (problem,))
        self.r.revoke(self.p, first["knowledge_id"])
        result = self.r.restore(self.p, first["knowledge_id"], 1)
        self.assertEqual(result["source_candidate_id"], second_candidate)
        item = self.r.read(self.p, first["knowledge_id"])
        self.assertTrue(item["sources"][0]["problem_changed"])

    def test_restore_rejects_when_all_current_source_contracts_changed(self):
        candidate = self.candidate()
        first = self.r.publish(self.p, candidate)
        self.r.db.execute("UPDATE problems SET revision=revision+1")
        self.r.revoke(self.p, first["knowledge_id"])
        with self.assertRaises(ContractError):
            self.r.restore(self.p, first["knowledge_id"], 1)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_published_tool_success_exposes_later_conflict(self):
        self.hook("PreToolUse", tool_use_id="success", tool_name="Bash")
        self.hook("PostToolUse", tool_use_id="success", tool_name="Bash", tool_response={"exit_code": 0})
        event = self.r.db.execute("SELECT id FROM events WHERE type='PostToolUse'").fetchone()[0]
        candidate = self.candidate(level="tool_success", refs=[event])
        first = self.r.publish(self.p, candidate)
        self.assertTrue(self.r.read(self.p, first["knowledge_id"])["current_tool_success"])
        self.hook("PostToolUse", tool_use_id="success", tool_name="Bash", tool_response={"exit_code": 1})
        item = self.r.read(self.p, first["knowledge_id"])
        self.assertEqual(item["source_health"], "conflicted")
        self.assertFalse(item["current_tool_success"])
        self.assertEqual(item["validation_level"], "tool_success")
        self.assertEqual(self.r.publish(self.p, candidate), first)
        self.r.revoke(self.p, first["knowledge_id"])
        with self.assertRaises(ContractError):
            self.r.restore(self.p, first["knowledge_id"], 1)

    def test_source_missing_and_denied_are_explicit(self):
        first = self.r.publish(self.p, self.candidate())
        evidence = self.r.read(self.p, first["knowledge_id"])["evidence"][0]["event_id"]
        self.r.db.execute("DELETE FROM problem_events WHERE event=?", (evidence,))
        item = self.r.read(self.p, first["knowledge_id"])
        self.assertEqual(item["source_health"], "missing")
        self.assertEqual(item["sources"][0]["health"], "missing")

    def test_source_denial_invalidates_current_success_without_rewriting_history(self):
        self.hook("PreToolUse", tool_use_id="success", tool_name="Bash")
        self.hook("PostToolUse", tool_use_id="success", tool_name="Bash", tool_response={"exit_code": 0})
        event = self.r.db.execute("SELECT id FROM events WHERE type='PostToolUse'").fetchone()[0]
        first = self.r.publish(self.p, self.candidate(level="tool_success", refs=[event]))
        self.r.db.execute("UPDATE operations SET decision='denied'")
        item = self.r.read(self.p, first["knowledge_id"])
        self.assertEqual(item["source_health"], "denied")
        self.assertFalse(item["current_tool_success"])
        self.assertEqual(next(ref for ref in item["evidence"] if ref["event_id"] == event)["outcome"], "succeeded")

    def test_read_uses_one_snapshot_for_body_and_source_health(self):
        self.hook("PreToolUse", tool_use_id="success", tool_name="Bash")
        self.hook("PostToolUse", tool_use_id="success", tool_name="Bash", tool_response={"exit_code": 0})
        event = self.r.db.execute("SELECT id FROM events WHERE type='PostToolUse'").fetchone()[0]
        first = self.r.publish(self.p, self.candidate(level="tool_success", refs=[event]))
        second = Runtime(self.home)
        original = self.r._public
        def concurrent_conflict(project, row):
            second.db.execute("UPDATE operations SET conflict=1")
            return original(project, row)
        try:
            with patch.object(self.r, "_public", side_effect=concurrent_conflict):
                result = self.r.read(self.p, first["knowledge_id"])
            self.assertTrue(result["current_tool_success"])
            self.assertEqual(result["source_health"], "valid")
            self.assertEqual(self.r.read(self.p, first["knowledge_id"])["source_health"], "conflicted")
        finally:
            second.close()

    def legacy_state(self, ambiguous=False, unmatched=False, old_version=4, dedup=False):
        candidate = self.candidate(content="api_key=SYNTHETIC_LEGACY_SECRET compiler guidance")
        first = self.r.publish(self.p, candidate)
        second_candidate = None
        if dedup:
            second_candidate = self.candidate("来源B", "api_key=SYNTHETIC_LEGACY_SECRET compiler guidance")
            self.r.publish(self.p, second_candidate)
        self.r.db.execute("DROP TABLE knowledge_sources")
        self.r.db.execute("DROP TABLE publication_receipts")
        self.r.db.execute("DROP TABLE candidate_identity")
        self.r.db.execute("UPDATE candidates SET id='legacy-'||id")
        candidate = "legacy-" + candidate
        if second_candidate is not None:
            second_candidate = "legacy-" + second_candidate
        for table in ("knowledge", "candidates"):
            for row in self.r.db.execute(f"SELECT rowid,body FROM {table}").fetchall():
                body = json.loads(row["body"])
                body.pop("knowledge_kind", None)
                body.pop("metadata", None)
                body["content"] = "api_key=SYNTHETIC_LEGACY_SECRET compiler guidance"
                if unmatched and table == "knowledge":
                    body["content"] = "different conclusion with the same title"
                self.r.db.execute(f"UPDATE {table} SET body=? WHERE rowid=?", (encode(body), row["rowid"]))
        if ambiguous:
            self.r.db.execute("INSERT INTO knowledge SELECT project,'another-id',revision,body,created FROM knowledge")
        self.r.db.execute("INSERT INTO knowledge_fts VALUES(?,?,?,?)", (self.p, first["knowledge_id"], 1, "synthetic_legacy_secret"))
        self.r.db.execute("INSERT OR REPLACE INTO cache VALUES('legacy-cache','[]')")
        self.r.db.execute(f"PRAGMA user_version={old_version}")
        self.r.close()
        self.r = Runtime(self.home)
        return (second_candidate or candidate), first

    def test_legacy_migration_rebuilds_clean_index_and_recovers_unique_receipt(self):
        candidate, first = self.legacy_state()
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertEqual(self.r.publish(self.p, candidate), first)
        self.assertEqual(len(self.r.read(self.p, first["knowledge_id"])["sources"]), 1)
        self.assertEqual(self.r.search(self.p, "synthetic_legacy_secret")["items"], [])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM cache WHERE key='legacy-cache'").fetchone()[0], 0)
        self.assertEqual(self.r.doctor()["status"], "ok")

    def test_legacy_ambiguous_publication_gets_protective_tombstone(self):
        candidate, first = self.legacy_state(ambiguous=True)
        before = self.generation()
        with self.assertRaisesRegex(ContractError, "unresolved"):
            self.r.publish(self.p, candidate)
        self.assertEqual(self.generation(), before)
        self.assertEqual(self.r.doctor()["checks"]["receipts"], 1)
        self.assertEqual(self.r.read(self.p, first["knowledge_id"])["sources"], [])

    def test_legacy_no_exact_body_does_not_guess_by_title(self):
        candidate, _ = self.legacy_state(unmatched=True)
        with self.assertRaisesRegex(ContractError, "unresolved"):
            self.r.publish(self.p, candidate)

    def test_legacy_dedup_recovers_second_source_from_unique_full_fingerprint(self):
        candidate, first = self.legacy_state(dedup=True)
        result = self.r.publish(self.p, candidate)
        self.assertEqual(result["knowledge_id"], first["knowledge_id"])
        self.assertTrue(result["deduplicated"])
        self.assertEqual(len(self.r.read(self.p, first["knowledge_id"])["sources"]), 2)

    def test_legacy_same_body_resubmission_preserves_candidate_and_revocation(self):
        candidate, first = self.legacy_state()
        self.r.revoke(self.p, first["knowledge_id"])
        body = json.loads(self.r.db.execute("SELECT body FROM candidates WHERE id=?", (candidate,)).fetchone()[0])
        submitted = self.r.submit_candidate(self.p, **body)
        self.assertEqual(submitted, {"candidate_id": candidate, "status": "published"})
        self.assertEqual(self.r.publish(self.p, submitted["candidate_id"]), first)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_same_case_evidence_order_and_duplicates_preserve_candidate_identity(self):
        self.hook("PreToolUse", tool_use_id="evidence", tool_name="Bash")
        self.hook("PostToolUse", tool_use_id="evidence", tool_name="Bash", tool_response={"exit_code": 0})
        refs = [row[0] for row in self.r.db.execute("SELECT id FROM events WHERE operation IS NOT NULL ORDER BY created")]
        self.assertEqual(len(refs), 2)
        candidate = self.candidate(refs=refs)
        body = json.loads(self.r.db.execute("SELECT body FROM candidates WHERE id=?", (candidate,)).fetchone()[0])
        self.assertGreaterEqual(len(body["evidence_refs"]), 2)
        body["evidence_refs"] = list(reversed(body["evidence_refs"])) + body["evidence_refs"][:1]
        self.assertEqual(self.r.submit_candidate(self.p, **body)["candidate_id"], candidate)

    def test_migration_identity_prefers_protected_published_history(self):
        candidate = self.candidate()
        first = self.r.publish(self.p, candidate)
        self.r.db.execute("INSERT INTO candidates SELECT 'earlier-duplicate',project,problem,problem_revision,body,'candidate',0 "
                          "FROM candidates WHERE id=?", (candidate,))
        self.r.db.execute("PRAGMA user_version=4")
        self.r._migrate_knowledge_store()
        body = json.loads(self.r.db.execute("SELECT body FROM candidates WHERE id=?", (candidate,)).fetchone()[0])
        self.r.revoke(self.p, first["knowledge_id"])
        self.assertEqual(self.r.submit_candidate(self.p, **body)["candidate_id"], candidate)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidate_identity").fetchone()[0], 1)

    def test_v1_migration_clears_secret_from_canonical_and_fts(self):
        _, first = self.legacy_state(old_version=1)
        self.assertEqual(self.r.search(self.p, "synthetic_legacy_secret")["items"], [])
        self.assertNotIn("SYNTHETIC_LEGACY_SECRET", self.r.db.execute("SELECT body FROM knowledge").fetchone()[0])
        self.assertEqual(self.r.read(self.p, first["knowledge_id"])["knowledge_kind"], "solution")

    def test_migration_failure_rolls_back_body_receipts_and_version(self):
        self.r.publish(self.p, self.candidate())
        self.r.db.execute("DROP TABLE knowledge_sources")
        self.r.db.execute("DROP TABLE publication_receipts")
        row = self.r.db.execute("SELECT rowid,body FROM knowledge").fetchone()
        body = json.loads(row["body"])
        body["content"] = "password=SYNTHETIC_ROLLBACK_SECRET"
        self.r.db.execute("UPDATE knowledge SET body=? WHERE rowid=?", (encode(body), row["rowid"]))
        self.r.db.execute("PRAGMA user_version=4")
        with patch.object(self.r, "_rebuild_knowledge_index", side_effect=sqlite3.OperationalError("index failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.r._migrate_knowledge_store()
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertIn("SYNTHETIC_ROLLBACK_SECRET", self.r.db.execute("SELECT body FROM knowledge").fetchone()[0])
        self.assertIsNone(self.r.db.execute("SELECT 1 FROM sqlite_master WHERE name='publication_receipts'").fetchone())
        self.assertFalse(self.r.db.in_transaction)

    def test_backup_includes_committed_wal_receipts_and_rejects_overwrite(self):
        first = self.r.publish(self.p, self.candidate())
        destination = Path(self.temp.name) / "backup.sqlite3"
        result = self.r.backup(destination)
        self.assertEqual(result["status"], "backed_up")
        copy = sqlite3.connect(destination)
        try:
            self.assertEqual(copy.execute("SELECT id FROM active").fetchone()[0], first["knowledge_id"])
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM publication_receipts").fetchone()[0], 1)
            self.assertEqual(copy.execute("PRAGMA quick_check").fetchone()[0], "ok")
        finally:
            copy.close()
        with self.assertRaises(ContractError):
            self.r.backup(destination)
        with self.assertRaises(ContractError):
            self.r.backup(self.home / "runtime.sqlite3")
        with self.assertRaises(ContractError):
            self.r.backup(self.home / "runtime.sqlite3-journal")
        with self.r.transaction():
            with self.assertRaises(ContractError):
                self.r.backup(Path(self.temp.name) / "transaction.sqlite3")

    def test_failed_backup_removes_only_own_new_file(self):
        destination = Path(self.temp.name) / "backup.sqlite3"
        with patch("agent_learning.knowledge_store.sqlite3.connect", side_effect=sqlite3.OperationalError("synthetic failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.r.backup(destination)
        self.assertFalse(destination.exists())
        destination.write_text("preserved", encoding="utf-8")
        with self.assertRaises(ContractError):
            self.r.backup(destination)
        self.assertEqual(destination.read_text(encoding="utf-8"), "preserved")

    def test_doctor_reports_broken_derived_records_without_mutation(self):
        first = self.r.publish(self.p, self.candidate())
        self.assertEqual(self.r.doctor()["status"], "ok")
        before = self.generation()
        self.r.db.execute("UPDATE knowledge_fingerprints SET fingerprint='invalid'")
        self.r.db.execute("DELETE FROM knowledge_fts")
        result = self.r.doctor()
        self.assertEqual(result["status"], "issues")
        self.assertEqual(result["checks"]["fingerprints"], 1)
        self.assertGreater(result["checks"]["index"], 0)
        self.assertEqual(self.generation(), before)
        self.assertEqual(self.r.read(self.p, first["knowledge_id"])["content"], "original compiler guidance")

    def test_doctor_reports_stale_text_inside_an_otherwise_valid_index_row(self):
        self.r.publish(self.p, self.candidate())
        self.r.db.execute("UPDATE knowledge_fts SET text='synthetic stale token'")
        self.assertGreater(self.r.doctor()["checks"]["index"], 0)

    def test_doctor_reports_candidate_identity_loss(self):
        self.r.publish(self.p, self.candidate())
        self.r.db.execute("DELETE FROM candidate_identity")
        self.assertEqual(self.r.doctor()["checks"]["candidate_identity"], 1)

    def test_foreign_keys_prevent_cross_project_source_assignment(self):
        first = self.r.publish(self.p, self.candidate())
        source = self.r.db.execute("SELECT * FROM knowledge_sources").fetchone()
        with self.assertRaises(sqlite3.IntegrityError):
            self.r.db.execute("INSERT INTO knowledge_sources VALUES(?,?,?,?,?,?,?,?)",
                ("other-project", first["knowledge_id"], 1, source["candidate_id"], source["problem_id"],
                 source["problem_revision"], source["evidence_refs"], source["created"]))


if __name__ == "__main__":
    unittest.main()
