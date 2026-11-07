"""Previously displayed files are verified independently of bounded previews."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import sqlite3
from unittest.mock import patch

from agent_learning.runtime import Runtime
from agent_learning.mcp import Server
from agent_learning.work_files import checkout_key
from agent_learning.work_memory import WORK_EXPOSURE_FILE_CHECK_LIMIT
from agent_learning.work_notice_delivery import visible_file_keys


class WorkExposureFullCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.roots = [self.base / ('checkout-%d' % index) for index in range(9)]
        for root in self.roots:
            root.mkdir()
            (root / 'same.py').write_text('original', encoding='utf-8')
        self.main, self.home = self.roots[0], self.base / 'home'
        initial = Runtime(self.home, config={})
        self.p = initial.project(self.main)
        initial.close()
        self.r = Runtime(self.home, config={'linked_workspaces': {self.p: [str(root) for root in self.roots[1:]]},
            'admin': {'enabled': False}, 'scheduler': {'enabled': False}, 'hindsight': {'enabled': False},
            'work_memory': {'max_items': 5, 'max_context_chars': 1800}})
        self.addCleanup(self.r.close)
        self.r.task_update(self.p, 'reader', 'parser transaction boundary', {'mode': 'analysis'})
        self.handle = self.r.register_context(self.p, 'reader')['context_handle']
        raw = {'tool_name': 'apply_patch', 'tool_input': '*** Begin Patch\n' + ''.join(
            '*** Update File: ' + str(root / 'same.py') + '\n' for root in self.roots) + '*** End Patch',
            'tool_response': {'exit_code': 0, 'output': 'parser transaction boundary changed in the synthetic source'}}
        with self.r.transaction():
            self.event, _ = self.r._event(self.p, 'writer', 'PostToolUse', raw, outcome='succeeded')
            self.created = self.r._work_capture_event(self.p, self.event['id'], self.main, raw=raw)
        self.memory = self.r.db.execute('SELECT * FROM work_memories WHERE id=?', (self.created['memory_id'],)).fetchone()
        self.now = time.time() + 1

    def expose_all(self):
        server = Server(self.r, self.p, context_handle=self.handle, workspace=self.main)
        self.addCleanup(server.close)
        for root in self.roots:
            result = server.call('work_memory_search', {'file_path': str(root / 'same.py'), 'max_chars': 6000})
            self.assertTrue(any(file.get('checkout_key') == checkout_key(root)
                for file in result['items'][0]['files']))
        self.assertEqual(len(self.exposure()), 9)

    def exposure(self):
        row = self.r.db.execute("SELECT file_state FROM work_exposures WHERE conversation='reader'").fetchone()
        return json.loads(row[0]) if row else {}

    def state_key(self, root):
        return 'same.py' if root == self.main else checkout_key(root) + ':same.py'

    def context(self, step=0):
        with patch('agent_learning.work_notice_delivery.time.time', return_value=self.now + step * 31):
            text = self.r._context(self.p, 'reader', query='parser transaction boundary',
                context_handle=self.handle, workspace=self.main, tool_only=True)
        return [json.loads(line.split(' ', 1)[1]) for line in text.splitlines() if line.startswith('[工作历史] ')]

    def test_public_mcp_tail_file_change_reaches_actual_context(self):
        self.expose_all()
        tail = max(self.roots[1:], key=self.state_key)
        (tail / 'same.py').write_text('tail source changed after actual MCP display', encoding='utf-8')
        updates = self.r.work_exposure_updates(self.p, 'reader', workspace=self.main)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['file_observation']['checked_count'], 9)
        self.assertTrue(updates[0]['file_observation']['complete'])
        self.assertLessEqual(len(updates[0]['files']), 3)
        self.assertIn(self.state_key(tail), updates[0]['_changed_file_paths'])
        cards = self.context()
        self.assertTrue(cards)
        self.assertIn(self.state_key(tail), set().union(*(visible_file_keys(card) for card in cards)))

    def test_simultaneous_changes_progress_without_acknowledging_hidden_files(self):
        self.expose_all()
        for root in self.roots:
            (root / 'same.py').write_text('all roots changed together', encoding='utf-8')
        acknowledged = set()
        for step in range(7):
            before = self.exposure()
            cards = self.context(step)
            visible = set().union(*(visible_file_keys(card) for card in cards)) if cards else set()
            after = self.exposure()
            changed = {key for key in after if after[key] != before[key]}
            self.assertEqual(changed, visible)
            acknowledged.update(changed)
            if len(acknowledged) == 9:
                break
        self.assertEqual(len(acknowledged), 9)
        self.assertEqual(self.context(10), [])

    def test_tiny_budget_does_not_confirm_changed_paths(self):
        self.expose_all()
        original = self.exposure()
        for root in self.roots:
            (root / 'same.py').write_text('changed under a narrow budget', encoding='utf-8')
        self.r.config['work_memory']['max_context_chars'] = 128
        self.assertEqual(self.context(), [])
        self.assertEqual(self.exposure(), original)
        self.r.config['work_memory']['max_context_chars'] = 1800
        self.assertTrue(self.context(1))

    def test_revoked_previously_exposed_file_has_unverified_notice_without_stat(self):
        self.expose_all()
        revoked = max(self.roots[1:], key=self.state_key)
        self.r.config['linked_workspaces'] = {}
        original_stat = Path.stat
        def guard(path, *args, **kwargs):
            if any(path == root or root in path.parents for root in self.roots[1:]):
                raise AssertionError('revoked checkout was inspected')
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, 'stat', guard):
            updates = self.r.work_exposure_updates(self.p, 'reader', workspace=self.main)
            self.assertTrue(updates)
            self.assertTrue(updates[0]['file_state_unverified'])
            self.assertFalse(updates[0]['file_observation']['complete'])
            self.assertTrue(all(file.get('version_unverified') and file['version_changed'] is False
                                for file in updates[0]['files']))
            cards = self.context()
        self.assertTrue(cards)
        self.assertTrue(any(card.get('source_files_unverified') for card in cards))
        self.assertTrue(any('无法核实' in card.get('recheck', '') for card in cards))
        self.assertFalse(any(card.get('file_version_changed') for card in cards))

    def test_same_call_file_cache_and_root_resolution_are_shared(self):
        self.expose_all()
        from agent_learning.work_memory import current_file_version, file_workspace_roots
        with (patch('agent_learning.work_memory.current_file_version', wraps=current_file_version) as version,
              patch('agent_learning.work_memory.file_workspace_roots', wraps=file_workspace_roots) as roots):
            self.assertEqual(self.r.work_exposure_updates(self.p, 'reader', workspace=self.main), [])
        self.assertEqual(version.call_count, 9)
        self.assertEqual(roots.call_count, 1)

    def test_restored_scope_does_not_claim_unchanged_file_versions_changed(self):
        self.expose_all()
        original = self.r.config['linked_workspaces']
        self.r.config['linked_workspaces'] = {}
        lost = self.context()
        self.assertTrue(any(card.get('source_files_unverified') for card in lost))
        self.r.config['linked_workspaces'] = original
        restored = self.context(1)
        self.assertTrue(restored)
        self.assertFalse(any(file.get('version_changed') for card in restored for file in card.get('files', [])))
        self.assertFalse(any(card.get('file_version_changed') for card in restored))

    def test_primary_workspace_switch_retains_original_checkout_identity(self):
        self.expose_all()
        other = self.roots[1]
        self.r.config['project_aliases'][str(other)] = self.p
        updates = self.r.work_exposure_updates(self.p, 'reader', workspace=other)
        self.assertTrue(updates)
        lost_primary = checkout_key(self.main) + ':same.py'
        changed = set().union(*(set(item['_changed_file_paths']) for item in updates))
        # The original primary becomes out of scope; its same-name baseline
        # must never be relabelled as the new current primary checkout.
        self.assertIn(lost_primary, changed)
        for item in updates:
            for file in item['files']:
                if file['checkout_key'] == checkout_key(self.main):
                    self.assertTrue(file['version_unverified'])
                    self.assertFalse(file['version_changed'])

    def test_stale_derived_status_cannot_override_independently_updated_core(self):
        self.expose_all()
        before = self.exposure()
        before['same.py'] = 'independent-old-runtime-baseline'
        self.r.db.execute("UPDATE work_exposures SET file_state=? WHERE conversation='reader'", (json.dumps(before),))
        updates = self.r.work_exposure_updates(self.p, 'reader', workspace=self.main)
        for item in updates:
            self.assertNotIn('same.py', item.get('_previous_file_status', {}))
        self.assertEqual(self.exposure(), before)

    def test_legacy_unknown_identity_notice_is_not_repeated_or_budget_acknowledged(self):
        self.expose_all()
        baseline = {'same.py': self.exposure()['same.py']}
        self.r.db.execute("UPDATE work_exposures SET file_state=? WHERE conversation='reader'", (json.dumps(baseline),))
        self.r.db.execute("DELETE FROM work_file_check_schedule WHERE conversation='reader'")
        self.r.config['work_memory']['max_context_chars'] = 128
        self.assertEqual(self.context(), [])
        token = self.r.db.execute("SELECT uncertainty_token FROM work_file_check_schedule WHERE conversation='reader'").fetchone()
        self.assertTrue(token is None or token[0] is None)
        self.assertEqual(self.exposure(), baseline)
        self.r.config['work_memory']['max_context_chars'] = 1800
        notices = [self.context(step) for step in range(1, 4)]
        self.assertTrue(any(card.get('source_files_unverified') for card in notices[0]))
        self.assertFalse(any(card.get('source_files_unverified') for cards in notices[1:] for card in cards))

    def test_rotation_write_failure_advances_fallback_and_recovers(self):
        self.expose_all()
        raw = {'tool_name': 'apply_patch', 'tool_input': '*** Begin Patch\n' + ''.join(
            '*** Update File: ' + str(root / 'same.py') + '\n' for root in self.roots) + '*** End Patch',
            'tool_response': {'exit_code': 0, 'output': 'another synthetic observed source'}}
        with self.r.transaction():
            event, _ = self.r._event(self.p, 'writer-2', 'PostToolUse', raw, outcome='succeeded')
            created = self.r._work_capture_event(self.p, event['id'], self.main, raw=raw)
            row = self.r.db.execute('SELECT * FROM work_memories WHERE id=?', (created['memory_id'],)).fetchone()
            second = self.r._work_item(self.p, row, workspace=self.main)
            self.r.work_record_exposure(self.p, 'reader', [second])
        self.r.db.execute("CREATE TRIGGER fail_rotation BEFORE UPDATE OF last_checked ON work_file_check_schedule BEGIN SELECT RAISE(FAIL,'synthetic'); END")
        checked = []
        original = self.r._work_item
        def observe(*args, **kwargs):
            if kwargs.get('workspace') and kwargs.get('_previous_file_state'):
                checked.append(args[1]['id'])
            return original(*args, **kwargs)
        with patch('agent_learning.work_memory.WORK_EXPOSURE_CALLBACK_FILE_LIMIT', 9), patch.object(self.r, '_work_item', side_effect=observe):
            self.r.work_exposure_updates(self.p, 'reader', workspace=self.main)
            first = set(checked)
            checked.clear()
            self.r.work_exposure_updates(self.p, 'reader', workspace=self.main)
            self.assertTrue(set(checked) - first)
        self.r.db.execute('DROP TRIGGER fail_rotation')
        self.r.work_exposure_updates(self.p, 'reader', workspace=self.main)
        self.assertNotIn((self.p, 'reader', ''), getattr(self.r, '_work_file_schedule_failures', set()))
        self.assertGreater(self.r.db.execute('SELECT MAX(last_checked) FROM work_file_check_schedule').fetchone()[0], 0)

    def test_delivery_failure_keeps_core_and_derived_baselines_atomic(self):
        self.expose_all()
        before = self.exposure()
        saved = [tuple(row) for row in self.r.db.execute('SELECT primary_checkout,file_states FROM work_file_check_schedule')]
        tail = max(self.roots[1:], key=self.state_key)
        (tail / 'same.py').write_text('changed before an optional delivery receipt failure', encoding='utf-8')
        self.r.db.execute("CREATE TRIGGER fail_delivery BEFORE UPDATE OF file_states ON work_file_check_schedule BEGIN SELECT RAISE(FAIL,'synthetic'); END")
        self.assertTrue(self.context())
        self.assertEqual(self.exposure(), before)
        self.assertEqual([tuple(row) for row in self.r.db.execute('SELECT primary_checkout,file_states FROM work_file_check_schedule')], saved)
        self.r.db.execute('DROP TRIGGER fail_delivery')

    def test_version_62_receipt_reopens_and_installs_optional_schedule(self):
        self.expose_all()
        self.r.db.execute('DROP TABLE work_file_check_schedule')
        self.r.db.execute("UPDATE runtime_storage_receipt SET signature=replace(signature,'1.4.63','1.4.62')")
        before = self.exposure()
        reopened = Runtime(self.home, config=self.r.config)
        try:
            self.assertTrue(reopened.db.execute("SELECT 1 FROM sqlite_master WHERE name='work_file_check_schedule'").fetchone())
            self.assertEqual(json.loads(reopened.db.execute("SELECT file_state FROM work_exposures WHERE conversation='reader'").fetchone()[0]), before)
        finally:
            reopened.close()

    def test_more_than_32_recorded_keys_does_not_claim_complete(self):
        self.expose_all()
        known = self.exposure()
        known.update({'unavailable-%d.py' % index: 'old' for index in range(30)})
        item = self.r._work_item(self.p, self.memory, workspace=self.main, _previous_file_state=known)
        self.assertFalse(item['file_observation']['complete'])
        self.assertEqual(item['file_observation']['reason'], 'file_check_limit')
        self.assertEqual(item['file_observation']['total_count'], 39)
        self.assertLessEqual(item['file_observation']['checked_count'], WORK_EXPOSURE_FILE_CHECK_LIMIT)
        self.assertLessEqual(len(item['files']), 3)

    def test_missing_original_association_stays_pending_until_exact_source_returns(self):
        self.expose_all()
        baseline = self.exposure()
        tail = max(self.roots[1:], key=self.state_key)
        original = dict(self.r.db.execute('SELECT * FROM work_file_links WHERE checkout_key=?',
                                          (checkout_key(tail),)).fetchone())
        with self.r.transaction():
            self.r.db.execute('DELETE FROM work_file_links WHERE checkout_key=?', (checkout_key(tail),))
        item = self.r._work_item(self.p, self.memory, workspace=self.main, _previous_file_state=baseline)
        self.assertFalse(item['file_observation']['complete'])
        self.assertEqual(item['file_observation']['reason'], 'file_association_unavailable')
        self.assertEqual(self.r.work_exposure_updates(self.p, 'reader', workspace=self.main), [])
        self.assertEqual(self.exposure(), baseline)
        (tail / 'same.py').write_text('changed while derived association was unavailable', encoding='utf-8')
        with self.r.transaction():
            self.r.db.execute('INSERT INTO work_file_links VALUES(?,?,?,?,?,?,?)', tuple(original.values()))
        updates = self.r.work_exposure_updates(self.p, 'reader', workspace=self.main)
        self.assertTrue(updates)
        self.assertIn(self.state_key(tail), updates[0]['_changed_file_paths'])
        self.assertTrue(updates[0]['file_observation']['complete'])


if __name__ == '__main__':
    unittest.main()
