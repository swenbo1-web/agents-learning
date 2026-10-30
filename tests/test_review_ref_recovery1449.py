"""Qualified evidence recovery preserves the public reply and paid-call ledger."""
import json
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_response_store import status
from tests import test_admin_response_store as fixtures


def qualify(body):
    for item in body["items"]:
        for candidate in item["candidates"]:
            prefix = f'{candidate["problem_id"]}@{candidate["problem_revision"]}:'
            candidate["evidence_refs"] = [prefix + ref for ref in candidate["evidence_refs"]]


class QualifiedReviewRecoveryTests(unittest.TestCase):
    setUp = fixtures.AdminResponseStoreTests.setUp
    phase = fixtures.AdminResponseStoreTests.phase
    pending = fixtures.AdminResponseStoreTests.pending
    failed = fixtures.AdminResponseStoreTests.failed
    recover = fixtures.AdminResponseStoreTests.recover

    def frozen(self, identity):
        row = self.r.db.execute("SELECT * FROM admin_response_records WHERE call_id=?", (identity,)).fetchone()
        return {key: row[key] for key in ("response", "response_digest", "reserved", "reserved_digest", "plan_digest")}

    def assert_canonical_candidates(self, archive):
        expected = {}
        for item in json.loads(archive["response"])["items"]:
            for candidate in item["candidates"]:
                prefix = f'{candidate["problem_id"]}@{candidate["problem_revision"]}:'
                self.assertTrue(all(ref.startswith(prefix) for ref in candidate["evidence_refs"]))
                expected[candidate["title"]] = sorted({ref[len(prefix):] for ref in candidate["evidence_refs"]})
        actual = {}
        for row in self.r.db.execute("SELECT body FROM candidates"):
            body = json.loads(row[0])
            actual[body["title"]] = sorted(body["evidence_refs"])
        self.assertEqual(actual, expected)

    def test_initial_accept_keeps_qualified_reply_but_stores_canonical_sources(self):
        with patch("agent_learning.admin.subprocess.Popen", self.pending(modify=qualify)):
            result = admin_once(self.r, self.p)
        self.assertEqual(result["status"], "candidate", result)
        self.assert_canonical_candidates(self.frozen(result["call_id"]))
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)

    def test_explicit_local_recheck_preserves_original_reply_and_accounting(self):
        identity = self.failed(modify=qualify)
        archive = self.frozen(identity)
        paid = dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (identity,)).fetchone())
        usage = [tuple(row) for row in self.r.db.execute("SELECT * FROM admin_usage_observations")]
        version = status(self.r, self.p, identity)["version"]
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            receipt = self.recover(identity, expected_version=version)
            replay = self.recover(identity, expected_version=version)
        launch.assert_not_called()
        self.assertEqual(receipt["status"], "accepted", receipt)
        self.assertEqual(replay, receipt)
        self.assertEqual(self.frozen(identity), archive)
        self.assertEqual(dict(self.r.db.execute("SELECT * FROM admin_calls WHERE id=?", (identity,)).fetchone()), paid)
        self.assertEqual([tuple(row) for row in self.r.db.execute("SELECT * FROM admin_usage_observations")], usage)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 1)
        self.assert_canonical_candidates(archive)

    def test_recheck_qualified_reply_still_rejects_changed_case_revision(self):
        identity = self.failed(modify=qualify)
        archive = self.frozen(identity)
        problem_id = json.loads(archive["response"])["items"][0]["problem_id"]
        self.r.db.execute("UPDATE problems SET revision=revision+1 WHERE id=?", (problem_id,))
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            receipt = self.recover(identity)
        launch.assert_not_called()
        self.assertEqual(receipt["status"], "rejected", receipt)
        self.assertEqual(receipt["rejection"]["code"], "phase_source_changed")
        self.assertEqual(self.frozen(identity), archive)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
