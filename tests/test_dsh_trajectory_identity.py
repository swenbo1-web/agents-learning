"""DSH reset namespaces and explicit native bridge handshakes, without models."""
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from agent_learning.dsh import DshAdapter, reset_session
from agent_learning.runtime import Runtime


class DshTrajectoryIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dsh-trajectory-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "workspace"
        self.work.mkdir()
        self.r = Runtime(self.root / "home", config={}, host="dsh")
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.work)
        self.c = self.r.host_conversation("native-chat")
        self.a = DshAdapter(self.r, self.work)
        self.instance = uuid.uuid4().hex

    def send(self, kind, *, instance=None, trajectory=None, **values):
        raw = dict(sessionId="native-chat", **values)
        if instance is not None:
            raw["dsh_bridge_instance"] = instance
        if trajectory is not None:
            raw["dsh_trajectory"] = trajectory
        return self.a.handle(kind, raw)

    def start(self, instance=None):
        instance = instance or self.instance
        return self.send("SessionStart", instance=instance, source="resume")["dsh_trajectory"]

    def count(self, kind):
        return self.r.db.execute("SELECT count(*) FROM events WHERE project=? AND type=?", (self.p, kind)).fetchone()[0]

    def test_reset_reused_prompt_turn_is_new_only_after_new_bridge_start(self):
        first = self.start()
        self.send("UserPromptSubmit", instance=self.instance, trajectory=first, turn=1, prompt="same public request")
        reset_session(self.r, self.work, "native-chat")
        before = self.count("UserPromptSubmit")
        refused = self.send("UserPromptSubmit", turn=1, prompt="same public request")
        self.assertTrue(refused["dsh_reconnect_required"])
        self.assertEqual(self.count("UserPromptSubmit"), before)
        new_instance = uuid.uuid4().hex
        second = self.start(new_instance)
        self.assertEqual(second["generation"], 2)
        self.send("UserPromptSubmit", instance=new_instance, trajectory=second, turn=1, prompt="same public request")
        self.assertEqual(self.count("UserPromptSubmit"), 2)

    def test_late_old_bridge_and_old_token_cannot_rebind_after_reset(self):
        first = self.start()
        reset_session(self.r, self.work, "native-chat")
        for event, values in (("SessionStart", {"source": "resume"}),
                              ("UserPromptSubmit", {"turn": 1, "prompt": "late old request"}),
                              ("Stop", {"turn": 1, "lastAssistantMessage": "late old delivery"})):
            result = self.send(event, instance=self.instance, trajectory=first, **values)
            self.assertTrue(result["dsh_reconnect_required"], event)
        self.assertTrue(self.send("SessionStart", instance=self.instance, source="resume")["dsh_reconnect_required"])
        self.assertEqual(self.count("UserPromptSubmit"), 0)
        self.assertEqual(self.count("Stop"), 0)

    def test_reset_reused_tool_id_has_independent_operation_and_task(self):
        self.r.task_update(self.p, self.c, "original work", new_task=True)
        first = self.start()
        call = {"callId": "reused-call", "name": "pwsh", "arguments": {"command": "echo ok"}}
        self.send("PreToolUse", instance=self.instance, trajectory=first, **call)
        self.send("PostToolUse", instance=self.instance, trajectory=first, result={"exit_code": 0}, **call)
        reset_session(self.r, self.work, "native-chat")
        new_instance = uuid.uuid4().hex
        second = self.start(new_instance)
        new_task = self.r.task_update(self.p, self.c, "new work", new_task=True)
        self.send("PreToolUse", instance=new_instance, trajectory=second, **call)
        self.send("PostToolUse", instance=new_instance, trajectory=second, result={"exit_code": 0}, **call)
        rows = self.r.db.execute("SELECT id,task_id,observation FROM operations ORDER BY rowid").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])
        self.assertNotEqual(rows[0]["task_id"], rows[1]["task_id"])
        self.assertEqual(rows[1]["task_id"], new_task["id"])
        self.assertEqual([row["observation"] for row in rows], ["succeeded", "succeeded"])
        before = self.count("PostToolUse")
        stale = self.send("PostToolUse", instance=self.instance, trajectory=first, result={"exit_code": 1}, **call)
        self.assertTrue(stale["dsh_reconnect_required"])
        self.assertEqual(self.count("PostToolUse"), before)
        self.assertFalse(self.r.db.execute("SELECT 1 FROM operations WHERE conflict=1").fetchone())

    def test_token_cannot_be_borrowed_by_another_bridge_or_child(self):
        first = self.start()
        for instance, values in ((uuid.uuid4().hex, {}),
                                  (self.instance, {"agentId": "child"})):
            result = self.send("PreToolUse", instance=instance, trajectory=first, callId="stale", name="read",
                               arguments={}, **values)
            self.assertEqual(result["decision"], "deny")
        self.assertEqual(self.count("PreToolUse"), 0)

    def test_initial_legacy_events_remain_compatible_without_metadata(self):
        self.send("UserPromptSubmit", turn=1, prompt="legacy request")
        self.send("UserPromptSubmit", turn=1, prompt="legacy request")
        self.assertEqual(self.count("UserPromptSubmit"), 1)

    def test_repeated_handshake_reuses_token_and_no_model_is_launched(self):
        with patch("subprocess.Popen") as model:
            first = self.start()
            second = self.start()
        self.assertEqual(first, second)
        self.assertEqual(self.r.db.execute("SELECT count(*) FROM dsh_bridge_bindings").fetchone()[0], 1)
        model.assert_not_called()

    def test_reset_blocks_missing_handshake_even_when_instance_is_new(self):
        reset_session(self.r, self.work, "native-chat")
        result = self.send("UserPromptSubmit", instance=uuid.uuid4().hex, turn=1, prompt="new but unbound")
        self.assertTrue(result["dsh_reconnect_required"])
        self.assertEqual(self.count("UserPromptSubmit"), 0)


if __name__ == "__main__":
    unittest.main()
