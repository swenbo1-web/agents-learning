"""Project-scoped knowledge browser using only the standard library.

Default viewing does not initialize Runtime or write storage. Explicit review
mode uses a separate capability and fixed candidate plans for user decisions.
Access keys belong to this process and are never persisted.
"""
from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

from .case_store import CaseStoreMixin
from .knowledge_releases import KnowledgeReleaseMixin
from .knowledge_store import KnowledgeStoreMixin
from .publication_state import PublicationStateMixin
from .browser_review_http import ReviewHandlerMixin
from .browser_ui import HTML, CSS, JAVASCRIPT
from .runtime import CATEGORIES, KNOWLEDGE_KINDS, ContractError, clean


MAX_PAGE = 50
MAX_OFFSET = 10000
MAX_FIELD_PAGE = 8000
MAX_RESPONSE_BYTES = 256000
MAX_DISCARD_BYTES = 8192
UI_LANGUAGES = {"auto", "zh-CN", "en"}


def _connection(database):
    path = Path(database).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ContractError("browser database must be an existing file")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                timeout=1, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    # A live WAL database must remain live: immutable=1 would silently ignore WAL.
    return connection


def browser_projects(database):
    """List registered projects without opening the writable runtime."""
    with contextlib.closing(_connection(database)) as db:
        _schema(db)
        return [row[0] for row in db.execute("SELECT id FROM projects ORDER BY id")]


list_browser_projects = browser_projects


