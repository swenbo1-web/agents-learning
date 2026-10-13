"""Update metadata tests use isolated files and patched official HTTP transport."""
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib import error

from agent_learning import software_updates as updates


def release(version="0.13.0", *, identifier=1, prerelease=False, draft=False, **extra):
    tag = "v" + version
    return {"id": identifier, "tag_name": tag, "draft": draft, "prerelease": prerelease,
            "published_at": "2026-10-11T00:00:00Z",
            "html_url": "https://github.com/swenbo1-web/agents-learning/releases/tag/" + tag,
            **extra}


class Response:
    def __init__(self, body=b"[]", headers=None, url=None):
        self.body = BytesIO(body)
        self.headers = headers or {}
        self.url = url or "https://api.github.com/repos/swenbo1-web/agents-learning/releases?per_page=20"
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def getcode(self):
        return 200

    def geturl(self):
        return self.url

    def read(self, limit):
        return self.body.read(limit)

    def read1(self, limit):
        return self.read(limit)


class SoftwareUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-software-update-")
        self.home = Path(self.temp.name) / "data"
        self.config = updates.validate_config({"enabled": True, "interval_seconds": 3600})
        self.clock = patch.object(updates.time, "time", return_value=100000)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def check(self, payload=None, *, headers=None, code=200, **kwargs):
        with patch.object(updates, "_fetch", return_value=(code, headers or {}, payload or [])) as fetch:
            result = updates.check_updates(self.home, self.config, current_version="0.12.0", **kwargs)
        return result, fetch

    def test_config_defaults_disabled_and_no_auth_fields(self):
        config = updates.validate_config({})
        self.assertFalse(config["enabled"])
        self.assertEqual(config["channel"], "prerelease")
        self.assertEqual(config["repository"], "swenbo1-web/agents-learning")
        for invalid in ({"token": "secret"}, {"enabled": 1}, {"channel": "nightly"}, {"channel": []}, {"channel": {}},
                        {"repository": "https://github.com/x/y"}, {"repository": "x/y.git"},
                        {"repository": "x/.."}, {"repository": "x/y?token=secret"},
                        {"repository": "x/y/z"}, {"timeout_seconds": float("nan")},
                        {"timeout_seconds": True}, {"timeout_seconds": 0.1}, {"timeout_seconds": 10**400},
                        {"interval_seconds": True}, {"interval_seconds": 1}, []):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                updates.validate_config(invalid)

    def test_status_and_disabled_due_do_not_create_any_files(self):
        result = updates.update_status(self.home, {}, "0.12.0")
        self.assertEqual(result["state"], "unchecked")
        self.assertFalse(updates.automatic_check_due(self.home, {}))
        self.assertIsNone(updates.reserve_automatic_check(self.home, {}))
        self.assertFalse(self.home.exists())

    def test_config_reader_ignores_unrelated_config_and_runtime(self):
        self.home.mkdir()
        source = self.home / "config.json"
        source.write_text(json.dumps({"admin": {"credential": "private"},
                                      "software_updates": {"enabled": True}}), encoding="utf-8-sig")
        with patch.dict(os.environ, {"ALP_CONFIG": str(source)}):
            value = updates.load_config(self.home)
        self.assertTrue(value["enabled"])
        self.assertNotIn("private", json.dumps(value))
        self.assertEqual([path.name for path in self.home.iterdir()], ["config.json"])

    def test_config_reader_rejects_bad_shapes_and_oversize(self):
        self.home.mkdir()
        path = self.home / "fixture.json"
        for content in ("[]", "not JSON", '{"software_updates": null}', " " * (256 * 1024 + 1)):
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content[:40]), self.assertRaises(ValueError):
                updates.load_config(self.home, path)

    def test_explicit_check_works_when_automatic_is_disabled(self):
        with patch.object(updates, "_fetch", return_value=(200, {}, [release()])) as fetch:
            result = updates.check_updates(self.home, {}, current_version="0.12.0")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["state"], "update_available")
        self.assertFalse(result["automatic_enabled"])
        self.assertFalse(result["downloaded"])
        self.assertFalse(result["installed"])
        self.assertFalse((self.home / "runtime.sqlite3").exists())

    def test_numeric_version_order_and_semver_prerelease_order(self):
        result, _ = self.check([release("0.9.9", identifier=2), release("0.13.0-rc.2", identifier=3),
                                release("0.13.0-rc.10", identifier=4)])
        self.assertEqual(result["latest"], "0.13.0-rc.10")
        result, _ = self.check([release("0.13.0"), release("0.13.0-rc.99", identifier=2)], force=True)
        self.assertEqual(result["latest"], "0.13.0")

    def test_local_ahead_same_version_and_build_metadata(self):
        self.check([release("0.11.1", prerelease=True)])
        self.assertEqual(updates.update_status(self.home, self.config, "0.12.0")["state"], "ahead")
        self.assertFalse(updates.update_status(self.home, self.config, "0.12.0")["update_available"])
        self.check([release("0.12.0+build.2")], force=True)
        result = updates.update_status(self.home, self.config, "0.12.0+local.1")
        self.assertEqual(result["state"], "up_to_date")
        self.assertFalse(result["update_available"])

    def test_draft_invalid_tag_and_stable_channel_exclusions(self):
        self.config["channel"] = "stable"
        result, _ = self.check([release("0.20.0", draft=True), release("0.15.0", prerelease=True),
                                release("0.14.0-beta.1"), release("0.13.0"), release("01.99.0")])
        self.assertEqual(result["latest"], "0.13.0")

    def test_empty_and_only_ineligible_releases(self):
        result, _ = self.check()
        self.assertEqual(result["state"], "no_eligible_release")
        self.assertIsNone(result["latest"])
        result, _ = self.check([release("unknown")], force=True)
        self.assertEqual(result["state"], "no_eligible_release")

    def test_invalid_local_semver_is_rejected_without_creating_home(self):
        for value in ("main", "0.12", "0.12.0.dev1", "01.2.3", "1.2.3-01", "v" + "1" * 100):
            with self.subTest(version=value), self.assertRaises(ValueError):
                updates.check_updates(self.home, {}, current_version=value)
        self.assertFalse(self.home.exists())

    def test_urls_credentials_wrong_project_and_tag_case_are_rejected(self):
        base = "https://github.com/swenbo1-web/agents-learning/releases/tag/v0.13.0"
        urls = [base.replace("github.com", "example.com"), base.replace("https://", "http://"),
                base.replace("github.com", "user:password@github.com"), base + "?token=private",
                base + "#fragment", base.replace("agents-learning", "another"),
                base.replace("github.com", "github.com:443"), base.replace("v0.13.0", "v0.14.0")]
        for url in urls:
            with self.subTest(url=url):
                result, _ = self.check([release(html_url=url)], force=True)
                self.assertIsNone(result["verified_release_url"])
        value = release("0.13.0-RC.1")
        value["html_url"] = value["html_url"].replace("RC", "rc")
        result, _ = self.check([value], force=True)
        self.assertIsNone(result["latest"])

    def test_wrong_api_metadata_url_and_id_are_ignored(self):
        result, _ = self.check([release(url="https://evil.invalid/token"), release(identifier=True),
                                release(published_at="tomorrow"), release(draft="false")])
        self.assertIsNone(result["latest"])

    def test_ttl_avoids_request_and_etag_304_refreshes_check_time(self):
        self.check([release()], headers={"ETag": '"first"'})
        result, fetch = self.check([release("0.14.0")])
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(result["latest"], "0.13.0")
        with patch.object(updates.time, "time", return_value=104000):
            result, fetch = self.check(code=304)
        fetch.assert_called_once_with(self.config["repository"], '"first"', 3)
        self.assertEqual(result["latest"], "0.13.0")
        self.assertEqual(result["checked_at"], 104000)
        self.assertEqual(result["next_check_at"], 107600)

    def test_new_response_without_etag_drops_old_validator(self):
        self.check([release()], headers={"etag": '"old"'})
        self.check([release("0.14.0")], force=True)
        _, fetch = self.check(code=304, force=True)
        self.assertIsNone(fetch.call_args.args[1])

    def test_304_without_cache_cannot_manufacture_success(self):
        result, _ = self.check(code=304)
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["error_reason"], "cache_missing_for_304")
        self.assertIsNone(result["checked_at"])

    def test_network_error_keeps_prior_release_and_has_safe_reason(self):
        self.check([release()])
        with patch.object(updates, "_fetch", side_effect=OSError("password=/secret/private")):
            result = updates.check_updates(self.home, self.config, force=True, current_version="0.12.0")
        self.assertEqual(result["error_reason"], "network_error")
        self.assertEqual(result["latest"], "0.13.0")
        self.assertTrue(result["update_available"])
        self.assertNotIn("private", json.dumps(result))
        result, fetch = self.check()
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(result["state"], "error")

    def test_force_can_retry_network_failure_but_not_rate_limits(self):
        with patch.object(updates, "_fetch", side_effect=OSError("offline")):
            updates.check_updates(self.home, self.config, current_version="0.12.0")
        result, fetch = self.check([release()], force=True)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["state"], "update_available")
        self.check(code=429, headers={"Retry-After": "10000"}, force=True)
        result, fetch = self.check([release("0.14.0")], force=True)
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(result["state"], "rate_limited")
        self.assertEqual(result["next_check_at"], 110000)

    def test_rate_reset_and_retry_after_both_are_respected(self):
        result, _ = self.check(code=403, headers={"Retry-After": "120",
                                                 "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "101000"})
        self.assertEqual(result["next_check_at"], 101000)
        with patch.object(updates.time, "time", return_value=101001):
            result, fetch = self.check([release()])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["state"], "update_available")

    def test_not_found_and_redirect_have_distinct_safe_reasons(self):
        result, _ = self.check(code=404)
        self.assertEqual(result["error_reason"], "not_found")
        self.assertEqual(result["next_check_at"], 103600)
        result, _ = self.check(code=302, force=True)
        self.assertEqual(result["error_reason"], "redirect_refused")

    def test_response_shapes_and_page_limit_are_bounded(self):
        for payload in ({"message": "secret"}, [release()] * 21):
            with self.subTest(payload=type(payload).__name__):
                result, _ = self.check(payload, force=True)
                self.assertEqual(result["error_reason"], "invalid_response")

    def test_cache_keeps_bounded_release_summary_but_omits_assets_and_author_material(self):
        result, _ = self.check([release(body="新增项目历史吸收", assets=[{"token": "private-assets"}],
                            author={"token": "private-author"})])
        _, cache_path, _, _ = updates._paths(self.home, self.config)
        self.assertNotIn("private", cache_path.read_text(encoding="utf-8"))
        self.assertEqual(result["release_notes"], "新增项目历史吸收")
        self.assertEqual(list(self.home.glob("*.sqlite3")), [])
        self.assertEqual(list(self.home.glob("*.tmp")), [])

    def test_duplicate_release_rows_do_not_compare_dicts(self):
        result, _ = self.check([release(), release()])
        self.assertEqual(result["latest_release_id"], 1)
        self.assertEqual(updates.update_status(self.home, self.config, "0.12.0")["latest"], "0.13.0")

    def test_invalid_cache_types_and_huge_integers_are_ignored(self):
        self.check([release()])
        _, path, lease_path, _ = updates._paths(self.home, self.config)
        baseline = json.loads(path.read_text(encoding="utf-8"))
        for field, value in (("error_reason", []), ("error_reason", {}), ("checked_at", 10**400),
                             ("poll_interval", []), ("schema", True)):
            path.write_text(json.dumps({**baseline, field: value}), encoding="utf-8")
            self.assertEqual(updates.update_status(self.home, self.config, "0.12.0")["state"], "unchecked")
        path.write_text('{"huge":' + "1" * 5000 + "}", encoding="utf-8")
        self.assertEqual(updates.update_status(self.home, self.config, "0.12.0")["state"], "unchecked")
        for kind in ([], {}):
            lease_path.write_text(json.dumps({"kind": kind, "token": "0" * 32,
                                             "created_at": 100000, "expires_at": 100030}), encoding="utf-8")
            self.assertTrue(updates.automatic_check_due(self.home, self.config))

    def test_changed_interval_applies_to_existing_cache_and_honors_server_poll(self):
        self.config["interval_seconds"] = 86400
        self.check([release()])
        self.config["interval_seconds"] = 3600
        with patch.object(updates.time, "time", return_value=104000):
            self.assertTrue(updates.automatic_check_due(self.home, self.config))
        self.check([release()], headers={"X-Poll-Interval": "10000"}, force=True)
        self.assertEqual(updates.update_status(self.home, self.config, "0.12.0")["next_check_at"], 110000)
        with patch.object(updates.time, "time", return_value=104000):
            self.assertFalse(updates.automatic_check_due(self.home, self.config))

    def test_transport_response_read_deadline_is_not_reset_per_chunk(self):
        response = Response()
        class Opener:
            def open(self, *args, **kwargs):
                return response
        with patch.object(updates.request, "build_opener", return_value=Opener()), \
                patch.object(updates.time, "monotonic", side_effect=[0, 4]):
            with self.assertRaises(TimeoutError):
                updates._fetch("swenbo1-web/agents-learning", None, 3)
        self.assertTrue(response.closed)

    def test_tampered_oversized_or_corrupt_cache_is_not_used(self):
        self.check([release()])
        _, path, _, _ = updates._paths(self.home, self.config)
        original = json.loads(path.read_text(encoding="utf-8"))
        corrupted = dict(original, etag="good\r\nAuthorization: secret")
        corrupted["releases"][0]["html_url"] = "https://evil.invalid/secret"
        for value in (json.dumps(corrupted), " " * (updates._MAX_CACHE_BYTES + 1), "not-json"):
            path.write_text(value, encoding="utf-8")
            result = updates.update_status(self.home, self.config, "0.12.0")
            self.assertIsNone(result["latest"])
            self.assertIsNone(result["verified_release_url"])

    def test_reservation_prevents_duplicate_launch_and_checks_ownership(self):
        token = updates.reserve_automatic_check(self.home, self.config)
        self.assertIsNotNone(token)
        self.assertFalse(updates.automatic_check_due(self.home, self.config))
        self.assertIsNone(updates.reserve_automatic_check(self.home, self.config))
        self.assertFalse(updates.cancel_reserved_check(self.home, self.config, "0" * 32))
        result, fetch = self.check([release()], reservation="0" * 32)
        self.assertEqual(result["error_reason"], "reservation_expired")
        self.assertEqual(fetch.call_count, 0)
        result, fetch = self.check([release()], reservation=token)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["state"], "update_available")
        self.assertFalse(updates.automatic_check_due(self.home, self.config))
        _, _, lease_path, _ = updates._paths(self.home, self.config)
        self.assertFalse(lease_path.exists())

    def test_cancelled_and_expired_reservations_recover(self):
        token = updates.reserve_automatic_check(self.home, self.config)
        self.assertTrue(updates.cancel_reserved_check(self.home, self.config, token))
        self.assertTrue(updates.automatic_check_due(self.home, self.config))
        token = updates.reserve_automatic_check(self.home, self.config)
        with patch.object(updates.time, "time", return_value=100031):
            result, fetch = self.check([release()], reservation=token)
            self.assertEqual(result["error_reason"], "reservation_expired")
            self.assertEqual(fetch.call_count, 0)
            self.assertIsNotNone(updates.reserve_automatic_check(self.home, self.config))

    def test_disabled_reserved_worker_cancels_without_request(self):
        token = updates.reserve_automatic_check(self.home, self.config)
        self.config["enabled"] = False
        result, fetch = self.check([release()], reservation=token)
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(result["state"], "unchecked")
        _, _, lease_path, _ = updates._paths(self.home, self.config)
        self.assertFalse(lease_path.exists())

    def test_active_request_cross_process_lock_blocks_duplicate_work(self):
        self.home.mkdir()
        _, _, _, lock_path = updates._paths(self.home, self.config)
        with updates._locked(lock_path) as acquired:
            self.assertTrue(acquired)
            self.assertIsNone(updates.reserve_automatic_check(self.home, self.config))
            result, fetch = self.check([release()])
            self.assertEqual(fetch.call_count, 0)
            self.assertEqual(result["state"], "checking")

    def test_separate_process_cannot_replace_active_reservation(self):
        token = updates.reserve_automatic_check(self.home, self.config)
        # The child uses the real clock rather than the parent's deterministic clock.
        _, _, lease_path, _ = updates._paths(self.home, self.config)
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
        from datetime import datetime
        lease["created_at"] = datetime.now().timestamp()
        lease["expires_at"] = lease["created_at"] + 30
        lease_path.write_text(json.dumps(lease), encoding="utf-8")
        code = "import sys; from agent_learning.software_updates import reserve_automatic_check; print(reserve_automatic_check(sys.argv[1], {'enabled':True,'interval_seconds':3600}))"
        result = subprocess.run([sys.executable, "-B", "-c", code, str(self.home)],
                                capture_output=True, text=True, timeout=10,
                                cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "None")
        self.assertEqual(json.loads(lease_path.read_text(encoding="utf-8"))["token"], token)

    def test_network_work_is_single_flight_across_threads(self):
        entered = threading.Event()
        finish = threading.Event()
        results = []

        def fetch(*args):
            entered.set()
            self.assertTrue(finish.wait(5))
            return 200, {}, [release()]

        with patch.object(updates, "_fetch", side_effect=fetch) as transport:
            thread = threading.Thread(target=lambda: results.append(updates.check_updates(
                self.home, self.config, current_version="0.12.0")))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertIsNone(updates.reserve_automatic_check(self.home, self.config))
                duplicate = updates.check_updates(self.home, self.config, current_version="0.12.0")
                self.assertEqual(duplicate["state"], "checking")
            finally:
                finish.set()
                thread.join(5)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(results[0]["state"], "update_available")

    def test_actual_transport_uses_only_official_url_and_no_credentials_or_proxy(self):
        class Opener:
            def open(inner, req, timeout):
                self.assertEqual(req.full_url, "https://api.github.com/repos/swenbo1-web/agents-learning/releases?per_page=20")
                headers = {key.lower(): value for key, value in req.header_items()}
                self.assertNotIn("authorization", headers)
                self.assertNotIn("cookie", headers)
                self.assertEqual(headers["if-none-match"], '"fixture"')
                self.assertEqual(headers["x-github-api-version"], updates.API_VERSION)
                self.assertEqual(timeout, 3)
                return Response()

        def build(*handlers):
            self.assertEqual(handlers[0].proxies, {})
            self.assertIsInstance(handlers[1], updates._NoRedirect)
            self.assertIsNone(handlers[1].redirect_request(None, None, 302, "", {}, "https://evil.invalid"))
            return Opener()

        with patch.object(updates.request, "build_opener", side_effect=build):
            code, _, payload = updates._fetch("swenbo1-web/agents-learning", '"fixture"', 3)
        self.assertEqual((code, payload), (200, []))

    def test_actual_transport_rejects_large_malformed_and_redirected_responses(self):
        fixtures = [(Response(b"x" * (updates._MAX_RESPONSE_BYTES + 1)), "response_too_large"),
                    (Response(headers={"Content-Length": str(updates._MAX_RESPONSE_BYTES + 1)}), "response_too_large"),
                    (Response(b"not-json"), "invalid_json"),
                    (Response(url="https://evil.invalid"), "redirect_refused")]
        for response, reason in fixtures:
            class Opener:
                def open(self, *args, **kwargs):
                    return response
            with self.subTest(reason=reason), patch.object(updates.request, "build_opener", return_value=Opener()):
                with self.assertRaises(updates._FetchFailure) as caught:
                    updates._fetch("swenbo1-web/agents-learning", None, 3)
                self.assertEqual(str(caught.exception), reason)
                self.assertTrue(response.closed)

    def test_http_error_body_is_not_read_or_leaked(self):
        private = BytesIO(b"private credentials")
        problem = error.HTTPError("https://api.github.com/", 429, "private", {"Retry-After": "60"}, private)
        class Opener:
            def open(self, *args, **kwargs):
                raise problem
        with patch.object(updates.request, "build_opener", return_value=Opener()):
            code, headers, payload = updates._fetch("swenbo1-web/agents-learning", None, 3)
        self.assertEqual(code, 429)
        self.assertEqual(headers, {"Retry-After": "60"})
        self.assertIsNone(payload)
        self.assertTrue(private.closed)


if __name__ == "__main__":
    unittest.main()
