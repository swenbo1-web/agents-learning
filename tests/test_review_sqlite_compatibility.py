"""Review comparison is a bounded read, including while other SQLite cursors are active."""
import contextlib
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_learning.review_packet import _match_info
from agent_learning.runtime import Runtime


class _NoRegistration:
    """Catch accidental reintroduction of per-query connection mutation."""
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def create_function(self, *args, **kwargs):
        raise AssertionError("review comparison must not register or replace SQLite functions")


class ReviewSQLiteCompatibilityTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "build" / "local" / "fix144-sqlite"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="isolated-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("home", "project", "codex", "tmp", "runtime-releases"):
            (self.root / name).mkdir()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"ALP_HOME": str(self.root / "home"),
            "ALP_CONFIG": str(self.root / "config.json"), "CODEX_HOME": str(self.root / "codex"),
            "ALP_RUNTIME_STORE": str(self.root / "runtime-releases"),
            "PYTHONDONTWRITEBYTECODE": "1",
            **{key: str(self.root / "tmp") for key in ("TMP", "TEMP", "TMPDIR")}}).start()
        patch.object(tempfile, "tempdir", str(self.root / "tmp")).start()
        self.r = Runtime(self.root / "home", config={"admin": {"enabled": False},
                                                   "scheduler": {"enabled": False}})
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.root / "project")
        self.serial = 0
        self.alpha = self.publish("alpha", "beta")
        self.beta = self.publish("beta", "alpha")
        self.third = self.publish("gamma", "unrelated")
        self.problem = self.r.feedback(self.p, "review", "Inspect a comparison", category="operation")

    def publish(self, title, content):
        self.serial += 1
        source = self.r.feedback(self.p, "seed", "seed source " + str(self.serial), category="operation")
        packet = self.r.review_packet(self.p, source["id"])
        candidate = self.r.submit_candidate(self.p, problem_id=source["id"],
            problem_revision=source["revision"], title=title, content=content,
            category="operation", applicability="Same environment", limitations="Reported only",
            validation_level="reported", evidence_refs=[event["id"] for event in packet["events"]])
        return self.r.publish(self.p, candidate["candidate_id"])["knowledge_id"]

    def related(self, query, runtime=None):
        knowledge, _ = (runtime or self.r)._review_related(self.p, self.problem["id"], query)
        return [item["knowledge_id"] for item in knowledge]

    def test_active_old_score_cursor_remains_valid_during_new_reviews(self):
        # The old implementation replaced this exact function while its statement was active.
        self.r.db.create_function("alp_packet_score", 1, lambda body: 7)
        cursor = self.r.db.execute("SELECT alp_packet_score(body) FROM knowledge ORDER BY rowid")
        try:
            self.assertEqual(cursor.fetchone()[0], 7)
            self.assertEqual(self.related("alpha")[0], self.alpha)
            self.assertEqual(self.related("beta")[0], self.beta)
            self.assertEqual([row[0] for row in cursor.fetchall()], [7, 7])
        finally:
            cursor.close()

    def test_active_unrelated_cursor_and_forbidden_registration_do_not_block_packet(self):
        db = self.r.db
        cursor = db.execute("SELECT id FROM knowledge ORDER BY id")
        first = cursor.fetchone()[0]
        self.r.db = _NoRegistration(db)
        try:
            self.assertEqual(self.related("alpha")[0], self.alpha)
            packet = self.r.review_packet(self.p, self.problem["id"])
            self.assertFalse(packet["incomplete"])
            self.assertEqual({first, *(row[0] for row in cursor.fetchall())},
                             {self.alpha, self.beta, self.third})
        finally:
            self.r.db = db
            cursor.close()

    def test_alternating_query_weights_never_share_mutable_state(self):
        for _ in range(5):
            self.assertEqual(self.related("alpha")[0], self.alpha)
            self.assertEqual(self.related("beta")[0], self.beta)
            self.assertEqual(self.related("gamma"), [self.third])

    def test_read_only_connection_and_cached_statements_need_no_registration(self):
        uri = (self.root / "home" / "runtime.sqlite3").as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, isolation_level=None,
                                               cached_statements=128)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            statements = []
            db.set_trace_callback(statements.append)
            runtime = object.__new__(Runtime)
            runtime.__dict__.update(self.r.__dict__)
            runtime.db = _NoRegistration(db)
            for query, expected in (("alpha", self.alpha), ("beta", self.beta), ("alpha", self.alpha)):
                self.assertEqual(self.related(query, runtime)[0], expected)
            self.assertFalse(any(sql.lstrip().upper().startswith(
                ("BEGIN IMMEDIATE", "CREATE", "ALTER", "INSERT", "UPDATE", "DELETE", "DROP"))
                for sql in statements), statements)

    def test_independent_connection_queries_do_not_leak_weights_between_threads(self):
        barrier = threading.Barrier(2)
        failures = []

        def check(query, expected):
            try:
                runtime = Runtime(self.root / "home")
                try:
                    barrier.wait(timeout=10)
                    for _ in range(4):
                        if self.related(query, runtime)[0] != expected:
                            failures.append("unexpected " + query + " comparison")
                finally:
                    runtime.close()
            except Exception as exc:
                failures.append(repr(exc))

        threads = [threading.Thread(target=check, args=args)
                   for args in (("alpha", self.alpha), ("beta", self.beta))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])

    def test_comparison_scoring_is_bounded_before_source_expansion(self):
        row = self.r.db.execute("SELECT * FROM knowledge WHERE id=?", (self.alpha,)).fetchone()
        # A large recall result must remain bounded even if the query term is very common.
        # Only the 3 chosen records are expanded through source and lineage health checks.
        rows = [dict(row, id="synthetic-" + str(index)) for index in range(128)]
        row_public = self.r._public(self.p, row)
        expansions = []

        def public(project, selected):
            expansions.append(selected["id"])
            return dict(row_public, knowledge_id=selected["id"])

        with patch("agent_learning.review_packet.lexical_rows", return_value=rows), patch(
                "agent_learning.review_packet._match_info", wraps=_match_info) as score, patch.object(
                self.r, "_public", side_effect=public):
            self.assertEqual(len(self.related("alpha")), 3)
        self.assertEqual(score.call_count, 128)
        self.assertEqual(len(expansions), 3)


if __name__ == "__main__":
    unittest.main()