def resolve_browser_project(database, workspace, project_id=None):
    """Resolve a registered path or explicitly selected ID without side effects."""
    with contextlib.closing(_connection(database)) as db:
        _schema(db)
        if project_id is not None:
            if not isinstance(project_id, str) or not db.execute(
                    "SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise ContractError("unknown browser project")
            return project_id
        path = os.path.normcase(str(Path(workspace).expanduser().resolve()))
        row = next((item for item in db.execute("SELECT path,project FROM project_paths ORDER BY length(path) DESC")
                    if path == item["path"] or path.startswith(item["path"].rstrip(os.sep) + os.sep)), None)
        if not row or not db.execute("SELECT 1 FROM projects WHERE id=?", (row["project"],)).fetchone():
            raise ContractError("workspace is not registered; select an existing --project-id")
        return row["project"]


def _schema(db):
    if db.execute("PRAGMA user_version").fetchone()[0] not in {11, 12, 13, 14, 15, 16, 17, 18, 19}:
        raise ContractError("browser requires database schema 11..19; run an explicit upgrade first")


class _ReadModel(KnowledgeStoreMixin, CaseStoreMixin, KnowledgeReleaseMixin,
                 PublicationStateMixin):
    """Only pure read helpers are used; query_only enforces that contract."""
    def __init__(self, db):
        self.db = db


def _integer(params, name, default, maximum, minimum=0):
    value = params.get(name, str(default))
    if not value.isascii() or not value.isdecimal() or not minimum <= int(value) <= maximum:
        raise ContractError(f"invalid {name}")
    return int(value)


def _text(params, name, default="", maximum=200):
    value = params.get(name, default)
    if len(value) > maximum or any(ord(ch) < 32 for ch in value):
        raise ContractError(f"invalid {name}")
    return value


def _page(items, total, offset, limit):
    return {"items": items, "total": total, "offset": offset, "limit": limit,
            "next_offset": offset + len(items) if offset + len(items) < total else None}


def _preview(row, public=None):
    body = clean(json.loads(row["body"]))
    result = {"knowledge_id": row["id"], "revision": row["revision"],
              "title": str(body.get("title", ""))[:250],
              "preview": str(body.get("content", ""))[:350],
              "category": body.get("category", "unknown"),
              "knowledge_kind": body.get("knowledge_kind", "solution"),
              "validation_level": body.get("validation_level", "reported"),
              "created": row["created"], "active_revision": row["active_revision"],
              "state": "active" if row["active_revision"] == row["revision"] else
                       "revoked" if row["active_revision"] is None else "historical"}
    if public is not None:
        for key in ("source_health", "current_tool_success", "problem_changed",
                    "source_version", "maturity", "pending_counterexample_count"):
            result[key] = public[key]
    return result


class BrowserServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, database, project, *, host="127.0.0.1", port=0, token=None,
                 review=False, admin_control=False, language="auto", runtime_host="codex", runtime_instance="default",
                 access_guard=None):
        if host != "127.0.0.1":
            raise ContractError("browser host must be 127.0.0.1")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ContractError("invalid browser port")
        if not isinstance(project, str) or not project.strip() or len(project) > 500:
            raise ContractError("browser requires an explicit project ID")
        if token is not None and (not isinstance(token, str) or len(token) < 32 or len(token) > 200
                                  or not token.isascii() or not token.isalnum()):
            raise ContractError("browser token must be 32..200 ASCII letters or digits")
        if access_guard is not None and not callable(access_guard):
            raise ContractError("browser access guard must be callable")
        self.access_guard = access_guard
        self.database = Path(database).expanduser().resolve(strict=True)
        if not isinstance(review, bool) or not isinstance(admin_control, bool) or not isinstance(language, str) or language not in UI_LANGUAGES:
            raise ContractError("invalid browser mode or language")
        from .host_sessions import host_scope_key
        host_scope_key(runtime_host, runtime_instance)
        with contextlib.closing(_connection(self.database)) as db:
            _schema(db)
            if review and (db.execute("PRAGMA user_version").fetchone()[0] not in {15, 16, 17, 18, 19}
                           or self.database.name != "runtime.sqlite3"):
                raise ContractError("review mode requires an existing schema 15..19 runtime database")
            if admin_control and (db.execute("PRAGMA user_version").fetchone()[0] not in {16, 17, 18, 19} or self.database.name != "runtime.sqlite3"):
                raise ContractError("administrator control requires an explicitly upgraded schema 16..19 runtime database")
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone():
                raise ContractError("unknown browser project")
        self.project = project
        self.review_enabled = review
        self.admin_control_enabled = admin_control
        self.language = language
        self.runtime_host = runtime_host
        self.runtime_instance = runtime_instance
        self.access_token = token or secrets.token_hex(24)
        self.review_token = secrets.token_hex(32) if review else None
        self.admin_token = secrets.token_hex(32) if admin_control else None
        self.review_plans = {}
        self.review_lock = threading.Lock()
        super().__init__((host, port), _Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}/"
        self.origin = self.url.rstrip("/")
        self.browser_url = self.url + "#token=" + quote(self.access_token, safe="")
        if self.review_enabled:
            self.browser_url += "&review_token=" + self.review_token
        if self.admin_control_enabled:
            self.browser_url += "&admin_token=" + self.admin_token

    @contextlib.contextmanager
    def snapshot(self):
        with contextlib.closing(_connection(self.database)) as db:
            _schema(db)
            deadline, checks = time.monotonic() + 2, [0]

            def budget():
                checks[0] += 1
                return checks[0] > 4000 or time.monotonic() > deadline

            db.set_progress_handler(budget, 1000)
            db.execute("BEGIN")
            try:
                # Pin a snapshot before any response field is assembled.
                if not db.execute("SELECT 1 FROM projects WHERE id=?", (self.project,)).fetchone():
                    raise ContractError("browser project no longer exists")
                yield db
            finally:
                if db.in_transaction:
                    db.execute("ROLLBACK")


def make_browser_server(database, project, *, host="127.0.0.1", port=0, token=None,
                        review=False, admin_control=False, language="auto", runtime_host="codex", runtime_instance="default",
                        access_guard=None):
    return BrowserServer(database, project, host=host, port=port, token=token,
                         review=review, admin_control=admin_control, language=language, runtime_host=runtime_host,
                         runtime_instance=runtime_instance, access_guard=access_guard)


