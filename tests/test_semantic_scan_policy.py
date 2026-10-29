"""Semantic selection uses synthetic in-memory SQLite only; no provider or host."""
from __future__ import annotations

import json
import math
import sqlite3
import struct
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
                "query_key": key or semantic._hash(query), "diagnostics": {}}


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
        self.assertEqual(diagnostics["eligible_vectors"], 22)
        self.assertEqual(diagnostics["scan_policy"], "lexical_recent_history")
        self.assertEqual(diagnostics["scan_lanes"], {"lexical": 1, "recent": 1, "historical": 2})
        self.assertNotIn("E713_IMPORT", json.dumps(diagnostics))

    def test_history_window_is_stable_for_same_query_and_varies_across_queries(self):
        self.populate()
        first = self.runtime.prepared(4, key="0000000000000000")
        repeat = self.runtime.prepared(4, key="0000000000000000")
        later = self.runtime.prepared(4, key="0000000000000008")
        a, b, c = self.search(first), self.search(repeat), self.search(later)
        self.assertEqual(a, b)
        self.assertEqual(first["diagnostics"], repeat["diagnostics"])
        self.assertNotEqual(set(a), set(c))
        self.assertTrue({"item-008", "item-009", "item-010"}.issubset(c))
        self.assertIn("item-019", a)
        self.assertIn("item-019", c)

    def test_history_window_wraps_and_overlapping_recent_ids_do_not_waste_budget(self):
        self.populate(12)
        prepared = self.runtime.prepared(4, key="000000000000000b")
        result = self.search(prepared)
        self.assertEqual(set(result), {"item-011", "item-000", "item-001", "item-002"})
        self.assertEqual(sum(prepared["diagnostics"]["scan_lanes"].values()), 4)

    def test_one_two_and_three_slot_budgets_keep_history_and_never_overrun_body_work(self):
        self.populate(12)
        for limit in [1, 2, 3]:
            with self.subTest(limit=limit):
                prepared = self.runtime.prepared(limit, key="0000000000000005")
                with patch.object(semantic, "_document", wraps=semantic._document) as documents:
                    result = self.search(prepared)
                self.assertEqual(len(result), limit)
                self.assertEqual(documents.call_count, limit)
                self.assertEqual(prepared["diagnostics"]["scanned"], limit)
                self.assertGreaterEqual(prepared["diagnostics"]["scan_lanes"]["historical"], 1)
        self.assertEqual(self.search(self.runtime.prepared(1, key="0000000000000005")).keys(), {"item-005"})

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
        self.assertEqual(prepared["diagnostics"]["eligible_vectors"], 21)

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


if __name__ == "__main__":
    unittest.main()
