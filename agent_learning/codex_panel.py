"""Explicit project control window for Codex local-environment Actions.

Only the installed project may launch this process. It owns a temporary loopback
server, never creates a conversation and starts no administrator on page open.
"""
from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from urllib.parse import urlsplit
import webbrowser

from .install import _hook_commands, _resource_root
from .install_update import inspect_installation, safe_path
from .panel_browser import make_panel_guard
from .panel_control import ACTIONS, EXPECTED_FIELDS, PanelControl
from .runtime import ContractError, Runtime


MAX_BODY = 32768
_BOOTSTRAP = frozenset({"ui_session_id"})
_ACTION = _BOOTSTRAP | {"capability", "action", "request_id", "expected", "preset", "confirm"}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate panel field")
        result[key] = value
    return result


def _parameters(method, body):
    if (not isinstance(body, dict) or set(body) - (_BOOTSTRAP if method == "bootstrap" else _ACTION)
            or not PanelControl._ui_session(body.get("ui_session_id"))):
        raise ContractError("invalid project UI request")
    if method == "action":
        if (not isinstance(body.get("capability"), str) or not 1 <= len(body["capability"]) <= 256
                or body.get("action") not in ACTIONS):
            raise ContractError("invalid project UI action")
        if "request_id" in body and (not isinstance(body["request_id"], str)
                or not 1 <= len(body["request_id"]) <= 200 or not body["request_id"].strip()):
            raise ContractError("invalid request identity")
        if "confirm" in body and type(body["confirm"]) is not bool:
            raise ContractError("invalid confirmation")
        expected = body.get("expected")
        if expected is not None and (not isinstance(expected, dict) or set(expected) != EXPECTED_FIELDS
                or any(type(value) is not int or value < 0 for value in expected.values())):
            raise ContractError("invalid settings versions")
        if "preset" in body and (not isinstance(body["preset"], str)
                or body["preset"] not in {"off", "minimal", "light", "balanced", "active"}):
            raise ContractError("invalid administrator preset")
    return body


class CodexPanelServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 8

    def __init__(self, runtime, controller, access_guard, html, port):
        self.runtime, self.controller, self.access_guard = runtime, controller, access_guard
        self.lock = threading.RLock()
        self.closed = False
        self.entry_code = secrets.token_hex(32)
        self.cookie_name = "alp_panel_" + secrets.token_hex(8)
        self.cookie_value = secrets.token_hex(32)
        self.html = html.encode("utf-8")
        scripts = re.findall(r"<script\b[^>]*>([\s\S]*?)</script\s*>", html, re.IGNORECASE)
        self.script_hashes = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii") + "'"
                                     for script in scripts)
        super().__init__(("127.0.0.1", port), _Handler)
        self.origin = "http://127.0.0.1:" + str(self.server_address[1])
        self.url = self.origin + "/"
        # Never print or put this code in a tool result, document or log.
        self.browser_url = self.origin + "/access/" + self.entry_code

    def server_close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.entry_code = None
                self.cookie_value = ""
                self.controller.close()
                self.runtime.close()
        super().server_close()

    def dispatch(self, method, body):
        with self.lock:
            if self.closed or not self.access_guard():
                raise ContractError("project UI authorization expired")
            params = _parameters(method, body)
            workspace = self.controller.workspace
            if method == "bootstrap":
                return self.controller.bootstrap(workspace=workspace, **params)
            return self.controller.action(workspace=workspace, **params)