class _Handler(ReviewHandlerMixin, BaseHTTPRequestHandler):
    server_version = "AgentLearningBrowser"
    sys_version = ""

    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, format, *args):
        # Neither URL parameters nor access keys enter terminal/file logs.
        return

    def _send(self, status, data, content_type="application/json; charset=utf-8"):
        payload = (json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                   if not isinstance(data, bytes) else data)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                         "style-src 'self'; connect-src 'self'; img-src 'none'; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _boundary(self):
        guard = self.server.access_guard
        if guard is not None:
            try:
                valid = guard() is True
            except Exception:
                valid = False
            if not valid:
                self._send(403, {"error": "browser access expired; reopen the current project panel"})
                return False
        expected = "127.0.0.1:" + str(self.server.server_address[1])
        if self.headers.get_all("Host") != [expected]:
            self._send(403, {"error": "invalid host"})
            return False
        origins = self.headers.get_all("Origin") or []
        if origins and origins != [self.server.origin]:
            self._send(403, {"error": "invalid origin"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._send(403, {"error": "cross-site request denied"})
            return False
        return True

    def do_GET(self):
        if not self._boundary():
            return
        if len(self.path) > 4000:
            self._send(414, {"error": "request URL too long"})
            return
        parsed = urlsplit(self.path)
        static = {"/": (HTML, "text/html; charset=utf-8"),
                  "/app.js": (JAVASCRIPT, "text/javascript; charset=utf-8"),
                  "/style.css": (CSS, "text/css; charset=utf-8")}
        if parsed.path in static and not parsed.query:
            content, mime = static[parsed.path]
            self._send(200, content.encode("utf-8"), mime)
            return
        if not parsed.path.startswith("/api/"):
            self._send(404, {"error": "not found"})
            return
        headers = self.headers.get_all("Authorization") or []
        if len(headers) != 1 or not headers[0].isascii() or not secrets.compare_digest(headers[0], "Bearer " + self.server.access_token):
            self._send(401, {"error": "access key required"})
            return
        try:
            # Python 3.10 的严格解析把空查询视为坏字段；无参数的 API 路由合法。
            # 非空查询仍严格检查空片段、重复字段和字段数量。
            parsed_params = (parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True,
                                      max_num_fields=12) if parsed.query else {})
            if any(len(values) != 1 for values in parsed_params.values()):
                raise ContractError("duplicate query parameter")
            params = {key: values[0] for key, values in parsed_params.items()}
            if parsed.path == "/api/session":
                if params:
                    raise ContractError("session endpoint takes no query parameters")
                result = {"project": self.server.project, "review_enabled": self.server.review_enabled,
                          "admin_control_enabled": self.server.admin_control_enabled,
                          "read_only": not (self.server.review_enabled or self.server.admin_control_enabled), "language": self.server.language}
            elif parsed.path in {"/api/admin-control", "/api/admin-control/receipt", "/api/admin-control/publication-preview"}:
                if not self.server.admin_control_enabled:
                    raise ContractError("administrator control capability was not opened")
                from .admin_control import readonly_runtime, control_status, request_receipt
                with self.server.snapshot() as db:
                    view = readonly_runtime(self.server.database, db, self.server.runtime_host, self.server.runtime_instance)
                    if parsed.path.endswith("/publication-preview"):
                        if set(params) != {"candidate_id"}:
                            raise ContractError("publication preview requires candidate_id")
                        from .admin_publication import publication_preview
                        result = publication_preview(view, self.server.project, params["candidate_id"])
                    elif parsed.path.endswith("/receipt"):
                        if set(params) != {"request_id"}:
                            raise ContractError("receipt requires request_id")
                        result = request_receipt(view, self.server.project, params["request_id"])
                    else:
                        if params:
                            raise ContractError("administrator status takes no parameters")
                        result = control_status(view, self.server.project)
            else:
                with self.server.snapshot() as db:
                    result = _read_api(db, self.server.project, parsed.path, params)
            if parsed.path == "/api/stats":
                result["read_only"] = not (self.server.review_enabled or self.server.admin_control_enabled)
            payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(payload) > MAX_RESPONSE_BYTES:
                self._send(413, {"error": "response exceeds browser budget; reduce page size"})
                return
            self._send(200, payload)
        except json.JSONDecodeError:
            self._send(503, {"error": "stored data is not valid JSON"})
        except (ContractError, ValueError) as exc:
            self._send(400, {"error": str(exc)})
        except LookupError:
            self._send(404, {"error": "item not found in this project"})
        except sqlite3.DatabaseError:
            self._send(503, {"error": "read unavailable or query budget exceeded"})
        except (TypeError, KeyError, AttributeError):
            self._send(503, {"error": "stored data does not match the browser contract"})

    def _write_denied(self):
        # Small rejected bodies must be consumed before closing the socket;
        # unread bytes can reset TCP and hide the 405 response on Windows.
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) == 1 and not self.headers.get_all("Transfer-Encoding"):
            try:
                length = int(lengths[0])
            except ValueError:
                length = -1
            if 0 < length <= MAX_DISCARD_BYTES:
                timeout = self.request.gettimeout()
                try:
                    self.request.settimeout(0.25)
                    self.rfile.read(length)
                except (OSError, ValueError):
                    pass
                finally:
                    self.request.settimeout(timeout)
        if self._boundary():
            self._send(405, {"error": "browser is read-only"})
        self.close_connection = True

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = _write_denied

    def do_HEAD(self):
        self.do_GET()


