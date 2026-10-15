"""Panel protocol boundaries with deterministic adapters; no installed host or database."""
import copy
import json
import sys
import types
import unittest
from unittest.mock import patch

from agent_learning.mcp import Server
from agent_learning.panel_ui import MIME_TYPE, RESOURCE_URI, UI_EXTENSION
from agent_learning.runtime import ContractError


EXPECTED = {key: 0 for key in ("expected_policy_version", "expected_budget_version",
    "expected_settings_version", "expected_session_epoch", "expected_governance_epoch")}
SUMMARY = {"project_id": "project", "project_name": "Project", "workspace": "project-path",
           "host_scope": "codex:default", "admin_admission_enabled": False,
           "model_mode": "first", "model": None, "remaining_calls": 100, "max_calls_per_day": 100,
           "counts": {"active": 2, "work_memory": 3, "pending_problems": 4, "candidates": 5},
           "budget_scope": "ALP_HOME", "reason": "disabled", "expected": EXPECTED}


class FakeRuntime:
    host = "codex"
    epoch = 0

    def session_epoch(self): return self.epoch
    def host_epoch(self): return self.epoch


class FakePanel:
    active = True

    def __init__(self, runtime, project, workspace, installation, require_context=True):
        self.calls, self.closed = [], False
        self.runtime, self.require_context = runtime, require_context

    def eligible(self): return self.active

    def bootstrap(self, **args):
        self.calls.append(("bootstrap", args))
        if args["context_handle"] != "current-handle":
            return {"visible": False, "reason": "context_required"}
        return {"visible": True, "summary": copy.deepcopy(SUMMARY), "capability": "private-panel-capability"}

    def action(self, capability, action, **args):
        if capability != "private-panel-capability":
            raise ContractError("panel capability is invalid")
        self.calls.append((action, args))
        return {"visible": True, "summary": copy.deepcopy(SUMMARY)}

    def close(self): self.closed = True


