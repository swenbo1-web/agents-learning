"""Explicit version scope is shared by automatic lanes, not manual access."""
import json
import unittest
from unittest.mock import patch

from agent_learning import candidate_references as refs
from tests import test_context_dedup as fixtures


class VersionContextChannelTests(unittest.TestCase):
    OLD = "适用条件：仅 v1.4.35。\n结论：versionguard 使用旧接口。\n限制：v1.4.37 不适用。"
    NEW = "适用条件：仅 v1.4.37。\n结论：versionguard 使用新接口。\n限制：仅为公开陈述，尚未独立验收。"

    def setUp(self):
        self.f = fixtures.ContextDedupTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.r, self.p = self.f.r, self.f.p

    @staticmethod
    def cards(text):
        result = {"formal": [], "candidate": [], "work": []}
        for line in text.splitlines():
            if "{" not in line:
                continue
            try:
                item = json.loads(line[line.index("{"):])
            except ValueError:
                continue
            if isinstance(item, dict):
                for lane, key in (("formal", "knowledge_id"), ("candidate", "candidate_id"), ("work", "memory_id")):
                    if key in item:
                        result[lane].append(item[key])
        return result

    def material(self, lane, content, version, *, capture=False):
        source = self.f.source(content, capture=capture or lane == "work")
        if lane == "work":
            return source["memory"]["memory_id"]
        candidate, _ = self.f.candidate(source, content=content, title="versionguard " + version)
        return (self.r.publish(self.p, candidate["candidate_id"])["knowledge_id"]
                if lane == "formal" else candidate["candidate_id"])

    def check_lane(self, lane):
        if lane == "candidate":
            policy = refs.policy(self.r, self.p)
            refs.configure(self.r, self.p, True, policy["version"])
        old = self.material(lane, self.OLD, "v1.4.35")
        new = self.material(lane, self.NEW, "v1.4.37")
        if lane == "candidate":
            selected = refs.search(self.r, self.p, "versionguard v1.4.37", limit=3, workspace=self.f.workspace)
            self.assertNotIn(old, [item["candidate_id"] for item in selected])
            self.assertIn(new, [item["candidate_id"] for item in selected])
        actual = self.cards(self.f.context("versionguard v1.4.37"))[lane]
        self.assertNotIn(old, actual)
        self.assertIn(new, actual)
        if lane == "formal":
            self.assertEqual(self.r.read(self.p, old)["content"], self.OLD)
            self.assertIn(old, [item["knowledge_id"] for item in self.r.search(self.p, "versionguard v1.4.37")["items"]])
        elif lane == "candidate":
            version = refs.assess(self.r, self.p, old)["reference_version"]
            self.assertEqual(refs.read(self.r, self.p, self.f.handle, old, version,
                                      workspace=self.f.workspace)["content"], self.OLD)
        else:
            self.assertEqual(self.r.work_read(self.p, old, workspace=self.f.workspace)["content"], self.OLD)

    def test_formal_version_scope_keeps_manual_read(self):
        self.check_lane("formal")

    def test_candidate_version_scope_keeps_manual_read(self):
        self.check_lane("candidate")

    def test_work_version_scope_keeps_manual_read(self):
        self.check_lane("work")

    def test_source_work_cannot_bypass_formal_version_exclusion(self):
        old = self.material("formal", self.OLD, "v1.4.35", capture=True)
        actual = self.cards(self.f.context("versionguard v1.4.37"))
        self.assertNotIn(old, actual["formal"])
        self.assertEqual(actual["work"], [])

    def test_final_candidate_check_uses_current_query_and_full_body(self):
        policy = refs.policy(self.r, self.p)
        refs.configure(self.r, self.p, True, policy["version"])
        text = "适用条件：仅 v1.4.35。\n结论：versionguard 使用旧接口。\n" + "背景说明。" * 50 + "\n限制：v1.4.37 不适用。"
        old = self.material("candidate", text, "v1.4.35")
        selected = refs.search(self.r, self.p, "versionguard v1.4.35", workspace=self.f.workspace)
        self.assertIn(old, [item["candidate_id"] for item in selected])
        self.assertNotIn("不适用", selected[0]["content_summary"])
        with patch.object(refs, "search", return_value=selected):
            self.assertNotIn(old, self.cards(self.f.context("versionguard v1.4.37"))["candidate"])

    def test_new_text_filter_does_not_expand_existing_manual_environment_blocks(self):
        (self.f.workspace / "package.json").write_text(
            json.dumps({"name": "versionguard", "version": "1.4.37"}), encoding="utf-8")
        policy = refs.policy(self.r, self.p)
        refs.configure(self.r, self.p, True, policy["version"])
        candidate, _ = self.f.candidate(content=self.OLD, title="versionguard old interface",
                                        applicability="仅 v1.4.35")
        ident = candidate["candidate_id"]
        version = refs.assess(self.r, self.p, ident)["reference_version"]
        self.assertEqual(refs.search(self.r, self.p, "versionguard v1.4.37", workspace=self.f.workspace), [])
        self.assertEqual(refs.read(self.r, self.p, self.f.handle, ident, version,
                                  workspace=self.f.workspace)["content"], self.OLD)

    def test_fault_lookup_cannot_erase_the_explicit_current_version(self):
        task = self.r.task_update(self.p, self.f.conversation, "versionguard v1.4.37", {"mode": "analysis"})
        self.r.config["work_memory"]["automatic_context"] = False
        old = self.material("formal", self.OLD, "v1.4.35")
        self.assertNotIn(old, self.cards(self.f.context("versionguard v1.4.37"))["formal"])
        with self.r.transaction():
            self.r._queue_fault(self.p, self.f.conversation, None, task, self.r.session_epoch(),
                                {"tool_name": "versionguard", "tool_response": "versionguard v1.4.37 interface failed"})
        self.assertNotIn(old, self.cards(self.f.context("", tool_only=True))["formal"])

    def test_file_lookup_cannot_erase_the_explicit_current_version(self):
        self.r.task_update(self.p, self.f.conversation, "versionguard v1.4.37", {"mode": "analysis"})
        source = self.f.source(self.OLD, file=True)
        ident = source["memory"]["memory_id"]
        self.assertNotIn(ident, self.cards(self.f.context("versionguard v1.4.37"))["work"])
        result = self.f.context("", tool_only=True, raw={"tool_name": "read_file",
                               "tool_input": {"path": str(self.f.workspace / "ledger.py")}})
        self.assertNotIn(ident, self.cards(result)["work"])

    def test_work_explicit_project_version_uses_the_project_manifest(self):
        (self.f.workspace / "package.json").write_text(
            json.dumps({"name": "versionguard", "version": "1.4.37"}), encoding="utf-8")
        text = "适用条件：仅项目版本 v1.4.35。\n结论：versionguard 使用旧接口。"
        ident = self.material("work", text, "v1.4.35")
        self.assertNotIn(ident, self.cards(self.f.context("versionguard v1.4.37"))["work"])
        self.assertEqual(self.r.work_read(self.p, ident, workspace=self.f.workspace)["content"], text)
