"""Disposable cache maintenance across short-lived runtimes and real searches."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.knowledge_index import lexical_rows, normalized_query_plan
from agent_learning.runtime import Runtime


class SearchCacheMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.r = Runtime(self.home)
        self.addCleanup(self.r.close)
        self.p = self.r.project(Path(self.temp.name) / 'project')

    def cached(self, key):
        return self.r.db.execute(
            'SELECT rowid,body,(SELECT MAX(rowid) FROM cache) AS newest_rowid,'
            '(SELECT COUNT(*) FROM cache) AS cache_size FROM cache WHERE key=?', (key,)).fetchone()

    def store(self, key, body='[]'):
        cached = self.cached(key)
        with self.r.transaction():
            self.r._store_search_cache(key, body, cached)

    def publish(self, title='cachecompiler'):
        problem = self.r.feedback(self.p, 'chat', title, category='operation')
        packet = self.r.review_packet(self.p, problem['id'])
        candidate = self.r.submit_candidate(self.p, problem_id=problem['id'],
            problem_revision=problem['revision'], title=title, content='Use the documented compiler setting.',
            category='operation', applicability='Same setup', limitations='Reported only',
            validation_level='reported', evidence_refs=[event['id'] for event in packet['events']])
        return self.r.publish(self.p, candidate['candidate_id'])

    def test_repeated_search_keeps_results_metrics_and_avoids_cache_rewrites(self):
        published = self.publish()
        first = self.r.search(self.p, 'cachecompiler')
        before = self.r.db.execute("SELECT value FROM metrics WHERE project=? AND name='search_queries'",
                                   (self.p,)).fetchone()[0]
        statements = []
        self.r.db.set_trace_callback(statements.append)
        try:
            for _ in range(20):
                self.assertEqual(self.r.search(self.p, 'cachecompiler')['items'], first['items'])
        finally:
            self.r.db.set_trace_callback(None)
        cache_writes = [sql for sql in statements if sql.upper().startswith(
            ('INSERT OR REPLACE INTO CACHE ', 'DELETE FROM CACHE '))]
        self.assertEqual(cache_writes, [])
        self.assertEqual(first['items'][0]['knowledge_id'], published['knowledge_id'])
        after = self.r.db.execute("SELECT value FROM metrics WHERE project=? AND name='search_queries'",
                                  (self.p,)).fetchone()[0]
        self.assertEqual(after - before, 20)

    def test_older_hit_refreshes_recency_and_survives_batch_eviction(self):
        for index in range(256):
            self.store(str(index))
        old_rowid = self.cached('0')['rowid']
        self.store('0')
        self.assertGreater(self.cached('0')['rowid'], old_rowid)
        self.store('new')
        self.assertIsNotNone(self.cached('0'))
        self.assertIsNone(self.cached('1'))
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM cache').fetchone()[0], 224)

    def test_separate_runtime_each_insert_still_has_hard_capacity(self):
        for index in range(290):
            other = Runtime(self.home)
            try:
                with other.transaction():
                    other._store_search_cache('short-process-' + str(index), '[]')
                self.assertLessEqual(other.db.execute('SELECT COUNT(*) FROM cache').fetchone()[0], 256)
            finally:
                other.close()

    def test_existing_oversize_cache_is_repaired_even_on_a_recent_hit(self):
        self.r.db.executemany('INSERT INTO cache VALUES(?,?)', [(str(i), '[]') for i in range(400)])
        self.store('399')
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM cache').fetchone()[0], 224)
        self.assertIsNotNone(self.cached('399'))

    def test_sparse_rowids_clear_and_rebuild_do_not_disable_maintenance(self):
        self.r.db.executemany('INSERT INTO cache(rowid,key,body) VALUES(?,?,?)',
                              [(1000 * (i + 1), str(i), '[]') for i in range(300)])
        self.store('299')
        self.assertLessEqual(self.r.db.execute('SELECT COUNT(*) FROM cache').fetchone()[0], 256)
        self.r.rebuild_index()
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM cache').fetchone()[0], 0)
        for index in range(257):
            self.store('after-' + str(index))
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM cache').fetchone()[0], 224)

    def test_corrupt_recent_cached_body_is_recomputed_and_replaced(self):
        published = self.publish()
        self.r.search(self.p, 'cachecompiler')
        key = self.r.db.execute('SELECT key FROM cache').fetchone()[0]
        self.r.db.execute("UPDATE cache SET body='invalid-json' WHERE key=?", (key,))
        result = self.r.search(self.p, 'cachecompiler')
        self.assertEqual(result['items'][0]['knowledge_id'], published['knowledge_id'])
        repaired = json.loads(self.cached(key)['body'])
        self.assertEqual(repaired, [{'knowledge_id': published['knowledge_id'], 'revision': 1}])

    def test_cache_eviction_recomputes_results_after_unrelated_queries(self):
        expected = self.publish()
        self.r.search(self.p, 'cachecompiler')
        for index in range(300):
            self.store('unrelated-project-query-' + str(index))
        found = self.r.search(self.p, 'cachecompiler')
        self.assertEqual(found['items'][0]['knowledge_id'], expected['knowledge_id'])

    def test_lexical_seed_limit_bounds_both_anchor_and_broad_lanes(self):
        for index in range(8):
            self.publish('cachecompiler error_e41 variant ' + str(index))
        plan = normalized_query_plan('cachecompiler error_e41')
        for limit in (0, 1, 2, 5):
            with self.subTest(limit=limit):
                rows = lexical_rows(self.r.db, self.p, plan, max_candidates=limit)
                self.assertLessEqual(len(rows), limit)
        self.assertEqual(len(lexical_rows(self.r.db, self.p, plan)), 8)

    def test_truncated_semantic_absence_does_not_create_a_negative_result_cache(self):
        self.r.config['retrieval']['semantic'].update(enabled=True, model='synthetic', scan_limit=2)
        for index in range(4):
            self.publish('unrelated seed ' + str(index))
        with patch('agent_learning.semantic_retrieval.embed_texts',
                   side_effect=lambda config, texts, expected_dimension=None: [[0.0, 1.0] for _ in texts]):
            self.r.semantic_refresh(self.p)
        self.r.db.execute('DELETE FROM cache')
        with patch('agent_learning.semantic_retrieval.embed_texts', return_value=[[1.0, 0.0]]):
            result = self.r.search(self.p, 'zzzx_no_lexical_overlap')
        self.assertEqual(result['items'], [])
        self.assertTrue(result['retrieval']['scan_truncated'])
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM cache').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
