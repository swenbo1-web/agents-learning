"""Historical local acceptance uses existing operator boundaries and receipts."""
import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch

from agent_learning.admin_control import request_receipt
from agent_learning.admin_response_control import apply_history_response_control
from agent_learning.cli import main, parser
from agent_learning.runtime import ContractError
from tests import test_admin_control as fixtures


class HistoryResponseHttpTests(unittest.TestCase):
    setUp = fixtures.AdminControlHttpTests.setUp
    body = fixtures.AdminControlHttpTests.body
    request = fixtures.AdminControlHttpTests.request
    stop = fixtures.AdminControlHttpTests.stop

    def payload(self, **extra):
        return {"request_id": "history-response-http", "call_id": "saved-history-call", "expected_version": 2,
                "expected_job_version": "a" * 64, "expected_session_epoch": self.r.session_epoch(),
                "expected_governance_epoch": self.r.host_epoch(), "acknowledge_boundary": False,
                "reason": "Accept this call's frozen response", "allow_publication": False, "confirmed": True, **extra}

    def test_history_endpoint_has_independent_capability_and_immutable_retrievable_receipt(self):
        result = {"status": "accepted", "models_started": False, "local_recheck": True, "call_id": "saved-history-call"}
        body = self.payload()
        with patch("agent_learning.history_response_recovery.recheck", return_value=result) as recheck, patch("subprocess.Popen") as model:
            code, answer = self.request("/api/admin-control/history-response-recheck", body)
            self.assertEqual(code, 200, answer)
            self.assertEqual(answer["action"], "history_response_recheck")
            self.assertEqual(answer["result"], result)
            self.assertEqual(self.request("/api/admin-control/history-response-recheck", body), (code, answer))
            recheck.assert_called_once()
            self.assertFalse(recheck.call_args.kwargs["allow_publication"])
            self.assertEqual(recheck.call_args.kwargs["expected_job_version"], "a" * 64)
            code, receipt = self.request("/api/admin-control/receipt?request_id=" + body["request_id"])
            self.assertEqual(code, 200, receipt)
            self.assertEqual(receipt, {"found": True, "response": answer})
            changed = self.payload(allow_publication=True)
            self.assertEqual(self.request("/api/admin-control/history-response-recheck", changed)[0], 400)
            recheck.assert_called_once()
            model.assert_not_called()

    def test_history_route_rejects_missing_capability_wrong_origin_epoch_and_extra_fields(self):
        with patch("agent_learning.history_response_recovery.recheck") as recheck, patch("subprocess.Popen") as model:
            for headers in ({"X-Admin-Token": None}, {"Origin": "https://outside.example"}, {"Authorization": None}):
                self.assertEqual(self.request("/api/admin-control/history-response-recheck", self.payload(), **headers)[0], 403)
            for body in (self.payload(expected_session_epoch=999999), self.payload(expected_governance_epoch=999999), self.payload(start=True)):
                self.assertEqual(self.request("/api/admin-control/history-response-recheck", body)[0], 400)
            recheck.assert_not_called()
            model.assert_not_called()

    def test_acceptance_lease_is_independent_and_settings_failure_retries_the_same_core_request(self):
        body = self.payload()
        observations = []

        def local_accept(runtime, project, call_id, **values):
            observations.append((runtime.db.in_transaction, dict(values)))
            return {"status": "accepted", "models_started": False}

        with patch("agent_learning.history_response_recovery.recheck", side_effect=local_accept), patch(
                "agent_learning.admin_control.control_status", side_effect=ContractError("settings read interrupted")):
            with self.assertRaises(ContractError):
                apply_history_response_control(self.r, self.p, body)
        self.assertFalse(request_receipt(self.r, self.p, body["request_id"])["found"])
        with patch("agent_learning.history_response_recovery.recheck", side_effect=local_accept):
            answer = apply_history_response_control(self.r, self.p, body)
        self.assertEqual(answer["status"], "accepted")
        self.assertEqual(observations[0], observations[1])
        self.assertFalse(observations[0][0], "the control transaction must not enclose the core's durable lease")
        self.assertEqual(request_receipt(self.r, self.p, body["request_id"])["response"], answer)


class HistoryResponseCliTests(unittest.TestCase):
    def test_cli_default_is_local_only_and_publication_requires_its_own_flag(self):
        argv = ["--workspace", "D:/selected-project", "history-response-recheck", "call", "--request-id", "local-request",
                "--expected-version", "2", "--expected-job-version", "a" * 64, "--reason", "One local result", "--confirm"]
        args = parser().parse_args(argv)
        self.assertFalse(args.allow_publication)
        self.assertFalse(args.acknowledge_boundary)
        for host in ("codex", "antigravity", "dsh"):
            runtime = MagicMock()
            runtime.project.return_value = "project"
            runtime.session_epoch.return_value = 3
            runtime.host_epoch.return_value = 5
            with patch("agent_learning.cli.Runtime", return_value=runtime), patch(
                    "agent_learning.history_response_recovery.recheck", return_value={"status": "accepted", "models_started": False}) as recheck, \
                    contextlib.redirect_stdout(io.StringIO()) as output, patch("subprocess.Popen") as model:
                self.assertEqual(main(["--host", host, *argv]), 0)
                self.assertFalse(recheck.call_args.kwargs["allow_publication"])
                self.assertFalse(recheck.call_args.kwargs["acknowledge_boundary"])
                self.assertEqual(recheck.call_args.kwargs["expected_job_version"], "a" * 64)
                self.assertFalse(json.loads(output.getvalue())["models_started"])
                model.assert_not_called()
                runtime.close.assert_called_once()

    def test_cli_requires_explicit_workspace_before_initializing_runtime(self):
        args = ["history-response-recheck", "call", "--request-id", "one", "--expected-version", "2", "--expected-job-version", "a" * 64,
                "--reason", "This call", "--confirm"]
        with patch("agent_learning.cli.Runtime") as runtime, contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(args), 1)
        self.assertIn("explicit --workspace", error.getvalue())
        runtime.assert_not_called()
