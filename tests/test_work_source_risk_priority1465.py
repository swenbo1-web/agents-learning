"""Pending validity warnings precede ordinary indexes without granting authority."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_learning.runtime import digest
from agent_learning.mcp import Server
from agent_learning import candidate_references as refs
from agent_learning.work_notice_delivery import pending_source_risk
from tests import test_work_exposure_full_check as fixtures


class WorkSourceRiskPriorityTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.WorkExposureFullCheckTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.expose_all()
        f = self.f
        self.path = f.main / 'risk.py'
        self.path.write_text('synthetic risk source', encoding='utf-8')
        f.r.task_update(f.p, 'risk-writer', 'parser transaction boundary risk source', {'mode': 'analysis'})
        self.base = {'session_id': 'risk-writer', 'cwd': str(f.main), 'tool_name': 'write_file',
                     'tool_use_id': 'risk-operation', 'tool_input': {'path': str(self.path)}}
        f.r.handle_hook(dict(self.base, hook_event_name='PreToolUse'))
        f.r.handle_hook(dict(self.base, hook_event_name='PostToolUse', tool_response={
            'exit_code': 0, 'output': 'parser transaction boundary risk source'}))
        self.event = dict(f.r.db.execute("SELECT * FROM events WHERE conversation='risk-writer' AND type='PostToolUse'").fetchone())
        self.risk_id = f.r.db.execute('SELECT id FROM work_memories WHERE event_id=?', (self.event['id'],)).fetchone()[0]
        self.search(self.path)
        self.ordinary_path = f.roots[-1] / 'same.py'
        for index in range(6):
            raw = {'tool_name': 'write_file', 'tool_input': {'path': str(self.ordinary_path)},
                   'tool_response': {'exit_code': 0, 'output': 'parser transaction boundary ordinary source %d' % index}}
            with f.r.transaction():
                event, _ = f.r._event(f.p, 'ordinary-%d' % index, 'PostToolUse', raw, outcome='succeeded')
                f.r._work_capture_event(f.p, event['id'], f.main, raw=raw)
        self.search(self.ordinary_path)

    def search(self, path):
        f = self.f
        server = Server(f.r, f.p, context_handle=f.handle, workspace=f.main)
        try:
            return server.call('work_memory_search', {'file_path': str(path), 'limit': 10, 'max_chars': 20000})
        finally:
            server.close()

    def conflict(self):
        self.f.r.handle_hook(dict(self.base, hook_event_name='PostToolUse', tool_response={
            'exit_code': 1, 'output': 'synthetic contradictory terminal completion'}))

    def shown(self, conversation='reader'):
        return self.f.r.db.execute('SELECT source_version FROM work_exposures WHERE conversation=? AND memory_id=?',
                                  (conversation, self.risk_id)).fetchone()[0]

    def candidate(self):
        f = self.f
        task = f.r.task_update(f.p, 'candidate-source', 'parser transaction boundary reference', {'mode': 'analysis'})
        with f.r.transaction():
            event, _ = f.r._event(f.p, 'candidate-source', 'Stop', {'last_assistant_message':
                'A separate parser transaction boundary reference for optional investigation.'},
                task_id=task['id'], task_revision=task['revision'], outcome='reported')
            problem = f.r._problem(f.p, 'candidate-source', 'parser transaction boundary candidate',
                                   'operation', [event['id']], task_binding=task)
            submitted = f.r.submit_candidate(f.p, problem_id=problem['id'], problem_revision=problem['revision'],
                title='parser transaction boundary reference', content='Inspect parser transaction boundaries in this synthetic project.',
                category='operation', knowledge_kind='solution', metadata={}, applicability='Current synthetic parser project.',
                limitations='Reported unverified reference; validate before use.', validation_level='reported', evidence_refs=[event['id']])
        refs.configure(f.r, f.p, True, refs.policy(f.r, f.p)['version'])
        return submitted['candidate_id']

    def context_with_candidates(self):
        f = self.f
        text = f.r._context(f.p, 'reader', query='parser transaction boundary', context_handle=f.handle, workspace=f.main)
        cards = [json.loads(line.split(' ', 1)[1]) for line in text.splitlines() if line.startswith('[工作历史] ')]
        return text, cards

    def test_h64_burst_emits_pending_conflict_in_first_context_and_advances_files(self):
        f = self.f
        before = self.shown()
        self.conflict()
        self.ordinary_path.write_text('ordinary burst source changed concurrently', encoding='utf-8')
        cards = f.context()
        self.assertTrue(cards)
        self.assertEqual(cards[0]['memory_id'], self.risk_id)
        self.assertEqual(cards[0]['state'], 'conflicted')
        self.assertTrue(cards[0]['source_changed'])
        self.assertTrue(cards[0]['read_required'])
        self.assertEqual(cards[0]['validation_level'], 'tool_observed')
        self.assertNotEqual(self.shown(), before)
        self.assertTrue(any(card.get('file_state_changed') for card in cards[1:]))
        for turn in range(1, 4):
            self.ordinary_path.write_text('continuously changing ordinary source %d' % turn, encoding='utf-8')
            later = f.context(turn)
            self.assertNotIn(self.risk_id, [card['memory_id'] for card in later])
            self.assertTrue(any(card.get('file_state_changed') for card in later))
        self.assertEqual(f.r.db.execute('SELECT payload FROM events WHERE id=?', (self.event['id'],)).fetchone()[0], self.event['payload'])

    def test_pending_risk_precedes_new_candidate_in_default_600_pool(self):
        f = self.f
        candidate = self.candidate()
        self.assertTrue(refs.search(f.r, f.p, 'parser transaction boundary', context_handle=f.handle, workspace=f.main))
        f.r.config['work_memory']['max_context_chars'] = 600
        self.conflict()
        text, cards = self.context_with_candidates()
        self.assertTrue(any(card['memory_id'] == self.risk_id for card in cards))
        self.assertNotIn(candidate, text)
        next_text, next_cards = self.context_with_candidates()
        self.assertIn(candidate, next_text)
        self.assertNotIn(self.risk_id, [card['memory_id'] for card in next_cards])

    def test_tiny_pool_does_not_acknowledge_risk_or_candidate(self):
        f = self.f
        candidate = self.candidate()
        self.conflict()
        before = self.shown()
        f.r.config['work_memory']['max_context_chars'] = 128
        text, cards = self.context_with_candidates()
        self.assertNotIn(self.risk_id, text)
        self.assertNotIn(candidate, text)
        self.assertEqual(self.shown(), before)
        f.r.config['work_memory']['max_context_chars'] = 600
        self.assertTrue(any(card['memory_id'] == self.risk_id for card in self.context_with_candidates()[1]))

    def test_owner_boundary_does_not_consume_another_readers_risk(self):
        f = self.f
        other = f.r.register_context(f.p, 'other-reader')['context_handle']
        self.conflict()
        before = self.shown()
        text = f.r._context(f.p, 'other-reader', query='parser transaction boundary', context_handle=other,
                            workspace=f.main, tool_only=True)
        self.assertEqual(self.shown(), before)
        self.assertNotIn(self.risk_id, text)
        self.assertEqual(f.context()[0]['memory_id'], self.risk_id)

    def test_validity_predicate_uses_current_snapshot_and_explicit_risks(self):
        f = self.f
        row = f.r.db.execute('SELECT * FROM work_memories WHERE id=?', (self.risk_id,)).fetchone()
        snapshot = f.r.work_source_snapshot(f.p, self.event['id'])
        for state, expected in (('succeeded', None), ('reported', None), ('call_completed', None),
                                ('conflicted', 'operation_conflict'), ('denied', 'operation_denied'),
                                ('failed', 'source_failed'), ('missing', 'source_missing')):
            with self.subTest(state=state):
                current = dict(snapshot, state=state, source_version='new-observation-version')
                if state == 'missing':
                    current.pop('capture_digest')
                with patch.object(f.r, 'work_source_snapshot', return_value=current):
                    item, files, kind = f.r._work_source_item(f.p, row)
                self.assertEqual(kind, expected)
                # A changed observation identity alone does not establish a
                # validity risk or a new execution permission.
                self.assertTrue(item['source_changed'])
                self.assertFalse(item['execution_authority'])
        for target, result, expected in (
                ('agent_learning.candidate_references.lineage_risk', {'blocked': True}, 'candidate_reference_risk'),
                ('agent_learning.work_publication_risk.assess', [{'blocked': True}], 'published_knowledge_risk')):
            with self.subTest(expected=expected), patch(target, return_value=result):
                self.assertEqual(f.r._work_source_item(f.p, row)[2], expected)
        with patch.object(f.r, 'work_source_snapshot', return_value=dict(snapshot, capture_digest='changed-authority-hash')):
            self.assertEqual(f.r._work_source_item(f.p, row)[2], 'capture_mismatch')
        self.assertFalse(pending_source_risk({'source_changed': True, 'state': 'source_changed'}))
        self.assertFalse(pending_source_risk({'_source_risk_kind': 'operation_conflict', '_pending_source_risk': False}))

    def test_source_snapshot_cache_is_local_and_shared_only_within_one_snapshot(self):
        f = self.f
        row = f.r.db.execute('SELECT * FROM work_memories WHERE id=?', (self.risk_id,)).fetchone()
        with patch.object(f.r, 'work_source_snapshot', wraps=f.r.work_source_snapshot) as read:
            cache = {}
            first = f.r._work_source_item(f.p, row, _source_cache=cache)
            f.r._work_item(f.p, row, _source_projection=first)
            f.r._work_source_item(f.p, row, _source_cache=cache)
            self.assertEqual(read.call_count, 1)
            f.r._work_source_item(f.p, row)  # An independent validation cannot borrow recall's cache.
            self.assertEqual(read.call_count, 2)

    def test_multifile_focus_preserves_the_selected_filename_in_title(self):
        f = self.f
        paths = [f.main / name for name in ('first.py', 'middle.py', 'target.py')]
        for path in paths:
            path.write_text('synthetic grouped target', encoding='utf-8')
        raw = {'tool_name': 'apply_patch', 'tool_input': '*** Begin Patch\n' + ''.join(
            '*** Update File: ' + str(path) + '\n' for path in paths) + '*** End Patch',
            'tool_response': {'exit_code': 0, 'output': 'parser transaction boundary grouped mutation'}}
        with f.r.transaction():
            event, _ = f.r._event(f.p, 'grouped-writer', 'PostToolUse', raw, outcome='succeeded')
            created = f.r._work_capture_event(f.p, event['id'], f.main, raw=raw)
        result = self.search(paths[-1])
        selected = next(item for item in result['items'] if item['memory_id'] == created['memory_id'])
        self.assertTrue(selected['title'].startswith('文件操作记录：target.py'), selected['title'])


if __name__ == '__main__':
    unittest.main()
