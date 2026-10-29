"""脱敏热路径优化必须与既有行为一致，尤其不放过脱敏标记后的私密尾巴。"""
import copy
import re
import unittest

from agent_learning.runtime import clean


def legacy_clean(value):
    """Frozen pre-optimization behavior, used only as the equivalence oracle."""
    if isinstance(value, dict):
        return {k: "[REDACTED]" if re.search(r"(?i)(password|secret|api.?key|authorization|token)$", k)
                else legacy_clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [legacy_clean(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
        def redact_assignment(match):
            if re.match(r"\[REDACTED\](?:[&#]|$)", match[2]):
                return match[0]
            return match[1] + "[REDACTED]"
        value = re.sub(r"(?i)((?:api[_-]?key|password|secret|access_token)\s*[=:]\s*)([^\s,;]+)",
                       redact_assignment, value)
    return value


class RedactionEfficiencyTests(unittest.TestCase):
    def test_seven_character_prefixes_stay_unchanged(self):
        for value in ("apikey=", "secret=", "Bearer "):
            with self.subTest(value=value):
                self.assertEqual(len(value), 7)
                self.assertEqual(clean(value), value)
                self.assertEqual(clean(value), legacy_clean(value))

    def test_eight_character_secrets_are_not_short_circuited(self):
        for value, expected in (("apikey=x", "apikey=[REDACTED]"),
                                ("secret=x", "secret=[REDACTED]"),
                                ("Bearer x", "Bearer [REDACTED]")):
            with self.subTest(value=value):
                self.assertEqual(len(value), 8)
                self.assertEqual(clean(value), expected)
                self.assertEqual(clean(value), legacy_clean(value))

    def test_case_spacing_quotes_and_assignment_separators_match_legacy(self):
        for key in ("apikey", "api_key", "api-key", "password", "secret", "access_token"):
            for separator in ("=", ":", " = ", "\t:\t"):
                value = key.upper() + separator + "SYNTHETIC_VALUE, public=ok"
                with self.subTest(key=key, separator=separator):
                    self.assertEqual(clean(value), legacy_clean(value))
                    self.assertNotIn("SYNTHETIC_VALUE", clean(value))
        for value in ("bearer x", "BEARER x", "bEaReR\tx", "Bearer\nvalue",
                      "'Bearer value'", '"Bearer value"', "labelBearer value",
                      "Bearer x Bearer y", "Bearer [REDACTED]"):
            with self.subTest(value=value):
                self.assertEqual(clean(value), legacy_clean(value))

    def test_nested_values_and_sensitive_dictionary_keys_match_legacy(self):
        original = {"Authorization": "not bearer", "api key": ["private"],
                    "clientToken": {"secret": "private"}, "public": [
                        "", "0", "read", "true", "普通文本", "password=SYNTHETIC_VALUE",
                        {"api_key": "private", "message": "Bearer value"},
                        [None, False, 0, 1.5, {"detail": "secret=x"}]],
                    "passthrough": ("secret=x",)}
        before = copy.deepcopy(original)
        result = clean(original)
        self.assertEqual(result, legacy_clean(original))
        self.assertEqual(original, before)
        self.assertEqual(result["Authorization"], "[REDACTED]")
        self.assertEqual(result["api key"], "[REDACTED]")
        self.assertEqual(result["clientToken"], "[REDACTED]")
        self.assertEqual(result["public"][-1][-1]["detail"], "secret=[REDACTED]")

    def test_public_url_query_and_fragment_survive_repeated_cleaning(self):
        for value in ("https://example.test/?access_token=[REDACTED]&view=public#section",
                      "https://example.test/?password=[REDACTED]#section",
                      "secret=[REDACTED]", "secret=[REDACTED]; public=ok"):
            with self.subTest(value=value):
                self.assertEqual(clean(value), value)
                self.assertEqual(clean(clean(value)), value)
                self.assertEqual(clean(value), legacy_clean(value))

    def test_redaction_marker_does_not_allow_private_colon_or_adjacent_tail(self):
        for tail in (":SYNTHETIC_PRIVATE", "SYNTHETIC_PRIVATE", "/SYNTHETIC_PRIVATE",
                     "?SYNTHETIC_PRIVATE", "[SYNTHETIC_PRIVATE]"):
            value = "secret=[REDACTED]" + tail
            with self.subTest(tail=tail):
                self.assertEqual(clean(value), "secret=[REDACTED]")
                self.assertEqual(clean(value), legacy_clean(value))

    def test_string_subclass_keeps_existing_plain_string_result(self):
        class PublicText(str):
            pass
        for value in (PublicText("read"), PublicText("secret=x")):
            with self.subTest(value=value):
                self.assertEqual(clean(value), legacy_clean(value))
                self.assertIs(type(clean(value)), type(legacy_clean(value)))


if __name__ == "__main__":
    unittest.main()
