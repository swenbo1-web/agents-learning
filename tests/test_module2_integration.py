"""模块二真实 SQLite 与 Hook/MCP 链：不启动宿主、工具、模型或外部服务。"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from agent_learning.mcp import Server, TASK_SCOPE, validate
from agent_learning.runtime import ContractError, Runtime, SCHEMA, encode


class Module2IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / 'project'
        self.workspace.mkdir()
        self.r = Runtime(Path(self.temp.name) / 'state')
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.a = self.r.task_update(self.p, 'chat', '分析加载问题', {'mode': 'analysis'})

    def hook(self, kind, **kwargs):
        return self.r.handle_hook(dict(hook_event_name=kind, cwd=str(self.workspace), session_id='chat', **kwargs))

    def child(self, agent='child'):
        self.hook('SubagentStart', agent_id=agent)
        return self.r.db.execute('SELECT handle FROM context_access WHERE agent=?', (agent,)).fetchone()[0]

    def approve(self):
        task = self.r.task(self.p, 'chat')
        return self.r.approve_execution(self.p, 'chat', task['id'], task['write_version'],
            {'mode': 'read_only', 'allowed_tools': ['Read'], 'tool_rules': {'Read': {'action': 'read'}}})

    def test_old_child_feedback_and_recovery_keep_original_assignment(self):
        handle = self.child()
        newer = self.r.task_update(self.p, 'chat', '另一项任务', new_task=True)
        problem = self.r.feedback(self.p, 'chat', '旧任务的发现', context_handle=handle)
        self.assertEqual(problem['task_id'], self.a['id'])
        self.assertNotEqual(problem['task_id'], newer['id'])
        recovered = self.r.session_context(self.p, handle)
        self.assertEqual(recovered['task']['id'], self.a['id'])
        self.assertFalse(recovered['assignment']['current'])
        self.assertEqual(self.r.review_packet(self.p, problem['id'])['task']['goal'], self.a['goal'])

    def test_old_child_scope_revision_is_preserved(self):
        handle = self.child()
        self.r.task_update(self.p, 'chat', self.a['goal'], {'mode': 'implementation'})
        problem = self.r.feedback(self.p, 'chat', '旧范围发现', context_handle=handle)
        self.assertEqual(problem['task_revision'], 1)
        self.assertEqual(self.r.session_context(self.p, handle)['task']['scope']['mode'], 'analysis')

    def test_child_without_parent_task_never_guesses_later_assignment(self):
        handle = self.r.register_context(self.p, 'empty', 'unknown')['context_handle']
        self.r.task_update(self.p, 'empty', '后来才有任务')
        with self.assertRaisesRegex(ContractError, 'assignment_required'):
            self.r.feedback(self.p, 'empty', '不能猜任务', context_handle=handle)

    def test_late_post_with_pre_preserves_original_and_without_pre_stays_unbound(self):
        self.hook('PreToolUse', tool_name='Read', tool_use_id='old')
        self.r.task_update(self.p, 'chat', '新任务', new_task=True)
        self.hook('PostToolUse', tool_name='Read', tool_use_id='old', tool_response={'exit_code': 0})
        self.hook('PostToolUse', tool_name='Read', tool_use_id='orphan', tool_response={'exit_code': 0})
        rows = self.r.db.execute("SELECT task_id FROM events WHERE type='PostToolUse' ORDER BY created").fetchall()
        self.assertEqual(rows[0][0], self.a['id'])
        self.assertIsNone(rows[1][0])

    def test_post_with_known_old_child_stays_old_without_pre(self):
        self.child()
        self.r.task_update(self.p, 'chat', '新任务', new_task=True)
        self.hook('PostToolUse', agent_id='child', tool_name='Read', tool_response={'exit_code': 1})
        self.assertEqual(self.r.problems(self.p)[0]['task_id'], self.a['id'])
        self.hook('PostToolUse', agent_id='never-started', tool_name='Read', tool_response={'exit_code': 1})
        self.assertCountEqual([problem['task_id'] for problem in self.r.problems(self.p)], [self.a['id'], None])

    def test_prompt_fence_is_idempotent_continuation_and_compaction_keep_permit(self):
        self.r.config['enforce_task_policy'] = True
        self.approve()
        self.hook('UserPromptSubmit', turn_id='t1', prompt='继续')
        self.hook('SessionStart', source='compact')
        self.assertTrue(self.r.execution_status(self.p, 'chat')['ready'])
        self.hook('UserPromptSubmit', turn_id='t2', prompt='停止执行，只分析')
        fenced = self.r.task(self.p, 'chat')
        self.hook('UserPromptSubmit', turn_id='t2', prompt='停止执行，只分析')
        self.assertEqual(self.r.task(self.p, 'chat')['write_version'], fenced['write_version'])
        result = self.hook('PreToolUse', tool_name='Read', tool_use_id='blocked')
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('execution_approval_stale', result['hookSpecificOutput']['permissionDecisionReason'])

    def test_interrupt_pauses_and_agent_resume_does_not_restore_approval(self):
        self.approve()
        self.hook('PreToolUse', tool_name='Read', tool_use_id='active')
        self.hook('Interrupt')
        self.assertEqual(self.r.task(self.p, 'chat')['status'], 'paused')
        self.assertEqual(self.r.db.execute('SELECT state FROM operations').fetchone()[0], 'unknown')
        self.r.task_update(self.p, 'chat', self.a['goal'], resume=True)
        self.assertFalse(self.r.execution_status(self.p, 'chat')['ready'])

    def test_old_child_cannot_execute_with_new_parent_permit(self):
        self.child()
        self.r.task_update(self.p, 'chat', '新任务', {'mode': 'analysis'}, new_task=True)
        self.approve()
        self.r.config['enforce_task_policy'] = True
        result = self.hook('PreToolUse', agent_id='child', tool_name='Read')
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('assignment_stale', result['hookSpecificOutput']['permissionDecisionReason'])

    def test_denial_and_observation_remain_separate_and_replay_does_not_erase_conflict(self):
        self.r.config['enforce_task_policy'] = True
        self.hook('PreToolUse', tool_name='Read', tool_use_id='x')
        self.hook('PostToolUse', tool_name='Read', tool_use_id='x', tool_response={'exit_code': 0})
        operation = dict(self.r.db.execute('SELECT * FROM operations').fetchone())
        self.assertEqual(operation['decision'], 'denied')
        self.assertEqual(operation['state'], 'conflict')
        self.assertTrue(operation['conflict'])
        self.hook('PostToolUse', tool_name='Read', tool_use_id='x', tool_response={'exit_code': 0})
        self.assertTrue(self.r.db.execute('SELECT conflict FROM operations').fetchone()[0])
        payload = json.loads(self.r.db.execute("SELECT payload FROM events WHERE type='PreToolUse'").fetchone()[0])
        self.assertEqual(payload['control_decision']['reason'], 'execution_approval_required')

    def test_conflicting_success_cannot_support_tool_success_candidate(self):
        self.hook('PreToolUse', tool_name='Read', tool_use_id='x')
        self.hook('PostToolUse', tool_name='Read', tool_use_id='x', tool_response={'exit_code': 0})
        event = self.r.db.execute("SELECT id FROM events WHERE type='PostToolUse'").fetchone()[0]
        problem = self.r.feedback(self.p, 'chat', '结果', event_refs=[event])
        self.hook('PostToolUse', tool_name='Read', tool_use_id='x', tool_response={'exit_code': 1})
        self.hook('PostToolUse', tool_name='Read', tool_use_id='x', tool_response={'exit_code': 0})
        packet = self.r.review_packet(self.p, problem['id'])
        with self.assertRaisesRegex(ContractError, 'no observed completed tool success'):
            self.r.submit_candidate(self.p, problem_id=problem['id'], problem_revision=packet['problem']['revision'],
                title='结果', content='检查', category='operation', applicability='当前项目', limitations='仅工具结果',
                evidence_refs=[event], validation_level='tool_success')

    def test_replayed_pre_after_delivery_preserves_original_permission_and_success(self):
        self.r.config['enforce_task_policy'] = True
        self.approve()
        self.hook('PreToolUse', tool_name='Read', tool_use_id='x')
        self.hook('PostToolUse', tool_name='Read', tool_use_id='x', tool_response={'exit_code':0})
        old = dict(self.r.db.execute('SELECT * FROM operations').fetchone())
        self.r.task_update(self.p,'chat',self.a['goal'],status='delivered')
        reply = self.hook('PreToolUse', tool_name='Read', tool_use_id='x')
        self.assertEqual(reply['hookSpecificOutput']['permissionDecision'], 'deny')
        current = dict(self.r.db.execute('SELECT * FROM operations').fetchone())
        self.assertEqual((current['decision'],current['state'],current['observation']),
                         (old['decision'],old['state'],old['observation']))

    def test_replayed_pre_cannot_borrow_new_task_permission(self):
        self.r.config['enforce_task_policy'] = True
        self.approve()
        self.hook('PreToolUse', tool_name='Read', tool_use_id='x')
        self.r.task_update(self.p,'chat','新任务',{'mode':'analysis'},new_task=True)
        self.approve()
        reply = self.hook('PreToolUse', tool_name='Read', tool_use_id='x')
        self.assertEqual(reply['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('operation_execution_epoch_stale', reply['hookSpecificOutput']['permissionDecisionReason'])
        operation = self.r.db.execute('SELECT task_id,decision,state FROM operations').fetchone()
        self.assertEqual(tuple(operation), (self.a['id'],'allowed','started'))

    def test_event_time_status_is_stable_after_newer_evidence_exceeds_packet_limit(self):
        problem = self.r.feedback(self.p, 'chat', '同一个问题')
        self.r.task_update(self.p, 'chat', self.a['goal'], status='delivered')
        for _ in range(41):
            self.r.feedback(self.p, 'chat', '同一个问题')
        # 复现系统时钟精度下连续事件同一时点，历史状态必须仍取首个入库事件。
        self.r.db.execute("UPDATE events SET created=1 WHERE project=?", (self.p,))
        self.assertEqual(self.r.review_packet(self.p, problem['id'])['task']['status'], 'active')

    def test_child_stop_cannot_take_parent_delivery_slot(self):
        self.child()
        self.r.task_update(self.p, 'chat', self.a['goal'], status='delivered')
        self.hook('Stop', agent_id='child', last_assistant_message='子任务结果')
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0], 0)
        self.hook('Stop', last_assistant_message='父任务结果')
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0], 1)

    def test_parent_feedback_requires_exact_task_contract(self):
        handle = self.r.register_context(self.p, 'chat')['context_handle']
        server = Server(self.r, self.p, context_handle=handle)
        with self.assertRaisesRegex(ContractError, 'feedback_task_required'):
            server.call('case_feedback', {'summary': '缺少版本'})
        self.r.task_update(self.p, 'chat', '新任务', new_task=True)
        with self.assertRaisesRegex(ContractError, 'stale feedback task identity'):
            server.call('case_feedback', {'summary': '旧反馈', 'expected_task_id': self.a['id'], 'expected_task_revision': 1})
        self.assertNotIn('execution-approve', server.tools)
        self.assertNotIn('approve_execution', server.tools)

    def test_alias_specificity_uses_normalized_path_and_collision_rejects(self):
        deep = self.workspace / 'src'
        self.r.config['project_aliases'] = {str(deep): 'specific',
            str(self.workspace / 'padding' / '..' / 'long-padding' / '..'): 'ancestor'}
        self.assertEqual(self.r.project(deep), 'specific')
        self.r.config['project_aliases'][str(self.workspace)] = 'conflict'
        with self.assertRaisesRegex(ContractError, 'alias collision'):
            self.r.project(deep)

    def test_v3_migration_preserves_evidence_and_marks_missing_event_state_unknown(self):
        legacy_home = Path(self.temp.name) / 'legacy'
        legacy_home.mkdir()
        db = sqlite3.connect(legacy_home / 'runtime.sqlite3')
        db.executescript(SCHEMA)
        db.execute('ALTER TABLE operations ADD COLUMN host_epoch INTEGER NOT NULL DEFAULT 0')
        db.execute('ALTER TABLE admin_calls ADD COLUMN host_epoch INTEGER NOT NULL DEFAULT 0')
        db.execute("ALTER TABLE contexts ADD COLUMN shown TEXT NOT NULL DEFAULT '[]'")
        db.execute("INSERT INTO projects(id) VALUES('legacy')")
        db.execute("INSERT INTO tasks VALUES('a','legacy','legacy-chat',1,'旧目标','{}','delivered',1)")
        db.execute("INSERT INTO task_snapshots VALUES('legacy','a',1,'legacy-chat','旧目标','{}','delivered',1)")
        db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('old-event','legacy','legacy-chat','a',1,None,None,'Feedback','reported',encode({'summary':'旧反馈'}),1))
        db.execute("INSERT INTO problems VALUES('old-problem','legacy','legacy-chat','a',1,1,'旧反馈','unknown','pending',NULL,1)")
        db.execute("INSERT INTO problem_events VALUES('old-problem','old-event')")
        db.execute('PRAGMA user_version=3')
        db.commit()
        db.close()
        upgraded = Runtime(legacy_home)
        try:
            self.assertEqual(upgraded.db.execute('PRAGMA user_version').fetchone()[0], 25)
            self.assertEqual(upgraded.task('legacy','legacy-chat')['write_version'], 1)
            packet = upgraded.review_packet('legacy','old-problem')
            self.assertEqual(packet['events'][0]['id'], 'old-event')
            self.assertEqual(packet['task']['status'], 'unknown_legacy')
        finally:
            upgraded.close()

    def test_cli_approval_and_mcp_scope_contract_use_same_runtime(self):
        rules = Path(self.temp.name) / 'rules.json'
        rules.write_text(encode({'mode':'read_only','allowed_tools':['Read'],
            'tool_rules':{'Read':{'action':'read','path_fields':['path']}},'allowed_paths':['Docs']}), encoding='utf-8')
        env = dict(os.environ, ALP_DISABLED='1')
        env.pop('ALP_CONFIG', None)
        result = subprocess.run([sys.executable,'-B','-m','agent_learning.cli','--home',str(self.r.home),
            '--workspace',str(self.workspace),'execution-approve','--conversation','chat','--expected-task-id',self.a['id'],
            '--expected-write-version',str(self.a['write_version']),'--rules',str(rules)],
            capture_output=True, env=env, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8','replace'))
        self.assertTrue(json.loads(result.stdout)['ready'])
        self.r.config['enforce_task_policy'] = True
        self.assertEqual(self.hook('PreToolUse', tool_use_id='read', tool_name='Read', tool_input={'path':'Docs/a.md'}), {})
        validate({'budget':{'remaining':None},'mode':'analysis'}, TASK_SCOPE)
        for value in (True, -1, float('nan'), '0'):
            with self.assertRaises(ContractError):
                validate({'budget':{'remaining':value}}, TASK_SCOPE)


if __name__ == '__main__':
    unittest.main()
