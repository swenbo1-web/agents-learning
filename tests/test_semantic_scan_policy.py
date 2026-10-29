"""Semantic selection uses synthetic in-memory SQLite only; no provider or host."""
from __future__ import annotations

import json
import math
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from agent_learning import semantic_retrieval as semantic
from agent_learning.knowledge_index import _index_text


class MemoryRuntime(semantic.SemanticRetrievalMixin):
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.config = {"retrieval": {"semantic": {"enabled": True, "model": "synthetic"}}}
        self.db.executescript("""
            CREATE TABLE knowledge(project TEXT,id TEXT,revision INTEGER,body TEXT,created REAL,
                PRIMARY KEY(project,id,revision));
            CREATE INDEX knowledge_recent ON knowledge(project,created DESC,id,revision);
            CREATE TABLE active(project TEXT,id TEXT,revision INTEGER,PRIMARY KEY(project,id));
            CREATE TABLE semantic_vectors(project TEXT,knowledge_id TEXT,revision INTEGER,identity TEXT,
                content_hash TEXT,dimensions INTEGER,vector BLOB,input_truncated INTEGER,
                PRIMARY KEY(project,knowledge_id,identity));
            CREATE TABLE semantic_index_state(project TEXT,identity TEXT,generation INTEGER,dimensions INTEGER,
                PRIMARY KEY(project,identity));
            CREATE VIRTUAL TABLE knowledge_fts USING fts5(project UNINDEXED,id UNINDEXED,
                revision UNINDEXED,text);
        """)

    def add(self, identifier, *, project="p", created=0, title="unrelated material",
            vector=(1.0, 0.0), environment="A", revision=1, active_revision=None, identity=None):
        config = self._semantic_config()
        body = {"title": title, "content": "synthetic retained knowledge", "category": "operation",
                "knowledge_kind": "solution", "metadata": {"environment": environment}}
        encoded = json.dumps(body)
        vector_identity = identity or semantic._vector_identity(config)
        self.db.execute("INSERT INTO knowledge VALUES(?,?,?,?,?)", (project, identifier, revision, encoded, created))
        self.db.execute("INSERT OR REPLACE INTO active VALUES(?,?,?)",
                        (project, identifier, active_revision if active_revision is not None else revision))
        self.db.execute("INSERT INTO semantic_vectors VALUES(?,?,?,?,?,?,?,?)",
                        (project, identifier, revision, vector_identity, semantic._document(encoded, 8000)[1],
                         len(vector), struct.pack("<" + "f" * len(vector), *vector), 0))
        self.db.execute("INSERT INTO knowledge_fts VALUES(?,?,?,?)",
                        (project, identifier, revision, _index_text(body)))
        self.db.execute("INSERT OR IGNORE INTO semantic_index_state VALUES(?,?,1,?)",
                        (project, vector_identity, len(vector)))

    def prepared(self, limit, query="query with no lexical overlap", *, key=None, vector=(1.0, 0.0)):
        return {"config": {**self._semantic_config(), "scan_limit": limit},
                "vector": list(vector), "query_text": query,
                "query_key": semantic._hash(query), "scan_key": key or semantic._hash(query)[:32],
                "diagnostics": {}}


