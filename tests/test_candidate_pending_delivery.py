"""Candidate pages and exposure tracking cannot acknowledge an unsent notice."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning import candidate_references as refs
from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime, encode
from agent_learning.work_files import normalize_file_target
from agent_learning.workspace_links import file_workspace_roots
from tests import test_candidate_references as fixtures


class CandidatePendingDeliveryTests(unittest.TestCase):
    candidate = fixtures.CandidateReferenceTests.candidate
    enable = fixtures.CandidateReferenceTests.enable

    def setUp(self):
        area = Path(__file__).resolve().parents[1] / "build/local/f64i/t"
        area.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="p-", dir=area)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("project", "linked", "home", "tmp", "codex", "runtime"):
            (self.root / name).mkdir()
        isolated = patch.dict(os.environ, {
            "ALP_HOME": str(self.root / "home"), "ALP_CONFIG": str(self.root / "config.json"),
            "ALP_RUNTIME_STORE": str(self.root / "runtime"), "CODEX_HOME": str(self.root / "codex"),
            "PYTHONDONTWRITEBYTECODE": "1", **{key: str(self.root / "tmp") for key in ("TMP", "TEMP", "TMPDIR")}})
        isolated.start()
        self.addCleanup(isolated.stop)
        self.workspace = self.root / "project"
        self.linked = self.root / "linked"
        initial = Runtime(self.root / "home", config={})
        self.p = initial.project(self.workspace)
        initial.close()
        self.r = Runtime(self.root / "home", config={"linked_workspaces": {self.p: [str(self.linked)]},
            "work_memory": {"enabled": False}, "admin": {"enabled": False},
            "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.target = self.r.task_update(self.p, "consumer", "Investigate Python memory leak")
        self.handle = self.r.register_context(self.p, "consumer")["context_handle"]
        self.identity, self.body, _, event, _ = self.candidate(content="Python memory leak detail; " * 120)
        self.path = self.linked / "allocator.py"
        self.path.write_text("unchanged allocator\n", encoding="utf-8")
        link = normalize_file_target(str(self.path), self.workspace,
            roots=file_workspace_roots(self.r, self.p, self.workspace))
        self.assertIsNotNone(link)
        self.r.db.execute("INSERT INTO work_file_links VALUES(?,?,?,?,?,?,?)", (
            self.p, event["id"], link["checkout_key"], link["relative_path"], link["path_key"],
            "read_target", encode(link["version"])))
        self.enable()
        self.version = refs.assess(self.r, self.p, self.identity)["reference_version"]
        self.server = Server(self.r, self.p, context_handle=self.handle, workspace=self.workspace)
        self.addCleanup(self.server.close)

    def context(self, budget=2400):
        self.r.config["injection_max_chars"] = budget
        return self.r._context(self.p, "consumer", "Python memory leak", context_handle=self.handle,
                               workspace=self.workspace)

    def saved(self):
        return dict(self.r.db.execute("SELECT * FROM candidate_reference_exposures WHERE project=? "
            "AND conversation='consumer' AND agent='' AND host_epoch=? AND candidate_id=?",
            (self.p, self.r.session_epoch(), self.identity)).fetchone())

    def queued(self):
        self.assertIn(self.identity, self.context())
        self.r.config["linked_workspaces"] = {}
        tiny = self.context(128)
        self.assertNotIn(self.identity, tiny)
        notice = json.loads(self.saved()["pending_notice"])
        self.assertEqual(notice["reason"], "reference_files_unverified")
        return notice

    def restore(self):
        self.r.config["linked_workspaces"] = {self.p: [str(self.linked)]}

    def page(self, **extra):
        return self.server.call("candidate_reference_read", {"candidate_id": self.identity,
            "expected_version": self.version, "max_chars": 512, **extra})

    def test_partial_page_keeps_unsent_notice_until_complete_hook_output(self):
        notice = self.queued()
        self.restore()
        with patch.object(refs, "ack_notifications", wraps=refs.ack_notifications) as acknowledge:
            page = self.page()
        self.assertIsNotNone(page["next_offset"])
        self.assertLessEqual(len(encode(page)), 512)
        acknowledge.assert_not_called()
        self.assertTrue(self.saved()["pending_notice"])
        self.assertEqual(json.loads(self.saved()["pending_notice"])["id"], notice["id"])
        self.assertNotIn(self.identity, self.context(128))
        self.assertTrue(self.saved()["pending_notice"])
        text = self.context(2400)
        self.assertIn(self.identity, text)
        self.assertIn("曾展示", text)
        self.assertIn("当前已恢复可核验", text)
        self.assertNotIn("当前无法核实", text)
        self.assertEqual(self.saved()["pending_notice"], "")
        self.assertNotIn("曾展示", self.context())

    def test_search_exposure_and_repeated_pages_are_not_notification_ack(self):
        notice = self.queued()
        self.restore()
        found = refs.search(self.r, self.p, "Python memory leak", context_handle=self.handle,
                            workspace=self.workspace)[0]
        self.assertTrue(refs.record_exposure(self.r, self.p, self.handle, found, workspace=self.workspace))
        page = self.page()
        self.page(offset=page["next_offset"])
        self.assertTrue(self.saved()["pending_notice"])
        self.assertEqual(json.loads(self.saved()["pending_notice"])["id"], notice["id"])

    def test_unknown_scope_page_keeps_current_unknown_notice(self):
        notice = self.queued()
        page = self.page()
        self.assertTrue(page["source_files_unverified"])
        notes = refs.notifications(self.r, self.p, self.handle, workspace=self.workspace)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["id"], notice["id"])
        self.assertIn("当前无法核实", notes[0]["text"])

    def test_newer_source_risk_supersedes_old_pending_and_rejects_stale_ack(self):
        old = self.queued()
        self.restore()
        self.page()
        self.r.db.execute("UPDATE candidates SET status='rejected' WHERE id=?", (self.identity,))
        latest = refs.notifications(self.r, self.p, self.handle, workspace=self.workspace)[0]
        self.assertNotEqual(old["id"], latest["id"])
        self.assertFalse(latest["eligible"])
        self.assertEqual(refs.ack_notifications(self.r, self.p, self.handle, [old], workspace=self.workspace), 0)
        self.assertEqual(refs.ack_notifications(self.r, self.p, self.handle, [latest], workspace=self.workspace), 1)
        self.assertEqual(refs.ack_notifications(self.r, self.p, self.handle, [latest], workspace=self.workspace), 0)

    def test_old_display_notice_survives_task_and_reference_version_refresh(self):
        old = self.queued()
        self.restore()
        task = self.r.task_update(self.p, "consumer", "Independent Python memory leak followup", new_task=True)
        body = dict(self.body, content="A fresh reported version: " + self.body["content"])
        self.r.db.execute("UPDATE candidates SET body=? WHERE id=?", (encode(body), self.identity))
        self.version = refs.assess(self.r, self.p, self.identity)["reference_version"]
        self.assertNotEqual(self.version, old["reference_version"])
        with self.assertRaises(ContractError):
            self.page(expected_version=old["reference_version"])
        self.page()
        self.assertEqual(self.saved()["task_id"], task["id"])
        notes = refs.notifications(self.r, self.p, self.handle, workspace=self.workspace)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["id"], old["id"])
        self.assertEqual(notes[0]["reference_version"], old["reference_version"])
        self.assertIn("曾展示", notes[0]["text"])
        self.assertTrue(refs.assess(self.r, self.p, self.identity)["eligible"])
        self.assertEqual(refs.ack_notifications(self.r, self.p, self.handle, notes, workspace=self.workspace), 1)

    def test_wrong_assignment_and_other_owner_cannot_consume_old_notice(self):
        old = self.queued()
        self.restore()
        current = refs.assess(self.r, self.p, self.identity)
        with self.assertRaises(ContractError):
            refs.record_exposure(self.r, self.p, self.handle, current, workspace=self.workspace,
                task_state={"id": self.target["id"], "revision": self.target["revision"], "assignment_id": "not-this-assignment"})
        self.r.task_update(self.p, "another-consumer", "Python memory leak")
        other = self.r.register_context(self.p, "another-consumer")["context_handle"]
        refs.read(self.r, self.p, other, self.identity, self.version, workspace=self.workspace, max_chars=512)
        self.assertEqual(refs.notifications(self.r, self.p, other, workspace=self.workspace), [])
        self.assertEqual(json.loads(self.saved()["pending_notice"])["id"], old["id"])

    def test_complete_ack_is_not_recreated_by_later_ordinary_exposure(self):
        self.queued()
        self.restore()
        notes = refs.notifications(self.r, self.p, self.handle, workspace=self.workspace)
        self.assertEqual(refs.ack_notifications(self.r, self.p, self.handle, notes, workspace=self.workspace), 1)
        self.page()
        self.assertEqual(refs.notifications(self.r, self.p, self.handle, workspace=self.workspace), [])

    def test_login_epoch_keeps_old_pending_out_of_new_owner(self):
        old = self.queued()
        previous_epoch = self.r.session_epoch()
        self.restore()
        self.r.account_changed()
        with self.assertRaises(ContractError):
            self.page()
        self.r.task_update(self.p, "consumer", "Python memory leak after login", new_task=True)
        self.handle = self.r.register_context(self.p, "consumer")["context_handle"]
        refs.read(self.r, self.p, self.handle, self.identity, self.version, workspace=self.workspace, max_chars=512)
        self.assertEqual(refs.notifications(self.r, self.p, self.handle, workspace=self.workspace), [])
        archived = self.r.db.execute("SELECT pending_notice FROM candidate_reference_exposures WHERE project=? "
            "AND conversation='consumer' AND agent='' AND host_epoch=? AND candidate_id=?",
            (self.p, previous_epoch, self.identity)).fetchone()[0]
        self.assertEqual(json.loads(archived)["id"], old["id"])
