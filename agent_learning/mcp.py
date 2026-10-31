"""本地 JSON-RPC stdio MCP；工作资料有界且脱敏，不暴露后台凭证或发布入口。"""
from __future__ import annotations

import json
import math
import re
import sys
from . import __version__
from .runtime import CATEGORIES, KNOWLEDGE_KINDS, METADATA_KEYS, TASK_STATUSES, VALIDATION_LEVELS, ContractError, encode
from .knowledge_output import read_page, search_preview
from .status_output import StatusSummary, status_summary


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def parameter(contract, description, **hints):
    """Document a parameter without changing shared schemas or applying defaults."""
    return {**contract, "description": description, **hints}


S = {"type": "string"}
NONEMPTY = {"type": "string", "minLength": 1, "pattern": r"\S"}
STRINGS = {"type": "array", "items": S}
METADATA = schema({key: parameter(S, "Observed source metadata; omit unknown values.") for key in sorted(METADATA_KEYS)})
CATEGORY = {"type": "string", "enum": sorted(CATEGORIES)}
KNOWLEDGE_KIND = {"type": "string", "enum": sorted(KNOWLEDGE_KINDS)}
PANEL_EXPECTED = schema({key: {"type": "integer", "minimum": 0} for key in (
    "expected_policy_version", "expected_project_budget_version", "expected_settings_version",
    "expected_session_epoch", "expected_governance_epoch")}, (
    "expected_policy_version", "expected_project_budget_version", "expected_settings_version",
    "expected_session_epoch", "expected_governance_epoch"))
PANEL_TOOLS = [
    ("learning_panel", "打开当前已安装项目的轻量控制卡；仅展示状态，不开启管理员或历史吸收。需要本轮真实会话句柄；不支持UI时返回learning-browser本机可视化启动指引。",
     schema({"context_handle": NONEMPTY})),
    ("learning_panel_action", "仅供控制卡按钮使用；需要当前卡片的私有能力及真实会话句柄，不能用作模型执行入口。",
     schema({"capability": {"type": "string", "minLength": 1, "maxLength": 256},
             "context_handle": NONEMPTY,
             "action": {"type": "string", "enum": ["status", "debug", "open_browser", "open_review", "set_admin_preset", "archive_history"]},
             "request_id": {"type": "string", "minLength": 1, "maxLength": 200},
             "expected": PANEL_EXPECTED,
             "preset": {"type": "string", "enum": ["off", "minimal", "light", "balanced", "active"]},
             "confirm": {"type": "boolean"}}, ("capability", "context_handle", "action"))),
]
DIRECT_PANEL_TOOLS = [
    ("learning_panel_open", "从当前项目的插件菜单打开可视化控制台；仅供宿主界面直接调用，不调用模型。",
     schema({})),
    ("learning_panel_direct_action", "仅供已验证项目控制台按钮使用；需要私有能力及宿主提供的当前界面会话。",
     schema({key: value for key, value in PANEL_TOOLS[1][2]["properties"].items() if key != "context_handle"},
            ("capability", "action"))),
]
PANEL_TOOL_NAMES = {entry[0] for entry in PANEL_TOOLS + DIRECT_PANEL_TOOLS}
DIRECT_PANEL_TOOL_NAMES = {entry[0] for entry in DIRECT_PANEL_TOOLS}
KNOWLEDGE_FILTERS = schema({**{key: parameter(NONEMPTY, "Exact source metadata filter; omit if unknown.")
                              for key in sorted(METADATA_KEYS)},
    "category": parameter(CATEGORY, "Knowledge category; omit to search all categories."),
    "knowledge_kind": parameter(KNOWLEDGE_KIND, "fact, solution or pattern; omit to search all kinds.")})
