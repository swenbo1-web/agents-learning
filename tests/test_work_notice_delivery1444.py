"""Coalesce ordinary file updates without acknowledging unsent warnings."""
import json
import time
import unittest
from unittest.mock import patch

from agent_learning.work_notice_delivery import FileNoticeGate, coalesce
from tests import test_context_dedup as fixtures


class FileNoticeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ContextDedupTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.r, self.p = self.f.r, self.f.p
        self.source = self.f.source(file=True)
        self.identity = self.source['memory']['memory_id']
        self.f.context()
        self.now = time.time() + 1

    def change(self, value):
        (self.f.workspace / 'ledger.py').write_text(value, encoding='utf-8')

    def context(self, seconds=0):
        with patch('agent_learning.work_notice_delivery.time.time', return_value=self.now + seconds):
            return self.f.context()

    def test_first_warning_immediate_then_latest_state_after_cooldown(self):
        self.change('ledger = 200\n')
        first = self.context()
        self.assertIn(self.identity, first)
        self.assertIn('重新核对', first)
        self.assertNotIn(self.f.CONTENT, first)
        exposure = self.r.db.execute('SELECT file_state FROM work_exposures WHERE memory_id=?', (self.identity,)).fetchone()[0]
        self.change('ledger = 30000\n')
        self.assertNotIn(self.identity, self.context(1))
        self.assertEqual(exposure, self.r.db.execute('SELECT file_state FROM work_exposures WHERE memory_id=?', (self.identity,)).fetchone()[0])
        self.assertIn(self.identity, self.context(31))
        self.assertNotIn(self.identity, self.context(32))

    def test_tiny_budget_does_not_acknowledge_or_start_cooldown(self):
        self.change('ledger = 200\n')
        self.r.config['injection_max_chars'] = 128
        self.assertNotIn(self.identity, self.context())
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM work_notice_delivery').fetchone()[0], 0)
        self.r.config['injection_max_chars'] = 2400
        self.assertIn(self.identity, self.context(1))

    def test_optional_cache_missing_fails_open(self):
        self.change('ledger = 200\n')
        self.context()
        self.r.db.execute('DROP TABLE work_notice_delivery')
        self.change('ledger = 30000\n')
        self.assertIn(self.identity, self.context(1))

    def test_source_change_bypasses_recent_file_warning(self):
        self.change('ledger = 200\n')
        self.context()
        self.r.db.execute("UPDATE events SET payload=? WHERE id=?",
                          ('{"last_assistant_message":"changed source"}', self.source['event']['id']))
        result = self.context(1)
        self.assertIn(self.identity, result)
        self.assertIn('source_changed', result)

    @staticmethod
    def notice(identity='one', path='a.py', **updates):
        return dict({'memory_id': identity, 'revision': 1, 'source_version': 'source',
                     'file_state_changed': True, 'file_state': {path: 'new'}, 'state': 'reported',
                     'validation_level': 'reported'}, **updates)

    def gate(self, scope='scope', delivery='persistent'):
        return FileNoticeGate(self.r, self.p, 'reader', '', scope, delivery)

    def test_risk_flags_and_ephemeral_delivery_never_defer(self):
        gate = self.gate()
        with self.r.transaction():
            gate.record(self.notice())
        self.assertTrue(gate.deferred(self.notice()))
        for updates in ({'source_changed': True}, {'state': 'conflicted'}, {'state': 'missing'},
                        {'state': 'denied'}, {'candidate_reference_risk': ['risk']},
                        {'pending_counterexample_count': 1}, {'completion': 'partial'},
                        {'files': [{'version_changed': False}]}):
            with self.subTest(updates=updates):
                self.assertFalse(gate.deferred(self.notice(**updates)))
        self.assertFalse(self.gate(delivery='ephemeral').deferred(self.notice()))
        self.assertFalse(self.gate(scope='different-task').deferred(self.notice()))
        self.assertFalse(gate.deferred(self.notice(path='new.py')))

    def test_grouping_keeps_every_identity_and_never_merges_source_risks(self):
        one, two = self.notice(), self.notice('two')
        grouped = coalesce([one, two, self.notice('risk', source_changed=True)])
        self.assertEqual(len(grouped), 2)
        self.assertEqual([x['memory_id'] for x in grouped[0]['_notice_members']], ['one', 'two'])
        self.assertNotIn('_notice_members', grouped[1])
        self.assertNotIn('_notice_members', one)

    def test_grouped_context_records_all_visible_member_ids(self):
        other = self.f.source(file=True)
        # Equal business content may be deduplicated on initial automatic
        # delivery. Reproduce two genuinely shown records through manual reads.
        row = self.r.db.execute('SELECT * FROM work_memories WHERE id=?',
                                (other['memory']['memory_id'],)).fetchone()
        self.r.work_record_exposure(self.p, self.f.conversation,
                                    [self.r._work_item(self.p, row, workspace=self.f.workspace)], method='mcp')
        self.change('ledger = 200\n')
        result = self.context()
        cards = self.f.work_cards(result)
        warnings = [x for x in cards if x.get('file_state_changed')]
        self.assertEqual(len(warnings), 1)
        visible = [warnings[0]['memory_id'], *[item['memory_id'] for item in warnings[0]['related_memories']]]
        self.assertEqual(set(visible), {self.identity, other['memory']['memory_id']})
        self.assertEqual(warnings[0]['related_memories'][0]['revision'], 1)
        self.assertNotIn(self.identity, self.context(1))
        self.assertNotIn(other['memory']['memory_id'], self.context(1))

    def test_clock_rollback_is_not_an_indefinite_suppression(self):
        gate = self.gate()
        with patch('agent_learning.work_notice_delivery.time.time', return_value=self.now):
            gate.record(self.notice())
        with patch('agent_learning.work_notice_delivery.time.time', return_value=self.now - 10):
            self.assertFalse(gate.deferred(self.notice()))

    def test_malformed_cache_timestamp_fails_open(self):
        gate = self.gate()
        gate.record(self.notice())
        self.r.db.execute("UPDATE work_notice_delivery SET created='invalid'")
        self.assertFalse(gate.deferred(self.notice()))

    def test_three_deferred_file_notices_do_not_hide_the_fourth_source_risk(self):
        for sequence in range(3):
            source = self.f.source(file=True)
            row = self.r.db.execute('SELECT * FROM work_memories WHERE id=?',
                                    (source['memory']['memory_id'],)).fetchone()
            self.r.work_record_exposure(self.p, self.f.conversation,
                [self.r._work_item(self.p, row, workspace=self.f.workspace)], method='mcp')
            self.r.db.execute('UPDATE work_exposures SET created=? WHERE memory_id=?',
                              (self.now + sequence, source['memory']['memory_id']))
        self.change('ledger = 200\n')
        self.assertIn('file_state_changed', self.context())
        self.change('ledger = 30000\n')
        self.r.db.execute("UPDATE events SET payload=? WHERE id=?",
                          ('{"last_assistant_message":"changed source"}', self.source['event']['id']))
        text = self.context(1)
        self.assertIn(self.identity, text)
        self.assertIn('source_changed', text)


if __name__ == '__main__':
    unittest.main()
