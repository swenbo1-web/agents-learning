"""Bounded synthetic Codex failures; no CLI, credentials or provider calls."""
import json
import unittest

from agent_learning.admin_diagnostics import (
    FIELDS, codex_failure, codex_text_failure, configuration_failure,
    merge_codex_diagnostic, safe_diagnostic,
)


class CodexFailureDiagnosticTests(unittest.TestCase):
    def assert_reason(self, diagnostic, reason, status=None):
        self.assertIsNotNone(diagnostic)
        self.assertEqual(diagnostic["reason_code"], reason)
        self.assertEqual(diagnostic["http_status"], status)
        self.assertEqual(diagnostic["http_response_observed"], status is not None)
        self.assertEqual(diagnostic["retry_policy"], "no_automatic_retry")
        self.assertIsNone(diagnostic["parameter_code"])
        self.assertFalse(configuration_failure(diagnostic))
        self.assertEqual(set(diagnostic), FIELDS)
        self.assertEqual(safe_diagnostic(diagnostic), diagnostic)

    def test_original_nested_machine_failure_contract_is_unchanged(self):
        value = codex_failure({"type": "turn.failed", "error": {
            "code": "model_not_found", "status_code": 404, "message": "PRIVATE"}})
        self.assertEqual(value, {"version": 1, "phase": "stream", "reason_code": "model_unavailable",
            "http_status": 404, "parameter_code": None, "retry_policy": "no_automatic_retry",
            "stream_invoked": False, "http_response_observed": True, "usage_observed": False})

    def test_codes_are_read_from_explicit_nested_and_top_level_fields(self):
        variants = (
            {"code": "invalid_api_key", "status_code": 401},
            {"error_code": "invalid_api_key", "http_status": 401},
            {"error": {"code": "invalid_api_key", "status": 401}},
            {"error": {"error": {"type": "authentication_error", "status_code": 401}}},
        )
        for values in variants:
            with self.subTest(values=values):
                self.assert_reason(codex_failure({"type": "error", **values}), "invalid_credential", 401)

    def test_known_code_families_have_fixed_reasons(self):
        for code, reason in (
            ("insufficient_quota", "provider_quota_exceeded"),
            ("rate_limit_exceeded", "provider_quota_exceeded"),
            ("unsupported_model", "model_unavailable"),
            ("invalid_json_schema", "invalid_response_schema"),
            ("invalid_arguments", "invalid_arguments"),
        ):
            with self.subTest(code=code):
                self.assert_reason(codex_failure({"type": "error", "code": code}), reason)

    def test_only_unambiguous_typed_http_statuses_classify_without_code(self):
        for status, reason in ((401, "invalid_credential"), (429, "provider_quota_exceeded"),
                               (400, "model_request_failed"), (403, "model_request_failed"),
                               (404, "model_request_failed"), (500, "model_request_failed")):
            self.assert_reason(codex_failure({"type": "error", "status": status}), reason, status)

    def test_invalid_types_are_ignored_without_crashing(self):
        for value in (None, [], {}, True, 401.0, "401", -1, 600):
            with self.subTest(value=value):
                self.assert_reason(codex_failure({"type": "error", "error": {
                    "code": value, "status_code": value, "message": value}}), "model_request_failed")
        for event in (None, [], "error", {"type": []}, {"type": {}}, {"type": True}):
            self.assertIsNone(codex_failure(event))

    def test_known_typed_code_precedes_conflicting_message(self):
        value = codex_failure({"type": "turn.failed", "error": {
            "code": "invalid_api_key", "message": "Rate limit exceeded. Please try again later."}})
        self.assert_reason(value, "invalid_credential")

    def test_known_top_level_code_survives_unknown_nested_code(self):
        self.assert_reason(codex_failure({"type": "error", "code": "model_not_found",
            "error": {"code": "unknown_private_code", "status_code": 404}}), "model_unavailable", 404)

    def test_message_only_events_support_known_auth_quota_model_schema_and_arguments(self):
        cases = (
            ("Your authentication token has been invalidated. Please try signing in again.", "invalid_credential"),
            ("Your access token could not be refreshed because your refresh token was already used. Please log out and sign in again.", "invalid_credential"),
            ("You exceeded your current quota, please check your plan and billing details.", "provider_quota_exceeded"),
            ("You've hit your usage limit. Try again at 10:30 AM.", "provider_quota_exceeded"),
            ("The model `gpt-test` does not exist or you do not have access to it.", "model_unavailable"),
            ("The 'gpt-test' model is not supported when using Codex with a ChatGPT account.", "model_unavailable"),
            ("Invalid schema for response_format 'response': required must include every key", "invalid_response_schema"),
            ("Invalid JSON schema: missing properties", "invalid_response_schema"),
            ("unexpected argument '--bogus' found", "invalid_arguments"),
            ("invalid value 'wrong' for '--sandbox <SANDBOX_MODE>': invalid mode", "invalid_arguments"),
            ("Unknown feature: missing_feature", "invalid_arguments"),
        )
        for message, reason in cases:
            with self.subTest(message=message):
                self.assert_reason(codex_failure({"type": "error", "message": message}), reason)
                self.assert_reason(codex_failure({"type": "turn.failed", "error": message}), reason)
                self.assert_reason(codex_text_failure("Error: " + message + "\r\n"), reason)
                self.assertIsNone(codex_text_failure(message))

    def test_lowercase_cli_prefix_and_utf8_bytes_are_supported(self):
        self.assert_reason(codex_text_failure(b"error: unexpected argument '--bad' found\n"), "invalid_arguments")

    def test_normal_protocol_or_user_content_is_not_an_error(self):
        text = "Error: Your authentication token has expired. Please try signing in again."
        for kind in ("item.completed", "turn.completed", "turn.started", "message", "thinking"):
            self.assertIsNone(codex_failure({"type": kind, "message": text, "code": "invalid_api_key"}))
        for line in (
            json.dumps({"type": "message", "content": text}), json.dumps(text),
            "The user said: " + text, "> " + text, "```" + text, "[INFO] " + text,
            "Example: " + text, " ERROR: " + text, "authentication quota model schema error",
            "Error: An example says 'unexpected argument --bad found'", "Error: quota exceeded in example",
            "Error: The model 'gpt-test` does not exist.",
        ):
            with self.subTest(line=line):
                self.assertIsNone(codex_text_failure(line))

    def test_quoted_multiple_messages_control_sequences_and_multiline_are_rejected(self):
        valid = "Error: unexpected argument '--bad' found"
        for line in ('"' + valid + '"', valid + "\nError: Not logged in", valid + "\n\n",
                     "\x1b[31m" + valid, valid + "\x00", valid + "\t", valid + "\rignored",
                     "Error: Invalid JSON schema: first\u2028second", "Error: Invalid JSON schema: first\u0085second",
                     b"Error: Not logged in\xff"):
            self.assertIsNone(codex_text_failure(line))

    def test_unknown_errors_stay_generic_and_unknown_stderr_is_rejected(self):
        for message in ("PRIVATE_PATH https://secret.invalid/account token=PRIVATE", "HTTP 401",
                        "quota", "model_not_found", "An unspecified error occurred"):
            self.assert_reason(codex_failure({"type": "error", "message": message}), "model_request_failed")
            self.assertIsNone(codex_text_failure("Error: " + message))

    def test_text_never_guesses_http_status_or_grants_configuration_retry(self):
        value = codex_text_failure("Error: Invalid JSON schema: HTTP 400 reasoning_effort token=PRIVATE")
        self.assert_reason(value, "invalid_response_schema")
        self.assertFalse(value["stream_invoked"])

    def test_secrets_paths_urls_and_accounts_never_survive_recognition(self):
        lines = (
            "Error: Incorrect API key provided: sk-PRIVATE_CREDENTIAL. You can find your API key at https://platform.openai.com/account/api-keys.",
            "Error: Invalid schema for response_format 'response': PRIVATE_ACCOUNT C:\\private\\source.json https://private.invalid PRIVATE_THINKING",
            "Error: invalid value 'PRIVATE_ACCOUNT' for '--model': PRIVATE_CREDENTIAL",
        )
        for line in lines:
            value = codex_text_failure(line)
            self.assertIsNotNone(value)
            encoded = json.dumps(value)
            for secret in ("PRIVATE", "private", "http", "C:\\", "sk-"):
                # The fixed field name http_status is expected; URL text is not.
                if secret == "http":
                    self.assertNotIn("https://", encoded)
                else:
                    self.assertNotIn(secret, encoded)

    def test_bounds_cover_bytes_unicode_and_malformed_values(self):
        for line in (None, True, [], {}, "Error: Invalid JSON schema: " + "x" * 4096,
                     "Error: Invalid JSON schema: " + "中" * 1400,
                     b"Error: Invalid JSON schema: " + b"x" * 4096, "Error: Not logged in\ud800"):
            with self.subTest(type=type(line).__name__):
                self.assertIsNone(codex_text_failure(line))
                self.assert_reason(codex_failure({"type": "error", "message": line}), "model_request_failed")

    def test_deep_or_unrelated_payloads_are_not_searched(self):
        for event in (
            {"type": "error", "data": {"code": "invalid_api_key"}},
            {"type": "error", "error": {"error": {"error": {"code": "invalid_api_key"}}}},
            {"type": "error", "message": {"code": "invalid_api_key"}},
        ):
            self.assert_reason(codex_failure(event), "model_request_failed")


