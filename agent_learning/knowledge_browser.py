"""Project-scoped read-only knowledge browser using only the standard library.

The browser does not initialize Runtime, migrate storage, mark exposures or run
an administrator. Its access key belongs to this process and is never persisted.
"""
from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, quote, urlsplit

from .case_store import CaseStoreMixin
from .knowledge_releases import KnowledgeReleaseMixin
from .knowledge_store import KnowledgeStoreMixin
from .publication_state import PublicationStateMixin
from .runtime import CATEGORIES, KNOWLEDGE_KINDS, ContractError, clean


MAX_PAGE = 50
MAX_OFFSET = 10000
MAX_FIELD_PAGE = 8000
MAX_RESPONSE_BYTES = 256000
MAX_DISCARD_BYTES = 8192


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
    if db.execute("PRAGMA user_version").fetchone()[0] not in {11, 12, 13, 14, 15}:
        raise ContractError("browser requires database schema 11..15; run an explicit upgrade first")


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

    def __init__(self, database, project, *, host="127.0.0.1", port=0, token=None):
        if host != "127.0.0.1":
            raise ContractError("browser host must be 127.0.0.1")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ContractError("invalid browser port")
        if not isinstance(project, str) or not project.strip() or len(project) > 500:
            raise ContractError("browser requires an explicit project ID")
        if token is not None and (not isinstance(token, str) or len(token) < 32 or len(token) > 200
                                  or not token.isascii() or not token.isalnum()):
            raise ContractError("browser token must be 32..200 ASCII letters or digits")
        self.database = Path(database).expanduser().resolve(strict=True)
        with contextlib.closing(_connection(self.database)) as db:
            _schema(db)
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone():
                raise ContractError("unknown browser project")
        self.project = project
        self.access_token = token or secrets.token_hex(24)
        super().__init__((host, port), _Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}/"
        self.origin = self.url.rstrip("/")
        self.browser_url = self.url + "#token=" + quote(self.access_token, safe="")

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


def make_browser_server(database, project, *, host="127.0.0.1", port=0, token=None):
    return BrowserServer(database, project, host=host, port=port, token=token)


