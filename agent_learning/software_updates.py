"""Bounded GitHub release checks, separate from knowledge data and installation.

Only public release metadata is fetched. This module never downloads code, starts an
installer, opens SQLite, or reads authentication material. Automatic checks require
an explicit opt-in and use short reservations plus a cross-process file lock.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import total_ordering
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib import error, parse, request
import uuid

DEFAULTS = {
    "enabled": False,
    "repository": "swenbo1-web/agents-learning",
    "channel": "prerelease",
    "interval_seconds": 86400,
    "timeout_seconds": 3,
}
API_VERSION = "2026-03-10"
_MAX_RESPONSE_BYTES = 512 * 1024
_MAX_CACHE_BYTES = 32 * 1024
_RESERVATION_SECONDS = 30
_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
_REPOSITORY = re.compile(rf"{_OWNER}/[A-Za-z0-9_.-]{{1,100}}\Z")
_VERSION = re.compile(
    r"v?(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})"
    r"(?:-([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*))?"
    r"(?:\+([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*))?\Z"
)


def validate_config(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("software_updates contains unsupported configuration fields")
    config = {**DEFAULTS, **value}
    if not isinstance(config["enabled"], bool):
        raise ValueError("software_updates.enabled must be a boolean")
    repo = config["repository"]
    if (not isinstance(repo, str) or not _REPOSITORY.fullmatch(repo)
            or repo.split("/")[1] in {".", ".."} or repo.endswith(".git")):
        raise ValueError("software_updates.repository must be a GitHub owner/repository name")
    config["repository"] = repo.lower()
    if not isinstance(config["channel"], str) or config["channel"] not in {"stable", "prerelease"}:
        raise ValueError("software_updates.channel must be stable or prerelease")
    if (type(config["interval_seconds"]) is not int
            or not 3600 <= config["interval_seconds"] <= 604800):
        raise ValueError("software_updates.interval_seconds must be between 3600 and 604800")
    timeout = config["timeout_seconds"]
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 0.5 <= timeout <= 10 or not math.isfinite(timeout)):
        raise ValueError("software_updates.timeout_seconds must be between 0.5 and 10")
    return config


def load_config(home, config_path=None):
    """Read only the update section; do not create a home or initialize Runtime."""
    path = Path(config_path or os.environ.get("ALP_CONFIG") or Path(home) / "config.json")
    if not path.exists():
        return validate_config({})
    try:
        with path.open("rb") as stream:
            data = stream.read(256 * 1024 + 1)
        if len(data) > 256 * 1024:
            raise ValueError("configuration exceeds the update reader limit")
        value = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("configuration is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("configuration must be a JSON object")
    return validate_config(value.get("software_updates", {}))


@total_ordering
class _SemanticVersion:
    def __init__(self, value):
        if not isinstance(value, str) or len(value) > 80:
            raise ValueError("version must be a bounded semantic version")
        match = _VERSION.fullmatch(value)
        if not match:
            raise ValueError("version must use major.minor.patch with optional prerelease/build")
        self.parts = tuple(int(part) for part in match.group(1, 2, 3))
        self.prerelease = tuple((match.group(4) or "").split(".")) if match.group(4) else ()
        for part in self.prerelease:
            if len(part) > 32 or (part.isdigit() and len(part) > 1 and part.startswith("0")):
                raise ValueError("version has an invalid prerelease identifier")
        if match.group(5) and any(len(part) > 32 for part in match.group(5).split(".")):
            raise ValueError("version has an invalid build identifier")
        self.text = value.removeprefix("v")

    def __eq__(self, other):
        if not isinstance(other, _SemanticVersion):
            return NotImplemented
        return self.parts == other.parts and self.prerelease == other.prerelease

    def __lt__(self, other):
        if not isinstance(other, _SemanticVersion):
            return NotImplemented
        if self.parts != other.parts:
            return self.parts < other.parts
        if not self.prerelease or not other.prerelease:
            return bool(self.prerelease) and not other.prerelease
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            if left.isdigit() and right.isdigit():
                return int(left) < int(right)
            if left.isdigit() != right.isdigit():
                return left.isdigit()
            return left < right
        return len(self.prerelease) < len(other.prerelease)


def _current(value):
    if value is None:
        from . import __version__
        value = __version__
    return _SemanticVersion(value)


def _timestamp(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and 0 <= value <= 10**12 and math.isfinite(value))


def _published(value):
    if (not isinstance(value, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value)):
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def _release(value, repository):
    if not isinstance(value, dict) or type(value.get("draft")) is not bool:
        return None
    if value["draft"] or type(value.get("prerelease")) is not bool:
        return None
    tag = value.get("tag_name")
    try:
        version = _SemanticVersion(tag)
    except ValueError:
        return None
    release_id = value.get("id")
    if type(release_id) is not int or not 1 <= release_id <= 10**18:
        return None
    if not _published(value.get("published_at")):
        return None
    url = value.get("html_url")
    if not isinstance(url, str) or len(url) > 512:
        return None
    try:
        parsed = parse.urlsplit(url)
        decoded_path = parse.unquote(parsed.path)
        prefix = "/" + repository + "/releases/tag/"
        if (parsed.scheme != "https" or parsed.netloc != "github.com"
                or parsed.query or parsed.fragment or not decoded_path.lower().startswith(prefix)
                or decoded_path[len(prefix):] != tag):
            return None
    except ValueError:
        return None
    api_url = value.get("url")
    if api_url is not None and (not isinstance(api_url, str) or api_url.lower()
                               != f"https://api.github.com/repos/{repository}/releases/{release_id}"):
        return None
    # Ignore release prose, assets and author fields: they are not needed to notify.
    return {
        "id": release_id, "tag_name": tag, "version": version.text,
        "draft": False, "prerelease": value["prerelease"],
        "html_url": url, "published_at": value["published_at"],
    }


def _paths(home, config):
    home = Path(home).expanduser().resolve()
    key = hashlib.sha256((config["repository"] + "/" + config["channel"]).encode()).hexdigest()[:20]
    stem = home / ("software-update-" + key)
    return home, Path(str(stem) + ".json"), Path(str(stem) + ".lease.json"), Path(str(stem) + ".lock")


def _read_json(path, limit):
    try:
        if path.is_symlink():
            return None
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            return None
        return json.loads(data.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, OverflowError, RecursionError):
        return None


def _etag(value):
    return (value if isinstance(value, str) and 1 <= len(value) <= 512
            and all(32 <= ord(char) <= 126 for char in value) else None)


def _cache(path, config):
    value = _read_json(path, _MAX_CACHE_BYTES)
    if (not isinstance(value, dict) or type(value.get("schema")) is not int or value.get("schema") != 1
            or value.get("repository") != config["repository"]
            or value.get("channel") != config["channel"]):
        return None
    if any(not _timestamp(value.get(key, 0)) for key in
           ("checked_at", "last_attempt_at", "next_check_at", "backoff_until")):
        return None
    if type(value.get("failures", 0)) is not int or not 0 <= value.get("failures", 0) <= 32:
        return None
    poll_interval = value.get("poll_interval", 0)
    if type(poll_interval) is not int or not 0 <= poll_interval <= 10**10:
        return None
    releases = value.get("releases")
    if not isinstance(releases, list) or len(releases) > 20:
        return None
    clean = [_release(item, config["repository"]) for item in releases]
    if any(item is None for item in clean):
        return None
    reason = value.get("error_reason")
    if (reason is not None and not isinstance(reason, str)) or reason not in {None, "network_error", "invalid_response", "response_too_large",
                      "invalid_json", "redirect_refused", "rate_limited", "http_error",
                      "not_found", "cache_missing_for_304"}:
        return None
    clean_cache = {"schema": 1, "repository": config["repository"], "channel": config["channel"],
            "releases": clean, "etag": _etag(value.get("etag")), "error_reason": reason,
            "failures": value.get("failures", 0), "poll_interval": poll_interval,
            **{key: value.get(key, 0) for key in
               ("checked_at", "last_attempt_at", "next_check_at", "backoff_until")}}
    if clean_cache["checked_at"] and not reason:
        clean_cache["next_check_at"] = clean_cache["checked_at"] + max(config["interval_seconds"], poll_interval)
    return clean_cache


def _lease(path):
    value = _read_json(path, 1024)
    if (not isinstance(value, dict) or not isinstance(value.get("kind"), str)
            or value["kind"] not in {"reserved", "running"}
            or not re.fullmatch(r"[a-f0-9]{32}", str(value.get("token", "")))
            or not _timestamp(value.get("created_at"))
            or not _timestamp(value.get("expires_at"))
            or not 0 < value["expires_at"] - value["created_at"] <= 60
            or value["expires_at"] > time.time() + 60):
        return None
    return value


def _atomic_json(path, value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def _locked(path):
    if path.is_symlink():
        raise ValueError("software update lock must not be a symbolic link")
    stream = path.open("a+b")
    acquired = False
    try:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (OSError, BlockingIOError):
            pass
        yield acquired
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _clear_lease(path, token):
    lease = _lease(path)
    if lease and lease["token"] == token:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _due(cache, now, force=False):
    if (cache and cache["backoff_until"] > now
            and (not force or cache["error_reason"] == "rate_limited")):
        return False
    return force or not cache or cache["next_check_at"] <= now


def _status(config, current, cache, now, lease=None):
    eligible = []
    for item in (cache or {}).get("releases", []):
        version = _SemanticVersion(item["version"])
        if config["channel"] == "stable" and (item["prerelease"] or version.prerelease):
            continue
        eligible.append((version, item["published_at"], item["id"], item))
    latest = max(eligible, key=lambda candidate: candidate[:3], default=None)
    item = latest[-1] if latest else None
    available = bool(latest and current < latest[0])
    checked_at = (cache or {}).get("checked_at", 0)
    reason = (cache or {}).get("error_reason")
    if reason:
        state = "rate_limited" if reason == "rate_limited" else "error"
    elif not checked_at:
        state = "unchecked"
    elif not latest:
        state = "no_eligible_release"
    elif available:
        state = "update_available"
    elif latest[0] < current:
        state = "ahead"
    else:
        state = "up_to_date"
    if lease and lease["expires_at"] > now:
        state = "checking"
    age = max(0, now - checked_at) if checked_at else None
    return {
        "state": state, "automatic_enabled": config["enabled"],
        "repository": config["repository"], "channel": config["channel"],
        "current": current.text, "latest": item["version"] if item else None,
        "latest_tag": item["tag_name"] if item else None,
        "latest_release_id": item["id"] if item else None,
        "update_available": available, "verified_release_url": item["html_url"] if item else None,
        "published_at": item["published_at"] if item else None,
        "checked_at": checked_at or None,
        "last_attempt_at": (cache or {}).get("last_attempt_at") or None,
        "next_check_at": (cache or {}).get("next_check_at") or None,
        "cache_age_seconds": age,
        "stale": age is None or age >= config["interval_seconds"],
        "error_reason": reason,
        "downloaded": False, "installed": False,
        "release_window_limit": 20,
    }


def update_status(home, config, current_version=None):
    """Pure cached read. It does not make directories, locks, requests, or databases."""
    config = validate_config(config)
    current = _current(current_version)
    _, cache_path, lease_path, _ = _paths(home, config)
    return _status(config, current, _cache(cache_path, config), time.time(), _lease(lease_path))


def automatic_check_due(home, config):
    config = validate_config(config)
    if not config["enabled"]:
        return False
    _, cache_path, lease_path, _ = _paths(home, config)
    now = time.time()
    lease = _lease(lease_path)
    return not (lease and lease["expires_at"] > now) and _due(_cache(cache_path, config), now)


def reserve_automatic_check(home, config):
    """Reserve one short background launch, without performing network I/O."""
    config = validate_config(config)
    if not config["enabled"]:
        return None
    directory, cache_path, lease_path, lock_path = _paths(home, config)
    directory.mkdir(parents=True, exist_ok=True)
    with _locked(lock_path) as acquired:
        if not acquired:
            return None
        now = time.time()
        lease = _lease(lease_path)
        if (lease and lease["expires_at"] > now) or not _due(_cache(cache_path, config), now):
            return None
        token = uuid.uuid4().hex
        _atomic_json(lease_path, {"kind": "reserved", "token": token,
                                  "created_at": now, "expires_at": now + _RESERVATION_SECONDS})
        return token


def cancel_reserved_check(home, config, token):
    config = validate_config(config)
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
        return False
    directory, _, lease_path, lock_path = _paths(home, config)
    if not directory.exists():
        return False
    with _locked(lock_path) as acquired:
        if not acquired:
            return False
        lease = _lease(lease_path)
        if not lease or lease["token"] != token or lease["kind"] != "reserved":
            return False
        _clear_lease(lease_path, token)
        return True


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _FetchFailure(ValueError):
    pass


def _fetch(repository, etag, timeout):
    """Socket timeout plus a response-read deadline; DNS is OS controlled.

    This is not a guaranteed whole-process wall-clock timeout. Automatic callers
    run it outside the task process, and a file lock prevents duplicate requests.
    """
    url = f"https://api.github.com/repos/{repository}/releases?per_page=20"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION,
               "User-Agent": "agents-learning-update-check"}
    if etag:
        headers["If-None-Match"] = etag
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
    req = request.Request(url, headers=headers, method="GET")
    started = time.monotonic()
    try:
        response = opener.open(req, timeout=timeout)
    except error.HTTPError as exc:
        try:
            return exc.code, dict(exc.headers.items()), None
        finally:
            exc.close()
    with response:
        status = response.getcode()
        response_headers = dict(response.headers.items())
        if response.geturl() != url:
            raise _FetchFailure("redirect_refused")
        if status != 200:
            return status, response_headers, None
        length = response.headers.get("Content-Length")
        if length and (not length.isdigit() or int(length) > _MAX_RESPONSE_BYTES):
            raise _FetchFailure("response_too_large")
        chunks, size = [], 0
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("update response deadline exceeded")
            # The socket timeout is shortened for each read, preventing slow bodies
            # from receiving a fresh full timeout for every packet.
            raw_socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if raw_socket is not None:
                raw_socket.settimeout(remaining)
            read = getattr(response, "read1", response.read)
            chunk = read(min(8192, _MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_RESPONSE_BYTES:
                raise _FetchFailure("response_too_large")
            chunks.append(chunk)
        try:
            payload = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeError, ValueError, OverflowError, RecursionError) as exc:
            raise _FetchFailure("invalid_json") from exc
        return status, response_headers, payload


def _retry_at(headers, now, failures):
    headers = {str(key).lower(): value for key, value in headers.items()}
    wait = min(86400, 60 * (2 ** min(failures - 1, 10)))
    retry = headers.get("retry-after")
    if isinstance(retry, str):
        if retry.isdigit() and len(retry) <= 10:
            wait = max(wait, int(retry))
        else:
            try:
                from email.utils import parsedate_to_datetime
                stamp = parsedate_to_datetime(retry)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                wait = max(wait, stamp.timestamp() - now)
            except (ValueError, TypeError, OverflowError):
                pass
    reset = headers.get("x-ratelimit-reset")
    if (headers.get("x-ratelimit-remaining") == "0" and isinstance(reset, str)
            and reset.isdigit() and len(reset) <= 12):
        wait = max(wait, int(reset) - now)
    return now + wait


def check_updates(home, config, force=False, current_version=None, reservation=None):
    """Check metadata once. ``force`` skips TTL, but respects active rate backoff.

    A reservation is valid only for automatic checks while configuration is enabled.
    Cache state is recomputed against the current installed version on every read.
    """
    config = validate_config(config)
    current = _current(current_version)
    if not isinstance(force, bool):
        raise ValueError("force must be a boolean")
    if reservation is not None and (not isinstance(reservation, str)
                                   or not re.fullmatch(r"[a-f0-9]{32}", reservation)):
        raise ValueError("invalid update reservation")
    if reservation is not None and not config["enabled"]:
        cancel_reserved_check(home, config, reservation)
        return update_status(home, config, current.text)
    directory, cache_path, lease_path, lock_path = _paths(home, config)
    directory.mkdir(parents=True, exist_ok=True)
    with _locked(lock_path) as acquired:
        if not acquired:
            result = update_status(home, config, current.text)
            result["state"] = "checking"
            return result
        now = time.time()
        cache = _cache(cache_path, config)
        lease = _lease(lease_path)
        if reservation is not None:
            if (not lease or lease["kind"] != "reserved" or lease["token"] != reservation
                    or lease["expires_at"] <= now):
                if lease and lease["token"] == reservation:
                    _clear_lease(lease_path, reservation)
                result = _status(config, current, cache, now, _lease(lease_path))
                result["error_reason"] = "reservation_expired"
                return result
        elif lease and lease["expires_at"] > now:
            return _status(config, current, cache, now, lease)
        if not _due(cache, now, force):
            if reservation:
                _clear_lease(lease_path, reservation)
            return _status(config, current, cache, now)
        token = reservation or uuid.uuid4().hex
        _atomic_json(lease_path, {"kind": "running", "token": token,
                                  "created_at": now,
                                  "expires_at": now + max(30, config["timeout_seconds"] + 10)})
        stored = cache or {"schema": 1, "repository": config["repository"],
                           "channel": config["channel"], "releases": [], "etag": None,
                           "checked_at": 0, "last_attempt_at": 0, "next_check_at": 0,
                           "backoff_until": 0, "error_reason": None, "failures": 0, "poll_interval": 0}
        headers = {}
        reason = None
        try:
            try:
                code, headers, payload = _fetch(config["repository"], stored["etag"], config["timeout_seconds"])
                if code == 304:
                    if not stored["checked_at"]:
                        reason = "cache_missing_for_304"
                elif code == 200:
                    if not isinstance(payload, list) or len(payload) > 20:
                        reason = "invalid_response"
                    else:
                        stored["releases"] = [clean for item in payload
                                              if (clean := _release(item, config["repository"])) is not None]
                elif code in {403, 429}:
                    reason = "rate_limited"
                elif code in {301, 302, 303, 307, 308}:
                    reason = "redirect_refused"
                elif code == 404:
                    reason = "not_found"
                else:
                    reason = "http_error"
            except _FetchFailure as exc:
                reason = str(exc)
            except (OSError, ValueError, error.URLError, TimeoutError):
                reason = "network_error"
            finished = time.time()
            stored["last_attempt_at"] = finished
            stored["error_reason"] = reason
            if reason:
                stored["failures"] = min(32, stored["failures"] + 1)
                if reason == "rate_limited":
                    until = _retry_at(headers, finished, stored["failures"])
                elif reason == "not_found":
                    until = finished + config["interval_seconds"]
                else:
                    until = finished + min(3600, 60 * (2 ** min(stored["failures"] - 1, 6)))
                stored["backoff_until"] = until
                stored["next_check_at"] = until
            else:
                normalized = {str(key).lower(): value for key, value in headers.items()}
                response_etag = _etag(normalized.get("etag"))
                stored["etag"] = (response_etag or stored["etag"]) if code == 304 else response_etag
                stored["checked_at"] = finished
                stored["failures"] = 0
                stored["backoff_until"] = 0
                poll = normalized.get("x-poll-interval")
                if isinstance(poll, str) and poll.isdigit() and len(poll) <= 10:
                    stored["poll_interval"] = int(poll)
                elif code != 304:
                    stored["poll_interval"] = 0
                stored["next_check_at"] = finished + max(config["interval_seconds"], stored["poll_interval"])
            _atomic_json(cache_path, stored)
            return _status(config, current, stored, finished)
        finally:
            _clear_lease(lease_path, token)
