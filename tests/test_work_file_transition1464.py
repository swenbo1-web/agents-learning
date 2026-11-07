"""Delivery transitions never inherit trust or drift from unrelated metadata."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_learning.runtime import digest
from agent_learning.mcp import Server
from agent_learning.work_files import checkout_key
from agent_learning.work_notice_delivery import canonical_file_states, mark_file_transition, visible_file_keys
from tests import test_work_exposure_full_check as fixtures


class FileTransitionContractTests(unittest.TestCase):
    def test_invalid_primary_does_not_reinterpret_relative_baselines(self):
        checkout = 'a' * 64
        fingerprint = digest({'status': 'stat_observed', 'size': 3, 'mtime_ns': 1})
        data = {checkout + ':same.py': {'fingerprint': fingerprint, 'status': 'stat_observed'}}
        for primary in ([], {}, 1, 'invalid', 'A' * 64):
            with self.subTest(primary=primary):
                self.assertEqual(canonical_file_states(data, baseline={'same.py': fingerprint}, primary=primary), {})
                self.assertIn(checkout + ':same.py', canonical_file_states(data,
                    baseline={checkout + ':same.py': fingerprint}, primary=primary))

    def test_known_unknown_fingerprint_cannot_be_upgraded_by_stat_status(self):
        checkout = 'a' * 64
        for status in ('outside_workspace', 'outside_scope', 'unknown', 'unavailable', 'not_observed'):
            for current in ({'status': status}, {'status': status, 'content_hash': None}):
                with self.subTest(status=status, fingerprint=current):
                    fingerprint = digest(current)
                    states = canonical_file_states({checkout + ':same.py': {
                        'fingerprint': fingerprint, 'status': 'stat_observed'}},
                        baseline={'same.py': fingerprint}, primary=checkout)
                    self.assertEqual(states[checkout + ':same.py']['status'], status)

    def test_inconsistent_unknown_label_stays_unverified(self):
        checkout = 'a' * 64
        fingerprint = digest({'status': 'stat_observed', 'size': 3, 'mtime_ns': 1, 'content_hash': None})
        for status in ('outside_workspace', 'invalid-status', ['stat_observed']):
            with self.subTest(status=status):
                states = canonical_file_states({checkout + ':same.py': {'fingerprint': fingerprint, 'status': status}},
                    baseline={'same.py': fingerprint}, primary=checkout)
                self.assertEqual(states[checkout + ':same.py']['status'], 'unknown')

    def test_direct_marker_rechecks_status_against_unknown_fingerprint(self):
        old = digest({'status': 'outside_workspace', 'content_hash': None})
        current = {'status': 'stat_observed', 'size': 8, 'mtime_ns': 1, 'content_hash': None}
        item = {'file_state': {'same.py': digest(current)},
                'files': [{'relative_path': 'same.py', 'current_version': current, 'version_changed': False}],
                '_previous_file_status': {'same.py': 'stat_observed'}}
        mark_file_transition(item, {'same.py': old})
        self.assertTrue(item.get('file_state_restored'))
        self.assertFalse(item.get('file_state_metadata_changed'))


class FileTransitionDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.WorkExposureFullCheckTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.expose_all()

    def refresh(self, root):
        f = self.f
        server = Server(f.r, f.p, context_handle=f.handle, workspace=f.main)
        try:
            result = server.call('work_memory_search', {'file_path': str(root / 'same.py'), 'max_chars': 6000})
            self.assertIn(f.state_key(root), result['items'][0]['file_state'])
        finally:
            server.close()

    def test_corrupt_status_scope_restoration_matches_uncorrupted_control(self):
        f = self.f
        roots = f.r.config['linked_workspaces']
        f.r.config['linked_workspaces'] = {}
        self.assertTrue(any(card.get('source_files_unverified') for card in f.context()))
        tables = ('work_exposures', 'work_file_check_schedule', 'work_notice_delivery', 'context_delivery_cache')
        saved = {table: [dict(row) for row in f.r.db.execute('SELECT * FROM ' + table + ' WHERE project=?', (f.p,))] for table in tables}
        f.r.config['linked_workspaces'] = roots
        control = f.context(1)
        self.assertTrue(control)
        self.assertFalse(any(card.get('file_version_changed') for card in control))
        for table, rows in saved.items():
            f.r.db.execute('DELETE FROM ' + table + ' WHERE project=?', (f.p,))
            for row in rows:
                f.r.db.execute('INSERT INTO ' + table + '(' + ','.join(row) + ') VALUES(' + ','.join('?' for _ in row) + ')', tuple(row.values()))
        stored = json.loads(f.r.db.execute("SELECT file_states FROM work_file_check_schedule WHERE conversation='reader'").fetchone()[0])
        forged = 0
        for state in stored.values():
            if state['status'] == 'outside_workspace':
                state['status'] = 'stat_observed'
                forged += 1
        self.assertGreater(forged, 0)
        f.r.db.execute("UPDATE work_file_check_schedule SET file_states=? WHERE conversation='reader'", (json.dumps(stored),))
        corrupted = f.context(1)
        self.assertTrue(corrupted)
        self.assertFalse(any(card.get('file_version_changed') for card in corrupted))
        self.assertTrue(all(not file['version_changed'] for card in corrupted for file in card.get('files', [])))

    def test_permission_restoration_excludes_already_acknowledged_companion(self):
        f = self.f
        target, companion = sorted(f.roots[1:], key=f.state_key)[:2]
        (companion / 'same.py').write_text('the companion changed before this observation transition', encoding='utf-8')
        self.refresh(companion)
        original_stat = Path.stat
        def denied(path, *args, **kwargs):
            if path == target / 'same.py':
                raise PermissionError('synthetic unavailable metadata')
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, 'stat', denied):
            unavailable = f.context()
        self.assertTrue(any(card.get('source_files_unverified') for card in unavailable))
        self.assertTrue(all(visible_file_keys(card) <= {f.state_key(target)} for card in unavailable))
        before = f.exposure()
        updates = f.r.work_exposure_updates(f.p, 'reader', workspace=f.main)
        self.assertEqual(set().union(*(set(item['_changed_file_paths']) for item in updates)), {f.state_key(target)})
        restored = f.context(1)
        self.assertTrue(restored)
        self.assertTrue(all(card.get('reference_role') == 'source_update' for card in restored))
        self.assertFalse(any(card.get('file_version_changed') for card in restored))
        self.assertTrue(all(visible_file_keys(card) <= {f.state_key(target)} for card in restored))
        self.assertEqual(f.exposure()[f.state_key(companion)], before[f.state_key(companion)])

    def test_real_stat_return_to_capture_baseline_is_a_new_transition(self):
        f = self.f
        target = min(f.roots[1:], key=f.state_key)
        path = target / 'same.py'
        original = path.stat()
        path.write_text('a different explicitly displayed intermediate version', encoding='utf-8')
        self.refresh(target)
        path.write_text('original', encoding='utf-8')
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        cards = f.context()
        self.assertTrue(any(card.get('file_version_changed') for card in cards))
        self.assertTrue(any(file.get('version_changed') is True for card in cards for file in card.get('files', [])))
        self.assertTrue(all(visible_file_keys(card) <= {f.state_key(target)} for card in cards))

    def test_mixed_restoration_and_real_change_keep_budget_and_per_path_labels(self):
        f = self.f
        target, changed = sorted(f.roots[1:], key=f.state_key)[:2]
        original_stat = Path.stat
        def denied(path, *args, **kwargs):
            if path == target / 'same.py':
                raise PermissionError('synthetic unavailable metadata')
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, 'stat', denied):
            f.context()
        (changed / 'same.py').write_text('a genuinely new version after the previous delivery', encoding='utf-8')
        baseline = f.exposure()
        f.r.config['work_memory']['max_context_chars'] = 128
        self.assertEqual(f.context(1), [])
        self.assertEqual(f.exposure(), baseline)
        f.r.config['work_memory']['max_context_chars'] = 1800
        cards = f.context(2)
        self.assertTrue(any(card.get('file_version_changed') for card in cards))
        files = {file['checkout_key']: file for card in cards for file in card.get('files', [])}
        self.assertFalse(files[checkout_key(target)]['version_changed'])
        self.assertTrue(files[checkout_key(changed)]['version_changed'])
        self.assertEqual(set().union(*(visible_file_keys(card) for card in cards)), {f.state_key(target), f.state_key(changed)})


if __name__ == '__main__':
    unittest.main()
