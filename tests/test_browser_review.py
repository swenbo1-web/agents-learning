"""Real isolated HTTP checks for explicit browser approval."""
import contextlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_learning.knowledge_browser import make_browser_server
from agent_learning.runtime import Runtime


class BrowserReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-browser-review-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.workspace = root / "project"
        self.workspace.mkdir()
        self.home = root / "home"
        self.runtime = Runtime(self.home, config={})
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.project(self.workspace)
        self.runtime.task_update(self.project, "chat", "Review observable project facts")
        self.server = make_browser_server(self.home / "runtime.sqlite3", self.project,
                                          review=True, language="en")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, path, body=None, *, headers=None, method=None):
        supplied = {"Authorization": "Bearer " + self.server.access_token,
                    "X-Review-Token": self.server.review_token,
                    "Origin": self.server.origin, "Content-Type": "application/json"}
        supplied.update(headers or {})
        supplied = {k: v for k, v in supplied.items() if v is not None}
        data = json.dumps(body) if body is not None else None
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.request(method or ("POST" if body is not None else "GET"), path, data, supplied)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def candidate(self, title="Observable fact"):
        problem = self.runtime.feedback(self.project, "chat", title, category="project")
        refs = [row[0] for row in self.runtime.db.execute(
            "SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        return self.runtime.submit_candidate(self.project, problem_id=problem["id"],
            problem_revision=problem["revision"], title=title, content="Only the cited text is known.",
            category="project", knowledge_kind="fact", applicability="This project and source version",
            limitations="Reported historical text, no behavior verification.", evidence_refs=refs,
            validation_level="reported")["candidate_id"]

    def prepare(self, identities, action="approve"):
        code, response = self.request("/api/review/prepare", {"candidate_ids": identities, "action": action})
        self.assertEqual(code, 200, response)
        return response

    def test_session_and_candidates_are_readable_without_write_capability(self):
        identity = self.candidate()
        code, session = self.request("/api/session", headers={"X-Review-Token": None})
        self.assertEqual(code, 200)
        self.assertTrue(session["review_enabled"])
        self.assertEqual(session["language"], "en")
        self.assertNotIn(self.server.review_token, json.dumps(session))
        code, items = self.request("/api/candidates", headers={"X-Review-Token": None})
        self.assertEqual(code, 200, items)
        self.assertEqual(items["items"][0]["candidate_id"], identity)
        code, detail = self.request("/api/candidate?candidate_id=" + identity)
        self.assertEqual(code, 200, detail)
        self.assertIn("Only the cited text", detail["field"]["text"])
        self.assertTrue(detail["evidence"]["items"])

    def test_read_key_alone_or_cross_origin_cannot_write(self):
        identity = self.candidate()
        for headers in ({"X-Review-Token": None}, {"X-Review-Token": "wrong"},
                        {"Origin": None}, {"Origin": "http://evil.example"},
                        {"Authorization": "Bearer wrong"}):
            with self.subTest(headers=headers):
                status, _ = self.request("/api/review/prepare",
                    {"candidate_ids": [identity], "action": "approve"}, headers=headers)
                self.assertEqual(status, 403)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_batch_publication_and_replay_do_not_invoke_models(self):
        identities = [self.candidate("First fact"), self.candidate("Second fact")]
        with patch("agent_learning.admin.subprocess.Popen") as model:
            preview = self.prepare(identities)
            self.assertTrue(preview["plan"]["ready"])
            self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)
            body = {"plan_id": preview["plan_id"], "request_id": "http-test-once"}
            first = self.request("/api/review/commit", body)
            second = self.request("/api/review/commit", body)
        model.assert_not_called()
        self.assertEqual(first[0], 200, first)
        self.assertEqual(first, second)
        self.assertEqual(len(first[1]["results"]), 2)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 2)

    def test_preview_cannot_be_replaced_by_client_or_reused_after_change(self):
        identity = self.candidate()
        preview = self.prepare([identity])
        self.runtime.db.execute("UPDATE candidates SET status='deferred' WHERE id=?", (identity,))
        code, _ = self.request("/api/review/commit", {"plan_id": preview["plan_id"], "request_id": "changed"})
        self.assertEqual(code, 400)
        code, _ = self.request("/api/review/commit", {"plan": preview["plan"], "request_id": "forged"})
        self.assertEqual(code, 400)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_reject_and_defer_are_explicit_nonpublication_decisions(self):
        for action, expected in (("defer", "deferred"), ("reject", "rejected")):
            identity = self.candidate(action)
            preview = self.prepare([identity], action)
            code, result = self.request("/api/review/commit",
                {"plan_id": preview["plan_id"], "request_id": action})
            self.assertEqual(code, 200, result)
            self.assertEqual(result["results"][0]["status"], expected)
        self.assertEqual(self.runtime.db.execute("SELECT COUNT(*) FROM active").fetchone()[0], 0)

    def test_durable_receipt_can_resolve_an_unknown_http_result(self):
        identity = self.candidate()
        preview = self.prepare([identity])
        code, empty = self.request("/api/review-receipt?request_id=lost-response")
        self.assertEqual((code, empty), (200, {"found": False}))
        code, result = self.request("/api/review/commit",
            {"plan_id": preview["plan_id"], "request_id": "lost-response"})
        self.assertEqual(code, 200, result)
        self.server.review_plans.clear()
        code, receipt = self.request("/api/review-receipt?request_id=lost-response")
        self.assertEqual(code, 200, receipt)
        self.assertEqual(receipt["plan_digest"], preview["plan"]["plan_digest"])
        self.assertEqual(receipt["response"], result)

    def test_invalid_action_is_a_bounded_client_error(self):
        identity = self.candidate()
        code, _ = self.request("/api/review/prepare", {"candidate_ids": [identity], "action": []})
        self.assertEqual(code, 400)
        code, _ = self.request("/api/review/prepare", {"candidate_ids": [identity], "action": "approve"},
                               headers={"Content-Type": "text/plain"})
        self.assertEqual(code, 400)

    def test_expired_or_missing_candidate_and_oversized_selection(self):
        identity = self.candidate()
        preview = self.prepare([identity])
        self.server.review_plans[preview["plan_id"]]["deadline"] = 0
        code, _ = self.request("/api/review/commit", {"plan_id": preview["plan_id"], "request_id": "expired"})
        self.assertEqual(code, 400)
        code, _ = self.request("/api/review/prepare", {"candidate_ids": ["absent"], "action": "approve"})
        self.assertEqual(code, 404)
        code, _ = self.request("/api/review/prepare", {"candidate_ids": [identity] * 21, "action": "approve"})
        self.assertEqual(code, 400)

    def test_read_only_server_still_refuses_all_writes(self):
        server = make_browser_server(self.home / "runtime.sqlite3", self.project)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            connection.request("POST", "/api/review/prepare", "{}", {"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 405)
            response.read()
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(3)
