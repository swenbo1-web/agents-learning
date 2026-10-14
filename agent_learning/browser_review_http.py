"""Same-origin, explicitly enabled browser review endpoints."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from .runtime import ContractError

MAX_REVIEW_BODY = 32768
MAX_REVIEW_PLANS = 64
REVIEW_PLAN_SECONDS = 900


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate JSON field")
        result[key] = value
    return result


class ReviewHandlerMixin:
    def do_POST(self):
        if urlsplit(self.path).path.startswith("/api/admin-control/"):
            from .browser_admin_http import handle_admin_post
            return handle_admin_post(self)
        if not self.server.review_enabled:
            return self._write_denied()
        if not self._boundary():
            self.close_connection = True
            return
        try:
            lengths = self.headers.get_all("Content-Length") or []
            if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal()
                    or self.headers.get_all("Transfer-Encoding")):
                raise ContractError("one valid Content-Length is required")
            length = int(lengths[0])
            if not 0 < length <= MAX_REVIEW_BODY:
                self._send(413, {"error": "review request exceeds budget"})
                return
            old_timeout = self.request.gettimeout()
            try:
                self.request.settimeout(2)
                raw = self.rfile.read(length)
            finally:
                self.request.settimeout(old_timeout)
            if len(raw) != length:
                raise ContractError("incomplete review request")
            headers = self.headers.get_all("Authorization") or []
            review_headers = self.headers.get_all("X-Review-Token") or []
            if (len(headers) != 1 or not headers[0].isascii()
                    or not secrets.compare_digest(headers[0], "Bearer " + self.server.access_token)
                    or len(review_headers) != 1 or not review_headers[0].isascii()
                    or not secrets.compare_digest(review_headers[0], self.server.review_token)):
                self._send(403, {"error": "review access key required"})
                return
            if self.headers.get_all("Origin") != [self.server.origin]:
                self._send(403, {"error": "same-origin review request required"})
                return
            types = self.headers.get_all("Content-Type") or []
            if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
                raise ContractError("review requests require application/json")
            parsed = urlsplit(self.path)
            if parsed.query or parsed.path not in {"/api/review/prepare", "/api/review/commit"}:
                self._send(404, {"error": "unknown review endpoint"})
                return
            body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
            if not isinstance(body, dict):
                raise ContractError("review request must be an object")
            result = self._prepare_review(body) if parsed.path.endswith("/prepare") else self._commit_review(body)
            self._send(200, result)
        except (ContractError, ValueError, UnicodeError) as exc:
            self._send(400, {"error": str(exc)})
        except LookupError:
            self._send(404, {"error": "candidate not found in this project"})
        except RecursionError:
            self._send(400, {"error": "review request is too deeply nested"})
        except sqlite3.DatabaseError:
            self._send(503, {"error": "review storage unavailable; refresh and retry"})
        except (OSError, TypeError, KeyError, AttributeError):
            self._send(503, {"error": "review unavailable; inspect local configuration"})
        finally:
            self.close_connection = True

    def _review_runtime(self):
        from .runtime import Runtime
        return Runtime(self.server.database.parent, host=self.server.runtime_host,
                       host_instance=self.server.runtime_instance)

    def _prepare_review(self, body):
        from .candidate_review import MAX_BATCH, prepare_review
        if set(body) - {"candidate_ids", "action", "reason"} or not {"candidate_ids", "action"} <= set(body):
            raise ContractError("invalid review preview fields")
        identities = body["candidate_ids"]
        if (not isinstance(identities, list) or not 1 <= len(identities) <= MAX_BATCH
                or any(not isinstance(value, str) or not value.strip() for value in identities)):
            raise ContractError(f"select 1..{MAX_BATCH} candidate IDs")
        runtime = self._review_runtime()
        try:
            plan = prepare_review(runtime, self.server.project, identities,
                                  body["action"], reason=body.get("reason", ""))
        finally:
            runtime.close()
        now = time.monotonic()
        plan_id = secrets.token_hex(24)
        with self.server.review_lock:
            self.server.review_plans = {key: value for key, value in self.server.review_plans.items()
                                        if value["deadline"] > now}
            if len(self.server.review_plans) >= MAX_REVIEW_PLANS:
                raise ContractError("too many pending review plans; wait for expiry")
            self.server.review_plans[plan_id] = {"plan": plan, "deadline": now + REVIEW_PLAN_SECONDS}
        return {"plan_id": plan_id, "plan": plan, "expires_at": time.time() + REVIEW_PLAN_SECONDS}

    def _commit_review(self, body):
        from .candidate_review import commit_review
        if set(body) != {"plan_id", "request_id"}:
            raise ContractError("invalid review commit fields")
        plan_id, request_id = body["plan_id"], body["request_id"]
        if (not isinstance(plan_id, str) or len(plan_id) > 100
                or not isinstance(request_id, str) or not 1 <= len(request_id) <= 200):
            raise ContractError("invalid review request identity")
        with self.server.review_lock:
            saved = self.server.review_plans.get(plan_id)
            if not saved or saved["deadline"] <= time.monotonic():
                raise ContractError("review preview expired; prepare it again")
            plan = saved["plan"]
        runtime = self._review_runtime()
        try:
            return commit_review(runtime, self.server.project, plan, request_id)
        finally:
            runtime.close()
