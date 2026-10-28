"""Real SQLite interruption and damaged legacy data cannot block project use."""
import unittest
from unittest.mock import patch

from agent_learning import knowledge_health as health
from tests import test_knowledge_health as fixtures


class KnowledgeHealthLimitTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.KnowledgeHealthTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.r, self.p = self.f.r, self.f.p

    def test_real_sqlite_read_interrupt_preserves_transaction_and_makes_progress(self):
        for _ in range(3):
            self.f.publish()

        def expensive(runtime, project, row):
            runtime.db.execute('WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL '
                               'SELECT n+1 FROM x WHERE n<10000000) SELECT sum(n) FROM x').fetchone()
            raise AssertionError('SQLite read deadline did not interrupt the expensive query')

        with patch.object(health, '_issues', side_effect=expensive):
            health.advance(self.r, self.p, max_items=1, max_seconds=.02)
        self.assertFalse(self.r.db.in_transaction)
        report = health.report(self.r.db, self.p)
        self.assertEqual(report['scanned'], 1)
        self.assertEqual(report['issue_counts'], {'check_budget_partial': 1})
        self.assertEqual(self.f.finish()['scanned'], 3)

    def test_missing_recovery_reference_does_not_establish_unresolved_failure(self):
        _, events = self.f.source(('failed', 'succeeded'))
        self.r.db.execute('DELETE FROM problem_events WHERE event=?', (events[-1]['id'],))
        report = self.f.finish()
        self.assertIn('source_missing', report['issue_counts'])
        self.assertIn('source_observed_failure_recovery_unknown', report['issue_counts'])
        self.assertNotIn('unresolved_source_failure', report['issue_counts'])

    def test_deep_legacy_json_is_a_format_observation_not_a_project_failure(self):
        record = self.f.publish()
        deep = '[' * 2000 + '0' + ']' * 2000
        self.r.db.execute('UPDATE knowledge SET body=? WHERE project=? AND id=?',
                          (deep, self.p, record['knowledge_id']))
        report = self.f.finish()
        self.assertEqual(report['scanned'], 1)
        self.assertEqual(report['issue_counts'], {'body_invalid': 1})

    def test_corrupt_derived_report_is_unavailable_and_never_mutates_authority(self):
        self.f.publish()
        self.f.finish()
        self.r.db.execute("UPDATE knowledge_health_runs SET issue_counts='bad-json' WHERE project=?", (self.p,))
        before = self.f.authority()
        report = health.report(self.r.db, self.p)
        self.assertEqual(report['state'], 'unavailable')
        self.assertIsNone(report['scanned'])
        self.assertEqual(self.f.authority(), before)
