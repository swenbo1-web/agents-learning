"""完整目标才构成潜在恢复；通过真实案例/管理员链验证，不执行任何模型。"""
import json
from contextlib import closing
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.admin import admin_once
from agent_learning.event_capture import _targets_compatible
from agent_learning.runtime import Runtime
from tests.test_module6_queue import fake_reviewer


class RecoveryTargetBoundaryTests(unittest.TestCase):
    def test_complete_script_and_module_targets_must_match(self):
        pairs = [
            ("python service_a/test.py", "python service_b/test.py"),
            ("python -m unittest tests.alpha --verbose", "python -m unittest tests.beta --verbose"),
            ("pytest tests/a.py tests/b.py", "pytest tests/a.py tests/c.py"),
            ("python a.py && python b.py", "python a.py && python c.py"),
            ("node src/check.js --quick", "node other/check.js --full"),
            ("python -c 'print(1)'", "python -c 'print(2)'"),
        ]
        if os.name != "nt":
            pairs.append(("python src/Test.py", "python src/test.py"))
        for first, second in pairs:
            with self.subTest(first=first, second=second):
                self.assertFalse(_targets_compatible({"command": first}, {"command": second}))

    def test_same_target_parameter_changes_allow_potential_recovery(self):
        for first, second in [
            ("pytest tests/test_runner.py --bad", "pytest tests/test_runner.py --full"),
            ("python ./src/a.py --bad", "python src/a.py --fixed"),
            ('python "src/path with spaces/a.py" --bad', 'python "src/path with spaces/a.py" --fixed'),
            ("python -m unittest tests.alpha --verbose", "python -m unittest tests.alpha --quiet"),
            ("python -m pytest tests/a.py --maxfail 1", "pytest tests/a.py --maxfail 2"),
        ]:
            with self.subTest(first=first):
                self.assertTrue(_targets_compatible({"command": first}, {"command": second}))
        self.assertFalse(_targets_compatible({"cmd": "python test.py", "workdir": "service_a"},
                                             {"cmd": "python test.py", "workdir": "service_b"}))
        self.assertFalse(_targets_compatible({"CommandLine": "pytest a.py", "Cwd": "a"},
                                             {"CommandLine": "pytest a.py", "Cwd": "b"}))
        self.assertFalse(_targets_compatible({"cmd": "python test.py --old", "cwd": "service_a"},
                                             {"cmd": "python test.py --new"}))
        self.assertFalse(_targets_compatible({"cmd": "python test.py", "cwd": "service_a"},
                                             {"cmd": "python test.py"}))
        self.assertTrue(_targets_compatible({"cmd": "python test.py --old", "Cwd": "service_a"},
                                            {"cmd": "python test.py --new", "workdir": "service_a"}))

    def test_unrelated_success_cannot_recover_or_publish_failure(self):
        for failed, succeeded in [
            ("python service_a/test.py", "python service_b/test.py"),
            ("python -m unittest tests.alpha --verbose", "python -m unittest tests.beta --verbose"),
        ]:
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "project"
                workspace.mkdir()
                with closing(Runtime(Path(directory) / "home")) as runtime:
                    project = runtime.project(workspace)
                    runtime.task_update(project, "chat", "验证测试错误")
                    runtime.config["admin"].update(enabled=True, auto_publish=True, command=[sys.executable])
                    for ident, command, code in [("failure", failed, 1), ("success", succeeded, 0)]:
                        shared = dict(session_id="chat", cwd=str(workspace), tool_use_id=ident,
                                      tool_name="Bash", tool_input={"command": command})
                        runtime.handle_hook(dict(shared, hook_event_name="PreToolUse"))
                        runtime.handle_hook(dict(shared, hook_event_name="PostToolUse", tool_response={"exit_code": code}))
                    problem = runtime.problems(project)[0]
                    self.assertEqual(problem["resolution_status"], "open")
                    self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM problem_events WHERE problem=? "
                                                       "AND role='potential_recovery'", (problem["id"],)).fetchone()[0], 0)
                    base = fake_reviewer("candidate")

                    class ClaimToolSuccess(base):
                        def communicate(self, data=None, timeout=None):
                            super().communicate(data, timeout)
                            response = json.loads(self.output.read_text(encoding="utf-8"))
                            response["candidate"]["validation_level"] = "tool_success"
                            self.output.write_text(json.dumps(response), encoding="utf-8")

                    with patch("agent_learning.admin.subprocess.Popen", ClaimToolSuccess):
                        result = admin_once(runtime, project, problem["id"])
                    self.assertNotEqual(result.get("publication", {}).get("status"), "published")
                    self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM active WHERE project=?", (project,)).fetchone()[0], 0)
