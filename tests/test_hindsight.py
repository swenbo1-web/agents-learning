"""只使用 localhost 假服务核对 HTTP 合同，不连接真实 Hindsight 或付费接口。"""

from __future__ import annotations

import json
import os
import threading
import traceback
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import URLError

from agent_learning.hindsight import HindsightClient, HindsightError


@contextmanager
def fake_hindsight(*, bank_config=None, retain_response=None, recall_response=None, error_status=None, redirect=False):
    records = []
    resolved = bank_config if bank_config is not None else {
        "retain_strategies": {"alp_chunks": {"retain_extraction_mode": "chunks"}},
        "enable_auto_consolidation": False,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def handle_request(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            records.append({
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "payload": json.loads(body) if body else None,
            })
            if redirect:
                self.send_response(302)
                self.send_header("Location", "/redirect-target")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            failure = error_status.get(self.command) if isinstance(error_status, dict) else error_status
            status = failure or 200
            if failure:
                # 检查远端错误回显凭证时，本地异常仍不会泄漏。
                response = {"detail": "Bearer test-private-key"}
            elif self.command == "GET":
                response = {"bank_id": "published", "config": resolved, "overrides": {}}
            elif self.path.endswith("/recall"):
                response = recall_response if recall_response is not None else {"results": []}
            else:
                response = retain_response if retain_response is not None else {
                    "success": True, "bank_id": "published", "items_count": 1, "async": False,
                }
            data = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = handle_request
        do_POST = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "url": "http://127.0.0.1:" + str(server.server_port),
            "bank_id": "published",
            "timeout_seconds": 2,
        }, records
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class HindsightContractTests(unittest.TestCase):
    def test_unconfigured_does_not_connect(self):
        client = HindsightClient({})
        self.assertFalse(client.configured)
        with patch.object(client._opener, "open") as network:
            with self.assertRaises(HindsightError) as error:
                client.recall("错误", "fps")
        self.assertEqual(error.exception.kind, "unconfigured")
        network.assert_not_called()

    def test_missing_configured_key_fails_before_network(self):
        with fake_hindsight() as (config, records):
            config["api_key_env"] = "ALP_TEST_HINDSIGHT_KEY"
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(HindsightError) as error:
                    HindsightClient(config).recall("错误", "fps")
            self.assertEqual(error.exception.kind, "missing_credentials")
            self.assertEqual(error.exception.write_outcome, "not_sent")
            self.assertEqual(records, [])

    def test_illegal_credentials_are_safe_and_never_reach_network(self):
        client = HindsightClient({"url": "http://127.0.0.1:1", "bank_id": "published",
                                  "api_key_env": "ALP_TEST_HINDSIGHT_KEY"})
        for suffix in ["\npart", "\rpart", "\x00part", "\x7fpart", "\u2603"]:
            key = "private-fixture" + suffix
            with self.subTest(suffix=repr(suffix)), patch("agent_learning.hindsight.os.environ",
                                                        {"ALP_TEST_HINDSIGHT_KEY": key}):
                with patch.object(client._opener, "open") as network:
                    with self.assertRaises(HindsightError) as error:
                        client.retain("k1:r1", "方法", "fps", 1, "op")
                self.assertEqual(error.exception.kind, "invalid_credentials")
                self.assertEqual(error.exception.write_outcome, "not_sent")
                self.assertNotIn("private-fixture", str(error.exception))
                formatted = "".join(traceback.format_exception(error.exception))
                self.assertNotIn("private-fixture", formatted)
                network.assert_not_called()

    def test_unexpected_constructor_error_is_safely_wrapped(self):
        with patch("agent_learning.hindsight.build_opener", side_effect=ValueError("private-fixture")):
            with self.assertRaises(HindsightError) as error:
                HindsightClient({"url": "http://127.0.0.1:1", "bank_id": "published"})
        self.assertEqual(error.exception.kind, "configuration")
        self.assertEqual(error.exception.write_outcome, "not_sent")
        self.assertNotIn("private-fixture", "".join(traceback.format_exception(error.exception)))

    def test_content_encoding_failure_prevents_write(self):
        with fake_hindsight() as (config, records):
            with self.assertRaises(HindsightError) as error:
                HindsightClient(config).retain("k1:r1", "private-fixture\ud800", "fps", 1, "op")
            self.assertEqual(error.exception.kind, "invalid_argument")
            self.assertEqual(error.exception.write_outcome, "not_sent")
            self.assertEqual([record["method"] for record in records], ["GET"])
            self.assertNotIn("private-fixture", "".join(traceback.format_exception(error.exception)))

    def test_network_exceptions_after_write_attempt_have_unknown_outcome(self):
        for exception, kind, retryable in [
            (ValueError("private-fixture"), "request_failed", False),
            (UnicodeEncodeError("ascii", "private-fixture\u2603", 15, 16, "fixture"), "request_failed", False),
            (URLError("private-fixture"), "transport_error", True),
        ]:
            client = HindsightClient({"url": "http://127.0.0.1:1", "bank_id": "published"})
            client._chunks_verified = True
            client._canonical_bank_id = "published"
            with self.subTest(kind=kind), patch.object(client._opener, "open", side_effect=exception) as network:
                with self.assertRaises(HindsightError) as error:
                    client.retain("k1:r1", "方法", "fps", 1, "op")
            self.assertEqual(error.exception.kind, kind)
            self.assertEqual(error.exception.write_outcome, "unknown")
            self.assertEqual(error.exception.retryable, retryable)
            self.assertNotIn("private-fixture", "".join(traceback.format_exception(error.exception)))
            self.assertEqual(network.call_count, 1)

    def test_http_errors_distinguish_preflight_and_write_attempt(self):
        for method, outcome, expected_methods in [
            ("GET", "not_sent", ["GET"]),
            ("POST", "unknown", ["GET", "POST"]),
        ]:
            with self.subTest(method=method), fake_hindsight(error_status={method: 503}) as (config, records):
                with self.assertRaises(HindsightError) as error:
                    HindsightClient(config).retain("k1:r1", "方法", "fps", 1, "op")
                self.assertEqual(error.exception.kind, "http_error")
                self.assertEqual(error.exception.status_code, 503)
                self.assertTrue(error.exception.retryable)
                self.assertEqual(error.exception.write_outcome, outcome)
                self.assertEqual([record["method"] for record in records], expected_methods)

    def test_invalid_write_response_preserves_unknown_outcome(self):
        with fake_hindsight(retain_response=["private-fixture"]) as (config, records):
            with self.assertRaises(HindsightError) as error:
                HindsightClient(config).retain("k1:r1", "方法", "fps", 1, "op")
            self.assertEqual(error.exception.kind, "invalid_response")
            self.assertEqual(error.exception.write_outcome, "unknown")
            self.assertNotIn("private-fixture", str(error.exception))
            self.assertEqual([record["method"] for record in records], ["GET", "POST"])

    def test_retain_contract_and_one_config_read_per_client(self):
        with fake_hindsight() as (config, records):
            config["api_key_env"] = "ALP_TEST_HINDSIGHT_KEY"
            with patch.dict(os.environ, {"ALP_TEST_HINDSIGHT_KEY": "test-private-key"}):
                client = HindsightClient(config)
                first = client.retain("knowledge:k1:r1", "已核实的中文方法。", "fps", 1, "outbox-1")
                client.retain("knowledge:k2:r1", "另一条方法。", "fps", 1, "outbox-2")
            self.assertTrue(first["success"])
            self.assertEqual([record["method"] for record in records], ["GET", "POST", "POST"])
            self.assertEqual(records[0]["path"], "/v1/default/banks/published/config")
            retain = records[1]
            self.assertEqual(retain["path"], "/v1/default/banks/published/memories")
            self.assertEqual(retain["authorization"], "Bearer test-private-key")
            payload = retain["payload"]
            self.assertIs(payload["async"], False)
            self.assertNotIn("operation_id", payload)
            item = payload["items"][0]
            self.assertEqual(item["strategy"], "alp_chunks")
            self.assertEqual(item["content"], "已核实的中文方法。")
            self.assertEqual(item["document_id"], "knowledge:k1:r1")
            self.assertEqual(item["update_mode"], "replace")
            self.assertEqual(item["tags"], ["project:fps"])
            self.assertEqual(item["metadata"], {"project_id": "fps", "revision": "1", "operation_id": "outbox-1"})

    def test_strategy_name_is_configurable_and_can_inherit_chunks(self):
        bank_config = {
            "retain_extraction_mode": "chunks",
            "retain_strategies": {"approved_docs": {}},
            "enable_auto_consolidation": False,
        }
        with fake_hindsight(bank_config=bank_config) as (config, records):
            config["strategy"] = "approved_docs"
            HindsightClient(config).retain("k1:r1", "方法", "fps", "r1", "op")
            self.assertEqual(records[1]["payload"]["items"][0]["strategy"], "approved_docs")

    def test_unsafe_or_missing_strategy_prevents_retain(self):
        examples = [
            {"retain_strategies": {}, "enable_auto_consolidation": False},
            {"retain_strategies": {"alp_chunks": {"retain_extraction_mode": "concise"}}, "enable_auto_consolidation": False},
            {"retain_strategies": {"alp_chunks": {"retain_extraction_mode": "chunks"}}, "enable_auto_consolidation": True},
            {"retain_strategies": {"alp_chunks": {"retain_extraction_mode": "chunks", "enable_auto_consolidation": True}}, "enable_auto_consolidation": False},
        ]
        for bank_config in examples:
            with self.subTest(bank_config=bank_config), fake_hindsight(bank_config=bank_config) as (config, records):
                with self.assertRaises(HindsightError) as error:
                    HindsightClient(config).retain("k1:r1", "方法", "fps", 1, "op")
                self.assertEqual(error.exception.kind, "unsafe_strategy")
                self.assertEqual(error.exception.write_outcome, "not_sent")
                self.assertEqual([record["method"] for record in records], ["GET"])

    def test_recall_uses_project_strict_filter_and_limits_only_client_results(self):
        results = [{"id": str(index), "text": "方法", "metadata": {"revision": "1"}} for index in range(4)]
        with fake_hindsight(recall_response={"results": results, "usage": {"tokens": 1}}) as (config, records):
            response = HindsightClient(config).recall("编译错误", "fps", limit=2)
            self.assertEqual(len(response["results"]), 2)
            self.assertTrue(response["client_truncated"])
            self.assertEqual(response["usage"], {"tokens": 1})
            self.assertEqual(len(records), 1)
            request = records[0]
            self.assertEqual(request["path"], "/v1/default/banks/published/memories/recall")
            self.assertEqual(request["payload"]["tags"], ["project:fps"])
            self.assertEqual(request["payload"]["tags_match"], "all_strict")
            self.assertEqual(request["payload"]["types"], ["world", "experience"])
            self.assertEqual(request["payload"]["budget"], "low")
            self.assertNotIn("limit", request["payload"])

    def test_http_failure_has_safe_status_and_no_retry(self):
        with fake_hindsight(error_status=503) as (config, records):
            config["api_key_env"] = "ALP_TEST_HINDSIGHT_KEY"
            with patch.dict(os.environ, {"ALP_TEST_HINDSIGHT_KEY": "test-private-key"}):
                with self.assertRaises(HindsightError) as error:
                    HindsightClient(config).recall("错误", "fps")
            self.assertEqual(len(records), 1)
            self.assertEqual(error.exception.status_code, 503)
            self.assertTrue(error.exception.retryable)
            self.assertEqual(error.exception.write_outcome, "not_sent")
            self.assertNotIn("test-private-key", str(error.exception))

    def test_redirect_is_not_followed(self):
        with fake_hindsight(redirect=True) as (config, records):
            with self.assertRaises(HindsightError) as error:
                HindsightClient(config).recall("错误", "fps")
            self.assertEqual(error.exception.status_code, 302)
            self.assertEqual(len(records), 1)

    def test_invalid_recall_response_is_not_a_no_match(self):
        with fake_hindsight(recall_response={"unexpected": []}) as (config, records):
            with self.assertRaises(HindsightError) as error:
                HindsightClient(config).recall("错误", "fps")
            self.assertEqual(error.exception.kind, "invalid_response")
            self.assertEqual(len(records), 1)

    def test_async_or_unsuccessful_retain_is_not_confirmed(self):
        for response in [
            {"success": True, "bank_id": "published", "items_count": 1, "async": True},
            {"success": False, "bank_id": "published", "items_count": 1, "async": False},
        ]:
            with self.subTest(response=response), fake_hindsight(retain_response=response) as (config, records):
                with self.assertRaises(HindsightError) as error:
                    HindsightClient(config).retain("k1:r1", "方法", "fps", 1, "op")
                self.assertEqual(error.exception.kind, "unconfirmed_write")
                self.assertEqual(error.exception.write_outcome, "unknown")
                self.assertEqual(len(records), 2)

    def test_invalid_inputs_fail_without_request(self):
        client = HindsightClient({"url": "http://127.0.0.1:1", "bank_id": "published"})
        with patch.object(client._opener, "open") as network:
            for limit in [0, True, 101]:
                with self.subTest(limit=limit), self.assertRaises(HindsightError):
                    client.recall("错误", "fps", limit)
            with self.assertRaises(HindsightError):
                client.retain("k1:r1", "方法", "", 1, "op")
        network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
