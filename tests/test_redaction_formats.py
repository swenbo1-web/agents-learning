"""Only synthetic credential markers: no model, host or real database access."""
import json
import unittest

from agent_learning.event_capture import prepare_event
from agent_learning.runtime import clean


class RedactionFormatTests(unittest.TestCase):
    PRIVATE = "SYNTHETIC_PRIVATE_MARKER"

    def masked(self, value):
        result = clean(value)
        self.assertNotIn(self.PRIVATE, str(result))
        self.assertEqual(clean(result), result)
        return result

    def test_string_json_fields_preserve_public_whitespace_and_value_types(self):
        source = '{  "password" : "' + self.PRIVATE + '", "public": [1, true, null] }'
        self.assertEqual(self.masked(source), '{  "password" : "[REDACTED]", "public": [1, true, null] }')
        self.assertEqual(json.loads(self.masked(source))["public"], [1, True, None])

    def test_multiline_json_field_colon_boundaries_preserve_public_formatting(self):
        for before, after in (('', '\n '), ('\n ', ''), ('\r\n\t', '\r\n\t'), ('\n', '\n')):
            with self.subTest(before=before, after=after):
                prefix = '{"api_key"' + before + ':' + after
                source = prefix + '"' + self.PRIVATE + ' tail", "public": 1}'
                expected = prefix + '"[REDACTED]", "public": 1}'
                self.assertEqual(self.masked(source), expected)
                self.assertEqual(json.loads(expected), {'api_key': '[REDACTED]', 'public': 1})

    def test_multiline_empty_missing_and_truncated_json_values(self):
        for whitespace in ('\n ', '\r\n\t'):
            prefix = '{"password"' + whitespace + ':' + whitespace
            with self.subTest(whitespace=whitespace):
                self.assertEqual(self.masked(prefix + '"", "public":1}'),
                                 prefix + '"[REDACTED]", "public":1}')
                self.assertEqual(clean(prefix), prefix)
                missing = prefix + ', "public":1}'
                self.assertEqual(clean(clean(missing)), missing)
                self.assertEqual(self.masked(prefix + '"first ' + self.PRIVATE),
                                 prefix + '"[REDACTED]"')

    def test_nested_sensitive_json_object_and_array_are_removed_atomically(self):
        for value in ([{"private": self.PRIVATE, "bracket": "}]"}], {"nested": {"private": self.PRIVATE}}):
            source = json.dumps({"public": {"api_key": value}, "safe": "keep"})
            result = json.loads(self.masked(source))
            self.assertEqual(result, {"public": {"api_key": "[REDACTED]"}, "safe": "keep"})

    def test_sensitive_json_keys_match_dictionary_suffix_and_escaped_keys(self):
        for key in ('clientToken', 'Authorization', 'api key', 'password', r'pass\u0077ord', r'\u0070assword'):
            with self.subTest(key=key):
                source = '{"' + key + '": "' + self.PRIVATE + '", "public": 2}'
                self.assertEqual(json.loads(self.masked(source))["public"], 2)

    def test_json_escaped_value_does_not_leak_or_consume_next_field(self):
        value = 'prefix "escaped quote" \\ ' + self.PRIVATE + '\n multiline'
        result = self.masked(json.dumps({"password": value, "public": "after"}))
        self.assertEqual(json.loads(result), {"password": "[REDACTED]", "public": "after"})

    def test_quoted_shell_values_include_spaces_delimiters_and_escaped_quote(self):
        for quote in ('"', "'"):
            source = 'run --password=' + quote + 'first , ; ' + self.PRIVATE + '\\' + quote + ' final' + quote + ' --mode=public'
            self.assertEqual(self.masked(source), 'run --password=' + quote + '[REDACTED]' + quote + ' --mode=public')

    def test_single_quoted_mapping_and_configuration_fields(self):
        source = "{'secret' : 'first " + self.PRIVATE + " last', 'safe': 3}"
        self.assertEqual(self.masked(source), "{'secret' : '[REDACTED]', 'safe': 3}")
        self.assertEqual(self.masked('"password" = "' + self.PRIVATE + ' with spaces"\nmode=public'),
                         '"password" = "[REDACTED]"\nmode=public')

    def test_explicit_token_assignment_including_shortest_value(self):
        self.assertEqual(clean('token=x'), 'token=[REDACTED]')
        self.assertEqual(self.masked('token=' + self.PRIVATE + '; public=true'), 'token=[REDACTED]; public=true')
        self.assertEqual(self.masked('token="value ' + self.PRIVATE + '" next=public'), 'token="[REDACTED]" next=public')

    def test_normal_business_token_words_are_not_rewritten(self):
        for value in ('token budget 4000; input tokens 12', 'token_count=12', 'max_token=4096',
                      'tokenizer=fast', 'tokenization: technical details', 'Basic education and basic settings',
                      '{"public": "tokens", "token_count": 10, "token_budget": 8000}'):
            self.assertEqual(clean(value), value)

    def test_basic_auth_is_limited_to_explicit_authorization_header(self):
        for prefix in ('Authorization: Basic ', 'Proxy-Authorization: basic ', 'authorization=Basic '):
            source = prefix + self.PRIVATE + '\nAccept: application/json'
            self.assertEqual(self.masked(source), prefix + '[REDACTED]\nAccept: application/json')
        self.assertEqual(clean('Basic public-training'), 'Basic public-training')

    def test_unicode_regex_equivalents_cannot_bypass_fast_hint(self):
        # Python's Unicode re.I accepts these non-ASCII forms. No-op hints
        # must not be narrower than either the legacy or new masking rule.
        for key in ('ſecret', 'paſſword', 'api_Key', 'acceſſ_toKen',
                    'apı_key', 'apİ_key', 'toKen'):
            with self.subTest(key=key):
                self.assertIn('[REDACTED]', self.masked(key + '=' + self.PRIVATE))
                self.assertIn('[REDACTED]', self.masked('run --' + key + '="first ' + self.PRIVATE + ' tail"'))
                self.assertIn('[REDACTED]', self.masked('{"' + key + '":"' + self.PRIVATE + '"}'))
        for key in ('Authorızatıon', 'Authorİzatİon', 'Authori\u0307zation'):
            if '\u0307' in key:
                # A literal combining dot is not itself equivalent under re.I;
                # the hint may admit it, but masking must not invent a match.
                self.assertEqual(clean(key + ': Basic public-value'), key + ': Basic public-value')
                continue
            self.assertIn('[REDACTED]', self.masked(key + ': Basic ' + self.PRIVATE))

    def test_masked_url_and_private_marker_tail_still_have_distinct_boundaries(self):
        for key in ('token', 'access_token', 'password'):
            source = 'https://example.test/?' + key + '=[REDACTED]&view=public#fragment'
            self.assertEqual(clean(clean(source)), source)
            for tail in (':', '/', '?', ''):
                self.assertNotIn(self.PRIVATE, clean(key + '=[REDACTED]' + tail + self.PRIVATE))

    def test_quoted_mask_with_private_tail_is_not_an_exemption(self):
        for source in ('password="[REDACTED]:' + self.PRIVATE + '"',
                       '{"password":"[REDACTED]:' + self.PRIVATE + '"}'):
            self.masked(source)

    def test_truncated_quoted_credential_does_not_leave_visible_secret(self):
        for source in ('password="first ' + self.PRIVATE, '{"password": "' + self.PRIVATE):
            self.masked(source)

    def test_deep_sensitive_text_uses_bounded_stack_not_recursive_json_parse(self):
        source = '{"password":' + '[' * 1000 + '"' + self.PRIVATE + '"' + ']' * 1000 + '}'
        result = self.masked(source)
        self.assertLess(len(result), 100)

    def test_unquoted_free_form_value_has_explicit_first_word_limit(self):
        # No safe way to distinguish a multiword unquoted password from the rest
        # of a tool log. Callers must quote it or supply a structured field.
        self.assertEqual(clean('run --password=first diagnostic output follows'),
                         'run --password=[REDACTED] diagnostic output follows')

    def test_standalone_credential_line_masks_multiword_value_to_explicit_delimiter(self):
        for suffix in ('\npublic details', '; mode=public', ', count=3', ''):
            source = 'public before\n  password=first ' + self.PRIVATE + ' tail' + suffix
            self.assertEqual(self.masked(source), 'public before\n  password=[REDACTED]' + suffix)
        self.assertEqual(self.masked('token=first ' + self.PRIVATE + ' tail'), 'token=[REDACTED]')

    def test_capture_projection_and_digest_use_same_masked_payload(self):
        for source in ('{"password":"first ' + self.PRIVATE + ' last"}',
                       'token=' + self.PRIVATE,
                       'Authorization: Basic ' + self.PRIVATE,
                       'password="first ' + self.PRIVATE + ' last"'):
            payload = {"tool_response": {"exit_code": 0, "output": source}}
            encoded, digest = prepare_event(payload, 6000)
            self.assertNotIn(self.PRIVATE, encoded)
            other, same_digest = prepare_event(clean(payload), 6000)
            self.assertEqual((other, same_digest), (encoded, digest))
            self.assertEqual(payload["tool_response"]["output"], source)

    def test_long_value_and_late_private_field_are_masked_before_budget_clipping(self):
        output = 'public ' * 180000 + '\n{"password":"' + self.PRIVATE + ' ' + 'private ' * 10000 + '", "safe":1}'
        cleaned = self.masked(output)
        self.assertTrue(cleaned.endswith('{"password":"[REDACTED]", "safe":1}'))
        encoded, _ = prepare_event({"tool_response": {"exit_code": 0, "output": output}}, 200)
        self.assertNotIn(self.PRIVATE, encoded)
        self.assertLessEqual(len(encoded), 200)


if __name__ == '__main__':
    unittest.main()
