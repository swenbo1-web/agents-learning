"""Stage review lifecycle boundaries, isolated SQLite and deterministic reviewers."""
import json
import subprocess
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_lifecycle import retry_call
from tests import test_phase_review as fixtures


class PhaseReviewBoundaryTests(unittest.TestCase):
    setUp = fixtures.PhaseReviewTests.setUp
    phase = fixtures.PhaseReviewTests.phase

    def test_current_model_dedup_ignores_unrelated_ambiguous_observation(self):
        self.r.admin_configure(self.p, True, 'host', 'current', '', expected_policy_version=0)
        for conversation in ('one', 'two', 'three', 'four'):
            self.r.admin_observe_model(self.p, conversation, 'model-a')
        self.r.admin_observe_model(self.p, 'unrelated-chat', 'model-b')
        self.phase('one'); self.phase('two')
        with patch('agent_learning.admin.subprocess.Popen', fixtures.reviewer('skip')):
            self.assertEqual(admin_once(self.r, self.p)['status'], 'skip')
        before = self.r.db.execute('SELECT COUNT(*) FROM admin_calls').fetchone()[0]
        self.phase('three'); self.phase('four')
        with patch('agent_learning.admin.subprocess.Popen') as launch:
            admin_once(self.r, self.p)
            self.assertEqual(launch.call_count, 0, 'Equivalent stage evidence should reuse its accepted review')
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM admin_calls').fetchone()[0], before)

    def test_explicit_retry_reopens_same_frozen_phase_batch(self):
        self.phase('one'); self.phase('two', 'Other facts')
        class Timeout(fixtures.reviewer()):
            def communicate(self, *args, **kwargs):
                raise subprocess.TimeoutExpired('controlled', 5)
        with patch('agent_learning.admin.subprocess.Popen', Timeout):
            first = admin_once(self.r, self.p)
        self.assertEqual(first['status'], 'timeout_unknown', first)
        call = self.r.db.execute('SELECT id,problem FROM admin_calls').fetchone()
        self.assertEqual(retry_call(self.r, self.p, call['id'], 'User confirmed bounded retry')['status'], 'retry_allowed')
        self.r.db.execute('UPDATE admin_executor_failures SET next_try=0')
        with patch('agent_learning.admin.subprocess.Popen', fixtures.reviewer('skip')):
            second = admin_once(self.r, self.p)
        self.assertEqual(second['status'], 'skip', second)
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM admin_calls').fetchone()[0], 2)
        self.assertEqual(self.r.db.execute('SELECT state FROM admin_calls WHERE id=?', (call['id'],)).fetchone()[0], 'timeout_unknown')

    def test_unknown_phase_is_not_retried_without_explicit_recovery(self):
        self.phase('one'); self.phase('two', 'Other facts')
        class Timeout(fixtures.reviewer()):
            def communicate(self, *args, **kwargs):
                raise subprocess.TimeoutExpired('controlled', 5)
        with patch('agent_learning.admin.subprocess.Popen', Timeout):
            self.assertEqual(admin_once(self.r, self.p)['status'], 'timeout_unknown')
        with patch('agent_learning.admin.subprocess.Popen') as launch:
            admin_once(self.r, self.p)
            self.assertEqual(launch.call_count, 0, 'Unknown results require explicit recovery')
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM admin_calls').fetchone()[0], 1)

    def test_changed_comparison_is_replanned_before_paying_for_old_snapshot(self):
        self.phase('one'); self.phase('two', 'Other facts')
        member = self.r.db.execute('SELECT problem_id FROM phase_review_members LIMIT 1').fetchone()[0]
        self.r.db.execute('INSERT INTO admin_case_epochs(project,problem,revision) VALUES(?,?,1) '
                          'ON CONFLICT(project,problem) DO UPDATE SET revision=revision+1', (self.p, member))
        with patch('agent_learning.admin.subprocess.Popen', fixtures.reviewer('skip')):
            result = admin_once(self.r, self.p)
        self.assertEqual(result['status'], 'skip', result)
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM admin_calls').fetchone()[0], 1)

    def test_case_scan_bound_never_claims_unseen_stage_material_fully_covered(self):
        event = self.phase('one')
        task = self.r.task(self.p, 'one')
        for number in range(110):
            self.r._problem(self.p, 'one',
                '阶段公开记录：A sourced project fact [' + format(number, '032x') + '] part 1',
                'mixed', [event['id']], task_binding=task)
        self.phase('two', 'Other independent convention')
        frozen = json.loads(self.r.db.execute('SELECT packet FROM phase_review_batches').fetchone()[0])
        total = self.r.db.execute("SELECT COUNT(*) FROM problems WHERE conversation='one'").fetchone()[0]
        self.assertGreater(total, 100)
        self.assertFalse(frozen['coverage_complete'], 'Bounded case scan must retain an explicit continuation')


if __name__ == '__main__':
    unittest.main()
