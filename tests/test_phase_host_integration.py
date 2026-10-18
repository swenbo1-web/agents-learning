"""Real hook/adapter stage boundaries, with isolated storage and no model work."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin_control import apply_control, control_status
from agent_learning.admin_lifecycle import claim_call, finish_call
from agent_learning.admin_usage import admin_project_budget_status
from agent_learning.antigravity import AntigravityAdapter
from agent_learning.dsh import DshAdapter
from agent_learning.mcp import Server
from agent_learning.phase_review import configure, policy, status
from agent_learning.runtime import Runtime


class PhaseHostIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="phase-host-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"ALP_DSH_REVIEW_PROFILE": ""}).start()

    def runtime(self, host, name=None, *, enabled=True, shared=False):
        home = self.root / ("shared-home" if shared else (name or host) + "-home")
        r = Runtime(home, host=host, config={"admin": {"enabled": enabled, "command": [sys.executable],
                                                      "antigravity_command": [sys.executable],
                                                      "dsh_command": [sys.executable],
                                                      "timeout_seconds": 5, "max_input_chars": 16000}})
        self.addCleanup(r.close)
        # Hooks exercise queue admission; no external worker is started.
        patch.object(r, "_launch_admin").start()
        return r, r.project(self.workspace)

    def host_events(self, r, *, native="native-chat"):
        """Return an actual host entry, not direct phase arm/seal helpers."""
        if r.host == "antigravity":
            adapter = AntigravityAdapter(r, self.workspace)
            def send(kind, *, turn=1, text="Public project convention", child=False, partial=False):
                common = {"conversationId": native, "workspacePaths": [str(self.workspace)],
                          "modelName": "offline-host-model"}
                if kind in {"start", "prompt"}:
                    return adapter.handle("PreInvocation", {**common, "invocationNum": turn,
                        "initialNumSteps": 0, "lastUserInput": text})
                return adapter.handle("Stop", {**common, "executionNum": turn, "fullyIdle": not partial,
                    "terminationReason": "model_stop", "error": "", "finalModelOutput": text})
        elif r.host == "dsh":
            adapter = DshAdapter(r, self.workspace)
            def send(kind, *, turn=1, text="Public project convention", child=False, partial=False):
                common = {"sessionId": native, "turn": turn}
                if child:
                    common.update(parentSessionId=native, childSessionId="native-child")
                if kind == "start":
                    return adapter.handle("SessionStart", {**common, "source": "startup",
                        "requestContext": {"provider": "offline", "model": "host-model"}})
                if kind == "prompt":
                    return adapter.handle("UserPromptSubmit", {**common, "prompt": text})
                return adapter.handle("SubagentStop" if child else "Stop",
                                      {**common, "lastAssistantMessage": text})
        else:
            def send(kind, *, turn=1, text="Public project convention", child=False, partial=False):
                common = {"cwd": str(self.workspace), "session_id": native, "turn_id": str(turn)}
                if child:
                    common["agent_id"] = "native-child"
                if kind == "start":
                    return r.handle_hook({**common, "hook_event_name": "SessionStart", "source": "startup",
                                          "model": "offline-host-model"})
                if kind == "prompt":
                    return r.handle_hook({**common, "hook_event_name": "UserPromptSubmit", "prompt": text})
                return r.handle_hook({**common, "hook_event_name": "SubagentStop" if child else "Stop",
                                      "last_assistant_message": text})
        return send, r.host_conversation(native)

    def deliver(self, r, p, c):
        task = r.task(p, c)
        handle = r.register_context(p, c)["context_handle"]
        return Server(r, p, workspace=self.workspace).call("task_update", {
            "context_handle": handle, "goal": task["goal"], "status": "delivered",
            "expected_task_id": task["id"], "expected_write_version": task["write_version"]})

    def stage(self, r, p, send, c, number):
        r.task_update(p, c, "Project convention stage " + str(number), new_task=True)
        send("prompt", turn=number * 10, text="Document reusable convention " + str(number))
        self.deliver(r, p, c)
        return send("stop", turn=number * 10 + 1, text="Delivered convention " + str(number))

    def body(self, r, p, request_id, *, enabled=True):
        state = control_status(r, p)
        return {"request_id": request_id, "expected_policy_version": state["policy"]["policy_version"],
                "expected_project_budget_version": state["project_budget_policy"]["version"],
                "expected_settings_version": state["settings_version"], "expected_session_epoch": state["session_epoch"],
                "expected_governance_epoch": state["governance_epoch"], "enabled": enabled,
                "model_mode": "first", "model": "", "project_limits": {}, "start": False}

    def test_codex_real_hooks_count_delivered_stages_and_exclude_replies_children_replays(self):
        self.check_host_boundaries("codex")

    def test_antigravity_native_hooks_count_only_complete_delivered_stages(self):
        self.check_host_boundaries("antigravity")

    def test_dsh_native_hooks_count_delivered_stages_and_exclude_children_replays(self):
        self.check_host_boundaries("dsh")

    def check_host_boundaries(self, host):
        r, p = self.runtime(host)
        configure(r, p, {"mode": "phase", "every_phases": 2}, 0)
        send, c = self.host_events(r)
        send("start", turn=0)
        r.task_update(p, c, "Keep the current sourced project convention", new_task=True)
        send("prompt", turn=1)
        send("stop", turn=2, text="An ordinary progress reply, work is ongoing")
        self.assertEqual(status(r, p)["phases"], {}, "a normal reply is not a delivered stage")
        self.deliver(r, p, c)
        self.assertEqual(status(r, p)["phases"], {"awaiting_stop": 1})
        if host == "antigravity":
            send("stop", turn=3, text="A partial native result", partial=True)
        else:
            send("stop", turn=3, text="Child analysis finished", child=True)
        self.assertEqual(status(r, p)["phases"], {"awaiting_stop": 1})
        send("stop", turn=4, text="Delivered a reusable project convention")
        self.assertEqual(status(r, p)["phases"], {"sealed": 1})
        send("stop", turn=4, text="Delivered a reusable project convention")
        send("stop", turn=5, text="A follow-up explanation of the same delivery")
        self.assertEqual(status(r, p)["phases"], {"sealed": 1})
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0], 0)
        self.stage(r, p, send, c, 2)
        phases = r.db.execute("SELECT state,event_id FROM review_phases ORDER BY created").fetchall()
        self.assertEqual([row["state"] for row in phases], ["queued", "queued"])
        batch = r.db.execute("SELECT state,phase_ids,packet FROM phase_review_batches").fetchone()
        self.assertEqual(batch["state"], "pending")
        self.assertEqual(len(json.loads(batch["phase_ids"])), 2)
        packet = json.loads(batch["packet"])
        self.assertTrue(packet["items"], "the batch must contain real host evidence")
        self.assertEqual(packet["executor_conversation"], c)
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
        self.assertFalse(r.db.execute("SELECT 1 FROM work_summary_links").fetchone(),
                         "phase mode must not enqueue the legacy per-reply summary")

    def test_every_three_counts_three_deliveries_in_one_chat(self):
        r, p = self.runtime("codex")
        configure(r, p, {"mode": "phase", "every_phases": 3}, 0)
        send, c = self.host_events(r)
        send("start", turn=0)
        for n in (1, 2):
            self.stage(r, p, send, c, n)
            self.assertEqual(status(r, p)["phases"], {"sealed": n})
            self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0], 0)
        self.stage(r, p, send, c, 3)
        self.assertEqual(status(r, p)["phases"], {"queued": 3})
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM phase_review_batches").fetchone()[0], 1)

    def test_long_public_delivery_keeps_last_segment_in_each_host_stage_packet(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                r, p = self.runtime(host, "long-" + host)
                r.config["admin"]["max_input_chars"] = 32000
                configure(r, p, {"mode": "phase", "every_phases": 1}, 0)
                send, c = self.host_events(r)
                send("start", turn=0)
                r.task_update(p, c, "Retain all public release conventions", new_task=True)
                send("prompt", turn=1, text="Record the sourced convention and its final constraint")
                self.deliver(r, p, c)
                marker = "TAIL_CONSTRAINT_" + host
                reply = "A sourced project convention. " + "Details belong to this project. " * 75 + marker
                self.assertGreater(len(reply), 1400)
                send("stop", turn=2, text=reply)
                segments = r.db.execute("SELECT id,payload FROM events WHERE project=? AND conversation=? "
                    "AND type='PublicMessageSegment' ORDER BY json_extract(payload,'$.segment')", (p, c)).fetchall()
                self.assertGreater(len(segments), 1)
                self.assertIn(marker, json.loads(segments[-1]["payload"])["text"])
                batch = r.db.execute("SELECT packet FROM phase_review_batches WHERE project=?", (p,)).fetchone()
                self.assertIsNotNone(batch)
                packet = json.loads(batch[0])
                refs = {event["id"] for item in packet["items"] for event in item["events"]}
                self.assertIn(segments[-1]["id"], refs, "post-Stop segments must remain eligible by their original message time")
                self.assertIn(marker, json.dumps(packet, ensure_ascii=False))

    def test_first_explicit_enable_defaults_to_phase_on_all_hosts_without_launch(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                r, p = self.runtime(host, "first-" + host, enabled=False)
                send, _ = self.host_events(r)
                send("start", turn=0)
                self.assertEqual(policy(r, p)["mode"], "legacy")
                with patch("agent_learning.admin.subprocess.Popen") as model:
                    result = apply_control(r, p, self.body(r, p, "first-enable"))
                model.assert_not_called()
                self.assertTrue(result["settings"]["enabled"])
                self.assertEqual(result["settings"]["phase_review"]["policy"]["mode"], "phase")
                self.assertEqual(result["settings"]["phase_review"]["policy"]["every_phases"], 1)

    def test_existing_operator_and_explicit_legacy_choices_are_preserved_on_all_hosts(self):
        for host in ("codex", "antigravity", "dsh"):
            for choice in ("legacy_config", "operator_policy", "phase_legacy"):
                with self.subTest(host=host, legacy_choice=choice):
                    r, p = self.runtime(host, f"legacy-{host}-{choice}", enabled=choice == "legacy_config")
                    if choice == "phase_legacy":
                        configure(r, p, {"mode": "legacy"}, 0)
                    elif choice == "operator_policy":
                        r.admin_configure(p, True, provider="host", model_mode="default", command=[sys.executable])
                    send, _ = self.host_events(r)
                    send("start", turn=0)
                    result = apply_control(r, p, self.body(r, p, "preserve-legacy"))
                    self.assertEqual(result["settings"]["phase_review"]["policy"]["mode"], "legacy")

    def test_admin_enable_switches_are_host_scoped_with_one_project_identity(self):
        hosts = {host: self.runtime(host, enabled=False, shared=True)[0]
                 for host in ("codex", "antigravity", "dsh")}
        p = hosts["codex"].project(self.workspace)
        for host, r in hosts.items():
            self.assertEqual(r.project(self.workspace), p)
            send, _ = self.host_events(r, native=host + "-chat")
            send("start", turn=0)
        apply_control(hosts["codex"], p, self.body(hosts["codex"], p, "codex-on"))
        self.assertEqual({host: control_status(r, p)["enabled"] for host, r in hosts.items()},
                         {"codex": True, "antigravity": False, "dsh": False})
        apply_control(hosts["dsh"], p, self.body(hosts["dsh"], p, "dsh-on"))
        apply_control(hosts["codex"], p, self.body(hosts["codex"], p, "codex-off", enabled=False))
        self.assertEqual({host: control_status(r, p)["enabled"] for host, r in hosts.items()},
                         {"codex": False, "antigravity": False, "dsh": True})

    def test_tokens_and_unknown_reservations_share_project_ledger_across_hosts(self):
        hosts = [self.runtime(host, enabled=False, shared=True)[0] for host in ("codex", "antigravity", "dsh")]
        p = hosts[0].project(self.workspace)
        for r in hosts:
            configure(r, p, {"mode": "phase", "daily_tokens": 50000}, 0)
        first, second = hosts[:2]
        with first.transaction():
            claim = claim_call(first, p, "isolated-codex-stage", 1, "codex-evidence", first.host_epoch(), 5,
                               reserved_tokens=10000)
        self.assertEqual(claim["status"], "reserved", claim)
        finish_call(first, claim["call_id"], "skip", usage={"token_totals": {"input_tokens": 1000, "output_tokens": 200},
                                                           "completeness": "complete"})
        with second.transaction():
            unresolved = claim_call(second, p, "isolated-ag-stage", 1, "ag-evidence", second.host_epoch(), 5,
                                    reserved_tokens=20000)
        self.assertEqual(unresolved["status"], "reserved", unresolved)
        finish_call(second, unresolved["call_id"], "timeout_unknown",
                    usage={"token_totals": {"input_tokens": 100}, "completeness": "partial"})
        for r in hosts:
            usage = admin_project_budget_status(r, p)
            self.assertEqual(usage["reported_tokens"], 1300)
            self.assertEqual(usage["token_reservations"], 20000)
            self.assertEqual(usage["accounted_tokens"], 21200)
            self.assertEqual(usage["remaining_reported_tokens"], 28800)
            self.assertFalse(usage["call_limit_enforced"])
            other_workspace = self.root / (r.host + "-other-project")
            other_workspace.mkdir()
            other = r.project(other_workspace)
            self.assertEqual(admin_project_budget_status(r, other)["reported_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