def _read_api(db, project, route, params):
    contracts = {
        "/api/stats": set(),
        "/api/knowledge-list": {"q", "state", "category", "kind", "offset", "limit"},
        "/api/knowledge": {"knowledge_id", "revision", "section", "offset", "limit"},
        "/api/revisions": {"knowledge_id", "offset", "limit"},
        "/api/actions": {"knowledge_id", "offset", "limit"},
        "/api/events": {"knowledge_id", "offset", "limit"},
        "/api/admin": {"offset", "limit"},
        "/api/candidates": {"status", "q", "offset", "limit"},
        "/api/candidate": {"candidate_id", "section", "offset", "limit", "evidence_offset", "evidence_limit"},
        "/api/review-receipt": {"request_id"},
    }
    if route not in contracts:
        raise LookupError
    if set(params) - contracts[route]:
        raise ContractError("unsupported query parameter")
    model = _ReadModel(db)
    if route == "/api/review-receipt":
        from .runtime import digest
        request_id = _text(params, "request_id", maximum=200)
        if not request_id.strip():
            raise ContractError("review request_id is required")
        event_id = digest(["candidate-review", project, request_id])
        event = db.execute("SELECT payload FROM events WHERE project=? AND id=? AND type='CandidateReview'",
                           (project, event_id)).fetchone()
        if event is None:
            return {"found": False}
        receipt = json.loads(event[0])
        return {"found": True, "plan_digest": receipt["plan_digest"], "response": receipt["response"]}
    if route in {"/api/candidates", "/api/candidate"}:
        from .candidate_review import candidate_review_list, candidate_review_detail
        if route == "/api/candidates":
            return candidate_review_list(model, project, status=_text(params, "status", "pending"),
                q=_text(params, "q"), offset=_integer(params, "offset", 0, MAX_OFFSET),
                limit=_integer(params, "limit", 20, MAX_PAGE, 1))
        return candidate_review_detail(model, project, _text(params, "candidate_id", maximum=200),
            section=_text(params, "section", "content"),
            offset=_integer(params, "offset", 0, 1000000),
            limit=_integer(params, "limit", 4000, MAX_FIELD_PAGE, 1),
            evidence_offset=_integer(params, "evidence_offset", 0, MAX_OFFSET),
            evidence_limit=_integer(params, "evidence_limit", 10, 20, 1))
    if route == "/api/stats":
        count = lambda table, suffix="": db.execute(
            "SELECT COUNT(*) FROM " + table + " WHERE project=?" + suffix, (project,)).fetchone()[0]
        states = lambda table, column: {row[0]: row[1] for row in db.execute(
            "SELECT " + column + ",COUNT(*) FROM " + table + " WHERE project=? GROUP BY " + column, (project,))}
        knowledge_count = db.execute("SELECT COUNT(DISTINCT id) FROM knowledge WHERE project=?", (project,)).fetchone()[0]
        active_count = count("active")
        path_row = db.execute("SELECT path FROM project_paths WHERE project=? ORDER BY length(path) ASC,path ASC LIMIT 1", (project,)).fetchone()
        project_path = path_row[0] if path_row and isinstance(path_row[0], str) else ""
        project_name = Path(project_path).name if project_path else ""
        return {"project_id": project, "project_name": project_name, "project_path": project_path, "read_only": True,
                "schema_version": db.execute("PRAGMA user_version").fetchone()[0],
                "knowledge_total": knowledge_count, "knowledge_active": active_count,
                "knowledge_revoked": knowledge_count - active_count,
                "knowledge_revisions": count("knowledge"), "events": count("events"),
                "tool_success_observations": count("events", " AND type='PostToolUse' AND outcome='succeeded'"),
                "problems": states("problems", "status"), "candidates": states("candidates", "status"),
                "review_states": states("review_states", "state"), "administrator_calls": states("admin_calls", "state")}
    offset = _integer(params, "offset", 0, MAX_OFFSET if route != "/api/knowledge" else 1000000)
    limit = _integer(params, "limit", 20 if route != "/api/knowledge" else MAX_FIELD_PAGE,
                     MAX_PAGE if route != "/api/knowledge" else MAX_FIELD_PAGE, 1)
    if route == "/api/knowledge-list":
        query, state = _text(params, "q"), _text(params, "state", "active")
        category, kind = _text(params, "category"), _text(params, "kind")
        if state not in {"active", "revoked", "all"} or category and category not in CATEGORIES or kind and kind not in KNOWLEDGE_KINDS:
            raise ContractError("invalid knowledge filter")
        source = ("FROM knowledge k LEFT JOIN active a ON a.project=k.project AND a.id=k.id "
                  "WHERE k.project=? AND k.revision=(SELECT MAX(x.revision) FROM knowledge x "
                  "WHERE x.project=k.project AND x.id=k.id)")
        args = [project]
        if state != "all":
            source += " AND a.id IS " + ("NOT NULL" if state == "active" else "NULL")
        if category:
            source += " AND json_extract(k.body,'$.category')=?"
            args.append(category)
        if kind:
            source += " AND COALESCE(json_extract(k.body,'$.knowledge_kind'),'solution')=?"
            args.append(kind)
        if query:
            source += " AND (json_extract(k.body,'$.title') LIKE ? ESCAPE '\\' OR json_extract(k.body,'$.content') LIKE ? ESCAPE '\\')"
            literal = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args.extend((literal, literal))
        total = db.execute("SELECT COUNT(*) " + source, args).fetchone()[0]
        rows = db.execute("SELECT k.*,a.revision AS active_revision " + source +
                          " ORDER BY k.created DESC,k.id LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return _page([_preview(row, model._public(project, row)) for row in rows], total, offset, limit)
    if route in {"/api/knowledge", "/api/revisions", "/api/actions"}:
        identity = _text(params, "knowledge_id")
        if not identity or not db.execute("SELECT 1 FROM knowledge WHERE project=? AND id=?", (project, identity)).fetchone():
            raise LookupError
        if route == "/api/knowledge":
            revision = _integer(params, "revision", 0, 1000000)
            row = db.execute("SELECT k.*,a.revision AS active_revision FROM knowledge k LEFT JOIN active a "
                             "ON a.project=k.project AND a.id=k.id WHERE k.project=? AND k.id=? "
                             "AND (?=0 OR k.revision=?) ORDER BY k.revision DESC LIMIT 1",
                             (project, identity, revision, revision)).fetchone()
            if not row:
                raise LookupError
            section = _text(params, "section", "content")
            if section not in {"content", "applicability", "limitations"}:
                raise ContractError("invalid knowledge section")
            public = model._public(project, row)
            text = public[section]
            sources = [{**{key: value for key, value in source.items() if key != "evidence"},
                        "evidence": source["evidence"][:20], "evidence_count": len(source["evidence"])}
                       for source in public["sources"][:20]]
            return {**_preview(row, public), "metadata": public["metadata"], "sources": sources,
                    "source_count": len(public["sources"]), "evidence": public["evidence"][:40],
                    "evidence_count": len(public["evidence"]), "sources_truncated": len(public["sources"]) > 20,
                    "field": {"name": section, "text": text[offset:offset + limit], "total_chars": len(text),
                              "offset": offset, "next_offset": offset + limit if offset + limit < len(text) else None}}
        if route == "/api/revisions":
            total = db.execute("SELECT COUNT(*) FROM knowledge WHERE project=? AND id=?", (project, identity)).fetchone()[0]
            rows = db.execute("SELECT k.*,a.revision AS active_revision FROM knowledge k LEFT JOIN active a "
                              "ON a.project=k.project AND a.id=k.id WHERE k.project=? AND k.id=? "
                              "ORDER BY k.revision DESC LIMIT ? OFFSET ?", (project, identity, limit, offset)).fetchall()
            return _page([_preview(row) for row in rows], total, offset, limit)
        return model.knowledge_actions(project, identity, limit=limit, offset=offset)
    if route == "/api/events":
        identity = _text(params, "knowledge_id")
        source, args = "FROM events e WHERE e.project=?", [project]
        if identity:
            if not db.execute("SELECT 1 FROM knowledge WHERE project=? AND id=?", (project, identity)).fetchone():
                raise LookupError
            source += (" AND (EXISTS(SELECT 1 FROM knowledge_sources s,json_each(s.evidence_refs) ref "
                       "WHERE s.project=e.project AND s.knowledge_id=? AND ref.value=e.id) "
                       "OR EXISTS(SELECT 1 FROM knowledge k,json_each(k.body,'$.evidence_refs') ref "
                       "WHERE k.project=e.project AND k.id=? AND ref.value=e.id))")
            args.extend((identity, identity))
        total = db.execute("SELECT COUNT(*) " + source, args).fetchone()[0]
        rows = db.execute("SELECT e.id,e.type,e.outcome,e.created,e.task_id,e.task_revision,e.payload " + source +
                          " ORDER BY e.created DESC,e.id LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        items = []
        for row in rows:
            payload = clean(json.loads(row["payload"]))
            summary = payload.get("summary", payload.get("prompt", payload.get("last_assistant_message",
                                      payload.get("text", payload.get("tool_response", "")))))
            items.append({**{key: row[key] for key in row.keys() if key != "payload"},
                          "tool_name": str(payload.get("tool_name", ""))[:200],
                          "summary": (summary if isinstance(summary, str) else json.dumps(summary, ensure_ascii=False))[:1200]})
        return _page(items, total, offset, limit)
    if route == "/api/admin":
        total = db.execute("SELECT COUNT(*) FROM problems WHERE project=?", (project,)).fetchone()[0]
        rows = db.execute("SELECT p.id,p.revision,p.summary,p.category,p.status,p.created,rs.state AS review_state,"
                          "rs.reason AS review_reason,rs.revision AS review_revision FROM problems p LEFT JOIN review_states rs "
                          "ON rs.project=p.project AND rs.problem=p.id WHERE p.project=? "
                          "ORDER BY (p.status='pending') DESC,p.created DESC,p.id LIMIT ? OFFSET ?", (project, limit, offset)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["summary"] = clean(item["summary"])[:800]
            item["review_reason"] = clean(item["review_reason"] or "")[:800]
            item["review_current"] = item["review_revision"] == item["revision"]
            item["candidates"] = [{**dict(candidate), "title": clean(json.loads(candidate["body"]).get("title", ""))[:250]}
                                  for candidate in db.execute("SELECT id,status,body FROM candidates WHERE project=? AND problem=? "
                                                              "ORDER BY created DESC,id LIMIT 5", (project, row["id"]))]
            for candidate in item["candidates"]:
                candidate.pop("body")
            call = db.execute("SELECT id,state,created,finished,retry_allowed FROM admin_calls WHERE project=? AND problem=? "
                              "ORDER BY created DESC,id DESC LIMIT 1", (project, row["id"])).fetchone()
            item["last_call"] = dict(call) if call else None
            items.append(item)
        return _page(items, total, offset, limit)