class _Handler(BaseHTTPRequestHandler):
    server_version = "AgentLearningPanel"
    sys_version = ""

    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, format, *args):
        # Paths may carry the one-time entry code; no request logging is allowed.
        return

    def _send(self, status, data, mime="application/json; charset=utf-8", headers=None):
        payload = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src " + (self.server.script_hashes or "'none'")
                         + "; style-src 'unsafe-inline'; connect-src 'self'; img-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        self.wfile.write(payload)

    def _boundary(self):
        if self.headers.get_all("Host") != [self.server.origin.removeprefix("http://")]:
            self._send(403, {"visible": False, "reason": "invalid_host"})
            return False
        origins = self.headers.get_all("Origin") or []
        if (origins and origins != [self.server.origin] or self.headers.get("Sec-Fetch-Site") == "cross-site"):
            self._send(403, {"visible": False, "reason": "invalid_origin"})
            return False
        try:
            valid = not self.server.closed and self.server.access_guard() is True
        except Exception:
            valid = False
        if not valid:
            self._send(403, {"visible": False, "reason": "panel_access_expired"})
            return False
        return True

    def _authenticated(self):
        cookies = self.headers.get_all("Cookie") or []
        matches = []
        if len(cookies) == 1:
            for part in cookies[0].split(";"):
                name, separator, value = part.strip().partition("=")
                if name == self.server.cookie_name and separator:
                    matches.append(value)
        return (len(matches) == 1 and matches[0].isascii()
                and secrets.compare_digest(matches[0], self.server.cookie_value))

    def do_GET(self):
        if not self._boundary():
            return
        if len(self.path) > 1000:
            self._send(414, {"visible": False, "reason": "invalid_path"})
            return
        path = urlsplit(self.path)
        if path.query or path.fragment or path.scheme or path.netloc:
            self._send(400, {"visible": False, "reason": "invalid_path"})
            return
        if path.path.startswith("/access/"):
            supplied = path.path.removeprefix("/access/")
            with self.server.lock:
                code = self.server.entry_code
                if not code or not supplied.isascii() or not secrets.compare_digest(supplied, code):
                    self._send(403, {"visible": False, "reason": "access_code_invalid"})
                    return
                self.server.entry_code = None
                self._send(303, {}, headers={"Location": "/", "Set-Cookie": self.server.cookie_name + "="
                    + self.server.cookie_value + "; Path=/; HttpOnly; SameSite=Strict"})
            return
        if not self._authenticated():
            self._send(401, {"visible": False, "reason": "access_cookie_required"})
            return
        if path.path != "/":
            self._send(404, {"visible": False, "reason": "not_found"})
            return
        self._send(200, self.server.html, "text/html; charset=utf-8")

    def do_POST(self):
        if not self._boundary():
            return
        if not self._authenticated() or self.headers.get_all("Origin") != [self.server.origin]:
            self._send(403, {"visible": False, "reason": "request_not_authorized"})
            return
        methods = {"/api/bootstrap": "bootstrap", "/api/action": "action"}
        method = methods.get(self.path)
        lengths = self.headers.get_all("Content-Length") or []
        types = self.headers.get_all("Content-Type") or []
        if (not method or self.headers.get_all("Transfer-Encoding") or len(lengths) != 1
                or not lengths[0].isascii() or not lengths[0].isdecimal() or not 0 < int(lengths[0]) <= MAX_BODY
                or len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json"):
            self._send(400, {"visible": False, "reason": "invalid_request"})
            return
        try:
            raw = self.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                raise ContractError("incomplete request")
            body = json.loads(raw, object_pairs_hook=_unique)
            result = self.server.dispatch(method, body)
        except (ContractError, ValueError, UnicodeError, TypeError, RecursionError):
            self._send(400, {"visible": False, "reason": "panel_request_rejected"})
            return
        except Exception:
            self._send(500, {"visible": False, "reason": "panel_unavailable"})
            return
        self._send(200, result)


def make_panel_server(installation, workspace=None, home=None, host_instance="default", port=0):
    """Validate the launch before opening a DB; caller owns this temporary server."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ContractError("invalid panel port")
    installed = inspect_installation(installation, _hook_commands, target_host="codex")
    receipt = installed["receipt"]
    running_root = Path(__file__).resolve().parents[1]
    if (running_root / "release.json").is_file() and running_root != safe_path(receipt["runtime_directory"]):
        raise ContractError("the installed runtime changed; refresh the Codex project Action before opening its panel")
    bound = receipt.get("panel_workspace")
    if not bound:
        raise ContractError("this Codex installation has no project UI binding")
    bound = safe_path(bound)
    actual = safe_path(Path.cwd())
    if actual != bound or workspace is not None and safe_path(workspace) != bound:
        raise ContractError("launch the Codex panel from its exact installed project workspace")
    default_home = (Path(os.environ["LOCALAPPDATA"]) / "AgentLearning" if os.environ.get("LOCALAPPDATA")
                    else Path.home() / ".local/share/agent-learning")
    selected_home = safe_path(home or receipt.get("home") or os.environ.get("ALP_HOME") or default_home)
    if receipt.get("home") and selected_home != safe_path(receipt["home"]):
        raise ContractError("panel knowledge directory does not match its installation")
    source = _resource_root()
    protected = [safe_path(installed["target"]), safe_path(receipt["runtime_directory"]), running_root]
    if isinstance(source, Path):
        protected.append(source.resolve())
    if any(selected_home == root or root in selected_home.parents for root in protected):
        raise ContractError("panel knowledge directory must stay outside source, export and runtime directories")
    safe_path(selected_home / "runtime.sqlite3")
    from .panel_ui import local_panel_html
    html = local_panel_html()
    runtime = Runtime(selected_home, host="codex", host_instance=host_instance)
    controller = None
    try:
        # This adapter serializes all controller operations under one lock. A
        # dedicated shared connection permits HTTP handler/cleanup threads; no
        # Runtime connection is ever used concurrently by two requests.
        runtime.db.close()
        runtime.db = sqlite3.connect(runtime.home / "runtime.sqlite3", timeout=1.5,
                                     isolation_level=None, check_same_thread=False)
        runtime.db.row_factory = sqlite3.Row
        runtime.db.execute("PRAGMA foreign_keys=ON")
        runtime.db.execute("PRAGMA synchronous=FULL")
        project = runtime.project(bound)
        controller = PanelControl(runtime, project, bound, installed["target"], project_ui=True)
        if not controller.eligible():
            raise ContractError("project UI installation could not be verified")
        guard = make_panel_guard(installed["target"], dict(controller.binding), runtime.home / "runtime.sqlite3",
                                 runtime._session_scope(), controller.epoch, controller.governance_epoch)
        return CodexPanelServer(runtime, controller, guard, html, port)
    except BaseException:
        if controller is not None:
            controller.close()
        runtime.close()
        raise


def open_panel(installation, workspace=None, port=0, open_browser=True, home=None, host_instance="default"):
    """Run until its foreground Action/terminal stops, with no persistent service."""
    if type(open_browser) is not bool:
        raise ContractError("open_browser must be boolean")
    server = make_panel_server(installation, workspace=workspace, home=home, host_instance=host_instance, port=port)
    try:
        if open_browser and not webbrowser.open(server.browser_url):
            raise ContractError("the browser could not be opened; relaunch the project panel")
        print("Agent Learning project controls are running; close this terminal to stop.", flush=True)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        return {"status": "stopped", "project_id": server.controller.project}
    finally:
        server.server_close()