TASK_SCOPE = {"type": "object", "additionalProperties": True, "properties": {
    "mode": {"type": "string", "enum": ["analysis", "read_only", "implementation", "execute"],
             "description": "Current user-authorized work mode; describing it grants no execution permission."},
    "allowed_tools": parameter(STRINGS, "Tool names within the user's current task scope."),
    "allowed_paths": parameter(STRINGS, "Paths within the user's current task scope."),
    "allowed_actions": {"type": "array", "description": "Authorized action classes, not a permission grant.",
                        "items": {"type": "string", "enum": ["read", "write", "execute", "network"]}},
    "budget": {"type": "object", "required": ["remaining"], "properties": {
        "remaining": {"type": ["number", "null"], "minimum": 0,
                      "description": "Remaining task allowance; null means unspecified, not unlimited permission."},
        "unit": parameter(NONEMPTY, "Unit of this task allowance, such as model calls.")},
        "description": "User-defined task allowance; does not reset administrator accounting."},
    "stop_conditions": parameter(STRINGS, "Conditions that require stopping the current work."),
    "verification": parameter(STRINGS, "Observable acceptance checks for this task."),
}}
CANDIDATE = schema({
    "problem_id": NONEMPTY, "problem_revision": {"type": "integer", "minimum": 1},
    "title": NONEMPTY, "content": NONEMPTY, "category": CATEGORY,
    "applicability": NONEMPTY, "limitations": NONEMPTY,
    "evidence_refs": {"type": "array", "items": NONEMPTY, "minItems": 1,
                      "description": "Copy visible raw events[].id verbatim; keep problem_id and problem_revision separate. History related IDs must be explicitly visible."},
    "validation_level": {"type": "string", "enum": sorted(VALIDATION_LEVELS)},
    "metadata": METADATA, "knowledge_kind": KNOWLEDGE_KIND,
}, ("problem_id", "problem_revision", "title", "content", "category", "applicability", "limitations", "evidence_refs", "validation_level"))
CONTEXT_HANDLE = parameter(NONEMPTY, "Copy the current Hook-issued context_handle; never guess or borrow another chat's handle. May be omitted only if this connection is already bound, or for an unbound project query.")
CONVERSATION_ID = parameter(NONEMPTY, "Normally omit: the verified handle selects the conversation. If supplied, it must match that handle.")
TASK_ID = parameter(NONEMPTY, "Copy task.id from session_context or the latest task_update result; never substitute a conversation ID.")
TASK_REVISION = parameter({"type": "integer", "minimum": 1}, "Copy task.revision for that exact task from session_context; this is not write_version.")
EVENT_REFS = parameter(STRINGS, "Visible event IDs from this project, conversation and task revision; a child may cite only its own events. Omit if none are available.", default=[])
IDEMPOTENCY_KEY = parameter({"type": "string", "minLength": 1, "maxLength": 200}, "Stable key for one request. Retry a lost response with the same key and identical original arguments; a genuinely new observation needs a new key.")
WORKER_TOOLS = [
    ("candidate_reference_read", "Read a policy-allowed UNPUBLISHED candidate as an unverified lead, not a formal solution or permission. Requires a bound context/current assignment and a known reference version. Returns versioned section text and pagination; records exposure. If its version/sources change, stop the old read and obtain a current reference; if published, use the mapped formal knowledge instead.", schema({
        "candidate_id": parameter(NONEMPTY, "Known project candidate_id paired with its current reference_version, usually from a [待定参考] card; read does not require prior exposure. Not a knowledge_id."),
        "context_handle": CONTEXT_HANDLE,
        "expected_version": parameter({"type": "string", "minLength": 64, "maxLength": 64}, "Copy the card/read result's 64-character reference_version unchanged; do not construct a version."),
        "section": parameter({"type": "string", "enum": ["content", "applicability", "limitations", "evidence"]}, "Section to expand; keep it unchanged during pagination.", default="content"),
        "offset": parameter({"type": "integer", "minimum": 0}, "Start at 0; continue using the returned next_offset and the same expected_version.", default=0),
        "max_chars": parameter({"type": "integer", "minimum": 512, "maximum": 4000}, "Response JSON character budget, not tokens.", default=2000)}, ["candidate_id", "expected_version"])),
    ("candidate_reference_feedback", "Report a concern about the exact unpublished candidate version displayed to this context. Supply the task id/revision when a task/current assignment exists. Writes a reported case, an unresolved dispute and a retry receipt, suspending automatic reference; does not prove falsity, publish, or launch review. Returns candidate_id/reference_version/problem_id, state=disputed and published=false. Retry lost replies with identical arguments/key. Changed versions require fresh evidence; published candidates must be reported against formal knowledge.", schema({
        "candidate_id": parameter(NONEMPTY, "candidate_id actually displayed to this context; an arbitrary candidate ID is insufficient."),
        "context_handle": CONTEXT_HANDLE,
        "expected_version": parameter({"type": "string", "minLength": 64, "maxLength": 64}, "Exact reference_version from that displayed card/read; stale or undisplayed versions are rejected."),
        "summary": parameter({"type": "string", "minLength": 1, "maxLength": 2000}, "Observed concern or inapplicability and its basis; do not claim unverified falsity."),
        "expected_task_id": TASK_ID, "expected_task_revision": TASK_REVISION,
        "idempotency_key": IDEMPOTENCY_KEY, "event_refs": EVENT_REFS}, ["candidate_id", "expected_version", "summary", "idempotency_key"])),
    ("knowledge_search", "Search this project's published formal knowledge before expanding relevant entries with knowledge_read. Returns bounded items with knowledge_id/revision, previews, applicability, source health and truncation flags; a match is not proof of suitability. Updates retrieval/cache accounting and, when bound, records returned exposures; does not edit knowledge. Empty items are a valid result. Unknown filters should be omitted.", schema({
        "query": parameter(S, "Current concrete problem, error code or technical identifier; avoid repeating an unchanged search without new need."),
        "limit": parameter({"type": "integer", "minimum": 1, "maximum": 10}, "Maximum result count, subject to the response budget.", default=5),
        "filters": parameter(KNOWLEDGE_FILTERS, "Optional exact metadata/category filters; omit unknown fields."),
        "context_handle": CONTEXT_HANDLE,
        "max_chars": parameter({"type": "integer", "minimum": 512, "maximum": 20000}, "Response JSON character budget, not tokens.", default=6000)}, ["query"])),
    ("knowledge_read", "Expand one active formal knowledge revision; returns section text, revision, health, truncated/next_offset and fields_truncated. Check conditions and limitations before use. Continue the same section with next_offset and expected_revision; read other truncated fields in their own sections. A revision conflict requires restarting at offset 0 against the current revision. When bound, records exposure and file-state observations; does not modify knowledge.", schema({
        "knowledge_id": parameter(NONEMPTY, "knowledge_id from formal search/injection; never a candidate_id or memory_id."),
        "context_handle": CONTEXT_HANDLE,
        "max_chars": parameter({"type": "integer", "minimum": 1024, "maximum": 50000}, "Response JSON character budget, not tokens.", default=12000),
        "expected_revision": parameter({"type": "integer", "minimum": 1}, "Copy the returned formal revision; required when offset>0, recommended on the first page."),
        "section": parameter({"type": "string", "enum": ["content", "applicability", "limitations"]}, "Section to read; retain during pagination.", default="content"),
        "offset": parameter({"type": "integer", "minimum": 0}, "First page is 0; subsequent pages use next_offset without increasing it manually.", default=0)}, ["knowledge_id"])),
    ("work_memory_search", "Find observed work/file-history indexes, not formal solutions or execution authority. Returns items with memory_id/revision, source state, summaries and file links; use work_memory_read only for relevant missing detail. Does not read project file contents or rewrite original history. Bound calls write exposure receipts, so this is not a database-write-free operation. No matches or disabled history need not be retried.", schema({
        "query": parameter({"type": "string", "maxLength": 8000}, "Current work topic; omit for an unqualified/file-only index query.", default=""),
        "file_path": parameter({"type": "string", "minLength": 1, "maxLength": 4096, "pattern": r"\S"}, "Optional path in this installed workspace to filter recorded file associations; not permission to scan files."),
        "context_handle": CONTEXT_HANDLE,
        "limit": parameter({"type": "integer", "minimum": 1, "maximum": 10}, "Maximum index items, subject to budget.", default=5),
        "max_chars": parameter({"type": "integer", "minimum": 512, "maximum": 20000}, "Response JSON character budget, not tokens.", default=3000)})),
    ("work_memory_read", "Expand relevant observed history in a verified context. Returns revision, section, pagination, source/file checks; previously complete reads may return already_read instead of the body. Keep section/revision and use next_offset on continuation. Revision changes require a fresh first page. Writes exposure/read tracking, not original history. file_check.complete=false means unknown file state; truncated original evidence cannot be reconstructed.", schema({
        "memory_id": parameter(NONEMPTY, "memory_id from work search/injection, not a knowledge_id or event ID."),
        "context_handle": CONTEXT_HANDLE,
        "expected_revision": parameter({"type": "integer", "minimum": 1}, "Copy this memory's returned revision; required when offset>0."),
        "offset": parameter({"type": "integer", "minimum": 0}, "0 for the first page; continue using next_offset with the same section/revision.", default=0),
        "max_chars": parameter({"type": "integer", "minimum": 1024, "maximum": 50000}, "Response JSON character budget, not tokens.", default=6000),
        "reread": parameter({"type": "boolean"}, "Set true only to deliberately re-present an already read version; starts a new read round. Normal continuation pages omit it.", default=False),
        "section": parameter({"type": "string", "enum": ["summary", "evidence"]}, "Select summary or visible evidence; keep the section unchanged while paging.", default="summary")}, ["memory_id"])),
    ("session_context", "Inspect the current verified binding before task writes. Returns context_bound/rebind_required, host_epoch, task (id/revision/write_version/status), and any assignment. Without a handle/bound connection it returns only project state, not another chat. A child sees its assignment and cannot overwrite the parent task. If reconnect_required is returned, reload the host connection for a new Hook-issued handle; never borrow one.", schema({"context_handle": CONTEXT_HANDLE})),
    ("task_update", "Persist the parent's current user-authorized task before writes/execution/delegation; this grants no execution rights. Returns task id/revision/write_version/status. If a task exists, ALWAYS supply its id and write_version from session_context, even for new_task=true; revision is not a substitute. Children cannot overwrite it. No idempotency key: after a lost reply or stale version, read session_context and compare the state before deciding to write again; never blindly repeat new_task.", schema({
        "context_handle": CONTEXT_HANDLE, "conversation_id": CONVERSATION_ID,
        "goal": parameter(NONEMPTY, "Current user-authorized goal; ordinary questions do not need a synthetic task."),
        "scope": parameter(TASK_SCOPE, "Task boundaries and acceptance checks. Omit to retain existing scope; a new task defaults to {}. This cannot approve execution."),
        "expected_revision": parameter({"type": "integer", "minimum": 0}, "Optional extra task.revision check; never replaces expected_task_id and expected_write_version."),
        "status": parameter({"type": "string", "enum": sorted(TASK_STATUSES)}, "Omit to retain existing status (new task: active). Use delivered only for completed work; resuming requires resume=true."),
        "new_task": parameter({"type": "boolean"}, "True for genuinely different work: creates a new task identity. Existing-task CAS fields remain required; false updates the current task.", default=False),
        "expected_task_id": TASK_ID,
        "expected_write_version": parameter({"type": "integer", "minimum": 0}, "Copy task.write_version from the latest context/result; required with expected_task_id whenever a task exists."),
        "resume": parameter({"type": "boolean"}, "Explicitly reactivate an existing inactive task; incompatible with new_task=true or non-active status. Does not restore execution approval.", default=False),
    }, ["goal"])),
    ("case_feedback", "Record a material issue, correction or resolution observation; routine tool events are captured by Hooks. Writes a reported Feedback event and problem/evidence records. With knowledge_id the default is an unresolved counterexample against that revision, which may suspend automatic reference; it is not a mere note or proof of falsity. Returns a problem (id/revision/status). Parent task identity/revision are required when a task exists; children use their fixed assignment. Retry lost replies only with the same idempotency_key and identical original arguments.", schema({
        "context_handle": CONTEXT_HANDLE, "conversation_id": CONVERSATION_ID,
        "summary": parameter(NONEMPTY, "Concise observed issue/correction and its basis; distinguish report, tool evidence and user acceptance."),
        "category": parameter(CATEGORY, "Issue category; does not imply a validation level.", default="unknown"),
        "event_refs": EVENT_REFS,
        "resolution": parameter(S, "Observed resolution, if any. Text alone never marks the issue resolved or user-accepted.", default=""),
        "knowledge_id": parameter(NONEMPTY, "Optional formal knowledge being questioned; supplying it defaults feedback_kind to counterexample."),
        "knowledge_revision": parameter({"type": "integer", "minimum": 1}, "Exact displayed formal revision; requires knowledge_id. Omission selects the active revision if available, so prefer the explicit observed revision."),
        "feedback_kind": parameter({"type": "string", "enum": ["observation", "counterexample"]}, "Default: counterexample when knowledge_id is supplied, otherwise observation. Counterexample requires knowledge_id."),
        "idempotency_key": parameter(IDEMPOTENCY_KEY, "Optional stable key. Omission creates no replay receipt: do not blindly retry a lost reply. With a key, preserve it and all original arguments on retry."),
        "expected_task_id": TASK_ID, "expected_task_revision": TASK_REVISION,
    }, ["summary"])),
    ("knowledge_use", "Record ACTUAL use of a formal revision previously returned to this exact task/agent/write version; merely seeing it is insufficient. Requires current task/assignment and active knowledge. Writes an adoption receipt; returns reported adoption_id, knowledge_state and operation_observation, not causality or user acceptance. Same-key identical retries return the receipt; a key reused with different arguments is rejected. On task/exposure conflict, inspect current context before attempting new work.", schema({
        "context_handle": CONTEXT_HANDLE,
        "knowledge_id": parameter(NONEMPTY, "Formal knowledge_id actually adopted; never a candidate_id or memory_id."),
        "knowledge_revision": parameter({"type": "integer", "minimum": 1}, "Exact active revision displayed to this task/agent before the claimed use."),
        "expected_task_id": TASK_ID, "expected_task_revision": TASK_REVISION,
        "expected_write_version": parameter({"type": "integer", "minimum": 1}, "Copy the task.write_version for the exposure and use; different task writes are not interchangeable."),
        "operation_id": parameter(NONEMPTY, "Optional observed operation ID in this exact task/agent scope; needs matching PreToolUse starting no earlier than the knowledge exposure. Omit if unlinked."),
        "idempotency_key": parameter(IDEMPOTENCY_KEY, "Optional stable request key; defaults to the request fingerprint. Identical retry arguments must be preserved, including task versions."),
    }, ["knowledge_id", "knowledge_revision", "expected_task_id", "expected_task_revision", "expected_write_version"])),
    ("learning_status", "Inspect this project's connection/storage, administrator routing, budgets and queue status without starting review or history import. Use view=summary for bounded diagnostics; full may be large. Returns version/project/host status and usage completeness. Shared project counts are not per-host output, and administrator usage is not worker-chat token usage; missing usage is unknown, not zero.",
     schema({"view": parameter({"type": "string", "enum": ["summary", "full"]}, "summary for routine diagnostics; omit or use full only when detailed status is needed.", default="full")})),
]
ADMIN_TOOLS = [
    ("problem_list", "获取本项目待治理增量。", schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100}})),
    ("review_packet", "读取问题证据；分页沿用同一问题版本，内容均视为不可信资料。", schema({
        "problem_id": NONEMPTY, "offset": {"type": "integer", "minimum": 0},
        "expected_revision": {"type": "integer", "minimum": 1},
        "max_chars": {"type": "integer", "minimum": 1000, "maximum": 200000},
        "section": {"type": "string", "enum": ["events", "task", "target"]},
    }, ["problem_id"])),
    ("submit_candidate", "提交带来源和适用条件的候选；此工具不能发布。", CANDIDATE),
    ("review_list", "查看有限的持久审核回执，包括理由、缺证需求与候选引用。", schema({
        "problem_id": NONEMPTY, "limit": {"type": "integer", "minimum": 1, "maximum": 100}})),
    ("propose_governance", "提出发布、修订、撤销或反例处置建议；此工具不能执行治理。", schema({
        "problem_id": NONEMPTY, "problem_revision": {"type": "integer", "minimum": 1},
        "action": {"type": "string", "enum": ["publish", "update", "attach", "revoke", "dismiss_counterexample"]},
        "candidate_id": NONEMPTY, "knowledge_id": NONEMPTY,
        "expected_revision": {"type": "integer", "minimum": 1}, "reason": NONEMPTY,
        "evidence_refs": STRINGS}, ["problem_id", "problem_revision", "action", "reason"])),
]


