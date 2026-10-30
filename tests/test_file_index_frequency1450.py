"""File-index frequency is optional; ephemeral knowledge and warnings stay live."""
import json
import unittest
from unittest.mock import patch

from agent_learning.antigravity import AntigravityAdapter
from agent_learning.runtime import Runtime, encode
from agent_learning.work_notice_delivery import FileNoticeGate
from tests import test_context_dedup as fixtures


class FileIndexFrequencyTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ContextDedupTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.workspace = self.f.workspace
        self.workspace.mkdir(exist_ok=True)
        self.path = self.workspace / "ledger.py"
        self.path.write_text("ledger = 1\n", encoding="utf-8")
        self.sequence = 0

    def host(self, host="antigravity", *, generation=True):
        if host != "codex":
            self.f.r = Runtime(self.f.root / (host+"-home"), host=host, config={
                "admin": {"enabled": False}, "scheduler": {"enabled": False}})
            self.f.addCleanup(self.f.r.close)
            self.f.p = self.f.r.project(self.workspace)
            self.f.conversation = self.f.r.host_conversation("reader")
            self.f.r.task_update(self.f.p, self.f.conversation, "dedupfocus ledger ordering", {"mode": "analysis"})
            self.f.handle = self.f.r.register_context(self.f.p, self.f.conversation)["context_handle"]
            if generation:
                table = "antigravity_trajectories" if host == "antigravity" else "dsh_sessions"
                self.f.r.db.execute(f"INSERT INTO {table}(project,conversation) VALUES(?,?)", (self.f.p,self.f.conversation))
        self.r, self.p = self.f.r, self.f.p

    def source(self, response=None, *, completion="complete", same_conversation=False):
        self.sequence += 1
        payload = {"tool_name": "write_to_file", "tool_input": {"TargetFile": str(self.path)},
                   "tool_response": {"error": "", "isError": False} if response is None else response,
                   "completion": completion}
        conversation = self.f.conversation if same_conversation else "writer-"+str(self.sequence)
        task = self.r.task(self.p, self.f.conversation)
        with self.r.transaction():
            event, _ = self.r._event(self.p, conversation, "PostToolUse", payload,
                task_id=task["id"], task_revision=task["revision"], outcome="call_completed")
            memory = self.r._work_capture_event(self.p, event["id"], self.workspace, raw=payload)
            if completion != "complete":
                row = self.r.db.execute("SELECT body FROM work_memories WHERE id=?", (memory["memory_id"],)).fetchone()
                self.r.db.execute("UPDATE work_memories SET body=? WHERE id=?",
                    (encode(dict(json.loads(row[0]), completion=completion)), memory["memory_id"]))
        return memory["memory_id"], event["id"]

    def context(self, **kwargs):
        return self.f.context(tool_only=True, delivery="ephemeral" if self.r.host == "antigravity" else "persistent",
            raw={"tool_name":"Read", "tool_input":{"path":"ledger.py"}}, **kwargs)

    def cards(self, **kwargs):
        return self.f.work_cards(self.context(**kwargs))

    def test_three_hosts_show_index_once_in_same_unchanged_boundary(self):
        for host in ("codex", "dsh", "antigravity"):
            with self.subTest(host=host):
                self.host(host)
                memory, _ = self.source()
                self.assertIn(memory, self.context())
                self.assertNotIn(memory, self.context())

    def test_ag_own_write_followed_by_multiple_invocations_is_not_replayed(self):
        self.host()
        memory, _ = self.source(same_conversation=True)
        self.assertIn(memory, self.context())
        for _ in range(3):
            self.assertNotIn(memory, self.context())

    def test_ag_native_post_write_to_pre_invocation_path_deduplicates_indices(self):
        self.host()
        adapter = AntigravityAdapter(self.r,self.workspace)
        common = {"conversationId":"reader","workspacePaths":[str(self.workspace)],"modelName":"offline-model"}
        adapter.handle("PreInvocation",dict(common,invocationNum=0,initialNumSteps=0))
        adapter.handle("PostToolUse",dict(common,stepIdx=0,toolCall={"name":"write_to_file","args":{"TargetFile":str(self.path)}},error=""))
        memory = self.r.db.execute("SELECT id FROM work_memories WHERE project=? AND conversation=? AND json_extract(body,'$.type')='file_change'",(self.p,self.f.conversation)).fetchone()[0]
        first = adapter.handle("PreInvocation",dict(common,invocationNum=1,initialNumSteps=1))
        self.assertIn(memory,encode(first))
        self.assertIn('task_affinity',encode(first))
        for invocation in (2,3):
            self.assertNotIn(memory,encode(adapter.handle("PreInvocation",dict(common,invocationNum=invocation,initialNumSteps=1))))

    def test_raw_protocol_summary_is_omitted_only_from_file_card(self):
        self.host()
        memory, _ = self.source()
        card = next(c for c in self.cards() if c["memory_id"] == memory)
        self.assertNotIn("summary", card)
        body = json.loads(self.r.db.execute("SELECT body FROM work_memories WHERE id=?", (memory,)).fetchone()[0])
        self.assertEqual(json.loads(body["summary"]), {"error":"", "isError":False})

    def test_substantive_tool_text_is_preserved(self):
        self.host()
        memory, _ = self.source({"stdout":"dedupfocus: ledger revision ordering was inspected"})
        for _ in range(2):
            cards = self.cards()
            self.assertIn(memory, {item["memory_id"] for item in cards})
            self.assertIn("ledger revision", cards[0]["summary"])

    def test_tiny_budget_does_not_acknowledge_an_unsent_file_card(self):
        self.host()
        memory, _ = self.source()
        self.r.config["injection_max_chars"] = 128
        self.assertNotIn(memory, self.context())
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_notice_delivery WHERE path_key LIKE 'index:%'").fetchone()[0], 0)
        self.r.config["injection_max_chars"] = 2400
        self.assertIn(memory, self.context())

    def test_file_change_and_source_change_bypass_index_frequency(self):
        self.host()
        memory, event = self.source()
        self.context()
        self.path.write_text("ledger = 200\n", encoding="utf-8")
        changed = next(c for c in self.cards() if c["memory_id"] == memory)
        self.assertEqual(changed.get("reference_role"), "source_update")
        self.r.db.execute("UPDATE events SET payload=? WHERE id=?", (encode({"tool_name":"write_to_file", "tool_response":{"error":"changed"}}), event))
        changed = next(c for c in self.cards() if c["memory_id"] == memory)
        self.assertTrue(changed["source_changed"])

    def test_partial_records_are_never_frequency_suppressed(self):
        self.host()
        memory, _ = self.source(completion="partial")
        for _ in range(2):
            self.assertIn(memory, self.context())

    def test_unknown_file_observation_does_not_establish_coverage(self):
        self.host()
        memory, _ = self.source()
        with patch("agent_learning.work_memory.current_file_version", return_value={"status":"unavailable","content_hash":None}):
            for _ in range(2):
                self.assertIn(memory, self.context())

    def test_unknown_or_ambiguous_native_generation_never_suppresses(self):
        self.host(generation=False)
        memory, _ = self.source()
        for _ in range(2):
            self.assertIn(memory, self.context())
        self.r.db.execute("INSERT INTO antigravity_trajectories(project,conversation,ambiguous) VALUES(?,?,1)", (self.p,self.f.conversation))
        for _ in range(2):
            self.assertIn(memory, self.context())

    def test_new_generation_and_real_compaction_restore_file_index(self):
        self.host()
        memory, _ = self.source()
        self.context()
        self.assertNotIn(memory, self.context())
        self.r.db.execute("UPDATE antigravity_trajectories SET generation=generation+1")
        self.assertIn(memory, self.context())
        self.assertNotIn(memory, self.context())
        self.assertIn(memory, self.context(compact=True))

    def test_new_task_restores_index_without_reassigning_original_source(self):
        self.host()
        memory, event = self.source()
        self.context()
        old = self.r.task(self.p,self.f.conversation)
        self.r.task_update(self.p,self.f.conversation,"dedupfocus updated work",new_task=True,
            expected_task_id=old["id"],expected_write_version=old["write_version"])
        self.assertIn(memory,self.context())
        self.assertEqual(self.r.db.execute("SELECT task_id FROM events WHERE id=?",(event,)).fetchone()[0],old["id"])

    def test_cache_failure_keeps_index_available(self):
        self.host()
        memory, _ = self.source()
        self.context()
        self.r.db.execute("DROP TABLE work_notice_delivery")
        self.assertIn(memory, self.context())

    def test_source_warning_is_not_hidden_by_rolled_back_delivery(self):
        self.host()
        memory, _ = self.source()
        with patch.object(self.r, "work_record_exposure", side_effect=ValueError("synthetic write failure")):
            self.assertNotIn(memory, self.context())
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM work_notice_delivery WHERE path_key LIKE 'index:%'").fetchone()[0],0)
        self.assertIn(memory, self.context())

    def test_child_does_not_borrow_parent_file_index_frequency(self):
        self.host()
        memory, _ = self.source()
        self.context()
        handle = self.r.register_context(self.p,self.f.conversation,"child")["context_handle"]
        def child():
            return self.r._context(self.p,self.f.conversation,"dedupfocus",agent_id="child",context_handle=handle,
                workspace=self.workspace,delivery="ephemeral",tool_only=True,
                raw={"tool_name":"Read","tool_input":{"path":"ledger.py"}})
        self.assertIn(memory,child())
        self.assertNotIn(memory,child())

    def test_another_context_owner_does_not_borrow_file_index_frequency(self):
        self.host()
        memory, _ = self.source()
        self.context()
        conversation = self.r.host_conversation("other-reader")
        self.r.task_update(self.p,conversation,"dedupfocus ledger ordering")
        self.r.db.execute("INSERT INTO antigravity_trajectories(project,conversation) VALUES(?,?)",(self.p,conversation))
        handle = self.r.register_context(self.p,conversation)["context_handle"]
        output = self.r._context(self.p,conversation,"dedupfocus",context_handle=handle,workspace=self.workspace,
            delivery="ephemeral",tool_only=True,raw={"tool_name":"Read","tool_input":{"path":"ledger.py"}})
        self.assertIn(memory,output)

    def test_project_partition_is_independent_of_the_optional_material_key(self):
        self.host()
        memory, _ = self.source()
        row = self.r.db.execute("SELECT * FROM work_memories WHERE id=?",(memory,)).fetchone()
        item = self.r._work_item(self.p,row,workspace=self.workspace)
        card = self.cards()[0]
        gate = FileNoticeGate(self.r,self.p,self.f.conversation,None,"same-trusted-scope","ephemeral")
        gate.record_index(item,card)
        self.assertTrue(gate.index_repeated(item,card))
        other_workspace = self.f.root / "other-project"
        other_workspace.mkdir()
        other_project = self.r.project(other_workspace)
        self.r.db.execute("INSERT INTO antigravity_trajectories(project,conversation) VALUES(?,?)",(other_project,self.f.conversation))
        other = FileNoticeGate(self.r,other_project,self.f.conversation,None,"same-trusted-scope","ephemeral")
        self.assertFalse(other.index_repeated(item,card))

    def test_account_epoch_change_restores_file_index(self):
        self.host()
        memory, _ = self.source()
        self.context()
        self.assertNotIn(memory,self.context())
        self.r.scope_account_changed("synthetic-account-boundary")
        self.f.handle = self.r.register_context(self.p,self.f.conversation)["context_handle"]
        self.assertIn(memory,self.context())

    def test_ephemeral_formal_material_is_still_sent_each_time(self):
        self.host()
        formal, _ = self.f.published()
        self.source()
        for _ in range(2):
            output = self.f.context(delivery="ephemeral",raw={"tool_name":"Read","tool_input":{"path":"ledger.py"}})
            self.assertIn(formal["knowledge_id"],output)


if __name__ == "__main__":
    unittest.main()
