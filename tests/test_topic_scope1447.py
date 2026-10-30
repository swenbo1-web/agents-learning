"""A setting name cannot replace a business topic or inline restriction."""
import json
import unittest
from unittest.mock import patch

from agent_learning.knowledge_index import _match_info, normalized_query_plan
from agent_learning.knowledge_topics import topic_text
from tests import test_context_dedup as fixtures


class TopicScopeMatchTests(unittest.TestCase):
    @staticmethod
    def match(query, title, content):
        plan = normalized_query_plan(query)
        weights = {word: 2 if any('\u3400' <= char <= '\u9fff' for char in word) and len(word) > 1
                   else 1 for word in plan['terms']}
        return _match_info(json.dumps(dict(title=title, content=content, applicability='Synthetic only', limitations='reported')),
            weights, sum(weights.values()), plan['meaningful'], _anchors=plan['exact_anchors'],
            _focus=plan['focused_settings'])

    def test_chinese_domain_cannot_be_replaced_by_shared_setting(self):
        samples = (
            ('CacheControl 阴影缓存', 'CacheControl HTTP缓存', '结论：CacheControl 缓存 HTTP 响应，并校验 ETag。'),
            ('RetryPolicy 数据库死锁', 'RetryPolicy 网络恢复', '结论：RetryPolicy 重试网络请求，连接失败后恢复。'),
            ('RetryPolicy 网络连接恢复', 'RetryPolicy 数据库死锁', '结论：RetryPolicy 在数据库死锁后回滚事务并恢复。'),
            ('SessionState 登录会话', 'SessionState 渲染队列', '结论：SessionState 保留渲染队列，失败后恢复帧。'),
        )
        for query, title, content in samples:
            with self.subTest(query=query):
                self.assertFalse(self.match(query, title, content)['auto_eligible'])

    def test_named_setting_still_matches_its_positive_scope(self):
        for query, title, content in (
            ('CacheControl 阴影缓存', 'CacheControl 阴影缓存', '结论：CacheControl 在阴影缓存失效后重建光照贴图。'),
            ('RetryPolicy 数据库死锁', 'RetryPolicy 数据库死锁', '结论：RetryPolicy 在数据库死锁后回滚并恢复事务。'),
            ('CacheControl 光照', 'CacheControl 光照缓存', '结论：CacheControl 用于光照贴图。'),
            ('PowerShell ExecutionPolicy 拒绝脚本执行，先检查签名，不要直接关闭全局安全策略。',
             'PowerShell ExecutionPolicy 脚本签名', '结论：PowerShell ExecutionPolicy 失败时先检查脚本签名。\n限制：不能直接全局关闭策略。'),
        ):
            with self.subTest(query=query):
                self.assertTrue(self.match(query, title, content)['auto_eligible'])

    def test_inline_exclusion_cannot_supply_a_chinese_topic(self):
        for content in (
            '结论：RetryPolicy 安装在固定位置。限制：不能用于诊断数据库死锁。',
            '**结论：** RetryPolicy 安装在固定位置。**限制：** 不能用于诊断数据库死锁。',
            'Conclusion: RetryPolicy is installed at a fixed path. Limitations: not applicable to database deadlock.',
        ):
            with self.subTest(content=content):
                self.assertFalse(self.match('数据库死锁', '安装位置说明', content)['auto_eligible'])

    def test_inline_restriction_projection_retains_the_original_conclusion(self):
        content = '结论：RetryPolicy 保留重试配置。限制：不能用于诊断数据库死锁。'
        projected = topic_text(content)
        self.assertIn('结论：RetryPolicy 保留重试配置。', projected)
        self.assertNotIn('数据库死锁', projected)
        self.assertTrue(self.match('RetryPolicy 重试配置', 'RetryPolicy 配置', content)['auto_eligible'])

    def test_useful_prohibitive_conclusion_and_inline_scope_stay_useful(self):
        content = '结论：LeaseGuard 不能用于跨机器共享租约，应使用外部一致性服务。限制：本结论未经独立验证。'
        self.assertTrue(self.match('LeaseGuard 跨机器共享租约', 'LeaseGuard 使用边界', content)['auto_eligible'])
        self.assertIn('不能用于跨机器共享租约', topic_text(content))

    def test_inline_code_and_fenced_examples_are_not_relabelled(self):
        for content in ('结论：读取 `example. Limitations: untouched` 这个字面量。',
                        '```text\n结论：示例。限制：E_EXAMPLE_42\n```\n结论：只用于格式展示。'):
            with self.subTest(content=content):
                projected = topic_text(content)
                self.assertIn('Limitations: untouched' if 'untouched' in content else 'E_EXAMPLE_42', projected)


class TopicScopeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ContextDedupTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    @staticmethod
    def cards(text, key):
        result=[]
        for line in text.splitlines():
            if '{' not in line: continue
            try: item=json.loads(line[line.index('{'):])
            except ValueError: continue
            if isinstance(item,dict) and key in item: result.append(item)
        return result

    def material(self, lane, title, content):
        f=self.f
        src=f.source(content,capture=lane=='work')
        if lane=='work': return src['memory']['memory_id']
        candidate,_=f.candidate(src,title=title,content=content)
        return f.r.publish(f.p,candidate['candidate_id'])['knowledge_id'] if lane=='formal' else candidate['candidate_id']

    def scoped_lane(self,lane):
        if lane=='candidate': self.f.enable_candidates()
        wrong=self.material(lane,'CacheControl HTTP缓存','结论：CacheControl 缓存 HTTP 响应，并校验 ETag。')
        right=self.material(lane,'CacheControl 阴影缓存','结论：CacheControl 管理阴影缓存，失效时重建光照贴图。')
        key={'formal':'knowledge_id','candidate':'candidate_id','work':'memory_id'}[lane]
        text=self.f.context('CacheControl 阴影缓存')
        ids=[c[key] for c in self.cards(text,key)]
        self.assertIn(right,ids);self.assertNotIn(wrong,ids)
        self.assertLessEqual(len(text),2400)
        self.assertEqual(self.cards(self.f.context('CacheControl 阴影缓存'),key),[])

    def test_formal_scope(self): self.scoped_lane('formal')
    def test_candidate_scope(self): self.scoped_lane('candidate')
    def test_work_scope(self): self.scoped_lane('work')

    def test_semantic_similarity_cannot_revive_scope_mismatch(self):
        wrong=self.material('formal','CacheControl HTTP缓存','结论：CacheControl 缓存 HTTP 响应，并校验 ETag。')
        row=self.f.r.db.execute('SELECT * FROM knowledge WHERE project=? AND id=?',(self.f.p,wrong)).fetchone()
        prepared={'config':{'enabled':False,'automatic_min_similarity':0},'diagnostics':{},'vector':[1,0],
                  'signature':self.f.r.semantic_signature(self.f.p)}
        matches={wrong:{'row':row,'score':1,'rank':1,'input_truncated':False}}
        with patch.object(self.f.r,'_semantic_prepare',return_value=prepared),patch.object(self.f.r,'_semantic_candidates',return_value=matches):
            for _ in range(2):
                self.assertNotIn(wrong,[c['knowledge_id'] for c in self.f.formal_cards(self.f.context('CacheControl 阴影缓存'))])

    def test_inline_restriction_keeps_manual_source_and_grade(self):
        body='结论：RetryPolicy 安装在固定位置。限制：不能用于诊断数据库死锁。'
        wrong=self.material('formal','安装位置说明',body)
        self.assertNotIn(wrong,[c['knowledge_id'] for c in self.f.formal_cards(self.f.context('数据库死锁'))])
        self.assertEqual(self.f.r.read(self.f.p,wrong)['content'],body)
        self.assertEqual(self.f.r.read(self.f.p,wrong)['validation_level'],'reported')


if __name__=='__main__': unittest.main()
