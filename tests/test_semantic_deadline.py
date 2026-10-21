"""A slow local HTTP fixture must not turn an optional lookup into an unbounded wait."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_learning.runtime import Runtime
from agent_learning.semantic_retrieval import (_DeadlineSocket, embed_texts,
                                               validate_semantic_config)

HEALTHY_TIMEOUT_SECONDS = 3
SLOW_TIMEOUT_SECONDS = 0.5


class DeadlineEmbeddings:
    def __init__(self):
        self.mode = "normal"
        self.calls = 0
        self.stopping = threading.Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.calls += 1
                body = json.dumps({"embeddings": [[1, 0] for _ in payload["input"]]}).encode()
                headers = ("HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
                           f"Content-Length: {len(body)}\r\n\r\n").encode()
                mode = fixture.mode
                try:
                    if mode == "incomplete_chunk":
                        self.wfile.write(b"HTTP/1.0 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n10\r\n{}")
                    elif mode == "invalid_status":
                        self.wfile.write(b"not-an-http-status\r\n\r\n")
                    elif mode == "headers":
                        fixture.trickle(self.wfile, headers)
                        self.wfile.write(body)
                    else:
                        self.wfile.write(headers)
                        if mode == "body":
                            fixture.trickle(self.wfile, body)
                        else:
                            self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = validate_semantic_config({"enabled": True, "model": "fixture",
            "url": f"http://127.0.0.1:{self.server.server_port}",
            "timeout_seconds": HEALTHY_TIMEOUT_SECONDS})

    def slow_config(self):
        return {**self.config, "timeout_seconds": SLOW_TIMEOUT_SECONDS}

    def trickle(self, output, data):
        for byte in data:
            if self.stopping.is_set():
                break
            output.write(bytes([byte]))
            output.flush()
            if self.stopping.wait(0.03):
                break

    def close(self):
        self.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class SemanticDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.provider = DeadlineEmbeddings()
        self.addCleanup(self.provider.close)

    def test_header_and_body_trickles_obey_total_deadline_and_release_sockets(self):
        original = _DeadlineSocket

        for mode in ("headers", "body"):
            sockets = []

            def socket(*args, **kwargs):
                connection = original(*args, **kwargs)
                sockets.append(connection)
                return connection

            with self.subTest(mode=mode), patch(
                    "agent_learning.semantic_retrieval._DeadlineSocket", side_effect=socket):
                self.provider.mode = mode
                started = time.monotonic()
                with self.assertRaisesRegex(ValueError, "embedding_provider_unavailable"):
                    embed_texts(self.provider.slow_config(), ["retained"])
                # Each byte arrives inside the socket timeout, but their sum cannot
                # extend the absolute request deadline indefinitely. This integration
                # bound allows CI scheduling jitter; the clock test below checks the
                # exact remaining budget independently of server/thread scheduling.
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertTrue(sockets)
                self.assertTrue(all(connection.fileno() == -1 for connection in sockets))

    def test_connect_send_and_receive_share_one_absolute_deadline(self):
        # Exercise the real wrapper while replacing only the OS operations. Each
        # operation must receive what remains, never a fresh per-operation budget.
        with (_DeadlineSocket(deadline=11) as connection,
              patch("agent_learning.semantic_retrieval.time.monotonic",
                    side_effect=[10, 10.25, 10.75, 11]),
              patch("socket.socket.connect") as connect,
              patch("socket.socket.sendall") as send,
              patch("socket.socket.recv_into", return_value=1) as receive):
            connection.connect(("127.0.0.1", 1))
            self.assertAlmostEqual(connection.gettimeout(), 1)
            connection.sendall(b"request")
            self.assertAlmostEqual(connection.gettimeout(), 0.75)
            self.assertEqual(connection.recv_into(bytearray(1)), 1)
            self.assertAlmostEqual(connection.gettimeout(), 0.25)
            with self.assertRaisesRegex(TimeoutError, "embedding deadline exceeded"):
                connection.recv_into(bytearray(1))
            connect.assert_called_once_with(("127.0.0.1", 1))
            send.assert_called_once_with(b"request", 0)
            self.assertEqual(receive.call_count, 1)
        self.assertEqual(connection.fileno(), -1)

    def test_completed_response_cannot_escape_absolute_request_deadline(self):
        config = {**self.provider.config, "timeout_seconds": 1}
        with (patch("agent_learning.semantic_retrieval.time.monotonic",
                    side_effect=[10, 10.25, 11]),
              patch("agent_learning.semantic_retrieval.request.build_opener") as build):
            opener = build.return_value
            response = opener.open.return_value.__enter__.return_value
            response.read.return_value = b'{"embeddings": [[1, 0]]}'
            with self.assertRaisesRegex(ValueError, "embedding_provider_unavailable"):
                embed_texts(config, ["retained"])
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 0.75)
            opener.open.return_value.__exit__.assert_called_once()

    def test_normal_reply_and_literal_localhost_fallback(self):
        config = {**self.provider.config,
                  "url": self.provider.config["url"].replace("127.0.0.1", "localhost")}
        self.assertEqual(embed_texts(config, ["retained"]), [[1, 0]])
        self.assertEqual(self.provider.calls, 1)

    def test_incomplete_http_and_invalid_status_degrade_then_recover(self):
        for mode in ("incomplete_chunk", "invalid_status"):
            with self.subTest(mode=mode):
                self.provider.mode = mode
                with self.assertRaisesRegex(ValueError, "embedding_provider_unavailable"):
                    embed_texts(self.provider.config, ["retained"])
        self.provider.mode = "normal"
        self.assertEqual(embed_texts(self.provider.config, ["retained"]), [[1, 0]])

    def runtime(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = temporary.name
        runtime = Runtime(Path(directory) / "state",
                          config={"retrieval": {"semantic": self.provider.config}})
        self.addCleanup(runtime.close)
        project = runtime.project(Path(directory) / "project")
        runtime.task_update(project, "chat", "review retained memory", {"mode": "analysis"})
        return runtime, project

    def publish(self, runtime, project, title="retained buffers"):
        problem = runtime.feedback(project, "chat", title, category="operation")
        packet = runtime.review_packet(project, problem["id"])
        candidate = runtime.submit_candidate(project, problem_id=problem["id"],
            problem_revision=problem["revision"], title=title,
            content="release retained buffers", category="operation", applicability="same environment",
            limitations="reported only", validation_level="reported",
            evidence_refs=[event["id"] for event in packet["events"]])["candidate_id"]
        return runtime.publish(project, candidate)["knowledge_id"]

    def test_timeout_retains_lexical_fallback_cooldown_and_recovery(self):
        runtime, project = self.runtime()
        expected = self.publish(runtime, project)
        refreshed = runtime.semantic_refresh(project)
        self.assertEqual(refreshed["processed"], 1, refreshed)
        self.provider.mode = "body"
        runtime.config["retrieval"]["semantic"] = self.provider.slow_config()
        result = runtime.search(project, "retained buffers")
        self.assertTrue(result["items"], result)
        self.assertEqual(result["items"][0]["knowledge_id"], expected, result)
        self.assertEqual(result["retrieval"]["semantic_status"], "degraded", result)
        self.assertEqual(result["retrieval"]["reason"], "embedding_provider_unavailable", result)
        calls = self.provider.calls
        cooling = runtime.search(project, "retained buffers")
        self.assertEqual(cooling["retrieval"]["semantic_status"], "degraded", cooling)
        self.assertEqual(self.provider.calls, calls)
        self.assertEqual(runtime.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 0)
        self.provider.mode = "normal"
        runtime.config["retrieval"]["semantic"] = self.provider.config
        # Expire only the failed query's retry timestamp; do not wait or alter
        # the request clock, which must continue enforcing a real deadline.
        for key, (_, reason) in runtime._semantic_query_failures.items():
            runtime._semantic_query_failures[key] = (0, reason)
        recovered = runtime.search(project, "retained buffers")
        self.assertEqual(recovered["retrieval"]["semantic_status"], "ready", recovered)
        self.assertTrue(recovered["items"], recovered)
        self.assertEqual(recovered["items"][0]["knowledge_id"], expected, recovered)
        self.assertEqual(self.provider.calls, calls + 1)

    def test_one_invalid_body_does_not_block_healthy_hybrid_automatic_match(self):
        runtime, project = self.runtime()
        invalid = self.publish(runtime, project, "retained buffers invalid fixture")
        expected = self.publish(runtime, project, "retained buffers healthy fixture")
        refreshed = runtime.semantic_refresh(project)
        self.assertEqual(refreshed["processed"], 2, refreshed)
        runtime.db.execute("UPDATE knowledge SET body=? WHERE project=? AND id=?", ("{", project, invalid))
        result = runtime.search(project, "retained buffers", _automatic=True)
        self.assertEqual([item["knowledge_id"] for item in result["items"]], [expected], result)
        self.assertEqual(result["retrieval"]["semantic_status"], "ready", result)
        self.assertEqual(result["retrieval"]["invalid_vectors"], 1, result)
        # The ordinary full read continues surfacing corruption for diagnosis;
        # this automatic retrieval fallback does not erase the authoritative row.
        with self.assertRaises(json.JSONDecodeError):
            runtime.read(project, invalid)


if __name__ == "__main__":
    unittest.main()
