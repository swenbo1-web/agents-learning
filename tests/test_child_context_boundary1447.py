"""Fixed child assignments keep delivery coverage across parent-only changes."""
import json
import unittest
import uuid

from agent_learning.runtime import ContractError
from tests import test_context_dedup as fixtures


class ChildContextBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ContextDedupTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.r, self.p = self.f.r, self.f.p
        self.parent = self.f.conversation
        self.r.config['work_memory']['automatic_context'] = False
        self.knowledge, _ = self.f.published()
        self.child = 'fixed-child'
        self.handle = self.r.register_context(self.p, self.parent, self.child)['context_handle']

    def context(self, **extra):
        return self.r._context(self.p, self.parent, query='dedupfocus', agent_id=self.child,
                               context_handle=self.handle, workspace=self.f.workspace, **extra)

    def child_state(self):
        return dict(self.r.db.execute('SELECT * FROM subagent_contexts WHERE project=? AND conversation=? AND agent=?',
            (self.p, self.parent, self.child)).fetchone())

    def prepare_covered_child(self):
        self.assertIn(self.knowledge['knowledge_id'], self.context())
        self.assertEqual(self.f.formal_cards(self.context()), [])
        return self.child_state()

    def test_new_parent_task_preserves_fixed_child_coverage_and_snapshot(self):
        before = self.prepare_covered_child()
        assignment = self.r.assignment_context(self.p, self.parent, self.child, self.handle)
        newer = self.r.task_update(self.p, self.parent, 'independent parent stage', {'mode': 'analysis'}, new_task=True)
        self.assertNotEqual(newer['id'], assignment['task_id'])
        self.assertEqual(self.f.formal_cards(self.context()), [])
        after = self.child_state()
        self.assertEqual(after['epoch'], before['epoch'])
        self.assertEqual(after['injected'], before['injected'])
        recovered = self.r.session_context(self.p, self.handle)
        self.assertEqual(recovered['assignment']['assignment_id'], assignment['assignment_id'])
        self.assertEqual(recovered['task']['id'], assignment['task_id'])
        self.assertFalse(recovered['assignment']['current'])
        # The parent still needs fresh delivery for its own new task.
        self.assertTrue(self.f.formal_cards(self.f.context()))

    def test_parent_goal_and_scope_revision_do_not_reset_fixed_child(self):
        before = self.prepare_covered_child()
        original = self.r.session_context(self.p, self.handle)['task']
        self.r.task_update(self.p, self.parent, 'parent revised goal', {'mode': 'implementation'})
        self.assertEqual(self.f.formal_cards(self.context()), [])
        self.assertEqual(self.child_state()['epoch'], before['epoch'])
        child_task = self.r.session_context(self.p, self.handle)['task']
        self.assertEqual((child_task['id'], child_task['revision'], child_task['scope']),
                         (original['id'], original['revision'], original['scope']))

    def test_missing_assignment_context_remains_conservatively_invalidated(self):
        self.prepare_covered_child()
        with self.r.transaction():
            self.r.db.execute('DELETE FROM assignments WHERE context_handle=?', (self.handle,))
        before = self.child_state()
        self.r.task_update(self.p, self.parent, 'new parent contract', new_task=True)
        after = self.child_state()
        self.assertEqual(after['epoch'], before['epoch'] + 1)
        self.assertEqual(json.loads(after['injected']), [])
        self.assertEqual(after['last_query'], '')

    def test_foreign_host_context_with_same_epoch_cannot_preserve_local_coverage(self):
        self.prepare_covered_child()
        before = self.child_state()
        # Host identity lives in the conversation namespace, not a separate
        # context_access.host_scope column. A malformed/legacy cross-host link
        # must not become a valid local assignment just because epochs match.
        self.r.db.execute('UPDATE context_access SET conversation=? WHERE handle=?',
                          ('dsh:default:' + self.parent, self.handle))
        self.r.task_update(self.p, self.parent, 'new local parent contract', new_task=True)
        after = self.child_state()
        self.assertEqual(after['epoch'], before['epoch'] + 1)
        self.assertEqual(json.loads(after['injected']), [])
        with self.assertRaisesRegex(ContractError, 'context_mismatch'):
            self.context()

    def test_real_child_compaction_still_restores_after_parent_switch(self):
        self.prepare_covered_child()
        self.r.task_update(self.p, self.parent, 'new parent contract', new_task=True)
        before = self.child_state()['epoch']
        restored = self.f.formal_cards(self.context(compact=True))
        self.assertEqual([card['knowledge_id'] for card in restored], [self.knowledge['knowledge_id']])
        self.assertEqual(self.child_state()['epoch'], before + 1)
        self.assertEqual(self.f.formal_cards(self.context()), [])

    def test_child_without_dispatched_task_does_not_gain_later_parent_assignment(self):
        conversation = 'initially-empty-parent'
        handle = self.r.register_context(self.p, conversation, 'unassigned-child')['context_handle']
        self.r._context(self.p, conversation, query='dedupfocus', agent_id='unassigned-child',
                        context_handle=handle, workspace=self.f.workspace)
        before = self.r.db.execute('SELECT epoch FROM subagent_contexts WHERE project=? AND conversation=? AND agent=?',
            (self.p, conversation, 'unassigned-child')).fetchone()[0]
        self.r.task_update(self.p, conversation, 'later parent task', {'mode':'analysis'})
        after = self.r.db.execute('SELECT epoch,injected FROM subagent_contexts WHERE project=? AND conversation=? AND agent=?',
            (self.p, conversation, 'unassigned-child')).fetchone()
        self.assertEqual(after['epoch'], before + 1)
        self.assertEqual(json.loads(after['injected']), [])
        self.assertIsNone(self.r.session_context(self.p, handle)['task'])

    def test_new_assignment_identity_does_not_borrow_child_coverage(self):
        self.prepare_covered_child()
        original = self.r.assignment_context(self.p, self.parent, self.child, self.handle)
        self.r.task_update(self.p, self.parent, 'new parent contract', new_task=True)
        # Model a trusted host re-assignment using the same context owner. The
        # assignment ID is the independent cache boundary, not parent epoch.
        replacement_id = str(uuid.uuid4())
        self.r.db.execute('UPDATE assignments SET id=? WHERE context_handle=?', (replacement_id, self.handle))
        replacement = self.r.assignment_context(self.p, self.parent, self.child, self.handle)
        self.assertNotEqual(original['assignment_id'], replacement['assignment_id'])
        self.assertEqual([c['knowledge_id'] for c in self.f.formal_cards(self.context())], [self.knowledge['knowledge_id']])
        self.assertEqual(self.f.formal_cards(self.context()), [])

    def test_child_revocation_notice_survives_parent_only_change(self):
        self.prepare_covered_child()
        self.r.task_update(self.p, self.parent, 'new parent contract', new_task=True)
        self.r.revoke(self.p, self.knowledge['knowledge_id'], expected_revision=1)
        text = self.context()
        self.assertIn(self.knowledge['knowledge_id'], text)
        self.assertIn('已撤销', text)
        self.assertEqual(self.f.formal_cards(text), [])
        self.assertNotIn('已撤销', self.context())

    def test_host_epoch_fences_handle_and_new_child_context_receives_knowledge(self):
        self.prepare_covered_child()
        old = self.handle
        self.r.scope_account_changed('synthetic-account-switch')
        with self.assertRaisesRegex(ContractError, 'reconnect_required'):
            self.context()
        self.handle = self.r.register_context(self.p, self.parent, self.child)['context_handle']
        self.assertNotEqual(self.handle, old)
        self.assertTrue(self.f.formal_cards(self.context()))
        self.assertEqual(self.f.formal_cards(self.context()), [])

    def test_preserved_child_coverage_does_not_restore_execution_permission(self):
        self.prepare_covered_child()
        self.r.task_update(self.p, self.parent, 'new parent contract', {'mode': 'analysis'}, new_task=True)
        task = self.r.task(self.p, self.parent)
        self.r.approve_execution(self.p, self.parent, task['id'], task['write_version'],
            {'mode': 'read_only', 'allowed_tools': ['Read'], 'tool_rules': {'Read': {'action': 'read'}}})
        self.r.config['enforce_task_policy'] = True
        result = self.r.handle_hook({'hook_event_name':'PreToolUse', 'cwd':str(self.f.workspace),
            'session_id':self.parent, 'agent_id':self.child, 'tool_name':'Read'})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('assignment_stale', result['hookSpecificOutput']['permissionDecisionReason'])


if __name__ == '__main__':
    unittest.main()