class PanelMCPTests(unittest.TestCase):
    def setUp(self):
        module = types.ModuleType("agent_learning.panel_control")
        module.PanelControl = FakePanel
        self.patch = patch.dict(sys.modules, {"agent_learning.panel_control": module})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.runtime, self.ident = FakeRuntime(), 0

    def request(self, server, method, params=None):
        self.ident += 1
        return server.request({"jsonrpc": "2.0", "id": self.ident, "method": method, "params": params or {}})

    def ready(self, *, install=True, ui=True, version="2025-11-25", role="worker", handle=None):
        server = Server(self.runtime, "project", workspace="project-path", role=role,
                        context_handle=handle, panel_installation="export-path" if install else None)
        capabilities = {"extensions": {UI_EXTENSION: {"mimeTypes": [MIME_TYPE]}}} if ui else {}
        result = self.request(server, "initialize", {"protocolVersion": version, "capabilities": capabilities,
                   "clientInfo": {"name": "offline-ui", "version": "1"}})
        self.assertNotIn("error", result)
        server.request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server, result["result"]

    def tool(self, server, name, args=None):
        return self.request(server, "tools/call", {"name": name, "arguments": args or {}})

    def test_no_install_or_admin_role_keeps_legacy_tools_only(self):
        for kwargs in ({"install": False}, {"role": "admin"}):
            with self.subTest(kwargs=kwargs):
                server, init = self.ready(**kwargs)
                self.assertNotIn("resources", init["capabilities"])
                names = {t["name"] for t in self.request(server, "tools/list")["result"]["tools"]}
                self.assertNotIn("learning_panel", names)
                self.assertNotIn("learning_panel_action", names)
                self.assertEqual(self.request(server, "resources/read", {"uri": RESOURCE_URI})["error"]["code"], -32601)

    def test_client_without_extension_has_text_fallback_and_no_action(self):
        server, init = self.ready(ui=False)
        self.assertNotIn("resources", init["capabilities"])
        tools = {t["name"]: t for t in self.request(server, "tools/list")["result"]["tools"]}
        self.assertNotIn("_meta", tools["learning_panel"])
        self.assertNotIn("learning_panel_action", tools)
        response = self.tool(server, "learning_panel", {"context_handle": "current-handle"})["result"]
        self.assertFalse(response["structuredContent"]["visible"])
        self.assertNotIn("_meta", response)
        self.assertFalse(server.panel.calls)

    def test_ui_metadata_is_only_on_render_and_app_only_action(self):
        server, init = self.ready()
        self.assertEqual(init["capabilities"]["resources"], {})
        tools = {t["name"]: t for t in self.request(server, "tools/list")["result"]["tools"]}
        self.assertEqual(tools["learning_panel"]["_meta"]["ui"]["resourceUri"], RESOURCE_URI)
        action = tools["learning_panel_action"]
        self.assertEqual(action["_meta"]["ui"], {"visibility": ["app"]})
        for name, tool in tools.items():
            if name not in {"learning_panel", "learning_panel_action"}:
                self.assertNotIn("_meta", tool)

    def test_capability_is_only_hidden_metadata_even_on_old_protocol(self):
        for version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            with self.subTest(version=version):
                server, _ = self.ready(version=version)
                response = self.tool(server, "learning_panel", {"context_handle": "current-handle"})["result"]
                self.assertEqual(response["_meta"]["agentLearningPanel"]["capability"], "private-panel-capability")
                self.assertEqual(response["_meta"]["agentLearningPanel"]["context_handle"], "current-handle")
                public = {k: v for k, v in response.items() if k != "_meta"}
                self.assertNotIn("private-panel-capability", json.dumps(public))
                self.assertNotIn("current-handle", json.dumps(public))
                direct = server.call("learning_panel", {"context_handle": "current-handle"})
                self.assertNotIn("private-panel-capability", json.dumps(direct))
                self.assertEqual("structuredContent" in response, version in {"2025-06-18", "2025-11-25"})

    def test_old_bound_handle_does_not_substitute_current_prompt_handle(self):
        server, _ = self.ready(handle="current-handle")
        missing = self.tool(server, "learning_panel")["result"]
        self.assertFalse(missing["structuredContent"]["visible"])
        mismatch = self.tool(server, "learning_panel", {"context_handle": "different-handle"})["result"]
        self.assertTrue(mismatch["isError"])
        self.assertIn("context_mismatch", mismatch["content"][0]["text"])

    def test_resource_has_no_secret_and_only_registered_uri_is_readable(self):
        server, _ = self.ready()
        listed = self.request(server, "resources/list")["result"]["resources"]
        self.assertEqual([r["uri"] for r in listed], [RESOURCE_URI])
        body = self.request(server, "resources/read", {"uri": RESOURCE_URI})["result"]["contents"][0]
        self.assertEqual(body["mimeType"], MIME_TYPE)
        self.assertNotIn("private-panel-capability", body["text"])
        self.assertEqual(body["_meta"]["ui"]["csp"]["connectDomains"], [])
        for uri in ("file:///runtime.sqlite3", "ui://agent-learning/unknown.html", "http://127.0.0.1/"):
            self.assertEqual(self.request(server, "resources/read", {"uri": uri})["error"]["code"], -32602)

    def test_removed_install_hides_resources_and_rejects_prior_capability(self):
        server, _ = self.ready()
        server.panel.active = False
        self.assertEqual(self.request(server, "resources/list")["result"], {"resources": []})
        self.assertNotIn("learning_panel", {t["name"] for t in self.request(server, "tools/list")["result"]["tools"]})
        response = self.tool(server, "learning_panel_action", {"capability": "private-panel-capability",
            "action": "status", "context_handle": "current-handle"})["result"]
        self.assertTrue(response["isError"])
        self.assertFalse(server.panel.calls)

    def test_action_requires_schema_capability_and_forwards_frozen_snapshot(self):
        server, _ = self.ready()
        base = {"capability": "private-panel-capability", "action": "set_admin_preset", "context_handle": "current-handle",
                "confirm": True, "request_id": "one-confirmation", "expected": EXPECTED, "preset": "light"}
        result = self.tool(server, "learning_panel_action", base)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(server.panel.calls[-1][0], "set_admin_preset")
        self.assertEqual(server.panel.calls[-1][1]["expected"], EXPECTED)
        self.assertEqual(server.panel.calls[-1][1]["request_id"], "one-confirmation")
        for changes in ({"capability": "wrong"}, {"preset": "infinite"}, {"expected": {}},
                        {"workspace": "foreign"}, {"action": "publish"}, {"confirm": 1}):
            with self.subTest(changes=changes):
                response = self.tool(server, "learning_panel_action", {**base, **changes})
                self.assertTrue("error" in response or response["result"]["isError"])
        self.assertEqual(len(server.panel.calls), 1)

    def test_epoch_change_denies_read_and_action_and_close_releases_controller(self):
        server, _ = self.ready()
        self.runtime.epoch = 1
        self.assertEqual(self.request(server, "resources/read", {"uri": RESOURCE_URI})["error"]["code"], -32001)
        self.assertEqual(self.tool(server, "learning_panel", {"context_handle": "current-handle"})["error"]["code"], -32001)
        server.close()
        self.assertTrue(server.panel.closed)
