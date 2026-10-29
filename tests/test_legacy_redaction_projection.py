"""Upgraded output/index masking never rewrites authoritative legacy evidence."""
import json
from unittest.mock import patch
import unittest

from agent_learning.runtime import Runtime, clean, encode
from agent_learning.knowledge_index import INDEX_VERSION
from agent_learning.work_memory import WORK_INDEX_VERSION
from tests import test_context_dedup as fixtures
from tests.test_redaction_efficiency import legacy_clean


class LegacyRedactionProjectionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ContextDedupTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.r, self.p = self.f.r, self.f.p

    def legacy(self):
        text = 'dedupfocus retained finding {"api_key": "SYNTHETIC_PRIVATE_MARKER"}.'
        with patch('agent_learning.runtime.clean', side_effect=legacy_clean):
            source = self.f.source(text)
        return source, source['memory']['memory_id']

    def authority(self):
        return {table:[tuple(row) for row in self.r.db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                for table in ('events','work_memories','work_memory_sources','knowledge','knowledge_sources')}

    def test_legacy_search_summary_evidence_and_final_context_are_masked(self):
        _, identity = self.legacy()
        before = self.authority()
        search = self.r.work_search(self.p, 'dedupfocus', workspace=self.f.workspace)
        self.assertTrue(search['items'])
        for section in ('summary','evidence'):
            result = self.r.work_read(self.p, identity, section=section, workspace=self.f.workspace)
            self.assertNotIn('SYNTHETIC_PRIVATE_MARKER', encode(result))
            self.assertIn('[REDACTED]', encode(result))
            self.assertEqual(result['validation_level'], 'reported')
        self.assertNotIn('SYNTHETIC_PRIVATE_MARKER', encode(search))
        context = self.f.context()
        self.assertIn(identity, context)
        self.assertNotIn('SYNTHETIC_PRIVATE_MARKER', context)
        self.assertEqual(before, self.authority())

    def test_only_affected_display_identity_changes_and_does_not_repeat(self):
        source, identity = self.legacy()
        row = self.r.db.execute('SELECT * FROM work_memories WHERE id=?',(identity,)).fetchone()
        with patch('agent_learning.runtime.clean', side_effect=legacy_clean):
            old = self.r._work_item(self.p,row,workspace=self.f.workspace)
        current = self.r._work_item(self.p,row,workspace=self.f.workspace)
        self.assertNotEqual(old['source_version'],current['source_version'])
        self.assertEqual(old['state'],current['state'])
        first = self.f.context()
        second = self.f.context()
        self.assertIn(identity,first)
        self.assertNotIn(identity,second)
        safe = self.f.source('dedupfocus public finding')
        row = self.r.db.execute('SELECT * FROM work_memories WHERE id=?',(safe['memory']['memory_id'],)).fetchone()
        with patch('agent_learning.runtime.clean', side_effect=legacy_clean):
            old = self.r._work_item(self.p,row)
        self.assertEqual(old['source_version'],self.r._work_item(self.p,row)['source_version'])

    def test_mask_before_paging_preserves_complete_reassembly(self):
        text = ('dedupfocus ' * 160 + '{"password":"SYNTHETIC_PRIVATE_MARKER with spaces"} ' + 'public ' * 160)
        with patch('agent_learning.runtime.clean', side_effect=legacy_clean):
            source = self.f.source(text)
        identity = source['memory']['memory_id']
        raw = json.loads(self.r.db.execute('SELECT body FROM work_memories WHERE id=?',(identity,)).fetchone()[0])['summary']
        chunks, offset = [], 0
        for _ in range(30):
            result = self.r.work_read(self.p,identity,offset=offset,max_chars=1024,workspace=self.f.workspace)
            self.assertLessEqual(len(encode(result)),1024)
            self.assertNotIn('SYNTHETIC_PRIVATE_MARKER',encode(result))
            chunks.append(result['content'])
            if result['next_offset'] is None:
                break
            self.assertGreater(result['next_offset'],offset)
            offset=result['next_offset']
        else:
            self.fail('bounded synthetic paging did not finish')
        self.assertEqual(''.join(chunks),clean(raw))

    def test_upgrade_rebuilds_masked_projections_without_rewriting_sources(self):
        source, identity = self.legacy()
        with patch('agent_learning.runtime.clean', side_effect=legacy_clean):
            published, _ = self.f.published(source, content='dedupfocus {"api_key":"SYNTHETIC_PRIVATE_MARKER"}')
        before = self.authority()
        self.r.db.execute('UPDATE knowledge_index_state SET version=?',(INDEX_VERSION-1,))
        self.r.db.execute('UPDATE work_index_state SET version=?',(WORK_INDEX_VERSION-1,))
        self.r.db.execute('DROP INDEX knowledge_recent')
        self.r.close()
        self.r = self.f.r = Runtime(self.f.root/'home', config={})
        self.addCleanup(self.r.close)
        with self.r.transaction():
            self.r._work_repair_index(self.p,limit=16)
        self.assertEqual(before,self.authority())
        for table in ('knowledge_fts','work_memory_fts'):
            indexed=' '.join(row[0] for row in self.r.db.execute('SELECT text FROM '+table))
            self.assertNotIn('synthetic_private_marker',indexed)
        public=self.r.read(self.p,published['knowledge_id'])
        self.assertNotIn('SYNTHETIC_PRIVATE_MARKER',encode(public))
        self.assertTrue(self.r.db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='knowledge_recent'").fetchone())


class LegacyAGDeliveryProjectionTests(unittest.TestCase):
    def test_legacy_ag_post_stop_proof_uses_authority_and_only_displays_masked_text(self):
        from tests.test_ag_delivery_dedup import AntigravityDeliveryDedupTests
        from contextlib import ExitStack
        import sys
        import agent_learning.runtime as runtime

        f = AntigravityDeliveryDedupTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        original_clean = runtime.clean
        # Emulate the old capture policy in the same public AG adapter. Modules
        # imported during capture receive the patched Runtime function too.
        try:
            with ExitStack() as stack:
                for name, module in list(sys.modules.items()):
                    if name.startswith('agent_learning.') and getattr(module, 'clean', None) is original_clean:
                        stack.enter_context(patch.object(module, 'clean', legacy_clean))
                partial, complete = f.prepare('dedupfence: retained result {"api_key":"SYNTHETIC_PRIVATE_MARKER"}')
        finally:
            # Lazy imports during capture must not retain the old fixture policy
            # and contaminate a later test in the same interpreter.
            for name, module in list(sys.modules.items()):
                if name.startswith('agent_learning.') and getattr(module, 'clean', None) is legacy_clean:
                    module.clean = original_clean
        self.assertIn('SYNTHETIC_PRIVATE_MARKER', json.loads(complete['body'])['summary'])
        tables = ('events', 'work_memories', 'work_memory_sources')
        before = {table:[tuple(row) for row in f.r.db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                  for table in tables}
        cards = f.automatic()
        self.assertIn(complete['id'], {card['memory_id'] for card in cards})
        self.assertNotIn(partial['id'], {card['memory_id'] for card in cards})
        self.assertNotIn('SYNTHETIC_PRIVATE_MARKER', encode(cards))
        self.assertIn('[REDACTED]', encode(cards))
        self.assertTrue(all(card['validation_level'] == 'reported' for card in cards))
        self.assertEqual(before, {table:[tuple(row) for row in f.r.db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                                  for table in tables})


class LegacyEvidenceReceiptTests(unittest.TestCase):
    setUp = LegacyRedactionProjectionTests.setUp
    authority = LegacyRedactionProjectionTests.authority

    def test_evidence_only_masking_rejects_old_receipt_without_changing_source_identity(self):
        from agent_learning.work_read_delivery import WorkReadDelivery, _hash
        conversation = self.r.host_conversation('legacy-evidence')
        task = self.r.task_update(self.p, conversation, 'dedupfocus evidence', {'mode':'analysis'})
        payload = {'last_assistant_message':'dedupfocus public explanation',
                   'extra_observation':'{"api_key":"SYNTHETIC_PRIVATE_MARKER"}'}
        with patch('agent_learning.runtime.clean', side_effect=legacy_clean), self.r.transaction():
            event, _ = self.r._event(self.p, conversation, 'Stop', payload,
                task_id=task['id'], task_revision=task['revision'], outcome='reported')
            memory = self.r._work_capture_event(self.p, event['id'], self.f.workspace, raw=payload)
        identity = memory['memory_id']
        before = self.authority()
        row = self.r.db.execute('SELECT * FROM work_memories WHERE id=?', (identity,)).fetchone()
        with patch('agent_learning.runtime.clean', side_effect=legacy_clean):
            old = self.r._work_item(self.p, row, workspace=self.f.workspace)
        self.assertEqual(old['source_version'], self.r._work_item(self.p, row, workspace=self.f.workspace)['source_version'])
        result = self.r.work_read(self.p, identity, section='evidence', workspace=self.f.workspace, max_chars=20000)
        self.assertIn('[REDACTED]', result['content'])
        self.assertNotIn('SYNTHETIC_PRIVATE_MARKER', result['content'])
        self.assertEqual(result['validation_level'], 'reported')
        self.assertTrue(result['file_check']['complete'])
        # Same content length/source metadata isolates the projection-version
        # guard: an older completed receipt alone must not suppress current text.
        scope = {'conversation':'reader', 'context_epoch':0}
        delivery = WorkReadDelivery()
        old_fingerprint = _hash({key:result.get(key) for key in (
            'revision','source_version','state','completion','source_changed','file_check','section_length')})
        delivery.receipts[_hash([scope, identity, 'evidence'])] = (
            old_fingerprint, [(0,result['section_length'])], result['section_length'])
        self.assertIsNone(delivery.completed(result, scope, section='evidence', offset=0, max_chars=20000))
        delivery.project(result, scope)
        self.assertTrue(delivery.completed(result, scope, section='evidence', offset=0, max_chars=20000)['already_read'])
        self.assertEqual(before, self.authority())


if __name__=='__main__':
    unittest.main()
