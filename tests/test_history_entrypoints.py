"""隔离数据目录中的历史入口、迁移与固定运行副本验收。"""
from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.cli import main
from agent_learning.install import install
from agent_learning.runtime import ContractError, Runtime
from agent_learning.mcp import Server


class HistoryEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-history-entry-")
        self.base = Path(self.temp.name).resolve()
        self.addCleanup(self.cleanup)
        self.workspace = self.base / "project"
        self.workspace.mkdir()
        (self.workspace / "AGENTS.md").write_text("# 已有项目\n构建问题应核对版本和当前配置。", encoding="utf-8")
        self.home = self.base / "knowledge"
        configuration = self.base / "test-config.json"
        configuration.write_text("{}", encoding="utf-8")
        environment = patch.dict(os.environ, {"ALP_HOME": str(self.home), "ALP_CONFIG": str(configuration),
                                             "PYTHONDONTWRITEBYTECODE": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def cleanup(self):
        from agent_learning.release import filesystem_path
        # 只清理本测试创建且仍位于临时根的准确目录，兼容 Windows 长路径。
        assert self.base.parent == Path(tempfile.gettempdir()).resolve()
        assert self.base.name.startswith("alp-history-entry-")
        assert Path(self.temp.name).resolve() == self.base
        if self.base.exists():
            shutil.rmtree(filesystem_path(self.base))
        self.temp.cleanup()

    def invoke(self, args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(["--home", str(self.home), *args])
        return code, json.loads(output.getvalue() or errors.getvalue())

    def test_unconfirmed_or_implicit_workspace_rejected_before_database_creation(self):
        for args in (["history-prepare", "--confirm"],
                     ["--workspace", str(self.workspace), "history-prepare"],
                     ["--workspace", str(self.workspace), "history-run", "unknown"]):
            code, result = self.invoke(args)
            self.assertEqual(code, 1)
            self.assertEqual(result["status"], "error")
            self.assertFalse(self.home.exists())

    def test_explicit_cli_archive_shared_across_hosts_without_model(self):
        prefix = ["--workspace", str(self.workspace)]
        with patch("subprocess.Popen", side_effect=AssertionError("unexpected model or worker")):
            code, prepared = self.invoke([*prefix, "history-prepare", "--confirm"])
            self.assertEqual(code, 0, prepared)
            job_id = prepared["job_id"]
            code, result = self.invoke([*prefix, "--host", "antigravity", "history-run", job_id, "--confirm"])
            self.assertEqual(code, 0, result)
        with closing(Runtime(self.home, config={})) as runtime:
            project = runtime.project(self.workspace)
            self.assertEqual(runtime.history_status(project, job_id)["job"]["job_id"], job_id)
            events = list(runtime.db.execute("SELECT * FROM events WHERE type='HistoryImport'"))
            self.assertTrue(events)
            self.assertTrue(all(row["outcome"] == "reported" and row["task_id"] is None and row["operation"] is None for row in events))
            self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM admin_calls").fetchone()[0], 0)
            self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
            for role in ("worker", "admin"):
                server = Server(runtime, project, role)
                names = set(server.tools)
                self.assertFalse(any(name.startswith("history_") for name in names))

    def test_cached_update_choices_do_not_open_database_or_start_ingestion(self):
        from agent_learning import software_updates as updates
        from tests.test_software_updates import release
        with patch.object(updates, "_fetch", return_value=(200, {}, [release("99.0.0", body="新增项目历史吸收")])):
            code, state = self.invoke(["update-check", "--force"])
        self.assertEqual(code, 0, state)
        self.assertTrue(state["update_available"])
        for args in (["update-notice"], ["update-choice", "snooze"], ["update-choice", "ignore", "--version", "99.0.0"],
                     ["update-choice", "reset"]):
            with patch.object(updates, "_fetch", side_effect=AssertionError("cached choice must not use network")):
                code, result = self.invoke(args)
            self.assertEqual(code, 0, result)
            self.assertFalse((self.home / "runtime.sqlite3").exists())

    def test_cli_source_report_pages_unparsed_materials(self):
        for index in range(43):
            (self.workspace / f"binary-{index}.dat").write_bytes(b"\x00not text")
        prefix = ["--workspace", str(self.workspace)]
        code, prepared = self.invoke([*prefix, "history-prepare", "--confirm"])
        self.assertEqual(code, 0, prepared)
        code, report = self.invoke([*prefix, "history-sources", prepared["job_id"], "--section", "pending", "--offset", "40"])
        self.assertEqual(code, 0, report)
        self.assertEqual(report["total"], 43)
        self.assertEqual(len(report["items"]), 3)
        self.assertIsNone(report["next_offset"])

    def test_sanitized_source_hash_and_public_url_remain_consistent_through_archive(self):
        from agent_learning.history_sources import scan_sources
        from agent_learning.runtime import clean
        url = "https://example.com/page?access_token=secret-original&view=public#section"
        document = self.workspace / "URL-notes.md"
        document.write_text(url, encoding="utf-8")
        scanned = next(source for source in scan_sources(self.workspace)["sources"] if source["path"] == str(document))
        self.assertIn("&view=public#section", scanned["content"])
        self.assertNotIn("secret-original", scanned["content"])
        self.assertEqual(clean(scanned["content"]), scanned["content"])
        self.assertNotIn("private-tail", clean("password=[REDACTED]private-tail"))
        with closing(Runtime(self.home, config={})) as runtime:
            project = runtime.project(self.workspace)
            job = runtime.history_prepare(project, self.workspace)["job_id"]
            material = runtime.db.execute("SELECT body,content_hash FROM history_materials WHERE source_key=?", (scanned["source_key"],)).fetchone()
            self.assertEqual(material["content_hash"], scanned["content_hash"])
            self.assertEqual(json.loads(material["body"])["content"], scanned["content"])
            runtime.history_run(project, job)
            events = [json.loads(row[0]) for row in runtime.db.execute("SELECT payload FROM events WHERE type='HistoryImport'")]
            captured = next(item for item in events if item["source"]["path"] == str(document))
            self.assertEqual(captured["content"], scanned["content"])

    def test_current_job_case_is_accessible_after_old_paused_cases_fill_global_list(self):
        with closing(Runtime(self.home, config={})) as runtime:
            project = runtime.project(self.workspace)
            for index in range(101):
                (self.workspace / f"old-{index}.md").write_text("Previously archived material.", encoding="utf-8")
            old = runtime.history_prepare(project, self.workspace, scan_options={"include": ["old-*.md"]})["job_id"]
            runtime.history_run(project, old)
            runtime.history_pause(project, old)
            runtime.history_defer(project, old, "Explicitly select a new source without reopening paused access")
            (self.workspace / "current.md").write_text("Current selected project fact.", encoding="utf-8")
            current = runtime.history_prepare(project, self.workspace, scan_options={"include": ["current.md"]})["job_id"]
            runtime.history_run(project, current)
        prefix = ["--workspace", str(self.workspace)]
        code, result = self.invoke([*prefix, "history-cases", current])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["total"], 1)
        case = result["items"][0]
        self.assertTrue(case["review_allowed"])
        with closing(Runtime(self.home, config={})) as runtime:
            project = runtime.project(self.workspace)
            packet = runtime.review_packet(project, case["problem_id"], expected_revision=case["revision"])
            candidate = runtime.submit_candidate(project, problem_id=case["problem_id"], problem_revision=case["revision"],
                title="Selected project fact", content="Current selected project fact.", category="project", knowledge_kind="fact",
                applicability="This source version", limitations="Historical statement; not current tool success.",
                evidence_refs=[event["id"] for event in packet["events"]], validation_level="reported")
            runtime.publish(project, candidate["candidate_id"])
            counts = runtime.history_status(project, current)["job"]["counts"]
            self.assertEqual(counts["candidate_cases"], 1)
            self.assertEqual(counts["published_cases"], 1)
            self.assertEqual(counts["review_decision_cases"], 0)
            self.assertEqual(counts["untouched_cases"], 0)
            runtime.history_cancel(project, current)
            knowledge = runtime.read(project, runtime.search(project, "Selected project fact")["items"][0]["knowledge_id"])
            runtime.revoke(project, knowledge["knowledge_id"], expected_revision=knowledge["revision"], reason="Explicit knowledge lifecycle check")
            restored = runtime.restore(project, knowledge["knowledge_id"], expected_revision=knowledge["revision"])
            self.assertEqual(runtime.read(project, restored["knowledge_id"])["validation_level"], "reported")

    def test_schema12_history_migration_failure_is_atomic_and_retry_preserves_project(self):
        runtime = Runtime(self.home, config={})
        project = runtime.project(self.workspace)
        runtime.close()
        with closing(sqlite3.connect(self.home / "runtime.sqlite3", isolation_level=None)) as db:
            for table in ("history_call_links", "history_case_links", "history_units", "history_job_sources",
                          "history_materials", "history_jobs", "project_onboarding"):
                db.execute("DROP TABLE " + table)
            db.execute("PRAGMA user_version=12")
        def interrupted(runtime):
            runtime.db.execute("CREATE TABLE partial_history_migration(id TEXT)")
            raise ContractError("history migration interrupted")
        with patch("agent_learning.history_import.install_storage", side_effect=interrupted):
            with self.assertRaisesRegex(ContractError, "interrupted"):
                Runtime(self.home, config={})
        with closing(sqlite3.connect(self.home / "runtime.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 12)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='partial_history_migration'").fetchone())
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='history_jobs'").fetchone())
        with closing(Runtime(self.home, config={})) as runtime:
            self.assertEqual(runtime.db.execute("PRAGMA user_version").fetchone()[0], 23)
            self.assertEqual(runtime.project(self.workspace), project)

    def test_both_exports_contain_explicit_history_skill_and_fixed_cli(self):
        for host in ("codex", "antigravity"):
            destination = self.base / (host + " plugin")
            receipt = install(destination, runtime_store=self.base / "releases", host=host,
                              **({"workspace": self.workspace, "home": self.home} if host == "antigravity" else {}))
            self.assertFalse(self.home.exists())
            self.assertFalse(receipt["history_import"]["automatic"])
            self.assertTrue((destination / "skills/learning-history/SKILL.md").is_file())
            self.assertIn("allow_implicit_invocation: false", (destination / "skills/learning-history/agents/openai.yaml").read_text(encoding="utf-8"))
            entry = Path(receipt["runtime_directory"]) / "scripts/learning.py"
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", ALP_DISABLED="1", ALP_HOME=str(self.home))
            env.pop("ALP_CONFIG", None)
            result = subprocess.run([sys.executable, "-B", str(entry), "--home", str(self.home),
                                     "--workspace", str(self.workspace), "--host", host,
                                     "history-status"], env=env, cwd=self.base, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            self.assertFalse(json.loads(result.stdout)["automatic_resume"])
            # The explicit status initialized its data directory; remove only test-owned data.
            import shutil
            shutil.rmtree(self.home)

    def test_deep_fixed_release_resources_validate_and_upgrade_for_both_hosts(self):
        from agent_learning.install_update import inspect_installation
        from agent_learning.install import _hook_commands
        from agent_learning.release import validate_release_root, filesystem_path
        store = self.base / ("nested runtime store " + "x" * 40)
        for host in ("codex", "antigravity"):
            target = self.base / ("deep " + host + " plugin")
            parameters = {"workspace": self.workspace, "home": self.home} if host == "antigravity" else {}
            receipt = install(target, runtime_store=store, host=host, **parameters)
            fixed = Path(receipt["runtime_directory"])
            nested = fixed / "host_resources/antigravity/skills/learning-history/agents/openai.yaml"
            if os.name == "nt":
                self.assertGreater(len(str(nested)), 260)
            self.assertTrue(filesystem_path(nested).is_file())
            self.assertEqual(validate_release_root(fixed)["release_id"], receipt["release_id"])
            inspected = inspect_installation(target, _hook_commands, target_host=host)
            self.assertEqual(inspected["release"]["release_id"], receipt["release_id"])
            updated = install(target, runtime_store=store, host=host, upgrade=True, **parameters)
            self.assertEqual(updated["release_id"], receipt["release_id"])
            self.assertFalse(self.home.exists())
            self.assertFalse(str(fixed).startswith("\\\\?\\"))


if __name__ == "__main__":
    unittest.main()