class SemanticScanPolicyTests(unittest.TestCase):
    def setUp(self):
        self.runtime = MemoryRuntime()
        self.addCleanup(self.runtime.db.close)

    def populate(self, count=20):
        for index in range(count):
            self.runtime.add(f"item-{index:03}", created=index)

    def search(self, prepared, *, project="p", filters=None):
        return self.runtime._semantic_candidates(project, prepared, filters or {})

    def test_small_collection_retains_complete_scan_and_score_id_ties(self):
        for identifier in ["z", "m", "a"]:
            self.runtime.add(identifier)
        prepared = self.runtime.prepared(4)
        result = self.search(prepared)
        self.assertEqual(list(result), ["a", "m", "z"])
        self.assertEqual([item["score"] for item in result.values()], [1, 1, 1])
        self.assertEqual(prepared["diagnostics"], {"scan_policy": "complete", "eligible_vectors": 3,
            "scan_truncated": False, "scan_lanes": {"complete": 3}, "scanned": 3, "invalid_vectors": 0})

    def test_empty_collection_and_missing_query_vector(self):
        prepared = self.runtime.prepared(1)
        self.assertEqual(self.search(prepared), {})
        self.assertEqual(prepared["diagnostics"]["scanned"], 0)
        prepared["vector"] = None
        with patch.object(self.runtime, "_semantic_scan_rows", side_effect=AssertionError("must not query")):
            self.assertEqual(self.search(prepared), {})

    def test_large_collection_keeps_recent_and_old_exact_lexical_seed(self):
        self.populate()
        self.runtime.add("legacy-z", created=-10, title="E713_IMPORT durable old repair")
        self.runtime.add("latest-z", created=100)
        prepared = self.runtime.prepared(4, "E713_IMPORT")
        result = self.search(prepared)
        self.assertIn("legacy-z", result)
        self.assertIn("latest-z", result)
        diagnostics = prepared["diagnostics"]
        self.assertTrue(diagnostics["scan_truncated"])
        self.assertIsNone(diagnostics["eligible_vectors"])
        self.assertEqual(diagnostics["eligible_vectors_lower_bound"], 5)
        self.assertEqual(diagnostics["scan_policy"], "stable_interleaved_keyset")
        self.assertEqual(diagnostics["scan_lanes"], {"lexical": 1, "recent": 1, "historical": 2})
        self.assertNotIn("E713_IMPORT", json.dumps(diagnostics))

    def test_history_window_is_stable_for_same_query_and_varies_across_queries(self):
        self.populate()
        first = self.runtime.prepared(4, key="item-000")
        repeat = self.runtime.prepared(4, key="item-000")
        later = self.runtime.prepared(4, key="item-008")
        a, b, c = self.search(first), self.search(repeat), self.search(later)
        self.assertEqual(a, b)
        self.assertEqual(first["diagnostics"], repeat["diagnostics"])
        self.assertNotEqual(set(a), set(c))
        self.assertTrue({"item-008", "item-009", "item-010"}.issubset(c))
        self.assertIn("item-019", a)
        self.assertIn("item-019", c)

    def test_history_window_wraps_and_overlapping_recent_ids_do_not_waste_budget(self):
        self.populate(12)
        prepared = self.runtime.prepared(4, key="item-011")
        result = self.search(prepared)
        self.assertEqual(set(result), {"item-011", "item-010", "item-000", "item-001"})
        self.assertEqual(sum(prepared["diagnostics"]["scan_lanes"].values()), 4)

    def test_one_two_and_three_slot_budgets_keep_history_and_never_overrun_body_work(self):
        self.populate(12)
        for limit in [1, 2, 3]:
            with self.subTest(limit=limit):
                prepared = self.runtime.prepared(limit, key="item-005")
                with patch.object(semantic, "_document", wraps=semantic._document) as documents:
                    result = self.search(prepared)
                self.assertEqual(len(result), limit)
                self.assertEqual(documents.call_count, limit)
                self.assertEqual(prepared["diagnostics"]["scanned"], limit)
                self.assertGreaterEqual(prepared["diagnostics"]["scan_lanes"]["historical"], 1)
        self.assertEqual(self.search(self.runtime.prepared(1, key="item-005")).keys(), {"item-005"})

    def test_all_lanes_apply_project_filter_active_revision_and_vector_identity(self):
        self.populate()
        self.runtime.add("foreign", project="q", created=100, title="BOUNDARY_SEED")
        self.runtime.add("wrong-env", environment="B", created=100, title="BOUNDARY_SEED")
        self.runtime.add("old-revision", revision=1, active_revision=2, created=100, title="BOUNDARY_SEED")
        self.runtime.add("old-model", identity="other-embedding-identity", created=100, title="BOUNDARY_SEED")
        self.runtime.add("eligible-seed", created=-100, title="BOUNDARY_SEED")
        prepared = self.runtime.prepared(4, "BOUNDARY_SEED")
        result = self.search(prepared, filters={"environment": "A"})
        self.assertEqual(len(result), 4)
        self.assertIn("eligible-seed", result)
        self.assertTrue(set(result).isdisjoint({"foreign", "wrong-env", "old-revision", "old-model"}))
        self.assertEqual(prepared["diagnostics"]["eligible_vectors_lower_bound"], 5)

    def test_corrupt_body_and_vector_still_count_invalid_without_poisoning_other_rows(self):
        self.populate()
        self.runtime.add("invalid-vector", created=-100, title="CORRUPT_SEED", vector=(math.nan, 0))
        self.runtime.add("invalid-body", created=100)
        self.runtime.db.execute("UPDATE knowledge SET body='{' WHERE id='invalid-body'")
        prepared = self.runtime.prepared(4, "CORRUPT_SEED")
        result = self.search(prepared)
        self.assertEqual(len(result), 2)
        self.assertEqual(prepared["diagnostics"]["invalid_vectors"], 2)
        self.assertEqual(prepared["diagnostics"]["scanned"], 4)

    def test_body_hash_is_rechecked_even_when_same_revision_changes(self):
        self.populate()
        self.runtime.add("changed-recent", created=100)
        self.runtime.db.execute("UPDATE knowledge SET body=? WHERE id='changed-recent'",
                                (json.dumps({"content": "unversioned corruption"}),))
        prepared = self.runtime.prepared(4)
        result = self.search(prepared)
        self.assertNotIn("changed-recent", result)
        self.assertEqual(prepared["diagnostics"]["invalid_vectors"], 1)

    def test_chunked_selection_respects_scan_limit_and_candidate_limit(self):
        self.populate(620)
        prepared = self.runtime.prepared(300)
        with patch.object(semantic, "_document", wraps=semantic._document) as documents:
            result = self.search(prepared)
        self.assertEqual(documents.call_count, 300)
        self.assertEqual(prepared["diagnostics"]["scanned"], 300)
        self.assertEqual(sum(prepared["diagnostics"]["scan_lanes"].values()), 300)
        self.assertEqual(len(result), prepared["config"]["candidate_limit"])

    def test_selection_version_invalidates_signature_without_invalidating_document_vectors(self):
        self.runtime.add("one")
        config = self.runtime._semantic_config()
        identity = semantic._vector_identity(config)
        signature = self.runtime.semantic_signature("p")
        with patch.object(semantic, "SEMANTIC_SCAN_POLICY_VERSION", semantic.SEMANTIC_SCAN_POLICY_VERSION + 1):
            self.assertNotEqual(self.runtime.semantic_signature("p"), signature)
            self.assertEqual(semantic._vector_identity(config), identity)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM semantic_vectors").fetchone()[0], 1)

    def test_growing_budget_preserves_all_previously_selected_candidates(self):
        self.populate(84)
        for index in range(12):
            self.runtime.add(f"legacy-{index:03}", title="E_RETAIN legacy repair", created=-100-index)
        previous = set()
        for limit in range(1, 101):
            prepared = self.runtime.prepared(limit, "E_RETAIN", key="item-010")
            rows = self.runtime._semantic_scan_rows("p", prepared, {})
            selected = {row["id"] for row in rows}
            self.assertTrue(previous <= selected, (limit, previous-selected))
            self.assertEqual(len(selected), min(limit, 96))
            previous = selected

    def test_seven_to_eight_budget_keeps_pure_semantic_result(self):
        self.populate(40)
        for index in range(4):
            self.runtime.add(f"legacy-{index}", title="E_RETAIN legacy repair", created=-100)
        small = self.runtime.prepared(7, "E_RETAIN", key="item-010")
        large = self.runtime.prepared(8, "E_RETAIN", key="item-010")
        rows = self.runtime._semantic_scan_rows("p", small, {})
        target = next(row["id"] for row in rows if row["id"].startswith("item-01"))
        self.runtime.db.execute("UPDATE semantic_vectors SET vector=?", (struct.pack("<ff", 0, 1),))
        self.runtime.db.execute("UPDATE semantic_vectors SET vector=? WHERE knowledge_id=?",
                                (struct.pack("<ff", 1, 0), target))
        self.assertIn(target, self.search(small))
        self.assertIn(target, self.search(large))

    def test_unrelated_insertion_and_generation_do_not_rotate_history_cursor(self):
        self.populate(30)
        original = self.runtime.prepared(8, key="item-010")
        before = self.runtime._semantic_scan_rows("p", original, {})
        self.runtime.add("000-unrelated-older", created=-100)
        self.runtime.db.execute("UPDATE semantic_index_state SET generation=generation+1")
        again = self.runtime.prepared(8, key="item-010")
        again["query_key"] = "different-generation-cache-key"
        after = self.runtime._semantic_scan_rows("p", again, {})
        self.assertEqual({row["id"] for row in before}, {row["id"] for row in after})

    def test_metadata_selection_uses_ranges_and_recent_index_without_count_or_offset(self):
        self.populate(620)
        statements = []
        self.runtime.db.set_trace_callback(statements.append)
        prepared = self.runtime.prepared(20, query="", key="item-500")
        self.runtime._semantic_scan_rows("p", prepared, {})
        self.runtime.db.set_trace_callback(None)
        self.assertFalse(any("COUNT(" in sql or "OFFSET" in sql for sql in statements))
        recent = next(sql for sql in statements if "ORDER BY k.created DESC" in sql)
        plan = [row[3] for row in self.runtime.db.execute("EXPLAIN QUERY PLAN " + recent)]
        self.assertTrue(any("knowledge_recent" in step for step in plan), plan)
        self.assertFalse(any("TEMP B-TREE" in step for step in plan), plan)
        ranges = [sql for sql in statements if "k.id>=" in sql or "k.id<" in sql]
        self.assertTrue(ranges)
        for sql in ranges:
            plan = [row[3] for row in self.runtime.db.execute("EXPLAIN QUERY PLAN " + sql)]
            self.assertTrue(any("id>?" in step or "id<?" in step for step in plan), plan)

    def test_old_read_only_database_without_recent_index_remains_compatible(self):
        self.populate()
        self.runtime.db.execute("DROP INDEX knowledge_recent")
        self.runtime.db.execute("PRAGMA query_only=ON")
        prepared = self.runtime.prepared(4)
        self.assertEqual(len(self.runtime._semantic_scan_rows("p", prepared, {})), 4)

    def test_dot_product_retains_previous_accumulation_result(self):
        vectors = [(0.8, 0.6), (0.6, 0.8), (0.0, 1.0)]
        for index, values in enumerate(vectors):
            self.runtime.add(str(index), vector=values)
        vector = (0.6, 0.8)
        prepared = self.runtime.prepared(4, vector=vector)
        result = self.search(prepared)
        for index, values in enumerate(vectors):
            stored = struct.unpack("<ff", struct.pack("<ff", *values))
            expected = max(-1.0, min(1.0, sum(left * right for left, right in zip(vector, stored))))
            self.assertEqual(result[str(index)]["score"], expected)

    def test_stored_vector_keeps_original_float32_boundary_and_invalid_contract(self):
        bits = struct.unpack("<I", struct.pack("<f", 0.01))[0]
        vectors = [[1.0, struct.unpack("<f", struct.pack("<I", bits + delta))[0]] for delta in range(-2, 3)]
        vectors += [[math.nan, 0], [math.inf, 0], [-math.inf, 0], [0, 0], [2, 0], [1, 0]]
        for values in vectors:
            blob = struct.pack("<ff", *values)
            stored = struct.unpack("<ff", blob)
            original_accepts = all(math.isfinite(value) for value in stored) and not (
                abs(sum(value * value for value in stored) - 1) > 0.0001)
            with self.subTest(values=values):
                if original_accepts:
                    self.assertEqual(semantic._stored_vector(blob, 2, 2), stored)
                else:
                    with self.assertRaisesRegex(ValueError, "invalid_stored_vector"):
                        semantic._stored_vector(blob, 2, 2)


