"""Project-scoped controls shared by MCP Apps and native sidecar panels.

Installation receipts establish project scope; a database entry alone is never
an installation. UI capabilities stay in process memory and expire on logout,
uninstall or replacement of the installation.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from urllib.parse import unquote, urlsplit

from .runtime import ContractError, digest, encode
from .admin_presets import PRESETS


PANEL_SECONDS = 300
MAX_PANELS = 64
MAX_REQUESTS = 128
MAX_PANEL_MESSAGE = 32768
EXPECTED_FIELDS = {"expected_policy_version", "expected_project_budget_version", "expected_settings_version",
                   "expected_session_epoch", "expected_governance_epoch"}
ACTIONS = {"status", "debug", "open_browser", "open_review", "set_admin_preset", "archive_history"}


def _path(value):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ContractError("panel workspace is required")
    text = str(value)
    if text.startswith("file:"):
        parsed = urlsplit(text)
        if parsed.scheme != "file" or parsed.query or parsed.fragment or parsed.netloc not in {"", "localhost"}:
            raise ContractError("unsupported panel workspace URI")
        text = unquote(parsed.path)
        if os.name == "nt" and len(text) > 2 and text[0] == "/" and text[2] == ":":
            text = text[1:]
    result = Path(text).expanduser()
    if not result.is_absolute():
        raise ContractError("panel workspace must be absolute")
    return result.resolve(strict=True)


def same_workspace(actual, bound):
    """Allow project subdirectories, but never another nested checkout/symlink."""
    try:
        actual, bound = _path(actual), _path(bound)
        if not actual.is_dir() or not bound.is_dir():
            return False
        actual.relative_to(bound)
        cursor = actual
        while cursor != bound:
            if (cursor / ".git").exists():
                return False
            cursor = cursor.parent
        return True
    except (ContractError, OSError, ValueError):
        return False


def _project_for_workspace(runtime, workspace):
    """Read-only equivalent of Runtime.project, including configured aliases."""
    resolved = _path(workspace)
    path = os.path.normcase(str(resolved))
    aliases = {}
    for key, value in runtime.config["project_aliases"].items():
        normalized = os.path.normcase(str(Path(key).resolve()))
        if normalized in aliases and aliases[normalized] != value:
            raise ContractError("project alias collision after normalization")
        aliases[normalized] = value
    alias = next((v for k, v in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True)
                  if path == k or path.startswith(k.rstrip(os.sep) + os.sep)), None)
    known = runtime.db.execute("SELECT path,project FROM project_paths ORDER BY length(path) DESC").fetchall()
    registered = next((row for row in known if path == row["path"] or path.startswith(row["path"] + os.sep)), None)
    git_root = None
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists():
            git_root = parent
            break
        if parent.name.lower() == "local" and parent.parent.name.lower() == "build":
            break
    root_path = os.path.normcase(str(git_root or resolved))
    exact = next((row for row in known if row["path"] == root_path), None)
    return alias or ((exact["project"] if exact else digest(root_path)[:24]) if git_root
                     else registered["project"] if registered else digest(root_path)[:24])


class PanelControl:
    def __init__(self, runtime, project, workspace, installation, require_context=True, *, project_ui=False):
        if type(project_ui) is not bool or project_ui and runtime.host != "codex":
            raise ContractError("project UI authorization is available only for Codex")
        self.runtime, self.project = runtime, project
        self.workspace, self.installation = workspace, installation
        self.project_ui = project_ui
        self.require_context = require_context and not project_ui
        self.epoch, self.governance_epoch = runtime.session_epoch(), runtime.host_epoch()
        self.leases, self.requests = OrderedDict(), OrderedDict()
        self.browsers = {}
        self.reason = "project_panel_not_installed"
        self.binding = None
        self.installation_identity = None

    def _installation(self):
        if self.installation is None:
            raise ContractError("project_panel_not_installed")
        from .install import _hook_commands
        from .install_update import inspect_installation
        installed = inspect_installation(self.installation, _hook_commands, target_host=self.runtime.host)
        receipt = installed["receipt"]
        if self.runtime.host == "codex":
            candidates = [receipt["panel_workspace"]] if receipt.get("panel_workspace") else []
        elif self.runtime.host == "dsh" and isinstance(receipt.get("workspaces"), list):
            candidates = list(receipt["workspaces"])
        else:
            candidates = [receipt["workspace"]] if receipt.get("workspace") else []
        bound = next((item for item in sorted(candidates, key=len, reverse=True)
                      if same_workspace(self.workspace, item)), None)
        if not bound:
            raise ContractError("project_panel_not_installed")
        if receipt.get("home") and _path(receipt["home"]) != self.runtime.home.resolve():
            raise ContractError("panel_knowledge_directory_mismatch")
        if _project_for_workspace(self.runtime, bound) != self.project:
            raise ContractError("panel_project_mismatch")
        return {"workspace": str(_path(bound)), "release_id": receipt["release_id"],
                "installation": str(Path(self.installation).resolve()),
                "receipt_digest": hashlib.sha256(encode(receipt).encode("utf-8")).hexdigest()}

    def eligible(self):
        try:
            if self.runtime.session_epoch() != self.epoch or self.runtime.host_epoch() != self.governance_epoch:
                raise ContractError("panel_context_expired")
            binding = self._installation()
            if self.installation_identity is not None and binding != self.installation_identity:
                raise ContractError("panel_installation_changed_restart_required")
            self.installation_identity = dict(binding)
            self.binding = binding
            self.reason = ""
            return True
        except (ContractError, ValueError, OSError, TypeError, KeyError):
            self.binding = None
            self.reason = "project_panel_not_installed"
            return False

    def _summary(self):
        from .admin_control import control_status
        state = control_status(self.runtime, self.project)
        routing = state["routing"]
        usage = state.get("project_usage") or {}
        executor = routing.get("executor") or {}
        policy = state["policy"]
        def count(table, where=""):
            return self.runtime.db.execute("SELECT COUNT(*) FROM " + table + " WHERE project=?" + where,
                                           (self.project,)).fetchone()[0]
        expected = {"expected_policy_version": policy["policy_version"],
                    "expected_project_budget_version": state["project_budget_policy"]["version"],
                    "expected_settings_version": state["settings_version"],
                    "expected_session_epoch": state["session_epoch"],
                    "expected_governance_epoch": state["governance_epoch"]}
        root = self.binding["workspace"]
        return {"project_id": self.project, "project_name": Path(root).name, "workspace": root,
                "host_scope": state["host_scope"], "admin_admission_enabled": state["enabled"],
                "model_mode": policy["model_mode"], "model": executor.get("model"),
                "remaining_calls": usage.get("remaining_calls"), "max_calls_per_day": usage.get("max_calls_per_day"),
                "budget_scope": "project", "project_usage": state.get("project_usage"),
                "global_usage": state.get("global_usage"),
                "project_budget_policy": state.get("project_budget_policy"),
                "global_budget_policy": state.get("global_budget_policy"),
                "admission": state.get("admission"),
                "reason": state.get("diagnostic", {}).get("reason")
                    or routing.get("reason") or state.get("admission", {}).get("admission_status"),
                "admin_phase": state.get("diagnostic", {}).get("phase", "idle"),
                "work_summary_enabled": state.get("work_summary", {}).get("enabled", False),
                "counts": {"active": count("active"), "work_memory": count("work_memories"),
                           "pending_problems": count("problems", " AND status='pending'"),
                           "pending_review_cases": state["queue"]["workload"]["stages"]["pending_review"],
                           "awaiting_evidence_cases": state["queue"]["workload"]["stages"]["awaiting_evidence"] + state["queue"]["workload"]["stages"]["local_deferred"],
                           "awaiting_approval_cases": state["queue"]["workload"]["stages"]["awaiting_approval"],
                           "historical_pending_units": state["queue"]["workload"]["history_pending_units"],
                           "candidates": count("candidates", " AND status IN ('candidate','deferred')")},
                "expected": expected}

    def _context(self, handle):
        if self.require_context:
            if not isinstance(handle, str) or not handle:
                raise ContractError("current_context_required")
            return self.runtime.access_context(self.project, handle, expected_epoch=self.epoch)
        return None

    def _debug(self, context_handle):
        """Read bounded observations, never infer Hook/MCP health from a UI pipe."""
        from . import __version__
        from .release import PROTOCOL_VERSION
        from .install import _hook_commands
        from .install_update import inspect_installation
        from .admin_diagnostics import safe_diagnostic

        installed = inspect_installation(self.installation, _hook_commands, target_host=self.runtime.host)
        receipt = installed["receipt"]
        state = self.runtime.status(self.project)
        routing = state.get("admin_routing") or {}
        executor = routing.get("executor") or {}
        dispatch = state.get("admin_dispatch_diagnostic") or {}
        admission = state.get("admission") or {}

        def code(value):
            return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,99}", value) else None

        def number(value):
            return value if type(value) in (int, float) and value >= 0 and value < float("inf") else None

        def usage(value):
            value = value if isinstance(value, dict) else {}
            return {key: number(value.get(key)) for key in ("counted_attempts", "completed_reviews", "failed_attempts",
                    "remaining_calls", "max_calls_per_day", "next_reset_at")}

        bound = False
        if self.require_context:
            # Do not return session_context's task, assignment or execution body.
            bound = self.runtime.session_context(self.project, context_handle, expected_epoch=self.epoch)["context_bound"]
        model = executor.get("model")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_./:@+\-]{1,240}", model):
            model = None
        latest = self.runtime.db.execute("SELECT c.usage FROM admin_calls c JOIN admin_execution_plans p ON p.call_id=c.id "
            "WHERE c.project=? AND p.host_scope=? AND c.state IN ('failed','timeout_unknown','account_changed_unknown','orphaned_unknown') "
            "ORDER BY c.created DESC LIMIT 1", (self.project, self.runtime._session_scope())).fetchone()
        failure = None
        if latest and isinstance(latest[0], str) and len(latest[0]) <= 65536:
            try:
                observed = json.loads(latest[0])
                if isinstance(observed, dict):
                    execution = observed.get("execution") or {}
                    failure = safe_diagnostic(execution.get("provider_diagnostic")) if isinstance(execution, dict) else None
                    failure = failure or safe_diagnostic(observed.get("provider_diagnostic"))
            except (ValueError, TypeError):
                pass
        return {"version": 1, "connection": {"ui_bridge": "responding",
                    "mcp": "responding" if self.require_context else "unknown",
                    "hook": "verified" if bound else "unknown", "context_bound": bound if self.require_context else None},
                "installation": {"state": "verified", "version": receipt["version"],
                    "protocol": receipt["protocol_version"], "release_id": receipt["release_id"],
                    "running_version": __version__, "running_protocol": PROTOCOL_VERSION,
                    "protocol_matches": str(receipt["protocol_version"]) == PROTOCOL_VERSION,
                    "running_runtime_matches_installation": Path(receipt["runtime_directory"]).resolve() == Path(__file__).resolve().parents[1],
                    "latest_release_checked": False},
                "project": {"id": self.project, "workspace": self.binding["workspace"], "home": str(self.runtime.home),
                    "host_scope": self.runtime._session_scope(), "storage_schema": number(state.get("storage_schema_version")),
                    "index_version": number(state.get("index_version"))},
                "administrator": {"enabled": bool(state.get("admin_admission_enabled")),
                    "model_mode": code((routing.get("policy") or {}).get("model_mode")), "model": model,
                    "phase": code(dispatch.get("phase")), "reason": code(dispatch.get("reason")) or code(routing.get("reason")),
                    "failure_reason": code(dispatch.get("failure_reason")), "next_due": number(dispatch.get("next_due")),
                    "blocking_scope": admission.get("blocking_scope") if admission.get("blocking_scope") in {"project", "ALP_HOME"} else None,
                    "project_usage": usage(state.get("project_usage")), "global_usage": usage(state.get("global_usage")),
                    "last_provider_failure": failure}}

    @staticmethod
    def _ui_session(value):
        return (isinstance(value, str) and 0 < len(value) <= 500 and value.strip() == value
                and not any(ord(character) < 32 for character in value))

    def bootstrap(self, context_handle=None, workspace=None, conversation_id=None, ui_session_id=None):
        if not self.eligible():
            return {"visible": False, "reason": self.reason}
        if workspace is not None and not same_workspace(workspace, self.binding["workspace"]):
            return {"visible": False, "reason": "project_panel_not_installed"}
        if self.project_ui:
            if (workspace is None or not self._ui_session(ui_session_id)
                    or context_handle is not None or conversation_id is not None):
                return {"visible": False, "reason": "current_project_ui_required"}
        elif ui_session_id is not None:
            return {"visible": False, "reason": "panel_scope_mismatch"}
        elif not self.require_context and (workspace is None or not isinstance(conversation_id, str)
                                           or not conversation_id.strip() or len(conversation_id) > 500):
            return {"visible": False, "reason": "current_context_required"}
        try:
            context = self._context(context_handle)
        except ContractError:
            return {"visible": False, "reason": "current_context_required"}
        now = time.monotonic()
        self.leases = OrderedDict((key, value) for key, value in self.leases.items() if value["expires"] > now)
        while len(self.leases) >= MAX_PANELS:
            self.leases.popitem(last=False)
        self.requests = OrderedDict((key, value) for key, value in self.requests.items() if key[0] in self.leases)
        capability = secrets.token_hex(32)
        self.leases[capability] = {"expires": now + PANEL_SECONDS, "binding": dict(self.binding),
                                  "context_handle": context_handle, "conversation_id": conversation_id,
                                  "ui_session_id": ui_session_id,
                                  "native_conversation": context["conversation_id"] if context else None}
        return {"visible": True, "summary": self._summary(), "capability": capability,
                **({"scope": "project"} if self.project_ui else {})}

    def _verify(self, capability, context_handle, workspace, conversation_id, ui_session_id=None):
        if not isinstance(capability, str) or capability not in self.leases:
            raise ContractError("panel_access_expired")
        lease = self.leases[capability]
        if lease["expires"] <= time.monotonic() or not self.eligible() or lease["binding"] != self.binding:
            self.leases.pop(capability, None)
            self.requests = OrderedDict((key, value) for key, value in self.requests.items() if key[0] != capability)
            raise ContractError("panel_access_expired")
        if workspace is not None and not same_workspace(workspace, self.binding["workspace"]):
            raise ContractError("panel_project_mismatch")
        if self.project_ui:
            if (workspace is None or not self._ui_session(ui_session_id) or ui_session_id != lease["ui_session_id"]
                    or context_handle is not None or conversation_id is not None):
                raise ContractError("panel_project_ui_mismatch")
        elif ui_session_id is not None:
            raise ContractError("panel_scope_mismatch")
        elif self.require_context:
            if context_handle != lease["context_handle"]:
                raise ContractError("panel_context_mismatch")
            self._context(context_handle)
        elif workspace is None or conversation_id != lease["conversation_id"]:
            raise ContractError("panel_context_mismatch")
        return lease

    def _open_browser(self, review):
        import webbrowser
        from .knowledge_browser import make_browser_server
        from .panel_browser import make_panel_guard
        key = "review" if review else "browser"
        if key in self.browsers:
            server, thread = self.browsers[key]
            try:
                current = server.access_guard is not None and server.access_guard()
            except Exception:
                current = False
            if not current:
                self.browsers.pop(key)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        if key not in self.browsers:
            server = make_browser_server(self.runtime.home / "runtime.sqlite3", self.project,
                review=review, admin_control=review, runtime_host=self.runtime.host,
                runtime_instance=self.runtime.host_instance,
                access_guard=make_panel_guard(self.installation, dict(self.binding),
                    self.runtime.home / "runtime.sqlite3", self.runtime._session_scope(),
                    self.epoch, self.governance_epoch))
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True)
            thread.start()
            self.browsers[key] = (server, thread)
        server, _ = self.browsers[key]
        if not webbrowser.open(server.browser_url):
            raise ContractError("panel_browser_open_failed")

    def action(self, capability, action, context_handle=None, workspace=None, conversation_id=None,
               request_id=None, expected=None, preset=None, confirm=False, ui_session_id=None):
        self._verify(capability, context_handle, workspace, conversation_id, ui_session_id)
        if action not in ACTIONS:
            raise ContractError("unsupported panel action")
        if action in {"status", "debug"}:
            return {"visible": True, "summary": self._summary(),
                    **({"scope": "project"} if self.project_ui else {}),
                    **({"diagnostics": self._debug(context_handle)} if action == "debug" else {})}
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise ContractError("panel request identity is required")
        fingerprint = hashlib.sha256(encode([action, expected, preset, confirm]).encode("utf-8")).hexdigest()
        key = (capability, request_id)
        if key in self.requests:
            prior = self.requests[key]
            if fingerprint != prior["fingerprint"]:
                raise ContractError("panel request identity reused with different content")
            if prior["result"] is None:
                raise ContractError("panel action outcome unknown; inspect status before retrying")
            return prior["result"]
        if sum(key[0] == capability for key in self.requests) >= MAX_REQUESTS:
            raise ContractError("panel request budget reached; reopen the panel")
        if action in {"set_admin_preset", "archive_history"} and confirm is not True:
            raise ContractError("panel action requires explicit confirmation")
        if action == "set_admin_preset":
            if preset not in {"off", *PRESETS}:
                raise ContractError("unsupported administrator preset")
            if (not isinstance(expected, dict) or set(expected) != EXPECTED_FIELDS
                    or any(type(value) is not int or value < 0 for value in expected.values())):
                raise ContractError("panel settings require the displayed versions")
        self.requests[key] = {"fingerprint": fingerprint, "result": None}
        result = {"visible": True, **({"scope": "project"} if self.project_ui else {})}
        if action in {"open_browser", "open_review"}:
            self._open_browser(action == "open_review")
            result["opened"] = "review" if action == "open_review" else "browser"
        elif action == "set_admin_preset":
            from .admin_control import apply_control, control_status
            state = control_status(self.runtime, self.project)
            limits = PRESETS.get(preset) or {key: state["project_budget_policy"]["values"][key] for key in next(iter(PRESETS.values()))}
            policy = state["policy"]
            mode = ("first" if preset != "off" and policy["policy_version"] == 0
                    and policy["model_mode"] == "default" else policy["model_mode"])
            body = {"request_id": request_id, **expected, "enabled": preset != "off",
                    "model_mode": mode, "model": policy["model"] if mode == "fixed" else "",
                    "project_limits": dict(limits), "start": preset != "off"}
            apply_control(self.runtime, self.project, body)
            result["preset"] = preset
        else:
            prepared = self.runtime.history_prepare(self.project, self.binding["workspace"], expected_epoch=self.governance_epoch)
            job_id = prepared["job_id"]
            status = self.runtime.history_run(self.project, job_id, allow_review=False,
                                              max_calls=0, max_seconds=180, expected_epoch=self.governance_epoch)
            job = status.get("job") or {}
            counts = job.get("counts", {})
            result["history"] = {"job_id": job_id, "state": job.get("state", "unknown"),
                "archived_cases": counts.get("cases", 0), "reviewed_cases": counts.get("reviewed_cases", 0),
                "published_knowledge": counts.get("published_candidates", 0), "review_requested": False}
        result["summary"] = self._summary()
        self.requests[key]["result"] = result
        return result

    def close(self):
        self.leases.clear()
        self.requests.clear()
        for server, thread in self.browsers.values():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.browsers.clear()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate panel field")
        result[key] = value
    return result


def serve_panel(installation, input_stream=None, output_stream=None):
    """Private, bounded stdio bridge for the authenticated native sidecar."""
    from .install import _hook_commands
    from .install_update import inspect_installation
    from .runtime import Runtime
    installed = inspect_installation(installation, _hook_commands, target_host="antigravity")
    receipt = installed["receipt"]
    runtime = Runtime(receipt["home"], host="antigravity")
    controller = PanelControl(runtime, runtime.project(receipt["workspace"]), receipt["workspace"],
                              installation, require_context=False)
    source = input_stream or getattr(sys.stdin, "buffer", sys.stdin)
    target = output_stream or sys.stdout
    try:
        while True:
            raw = source.readline(MAX_PANEL_MESSAGE + 1)
            if not raw:
                break
            ident = None
            try:
                if len(raw) > MAX_PANEL_MESSAGE:
                    # A framing error closes the private pipe; no partial body is dispatched.
                    raise ContractError("panel message exceeds budget")
                body = json.loads(raw, object_pairs_hook=_unique)
                if not isinstance(body, dict) or set(body) != {"id", "method", "params"}:
                    raise ContractError("invalid panel message")
                ident = body["id"]
                if isinstance(ident, bool) or not isinstance(ident, (str, int)):
                    raise ContractError("invalid panel request identity")
                params = body["params"]
                if not isinstance(params, dict):
                    raise ContractError("invalid panel arguments")
                allowed = {"context_handle", "workspace", "conversation_id"}
                if body["method"] == "bootstrap":
                    if set(params) - allowed:
                        raise ContractError("unexpected panel arguments")
                    result = controller.bootstrap(**params)
                elif body["method"] == "action":
                    allowed |= {"capability", "action", "request_id", "expected", "preset", "confirm"}
                    if set(params) - allowed:
                        raise ContractError("unexpected panel arguments")
                    result = controller.action(**params)
                else:
                    raise ContractError("unsupported panel method")
                response = {"id": ident, "result": result}
            except (ContractError, ValueError, UnicodeError, TypeError, RecursionError):
                response = {"id": ident, "error": {"message": "panel request rejected; refresh the current project"}}
            except Exception:
                response = {"id": ident, "error": {"message": "panel unavailable"}}
            target.write(encode(response) + "\n")
            target.flush()
            if len(raw) > MAX_PANEL_MESSAGE:
                break
    finally:
        controller.close()
        runtime.close()
    return 0
