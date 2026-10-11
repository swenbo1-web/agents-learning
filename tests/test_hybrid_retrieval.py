"""Real local HTTP contract tests; all models, vectors and databases are isolated fixtures."""
import json
import math
import os
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_learning.runtime import ContractError, Runtime
from agent_learning.semantic_retrieval import (SEMANTIC_DEFAULTS, embed_texts,
                                               validate_semantic_config, _normalize)


class LocalEmbeddings:
    def __init__(self):
        self.calls = []
        self.response = None
        self.status = 200
        self.redirect = None
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.calls.append((self.path, payload))
                self.send_response(fixture.status)
                if fixture.redirect:
                    self.send_header("Location", fixture.redirect)
                self.end_headers()
                if fixture.response is not None:
                    body = fixture.response
                else:
                    body = {"embeddings": [[1.0, 0.0] if any(
                        marker in text for marker in ("retained", "mémoire", "unrelated phrase", "E999"))
                        else [0.0, 1.0] for text in payload["input"]]}
                self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class HybridRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = LocalEmbeddings()
        self.config = {**SEMANTIC_DEFAULTS, "enabled": True, "model": "fixture", "url": self.provider.url}
        self.r = Runtime(Path(self.temp.name) / "state", config={"retrieval": {"semantic": self.config}})
        self.p = self.r.project(Path(self.temp.name) / "project")
        self.r.task_update(self.p, "chat", "review memory", {"mode": "analysis"})
        self.serial = 0

    def tearDown(self):
        self.r.close()
        self.provider.close()
        self.temp.cleanup()

    def candidate(self, title="release retained buffers", *, content="release retained memory",
                  environment="A", project=None, conversation="chat"):
        self.serial += 1
        project = project or self.p
        problem = self.r.feedback(project, conversation, "case " + str(self.serial), category="operation")
        packet = self.r.review_packet(project, problem["id"])
        return self.r.submit_candidate(project, problem_id=problem["id"], problem_revision=problem["revision"],
            title=title, content=content, category="operation", applicability="same environment",
            limitations="reported only", validation_level="reported", metadata={"environment": environment},
            evidence_refs=[event["id"] for event in packet["events"]])["candidate_id"]

    def publish(self, title="release retained buffers", **kwargs):
        project = kwargs.get("project") or self.p
        return self.r.publish(project, self.candidate(title, **kwargs))["knowledge_id"]

    def test_disabled_and_not_built_never_call_provider(self):
        self.publish()
        self.assertEqual(self.r.search(self.p, "mémoire saturée")["retrieval"]["semantic_status"], "not_built")
        self.r.config["retrieval"]["semantic"]["enabled"] = False
        result = self.r.search(self.p, "release")
        self.assertNotIn("retrieval", result)
        self.assertEqual(self.r.semantic_signature(self.p), "disabled")
        self.assertEqual(self.r.semantic_refresh(self.p)["status"], "disabled")
        self.assertEqual(self.provider.calls, [])

    def test_zero_lexical_overlap_uses_real_http_and_query_cache(self):
        expected = self.publish()
        built = self.r.semantic_refresh(self.p)
        self.assertEqual(built["processed"], 1)
        result = self.r.search(self.p, "mémoire saturée")
        self.assertEqual([item["knowledge_id"] for item in result["items"]], [expected])
        match = result["items"][0]["match"]
        self.assertEqual(match["score"], 0)
        self.assertEqual(match["semantic_score"], 1)
        self.assertTrue(match["auto_eligible"])
        self.assertEqual(result["retrieval"]["semantic_status"], "ready")
        again = self.r.search(self.p, "mémoire saturée")
        self.assertEqual(again["items"], result["items"])
        self.assertTrue(again["retrieval"]["query_cache_hit"])
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(self.provider.calls[0][0], "/api/embed")
        self.assertIs(self.provider.calls[0][1]["truncate"], False)

    def test_incremental_refresh_no_reembedding_and_signature_changes(self):
        self.publish()
        before = self.r.semantic_signature(self.p)
        self.r.semantic_refresh(self.p)
        self.assertNotEqual(before, self.r.semantic_signature(self.p))
        calls = len(self.provider.calls)
        self.assertEqual(self.r.semantic_refresh(self.p)["processed"], 0)
        self.assertEqual(len(self.provider.calls), calls)
        self.publish("different retained record")
        refreshed = self.r.semantic_refresh(self.p, limit=1)
        self.assertEqual(refreshed["processed"], 1)
        self.assertEqual(refreshed["pending"], 0)

    def test_filters_and_project_isolation_apply_to_semantic_candidates(self):
        expected = self.publish(environment="A")
        self.publish("other retained entry", environment="B")
        other = self.r.project(Path(self.temp.name) / "foreign")
        self.r.task_update(other, "foreign-chat", "foreign task", {"mode": "analysis"})
        self.publish("foreign retained entry", project=other, conversation="foreign-chat")
        self.r.semantic_refresh(self.p)
        self.r.semantic_refresh(other)
        result = self.r.search(self.p, "mémoire saturée", filters={"environment": "A"})
        self.assertEqual([item["knowledge_id"] for item in result["items"]], [expected])
        self.assertEqual(self.r.search(self.p, "mémoire saturée", filters={"category": "professional"})["items"], [])
        self.assertEqual(self.r.search(self.p, "mémoire saturée", filters={"knowledge_kind": "pattern"})["items"], [])

    def test_revoked_and_new_revision_do_not_use_stale_vectors(self):
        expected = self.publish()
        self.r.semantic_refresh(self.p)
        self.assertEqual(len(self.r.search(self.p, "mémoire saturée")["items"]), 1)
        updated = self.candidate("spreadsheet fonts", content="format cell widths")
        self.r.publish(self.p, updated, knowledge_id=expected, expected_revision=1)
        self.assertEqual(self.r.search(self.p, "mémoire saturée")["items"], [])
        self.assertEqual(self.r.semantic_status(self.p)["pending"], 1)
        self.r.semantic_refresh(self.p)
        self.assertEqual(self.r.search(self.p, "mémoire saturée")["items"], [])
        self.r.revoke(self.p, expected, expected_revision=2)
        self.assertEqual(self.r.search(self.p, "spreadsheet fonts")["items"], [])

    def test_automatic_semantic_checks_source_health_and_exact_identifiers(self):
        expected = self.publish()
        self.r.semantic_refresh(self.p)
        self.assertEqual(len(self.r.search(self.p, "mémoire saturée", _automatic=True)["items"]), 1)
        self.assertEqual(self.r.search(self.p, "E999 unrelated phrase", _automatic=True)["items"], [])
        original = self.r._public
        def conflicted(project, row):
            return {**original(project, row), "source_health": "conflicted"}
        self.r.db.execute("DELETE FROM cache")
        with patch.object(self.r, "_public", side_effect=conflicted):
            manual = self.r.search(self.p, "mémoire saturée")
            self.assertEqual(manual["items"][0]["knowledge_id"], expected)
            self.assertFalse(manual["items"][0]["match"]["auto_eligible"])
            self.assertEqual(self.r.search(self.p, "mémoire saturée", _automatic=True)["items"], [])

    def test_high_lexical_coverage_cannot_bypass_hybrid_exact_anchors(self):
        self.publish()
        self.r.semantic_refresh(self.p)
        for anchor in ("E999", "C++", "v99", "python.exe", "src/core/task"):
            with self.subTest(anchor=anchor):
                self.assertEqual(self.r.search(self.p, "release retained buffers " + anchor,
                                              _automatic=True)["items"], [])
        self.assertEqual(self.r.search(self.p, "unrelated phrase src/core/task", _automatic=True)["items"], [])
        self.provider.status = 503
        result = self.r.search(self.p, "release retained buffers E998", _automatic=True)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["retrieval"]["semantic_status"], "degraded")

    def test_exact_anchor_check_uses_all_matches_beyond_public_twelve(self):
        words = "retained " + " ".join("description" + chr(97 + index) for index in range(14)) + " E999"
        expected = self.publish(words)
        self.r.semantic_refresh(self.p)
        result = self.r.search(self.p, words, _automatic=True)
        self.assertEqual(result["items"][0]["knowledge_id"], expected)
        self.assertNotIn("e999", result["items"][0]["match"]["matched_terms"])

    def test_rrf_prioritizes_dual_match_over_one_channel(self):
        self.publish("retained buffers", content="retained problem")
        expected = self.publish("mémoire retained buffers", content="mémoire diagnosis")
        self.r.semantic_refresh(self.p)
        result = self.r.search(self.p, "mémoire")
        self.assertEqual(result["items"][0]["knowledge_id"], expected)
        self.assertEqual(result["items"][0]["match"]["fusion_method"], "reciprocal_rank_k60")

    def test_failure_falls_back_without_cache_poisoning_and_recovers(self):
        expected = self.publish()
        self.r.semantic_refresh(self.p)
        self.provider.status = 503
        self.r.db.execute("DELETE FROM cache")
        result = self.r.search(self.p, "retained")
        self.assertEqual(result["items"][0]["knowledge_id"], expected)
        self.assertEqual(result["retrieval"]["semantic_status"], "degraded")
        self.assertTrue(result["retrieval"]["retryable"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 0)
        calls = len(self.provider.calls)
        self.r.search(self.p, "retained")
        self.assertEqual(len(self.provider.calls), calls)
        self.provider.status = 200
        with patch("agent_learning.semantic_retrieval.time.monotonic", return_value=10 ** 15):
            self.assertEqual(self.r.search(self.p, "retained")["retrieval"]["semantic_status"], "ready")

    def test_network_is_deferred_inside_transaction_but_cached_query_is_allowed(self):
        self.publish()
        self.r.semantic_refresh(self.p)
        calls = len(self.provider.calls)
        with self.r.transaction():
            result = self.r.search(self.p, "mémoire saturée")
            self.assertEqual(result["retrieval"]["semantic_status"], "transaction_deferred")
        self.assertEqual(len(self.provider.calls), calls)
        self.r.search(self.p, "mémoire saturée")
        with self.r.transaction():
            self.assertEqual(self.r.search(self.p, "mémoire saturée")["retrieval"]["semantic_status"], "ready")
            with self.assertRaises(ContractError):
                self.r.semantic_refresh(self.p)

    def test_model_change_disables_old_index_and_refresh_builds_new_identity(self):
        self.publish()
        self.r.semantic_refresh(self.p)
        signature = self.r.semantic_signature(self.p)
        self.r.config["retrieval"]["semantic"]["model"] = "new-fixture"
        self.assertNotEqual(signature, self.r.semantic_signature(self.p))
        self.assertEqual(self.r.search(self.p, "mémoire")["retrieval"]["semantic_status"], "not_built")
        self.r.semantic_refresh(self.p)
        self.assertEqual(self.r.semantic_status(self.p)["indexed"], 1)

    def test_dimension_change_requires_rebuild_and_does_not_mix_vectors(self):
        self.publish()
        self.r.semantic_refresh(self.p)
        self.provider.response = {"embeddings": [[1, 0, 0]]}
        result = self.r.search(self.p, "mémoire")
        self.assertEqual(result["retrieval"]["reason"], "invalid_embedding_dimensions")
        self.assertEqual(self.r.semantic_refresh(self.p, rebuild=True)["dimensions"], 3)
        self.r._semantic_query_failures.clear()
        self.assertEqual(len(self.r.search(self.p, "mémoire")["items"]), 1)

    def test_provider_input_and_scan_budgets_are_enforced(self):
        self.r.config["retrieval"]["semantic"].update(document_max_chars=256, query_max_chars=128,
                                                        scan_limit=1, candidate_limit=1, sync_batch_size=1)
        self.publish(content="retained " * 200)
        self.publish("another retained record")
        built = self.r.semantic_refresh(self.p, limit=1)
        self.assertEqual(built["processed"], 1)
        self.assertEqual(built["pending"], 1)
        self.r.semantic_refresh(self.p)
        result = self.r.search(self.p, "mémoire " * 100)
        self.assertTrue(result["retrieval"]["query_truncated"])
        self.assertTrue(result["retrieval"]["scan_truncated"])
        self.assertLessEqual(result["retrieval"]["scanned"], 1)
        self.assertTrue(all(len(call[1]["input"][0]) <= 256 for call in self.provider.calls))
        self.assertLessEqual(len(self.provider.calls[-1][1]["input"][0]), 128)

    def test_authoritative_content_hash_mismatch_skips_vector(self):
        expected = self.publish()
        self.r.semantic_refresh(self.p)
        row = self.r.db.execute("SELECT body FROM knowledge WHERE project=? AND id=?", (self.p, expected)).fetchone()
        body = json.loads(row[0])
        body["content"] = "changed tail " * 1000
        self.r.db.execute("UPDATE knowledge SET body=? WHERE project=? AND id=?", (json.dumps(body), self.p, expected))
        result = self.r.search(self.p, "mémoire saturée")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["retrieval"]["invalid_vectors"], 1)
        self.assertEqual(self.r.semantic_status(self.p)["pending"], 1)

    def test_refresh_rechecks_revision_after_http_response(self):
        expected = self.publish()
        updated = self.candidate("spreadsheet formatting", content="cell formatting")
        original = embed_texts
        def concurrent(config, texts, expected_dimension=None):
            vectors = original(config, texts, expected_dimension)
            self.r.publish(self.p, updated, knowledge_id=expected, expected_revision=1)
            return vectors
        with patch("agent_learning.semantic_retrieval.embed_texts", side_effect=concurrent):
            result = self.r.semantic_refresh(self.p)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["pending"], 1)

    def test_refresh_failure_keeps_existing_vectors(self):
        expected = self.publish()
        self.r.semantic_refresh(self.p)
        self.provider.status = 503
        self.assertEqual(self.r.semantic_refresh(self.p, rebuild=True)["status"], "degraded")
        self.provider.status = 200
        self.assertEqual(self.r.search(self.p, "mémoire")["items"][0]["knowledge_id"], expected)

    def test_rebuild_invalidates_same_model_query_vector_cache(self):
        self.publish()
        self.r.semantic_refresh(self.p)
        self.r.search(self.p, "mémoire")
        self.provider.response = {"embeddings": [[0, 1]]}
        self.r.semantic_refresh(self.p, rebuild=True)
        calls = len(self.provider.calls)
        self.assertEqual(len(self.r.search(self.p, "mémoire")["items"]), 1)
        self.assertEqual(len(self.provider.calls), calls + 1)

    def test_index_change_between_query_prepare_and_snapshot_does_not_poison_cache(self):
        self.publish()
        self.r.semantic_refresh(self.p)
        original = self.r._semantic_prepare
        def changed(project, query):
            result = original(project, query)
            self.provider.response = {"embeddings": [[0, 1]]}
            self.r.semantic_refresh(self.p, rebuild=True)
            return result
        self.r.db.execute("DELETE FROM cache")
        with patch.object(self.r, "_semantic_prepare", side_effect=changed):
            result = self.r.search(self.p, "mémoire")
        self.assertEqual(result["retrieval"]["semantic_status"], "index_changed")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 0)
        self.assertEqual(len(self.r.search(self.p, "mémoire")["items"]), 1)

    def test_derived_index_does_not_change_authoritative_schema(self):
        schema = self.r.db.execute("PRAGMA user_version").fetchone()[0]
        self.publish()
        self.r.semantic_refresh(self.p)
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], schema)

    def test_disabling_a_built_index_reports_disabled(self):
        self.publish()
        self.r.semantic_refresh(self.p)
        self.r.config["retrieval"]["semantic"]["enabled"] = False
        self.assertEqual(self.r.semantic_status(self.p)["status"], "disabled")
        self.assertEqual(self.r.semantic_refresh(self.p)["status"], "disabled")

    def test_missing_projection_table_degrades_and_refresh_recreates_it(self):
        self.publish()
        self.r.semantic_refresh(self.p)
        self.r.db.execute("DROP TABLE semantic_vectors")
        self.assertEqual(self.r.search(self.p, "mémoire")["retrieval"]["semantic_status"], "not_built")
        self.assertEqual(self.r.semantic_refresh(self.p)["processed"], 1)

    def test_invalid_stored_vectors_are_skipped_and_incremental_refresh_repairs_them(self):
        self.publish()
        self.r.semantic_refresh(self.p)
        for vector in ([0, 0], [2, 0], [math.nan, 0]):
            self.r.db.execute("UPDATE semantic_vectors SET vector=?", (struct.pack("<ff", *vector),))
            self.r.db.execute("DELETE FROM cache")
            result = self.r.search(self.p, "mémoire")
            self.assertEqual(result["items"], [])
            self.assertEqual(result["retrieval"]["invalid_vectors"], 1)
            self.assertEqual(self.r.semantic_status(self.p)["pending"], 1)
            self.assertEqual(self.r.semantic_refresh(self.p)["processed"], 1)

    def test_concurrent_refresh_generation_rejects_old_provider_output(self):
        self.publish()
        original = embed_texts
        def changed(config, texts, expected_dimension=None):
            vectors = original(config, texts, expected_dimension)
            self.r.db.execute("UPDATE semantic_index_state SET generation=generation+1,dimensions=3")
            return vectors
        with patch("agent_learning.semantic_retrieval.embed_texts", side_effect=changed):
            result = self.r.semantic_refresh(self.p)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["reason"], "semantic_index_changed")
        self.assertEqual(result["processed"], 0)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM semantic_vectors").fetchone()[0], 0)

    def test_secrets_are_cleaned_before_provider_request(self):
        self.publish(content="retained api_key=private-document-value")
        self.r.semantic_refresh(self.p)
        self.r.search(self.p, "retained api_key=private-query-value")
        inputs = json.dumps(self.provider.calls)
        self.assertNotIn("private-document-value", inputs)
        self.assertNotIn("private-query-value", inputs)

    def test_single_character_remains_ineligible_with_high_semantic_score(self):
        self.publish()
        self.r.semantic_refresh(self.p)
        self.provider.response = {"embeddings": [[1, 0]]}
        self.assertEqual(self.r.search(self.p, "C", _automatic=True)["items"], [])


