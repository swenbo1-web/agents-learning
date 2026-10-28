"""A generic execution tool is optional background only for a concrete domain."""
import unittest
from tests import test_tool_business_relevance as fixtures
from agent_learning import candidate_references as refs
from agent_learning.knowledge_index import normalized_query_plan


class ToolContextRoutingTests(unittest.TestCase):
    setUp = fixtures.ToolBusinessRelevanceTests.setUp
    candidate = fixtures.ToolBusinessRelevanceTests.candidate
    publish = fixtures.ToolBusinessRelevanceTests.publish
    automatic_ids = fixtures.ToolBusinessRelevanceTests.automatic_ids
    match = fixtures.ToolBusinessRelevanceTests.match

    def test_domain_answer_does_not_have_to_name_the_execution_tool(self):
        right = self.publish("SQLite rollback preserves the original transaction boundary.")
        wrong = self.publish("exec_command image render chooses a four panel composition.")
        for query in ("exec_command SQLite rollback", "use functions.exec_command to check SQLite rollback"):
            with self.subTest(query=query):
                self.assertIn(right, self.automatic_ids(query))
                self.assertNotIn(wrong, self.automatic_ids(query))

    def test_business_coverage_is_not_satisfied_by_tool_components(self):
        self.assertFalse(self.match("exec_command SQLite rollback recovery policy",
                                    "exec_command image render SQLite.")["auto_eligible"])
        self.assertTrue(self.match("exec_command SQLite rollback recovery policy",
                                   "SQLite rollback recovery policy preserves the ledger.")["auto_eligible"])
        self.assertFalse(self.match("exec_command SQLite rollback", "exec_command SQLite image render",
                                    limitations="Not applicable to rollback")["auto_eligible"])

    def test_error_and_path_anchors_remain_complete(self):
        for query, right, wrong in (
            ("exec_command E_SQLITE_ROLLBACK", "E_SQLITE_ROLLBACK SQLite rollback preserves state.", "SQLite rollback error."),
            ("exec_command src/ledger.py", "src/ledger.py preserves ledger ordering.", "src/other.py handles ordering."),
        ):
            with self.subTest(query=query):
                self.assertTrue(self.match(query, right)["auto_eligible"])
                self.assertFalse(self.match(query, wrong)["auto_eligible"])

    def test_tool_failures_and_standalone_usage_keep_the_named_tool(self):
        for query in ("exec_command timeout", "exec_command permission denied", "exec_command 用法"):
            with self.subTest(query=query):
                self.assertIn("exec_command", normalized_query_plan(query)["exact_anchors"])
                self.assertFalse(self.match(query, "read_file timeout permission denied 用法")["auto_eligible"])

    def test_candidates_use_the_same_domain_only_recall(self):
        right = self.candidate("SQLite rollback preserves transaction boundaries.")
        self.candidate("exec_command image render uses four panels.")
        refs.configure(self.r, self.p, True, 0)
        self.assertEqual([x["candidate_id"] for x in refs.search(self.r, self.p,
                         "exec_command SQLite rollback", limit=3)], [right])

    def test_work_history_uses_the_same_domain_only_recall(self):
        self.r.handle_hook({"hook_event_name": "Stop", "cwd": str(self.workspace),
                           "session_id": "history", "last_assistant_message": "SQLite rollback preserves state."})
        self.assertEqual(len(self.r._work_search_rows(self.p, "exec_command SQLite rollback", automatic=True)), 1)

    def test_namespace_specific_tools_are_not_softened(self):
        query = "custom_sqlite.execute E_SQLITE_ROLLBACK"
        self.assertIn("custom_sqlite.execute", normalized_query_plan(query)["exact_anchors"])
        self.assertFalse(self.match(query, "E_SQLITE_ROLLBACK transaction recovery.")["auto_eligible"])
