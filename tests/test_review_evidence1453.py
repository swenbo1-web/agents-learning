"""Short review references preserve exact, frozen per-case source boundaries."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from agent_learning.admin import ResponseRejected, _canonical_candidate_evidence, admin_once
from agent_learning.admin_response_store import status
from agent_learning.review_evidence import aliases, canonical_refs, project_packet, REFERENCE_PREFIX
from agent_learning.review_packet import review_instruction
from agent_learning.runtime import encode
from tests import test_admin_evidence_refs1449 as fixtures
from tests import test_admin_response_store as recovery
from tests import test_history_candidates as history
from tests import test_phase_review as phase
from tests.test_review_transport import case


class ReviewEvidenceProjectionTests(unittest.TestCase):
    def test_projecting_twice_is_idempotent_and_cannot_trust_injected_alias_fields(self):
        packet = case()
        visible = project_packet(packet)
        self.assertEqual(project_packet(visible), visible)
        self.assertEqual(aliases(visible), aliases(packet))
        forged = deepcopy(packet)
        forged['events'][0]['evidence_ref'] = 'ev-forged-1'
        self.assertEqual(project_packet(forged), visible)

    def test_projection_keeps_raw_sources_and_business_payloads_untouched(self):
        packet = case()
        packet['events'][0]['payload']['literal'] = {'evidence_ref': 'ev-forged-1', 'id': 'business-id'}
        original = deepcopy(packet)
        visible = project_packet(packet)
        self.assertEqual(packet, original)
        for raw, shown in zip(packet['events'], visible['events']):
            self.assertEqual(shown['id'], raw['id'])
            self.assertEqual(shown['payload'], raw['payload'])
            self.assertEqual(canonical_refs(packet, [shown['evidence_ref']]), [raw['id']])
            self.assertLess(len(shown['evidence_ref']), 24)

    def test_aliases_are_deterministic_but_sibling_and_changed_sources_are_distinct(self):
        packet = case()
        self.assertEqual(aliases(packet), aliases(json.loads(encode(packet))))
        old = next(iter(aliases(packet)))
        for mutate in (
            lambda p: p['problem'].update(id='other-case'),
            lambda p: p['problem'].update(revision=4),
            lambda p: p['events'].pop(),
            lambda p: p['events'][0]['payload'].update(extra='changed-source'),
        ):
            changed = deepcopy(packet)
            mutate(changed)
            self.assertNotIn(old, aliases(changed))
            self.assertEqual(canonical_refs(changed, [old]), [old])

    def test_unknown_truncated_and_qualified_aliases_are_not_guessed(self):
        packet = case()
        alias = next(iter(aliases(packet)))
        for value in (alias[:-1], alias + '0', 'EV' + alias[2:], 'wrong:' + alias):
            self.assertEqual(canonical_refs(packet, [value]), [value])
        raw = packet['events'][0]['id']
        body = {'evidence_refs': [raw, f"{packet['problem']['id']}@3:{raw}"]}
        self.assertEqual(_canonical_candidate_evidence(packet, body)['evidence_refs'], [raw, raw])

    def test_only_explicit_history_source_slots_receive_aliases(self):
        packet = case()
        packet['related_history'] = [{'events': [{'id': 'history-source', 'payload': {'id': 'business'}}]}]
        packet['related_knowledge'] = [{'events': [{'id': 'knowledge-source'}]}]
        self.assertNotIn('evidence_ref', project_packet(packet)['related_history'][0]['events'][0])
        packet['purpose'] = 'history_review'
        shown = project_packet(packet)
        related = shown['related_history'][0]['events'][0]
        self.assertEqual(canonical_refs(packet, [related['evidence_ref']]), ['history-source'])
        self.assertNotIn('evidence_ref', shown['related_knowledge'][0]['events'][0])

    def test_phase_projection_uses_child_catalogs_and_never_sibling_sources(self):
        left, right = case(1), case(2)
        packet = dict(case(99), purpose='phase_review', items=[left, right])
        visible = project_packet(packet)
        for original, shown in zip(packet['items'], visible['items']):
            self.assertEqual(aliases(original), {event['evidence_ref']: event['id'] for event in shown['events']})
        foreign = visible['items'][0]['events'][0]['evidence_ref']
        self.assertEqual(canonical_refs(right, [foreign]), [foreign])
        self.assertNotIn('evidence_ref', visible['events'][0])
        reordered = project_packet(dict(packet, items=list(reversed(packet['items']))))
        self.assertEqual(reordered['items'], list(reversed(visible['items'])))

    def test_prompt_counts_new_projection_and_guidance_in_its_visible_length(self):
        packet = case()
        text = review_instruction(packet)
        self.assertIn(REFERENCE_PREFIX, text)
        visible = json.loads(text.split('<untrusted_evidence>', 1)[1].rsplit('</untrusted_evidence>', 1)[0])
        self.assertEqual(visible, project_packet(packet))
        self.assertEqual(packet, case())

    def test_alias_fields_do_not_change_evidence_or_dependency_signatures(self):
        from agent_learning.admin import _fingerprint
        from agent_learning.admin_decisions import dependency_signature
        packet = case()
        packet.update(purpose='history_review', related_history=[{
            'problem_id': 'related', 'events': [dict(case(2)['events'][0])]}])
        projected = project_packet(packet)
        self.assertEqual(_fingerprint(packet), _fingerprint(projected))
        self.assertEqual(dependency_signature(packet), dependency_signature(projected))
        projected['related_history'][0]['events'][0]['payload']['evidence_ref'] = 'real business value'
        self.assertNotEqual(_fingerprint(packet), _fingerprint(projected))

    def test_phase_projection_has_identical_signatures_and_revision_bound_aliases(self):
        from agent_learning.admin import _fingerprint
        from agent_learning.admin_decisions import dependency_signature
        packet = dict(case(99), purpose='phase_review', items=[case(1), case(2)])
        projected = project_packet(packet)
        self.assertEqual(_fingerprint(packet), _fingerprint(projected))
        self.assertEqual(dependency_signature(packet), dependency_signature(projected))
        changed = deepcopy(packet)
        changed['items'][0]['problem']['revision'] += 1
        old_alias = projected['items'][0]['events'][0]['evidence_ref']
        self.assertEqual(canonical_refs(changed['items'][0], [old_alias]), [old_alias])


class ReviewEvidenceAcceptanceTests(unittest.TestCase):
    setUp = fixtures.AdminEvidenceRefTests.setUp
    run_review = fixtures.AdminEvidenceRefTests.run_review

    def test_short_refs_accept_with_original_reply_and_raw_candidate_sources(self):
        def use_alias(packet, body):
            body['evidence_refs'] = [event['evidence_ref'] for event in packet['events']]
        result, seen = self.run_review(use_alias)
        self.assertEqual(result['status'], 'candidate', result)
        stored = json.loads(self.r.db.execute('SELECT body FROM candidates').fetchone()[0])
        self.assertEqual(stored['evidence_refs'], sorted(event['id'] for event in seen['packet']['events']))
        archive = json.loads(self.r.db.execute('SELECT response FROM admin_response_records').fetchone()[0])
        self.assertEqual(archive, seen['response'])
        self.assertTrue(all(ref.startswith('ev-') for ref in archive['candidate']['evidence_refs']))

    def test_raw_and_short_refs_cannot_manufacture_additional_sources(self):
        def mixed(packet, body):
            body['evidence_refs'] += [event['evidence_ref'] for event in packet['events']]
        result, seen = self.run_review(mixed)
        self.assertEqual(result['status'], 'candidate', result)
        stored = json.loads(self.r.db.execute('SELECT body FROM candidates').fetchone()[0])
        self.assertEqual(stored['evidence_refs'], sorted({event['id'] for event in seen['packet']['events']}))

    def test_bad_short_ref_retains_specific_failure_without_retry_or_writes(self):
        result, _ = self.run_review(lambda packet, body: body.update(
            evidence_refs=[packet['events'][0]['evidence_ref'] + 'invalid']))
        self.assertEqual(result['status'], 'failed', result)
        rejection = status(self.r, self.p, result['call_id'])['rejection']
        self.assertEqual(rejection, {'code': 'candidate_evidence_outside_packet',
                                    'path': 'response.candidate.evidence_refs'})
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0], 0)
        self.assertEqual(self.r.db.execute('SELECT retry_allowed FROM admin_calls').fetchone()[0], 0)
        with patch('agent_learning.admin.subprocess.Popen') as launch, patch(
                'agent_learning.admin_decisions.PACKET_FORMAT', 999):
            admin_once(self.r, self.p, self.problem['id'])
        launch.assert_not_called()

    def test_short_ref_cannot_upgrade_reported_to_tool_success(self):
        def invalid(packet, body):
            body.update(evidence_refs=[event['evidence_ref'] for event in packet['events']], validation_level='tool_success')
        result, _ = self.run_review(invalid)
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0], 0)


class PhaseReviewEvidenceTests(unittest.TestCase):
    setUp = fixtures.PhaseEvidenceRefTests.setUp
    phase = fixtures.PhaseEvidenceRefTests.phase
    run_review = fixtures.PhaseEvidenceRefTests.run_review

    def test_phase_short_refs_remain_local_and_canonical(self):
        def use_alias(packet, response):
            cases = {item['problem']['id']: item for item in packet['items']}
            for item in response['items']:
                for candidate in item['candidates']:
                    candidate['evidence_refs'] = [cases[item['problem_id']]['events'][0]['evidence_ref']]
        result = self.run_review(use_alias)
        self.assertEqual(result['status'], 'candidate', result)
        for row in self.r.db.execute('SELECT body FROM candidates'):
            self.assertTrue(all(not ref.startswith('ev-') for ref in json.loads(row[0])['evidence_refs']))

    def test_sibling_alias_rejects_all_outputs_with_precise_item_and_candidate_path(self):
        def wrong(packet, response):
            response['items'][1]['candidates'][1]['evidence_refs'] = [packet['items'][0]['events'][0]['evidence_ref']]
        result = self.run_review(wrong)
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(status(self.r, self.p, result['call_id'])['rejection'], {
            'code': 'candidate_evidence_outside_packet', 'path': 'response.items[1].candidates[1].evidence_refs'})
        for table in ('candidates', 'governance_proposals', 'review_receipts'):
            self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)


class HistoryReviewEvidenceTests(unittest.TestCase):
    setUp = history.HistoryCandidateTests.setUp
    run_history = history.HistoryCandidateTests.run_history

    def test_explicit_history_can_use_primary_and_visible_related_aliases(self):
        self.r.config['admin']['max_input_chars'] = 20000
        docs = self.workspace / 'Docs'
        docs.mkdir()
        (docs / 'architecture.md').write_text('RenderingPipeline ShaderCache project architecture requires an explicit journal.', encoding='utf-8')
        observed = []
        def transform(packet, response):
            observed.append(deepcopy(packet))
            response['candidate']['evidence_refs'] = [packet['events'][0]['evidence_ref']]
            response['candidate']['evidence_refs'] += [event['evidence_ref']
                for related in packet.get('related_history', []) for event in related['events']]
            return response
        _, result, calls = self.run_history(transform)
        self.assertEqual(calls, 1)
        self.assertTrue(observed[0].get('related_history'))
        self.assertEqual(result['counts']['candidates'], 1, result)
        stored = json.loads(self.r.db.execute('SELECT body FROM candidates').fetchone()[0])
        self.assertTrue(all(not ref.startswith('ev-') for ref in stored['evidence_refs']))
        self.assertGreater(len(stored['evidence_refs']), 1)

    def test_history_alias_does_not_remove_primary_source_requirement(self):
        self.r.config['admin']['max_input_chars'] = 20000
        docs = self.workspace / 'Docs'
        docs.mkdir()
        (docs / 'architecture.md').write_text('RenderingPipeline ShaderCache project architecture with related constraints.', encoding='utf-8')
        def related_only(packet, response):
            response['candidate']['evidence_refs'] = [event['evidence_ref']
                for related in packet.get('related_history', []) for event in related['events']]
            self.assertTrue(response['candidate']['evidence_refs'])
            return response
        _, result, calls = self.run_history(related_only)
        self.assertEqual(calls, 1)
        self.assertEqual(result['counts']['candidates'], 0, result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM problem_events WHERE role='history_related'").fetchone()[0], 0)


class EvidenceLocalRecoveryTests(unittest.TestCase):
    setUp = recovery.AdminResponseStoreTests.setUp
    phase = recovery.AdminResponseStoreTests.phase
    recover = recovery.AdminResponseStoreTests.recover

    def test_explicit_recheck_preserves_alias_reply_and_paid_call_ledger(self):
        self.phase('first', 'First stage convention')
        self.phase('second', 'Separate architecture fact')
        def use_alias(packet, response):
            cases = {item['problem']['id']: item for item in packet['items']}
            for item in response['items']:
                for candidate in item['candidates']:
                    candidate['evidence_refs'] = [cases[item['problem_id']]['events'][0]['evidence_ref']]
        reviewer = fixtures.transform_reviewer(phase.reviewer(), use_alias)
        with patch('agent_learning.admin.subprocess.Popen', reviewer), patch(
                'agent_learning.admin._accept_response', side_effect=ResponseRejected('candidate_batch_ambiguous', 'response.candidates')):
            result = admin_once(self.r, self.p)
        call = result['call_id']
        before = dict(self.r.db.execute('SELECT * FROM admin_calls WHERE id=?', (call,)).fetchone())
        archive = dict(self.r.db.execute('SELECT * FROM admin_response_records WHERE call_id=?', (call,)).fetchone())
        with patch('agent_learning.admin.subprocess.Popen') as launch:
            receipt = self.recover(call)
        launch.assert_not_called()
        self.assertEqual(receipt['status'], 'accepted', receipt)
        after = self.r.db.execute('SELECT * FROM admin_response_records WHERE call_id=?', (call,)).fetchone()
        for key in ('response', 'response_digest', 'reserved', 'reserved_digest'):
            self.assertEqual(after[key], archive[key])
        self.assertEqual(dict(self.r.db.execute('SELECT * FROM admin_calls WHERE id=?', (call,)).fetchone()), before)


if __name__ == '__main__':
    unittest.main()
