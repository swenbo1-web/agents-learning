"""历史扫描的独立离线检查：只使用临时资料，不接入用户数据库或模型。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.history_sources import HistorySourceError, safe_segment_end, scan_sources


class HistorySourcesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()

    def write(self, relative, content, *, root=None):
        path = (root or self.project) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")
        return path

    def conversation(self, records, *, suffix=".json", coverage=None):
        if suffix == ".json":
            value = {"messages": records}
            if coverage:
                value["coverage"] = coverage
            body = json.dumps(value, ensure_ascii=False)
        else:
            body = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
        return self.write("conversation" + suffix, body, root=self.root)

    def scan_conversation(self, path, **options):
        return scan_sources(self.project, [{"path": str(path), "kind": "conversation"}], options or None)

    @staticmethod
    def reasons(scan):
        return {entry["reason"] for entry in scan["skipped"]}

    def test_documents_code_and_configuration_are_all_read_in_priority_order(self):
        self.write("source/main.cpp", "int main(){return 0;}")
        self.write("package.toml", "[project]\nname='sample'")
        self.write("Docs/decision.md", "明确决策")
        self.write("README.md", "项目入口")
        scan = scan_sources(self.project)
        self.assertEqual([source["kind"] for source in scan["sources"]], ["document", "document", "configuration", "code"])
        self.assertEqual(len(scan["sources"]), 4)
        self.assertFalse(scan["incomplete"])

    def test_uncommitted_and_gitignored_documents_are_included(self):
        (self.project / ".git").mkdir()
        self.write(".gitignore", "notes/\n")
        path = self.write("notes/uncommitted.md", "未提交的重要历史")
        scan = scan_sources(self.project)
        self.assertIn(str(path), {source["path"] for source in scan["sources"]})
        self.assertIn("excluded_directory", self.reasons(scan))

    def test_full_text_segments_keep_all_characters_and_line_positions(self):
        content = "# 标题\n" + "长行" * 100 + "\n\n末尾\n"
        self.write("Docs/summary.md", content)
        source = scan_sources(self.project, options={"chunk_chars": 17})["sources"][0]
        self.assertEqual("".join(piece["text"] for piece in source["chunks"]), content)
        self.assertTrue(all(len(piece["text"]) <= 17 for piece in source["chunks"]))
        self.assertEqual([piece["index"] for piece in source["chunks"]], list(range(len(source["chunks"]))))
        self.assertEqual(source["chunks"][-1]["locator"]["line_end"], 4)

    def test_source_key_is_stable_but_content_hash_changes_on_overwrite(self):
        path = self.write("README.md", "old")
        first = scan_sources(self.project)["sources"][0]
        path.write_text("new value", encoding="utf-8")
        second = scan_sources(self.project)["sources"][0]
        self.assertEqual(first["source_key"], second["source_key"])
        self.assertNotEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(second["content"], "new value")

    def test_normalized_newlines_and_bom_have_same_material_hash(self):
        first = self.write("a.md", b"\xef\xbb\xbfline one\r\nline two\r\n")
        second = self.write("b.md", "line one\nline two\n")
        scan = scan_sources(self.project)
        sources = {Path(source["path"]).name: source for source in scan["sources"]}
        self.assertEqual(sources[first.name]["content_hash"], sources[second.name]["content_hash"])
        self.assertNotEqual(sources[first.name]["source_key"], sources[second.name]["source_key"])

    def test_sensitive_files_are_not_opened(self):
        names = [".env", ".env.local", "auth.json", "credentials.json", "id_rsa", "private.pem", "real.sqlite"]
        for name in names:
            self.write(name, "DO_NOT_READ")
        with patch("agent_learning.history_sources._read_file", side_effect=AssertionError("sensitive file opened")):
            scan = scan_sources(self.project)
        self.assertEqual(scan["sources"], [])
        self.assertEqual(len(scan["skipped"]), len(names))
        self.assertEqual(self.reasons(scan), {"sensitive_file"})

    def test_credentials_and_private_key_are_redacted_before_snapshot(self):
        sensitive = "sk-proj-" + "x" * 25
        self.write("notes.md", "api_key='quoted-secret'\nBearer bearer-secret\n" + sensitive
                   + "\n-----BEGIN PRIVATE KEY-----\nprivate-payload\n-----END PRIVATE KEY-----\nend")
        scan = scan_sources(self.project)
        text = json.dumps(scan, ensure_ascii=False)
        for value in ("quoted-secret", "bearer-secret", sensitive, "private-payload"):
            self.assertNotIn(value, text)
        self.assertIn("REDACTED", text)
        self.assertTrue(scan["sources"][0]["content"].endswith("end"))

    def test_unterminated_private_key_is_redacted(self):
        self.write("notes.md", "prefix\n-----BEGIN RSA PRIVATE KEY-----\nsecret-to-end")
        text = scan_sources(self.project)["sources"][0]["content"]
        self.assertIn("prefix", text)
        self.assertNotIn("secret-to-end", text)

    def test_url_query_secrets_and_encoded_keys_are_redacted_without_losing_public_query(self):
        self.write("notes.md", "https://example.com/mail?sid=session-value&email_token=email-value&%74oken=encoded-value&view=public#section")
        text = scan_sources(self.project)["sources"][0]["content"]
        for value in ("session-value", "email-value", "encoded-value"):
            self.assertNotIn(value, text)
        self.assertIn("view=public#section", text)

    def test_url_basic_auth_is_redacted(self):
        self.write("notes.md", "https://person:private-pass@example.com/page?q=public")
        text = scan_sources(self.project)["sources"][0]["content"]
        self.assertNotIn("private-pass", text)
        self.assertIn("page?q=public", text)

    def test_build_dependencies_and_fixed_runtime_are_explicitly_excluded(self):
        for directory in ("node_modules", "runtime-releases", "build", ".agents/plugins", ".codex/plugins", ".venv"):
            self.write(directory + "/should-not-read.md", "hidden")
        self.write("Docs/visible.md", "visible")
        scan = scan_sources(self.project)
        self.assertEqual([source["content"] for source in scan["sources"]], ["visible"])
        self.assertEqual(len([entry for entry in scan["skipped"] if entry["reason"] == "excluded_directory"]), 6)

    def test_root_named_build_is_a_valid_explicit_project(self):
        project = self.root / "build"
        project.mkdir()
        self.write("README.md", "own project", root=project)
        self.assertEqual(len(scan_sources(project)["sources"]), 1)

    def test_installation_archives_are_excluded_even_when_explicitly_selected(self):
        for directory in (".agents/agent-learning-installations", ".codex/agent-learning-installations"):
            with self.subTest(directory=directory):
                path = self.write(directory + "/identity/backups/old/notes.md", "generated backup")
                for selected in (path.parent, path):
                    scan = scan_sources(self.project, [{"path": str(selected), "kind": "artifacts"}])
                    self.assertEqual(scan["sources"], [])
                    self.assertIn("excluded_directory", self.reasons(scan))

    def test_binary_and_invalid_utf8_are_unparsed_not_claimed_understood(self):
        self.write("binary.md", b"a\x00b")
        self.write("legacy.txt", b"\xff\xfeinvalid")
        self.write("asset.uasset", b"binary")
        scan = scan_sources(self.project)
        self.assertEqual(scan["sources"], [])
        self.assertTrue(scan["incomplete"])
        self.assertTrue({"unparsed_binary", "unparsed_encoding", "unparsed_file_type"} <= self.reasons(scan))

    def test_include_and_exclude_apply_to_relative_paths(self):
        self.write("README.md", "root")
        self.write("Docs/keep.md", "keep")
        self.write("Docs/private.md", "excluded")
        self.write("src/main.py", "ignored")
        scan = scan_sources(self.project, options={"include": ["**/*.md"], "exclude": ["Docs/private.md"]})
        self.assertEqual({source["content"] for source in scan["sources"]}, {"root", "keep"})

    def test_invalid_selection_and_options_fail_before_reading(self):
        invalid = [{"max_files": True}, {"max_chunks": 0}, {"max_file_bytes": 10 ** 12}, {"unknown": 1}, {"include": ["../outside/*"]}]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(HistorySourceError):
                scan_sources(self.project, options=options)
        with self.assertRaises(HistorySourceError):
            scan_sources(self.project / ".." / "project")
        with self.assertRaises(HistorySourceError):
            scan_sources(self.write("a.md", "document"))

    def test_file_budget_prioritizes_documents_and_records_all_pending_sources(self):
        self.write("a.py", "code")
        self.write("b.py", "code")
        self.write("README.md", "entry")
        scan = scan_sources(self.project, options={"max_files": 1})
        self.assertEqual([source["content"] for source in scan["sources"]], ["entry"])
        self.assertEqual(len(scan["pending"]), 2)
        self.assertEqual(self.reasons(scan), {"budget_files"})

    def test_total_bytes_and_file_bytes_are_hard_and_shared_with_extras(self):
        self.write("README.md", "12345")
        artifact = self.root / "artifacts"
        artifact.mkdir()
        self.write("note.md", "123456", root=artifact)
        scan = scan_sources(self.project, [{"path": str(artifact), "kind": "artifacts"}], {"max_total_bytes": 10})
        self.assertLessEqual(scan["stats"]["bytes_read"], 10)
        self.assertEqual(len(scan["sources"]), 1)
        self.assertIn("budget_total_bytes", self.reasons(scan))
        scan = scan_sources(self.project, options={"max_file_bytes": 4})
        self.assertEqual(scan["sources"], [])
        self.assertIn("budget_file_bytes", self.reasons(scan))

    def test_chunk_budget_never_returns_a_silently_truncated_file(self):
        self.write("README.md", "1234567890")
        scan = scan_sources(self.project, options={"chunk_chars": 2, "max_chunks": 3})
        self.assertEqual(scan["sources"], [])
        self.assertEqual(scan["stats"]["chunks"], 0)
        self.assertIn("budget_chunks", self.reasons(scan))

    def test_redaction_marker_at_1996_survives_default_chunk_boundary(self):
        from agent_learning.runtime import clean
        marker_start = 1996
        prefix = "p" * (marker_start - len("access_token=") - 1) + "\naccess_token="
        self.write("README.md", prefix + "private-value\nend")
        source = scan_sources(self.project)["sources"][0]
        self.assertEqual(source["content"].index("[REDACTED]"), marker_start)
        chunks = source["chunks"]
        self.assertTrue(all(len(chunk["text"]) <= 2000 for chunk in chunks))
        self.assertEqual("".join(chunk["text"] for chunk in chunks), source["content"])
        self.assertEqual("".join(clean(chunk["text"]) for chunk in chunks), source["content"])
        self.assertIn("[REDACTED]", chunks[1]["text"])

    def test_shared_segment_helper_protects_marker_at_job_1496_boundary(self):
        text = "p" * 1496 + "[REDACTED]" + "tail"
        offset, pieces = 0, []
        while offset < len(text):
            end = safe_segment_end(text, offset, 1500)
            pieces.append(text[offset:end])
            self.assertGreater(end, offset)
            self.assertLessEqual(end - offset, 1500)
            offset = end
        self.assertEqual(len(pieces[0]), 1496)
        self.assertTrue(pieces[1].startswith("[REDACTED]"))
        self.assertEqual("".join(pieces), text)

    def test_small_chunk_budget_cannot_split_or_overrun_private_key_marker(self):
        self.write("README.md", "prefix\n-----BEGIN PRIVATE KEY-----\nprivate-value\n-----END PRIVATE KEY-----")
        scan = scan_sources(self.project, options={"chunk_chars": 10})
        self.assertEqual(scan["sources"], [])
        self.assertIn("budget_redaction_marker", self.reasons(scan))
        self.assertNotIn("private-value", json.dumps(scan))

    def test_segment_helper_rejects_budget_smaller_than_marker_or_midmarker_offset(self):
        with self.assertRaisesRegex(HistorySourceError, "cannot preserve"):
            safe_segment_end("[REDACTED]", 0, 9)
        with self.assertRaisesRegex(HistorySourceError, "inside"):
            safe_segment_end("[REDACTED]", 3, 20)
        self.assertEqual(safe_segment_end("[REDACTED]", 0, 10), 10)
        self.assertEqual(safe_segment_end("aa[REDACTED PRIVATE KEY]tail", 0, 10), 2)

    def test_entry_budget_marks_unenumerated_directory_coverage(self):
        for index in range(10):
            self.write(f"doc{index}.md", "x")
        scan = scan_sources(self.project, options={"max_entries": 3})
        self.assertLessEqual(scan["stats"]["entries_seen"], 3)
        self.assertTrue(scan["incomplete"])
        self.assertIn("budget_entries", self.reasons(scan))

    def test_overlapping_explicit_sources_are_read_once(self):
        path = self.write("README.md", "material")
        scan = scan_sources(self.project, [{"path": str(path), "kind": "artifacts"}])
        self.assertEqual(len(scan["sources"]), 1)
        self.assertEqual(scan["stats"]["bytes_read"], len(b"material"))
        self.assertIn("already_selected", self.reasons(scan))

    def test_project_conversation_json_uses_whitelist_without_configuration_fallback(self):
        path = self.write("chat.json", json.dumps({"messages": [{"role": "user", "content": "public"}]}))
        first = scan_sources(self.project)
        self.assertEqual(first["sources"][0]["kind"], "conversation")
        selected = self.scan_conversation(path)
        self.assertEqual(selected["sources"][0]["kind"], "conversation")
        self.assertEqual(len(selected["sources"]), 1)

    def test_structured_messages_keep_roles_timestamps_and_all_text(self):
        path = self.conversation([
            {"role": "user", "content": [{"type": "text", "text": "question"}], "id": "m1", "timestamp": "2026-10-13T00:00:00Z"},
            {"role": "assistant", "content": [{"type": "output_text", "text": "long answer" * 8}]},
            {"role": "tool", "content": "command result"},
        ], coverage="full")
        source = self.scan_conversation(path, chunk_chars=10)["sources"][0]
        self.assertEqual(source["coverage"], "full")
        self.assertEqual({piece["locator"]["role"] for piece in source["chunks"]}, {"user", "assistant", "tool"})
        self.assertEqual(source["chunks"][0]["locator"]["timestamp"], "2026-10-13T00:00:00Z")
        self.assertEqual(source["chunks"][0]["locator"]["message_id"], "m1")
        answer = "".join(piece["text"] for piece in source["chunks"] if piece["locator"]["role"] == "assistant")
        self.assertEqual(answer, "long answer" * 8)

    def test_summary_and_full_conversations_have_different_hash(self):
        path = self.conversation([{"role": "assistant", "content": "same material"}], coverage="summary")
        summary = self.scan_conversation(path)["sources"][0]
        path.write_text(json.dumps({"messages": [{"role": "assistant", "content": "same material"}], "coverage": "full"}), encoding="utf-8")
        full = self.scan_conversation(path)["sources"][0]
        self.assertNotEqual(summary["content_hash"], full["content_hash"])

    def test_repeated_identical_messages_keep_independent_message_positions(self):
        path = self.conversation([{"role": "user", "content": "repeat"}, {"role": "user", "content": "repeat"}])
        source = self.scan_conversation(path)["sources"][0]
        self.assertEqual([piece["locator"]["message_index"] for piece in source["chunks"]], [0, 1])
        self.assertEqual(source["metadata"]["observable_messages"], 2)

    def test_reasoning_blocks_and_analysis_messages_are_excluded_not_persisted(self):
        path = self.conversation([
            {"role": "assistant", "channel": "analysis", "content": "private-analysis-value"},
            {"role": "assistant", "content": [{"type": "thinking", "text": "private-thinking-value"}, {"type": "text", "text": "public answer"}], "reasoning_content": "private-extra-value"},
        ], coverage="full")
        scan = self.scan_conversation(path)
        text = json.dumps(scan)
        for secret in ("private-analysis-value", "private-thinking-value", "private-extra-value"):
            self.assertNotIn(secret, text)
        self.assertEqual(scan["sources"][0]["coverage"], "partial")
        self.assertEqual(scan["sources"][0]["content"], "[assistant]\npublic answer")

    def test_unsupported_content_blocks_are_explicitly_unparsed(self):
        path = self.conversation([{"role": "user", "content": [{"type": "text", "text": "visible"}, {"type": "image", "image_url": "private-image"}]}])
        scan = self.scan_conversation(path)
        self.assertEqual(scan["sources"], [])
        self.assertIn("unparsed_message_block", self.reasons(scan))
        self.assertNotIn("private-image", json.dumps(scan))

    def test_standard_jsonl_and_plain_export_have_conservative_coverage(self):
        path = self.conversation([{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}], suffix=".jsonl")
        source = self.scan_conversation(path)["sources"][0]
        self.assertEqual(source["coverage"], "partial")
        path = self.write("plain.md", "user: maybe\nassistant: unknown", root=self.root)
        source = self.scan_conversation(path)["sources"][0]
        self.assertEqual(source["coverage"], "partial")
        self.assertEqual(source["chunks"][0]["locator"]["role"], "unknown")

    def test_codex_public_jsonl_whitelist_keeps_public_messages_and_tools(self):
        records = [
            {"type": "session_meta", "payload": {"id": "selected-conversation", "cwd": "unused-private-path"}},
            {"type": "response_item", "payload": {"type": "reasoning", "text": "private-reasoning"}},
            {"type": "event_msg", "payload": {"type": "agent_reasoning", "text": "private-event"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "question"}]}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "Read", "arguments": '{"path":"README.md"}'}},
            {"type": "response_item", "payload": {"type": "function_call_output", "output": "public output"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "final", "content": [{"type": "output_text", "text": "final answer"}]}},
        ]
        source = self.scan_conversation(self.conversation(records, suffix=".jsonl"))["sources"][0]
        self.assertEqual(source["metadata"]["format"], "codex-public-jsonl")
        self.assertEqual(source["coverage"], "partial")
        self.assertIn("public output", source["content"])
        text = json.dumps(source)
        for value in ("private-reasoning", "private-event", "unused-private-path"):
            self.assertNotIn(value, text)

    def test_wrapped_public_message_jsonl_does_not_guess_unknown_trajectory(self):
        path = self.conversation([
            {"type": "user", "message": {"content": "question"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "answer"}]}},
        ], suffix=".jsonl")
        source = self.scan_conversation(path)["sources"][0]
        self.assertEqual(source["metadata"]["format"], "public-message-jsonl")
        unknown = self.conversation([{"type": "trajectory", "steps": [{"thought": "private"}]}], suffix=".jsonl")
        scan = self.scan_conversation(unknown)
        self.assertEqual(scan["sources"], [])
        self.assertIn("unparsed_host_record", self.reasons(scan))

    def test_codex_message_inherits_public_outer_record_timestamp(self):
        timestamp = "2026-10-12T09:10:11Z"
        records = [{"timestamp": timestamp, "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "channel": "final",
            "content": [{"type": "output_text", "text": "Visible final"}]}}]
        source = self.scan_conversation(self.conversation(records, suffix=".jsonl"))["sources"][0]
        locator = source["chunks"][0]["locator"]
        self.assertEqual(locator["timestamp"], timestamp)
        self.assertEqual(locator["record_timestamp"], timestamp)
        self.assertEqual(locator["timestamp_source"], "record")

    def test_wrapped_message_inherits_public_outer_record_timestamp(self):
        timestamp = "2026-10-12T09:10:11Z"
        records = [{"timestamp": timestamp, "type": "assistant", "message": {"content": "Visible final"}}]
        source = self.scan_conversation(self.conversation(records, suffix=".jsonl"))["sources"][0]
        self.assertEqual(source["chunks"][0]["locator"]["timestamp"], timestamp)
        self.assertEqual(source["chunks"][0]["locator"]["timestamp_source"], "record")

    def test_conflicting_message_and_record_times_preserve_both_provenance(self):
        inner, outer = "2026-10-12T09:10:11Z", "2026-10-13T09:10:11Z"
        records = [{"timestamp": outer, "type": "assistant", "message": {
            "content": "Visible final", "timestamp": inner}}]
        source = self.scan_conversation(self.conversation(records, suffix=".jsonl"))["sources"][0]
        locator = source["chunks"][0]["locator"]
        self.assertEqual(locator["timestamp"], inner)
        self.assertEqual(locator["record_timestamp"], outer)
        self.assertEqual(locator["timestamp_source"], "message")

    def test_unknown_host_payload_or_role_is_unparsed(self):
        cases = [
            [{"type": "response_item", "payload": {"type": "unknown", "private": "hidden"}}],
            [{"role": "alien", "content": "hidden"}],
            [{"role": "assistant", "content": {"text": "hidden"}}],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                scan = self.scan_conversation(self.conversation(rows, suffix=".jsonl"))
                self.assertEqual(scan["sources"], [])
                self.assertTrue(scan["incomplete"])
                self.assertNotIn("hidden", json.dumps(scan))

    def test_unhashable_malformed_message_metadata_is_safely_unparsed(self):
        for rows in (
                [{"role": [], "content": "hidden"}],
                [{"type": [], "payload": {"private": "hidden"}}],
                [{"type": "response_item", "payload": {"type": [], "private": "hidden"}}]):
            with self.subTest(rows=rows):
                scan = self.scan_conversation(self.conversation(rows, suffix=".jsonl"))
                self.assertEqual(scan["sources"], [])
                self.assertTrue(scan["pending"])
        path = self.write("conversation.json", json.dumps({"messages": [], "coverage": []}), root=self.root)
        self.assertIn("unparsed_conversation_coverage", self.reasons(self.scan_conversation(path)))
        with self.assertRaises(HistorySourceError):
            scan_sources(self.project, [{"path": str(path), "kind": []}])

    def test_marked_windows_junction_is_rejected_without_reading_target(self):
        path = self.write("junction.md", "must-not-read")
        original = Path.lstat
        def marked(current):
            information = original(current)
            if current == path:
                return SimpleNamespace(st_mode=information.st_mode, st_file_attributes=0x400)
            return information
        with patch.object(Path, "lstat", marked), patch("agent_learning.history_sources._read_file", side_effect=AssertionError("junction read")):
            scan = scan_sources(self.project)
        self.assertEqual(scan["sources"], [])
        self.assertIn("link_or_junction", self.reasons(scan))

    def test_symlink_file_and_directory_never_follow_outside_project(self):
        outside = self.write("private.md", "outside-secret", root=self.root)
        try:
            (self.project / "link.md").symlink_to(outside)
            (self.project / "linked-dir").symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("native symlink creation unavailable")
        scan = scan_sources(self.project)
        self.assertEqual(scan["sources"], [])
        self.assertEqual(len(scan["skipped"]), 2)
        self.assertNotIn("outside-secret", json.dumps(scan))

    def test_reading_mid_change_is_pending_and_spent_bytes_are_not_reused(self):
        path = self.write("README.md", "original")
        self.write("b.md", "second")
        original = os.read
        changed = False
        def changing(handle, count):
            nonlocal changed
            data = original(handle, count)
            if not changed:
                changed = True
                info = path.stat()
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
            return data
        with patch("agent_learning.history_sources.os.read", changing):
            scan = scan_sources(self.project, options={"max_total_bytes": 10})
        self.assertEqual(scan["sources"], [])
        self.assertIn("source_changed", self.reasons(scan))
        self.assertIn("budget_total_bytes", self.reasons(scan))
        self.assertEqual(scan["stats"]["bytes_read"], len(b"original"))

    def test_scan_does_not_initialize_runtime_or_execute_source(self):
        self.write("danger.py", "raise AssertionError('not executed')")
        with patch("agent_learning.runtime.Runtime", side_effect=AssertionError("Runtime initialized")):
            scan = scan_sources(self.project)
        self.assertIn("not executed", scan["sources"][0]["content"])

    def test_identical_scan_is_deterministic_and_uses_no_mtime_content_identity(self):
        path = self.write("README.md", "same")
        first = scan_sources(self.project)
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
        second = scan_sources(self.project)
        for name in ("source_key", "content_hash", "content", "chunks", "coverage"):
            self.assertEqual(first["sources"][0][name], second["sources"][0][name])
        self.assertNotEqual(first["sources"][0]["metadata"]["read_stat"]["mtime_ns"],
                            second["sources"][0]["metadata"]["read_stat"]["mtime_ns"])

    def test_generic_runtime_product_code_is_read_but_marked_plugin_release_is_excluded(self):
        self.write("runtime/core.py", "product runtime")
        self.write("export/release.json", "{}")
        self.write("export/agent_learning/core.py", "generated copy")
        scan = scan_sources(self.project)
        self.assertEqual([source["content"] for source in scan["sources"]], ["product runtime"])
        self.assertIn("excluded_directory", self.reasons(scan))

    def test_project_jsonl_and_messages_json_never_archive_internal_thoughts(self):
        self.write("history.jsonl", json.dumps({"role": "assistant", "content": "answer", "reasoning_content": "hidden-private"}))
        self.write("chat.json", json.dumps({"messages": [
            {"role": "assistant", "channel": "analysis", "content": "hidden-analysis"},
            {"role": "assistant", "content": "visible"}]}))
        scan = scan_sources(self.project)
        self.assertEqual(len(scan["sources"]), 2)
        self.assertEqual({source["kind"] for source in scan["sources"]}, {"conversation"})
        self.assertNotIn("hidden-private", json.dumps(scan))
        self.assertNotIn("hidden-analysis", json.dumps(scan))

    def test_unknown_rollout_and_transcript_json_are_pending_instead_of_configuration(self):
        self.write("rollout-unknown.json", '{"unrecognized": "hidden-thought"}')
        self.write("transcript.json", '{"unrecognized": "hidden-thought"}')
        scan = scan_sources(self.project)
        self.assertEqual(scan["sources"], [])
        self.assertEqual(self.reasons(scan), {"unparsed_conversation_format"})
        self.assertNotIn("hidden-thought", json.dumps(scan))

    def test_nested_messages_and_native_arrays_never_fall_back_to_raw_config(self):
        for name, value in (
                ("nested.json", {"session": {"messages": [{"role": "assistant", "content": "hidden-answer", "reasoning": "hidden-thought"}]}}),
                ("array.json", [{"type": "response_item", "payload": {"type": "reasoning", "text": "hidden-thought"}}]),
                ("response.json", {"choices": [{"message": {"role": "assistant", "content": "hidden-answer", "reasoning_content": "hidden-thought"}}]})):
            self.write(name, json.dumps(value))
        scan = scan_sources(self.project)
        self.assertEqual(scan["sources"], [])
        self.assertEqual(self.reasons(scan), {"unparsed_conversation_format"})
        self.assertNotIn("hidden-thought", json.dumps(scan))

    def test_invalid_json_does_not_silently_become_configuration_full_text(self):
        self.write("invalid.json", '{"messages": [{"reasoning":"hidden-thought"')
        scan = scan_sources(self.project)
        self.assertEqual(scan["sources"], [])
        self.assertIn("unparsed_json_structure", self.reasons(scan))

    def test_enumeration_budget_is_shared_by_deep_directories(self):
        for directory in range(10):
            for file in range(10):
                self.write(f"dir{directory}/doc{file}.md", "x")
        scan = scan_sources(self.project, options={"max_entries": 16})
        self.assertEqual(scan["stats"]["entries_seen"], 16)
        self.assertLessEqual(len(scan["sources"]), 5)
        self.assertIn("budget_entries", self.reasons(scan))

    def test_entry_budget_pending_states_unknown_remaining_directory_coverage(self):
        for index in range(5):
            self.write(f"doc{index}.md", "x")
        scan = scan_sources(self.project, options={"max_entries": 2})
        pending = [record for record in scan["pending"] if record["reason"] == "budget_entries"]
        self.assertTrue(pending)
        self.assertEqual(pending[0]["coverage"], "partial")
        self.assertEqual(pending[0]["remaining_entries"], "unknown")

    def test_empty_file_is_visible_as_skipped_without_claiming_failure(self):
        self.write("empty.md", "")
        scan = scan_sources(self.project)
        self.assertIn("empty_source", self.reasons(scan))
        self.assertFalse(scan["incomplete"])


if __name__ == "__main__":
    unittest.main()