class CodexDiagnosticMergeTests(unittest.TestCase):
    def diagnostic(self, code=None, status=None):
        return codex_failure({"type": "error", "code": code, "status_code": status})

    def test_specific_reason_is_not_overwritten_by_generic_failure(self):
        specific = self.diagnostic("invalid_api_key")
        generic = self.diagnostic()
        self.assertEqual(merge_codex_diagnostic(generic, specific), specific)
        self.assertEqual(merge_codex_diagnostic(specific, generic), specific)
        self.assertEqual(merge_codex_diagnostic(specific, specific), specific)

    def test_typed_status_wins_over_text_without_combining_observations(self):
        text = codex_text_failure("Error: Not logged in")
        typed = self.diagnostic("invalid_json_schema", 400)
        self.assertEqual(merge_codex_diagnostic(text, typed), typed)
        self.assertEqual(merge_codex_diagnostic(typed, text), typed)
        self.assertEqual(merge_codex_diagnostic(self.diagnostic(None, 500), text), text)

    def test_equal_strength_conflicts_preserve_first_and_do_not_mutate_inputs(self):
        first, second = self.diagnostic("model_not_found", 404), self.diagnostic("invalid_api_key", 401)
        snapshots = json.dumps([first, second], sort_keys=True)
        result = merge_codex_diagnostic(first, second)
        self.assertEqual(result, first)
        result["reason_code"] = "model_request_failed"
        self.assertEqual(json.dumps([first, second], sort_keys=True), snapshots)

    def test_invalid_or_configuration_retry_payloads_cannot_expand_permissions(self):
        valid = self.diagnostic("invalid_api_key")
        configuration = dict(valid, reason_code="invalid_request_parameter", parameter_code="reasoning_effort",
            retry_policy="configuration_change", http_status=400, http_response_observed=True, stream_invoked=True)
        self.assertIsNotNone(safe_diagnostic(configuration))  # Existing DSH contract remains valid.
        for invalid in ({}, {**valid, "raw_message": "PRIVATE"}, configuration, None):
            self.assertEqual(merge_codex_diagnostic(valid, invalid), valid)
            self.assertEqual(merge_codex_diagnostic(invalid, valid), valid)
        self.assertIsNone(merge_codex_diagnostic(None, configuration))


if __name__ == "__main__":
    unittest.main()
