"""Stage aggregation uses isolated SQLite and a deterministic host, never a model."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.phase_review import configure, policy, seal, status
from agent_learning.runtime import Runtime
from tests.test_module6_queue import fake_reviewer


def reviewer(decision="candidate", *, wrong_source=False, callback=None):
    base = fake_reviewer()
    class PhaseReviewer(base):
        def communicate(self, data=None, timeout=None):
            self.timeout = timeout
            packet = json.loads(data.decode().split("<untrusted_evidence>", 1)[1].rsplit("</untrusted_evidence>", 1)[0])
            if callback:
                callback(packet)
            items = []
            for i, item in enumerate(packet["items"]):
                candidates = []
                if decision == "candidate":
                    for n in range(2):
                        candidates.append({"problem_id": item["problem"]["id"], "problem_revision": item["problem"]["revision"],
                            "title": f"Project convention {i}/{n}", "content": f"Observed sourced convention {i}/{n}",
                            "category": "project", "knowledge_kind": "fact", "validation_level": "reported",
                            "metadata": {key:"" for key in ("tool","tool_version","error_code","environment","verification")},
                            "applicability": "This project and the cited stage", "limitations": "Reported, not implementation verification",
                            "evidence_refs": ["wrong-event" if wrong_source else item["events"][0]["id"]]})
                items.append({"problem_id": item["problem"]["id"], "decision": decision, "reason": "Controlled phase result",
                              "candidate": None, "candidates": candidates, "missing_evidence": [], "proposal": None})
            self.output.write_text(json.dumps({"decision": decision,"reason":"Controlled batch","candidate":None,
                "missing_evidence":[],"proposal":None,"items":items}),encoding="utf-8")
            return None, None
    return PhaseReviewer


class PhaseReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="phase-review-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": True, "command": [sys.executable],
            "timeout_seconds": 5, "max_input_chars": 16000, "max_calls_per_day": 100, "auto_publish": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        with self.r.transaction():
            configure(self.r, self.p, {"mode":"phase","every_phases":2}, 0)

    def phase(self, conversation, goal="A sourced project fact"):
        task = self.r.task_update(self.p, conversation, goal, new_task=True)
        self.r._event(self.p, conversation, "UserPromptSubmit", {"prompt": goal}, outcome="reported",task_id=task["id"],task_revision=1)
        self.r.task_update(self.p, conversation, goal, status="delivered", expected_task_id=task["id"], expected_write_version=task["write_version"])
        event, _ = self.r._event(self.p, conversation, "Stop", {"last_assistant_message":goal}, outcome="reported",task_id=task["id"],task_revision=1)
        with self.r.transaction():
            seal(self.r, self.p, conversation, event["id"])
        return event

    def test_two_completed_phases_one_call_multiple_independent_candidates(self):
        self.phase("first")
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            self.assertIn(admin_once(self.r,self.p)["status"], {"idle","scan_complete","needs_evidence"})
            launch.assert_not_called()
        self.phase("second", "An independent build convention")
        with patch("agent_learning.admin.subprocess.Popen", reviewer()):
            result=admin_once(self.r,self.p)
        self.assertEqual(result["status"],"candidate",result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0],1)
        self.assertGreaterEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],4)
        self.assertEqual(status(self.r,self.p)["phases"].get("completed"),2)

    def test_stop_replay_and_old_delivered_task_do_not_add_phase(self):
        event=self.phase("first")
        with self.r.transaction():
            seal(self.r,self.p,"first",event["id"])
        later,_=self.r._event(self.p,"first","Stop",{"last_assistant_message":"More explanation"})
        with self.r.transaction():
            seal(self.r,self.p,"first",later["id"])
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM review_phases").fetchone()[0],1)

    def test_cross_case_evidence_rejected_atomically(self):
        self.phase("one");self.phase("two","Other facts")
        with patch("agent_learning.admin.subprocess.Popen", reviewer(wrong_source=True)):
            result=admin_once(self.r,self.p)
        self.assertNotEqual(result["status"],"candidate")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],0)

    def test_reading_phase_status_does_not_write(self):
        before=self.r.db.total_changes
        status(self.r,self.p)
        self.assertEqual(self.r.db.total_changes,before)

    def test_unknown_call_keeps_dynamic_token_reservation(self):
        import subprocess
        self.phase("one");self.phase("two","Other facts")
        class Timeout(reviewer()):
            def communicate(self,*args,**kwargs):
                raise subprocess.TimeoutExpired("controlled",5)
        with patch("agent_learning.admin.subprocess.Popen",Timeout):
            result=admin_once(self.r,self.p)
        self.assertEqual(result["status"],"timeout_unknown",result)
        row=self.r.db.execute("SELECT reserved_tokens FROM admin_call_budgets").fetchone()
        self.assertGreater(row[0],12000)

    def test_repeated_business_content_reuses_prior_review(self):
        self.phase("one");self.phase("two")
        with patch("agent_learning.admin.subprocess.Popen",reviewer("skip")) as model:
            first=admin_once(self.r,self.p)
        self.assertEqual(first["status"],"skip",first)
        before=self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0]
        self.phase("three");self.phase("four")
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            admin_once(self.r,self.p)
            launch.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0],before)

    def test_token_budget_prevents_launch_without_increasing_usage(self):
        configure(self.r,self.p,{"daily_tokens":1},policy(self.r,self.p)["version"])
        self.phase("one");self.phase("two","Other convention")
        with patch("agent_learning.admin.subprocess.Popen") as launch:
            result=admin_once(self.r,self.p)
        self.assertEqual(result["status"],"token_budget_exhausted",result)
        launch.assert_not_called()
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0],0)

    def test_project_call_count_is_informational_in_token_mode(self):
        from agent_learning.admin_usage import configure_project_budget
        from agent_learning.admin_usage import project_budget_policy
        configure_project_budget(self.r,self.p,{"max_calls_per_day":0},expected_version=project_budget_policy(self.r,self.p)["version"])
        self.phase("one");self.phase("two","Other convention")
        with patch("agent_learning.admin.subprocess.Popen",reviewer("skip")):
            result=admin_once(self.r,self.p)
        self.assertEqual(result["status"],"skip",result)

    def test_tiered_publication_checks_each_candidate_in_one_transaction(self):
        import time
        self.r.db.execute("INSERT INTO admin_control_state(project,host_scope,enabled,version,updated,auto_publish,publication_mode) "
            "VALUES(?,?,1,1,?,1,'tiered')", (self.p,self.r._session_scope(),time.time()))
        self.phase("one");self.phase("two","Other convention")
        with patch("agent_learning.admin.subprocess.Popen",reviewer()):
            result=admin_once(self.r,self.p)
        self.assertEqual(result["status"],"candidate",result)
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM active").fetchone()[0],4)

    def test_evidence_changes_while_model_runs_roll_back_all_candidates(self):
        self.phase("one");self.phase("two","Other convention")
        def changed(packet):
            self.r.db.execute('UPDATE problems SET revision=revision+1 WHERE id=?',(packet['items'][-1]['problem']['id'],))
        with patch("agent_learning.admin.subprocess.Popen",reviewer(callback=changed)):
            result=admin_once(self.r,self.p)
        self.assertNotEqual(result["status"],"candidate")
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0],0)

    def test_source_change_before_launch_is_replanned_without_spending_twice(self):
        self.phase("one");self.phase("two","Other convention")
        member=self.r.db.execute('SELECT problem_id FROM phase_review_members LIMIT 1').fetchone()[0]
        self.r.db.execute('UPDATE problems SET revision=revision+1 WHERE id=?',(member,))
        with patch("agent_learning.admin.subprocess.Popen",reviewer("skip")):
            result=admin_once(self.r,self.p)
        self.assertEqual(result["status"],"skip",result)
        self.assertEqual(self.r.db.execute('SELECT COUNT(*) FROM admin_calls').fetchone()[0],1)

    def test_current_model_uses_real_source_conversation(self):
        self.r.admin_configure(self.p,True,'host','current','',expected_policy_version=0)
        self.r.admin_observe_model(self.p,'one','model-a')
        self.r.admin_observe_model(self.p,'two','model-a')
        self.phase('one');self.phase('two','Other convention')
        with patch('agent_learning.admin.subprocess.Popen',reviewer('skip')):
            result=admin_once(self.r,self.p)
        self.assertEqual(result['status'],'skip',result)

    def test_stages_are_host_scoped_and_use_shared_project_token_cap(self):
        for host in ('antigravity','dsh'):
            other=Runtime(self.r.home,host=host)
            try:
                self.assertEqual(policy(other,self.p)['mode'],'legacy')
                self.assertEqual(policy(other,self.p)['daily_tokens'],1000000)
                self.assertEqual(status(other,self.p)['phases'],{})
            finally:other.close()


if __name__=="__main__":unittest.main()
