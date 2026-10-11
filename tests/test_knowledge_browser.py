"""Real local HTTP and temporary SQLite browser checks; no user database access."""
import contextlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_learning.knowledge_browser import (HTML, JAVASCRIPT, _ReadModel,
    browser_projects, make_browser_server, resolve_browser_project)
from agent_learning.runtime import ContractError, Runtime


class KnowledgeBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-browser-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        self.home = Path(self.temp.name) / "state"
        self.r = Runtime(self.home)
        self.addCleanup(self.r.close)
        self.project = self.r.project(self.workspace)
        other = Path(self.temp.name) / "other"
        other.mkdir()
        self.other = self.r.project(other)
        self.r.task_update(self.project, "chat", "Read observable knowledge")
        self.r.task_update(self.other, "elsewhere", "Other project")
        self.database = self.home / "runtime.sqlite3"
        self.server = make_browser_server(self.database, self.project)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path, *, method="GET", token=True, headers=None, body=None):
        supplied = dict(headers or {})
        if token:
            supplied.setdefault("Authorization", "Bearer " + self.server.access_token)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.request(method, path, body=body, headers=supplied)
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def get(self, path):
        status, _, data = self.request(path)
        self.assertEqual(status, 200, data.decode())
        return json.loads(data)

    def source(self, *, project=None, title="SQLite validation", content="Use inspected settings", metadata=None):
        project = project or self.project
        conversation = "chat" if project == self.project else "elsewhere"
        problem = self.r.feedback(project, conversation, title + " observation", category="operation")
        refs = [row[0] for row in self.r.db.execute("SELECT event FROM problem_events WHERE problem=?", (problem["id"],))]
        candidate = self.r.submit_candidate(project, problem_id=problem["id"], problem_revision=problem["revision"],
            title=title, content=content, category="operation", applicability="Inspected local project",
            limitations="Verify equivalent environment", evidence_refs=refs, validation_level="reported", metadata=metadata or {})
        return problem, candidate["candidate_id"]

    def publish(self, **kwargs):
        _, candidate = self.source(**kwargs)
        return self.r.publish(kwargs.get("project", self.project), candidate)

    def test_reads_require_key_and_key_not_in_static_documents(self):
        for path in ("/api/stats", "/api/events", "/api/admin", "/api/knowledge-list"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, token=False)[0], 401)
        for path in ("/", "/app.js", "/style.css"):
            status, headers, data = self.request(path, token=False)
            self.assertEqual(status, 200)
            self.assertNotIn(self.server.access_token.encode(), data)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn(self.server.access_token, self.server.url)
        self.assertIn("#token=", self.server.browser_url)

    def test_host_origin_and_fetch_boundaries(self):
        for headers in ({"Host": "evil.example"}, {"Host": "localhost:" + str(self.server.server_address[1])},
                        {"Origin": "http://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/stats", headers=headers)[0], 403)
        self.assertEqual(self.request("/api/stats", headers={"Origin": self.server.origin})[0], 200)
        self.assertEqual(self.request("/api/stats", headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.request("/api/stats", headers={"Authorization": "Bearer é"})[0], 401)

    def test_duplicate_security_headers_denied(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            conn.putrequest("GET", "/api/stats")
            conn.putheader("Host", "127.0.0.1:" + str(self.server.server_address[1]))
            conn.putheader("Authorization", "Bearer " + self.server.access_token)
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            conn.close()

    def test_cross_project_identifiers_and_query_parameters_rejected(self):
        own = self.publish(title="Own entry")
        foreign = self.publish(project=self.other, title="Foreign secret")
        for route in ("knowledge", "revisions", "actions", "events"):
            self.assertEqual(self.request("/api/" + route + "?knowledge_id=" + foreign["knowledge_id"])[0], 404)
        for path in ("/api/stats?project=" + self.other,
                     "/api/knowledge-list?project_id=" + self.other,
                     "/api/knowledge?knowledge_id=" + own["knowledge_id"] + "&project=" + self.other):
            self.assertEqual(self.request(path)[0], 400)
        self.assertEqual(self.get("/api/knowledge-list")["total"], 1)
        self.assertEqual(self.get("/api/stats")["knowledge_total"], 1)

    def test_write_methods_denied_and_snapshot_is_read_only(self):
        before = self.r.db.total_changes
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"):
            self.assertEqual(self.request("/api/knowledge", method=method, body="{}", headers={"Content-Type": "application/json"})[0], 405)
        with self.server.snapshot() as db:
            self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("INSERT INTO projects(id) VALUES('forbidden')")
        self.assertEqual(self.r.db.total_changes, before)
        self.assertIsNone(self.r.db.execute("SELECT id FROM projects WHERE id='forbidden'").fetchone())

    def test_literal_search_filters_and_pagination(self):
        self.publish(title="100%_verified", content="literal percent underscore")
        self.publish(title="100 other verified", content="unrelated")
        self.publish(title="Compiler settings", content="local compiler guidance")
        data = self.get("/api/knowledge-list?q=100%25_&limit=1")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["title"], "100%_verified")
        first = self.get("/api/knowledge-list?limit=2&offset=0")
        second = self.get("/api/knowledge-list?limit=2&offset=2")
        self.assertEqual(first["total"], 3)
        self.assertEqual(first["next_offset"], 2)
        self.assertIsNone(second["next_offset"])
        self.assertFalse({x["knowledge_id"] for x in first["items"]} & {x["knowledge_id"] for x in second["items"]})
        self.assertEqual(self.get("/api/knowledge-list?category=professional")["total"], 0)

    def test_parameter_budgets_and_duplicate_parameters(self):
        for query in ("limit=0", "limit=51", "offset=-1", "offset=10001", "offset=true", "offset=NaN",
                      "limit=1&limit=2", "state=unpublished", "category=bad", "kind=bad", "q=" + "a" * 201,
                      "q=%00", "q=a&bad", "unknown=x"):
            with self.subTest(query=query):
                self.assertEqual(self.request("/api/knowledge-list?" + query)[0], 400)
        self.assertEqual(self.request("/api/stats?" + "a" * 4000)[0], 414)

    def test_revocation_retains_history_and_observable_status(self):
        item = self.publish(content="Old body")
        _, candidate = self.source(content="Corrected body")
        self.r.publish(self.project, candidate, knowledge_id=item["knowledge_id"], expected_revision=1)
        path = "/api/knowledge?knowledge_id=" + item["knowledge_id"]
        old = self.get(path + "&revision=1")
        self.assertEqual(old["field"]["text"], "Old body")
        self.assertEqual(old["state"], "historical")
        self.assertEqual(old["active_revision"], 2)
        self.r.revoke(self.project, item["knowledge_id"], 2, reason="Operator correction")
        latest = self.get(path)
        self.assertEqual(latest["revision"], 2)
        self.assertEqual(latest["state"], "revoked")
        self.assertEqual(latest["field"]["text"], "Corrected body")
        self.assertEqual(self.get("/api/knowledge-list")["total"], 0)
        self.assertEqual(self.get("/api/knowledge-list?state=revoked")["total"], 1)
        self.assertEqual(self.get("/api/revisions?knowledge_id=" + item["knowledge_id"])["total"], 2)
        actions = self.get("/api/actions?knowledge_id=" + item["knowledge_id"])
        self.assertEqual([x["action"] for x in actions["items"]], ["revoke", "update", "publish"])
        self.assertEqual(self.get("/api/stats")["knowledge_revoked"], 1)

    def test_body_and_conditions_are_revision_pinned_and_paged(self):
        item = self.publish(content="a" * 9000 + "END")
        path = "/api/knowledge?knowledge_id=" + item["knowledge_id"] + "&revision=1"
        first = self.get(path + "&offset=0&limit=8000")
        _, candidate = self.source(content="New revision")
        self.r.publish(self.project, candidate, knowledge_id=item["knowledge_id"], expected_revision=1)
        last = self.get(path + "&offset=8000&limit=8000")
        self.assertEqual(first["field"]["text"] + last["field"]["text"], "a" * 9000 + "END")
        self.assertEqual(first["revision"], last["revision"])
        self.assertEqual(last["state"], "historical")
        self.assertEqual(self.get(path + "&section=applicability")["field"]["text"], "Inspected local project")
        self.assertEqual(self.get(path + "&section=limitations")["field"]["text"], "Verify equivalent environment")
        for query in ("&revision=NaN", "&revision=true", "&limit=8001", "&section=html", "&offset=1000001"):
            self.assertEqual(self.request(path + query)[0], 400)

    def test_response_uses_one_snapshot_during_concurrent_revocation(self):
        item = self.publish()
        original = _ReadModel._public
        written = []

        def concurrent(reader, project, row, **kwargs):
            if not written:
                with contextlib.closing(sqlite3.connect(self.database)) as db:
                    db.execute("DELETE FROM active WHERE project=? AND id=?", (project, item["knowledge_id"]))
                    db.commit()
                written.append(True)
            return original(reader, project, row, **kwargs)

        with patch.object(_ReadModel, "_public", concurrent):
            page = self.get("/api/knowledge-list")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["state"], "active")
        self.assertEqual(self.get("/api/knowledge-list")["total"], 0)

    def test_source_health_tracks_conflict_without_claiming_acceptance(self):
        operation = "browser-tool"
        base = {"session_id": "chat", "cwd": str(self.workspace), "tool_use_id": operation, "tool_name": "Read"}
        self.r.handle_hook({**base, "hook_event_name": "PreToolUse", "tool_input": {"path": "sample"}})
        self.r.handle_hook({**base, "hook_event_name": "PostToolUse", "tool_response": {"exit_code": 0}})
        event_row = self.r.db.execute("SELECT id,operation FROM events WHERE project=? AND type='PostToolUse' ORDER BY created DESC LIMIT 1", (self.project,)).fetchone()
        event, actual_operation = event_row["id"], event_row["operation"]
        problem = self.r.feedback(self.project, "chat", "Inspected output", category="operation", event_refs=[event])
        candidate = self.r.submit_candidate(self.project, problem_id=problem["id"], problem_revision=problem["revision"],
            title="Verified tool", content="Observed settings", category="operation", applicability="Local",
            limitations="Check current version", evidence_refs=[event], validation_level="tool_success")
        item = self.r.publish(self.project, candidate["candidate_id"])
        url = "/api/knowledge?knowledge_id=" + item["knowledge_id"]
        valid = self.get(url)
        self.assertTrue(valid["current_tool_success"])
        self.assertEqual(valid["source_health"], "valid")
        self.r.db.execute("UPDATE operations SET conflict=1 WHERE project=? AND id=?", (self.project, actual_operation))
        conflicted = self.get(url)
        self.assertFalse(conflicted["current_tool_success"])
        self.assertEqual(conflicted["source_health"], "conflicted")
        self.assertNotIn("accepted", conflicted)
        timeline = self.get("/api/events?knowledge_id=" + item["knowledge_id"])
        self.assertEqual([x["id"] for x in timeline["items"]], [event])

    def test_admin_view_shows_pending_candidates_and_current_review_state(self):
        problem, candidate = self.source()
        self.r.db.execute("INSERT INTO review_states VALUES(?,?,?,?,?,?,?)",
                          (self.project, problem["id"], problem["revision"], "sig", "candidate_ready", "Review observation", 1))
        self.source(project=self.other, title="Foreign candidate")
        data = self.get("/api/admin")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["candidates"][0]["id"], candidate)
        self.assertEqual(data["items"][0]["review_state"], "candidate_ready")
        self.assertTrue(data["items"][0]["review_current"])
        self.assertNotIn("body", data["items"][0]["candidates"][0])

    def test_xss_is_data_and_frontend_has_no_html_execution_sink(self):
        attack = '<img src=x onerror="window.pwned=1"><script>alert(1)</script>'
        item = self.publish(title=attack, content=attack)
        body = self.get("/api/knowledge?knowledge_id=" + item["knowledge_id"])
        self.assertEqual(body["field"]["text"], attack)
        status, headers, _ = self.request("/api/knowledge?knowledge_id=" + item["knowledge_id"])
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        for dangerous in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(dangerous, JAVASCRIPT)
        self.assertIn("textContent", JAVASCRIPT)
        self.assertIn("history.replaceState", JAVASCRIPT)
        self.assertNotIn("localStorage", JAVASCRIPT)
        self.assertNotIn("sessionStorage", JAVASCRIPT)
        self.assertNotIn("https://", HTML + JAVASCRIPT)

    def test_reading_does_not_create_exposure_or_review_records(self):
        item = self.publish()
        tables = ("context_exposures", "learning_exposures", "knowledge_adoptions", "admin_calls", "review_receipts", "metrics", "cache")
        before = {table: self.r.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in tables}
        for path in ("/api/stats", "/api/knowledge-list", "/api/admin", "/api/events",
                     "/api/knowledge?knowledge_id=" + item["knowledge_id"]):
            self.get(path)
        self.assertEqual(before, {table: self.r.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in tables})

    def test_legacy_evidence_remains_visible_without_recovered_source_relation(self):
        item = self.publish()
        refs = self.r.db.execute("SELECT evidence_refs FROM knowledge_sources WHERE project=? AND knowledge_id=?",
                                (self.project, item["knowledge_id"])).fetchone()[0]
        self.r.db.execute("DELETE FROM knowledge_sources WHERE project=? AND knowledge_id=?", (self.project, item["knowledge_id"]))
        timeline = self.get("/api/events?knowledge_id=" + item["knowledge_id"])
        self.assertEqual({event["id"] for event in timeline["items"]}, set(json.loads(refs)))

    def test_constructor_rejects_remote_bind_or_unknown_storage_without_creation(self):
        for host in ("0.0.0.0", "localhost", "::", "::1", "192.168.0.1"):
            with self.assertRaises(ContractError):
                make_browser_server(self.database, self.project, host=host)
        for port in (-1, True, "0", 65536):
            with self.assertRaises(ContractError):
                make_browser_server(self.database, self.project, port=port)
        for token in ("short", "a" * 31, "é" * 40, "a\n" * 20):
            with self.assertRaises(ContractError):
                make_browser_server(self.database, self.project, token=token)
        with self.assertRaises(ContractError):
            make_browser_server(self.database, "unknown")
        missing = Path(self.temp.name) / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            make_browser_server(missing, self.project)
        self.assertFalse(missing.exists())

    def test_resolve_registered_project_only(self):
        child = self.workspace / "child"
        child.mkdir()
        self.assertEqual(resolve_browser_project(self.database, child), self.project)
        self.assertEqual(resolve_browser_project(self.database, child, self.other), self.other)
        self.assertEqual(set(browser_projects(self.database)), {self.project, self.other})
        with self.assertRaises(ContractError):
            resolve_browser_project(self.database, Path(self.temp.name) / "unknown")
        with self.assertRaises(ContractError):
            resolve_browser_project(self.database, child, "unknown")
        self.assertEqual(self.r.db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 2)

    def test_unsupported_schema_is_not_upgraded(self):
        self.r.db.execute("PRAGMA user_version=10")
        with self.assertRaises(ContractError):
            make_browser_server(self.database, self.project)
        self.assertEqual(self.request("/api/stats")[0], 400)
        self.assertEqual(self.r.db.execute("PRAGMA user_version").fetchone()[0], 10)

    def test_invalid_stored_json_fails_without_sensitive_diagnostics(self):
        item = self.publish()
        self.r.db.execute("UPDATE knowledge SET body='not-json' WHERE project=? AND id=?",
                          (self.project, item["knowledge_id"]))
        status, _, data = self.request("/api/knowledge?knowledge_id=" + item["knowledge_id"])
        self.assertEqual(status, 503)
        self.assertNotIn(str(self.database).encode(), data)
        self.assertNotIn(b"Traceback", data)
        self.assertEqual(self.request("/api/stats")[0], 200)


if __name__ == "__main__":
    unittest.main()
