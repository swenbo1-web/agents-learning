"""管理员对外接口及接收边界的隔离验证，不调用真实模型。"""
import contextlib
import io
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_batch, admin_once
from agent_learning.admin_decisions import get_reviews
from agent_learning.admin_lifecycle import retry_call
from agent_learning.admin_queue import claim_dispatch
from agent_learning.cli import main
from agent_learning.mcp import Server
from agent_learning.runtime import ContractError, Runtime
from tests.test_module6_queue import fake_reviewer


class Module6IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-module6-entry-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "state"
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.r = Runtime(self.home)
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.task = self.r.task_update(self.p, "chat", "compilerprobe")
        self.r.config["admin"].update(enabled=True, command=[sys.executable], max_calls_per_day=20)

    def problem(self, summary="compilerprobe", **values):
        return self.r.feedback(self.p, "chat", summary, category=values.pop("category", "operation"), **values)

    def candidate(self, problem, **values):
        refs = [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        body = dict(problem_id=problem["id"], problem_revision=problem["revision"], title="compilerprobe",
                    content="compilerprobe", category="operation", applicability="local compiler",
                    limitations="observed only", evidence_refs=refs, validation_level="reported")
        body.update(values)
        return self.r.submit_candidate(self.p, **body)

    def cli(self, *args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = main(["--home", str(self.home), "--workspace", str(self.workspace), *args])
        self.assertEqual(code, 0)
        return json.loads(stream.getvalue())

    def test_admin_mcp_proposes_but_cannot_execute(self):
        problem = self.problem()
        candidate = self.candidate(problem)
        args = dict(problem_id=problem["id"], problem_revision=problem["revision"], action="publish",
                    candidate_id=candidate["candidate_id"], reason="recorded observation")
        worker, admin = Server(self.r, self.p), Server(self.r, self.p, "admin")
        with self.assertRaises(ContractError):
            worker.call("propose_governance", args)
        proposal = admin.call("propose_governance", args)
        with self.assertRaises(ContractError):
            admin.call("governance_execute", {"proposal_id": proposal["proposal_id"]})
        self.assertEqual(self.r.status(self.p)["counts"]["active"], 0)
        result = self.cli("governance-execute", proposal["proposal_id"])
        self.assertEqual(result["state"], "executed")
        self.assertEqual(result, self.cli("governance-execute", proposal["proposal_id"]))

    def test_needs_evidence_reason_survives_restart_and_does_not_repeat(self):
        problem = self.problem()
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("needs_evidence")) as model:
            result = admin_once(self.r, self.p)
            self.assertEqual(result["status"], "needs_evidence")
            admin_batch(self.r, self.p)
            self.assertEqual(model.call_count, 1)
        receipt = self.cli("reviews", "--problem", problem["id"])["items"][0]
        self.assertEqual(receipt["state"], "awaiting_evidence")
        self.assertTrue(receipt["reason"])
        self.assertTrue(receipt["missing_evidence"])

    def test_new_candidate_wakes_compare_but_unrelated_candidate_costs_no_model(self):
        problem = self.problem()
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("needs_evidence")):
            admin_once(self.r, self.p)
        unrelated = self.problem("unrelatedxy")
        self.candidate(unrelated, title="unrelatedxy", content="unrelatedxy")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            self.assertTrue(claim_dispatch(self.r, self.p))
            admin_once(self.r, self.p, problem["id"])
            self.assertEqual(model.call_count, 0)
        self.candidate(unrelated, title="compilerprobe", content="compilerprobe alternative")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            result = admin_once(self.r, self.p, problem["id"])
            self.assertEqual(result["status"], "skip")
            self.assertEqual(model.call_count, 1)

    def test_review_context_changed_during_model_rejects_even_skip(self):
        source = self.problem("source")
        knowledge = self.r.publish(self.p, self.candidate(source)["candidate_id"])
        problem = self.problem()
        def revoke(packet, reviewer):
            self.assertTrue(packet["related_knowledge"])
            self.r.revoke(self.p, knowledge["knowledge_id"], expected_revision=self.r.read(self.p, knowledge["knowledge_id"])["revision"])
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("skip", revoke)):
            result = admin_once(self.r, self.p, problem["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(get_reviews(self.r, self.p, problem["id"])["items"], [])

    def test_unknown_cross_case_cache_wakes_after_explicit_retry(self):
        original = self.problem()
        def fail(packet, reviewer):
            reviewer.returncode = 2
        with patch("agent_learning.admin.subprocess.Popen", fake_reviewer("skip", fail)):
            failed = admin_once(self.r, self.p, original["id"])
        self.r.task_update(self.p, "other", "compilerprobe")
        other = self.r.feedback(self.p, "other", "compilerprobe", category="operation")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            self.assertEqual(admin_once(self.r, self.p, other["id"])["status"], "review_required")
            self.assertEqual(model.call_count, 0)
            retry_call(self.r, self.p, failed["call_id"], "reviewed failure; allow one retry")
            self.assertIsNone(self.r.db.execute("SELECT 1 FROM admin_review_checks WHERE problem=?", (other["id"],)).fetchone())
            self.assertEqual(admin_once(self.r, self.p, original["id"])["status"], "skip")
            result = admin_once(self.r, self.p, other["id"])
            self.assertEqual(result["status"], "skip")
            self.assertEqual(model.call_count, 1)

    def test_auto_publish_restricts_professional_and_pattern(self):
        self.r.config["admin"]["auto_publish"] = True
        for category, kind in (("professional", "solution"), ("operation", "pattern")):
            problem = self.problem(category + kind, category=category)
            event = self.r.record_event(self.p, "chat", "PostToolUse", {"exit_code": 0}, outcome="succeeded",
                                        task_id=self.task["id"], task_revision=self.task["revision"])
            problem = self.problem(category + kind, category=category, event_refs=[event["id"]])
            base = fake_reviewer("candidate")
            class Reviewer(base):
                def communicate(self, data=None, timeout=None):
                    super().communicate(data, timeout)
                    body = json.loads(self.output.read_text(encoding="utf-8"))
                    body["candidate"].update(validation_level="tool_success", knowledge_kind=kind)
                    self.output.write_text(json.dumps(body), encoding="utf-8")
            with patch("agent_learning.admin.subprocess.Popen", Reviewer):
                result = admin_once(self.r, self.p, problem["id"])
            self.assertEqual(result["status"], "candidate")
            self.assertEqual(result["publication"]["reason"], "requires operation solution")
        self.assertEqual(self.r.status(self.p)["counts"]["active"], 0)

    def test_auto_publish_operation_solution_has_published_receipt(self):
        self.r.config["admin"]["auto_publish"] = True
        event = self.r.record_event(self.p, "chat", "PostToolUse", {"exit_code": 0}, outcome="succeeded",
                                    task_id=self.task["id"], task_revision=self.task["revision"])
        problem = self.problem(event_refs=[event["id"]])
        base = fake_reviewer("candidate")
        class Reviewer(base):
            def communicate(self, data=None, timeout=None):
                super().communicate(data, timeout)
                body = json.loads(self.output.read_text(encoding="utf-8"))
                body["candidate"]["validation_level"] = "tool_success"
                self.output.write_text(json.dumps(body), encoding="utf-8")
        with patch("agent_learning.admin.subprocess.Popen", Reviewer):
            result = admin_once(self.r, self.p, problem["id"])
        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(get_reviews(self.r, self.p, problem["id"])["items"][0]["state"], "published")
        self.assertEqual(self.r.status(self.p)["governance_states"], {"executed": 1})

    def test_model_revoke_is_only_a_persisted_proposal(self):
        source = self.problem("source")
        knowledge = self.r.publish(self.p, self.candidate(source)["candidate_id"])
        problem = self.problem("counterexample", knowledge_id=knowledge["knowledge_id"], knowledge_revision=knowledge["revision"])
        base = fake_reviewer("governance")
        class Reviewer(base):
            def communicate(self, data=None, timeout=None):
                super().communicate(data, timeout)
                body = json.loads(self.output.read_text(encoding="utf-8"))
                body["proposal"] = dict(action="revoke", knowledge_id=knowledge["knowledge_id"], expected_revision=knowledge["revision"])
                self.output.write_text(json.dumps(body), encoding="utf-8")
        with patch("agent_learning.admin.subprocess.Popen", Reviewer):
            result = admin_once(self.r, self.p, problem["id"])
        self.assertEqual(result["status"], "governance")
        self.assertEqual(result["state"], "proposed")
        self.assertEqual(self.r.read(self.p, knowledge["knowledge_id"])["pending_counterexample_count"], 1)
        self.assertEqual(get_reviews(self.r, self.p, problem["id"])["items"][0]["state"], "proposal_ready")

    def test_migration_error_closes_connection_and_preserves_retry(self):
        home = Path(self.temp.name) / "failed-migration"
        with patch("agent_learning.admin_governance.install_governance", side_effect=RuntimeError("migration failure")):
            with self.assertRaises(RuntimeError):
                Runtime(home)
        with contextlib.closing(Runtime(home)) as recovered:
            self.assertEqual(recovered.db.execute("PRAGMA user_version").fetchone()[0], 17)

    def test_duplicate_origin_new_evidence_invalidates_without_losing_history(self):
        original = self.problem()
        self.r.task_update(self.p, "other", "compilerprobe")
        duplicate = self.r.feedback(self.p, "other", "compilerprobe", category="operation")
        with patch("agent_learning.admin.subprocess.Popen", side_effect=fake_reviewer("skip")) as model:
            admin_once(self.r, self.p, original["id"])
            reused = admin_once(self.r, self.p, duplicate["id"])
            self.assertTrue(reused["duplicate_of"])
            self.assertEqual(model.call_count, 1)
            self.problem(resolution="new contradictory observation")
            self.assertTrue(self.r.review_packet(self.p, duplicate["id"])["review_context_revision"])
            fresh = admin_once(self.r, self.p, duplicate["id"])
            self.assertEqual(fresh["status"], "skip")
            self.assertEqual(model.call_count, 2)
        self.assertEqual(len(get_reviews(self.r, self.p, duplicate["id"])["items"]), 2)

    def test_multi_turn_evolution_suppresses_in_session_self_anchoring_and_auto_updates_revision(self):
        self.r.config["admin"]["auto_publish"] = True
        # 1. 第 1 轮：命令报错 -> 改参数后同目标脚本成功（且无关的 git status 不误触发恢复）
        self.r.handle_hook({
            "hook_event_name": "PreToolUse", "session_id": "chat", "cwd": str(self.workspace),
            "tool_use_id": "t1", "tool_name": "Bash", "tool_input": {"command": "pytest tests/test_runner.py --bad"},
        })
        self.r.handle_hook({
            "hook_event_name": "PostToolUse", "session_id": "chat", "cwd": str(self.workspace),
            "tool_use_id": "t1", "tool_name": "Bash", "tool_input": {"command": "pytest tests/test_runner.py --bad"},
            "tool_response": {"exit_code": 1, "output": "runner error"},
        })
        prob_row = self.r.db.execute("SELECT id,resolution_status FROM problems ORDER BY created DESC LIMIT 1").fetchone()
        prob_id = prob_row["id"]
        self.assertEqual(prob_row["resolution_status"], "open")

        # 无关命令成功不应误关联为 test_runner.py 的恢复
        self.r.handle_hook({
            "hook_event_name": "PreToolUse", "session_id": "chat", "cwd": str(self.workspace),
            "tool_use_id": "t-unrelated", "tool_name": "Bash", "tool_input": {"command": "git status -sb"},
        })
        self.r.handle_hook({
            "hook_event_name": "PostToolUse", "session_id": "chat", "cwd": str(self.workspace),
            "tool_use_id": "t-unrelated", "tool_name": "Bash", "tool_input": {"command": "git status -sb"},
            "tool_response": {"exit_code": 0, "output": "clean"},
        })
        self.assertEqual(
            self.r.db.execute("SELECT resolution_status FROM problems WHERE id=?", (prob_id,)).fetchone()[0],
            "open",
        )

        # 同目标脚本改参数后执行成功 -> 自动关联为 recovery_observed
        self.r.handle_hook({
            "hook_event_name": "PreToolUse", "session_id": "chat", "cwd": str(self.workspace),
            "tool_use_id": "t2", "tool_name": "Bash", "tool_input": {"command": "pytest tests/test_runner.py --step1"},
        })
        self.r.handle_hook({
            "hook_event_name": "PostToolUse", "session_id": "chat", "cwd": str(self.workspace),
            "tool_use_id": "t2", "tool_name": "Bash", "tool_input": {"command": "pytest tests/test_runner.py --step1"},
            "tool_response": {"exit_code": 0, "output": "1 passed"},
        })
        self.assertEqual(
            self.r.db.execute("SELECT resolution_status FROM problems WHERE id=?", (prob_id,)).fetchone()[0],
            "recovery_observed",
        )

        # 第 1 轮管理员审核并自动发布 v1
        base = fake_reviewer("candidate")
        class PublishV1Reviewer(base):
            def communicate(self, data=None, timeout=None):
                super().communicate(data, timeout)
                body = json.loads(self.output.read_text(encoding="utf-8"))
                body["candidate"].update(
                    title="runner pytest fix",
                    content="Initial v1 fix for test_runner.py",
                    validation_level="tool_success",
                    knowledge_kind="solution",
                )
                self.output.write_text(json.dumps(body), encoding="utf-8")

        with patch("agent_learning.admin.subprocess.Popen", PublishV1Reviewer):
            res_v1 = admin_once(self.r, self.p, prob_id)
        self.assertEqual(res_v1["publication"]["status"], "published")
        kid = res_v1["publication"]["knowledge_id"]
        self.assertEqual(res_v1["publication"]["revision"], 1)

        # 2. 第 2 轮：模型深入测试时同一目标再次报错（振荡回退）
        self.r.handle_hook({
            "hook_event_name": "PreToolUse", "session_id": "chat", "cwd": str(self.workspace),
            "tool_use_id": "t3", "tool_name": "Bash", "tool_input": {"command": "pytest tests/test_runner.py --full"},
        })
        self.r.handle_hook({
            "hook_event_name": "PostToolUse", "session_id": "chat", "cwd": str(self.workspace),
            "tool_use_id": "t3", "tool_name": "Bash", "tool_input": {"command": "pytest tests/test_runner.py --full"},
            "tool_response": {"exit_code": 1, "output": "thinkingBudget error on full run"},
        })
        regressed_row = self.r.db.execute(
            "SELECT status,resolution_status,knowledge_id FROM problems WHERE id=?", (prob_id,)
        ).fetchone()
        self.assertEqual(tuple(regressed_row), ("pending", "open", kid))

        # 防反哺佐证验证：当前会话 chat 正在重修该问题时，自动检索不会把过时的 v1 注入回当前会话误导模型
        h_chat = self.r.register_context(self.p, "chat")["context_handle"]
        injected_in_same_session = self.r._context(
            self.p, "chat", "runner pytest fix", context_handle=h_chat, workspace=str(self.workspace)
        )
        self.assertNotIn("Initial v1 fix for test_runner.py", injected_in_same_session)

        # 振荡回退守卫验证：在未取得新的成功验证前，禁止把振荡失败中的案例自动发布
        with patch("agent_learning.admin.subprocess.Popen", PublishV1Reviewer):
            blocked_res = admin_once(self.r, self.p, prob_id)
        self.assertEqual(blocked_res["publication"]["status"], "not_published")
        self.assertEqual(blocked_res["publication"]["reason"], "latest observation is unresolved failure")

        # 3. 第 3 轮：修改文件并重新通过完整测试 -> 自动将 v1 迭代升级为 v2
        target_file = self.workspace / "runner.mjs"
        target_file.write_text("export const v = 2;\n", encoding="utf-8")
        self.r.handle_hook({
            "hook_event_name": "PreToolUse", "session_id": "chat", "cwd": str(self.workspace),
            "tool_use_id": "t4-edit", "tool_name": "edit", "tool_input": {"file_path": str(target_file)},
        })
        self.r.handle_hook({
            "hook_event_name": "PostToolUse", "session_id": "chat", "cwd": str(self.workspace),
            "tool_use_id": "t4-edit", "tool_name": "edit", "tool_input": {"file_path": str(target_file)},
            "tool_response": {"success": True, "output": "updated runner.mjs"},
        })
        self.r.handle_hook({
            "hook_event_name": "PreToolUse", "session_id": "chat", "cwd": str(self.workspace),
            "tool_use_id": "t5-ok", "tool_name": "Bash", "tool_input": {"command": "pytest tests/test_runner.py --full"},
        })
        self.r.handle_hook({
            "hook_event_name": "PostToolUse", "session_id": "chat", "cwd": str(self.workspace),
            "tool_use_id": "t5-ok", "tool_name": "Bash", "tool_input": {"command": "pytest tests/test_runner.py --full"},
            "tool_response": {"exit_code": 0, "output": "all full tests passed"},
        })
        self.assertEqual(
            self.r.db.execute("SELECT resolution_status FROM problems WHERE id=?", (prob_id,)).fetchone()[0],
            "recovery_observed",
        )

        class UpdateV2Reviewer(base):
            def communicate(self, data=None, timeout=None):
                super().communicate(data, timeout)
                body = json.loads(self.output.read_text(encoding="utf-8"))
                body["candidate"].update(
                    title="runner pytest fix",
                    content="Complete v2 fix with reasoningEfforts false for test_runner.py",
                    validation_level="tool_success",
                    knowledge_kind="solution",
                )
                body["proposal"] = {"action": "update", "knowledge_id": kid, "expected_revision": 1}
                self.output.write_text(json.dumps(body), encoding="utf-8")

        with patch("agent_learning.admin.subprocess.Popen", UpdateV2Reviewer):
            res_v2 = admin_once(self.r, self.p, prob_id)
        self.assertEqual(res_v2["publication"]["status"], "published")
        self.assertEqual(res_v2["publication"]["knowledge_id"], kid)
        self.assertEqual(res_v2["publication"]["revision"], 2)
        self.assertEqual(self.r.read(self.p, kid)["revision"], 2)
        self.assertIn("Complete v2 fix", self.r.read(self.p, kid)["content"])

    def test_day_n_cross_session_file_modification_flags_file_drift_and_allows_same_file_auto_update(self):
        self.r.config["admin"]["auto_publish"] = True
        target_file = self.workspace / "service.py"
        target_file.write_text("print('v1')\n", encoding="utf-8")

        # Day 1 会话：修改 service.py 并发布 v1 正式知识
        self.r.handle_hook({
            "hook_event_name": "PreToolUse", "session_id": "chat-day1", "cwd": str(self.workspace),
            "tool_use_id": "d1-edit", "tool_name": "edit", "tool_input": {"file_path": str(target_file)},
        })
        self.r.handle_hook({
            "hook_event_name": "PostToolUse", "session_id": "chat-day1", "cwd": str(self.workspace),
            "tool_use_id": "d1-edit", "tool_name": "edit", "tool_input": {"file_path": str(target_file)},
            "tool_response": {"exit_code": 0, "output": "edited service.py"},
        })
        ev_d1 = self.r.db.execute("SELECT id FROM events WHERE conversation='chat-day1' AND type='PostToolUse'").fetchone()[0]
        prob_d1 = self.r.feedback(self.p, "chat-day1", "service startup config", category="operation",
                                  resolution="Set v1 config", event_refs=[ev_d1])

        base = fake_reviewer("candidate")
        class Day1Reviewer(base):
            def communicate(self, data=None, timeout=None):
                super().communicate(data, timeout)
                body = json.loads(self.output.read_text(encoding="utf-8"))
                body["candidate"].update(
                    title="service startup config",
                    content="Day 1 v1 configuration for service.py",
                    validation_level="tool_success",
                    knowledge_kind="solution",
                )
                self.output.write_text(json.dumps(body), encoding="utf-8")

        with patch("agent_learning.admin.subprocess.Popen", Day1Reviewer):
            res1 = admin_once(self.r, self.p, prob_d1["id"])
        self.assertEqual(res1["publication"]["status"], "published")
        kid = res1["publication"]["knowledge_id"]

        # Day N 全新会话 chat-day2：磁盘上的 service.py 发生修改后，检索卡片自动标记 source_files_changed: true
        target_file.write_text("print('v2-modified-on-day-n')\n", encoding="utf-8")
        h2 = self.r.register_context(self.p, "chat-day2")["context_handle"]
        ctx_text = self.r._context(self.p, "chat-day2", "service startup config",
                                   context_handle=h2, workspace=str(self.workspace))
        self.assertIn('"source_files_changed":true', ctx_text)

        # Day N 在 chat-day2 中对同一文件 service.py 完成新修改并通过验证，自动召回 K@v1 并升级为 K@v2
        self.r.handle_hook({
            "hook_event_name": "PreToolUse", "session_id": "chat-day2", "cwd": str(self.workspace),
            "tool_use_id": "d2-edit", "tool_name": "edit", "tool_input": {"file_path": str(target_file)},
        })
        self.r.handle_hook({
            "hook_event_name": "PostToolUse", "session_id": "chat-day2", "cwd": str(self.workspace),
            "tool_use_id": "d2-edit", "tool_name": "edit", "tool_input": {"file_path": str(target_file)},
            "tool_response": {"exit_code": 0, "output": "updated service.py to v2"},
        })
        ev_d2 = self.r.db.execute("SELECT id FROM events WHERE conversation='chat-day2' AND type='PostToolUse'").fetchone()[0]
        prob_d2 = self.r.feedback(self.p, "chat-day2", "completely different wording on friday", category="operation",
                                  resolution="Refined v2 config", event_refs=[ev_d2])
        pkt_d2 = self.r.review_packet(self.p, prob_d2["id"])
        related_ids = [item["knowledge_id"] for item in pkt_d2["related_knowledge"]]
        self.assertIn(kid, related_ids)

        class Day2UpdateReviewer(base):
            def communicate(self, data=None, timeout=None):
                super().communicate(data, timeout)
                body = json.loads(self.output.read_text(encoding="utf-8"))
                body["candidate"].update(
                    title="service startup config",
                    content="Day N v2 refined configuration for service.py",
                    validation_level="tool_success",
                    knowledge_kind="solution",
                )
                body["proposal"] = {"action": "update", "knowledge_id": kid, "expected_revision": 1}
                self.output.write_text(json.dumps(body), encoding="utf-8")

        with patch("agent_learning.admin.subprocess.Popen", Day2UpdateReviewer):
            res2 = admin_once(self.r, self.p, prob_d2["id"])
        self.assertEqual(res2["publication"]["status"], "published")
        self.assertEqual(res2["publication"]["revision"], 2)
        self.assertEqual(self.r.read(self.p, kid)["revision"], 2)


if __name__ == "__main__":
    unittest.main()
