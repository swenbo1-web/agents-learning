"""Two DSH profiles share knowledge but cannot silently exchange review origins."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.admin_queue import dispatch_diagnostic, eligible_query, executor_query, scan_signature
from agent_learning.dsh import DshAdapter
from agent_learning.dsh_profile import ENVIRONMENT
from agent_learning.runtime import ContractError, Runtime


class DshProfileOriginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-profile-origin-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.dsh_home = self.root / "dsh"
        self.env = patch.dict(os.environ, {"DSH_HOME": str(self.dsh_home), ENVIRONMENT: ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sdk = patch("agent_learning.admin_providers.dsh_sdk_launcher", return_value={"args": ["node", "runner"]})
        self.sdk.start()
        self.addCleanup(self.sdk.stop)
        self.runtime = Runtime(self.root / "state", host="dsh")
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.project(self.workspace)
        self.adapter = DshAdapter(self.runtime, self.workspace)
        self.runtime.admin_configure(self.project, True, model_mode="current")

    def select(self, profile):
        os.environ[ENVIRONMENT] = json.dumps({"version": 1,
            "profile_directory": str(self.dsh_home / "profiles" / profile), "entries": [
                {"name": "@deepseek-ai/dsh-llm-pi-ai", "config": {"providers": {
                    "shared-provider": {"baseURL": "https://" + profile + ".invalid"}}}}]})

    def session(self, native, profile):
        self.select(profile)
        self.adapter.handle("SessionStart", {"sessionId": native, "requestContext": {
            "provider": "shared-provider", "model": "shared-model"}})
        return self.runtime.host_conversation(native)

    def eligible(self):
        query, args = eligible_query("test-origin")
        query, args = executor_query(self.runtime, self.project, query, args)
        return self.runtime.db.execute(query + "ORDER BY p.created LIMIT 10", (self.project, *args)).fetchall()

    def test_same_model_different_endpoint_profiles_cannot_review_each_other(self):
        desktop = self.session("desktop-session", "desktop")
        desktop_case = self.runtime.feedback(self.project, desktop, "desktop evidence")
        web = self.session("web-session", "web")
        web_case = self.runtime.feedback(self.project, web, "web evidence")
        with patch("agent_learning.admin.subprocess.Popen") as model:
            for current, wrong, correct in (("web", desktop_case, web_case), ("desktop", web_case, desktop_case)):
                self.select(current)
                self.assertEqual([item["id"] for item in self.eligible()], [correct["id"]])
                with self.assertRaisesRegex(ContractError, "dsh_profile_source_mismatch"):
                    self.runtime.resolve_admin_executor(self.project, wrong["conversation"])
                result = admin_once(self.runtime, self.project, wrong["id"])
                self.assertEqual(result["reason"], "dsh_profile_source_mismatch")
                self.assertEqual(self.runtime.resolve_admin_executor(self.project, correct["conversation"])["model"],
                                 "shared-provider/shared-model")
            model.assert_not_called()
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)

    def test_other_profiles_old_cases_do_not_starve_current_profile_after_limit(self):
        desktop = self.session("many-desktop", "desktop")
        for index in range(45):
            self.runtime.feedback(self.project, desktop, "desktop old case " + str(index))
        web = self.session("later-web", "web")
        own = self.runtime.feedback(self.project, web, "current profile case")
        self.assertEqual([row["id"] for row in self.eligible()], [own["id"]])

    def test_legacy_unbound_case_waits_until_actual_host_event_and_binding_changes_scan(self):
        self.select("web")
        conv = self.runtime.host_conversation("legacy")
        self.runtime.admin_observe_model(self.project, conv, "shared-provider/shared-model")
        case = self.runtime.feedback(self.project, conv, "legacy source has no origin")
        before = scan_signature(self.runtime, self.project)
        with patch("agent_learning.admin.subprocess.Popen") as model:
            result = admin_once(self.runtime, self.project, case["id"])
            self.assertEqual(result["reason"], "dsh_profile_source_unbound")
            self.assertEqual(dispatch_diagnostic(self.runtime, self.project)["reason"], "dsh_profile_source_unbound")
            model.assert_not_called()
        self.assertFalse(self.eligible())
        self.adapter.handle("UserPromptSubmit", {"sessionId": "legacy", "turn": 1, "prompt": "new actual host input"})
        self.assertNotEqual(scan_signature(self.runtime, self.project), before)
        self.assertEqual([row["id"] for row in self.eligible()], [case["id"]])
        packet = self.runtime.review_packet(self.project, case["id"])
        self.assertNotIn("DshProfileBound", json.dumps(packet))
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM work_memories WHERE event_id IN "
            "(SELECT id FROM events WHERE type='DshProfileBound')").fetchone()[0], 0)

    def test_cross_profile_reused_native_session_cannot_rebind_or_capture_wrong_tool(self):
        conversation = self.session("same-native", "desktop")
        binding = self.runtime.db.execute("SELECT payload FROM events WHERE type='DshProfileBound'").fetchone()[0]
        self.select("web")
        with self.assertRaisesRegex(ContractError, "dsh_profile_source_mismatch"):
            self.adapter.handle("PostToolUse", {"sessionId": "same-native", "callId": "wrong-profile-call",
                "name": "read", "arguments": {"file_path": "a.py"}, "result": {"success": True}})
        self.assertEqual(self.runtime.db.execute("SELECT payload FROM events WHERE type='DshProfileBound'").fetchone()[0], binding)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM events WHERE conversation=? AND type='PostToolUse'",
                                                (conversation,)).fetchone()[0], 0)

    def test_missing_profile_keeps_capture_and_payload_path_cannot_create_binding(self):
        self.adapter.handle("UserPromptSubmit", {"sessionId": "no-profile", "turn": 1, "prompt": "public normal requirement",
            "profile_directory": str(self.dsh_home / "profiles/web"), ENVIRONMENT: "untrusted"})
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM events WHERE type='DshProfileBound'").fetchone()[0], 0)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM events WHERE type='UserPromptSubmit'").fetchone()[0], 1)

    def test_non_dsh_source_explicit_provider_policy_and_explicit_history_keep_contract(self):
        self.select("web")
        self.runtime.admin_configure(self.project, True, model_mode="fixed", model="shared-provider/shared-model")
        # Explicit history uses no daily source conversation; an operator can
        # also deliberately route a Codex/AG source to the DSH provider.
        self.assertEqual(self.runtime.resolve_admin_executor(self.project, None)["model"], "shared-provider/shared-model")
        self.assertEqual(self.runtime.resolve_admin_executor(self.project, "codex-source")["model"], "shared-provider/shared-model")
        self.assertEqual(self.runtime.resolve_admin_executor(self.project, "antigravity:default:ag-source")["model"], "shared-provider/shared-model")

    def test_default_first_and_fixed_daily_modes_also_require_matching_profile_origin(self):
        desktop = self.session("desktop-shared-policy", "desktop")
        case = self.runtime.feedback(self.project, desktop, "desktop only origin")
        self.session("web-shared-policy", "web")
        for mode in ("default", "first", "fixed"):
            self.runtime.admin_configure(self.project, True, model_mode=mode,
                model="shared-provider/shared-model" if mode == "fixed" else "")
            with self.assertRaisesRegex(ContractError, "dsh_profile_source_mismatch"):
                self.runtime.resolve_admin_executor(self.project, desktop)
            self.assertNotIn(case["id"], [row["id"] for row in self.eligible()])
