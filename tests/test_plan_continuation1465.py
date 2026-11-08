"""Plan-only follow-ups retain a subject, never task or execution authority."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.continuation import is_continuation_query
from agent_learning.runtime import Runtime


class PlanContinuation1465Tests(unittest.TestCase):
    def setUp(self):
        area = Path(__file__).resolve().parents[1] / "build/local/f65q/tests"
        area.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=area)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def runtime(self, host):
        root = self.base / host
        root.mkdir()
        for name in ("home", "tmp", "codex", "runtime-releases", "project"):
            (root / name).mkdir()
        environment = patch.dict(os.environ, {"ALP_HOME": str(root / "home"), "ALP_CONFIG": str(root / "config.json"),
            "ALP_RUNTIME_STORE": str(root / "runtime-releases"), "CODEX_HOME": str(root / "codex"),
            "PYTHONDONTWRITEBYTECODE": "1", **{key: str(root / "tmp") for key in ("TMP", "TEMP", "TMPDIR")}})
        environment.start()
        self.addCleanup(environment.stop)
        runtime = Runtime(root / "home", host=host, config={"admin": {"enabled": False},
            "scheduler": {"enabled": False}, "hindsight": {"enabled": False}})
        self.addCleanup(runtime.close)
        workspace = root / "project"
        project = runtime.project(workspace)
        conversation = runtime.host_conversation("reader")
        runtime.task_update(project, conversation, "planfocus ledger ordering", {"mode": "analysis"})
        handle = runtime.register_context(project, conversation)["context_handle"]
        return runtime, project, workspace, conversation, handle

    def publish(self, runtime, project, subject="planfocus ledger ordering"):
        problem = runtime.feedback(project, runtime.host_conversation("source"), subject, category="project")
        packet = runtime.review_packet(project, problem["id"])
        candidate = runtime.submit_candidate(project, problem_id=problem["id"], problem_revision=problem["revision"],
            title=subject, content=subject + ": preserve the explicitly recorded boundary.", applicability=subject,
            limitations="Reported reference only; independently verify.", category="project", validation_level="reported",
            knowledge_kind="fact", evidence_refs=[event["id"] for event in packet["events"]])
        return runtime.publish(project, candidate["candidate_id"])["knowledge_id"]

    def context(self, args, query, **kwargs):
        runtime, project, workspace, conversation, handle = args
        return runtime._context(project, conversation, query=query, workspace=workspace, context_handle=handle,
            delivery="ephemeral" if runtime.host == "antigravity" else "persistent", **kwargs)

    @staticmethod
    def formal_ids(text):
        return [json.loads(line)["knowledge_id"] for line in text.splitlines()
                if line.startswith('{"') and "knowledge_id" in json.loads(line)]

    @staticmethod
    def saved(args):
        runtime, project, _, conversation, _ = args
        return runtime.db.execute("SELECT last_query FROM contexts WHERE project=? AND conversation=?", (project, conversation)).fetchone()[0]

    def test_real_antigravity_exact_plan_followup_retains_task_focus_and_formal_card(self):
        args = self.runtime("antigravity")
        runtime, project, _, conversation, _ = args
        knowledge = self.publish(runtime, project)
        self.assertIn(knowledge, self.formal_ids(self.context(args, "planfocus ledger ordering")))
        before = runtime.task(project, conversation)
        outcome = {}
        text = self.context(args, "按计划推进", _outcome=outcome)
        self.assertIn(knowledge, self.formal_ids(text))
        self.assertEqual(self.saved(args), "planfocus ledger ordering")
        self.assertFalse(outcome["cooling"])
        after = runtime.task(project, conversation)
        for field in ("id", "revision", "goal", "write_version", "status", "scope", "execution_epoch"):
            self.assertEqual(after[field], before[field])

    def test_whole_subject_preserving_plan_actions_are_recognized(self):
        for phrase in ("按计划推进", "按照计划继续推进", "请按原计划执行吧", "照原计划继续", "按既定方案实施",
                       "按上述方案继续推进并检查", "好的，按这个计划推进", "已重启，按计划继续推进",
                       "继续按计划执行", "按当前安排继续", "proceed as planned", "continue as planned",
                       "please proceed according to the plan", "proceed with the existing plan"):
            with self.subTest(phrase=phrase):
                self.assertTrue(is_continuation_query(phrase))

    def test_subject_negation_plan_numbers_versions_paths_and_new_plan_are_not_discarded(self):
        for phrase in ("按计划推进数据库迁移", "按计划不要推进", "不要按计划推进", "按计划推进但不发布",
                       "按计划2推进", "按第2个计划推进", "按方案A推进", "按计划 v2.1 推进", "按计划推进 src/main.py",
                       "按另一个计划推进", "按新计划执行", "按旧计划继续", "按计划", "按照计划讨论SQLite",
                       "proceed as planned with Redis", "do not proceed as planned", "proceed with plan B",
                       "proceed with the new plan", "proceed according to plan 2"):
            with self.subTest(phrase=phrase):
                self.assertFalse(is_continuation_query(phrase))

    def test_three_host_persistence_and_compaction_keep_expected_delivery(self):
        for host in ("codex", "antigravity", "dsh"):
            with self.subTest(host=host):
                args = self.runtime(host)
                runtime, project, _, _, _ = args
                knowledge = self.publish(runtime, project)
                self.assertIn(knowledge, self.formal_ids(self.context(args, "planfocus ledger ordering")))
                followup = self.formal_ids(self.context(args, "按计划推进"))
                self.assertEqual(knowledge in followup, host == "antigravity")
                self.assertIn(knowledge, self.formal_ids(self.context(args, "按计划推进", compact=True)))
                self.assertEqual(self.saved(args), "planfocus ledger ordering")

    def test_new_task_uses_new_goal_instead_of_previous_query(self):
        args = self.runtime("antigravity")
        runtime, project, _, conversation, _ = args
        old = self.publish(runtime, project)
        new = self.publish(runtime, project, "nextfocus canopy metrics")
        self.context(args, "planfocus ledger ordering")
        runtime.task_update(project, conversation, "nextfocus canopy metrics", new_task=True)
        text = self.context(args, "按计划推进")
        self.assertIn(new, self.formal_ids(text))
        self.assertNotIn(old, self.formal_ids(text))
        self.assertEqual(self.saved(args), "nextfocus canopy metrics")

    def test_explicit_new_subject_remains_the_query_and_missing_native_input_does_not_guess(self):
        args = self.runtime("antigravity")
        runtime, project, _, conversation, _ = args
        self.publish(runtime, project)
        self.context(args, "planfocus ledger ordering")
        query = "按计划推进 certificate v2.1"
        with patch.object(runtime, "search", return_value={"items": []}) as search:
            self.context(args, query)
        self.assertEqual(search.call_args.args[1], query)
        self.assertEqual(self.saved(args), query)
        runtime.task_update(project, conversation, "planfocus ledger ordering", status="delivered")
        with patch.object(runtime, "search", side_effect=AssertionError("missing input inferred a continuation")):
            self.assertEqual(self.formal_ids(self.context(args, "", _query_available=False)), [])
        self.assertEqual(self.saved(args), query)

    def test_unfocused_plan_followup_does_not_select_recent_history_or_resume_task(self):
        args = self.runtime("antigravity")
        runtime, project, _, conversation, _ = args
        self.publish(runtime, project)
        runtime.task_update(project, conversation, "按计划推进", new_task=True)
        before = runtime.task(project, conversation)
        with patch.object(runtime, "search", side_effect=AssertionError("generic plan became a topic")):
            self.assertEqual(self.formal_ids(self.context(args, "按计划推进")), [])
        self.assertEqual(self.saved(args), "")
        self.assertEqual(runtime.task(project, conversation), before)


if __name__ == "__main__":
    unittest.main()