class _Handler(BaseHTTPRequestHandler):
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
            with self.server.snapshot() as db:
                result = _read_api(db, self.server.project, parsed.path, params)
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

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = _write_denied

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
    }
    if route not in contracts:
        raise LookupError
    if set(params) - contracts[route]:
        raise ContractError("unsupported query parameter")
    model = _ReadModel(db)
    if route == "/api/stats":
        count = lambda table, suffix="": db.execute(
            "SELECT COUNT(*) FROM " + table + " WHERE project=?" + suffix, (project,)).fetchone()[0]
        states = lambda table, column: {row[0]: row[1] for row in db.execute(
            "SELECT " + column + ",COUNT(*) FROM " + table + " WHERE project=? GROUP BY " + column, (project,))}
        knowledge_count = db.execute("SELECT COUNT(DISTINCT id) FROM knowledge WHERE project=?", (project,)).fetchone()[0]
        active_count = count("active")
        return {"project_id": project, "read_only": True,
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
            summary = payload.get("summary", payload.get("prompt", payload.get("tool_response", "")))
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


HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>agents-learning · 项目知识浏览</title><link rel="stylesheet" href="/style.css"><script defer src="/app.js"></script></head>
<body><header><div><span class="eyebrow">AGENTS-LEARNING</span><h1>项目经验，随时核查</h1><p>查找知识、追溯来源、查看历史与审核状态。</p></div><span class="badge">只读浏览</span></header>
<main><section id="login" class="panel"><h2>连接本地知识库</h2><p>输入启动命令显示的访问码。访问码仅保留在当前页面内存中。</p>
<label for="token">访问码</label><input id="token" type="password" autocomplete="off"><button id="connect">连接</button></section>
<div id="loading" role="status" hidden>正在读取本地数据…</div><div id="error" role="alert" hidden></div><section id="application" hidden>
<div class="project-line"><strong id="project"></strong><button id="refresh">刷新当前视图</button><button id="disconnect">断开</button></div>
<div id="stats" class="stats"></div><p class="note">工具成功是可观察结果；展示、采用声明和用户验收分别记录。这里不会发布、撤销知识或执行任务。</p>
<nav aria-label="知识库视图"><button data-view="knowledge" class="selected">知识</button><button data-view="events">事件时间线</button><button data-view="admin">管理员待办</button></nav>
<section class="panel"><div id="filters"><label for="query">搜索标题与正文</label><input id="query" placeholder="输入错误码、工具名称或关键词" maxlength="200">
<select id="state" aria-label="发布状态"><option value="active">当前有效</option><option value="all">全部状态</option><option value="revoked">已撤销</option></select>
<select id="category" aria-label="分类"><option value="">全部分类</option><option value="project">项目</option><option value="operation">操作</option><option value="professional">专业</option><option value="mixed">混合</option><option value="unknown">未分类</option></select><button id="search">查询</button></div>
<div class="page-line"><span id="page-label"></span><div><button id="previous">上一页</button><button id="next">下一页</button></div></div><div id="list"></div></section>
<section id="detail" class="panel" hidden><div class="page-line"><h2 id="detail-title"></h2><button id="close-detail">关闭详情</button></div>
<div id="detail-meta"></div><div id="detail-tabs"><button data-section="content">正文</button><button data-section="applicability">适用条件</button><button data-section="limitations">限制</button></div>
<pre id="detail-text"></pre><div class="page-line"><span id="field-label"></span><div><button id="field-previous">上一段</button><button id="field-next">下一段</button></div></div>
<h3>来源与健康状态</h3><div id="sources"></div><h3>正文修订</h3><div id="revisions"></div><button id="more-revisions" hidden>查看更多修订</button><h3>发布与治理动作</h3><div id="actions"></div><button id="more-actions" hidden>查看更多动作</button><h3>来源事件时间线</h3><div id="source-events"></div><button id="more-events" hidden>查看更多来源事件</button></section>
</section></main><footer>127.0.0.1 · 本地 SQLite 一致快照 · 无模型调用</footer></body></html>"""

CSS = """*{box-sizing:border-box}body{margin:0;background:#f5f7fa;color:#192735;font:15px/1.65 system-ui,sans-serif}header,main,footer{max-width:1120px;margin:auto;padding:28px}header{display:flex;justify-content:space-between;align-items:center;gap:20px}h1{font-size:30px;line-height:1.25;margin:8px 0}h2{font-size:20px;margin:0 0 14px}h3{margin-top:24px}.eyebrow{font-size:12px;letter-spacing:2px;color:#547264}.badge{padding:6px 12px;background:#dfede3;color:#235537;border-radius:20px;white-space:nowrap}.panel{background:#fff;border:1px solid #dce3e9;border-radius:12px;padding:22px;margin:18px 0}button,input,select{font:inherit;border:1px solid #c5ced7;border-radius:6px;padding:7px 10px;background:#fff;color:inherit}button{cursor:pointer}button:hover,button.selected{background:#e8f0eb;border-color:#7d9f8b}button:disabled{opacity:.45;cursor:default}input{max-width:100%}#query{flex:1;min-width:160px}#filters,.page-line,.project-line,nav,#detail-tabs{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.page-line,.project-line{justify-content:space-between}.page-line{margin:15px 0}.project-line strong{overflow-wrap:anywhere}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:18px}.stat{border:1px solid #dce3e9;border-radius:10px;padding:16px;background:#fff}.stat strong{display:block;font-size:27px}.note,.muted,footer{color:#667482;font-size:13px}.card{padding:17px 0;border-top:1px solid #e1e7ed;overflow-wrap:anywhere}.card:first-child{border-top:0}.card p{margin:7px 0}.card-title{font-size:17px;text-align:left}.tags{color:#476257;font-size:13px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.8 ui-monospace,monospace;background:#f6f8fa;border:1px solid #e1e7ed;border-radius:8px;padding:15px}#error{background:#fff0ec;border:1px solid #e7b7aa;color:#973e25;padding:14px;border-radius:8px}[hidden]{display:none!important}#token{width:400px}.history-item{padding:10px 0;border-bottom:1px solid #e1e7ed}label{color:#50616f;font-size:13px}@media(max-width:650px){header,main,footer{padding:18px}.stats{grid-template-columns:repeat(2,1fr)}header{align-items:flex-start}h1{font-size:24px}.panel{padding:16px}}"""

JAVASCRIPT = r"""'use strict';
const $=id=>document.getElementById(id);
const state={token:'',connectionEpoch:0,view:'knowledge',offset:0,next:null,detail:null,section:'content',fieldOffset:0,fieldNext:null,pages:{},busy:false};
const labels={active:'有效',revoked:'已撤销',historical:'历史修订',valid:'有效来源',missing:'来源缺失',conflicted:'来源冲突',denied:'操作被拒绝',candidate:'待晋升',stable:'稳定',reported:'陈述证据',tool_success:'工具成功证据',operation:'操作',professional:'专业',project:'项目',mixed:'混合',unknown:'未分类',pending:'待处理',candidate_ready:'候选就绪',work_summary_ready:'工作摘要就绪',awaiting_evidence:'等待证据',proposal_ready:'治理建议就绪',skipped:'已跳过',solution:'解法',fact:'事实',pattern:'方法'};
const label=x=>labels[x]||x||'未知';
const node=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;if(tag==='button')e.disabled=state.busy;return e;};
const stamp=x=>x?new Date(x*1000).toLocaleString():'';
const error=x=>{$('error').textContent=x;$('error').hidden=!x;};
async function api(path,params={}){const epoch=state.connectionEpoch;const response=await fetch('/api/'+path+'?'+new URLSearchParams(params),{headers:{Authorization:'Bearer '+state.token},cache:'no-store',credentials:'omit'});const body=await response.json();if(epoch!==state.connectionEpoch)throw new Error('连接已断开，旧请求结果已丢弃。');if(!response.ok)throw new Error(body.error||'读取失败');return body;}
function busy(value){state.busy=value;$('loading').hidden=!value;document.body.setAttribute('aria-busy',String(value));for(const button of document.querySelectorAll('button'))if(button.id!=='disconnect')button.disabled=value;if(!value){$('previous').disabled=state.offset===0;$('next').disabled=state.next===null;$('field-previous').disabled=!state.detail||state.fieldOffset===0;$('field-next').disabled=!state.detail||state.fieldNext===null;}}
async function run(action){if(state.busy)return;busy(true);error('');try{await action();}catch(e){error(e.message);}finally{busy(false);}}
function pager(data){state.next=data.next_offset;$('page-label').textContent='共 '+data.total+' 条 · 当前 '+(data.items.length?data.offset+1:0)+'–'+(data.offset+data.items.length);$('previous').disabled=state.busy||state.offset===0;$('next').disabled=state.busy||state.next===null;}
function eventCard(item){const card=node('div',undefined,'card');card.append(node('strong',item.type+' · '+item.outcome),node('div',stamp(item.created)+' '+(item.tool_name||''),'muted'),node('p',item.summary||''),node('div',item.id,'muted'));return card;}
async function stats(){const data=await api('stats');$('project').textContent='项目：'+data.project_id;$('stats').replaceChildren();for(const [title,value]of [['有效知识',data.knowledge_active],['已撤销知识',data.knowledge_revoked],['观察事件',data.events],['待处理问题',data.problems.pending||0]]){const card=node('div',undefined,'stat');card.append(node('span',title),node('strong',String(value)));$('stats').append(card);}}
async function list(){const target=$('list');target.replaceChildren();$('filters').hidden=state.view!=='knowledge';let data;
if(state.view==='knowledge'){data=await api('knowledge-list',{q:$('query').value,state:$('state').value,category:$('category').value,offset:state.offset,limit:20});for(const item of data.items){const card=node('article',undefined,'card');const title=node('button',item.title,'card-title');title.addEventListener('click',()=>run(()=>showDetail(item.knowledge_id,item.revision)));card.append(title,node('div',[label(item.state),'r'+item.revision,label(item.category),label(item.validation_level),label(item.source_health),label(item.maturity)].join(' · '),'tags'),node('p',item.preview),node('div',stamp(item.created)+' · '+item.knowledge_id,'muted'));if(item.problem_changed)card.append(node('p','来源案例已经变化，需要复核。','tags'));target.append(card);}}
else if(state.view==='events'){data=await api('events',{offset:state.offset,limit:20});for(const item of data.items)target.append(eventCard(item));}
else{data=await api('admin',{offset:state.offset,limit:20});for(const item of data.items){const card=node('article',undefined,'card');card.append(node('strong',item.summary),node('div',label(item.status)+' · 案例 r'+item.revision+' · '+label(item.review_state)+(item.review_state&&!item.review_current?'（旧案例修订）':''),'tags'),node('p',item.review_reason||'尚无审核决定'),node('div',stamp(item.created)+' · '+item.id,'muted'));if(item.last_call)card.append(node('div','最近调用：'+item.last_call.state+' · '+stamp(item.last_call.created),'muted'));for(const c of item.candidates)card.append(node('p','候选：'+c.title+' · '+label(c.status)));target.append(card);}}
if(!data.items.length)target.append(node('p','当前视图没有记录。','muted'));pager(data);}
async function field(){const data=await api('knowledge',{knowledge_id:state.detail.id,revision:state.detail.revision,section:state.section,offset:state.fieldOffset,limit:8000});$('detail-title').textContent=data.title;$('detail-meta').textContent=[label(data.state),'r'+data.revision,label(data.validation_level),label(data.source_health),label(data.maturity)].join(' · ');$('detail-text').textContent=data.field.text||'（空）';state.fieldNext=data.field.next_offset;$('field-label').textContent=label(state.section)+' · '+data.field.total_chars+' 字符 · 从 '+data.field.offset+' 开始';$('field-previous').disabled=state.busy||state.fieldOffset===0;$('field-next').disabled=state.busy||state.fieldNext===null;
$('sources').replaceChildren();if(!data.sources.length)$('sources').append(node('p','未恢复关联来源；下方保留可查证据摘要。','muted'));for(const source of data.sources){const e=node('div',undefined,'history-item');e.append(node('strong',source.problem_id+' · 案例 r'+source.problem_revision),node('p',label(source.health)+(source.problem_changed?' · 案例已变化':'')+' · 工具成功观察：'+(source.current_tool_success?'有':'无')));for(const ref of source.evidence)e.append(node('div',ref.event_id+' · '+label(ref.health)+' · '+(ref.outcome||''),'muted'));if(source.evidence_count>source.evidence.length)e.append(node('p','更多证据请通过来源事件时间线查看。','muted'));$('sources').append(e);}if(!data.sources.length)for(const ref of data.evidence)$('sources').append(node('p',ref.event_id+' · '+label(ref.health)));if(data.sources_truncated)$('sources').append(node('p','来源较多，当前显示前 20 个；总计 '+data.source_count+' 个。','muted'));}
async function detailPage(kind,append=false){const key=kind==='events'?'source-events':kind;const data=await api(kind,{knowledge_id:state.detail.id,offset:append?(state.pages[kind]||0):0,limit:20});if(!append)$(key).replaceChildren();for(const item of data.items){if(kind==='events'){$(key).append(eventCard(item));continue;}const e=node('div',undefined,'history-item');if(kind==='revisions'){const b=node('button','r'+item.revision+' · '+label(item.state)+' · '+item.title);b.addEventListener('click',()=>run(async()=>{state.detail.revision=item.revision;state.fieldOffset=0;await field();}));e.append(b,node('span',' '+stamp(item.created),'muted'));}else e.append(node('strong',item.action+' · r'+item.revision),node('p',item.reason),node('span',stamp(item.created)+' · '+item.actor,'muted'));$(key).append(e);}if(!data.items.length&&!append)$(key).append(node('p','没有记录。','muted'));state.pages[kind]=data.next_offset;$('more-'+kind).hidden=data.next_offset===null;}
async function showDetail(id,revision){state.detail={id,revision};state.fieldOffset=0;state.section='content';state.pages={};$('detail').hidden=false;await field();for(const kind of ['revisions','actions','events'])await detailPage(kind);$('detail').scrollIntoView({behavior:'smooth',block:'start'});}
async function connect(){state.token=$('token').value.trim()||state.token;if(!state.token)throw new Error('请输入访问码。');await stats();$('token').value='';$('login').hidden=true;$('application').hidden=false;await list();}
$('connect').addEventListener('click',()=>run(connect));$('token').addEventListener('keydown',e=>{if(e.key==='Enter')run(connect);});$('disconnect').addEventListener('click',()=>{state.connectionEpoch++;state.token='';state.detail=null;state.pages={};state.fieldOffset=0;state.fieldNext=null;$('application').hidden=true;$('login').hidden=false;$('detail').hidden=true;for(const id of ['list','stats','sources','revisions','actions','source-events'])$(id).replaceChildren();for(const id of ['project','detail-title','detail-meta','detail-text','field-label','page-label'])$(id).textContent='';for(const id of ['more-revisions','more-actions','more-events'])$(id).hidden=true;error('');});
$('refresh').addEventListener('click',()=>run(async()=>{await stats();await list();if(state.detail){await field();for(const kind of ['revisions','actions','events'])await detailPage(kind);}}));$('search').addEventListener('click',()=>run(async()=>{state.offset=0;await list();}));$('query').addEventListener('keydown',e=>{if(e.key==='Enter')$('search').click();});$('previous').addEventListener('click',()=>run(async()=>{state.offset=Math.max(0,state.offset-20);await list();}));$('next').addEventListener('click',()=>run(async()=>{if(state.next!==null){state.offset=state.next;await list();}}));
for(const b of document.querySelectorAll('nav button'))b.addEventListener('click',()=>run(async()=>{state.view=b.dataset.view;state.offset=0;for(const x of document.querySelectorAll('nav button'))x.classList.toggle('selected',x===b);await list();}));
for(const b of document.querySelectorAll('[data-section]'))b.addEventListener('click',()=>run(async()=>{state.section=b.dataset.section;state.fieldOffset=0;await field();}));$('field-previous').addEventListener('click',()=>run(async()=>{state.fieldOffset=Math.max(0,state.fieldOffset-8000);await field();}));$('field-next').addEventListener('click',()=>run(async()=>{if(state.fieldNext!==null){state.fieldOffset=state.fieldNext;await field();}}));$('close-detail').addEventListener('click',()=>{$('detail').hidden=true;state.detail=null;});for(const kind of ['revisions','actions','events'])$('more-'+kind).addEventListener('click',()=>run(()=>detailPage(kind,true)));
const fragment=new URLSearchParams(location.hash.slice(1));const bootstrap=fragment.get('token');if(location.hash)history.replaceState(null,'',location.pathname);if(bootstrap){state.token=bootstrap;run(connect);}
"""
