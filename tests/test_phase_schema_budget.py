"""Compact stage envelopes retain the existing per-case public contract."""
import copy
import json
from pathlib import Path
import sys
import unittest

from agent_learning.admin import RESPONSE_SCHEMA, admin_once
from agent_learning.mcp import validate
from agent_learning.phase_review import response_schema
from agent_learning.runtime import ContractError, encode
from tests import test_phase_review as fixtures


FAKE_REVIEWER = r'''
import json,pathlib,sys
mode=sys.argv[1]
schema_flag='--output-schema' if mode=='codex' else '--json-schema'
schema=json.loads(pathlib.Path(sys.argv[sys.argv.index(schema_flag)+1]).read_text(encoding='utf-8'))
assert schema['properties']['candidate']=={'type':'null'}
assert schema['properties']['proposal']=={'type':'null'}
assert schema['properties']['items']['items']['properties']['candidates']['maxItems']==5
if mode=='codex':
    instruction=sys.stdin.read()
else:
    print(json.dumps({'event':'init','init':{'tools':[],'permission_mode':'request-review','agent':'alp-review'}}),flush=True)
    instruction=json.loads(sys.stdin.readline())['message']['content']
packet=json.loads(instruction.split('<untrusted_evidence>',1)[1].rsplit('</untrusted_evidence>',1)[0])
items=[{'problem_id':item['problem']['id'],'decision':'skip','reason':'Local fake review',
        'candidate':None,'candidates':[],'missing_evidence':[],'proposal':None} for item in packet['items']]
result={'decision':'skip','reason':'Local batch result','candidate':None,'missing_evidence':[],
        'proposal':None,'items':items}
if mode=='codex':
    pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(result),encoding='utf-8')
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':100,'output_tokens':20}}),flush=True)
else:
    print(json.dumps({'event':'result','result':{'status':'SUCCESS','num_turns':1,'structured_output':result,
          'usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120}}}),flush=True)
'''


class PhaseSchemaBudgetTests(unittest.TestCase):
    def test_envelope_removes_only_previously_forbidden_definitions(self):
        original = copy.deepcopy(RESPONSE_SCHEMA)
        compact = response_schema()
        self.assertEqual(compact["properties"]["candidate"], {"type": "null"})
        self.assertEqual(compact["properties"]["proposal"], {"type": "null"})
        item = compact["properties"]["items"]["items"]
        self.assertEqual(item["properties"]["candidate"], original["properties"]["candidate"])
        self.assertEqual(item["properties"]["proposal"], original["properties"]["proposal"])
        self.assertEqual(item["properties"]["candidates"]["maxItems"], 5)
        self.assertEqual(item["properties"]["problem_id"]["minLength"], 1)
        legacy = copy.deepcopy(compact)
        for key in ("candidate", "proposal"):
            legacy["properties"][key] = original["properties"][key]
        self.assertGreater(len(encode(legacy)) - len(encode(compact)), 1500)
        self.assertEqual(RESPONSE_SCHEMA, original, "stage schema construction must not change other review modes")

    def test_aggregate_null_is_enforced_by_local_validation(self):
        body = {"decision": "skip", "reason": "Envelope", "candidate": None, "proposal": None,
                "missing_evidence": [], "items": [{"problem_id": "one", "decision": "skip", "reason": "No new knowledge",
                    "candidate": None, "candidates": [], "missing_evidence": [], "proposal": None}]}
        validate(body, response_schema())
        for field in ("candidate", "proposal"):
            with self.subTest(field=field):
                changed = copy.deepcopy(body)
                changed[field] = {}
                with self.assertRaises(ContractError):
                    validate(changed, response_schema())


class PhaseSchemaHostTests(unittest.TestCase):
    setUp = fixtures.PhaseReviewTests.setUp
    phase = fixtures.PhaseReviewTests.phase

    def run_host(self, provider):
        fake = Path(self.temp.name) / "fake-phase-reviewer.py"
        fake.write_text(FAKE_REVIEWER, encoding="utf-8")
        command = [sys.executable, "-B", str(fake), provider]
        self.r.config["admin"].update(provider=provider, command=command,
            antigravity_command=command, dsh_command=command)
        self.phase("one", "First sourced convention")
        self.phase("two", "Second independent sourced convention")
        result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "skip", result)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["usage"]["completeness"], "complete")
        calls = self.r.db.execute("SELECT state FROM admin_calls").fetchall()
        self.assertEqual([row[0] for row in calls], ["skip"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)

    def test_codex_fake_subprocess_uses_compact_schema(self):
        self.run_host("codex")

    def test_antigravity_fake_subprocess_uses_compact_schema(self):
        self.run_host("antigravity")

    def test_dsh_fake_subprocess_uses_compact_schema(self):
        self.run_host("dsh")


if __name__ == "__main__":
    unittest.main()
