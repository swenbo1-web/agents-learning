"""Tool guidance stays discoverable and agrees with observable MCP behavior."""
import json
from pathlib import Path
import tempfile
import unittest

from agent_learning.mcp import Server, WORKER_TOOLS, validate
from agent_learning.runtime import ContractError, Runtime


class WorkerToolDocs1451Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()

    def server(self, host="codex"):
        r = Runtime(self.root / host, host=host, config={})
        self.addCleanup(r.close)
        p = r.project(self.workspace)
        c = r.host_conversation("contract-check")
        h = r.register_context(p, c)["context_handle"]
        s = Server(r, p, context_handle=h, workspace=str(self.workspace))
        self.addCleanup(s.close)
        s.request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "contract-check", "version": "1"}}})
        s.request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return r, p, c, h, s

    def test_every_parameter_has_guidance_and_defaults_validate(self):
        def inspect(contract, path):
            for name, spec in contract.get("properties", {}).items():
                with self.subTest(parameter=path + "." + name):
                    self.assertTrue(spec.get("description", "").strip())
                    if "default" in spec:
                        validate(spec["default"], spec)
                inspect(spec, path + "." + name)
        for name, description, contract in WORKER_TOOLS:
            self.assertTrue(description.strip(), name)
            self.assertLessEqual(len(description), 1000, name)
            inspect(contract, name)

    def test_discovery_guidance_has_a_finite_context_size(self):
        listing = [{"name": name, "description": text, "inputSchema": contract}
                   for name, text, contract in WORKER_TOOLS]
        # Tool definitions are model input too: prevent full manuals/examples
        # accumulating here. This is a character bound, not a token estimate.
        self.assertLessEqual(len(json.dumps(listing, ensure_ascii=False, separators=(",", ":"))), 24000)

    def test_three_mcp_hosts_preserve_worker_tool_set_and_metadata(self):
        expected = {name: (description, contract) for name, description, contract in WORKER_TOOLS}
        self.assertEqual(len(expected), 11)
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                _, _, _, _, s = self.server(host)
                result = s.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]
                self.assertEqual({t["name"] for t in result["tools"]}, set(expected))
                for tool in result["tools"]:
                    self.assertEqual((tool["description"], tool["inputSchema"]), expected[tool["name"]])
                    # Discovery must not misrepresent exposure/accounting writes
                    # as absolutely read-only, or expand publication authority.
                    if tool["name"] in {"work_memory_search", "case_feedback", "task_update"}:
                        self.assertFalse(tool.get("annotations", {}).get("readOnlyHint", False))
                self.assertNotIn("publish", expected)
                self.assertNotIn("revoke", expected)

    def test_lost_new_task_reply_requires_context_read_before_retry(self):
        r, p, c, h, s = self.server()
        first = s.call("task_update", {"goal": "first task", "new_task": True})
        request = {"goal": "different task", "new_task": True,
                   "expected_task_id": first["id"], "expected_write_version": first["write_version"]}
        created = s.call("task_update", request)
        with self.assertRaisesRegex(ContractError, "stale task identity"):
            s.call("task_update", request)
        current = s.call("session_context", {})["task"]
        self.assertEqual(current["id"], created["id"])
        self.assertEqual(current["write_version"], created["write_version"])
        self.assertEqual(r.db.execute("SELECT COUNT(DISTINCT id) FROM task_snapshots").fetchone()[0], 2)

    def test_feedback_retry_and_default_formal_counterexample_are_real_writes(self):
        r, p, c, h, s = self.server()
        source = r.feedback(p, "source", "compiler setup", category="operation")
        packet = r.review_packet(p, source["id"])
        candidate = r.submit_candidate(p, problem_id=source["id"], problem_revision=packet["problem"]["revision"],
            title="compiler setup", content="Check the configured compiler.", category="operation",
            applicability="This synthetic checkout.", limitations="Reported guidance, not validation.",
            evidence_refs=[packet["events"][0]["id"]], validation_level="reported")
        knowledge = r.publish(p, candidate["candidate_id"])
        task = s.call("task_update", {"goal": "check compiler"})
        request = {"summary": "This setup does not fit the current checkout", "idempotency_key": "one-report",
            "knowledge_id": knowledge["knowledge_id"], "knowledge_revision": knowledge["revision"],
            "expected_task_id": task["id"], "expected_task_revision": task["revision"]}
        result = s.call("case_feedback", request)
        event_count = r.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(s.call("case_feedback", request), result)
        self.assertEqual(r.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], event_count)
        self.assertEqual(r.db.execute("SELECT kind FROM knowledge_feedback WHERE problem_id=?", (result["id"],)).fetchone()[0], "counterexample")
        with self.assertRaisesRegex(ContractError, "idempotency key reused"):
            s.call("case_feedback", dict(request, feedback_kind="counterexample"))

    def test_bound_history_search_records_exposure_without_rewriting_history(self):
        r, p, c, h, s = self.server()
        s.call("task_update", {"goal": "animation mapping"})
        with r.transaction():
            event, _ = r._event(p, c, "PostToolUse", {
                "tool_name": "write_file", "tool_input": {"path": "src/animation.py"},
                "tool_response": {"exit_code": 0, "output": "animation mapping modified"}}, outcome="succeeded")
            r._work_capture_event(p, event["id"], self.workspace)
        before = [tuple(v) for v in r.db.execute("SELECT id,revision,source_hash,body FROM work_memories")]
        result = s.call("work_memory_search", {"query": "animation"})
        self.assertTrue(result["items"])
        self.assertGreater(r.db.execute("SELECT COUNT(*) FROM work_exposures").fetchone()[0], 0)
        self.assertEqual([tuple(v) for v in r.db.execute("SELECT id,revision,source_hash,body FROM work_memories")], before)


if __name__ == "__main__":
    unittest.main()
