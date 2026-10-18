"""Automatic history must exclude the current prompt without losing its record."""
import json
from pathlib import Path
import tempfile
import unittest

from agent_learning.runtime import Runtime


class WorkContextRelevanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-work-relevance-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()

    def runtime(self, host="codex"):
        r = Runtime(self.root / host, host=host, config={"admin": {"enabled": False},
                                                        "scheduler": {"enabled": False}})
        self.addCleanup(r.close)
        return r, r.project(self.workspace)

    def prompt(self, runtime, prompt, conversation="reader", turn="one"):
        return runtime.handle_hook({"hook_event_name": "UserPromptSubmit", "cwd": str(self.workspace),
            "session_id": runtime.host_conversation(conversation), "turn_id": turn, "prompt": prompt})

    @staticmethod
    def text(value):
        return value.get("hookSpecificOutput", {}).get("additionalContext", "")

    def observed(self, r, p, text, kind="Stop", conversation="past"):
        payload = {"prompt" if kind == "UserPromptSubmit" else "last_assistant_message": text}
        with r.transaction():
            event, _ = r._event(p, r.host_conversation(conversation), kind, payload, outcome="reported")
            r._work_capture_event(p, event["id"], self.workspace)
        return event["id"]

    def test_prompt_is_saved_but_not_injected_into_itself_on_each_host(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                r, p = self.runtime(host)
                value = self.prompt(r, "saturnringdiagnostic")
                self.assertNotIn("[工作历史]", self.text(value))
                self.assertEqual(r.db.execute("SELECT COUNT(*) FROM work_memories WHERE project=?", (p,)).fetchone()[0], 1)
                self.assertTrue(r.work_search(p, "saturnringdiagnostic")["items"])

    def test_long_prompt_segments_are_also_excluded_before_limit(self):
        r, p = self.runtime()
        text = "longpromptfence " + "longpromptfence observation " * 150
        value = self.prompt(r, text)
        self.assertNotIn("[工作历史]", self.text(value))
        self.assertGreater(r.db.execute("SELECT COUNT(*) FROM work_memories WHERE project=?", (p,)).fetchone()[0], 2)
        self.assertTrue(r.work_search(p, "longpromptfence")["items"])

    def test_prior_relevant_record_survives_current_prompt_exclusion(self):
        r, p = self.runtime()
        source = self.observed(r, p, "orbitguard: retain launch evidence for orbitguard.")
        value = self.prompt(r, "orbitguard")
        text = self.text(value)
        self.assertIn("[工作历史]", text)
        cards = [json.loads(line.removeprefix("[工作历史] ")) for line in text.splitlines()
                 if line.startswith("[工作历史] ")]
        for card in cards:
            event = r.db.execute("SELECT event_id FROM work_memories WHERE project=? AND id=?", (p, card["memory_id"])).fetchone()[0]
            self.assertEqual(event, source)

    def test_weak_keyword_match_is_manual_searchable_but_not_auto_injected(self):
        r, p = self.runtime()
        self.observed(r, p, "Python package installation and wheel packaging.")
        self.assertTrue(r.work_search(p, "Python memory leak")["items"])
        self.assertNotIn("[工作历史]", self.text(self.prompt(r, "Python memory leak")))

    def test_historical_user_correction_remains_available(self):
        r, p = self.runtime()
        self.observed(r, p, "不对，signingcompat 应保留已发布应用签名。", kind="UserPromptSubmit")
        value = self.prompt(r, "signingcompat")
        self.assertIn("[工作历史]", self.text(value))
        self.assertIn("signingcompat", self.text(value))


if __name__ == "__main__":
    unittest.main()