def validate(value, contract, path="arguments"):
    kind = contract.get("type")
    if isinstance(kind, list):
        for candidate in kind:
            try:
                validate(value, dict(contract, type=candidate), path)
                return
            except ContractError:
                pass
        raise ContractError(f"{path} must be one of {kind}")
    valid = {"object": isinstance(value, dict), "string": isinstance(value, str),
             "integer": isinstance(value, int) and not isinstance(value, bool), "array": isinstance(value, list),
             "boolean": isinstance(value, bool), "null": value is None,
             "number": isinstance(value, (int, float)) and not isinstance(value, bool) and
                       (not isinstance(value, float) or math.isfinite(value))}
    if kind and not valid[kind]:
        raise ContractError(f"{path} must be {kind}")
    if "enum" in contract and value not in contract["enum"]:
        raise ContractError(f"invalid {path}")
    if kind == "string":
        if len(value) < contract.get("minLength", 0) or len(value) > contract.get("maxLength", len(value)):
            raise ContractError(f"invalid {path} length")
        if "pattern" in contract and not re.search(contract["pattern"], value):
            raise ContractError(f"invalid {path} text")
    if kind in {"integer", "number"} and (value < contract.get("minimum", value) or value > contract.get("maximum", value)):
        raise ContractError(f"invalid {path} range")
    if kind == "object":
        if any(key not in value for key in contract.get("required", [])):
            raise ContractError(f"missing {path} field")
        properties = contract.get("properties", {})
        if contract.get("additionalProperties") is False and set(value) - set(properties):
            raise ContractError(f"unexpected {path} field")
        for key, subvalue in value.items():
            if key in properties:
                validate(subvalue, properties[key], f"{path}.{key}")
    if kind == "array":
        if len(value) < contract.get("minItems", 0) or len(value) > contract.get("maxItems", len(value)):
            raise ContractError(f"invalid {path} item count")
        for item in value:
            validate(item, contract["items"], path)


