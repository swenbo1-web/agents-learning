"""Separate capability for settings and model-backed daily review admission."""
import json
import secrets
import sqlite3
from urllib.parse import urlsplit

from .browser_review_http import MAX_REVIEW_BODY, _unique_fields
from .runtime import ContractError


def handle_admin_post(handler):
    if not handler.server.admin_control_enabled:
        return handler._write_denied()
    if not handler._boundary():
        handler.close_connection = True
        return
    try:
        authorization = handler.headers.get_all("Authorization") or []
        controls = handler.headers.get_all("X-Admin-Token") or []
        if (len(authorization) != 1 or not authorization[0].isascii()
                or not secrets.compare_digest(authorization[0], "Bearer " + handler.server.access_token)
                or len(controls) != 1 or not controls[0].isascii()
                or not secrets.compare_digest(controls[0], handler.server.admin_token)
                or handler.headers.get_all("Origin") != [handler.server.origin]):
            handler._send(403, {"error": "same-origin administrator capability required"})
            return
        lengths = handler.headers.get_all("Content-Length") or []
        if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal()
                or handler.headers.get_all("Transfer-Encoding")):
            raise ContractError("one valid Content-Length is required")
        length = int(lengths[0])
        if not 0 < length <= MAX_REVIEW_BODY:
            handler._send(413, {"error": "administrator request exceeds budget"})
            return
        content_types = handler.headers.get_all("Content-Type") or []
        if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
            raise ContractError("administrator requests require application/json")
        parsed = urlsplit(handler.path)
        if parsed.query or parsed.path not in {"/api/admin-control/configure", "/api/admin-control/wake"}:
            handler._send(404, {"error": "unknown administrator endpoint"})
            return
        old_timeout = handler.request.gettimeout()
        try:
            handler.request.settimeout(2)
            raw = handler.rfile.read(length)
        finally:
            handler.request.settimeout(old_timeout)
        if len(raw) != length:
            raise ContractError("incomplete administrator request")
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
        from .admin_control import apply_control
        runtime = handler._review_runtime()
        try:
            result = apply_control(runtime, handler.server.project, body, wake=parsed.path.endswith("/wake"))
        finally:
            runtime.close()
        handler._send(200, result)
    except (ContractError, ValueError, UnicodeError) as exc:
        handler._send(400, {"error": str(exc)})
    except RecursionError:
        handler._send(400, {"error": "administrator request is too deeply nested"})
    except (sqlite3.DatabaseError, OSError, TypeError, KeyError, AttributeError):
        handler._send(503, {"error": "administrator control unavailable; inspect local configuration"})
    finally:
        handler.close_connection = True