class SemanticScanRuntimeTests(unittest.TestCase):
    def setUp(self):
        from agent_learning.runtime import Runtime
        from tests.test_hybrid_retrieval import HybridRetrievalTests
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Every embedding in this suite is a controlled in-process fixture,
        # including comparisons made by review_packet while publishing.
        provider = patch.object(semantic, "embed_texts", side_effect=lambda config, texts, dimensions:
                                [[0.0, 1.0] for _ in texts])
        provider.start()
        self.addCleanup(provider.stop)
        self.r = Runtime(Path(self.temp.name) / "home", config={"retrieval": {
            "semantic": {"enabled": True, "model": "synthetic", "scan_limit": 7}}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(Path(self.temp.name) / "project")
        self.r.task_update(self.p, "chat", "read synthetic fixtures", {"mode": "analysis"})
        self.fixture = HybridRetrievalTests()
        self.fixture.r, self.fixture.p, self.fixture.serial = self.r, self.p, 0
        for index in range(40):
            self.fixture.publish(title="opaque record " + str(index), content="controlled synthetic text")
        for index in range(4):
            self.fixture.publish(title="capacitor voltage " + str(index), content="controlled synthetic text")
        self.r.semantic_refresh(self.p, limit=100)
        self.r.db.execute("UPDATE knowledge SET created=-1 WHERE json_extract(body,'$.title') LIKE 'capacitor%'")

    def test_public_automatic_search_preserves_pure_semantic_target_when_budget_grows(self):
        query = "capacitor voltage"
        with patch.object(semantic, "embed_texts", return_value=[[1.0, 0.0]]):
            prepared = self.r._semantic_prepare(self.p, query)
        rows = self.r._semantic_scan_rows(self.p, prepared, {})
        target = next(row["id"] for row in rows if json.loads(row["body"])["title"].startswith("opaque"))
        self.r.db.execute("UPDATE semantic_vectors SET vector=? WHERE knowledge_id=?",
                          (struct.pack("<ff", 1, 0), target))
        for limit in (7, 8, 9, 16, 32, 44, 45):
            self.r.config["retrieval"]["semantic"]["scan_limit"] = limit
            result = self.r.search(self.p, query, limit=10, _automatic=True)
            match = next((item["match"] for item in result["items"] if item["knowledge_id"] == target), None)
            self.assertIsNotNone(match, limit)
            self.assertEqual(match["score"], 0)
            self.assertEqual(match["semantic_score"], 1)

    def test_prepare_cursor_survives_index_generation_change_without_stale_embedding_key(self):
        before = self.r._semantic_prepare(self.p, "battery voltage")
        self.fixture.publish(title="unrelated older material", content="synthetic unrelated statement")
        self.r.semantic_refresh(self.p, limit=100)
        after = self.r._semantic_prepare(self.p, "battery voltage")
        self.assertEqual(before["scan_key"], after["scan_key"])
        self.assertNotEqual(before["query_key"], after["query_key"])
        self.assertNotEqual(before["signature"], after["signature"])


if __name__ == "__main__":
    unittest.main()