class ProtocolError(ContractError):
    """协议错误与可由模型修正的业务错误分开，仍兼容直接调用的ContractError。"""
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.data = code, data


class _PanelResult(dict):
    """模型可见结果与仅交给UI的短期能力分开，普通encode不会序列化私有字段。"""
    def __init__(self, body, private):
        super().__init__(body)
        self.panel_private = private


class _DirectPanelResult(_PanelResult):
    """A UI-only result has no model-visible body or structured content."""
    def __init__(self, private):
        super().__init__({}, private)


def error_response(ident, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": ident, "error": error}


class Server:
    MAX_MESSAGE_BYTES = 2 * 1024 * 1024
    SUPPORTED_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}

    def __init__(self, runtime, project, role="worker", context_handle=None, workspace=None,
                 panel_installation=None, panel_discovery=False):
        if role not in {"worker", "admin"}:
            raise ContractError("invalid server role")
        if type(panel_discovery) is not bool or panel_discovery and (runtime.host != "codex" or role != "worker"):
            raise ContractError("panel discovery requires a Codex worker connection")
        self.runtime, self.project = runtime, project
        self.role = role
        self.per_request_context = runtime.host in {"antigravity", "dsh"} and context_handle is None
        self.context_handle = context_handle
        # 仅受信启动入口可以提供工作区；模型参数不能选择任意外部checkout。
        self.workspace = workspace
        self.epoch = runtime.session_epoch()
        self.governance_epoch = runtime.host_epoch()
        self.state, self.protocol_version = "new", None
        from .work_read_delivery import WorkReadDelivery
        self.work_read_delivery = WorkReadDelivery()
        self.tools = {name: (description, contract) for name, description, contract in
                      WORKER_TOOLS + (ADMIN_TOOLS if role == "admin" else [])}
        self.panel, self.direct_panel, self.panel_ui_supported = None, None, False
        # Desktop discovers entrypoints through its host catalog, then routes
        # calls through the current thread's connection. Discovery grants no
        # project access and never chooses an installation for a caller.
        self.panel_discovery = panel_discovery
        if panel_discovery:
            self.tools.update({name: (description, contract) for name, description, contract in DIRECT_PANEL_TOOLS})
        if panel_installation is not None and role == "worker":
            from .panel_control import PanelControl
            self.panel = PanelControl(runtime, project, workspace, panel_installation, require_context=True)
            if self.panel.eligible():
                self.tools[PANEL_TOOLS[0][0]] = PANEL_TOOLS[0][1:]
                if runtime.host == "codex":
                    # Codex 的本地桥不一定声明 iframe 扩展；原生菜单描述与旧卡片协商分别处理。
                    self.direct_panel = PanelControl(runtime, project, workspace, panel_installation,
                                                     require_context=False, project_ui=True)
                    if self._direct_panel_eligible():
                        self.tools.update({name: (description, contract) for name, description, contract in DIRECT_PANEL_TOOLS})

    def _panel_eligible(self):
        return self.panel is not None and self.panel.eligible()

    def _direct_panel_eligible(self):
        return self.direct_panel is not None and self.direct_panel.eligible()

    def _work_delivery_scope(self, access, handle):
        r, p = self.runtime, self.project
        r.access_context(p, handle, expected_epoch=self.epoch)
        task = r._retrieval_task(p, access["conversation_id"], access["agent_id"], handle)
        state = r._work_task_state(p, access["conversation_id"], access["agent_id"], handle, task)
        table, where, key = r._ensure_context(p, access["conversation_id"], access["agent_id"])
        epoch = r.db.execute(f"SELECT epoch FROM {table} WHERE {where}", key).fetchone()[0]
        from .work_memory import _exposure_task_key
        scope = (p, handle, r.host, r.host_instance, self.epoch, r.host_epoch(), epoch, _exposure_task_key(state))
        if r.host == "antigravity":
            trajectory = r.db.execute("SELECT generation,last_invocation,ambiguous FROM antigravity_trajectories "
                "WHERE project=? AND conversation=?", (p, access["conversation_id"])).fetchone()
            scope = ((*scope, tuple(trajectory)) if trajectory and trajectory["last_invocation"] >= 0
                     and not trajectory["ambiguous"] else None)
        return scope, state, epoch

    def _direct_panel_call(self, name, args, request_meta):
        if not self._direct_panel_eligible():
            if self.panel_discovery and name == "learning_panel_open":
                return _DirectPanelResult({"visible": False, "scope": "project", "reason": "project_panel_not_installed"})
            raise ContractError("project_panel_not_installed")
        # 由 Codex 的宿主桥覆盖写入；不从工具参数、iframe hostContext 或历史句柄推断。
        if not isinstance(request_meta, dict):
            raise ContractError("trusted_ui_session_required_use_project_action")
        sessions = [request_meta[key] for key in ("thread_id", "threadId") if key in request_meta]
        if (not sessions or any(not isinstance(value, str) or not value or len(value) > 500
                               or value.strip() != value or any(ord(char) < 32 for char in value)
                               for value in sessions) or len(set(sessions)) != 1):
            raise ContractError("trusted_ui_session_required_use_project_action")
        ui_session_id = sessions[0]
        if name == "learning_panel_open":
            result = self.direct_panel.bootstrap(workspace=self.workspace, ui_session_id=ui_session_id)
        else:
            result = self.direct_panel.action(args["capability"], args["action"], workspace=self.workspace,
                ui_session_id=ui_session_id, request_id=args.get("request_id"), expected=args.get("expected"),
                preset=args.get("preset"), confirm=args.get("confirm", False))
        return _DirectPanelResult({**result, "scope": "project"})

    def _panel_call(self, name, args):
        if not self._panel_eligible():
            if name == "learning_panel_action":
                raise ContractError("panel UI is unavailable; reopen learning-panel in a supported client")
            return {"visible": False, "reason": "project_panel_not_installed"}
        # 旧连接的默认句柄不能证明当前对话；打开卡片和每次按钮操作均显式提交句柄。
        handle = args.get("context_handle")
        if handle is not None:
            self._handle(args)
        if not self.panel_ui_supported:
            if name == "learning_panel_action":
                raise ContractError("panel UI is unavailable; reopen learning-panel in a supported client")
            if not handle:
                return {"visible": False, "reason": "current_context_required"}
            try:
                self.runtime.access_context(self.project, handle, expected_epoch=self.epoch)
            except ContractError:
                return {"visible": False, "reason": "current_context_required"}
            # 只提供已验证项目的启动信息；不签发卡片能力、不弹窗、不启动模型。
            binding = self.panel.binding
            return {"visible": False, "reason": "panel_ui_unavailable", "fallback_skill": "learning-browser",
                    "fallback": {"kind": "local_browser", "mode": "read_only", "skill": "learning-browser",
                        "installation": binding["installation"], "workspace": binding["workspace"],
                        "home": str(self.runtime.home), "host": self.runtime.host,
                        "host_instance": self.runtime.host_instance, "diagnostic_skill": "learning-debug",
                        "instructions": "使用 learning-browser 核对该安装的固定运行入口并启动 browser --open；"
                            "页面提供知识浏览与待办查看，当前连接诊断使用 learning-debug。"
                            "用户明确请求审批或管理员设置时使用 learning-review；打开页面不批准知识或开启模型。"}}
        if name == "learning_panel":
            result = self.panel.bootstrap(context_handle=handle, workspace=self.workspace)
        else:
            result = self.panel.action(args["capability"], args["action"], context_handle=handle,
                workspace=self.workspace, request_id=args.get("request_id"), expected=args.get("expected"),
                preset=args.get("preset"), confirm=args.get("confirm", False))
        from .panel_output import model_panel_result
        public = model_panel_result(result)
        private = dict(result)
        if result.get("visible") is True and handle is not None:
            private["context_handle"] = handle
        return _PanelResult(public, private)

    def close(self):
        self.work_read_delivery.clear()
        if self.panel is not None:
            self.panel.close()
        if self.direct_panel is not None:
            self.direct_panel.close()

    def _check_epoch(self):
        if (self.runtime.session_epoch() != self.epoch
                or self.role == "admin" and self.runtime.host_epoch() != self.governance_epoch):
            raise ProtocolError(-32001, "reconnect_required", {"reason": "host_epoch_changed"})

    def _handle(self, args):
        if self.per_request_context:
            return args.get("context_handle")
        handle = args.get("context_handle", self.context_handle)
        if self.context_handle is not None and handle != self.context_handle:
            raise ContractError("context_mismatch: connection is bound to another handle")
        return handle

    def _bind(self, handle):
        if not self.per_request_context:
            self.context_handle = handle

    def call(self, name, args, *, request_meta=None):
        self._check_epoch()
        if not isinstance(name, str):
            raise ProtocolError(-32602, "tool name must be a string")
        if name not in self.tools:
            raise ProtocolError(-32602, "unknown tool")
        validate(args, self.tools[name][1])
        if name in DIRECT_PANEL_TOOL_NAMES:
            return self._direct_panel_call(name, args, request_meta)
        if name in {"learning_panel", "learning_panel_action"}:
            return self._panel_call(name, args)
        p, r = self.project, self.runtime
        if name in {"candidate_reference_read", "candidate_reference_feedback"}:
            from .candidate_references import read as candidate_read, feedback as candidate_feedback
            handle = self._handle(args)
            if handle is None:
                raise ContractError("candidate reference requires a verified context_handle")
            r.access_context(p, handle, expected_epoch=self.epoch)
            if name == "candidate_reference_read":
                result = candidate_read(r, p, handle, args["candidate_id"], args["expected_version"],
                    section=args.get("section", "content"), offset=args.get("offset", 0), max_chars=args.get("max_chars", 2000), workspace=self.workspace)
            else:
                result = candidate_feedback(r, p, handle, args["candidate_id"], args["expected_version"],
                    summary=args["summary"], idempotency_key=args["idempotency_key"], event_refs=args.get("event_refs", []),
                    expected_task_id=args.get("expected_task_id"), expected_task_revision=args.get("expected_task_revision"))
            r.assert_session_epoch(self.epoch)
            self._bind(handle)
            return result
        if name in {"work_memory_search", "work_memory_read"}:
            handle = self._handle(args)
            access = r.access_context(p, handle, expected_epoch=self.epoch) if handle is not None else None
            if name == "work_memory_search":
                result = r.work_search(p, args.get("query", ""), file_path=args.get("file_path"),
                                       workspace=self.workspace, limit=args.get("limit", 5),
                                       max_chars=args.get("max_chars", 3000))
                items = result["items"]
            else:
                if access is None:
                    raise ContractError("work_memory_read requires a verified context_handle")
                if args.get("offset", 0) and "expected_revision" not in args:
                    raise ContractError("expected_revision is required to continue a section page")
                # The receipt decision shares a snapshot with live identity, source
                # and file checks. Complete evidence need not be assembled again.
                with r.transaction():
                    delivery_scope, task_state, context_epoch = self._work_delivery_scope(access, handle)
                result = r.work_read(p, args["memory_id"], expected_revision=args.get("expected_revision"),
                    offset=args.get("offset", 0), max_chars=args.get("max_chars", 6000),
                    section=args.get("section", "summary"), workspace=self.workspace,
                    _delivery=self.work_read_delivery, _delivery_scope=delivery_scope,
                    _reread=args.get("reread", False))
                with r.transaction():
                    current_scope, task_state, context_epoch = self._work_delivery_scope(access, handle)
                    if current_scope != delivery_scope:
                        raise ContractError("work context changed; retry reading in the current context")
                    r.work_record_exposure(p, access["conversation_id"], [result], agent_id=access["agent_id"],
                        task_state=task_state, context_epoch=context_epoch, method="mcp",
                        context_handle=handle, expected_epoch=self.epoch)
                self._bind(handle)
                return (result if result.get("already_read") else self.work_read_delivery.project(
                    result, delivery_scope, reread=args.get("reread", False)))
            if access is not None:
                with r.transaction():
                    _, task_state, context_epoch = self._work_delivery_scope(access, handle)
                    r.work_record_exposure(p, access["conversation_id"], items, agent_id=access["agent_id"],
                                           task_state=task_state, context_epoch=context_epoch, method="mcp",
                                           context_handle=handle, expected_epoch=self.epoch)
                self._bind(handle)
            else:
                # 未绑定查询只返回项目公开索引，不能为猜测的会话登记展示。
                r.assert_session_epoch(self.epoch)
            return result
        if name in {"knowledge_search", "knowledge_read"}:
            file_observations = {}
            handle = self._handle(args)
            if handle is not None:
                # 先验证身份再读知识；无效句柄不能得到正文后才报错。
                r.access_context(p, handle, expected_epoch=self.epoch)
            if name == "knowledge_search":
                body = {key: value for key, value in args.items() if key not in {"context_handle", "max_chars"}}
                found = r.search(p, **body)
                from .work_files import knowledge_file_observation
                for item in found.get("items", []):
                    observation = knowledge_file_observation(r, p, item, self.workspace)
                    file_observations[f"{item['knowledge_id']}@{item['revision']}"] = observation
                    if observation and observation["changed"]:
                        item["source_files_changed"] = True
                result = search_preview(found, args.get("max_chars", 6000))
                items = result["items"]
            else:
                if args.get("offset", 0) and "expected_revision" not in args:
                    raise ContractError("expected_revision is required to continue a section page")
                try:
                    current = r.read(p, args["knowledge_id"])
                    from .work_files import knowledge_file_observation
                    observation = knowledge_file_observation(r, p, current, self.workspace)
                    file_observations[f"{current['knowledge_id']}@{current['revision']}"] = observation
                    if observation and observation["changed"]:
                        current["source_files_changed"] = True
                    if "expected_revision" in args and current["revision"] != args["expected_revision"]:
                        raise ContractError("knowledge revision changed; restart reading from offset 0")
                    result = read_page(current, args.get("max_chars", 12000), args.get("offset", 0),
                                       args.get("section", "content"))
                except ValueError as exc:
                    raise ContractError(str(exc)) from exc
                items = [result]
            if handle is not None:
                # 登记生成响应中实际存在的修订；再次验证epoch关闭身份变更窗口。
                r.record_exposure(p, handle, items, expected_epoch=self.epoch, file_observations=file_observations)
                self._bind(handle)
            else:
                r.assert_session_epoch(self.epoch)
            return result
        if name == "session_context":
            handle = self._handle(args)
            context = r.session_context(p, handle, expected_epoch=self.epoch)
            if handle is not None:
                self._bind(handle)
            return context
        if name in {"task_update", "case_feedback"}:
            handle = self._handle(args)
            context = r.access_context(p, handle, args.get("conversation_id"), expected_epoch=self.epoch)
            self._bind(handle)
            body = {key: value for key, value in args.items() if key != "context_handle"}
            body["conversation_id"] = context["conversation_id"]
            body.update(context_handle=handle, expected_epoch=self.epoch)
            if name == "task_update": return r.task_update(p, **body)
            return r.feedback(p, **body)
        if name == "learning_status":
            status = r.status(p)
            return status_summary(status) if args.get("view") == "summary" else status
        if name == "knowledge_use":
            handle = self._handle(args)
            body = {key: value for key, value in args.items() if key != "context_handle"}
            result = r.knowledge_use(p, context_handle=handle, expected_epoch=self.epoch, **body)
            self._bind(handle)
            return result
        if name == "problem_list": return r.problems(p, **args)
        if name == "review_packet": return r.review_packet(p, **args)
        if name == "submit_candidate": return r.submit_candidate(p, expected_epoch=self.governance_epoch, **args)
        if name == "review_list":
            from .admin_decisions import get_reviews
            return get_reviews(r, p, **args)
        if name == "propose_governance":
            from .admin_governance import propose_governance
            return propose_governance(r, p, expected_epoch=self.governance_epoch, **args)
        raise ContractError("unknown tool")

    def request(self, message):
        if isinstance(message, dict) and isinstance(message.get("method"), str) and message["method"] in {
                "initialize", "tools/list", "resources/list", "resources/read"}:
            from .panel_control import installation_inspection_scope
            with installation_inspection_scope():
                return self._request(message)
        return self._request(message)

    def _request(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return error_response(None, -32600, "invalid request")
        method, params = message.get("method"), message.get("params", {})
        if not isinstance(method, str) or not method:
            return error_response(None, -32600, "method must be a nonempty string")
        notification = "id" not in message
        ident = message.get("id")
        if not notification and (isinstance(ident, bool) or not isinstance(ident, (str, int))):
            return error_response(None, -32600, "id must be a string or integer")
        try:
            self._check_epoch()
            if not isinstance(params, dict):
                raise ProtocolError(-32602, "params must be an object")
            if notification:
                if method == "notifications/initialized" and self.state == "negotiated":
                    self.state = "ready"
                return None
            if method == "initialize":
                if self.state != "new":
                    raise ProtocolError(-32600, "already_initialized")
                version, client = params.get("protocolVersion"), params.get("clientInfo")
                if (not isinstance(version, str) or not version or not isinstance(params.get("capabilities"), dict)
                        or not isinstance(client, dict) or any(not isinstance(client.get(key), str) or not client[key]
                                                              for key in ("name", "version"))):
                    raise ProtocolError(-32602, "initialize requires protocolVersion, capabilities and clientInfo")
                self.protocol_version = version if version in self.SUPPORTED_VERSIONS else "2025-11-25"
                from .panel_ui import MIME_TYPE, UI_EXTENSION
                extension = params["capabilities"].get("extensions", {}).get(UI_EXTENSION) if isinstance(
                    params["capabilities"].get("extensions"), dict) else None
                self.panel_ui_supported = (isinstance(extension, dict) and
                    isinstance(extension.get("mimeTypes"), list) and MIME_TYPE in extension["mimeTypes"])
                if self._panel_eligible() and self.panel_ui_supported:
                    self.tools[PANEL_TOOLS[1][0]] = PANEL_TOOLS[1][1:]
                    if self._direct_panel_eligible():
                        self.tools.update({name: (description, contract) for name, description, contract in DIRECT_PANEL_TOOLS})
                self.state = "negotiated"
                result = {"protocolVersion": self.protocol_version, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "agent-learning", "version": __version__}}
                if self.panel_discovery or self._panel_eligible() and (self.panel_ui_supported or self._direct_panel_eligible()):
                    result["capabilities"]["resources"] = {}
            elif method == "ping":
                result = {}
            elif self.state != "ready":
                raise ProtocolError(-32002, "initialization_required", {"state": self.state})
            elif method == "tools/list":
                result = {"tools": []}
                for name, (description, contract) in self.tools.items():
                    if name in PANEL_TOOL_NAMES - DIRECT_PANEL_TOOL_NAMES and not self._panel_eligible():
                        continue
                    if name in DIRECT_PANEL_TOOL_NAMES and not (self.panel_discovery or self._direct_panel_eligible()):
                        continue
                    tool = {"name": name, "description": description, "inputSchema": contract}
                    if name == "learning_panel" and self.panel_ui_supported:
                        from .panel_ui import RESOURCE_URI
                        tool.update(annotations={"readOnlyHint": True, "openWorldHint": False},
                                    _meta={"ui": {"resourceUri": RESOURCE_URI}})
                    elif name == "learning_panel_action":
                        tool.update(annotations={"readOnlyHint": False, "openWorldHint": False},
                                    _meta={"ui": {"visibility": ["app"]}})
                    elif name == "learning_panel_open":
                        from .panel_ui import DIRECT_RESOURCE_URI
                        tool.update(title="Agent Learning", annotations={"readOnlyHint": True, "openWorldHint": False},
                                    _meta={"ui": {"resourceUri": DIRECT_RESOURCE_URI, "visibility": ["app"]},
                                           "openai/ui": {"entrypoints": [{"type": "thread"}]}})
                    elif name == "learning_panel_direct_action":
                        tool.update(annotations={"readOnlyHint": False, "openWorldHint": False},
                                    _meta={"ui": {"visibility": ["app"]}})
                    result["tools"].append(tool)
            elif (method in {"resources/list", "resources/read"}
                  and (self.panel_discovery or self.panel is not None
                       and (self.panel_ui_supported or self._direct_panel_eligible()))):
                from .panel_ui import DIRECT_COMPAT_RESOURCE_URI, DIRECT_RESOURCE_URI, MIME_TYPE, RESOURCE_URI, resource_content
                if not self._panel_eligible() and not self.panel_discovery:
                    if method == "resources/read":
                        raise ProtocolError(-32602, "panel resource is unavailable")
                    result = {"resources": []}
                elif method == "resources/list":
                    result = {"resources": ([{"uri": RESOURCE_URI, "name": "Agent Learning control panel",
                                              "mimeType": MIME_TYPE}] if self.panel_ui_supported and self._panel_eligible() else [])}
                    if self.panel_discovery or self._direct_panel_eligible():
                        result["resources"].append({"uri": DIRECT_RESOURCE_URI,
                            "name": "Agent Learning project controls", "mimeType": MIME_TYPE})
                else:
                    uris = ({RESOURCE_URI} if self.panel_ui_supported and self._panel_eligible() else set())
                    if self.panel_discovery or self._direct_panel_eligible():
                        uris.update((DIRECT_RESOURCE_URI, DIRECT_COMPAT_RESOURCE_URI))
                    if params.get("uri") not in uris or set(params) - {"uri", "_meta"}:
                        raise ProtocolError(-32602, "unknown panel resource")
                    content = resource_content(direct=params["uri"] in {DIRECT_RESOURCE_URI, DIRECT_COMPAT_RESOURCE_URI})
                    content["uri"] = params["uri"]
                    result = {"contents": [content]}
            elif method == "tools/call":
                if not isinstance(params.get("name"), str) or not isinstance(params.get("arguments", {}), dict):
                    raise ProtocolError(-32602, "tools/call requires a string name and object arguments")
                try:
                    body = self.call(params["name"], params.get("arguments", {}), request_meta=params.get("_meta"))
                    if isinstance(body, _DirectPanelResult):
                        return {"jsonrpc": "2.0", "id": ident, "result": {"content": [], "isError": False,
                            "_meta": {"agentLearningPanel": body.panel_private}}}
                    result = {"content": [{"type": "text", "text": encode(body)}], "isError": False}
                    # 旧协议保留文本结果；新版同时提供机器可直接读取的结构化结果。
                    if (self.protocol_version in {"2025-06-18", "2025-11-25"}
                            and isinstance(body, dict) and not isinstance(body, StatusSummary)):
                        result["structuredContent"] = body
                    if isinstance(body, _PanelResult):
                        result["_meta"] = {"agentLearningPanel": body.panel_private}
                except ProtocolError:
                    raise
                except ContractError as exc:
                    if str(exc).startswith("reconnect_required"):
                        raise ProtocolError(-32001, "reconnect_required", {"reason": "host_epoch_changed"}) from exc
                    if params["name"] in DIRECT_PANEL_TOOL_NAMES:
                        result = {"content": [], "isError": True, "_meta": {"agentLearningPanel": {
                            "visible": False, "scope": "project", "reason": str(exc)}}}
                    else:
                        result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            else:
                raise ProtocolError(-32601, "method not found")
            return {"jsonrpc": "2.0", "id": ident, "result": result}
        except ProtocolError as exc:
            return None if notification else error_response(ident, exc.code, str(exc), exc.data)
        except Exception as exc:
            return None if notification else error_response(ident, -32603, type(exc).__name__)

    def serve(self):
        try:
            self._serve()
        finally:
            self.close()

    def _serve(self):
        # MCP stdio每行一条JSON；诊断不得混进stdout协议流。
        # 从字节流按协议解码，Windows默认GBK不能用于MCP；console入口同样生效。
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        while True:
            line = stream.readline(self.MAX_MESSAGE_BYTES + 1)
            if not line:
                break
            if len(line) > self.MAX_MESSAGE_BYTES:
                # 分块排空同一超限帧，避免后半行被误解为下一条JSON，也不整行分配内存。
                newline = b"\n" if isinstance(line, bytes) else "\n"
                while not line.endswith(newline):
                    line = stream.readline(self.MAX_MESSAGE_BYTES + 1)
                    if not line:
                        break
                response = error_response(None, -32700, "message too large")
            else:
                try:
                    def invalid_constant(value):
                        raise ValueError("non-JSON numeric constant")
                    response = self.request(json.loads(line.decode("utf-8") if isinstance(line, bytes) else line,
                                                       parse_constant=invalid_constant))
                except (UnicodeError, ValueError, RecursionError):
                    response = error_response(None, -32700, "invalid JSON")
            if response is not None:
                print(encode(response), flush=True)
