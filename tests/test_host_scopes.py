import tempfile
import time
import unittest
from pathlib import Path

from agent_learning.host_sessions import host_scope_key
from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime


class HostScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "state"
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.codex = Runtime(self.home, config={})
        self.ag = Runtime(self.home, config={}, host="antigravity")
        self.project = self.codex.project(self.workspace)
        self.assertEqual(self.project, self.ag.project(self.workspace))

    def tearDown(self):
        self.ag.close()
        self.codex.close()
        self.temp.cleanup()

    def establish(self, runtime, native="same-chat"):
        conversation = runtime.host_conversation(native)
        access = runtime.register_context(self.project, conversation)
        runtime.task_update(self.project, conversation, "观察操作问题", {"mode": "analysis"},
                            context_handle=access["context_handle"], expected_epoch=runtime.session_epoch())
        return conversation, access

    def test_same_native_conversation_is_isolated_but_project_is_shared(self):
        codex_conversation, codex_access = self.establish(self.codex)
        ag_conversation, ag_access = self.establish(self.ag)
        self.assertEqual(codex_conversation, "same-chat")
        self.assertEqual(ag_conversation, "antigravity:default:same-chat")
        self.assertNotEqual(codex_access["context_handle"], ag_access["context_handle"])
        for runtime, other in ((self.codex, ag_access), (self.ag, codex_access)):
            with self.assertRaises(ContractError):
                runtime.access_context(self.project, other["context_handle"])
        self.assertEqual(self.codex.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)

    def test_reserved_native_namespace_is_rejected(self):
        for native in ("antigravity:default:chat", "codex:other:chat"):
            for runtime in (self.codex, self.ag):
                with self.assertRaises(ContractError):
                    runtime.host_conversation(native)
        for invalid in (None, "", " "):
            self.assertFalse(self.ag.owns_conversation(invalid))
        with self.assertRaises(ContractError):
            host_scope_key("antigravity", "instance:chat")

    def test_codex_account_change_preserves_other_host_session_and_task(self):
        codex_conversation, codex_access = self.establish(self.codex)
        ag_conversation, ag_access = self.establish(self.ag)
        codex_task = self.codex.task(self.project, codex_conversation)
        ag_task = self.ag.task(self.project, ag_conversation)
        ag_epoch = self.ag.session_epoch()
        ag_server = Server(self.ag, self.project)
        ag_server.call("session_context", {"context_handle": ag_access["context_handle"]})
        changed = self.codex.account_changed("account-change-a")
        self.assertTrue(changed["changed"])
        self.assertEqual(self.ag.session_epoch(), ag_epoch)
        self.assertEqual(self.ag.task(self.project, ag_conversation), ag_task)
        self.assertEqual(ag_server.call("session_context", {"context_handle": ag_access["context_handle"]})[
            "conversation_id"], ag_conversation)
        self.assertGreater(self.codex.task(self.project, codex_conversation)["execution_epoch"],
                           codex_task["execution_epoch"])
        with self.assertRaises(ContractError):
            self.codex.access_context(self.project, codex_access["context_handle"])

    def test_antigravity_account_change_preserves_codex_session_and_context_cache(self):
        codex_conversation, codex_access = self.establish(self.codex)
        ag_conversation, _ = self.establish(self.ag)
        for conversation in (codex_conversation, ag_conversation):
            self.codex.db.execute("INSERT OR IGNORE INTO contexts(project,conversation) VALUES(?,?)",
                                  (self.project, conversation))
            self.codex.db.execute("UPDATE contexts SET injected='[\"knowledge-a\"]',last_query='kept',"
                                  "retrieval_signature='signature' WHERE project=? AND conversation=?",
                                  (self.project, conversation))
        codex_epoch = self.codex.session_epoch()
        changed = self.ag.account_changed("account-change-b")
        self.assertTrue(changed["governance_reconnect_required"])
        self.assertEqual(self.codex.session_epoch(), codex_epoch)
        self.codex.access_context(self.project, codex_access["context_handle"])
        codex_context = self.codex.db.execute("SELECT injected,last_query,retrieval_signature FROM contexts "
                                              "WHERE project=? AND conversation=?", (self.project, codex_conversation)).fetchone()
        ag_context = self.ag.db.execute("SELECT injected,last_query,retrieval_signature FROM contexts "
                                       "WHERE project=? AND conversation=?", (self.project, ag_conversation)).fetchone()
        self.assertEqual(tuple(codex_context), ('["knowledge-a"]', "kept", "signature"))
        self.assertEqual(tuple(ag_context), ("[]", "", ""))

    def test_notification_id_is_idempotent_per_scope(self):
        start = self.codex.host_epoch()
        first = self.codex.account_changed("same-notification")
        duplicate = self.codex.account_changed("same-notification")
        other = self.ag.account_changed("same-notification")
        self.assertTrue(first["changed"])
        self.assertFalse(duplicate["changed"])
        self.assertTrue(other["changed"])
        self.assertEqual(self.codex.host_epoch(), start + 2)
        self.assertEqual(self.codex.session_epoch(), first["host_epoch"])
        self.assertEqual(self.ag.session_epoch(), other["host_epoch"])

    def test_shared_governance_call_is_fenced_on_either_host_change(self):
        self.establish(self.codex)
        self.establish(self.ag)
        epoch = self.codex.host_epoch()
        self.codex.db.execute("INSERT INTO admin_calls(id,project,problem,revision,day,state,created,host_epoch,"
                              "owner,lease_until) VALUES(?,?,?,?,?,'reserved',?,?,?,?)",
                              ("call-a", self.project, "problem-a", 1, "2026-10-11", time.time(), epoch,
                               "owner-a", time.time() + 60))
        self.ag.account_changed("change-while-reviewing")
        row = self.codex.db.execute("SELECT state,lease_until FROM admin_calls WHERE id='call-a'").fetchone()
        self.assertEqual(row["state"], "account_changed_unknown")
        self.assertIsNone(row["lease_until"])
        with self.assertRaises(ContractError):
            self.codex.assert_host_epoch(epoch)

    def test_operation_fence_affects_only_changed_host(self):
        codex_conversation, _ = self.establish(self.codex)
        ag_conversation, _ = self.establish(self.ag)
        for runtime, conversation, operation in ((self.codex, codex_conversation, "op-codex"),
                                                   (self.ag, ag_conversation, "op-ag")):
            runtime.db.execute("INSERT INTO operations(project,id,conversation,state,last_event,host_epoch) "
                               "VALUES(?,?,?,'running','event-placeholder',?)",
                               (self.project, operation, conversation, runtime.session_epoch()))
        self.ag.account_changed("fence-operation")
        codex_op = self.codex.db.execute("SELECT state,host_epoch FROM operations WHERE id='op-codex'").fetchone()
        ag_op = self.ag.db.execute("SELECT state,host_epoch FROM operations WHERE id='op-ag'").fetchone()
        self.assertEqual(tuple(codex_op), ("running", self.codex.session_epoch()))
        self.assertEqual(tuple(ag_op), ("unknown", -1))

    def test_legacy_codex_epoch_and_notification_are_migrated(self):
        with self.codex.transaction():
            self.codex.db.execute("DROP TABLE host_scope_notifications")
            self.codex.db.execute("DROP TABLE host_scopes")
            self.codex.db.execute("UPDATE host_state SET epoch=7 WHERE id=1")
            self.codex.db.execute("INSERT INTO host_notifications VALUES('legacy-change',7,?)", (time.time(),))
            self.codex._install_host_storage()
        self.assertEqual(self.codex.session_epoch(), 7)
        replay = self.codex.account_changed("legacy-change")
        self.assertFalse(replay["changed"])
        self.assertEqual(self.codex.host_epoch(), 7)


if __name__ == "__main__":
    unittest.main()
