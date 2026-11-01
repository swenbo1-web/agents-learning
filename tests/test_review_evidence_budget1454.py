"""Optional review references never displace original evidence or admission."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from agent_learning.admin import _canonical_candidate_evidence, _fingerprint, admin_once
from agent_learning.admin_decisions import dependency_signature
from agent_learning.admin_usage import estimate_reservation
from agent_learning.phase_review import configure, instruction, policy
from agent_learning.review_evidence import (MODE_FIELD, REFERENCE_PREFIX, aliases, canonical_refs,
    has_short_refs, prefer_short_refs, project_packet, raw_packet, source_packet)
from agent_learning.review_packet import _fits, review_instruction
from tests import test_admin_evidence_refs1449 as admin_fixture
from tests import test_history_context as history_fixture
from tests import test_module5_packet as packet_fixture
from tests import test_phase_review as phase_fixture
from tests.test_review_transport import batch, case


class EvidenceBudgetProjectionTests(unittest.TestCase):
    def test_fits_is_pure_even_when_only_raw_representation_could_fit(self):
        packet = case()
        before = deepcopy(packet)
        limit = len(review_instruction(raw_packet(packet)))
        self.assertFalse(_fits(packet, limit))
        self.assertEqual(packet, before)

    def test_raw_wire_preserves_sources_but_cannot_accept_unshown_aliases(self):
        packet = case()
        packet['events'][0]['payload'][MODE_FIELD] = 'business-value'
        raw = raw_packet(packet)
        visible = project_packet(raw)
        self.assertEqual(visible, packet)
        self.assertNotIn(MODE_FIELD, visible)
        self.assertEqual(visible['events'][0]['payload'][MODE_FIELD], 'business-value')
        short = next(iter(aliases(packet)))
        self.assertEqual(canonical_refs(raw, [short]), [short])
        body = {'evidence_refs': [short]}
        self.assertEqual(_canonical_candidate_evidence(raw, body), body)
        self.assertNotIn(REFERENCE_PREFIX, review_instruction(raw))
        # Re-render the frozen policy, never reconstruct it from a wire preview.
        self.assertEqual(project_packet(raw), visible)

    def test_finalization_preserves_all_evidence_and_semantic_signatures(self):
        packet = batch()
        before = deepcopy(packet)
        raw = raw_packet(packet)
        limit = len(review_instruction(raw))
        result = prefer_short_refs(raw, lambda value: _fits(value, limit))
        self.assertEqual(packet, before)
        self.assertEqual(source_packet(result), packet)
        self.assertTrue(all(item[MODE_FIELD] == 'raw' for item in result['items']))
        self.assertFalse(has_short_refs(project_packet(result)))
        self.assertEqual(_fingerprint(result), _fingerprint(packet))
        self.assertEqual(dependency_signature(result), dependency_signature(packet))
        self.assertLessEqual(len(review_instruction(result)), limit)
        short = prefer_short_refs(raw, lambda value: _fits(value, 200000))
        self.assertTrue(has_short_refs(project_packet(short)))
        self.assertEqual(source_packet(short), packet)

    def test_empty_task_or_target_pages_do_not_pay_for_reference_guidance(self):
        packet = dict(case(), events=[])
        self.assertNotIn(REFERENCE_PREFIX, review_instruction(packet))
        self.assertNotIn('evidence_ref', project_packet(packet))


class OriginalPacketCapacityTests(unittest.TestCase):
    setUp = packet_fixture.Module5PacketTests.setUp
    tearDown = packet_fixture.Module5PacketTests.tearDown
    configure = packet_fixture.Module5PacketTests.configure
    problem = packet_fixture.Module5PacketTests.problem

    def test_full_41_events_freeze_raw_policy_when_aliases_would_omit_a_source(self):
        self.configure(16000)
        for number in range(41):
            problem = self.problem(resolution='Observation %d' % number)
        packet = self.r.review_packet(self.p, problem['id'])
        self.assertEqual(len(packet['events']), 41)
        self.assertFalse(packet['incomplete'])
        self.assertEqual(packet[MODE_FIELD], 'raw')
        self.assertLessEqual(len(review_instruction(packet)), 16000)
        self.assertGreater(len(review_instruction(source_packet(packet))), 16000)
        self.assertFalse(has_short_refs(project_packet(packet)))
        self.assertEqual(admin_once(self.r, self.p, problem['id'])['status'], 'candidate')

    def test_small_packet_keeps_aliases_when_the_whole_evidence_set_fits(self):
        self.configure(16000)
        problem = self.problem()
        packet = self.r.review_packet(self.p, problem['id'])
        self.assertFalse(packet['incomplete'])
        self.assertNotEqual(packet.get(MODE_FIELD), 'raw')
        self.assertTrue(has_short_refs(project_packet(packet)))


class PhaseReferenceBudgetTests(unittest.TestCase):
    setUp = phase_fixture.PhaseReviewTests.setUp
    phase = phase_fixture.PhaseReviewTests.phase

    def test_outer_decoration_cannot_displace_a_whole_case(self):
        original = instruction
        def with_large_optional_decoration(packet):
            text = original(packet)
            return text + ('x' * 16000 if has_short_refs(project_packet(packet)) else '')
        with patch('agent_learning.phase_review.instruction', side_effect=with_large_optional_decoration):
            self.phase('first')
            self.phase('second', 'Separate sourced architecture convention')
            row = self.r.db.execute("SELECT packet FROM phase_review_batches WHERE state='pending'").fetchone()
            self.assertIsNotNone(row)
            frozen = json.loads(row[0])
            self.assertEqual(len(frozen['phase_ids']), 2)
            self.assertTrue(frozen['items'])
            self.assertTrue(all(item[MODE_FIELD] == 'raw' for item in frozen['items']))
            self.assertFalse(has_short_refs(project_packet(frozen)))
            with patch('agent_learning.admin.subprocess.Popen', phase_fixture.reviewer('skip')):
                result = admin_once(self.r, self.p)
        self.assertEqual(result['status'], 'skip', result)
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM admin_calls').fetchone()[0], 1)

    def test_actual_phase_token_cap_selects_raw_without_removing_evidence(self):
        packet = raw_packet(batch())
        short = source_packet(packet)
        before = estimate_reservation(self.r, self.p, instruction(packet), phase=True)
        decorated = estimate_reservation(self.r, self.p, instruction(short), phase=True)
        self.assertGreater(decorated['estimated_input_tokens'], before['estimated_input_tokens'])
        configure(self.r, self.p, {'max_input_tokens': before['estimated_input_tokens']},
                  policy(self.r, self.p)['version'])
        result = prefer_short_refs(packet, lambda value: len(instruction(value)) <= 200000
            and estimate_reservation(self.r, self.p, instruction(value), phase=True)['admitted'])
        self.assertFalse(has_short_refs(project_packet(result)))
        self.assertEqual(source_packet(result), source_packet(packet))
        self.assertTrue(estimate_reservation(self.r, self.p, instruction(result), phase=True)['admitted'])


class HistoryReferenceBudgetTests(unittest.TestCase):
    setUp = history_fixture.HistoryContextTests.setUp
    activate = history_fixture.HistoryContextTests.activate
    materialize = history_fixture.HistoryContextTests.materialize
    case_for = history_fixture.HistoryContextTests.case_for
    enriched = history_fixture.HistoryContextTests.enriched
    related_refs = staticmethod(history_fixture.HistoryContextTests.related_refs)

    def test_related_sources_take_priority_over_optional_alias_fields(self):
        complete = self.enriched()
        raw = raw_packet(complete)
        limit = len(review_instruction(raw))
        self.assertGreater(len(review_instruction(complete)), limit)
        result = self.enriched(max_chars=limit)
        self.assertEqual(result['events'], complete['events'])
        self.assertEqual(self.related_refs(result), self.related_refs(complete))
        self.assertEqual(result[MODE_FIELD], 'raw')
        self.assertFalse(has_short_refs(project_packet(result)))
        self.assertLessEqual(len(review_instruction(result)), limit)


class OrdinaryFixedBudgetTests(unittest.TestCase):
    setUp = admin_fixture.AdminEvidenceRefTests.setUp

    def test_short_refs_do_not_bypass_legacy_fixed_reservation_limit(self):
        self.r.config['admin']['max_reported_tokens_per_day'] = 3999
        self.r.config['admin']['token_reservation_per_call'] = 4000
        packet = self.r.review_packet(self.p, self.problem['id'])
        self.assertTrue(has_short_refs(project_packet(packet)))
        short = estimate_reservation(self.r, self.p, review_instruction(packet))
        raw = estimate_reservation(self.r, self.p, review_instruction(raw_packet(packet)))
        self.assertEqual(short, raw)
        self.assertEqual(short['reserved_tokens'], 4000)
        with patch('agent_learning.admin.subprocess.Popen') as launch:
            result = admin_once(self.r, self.p, self.problem['id'])
        launch.assert_not_called()
        self.assertEqual(result['status'], 'token_budget_exhausted', result)
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM admin_calls').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
