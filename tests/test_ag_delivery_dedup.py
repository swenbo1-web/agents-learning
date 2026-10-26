"""A proven complete AG delivery may replace only its exact short partial preview."""
import json
import unittest

from agent_learning.runtime import encode
from agent_learning.work_files import file_links_from_payload
from tests import test_antigravity_delivery_recovery as fixtures


class AntigravityDeliveryDedupTests(unittest.TestCase):
    setUp = fixtures.AntigravityDeliveryRecoveryTests.setUp
    raw = fixtures.AntigravityDeliveryRecoveryTests.raw
    write = fixtures.AntigravityDeliveryRecoveryTests.write
    add = fixtures.AntigravityDeliveryRecoveryTests.add
    invoke = fixtures.AntigravityDeliveryRecoveryTests.invoke
    post = fixtures.AntigravityDeliveryRecoveryTests.post
    pair = fixtures.AntigravityDeliveryRecoveryTests.pair
    stop = fixtures.AntigravityDeliveryRecoveryTests.stop
    events = fixtures.AntigravityDeliveryRecoveryTests.events

    def prepare(self, text="dedupfence: preserve verified evidence before applying this procedure."):
        self.pair(response=text)
        partial = self.memories("partial")[0]
        self.stop()
        complete = self.memories("complete")[0]
        return partial, complete

    def memories(self, completion):
        return [row for row in self.r.db.execute("SELECT * FROM work_memories WHERE project=? ORDER BY created,id", (self.p,))
                if json.loads(row["body"]).get("type") == "public_summary"
                and json.loads(row["body"]).get("completion") == completion]

    def automatic(self):
        return self.r.work_context_items(self.p, self.r.host_conversation("reader"), "dedupfence",
                                         workspace=self.workspace, limit=20)

    def ids(self):
        return {item["memory_id"] for item in self.automatic()}

    def set_body(self, row, **changes):
        body = json.loads(row["body"])
        body.update(changes)
        self.r.db.execute("UPDATE work_memories SET body=? WHERE project=? AND id=?", (encode(body), self.p, row["id"]))

    def test_fresh_native_post_then_stop_prefers_complete_without_losing_audit(self):
        partial, complete = self.prepare()
        rows = [tuple(row) for row in self.r.db.execute("SELECT * FROM work_memories ORDER BY id")]
        cards = self.automatic()
        self.assertIn(complete["id"], {card["memory_id"] for card in cards})
        self.assertNotIn(partial["id"], {card["memory_id"] for card in cards})
        self.assertTrue(all("proven_public_response_event" not in card for card in cards))
        self.assertEqual([tuple(row) for row in self.r.db.execute("SELECT * FROM work_memories ORDER BY id")], rows)
        manual = self.r.work_search(self.p, "dedupfence", limit=20, max_chars=10000)["items"]
        self.assertTrue({partial["id"], complete["id"]} <= {item["memory_id"] for item in manual})
        for row in (partial, complete):
            self.assertEqual(self.r.work_read(self.p, row["id"], workspace=self.workspace)["state"], "reported")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_post_without_stop_stays_available(self):
        self.pair(response="dedupfence public response")
        self.assertIn(self.memories("partial")[0]["id"], self.ids())

    def test_missing_proof_preserves_both_records(self):
        partial, complete = self.prepare()
        self.set_body(complete, proven_public_response_event=None)
        self.assertTrue({partial["id"], complete["id"]} <= self.ids())

    def test_changed_stop_source_does_not_hide_valid_partial(self):
        partial, complete = self.prepare()
        source = self.r.db.execute("SELECT payload FROM events WHERE project=? AND id=?", (self.p, complete["event_id"])).fetchone()
        payload = json.loads(source[0]);payload["extra_observation"] = "source changed after capture"
        self.r.db.execute("UPDATE events SET payload=? WHERE project=? AND id=?", (encode(payload), self.p, complete["event_id"]))
        self.assertIn(partial["id"], self.ids())

    def test_different_visible_text_is_not_suppressed(self):
        partial, complete = self.prepare()
        self.set_body(complete, summary="dedupfence: a different conclusion and procedure.")
        self.assertTrue({partial["id"], complete["id"]} <= self.ids())

    def test_different_task_or_assignment_boundary_does_not_suppress(self):
        partial, complete = self.prepare()
        self.r.db.execute("UPDATE work_memories SET task_id='another-task' WHERE project=? AND id=?", (self.p, complete["id"]))
        self.assertIn(partial["id"], self.ids())
        self.r.db.execute("UPDATE work_memories SET task_id=NULL WHERE project=? AND id=?", (self.p, complete["id"]))
        self.set_body(complete, assignment_id="another-assignment")
        self.assertIn(partial["id"], self.ids())

    def test_different_checkout_or_conversation_does_not_suppress(self):
        partial, complete = self.prepare()
        self.r.db.execute("UPDATE work_memories SET workspace_key='another-checkout' WHERE project=? AND id=?", (self.p, complete["id"]))
        self.assertIn(partial["id"], self.ids())
        self.r.db.execute("UPDATE work_memories SET workspace_key=?,conversation='antigravity:default:other' WHERE project=? AND id=?",
                          (partial["workspace_key"], self.p, complete["id"]))
        self.assertIn(partial["id"], self.ids())

    def test_truncated_or_incomplete_complete_projection_cannot_cover_partial(self):
        partial, complete = self.prepare()
        body = json.loads(complete["body"])
        self.set_body(complete, coverage={**body["coverage"], "truncated": True})
        self.assertIn(partial["id"], self.ids())
        self.set_body(complete, coverage={**body["coverage"], "file_projection_complete": False})
        self.assertIn(partial["id"], self.ids())

    def test_changed_or_different_file_evidence_cannot_cover_partial(self):
        partial, complete = self.prepare()
        path = self.workspace / "notes.txt";path.write_text("initial file", encoding="utf-8")
        link = file_links_from_payload("write_file", {"path": str(path)}, self.workspace)[0]
        self.r.db.execute("INSERT INTO work_file_links VALUES(?,?,?,?,?,?,?)", (self.p, complete["event_id"],
            link["checkout_key"], link["relative_path"], link["path_key"], link["relation"], encode(link["version"])))
        self.assertIn(partial["id"], self.ids())
        path.write_text("changed file content", encoding="utf-8")
        self.assertIn(partial["id"], self.ids())

    def test_long_segmented_delivery_keeps_sources_separate(self):
        self.pair(response="dedupfence " + "dedupfence public detail " * 70)
        self.stop()
        ids = self.ids()
        self.assertTrue(ids & {row["id"] for row in self.memories("partial")})
        self.assertTrue(ids & {row["id"] for row in self.memories("complete")})


if __name__ == "__main__":
    unittest.main()