class EmbeddingContractTests(unittest.TestCase):
    def test_invalid_urls_models_and_budgets_are_rejected(self):
        cases = [{"url": url} for url in ("https://example.com", "http://example.com", "http://127.0.0.1/api",
                 "http://user:secret@127.0.0.1", "http://localhost:bad", "http://127.0.0.1?secret=abc",
                 "http://[invalid", "http://[::bad]")]
        cases += [{"enabled": True}, {"model": "embedding:cloud"}, {"query_max_chars": True},
                  {"timeout_seconds": math.nan}, {"scan_limit": 0}, {"provider": "remote"},
                  {"automatic_min_similarity": 0.1, "min_similarity": 0.5}, {"unknown": 1}]
        for config in cases:
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate_semantic_config(config)
        self.assertEqual(validate_semantic_config({"url": "http://[::1]:11434/"})["url"], "http://[::1]:11434")

    def test_vector_validation_rejects_empty_nan_bool_and_dimensions(self):
        for vector in ([], [0, 0], [math.nan, 1], [math.inf, 1], [True, 1], ["1", 0], [1] * 4097, [10 ** 400, 1]):
            with self.subTest(vector=str(vector)[:30]), self.assertRaises(ValueError):
                _normalize(vector)
        with self.assertRaises(ValueError):
            _normalize([1, 0], 3)
        self.assertEqual(_normalize([1e308, 0]), [1, 0])

    def test_http_response_shape_size_and_nonfinite_values(self):
        fixture = LocalEmbeddings()
        config = validate_semantic_config({"enabled": True, "url": fixture.url, "model": "fixture"})
        try:
            for response in ({}, {"embeddings": []}, {"embeddings": [[math.nan, 1]]},
                             {"embeddings": [[1], [1]]}, b"x" * (2 * 1024 * 1024 + 1), b"broken json"):
                fixture.response = response
                with self.subTest(response=str(response)[:30]), self.assertRaises(ValueError):
                    embed_texts(config, ["text"])
        finally:
            fixture.close()

    def test_proxy_environment_is_ignored_and_redirects_are_rejected(self):
        fixture = LocalEmbeddings()
        config = validate_semantic_config({"enabled": True, "url": fixture.url, "model": "fixture"})
        try:
            with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1", "no_proxy": ""}):
                self.assertEqual(embed_texts(config, ["retained"]), [[1, 0]])
            fixture.status, fixture.redirect = 302, "http://example.invalid/secret"
            with self.assertRaisesRegex(ValueError, "embedding_redirect_rejected"):
                embed_texts(config, ["retained"])
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
