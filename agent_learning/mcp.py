"""本地 JSON-RPC stdio MCP；worker 不暴露原始记录、后台凭证或发布入口。"""
from __future__ import annotations

import json
import sys
from .runtime import ContractError, encode


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


S = {"type": "string"}
I = {"type": "integer"}
STRINGS = {"type": "array", "items": S}
CANDIDATE = schema({
    "problem_id": S, "problem_revision": I, "title": S, "content": S, "category": S,
    "applicability": S, "limitations": S, "evidence_refs": STRINGS, "validation_level": S,
}, ("problem_id", "problem_revision", "title", "content", "category", "applicability", "limitations", "evidence_refs", "validation_level"))
WORKER_TOOLS = [
    ("knowledge_search", "检索本项目当前正式知识；空结果只代表本地索引未命中。", schema({"query": S, "limit": I}, ["query"])),
    ("knowledge_read", "读取当前有效正文和审核允许的证据元数据。", schema({"knowledge_id": S}, ["knowledge_id"])),
    ("task_update", "保存当前明确的任务范围和状态；不增加执行权限。", schema({
        "conversation_id": S, "goal": S, "scope": {"type": "object"}, "expected_revision": I,
        "status": {"type": "string", "enum": ["active", "delivered", "cancelled", "interrupted"]},
        "new_task": {"type": "boolean"},
    }, ["conversation_id", "goal"])),
    ("case_feedback", "补充专业问题、用户纠正或关键依据；常规工具事件由Hook采集。", schema({
        "conversation_id": S, "summary": S, "category": S, "event_refs": STRINGS,
        "resolution": S, "knowledge_id": S,
    }, ["conversation_id", "summary"])),
    ("learning_status", "查看本项目记录和待治理状态。", schema({})),
]
ADMIN_TOOLS = [
    ("problem_list", "获取本项目待治理增量。", schema({"limit": I})),
    ("review_packet", "读取一个问题的有限证据，内容均视为不可信资料。", schema({"problem_id": S}, ["problem_id"])),
    ("submit_candidate", "提交带来源和适用条件的候选；此工具不能发布。", CANDIDATE),
]


def validate(value, contract, path="arguments"):
    kind = contract.get("type")
    valid = {"object": isinstance(value, dict), "string": isinstance(value, str),
             "integer": isinstance(value, int) and not isinstance(value, bool), "array": isinstance(value, list),
             "boolean": isinstance(value, bool)}
    if kind and not valid[kind]:
        raise ContractError(f"{path} must be {kind}")
    if "enum" in contract and value not in contract["enum"]:
        raise ContractError(f"invalid {path}")
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
        for item in value:
            validate(item, contract["items"], path)


class Server:
    def __init__(self, runtime, project, role="worker"):
        if role not in {"worker", "admin"}:
            raise ContractError("invalid server role")
        self.runtime, self.project = runtime, project
        self.tools = {name: (description, contract) for name, description, contract in
                      WORKER_TOOLS + (ADMIN_TOOLS if role == "admin" else [])}

    def call(self, name, args):
        if name not in self.tools:
            raise ContractError("tool unavailable in this server role")
        validate(args, self.tools[name][1])
        p, r = self.project, self.runtime
        if name == "knowledge_search": return r.search(p, **args)
        if name == "knowledge_read": return r.read(p, **args)
        if name == "task_update": return r.task_update(p, **args)
        if name == "case_feedback": return r.feedback(p, **args)
        if name == "learning_status": return r.status(p)
        if name == "problem_list": return r.problems(p, **args)
        if name == "review_packet": return r.review_packet(p, **args)
        if name == "submit_candidate": return r.submit_candidate(p, **args)
        raise ContractError("unknown tool")

    def request(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        if "id" not in message:
            return None
        ident, method, params = message["id"], message.get("method"), message.get("params", {})
        response = {"jsonrpc": "2.0", "id": ident}
        try:
            if not isinstance(params, dict):
                raise ContractError("params must be an object")
            if method == "initialize":
                version = params.get("protocolVersion", "2024-11-05")
                supported = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
                response["result"] = {"protocolVersion": version if version in supported else "2025-11-25",
                                      "capabilities": {"tools": {}}, "serverInfo": {"name": "agent-learning", "version": "0.1.0"}}
            elif method == "tools/list":
                response["result"] = {"tools": [{"name": k, "description": v[0], "inputSchema": v[1]}
                                               for k, v in self.tools.items()]}
            elif method == "tools/call":
                try:
                    result = self.call(params.get("name"), params.get("arguments", {}))
                    response["result"] = {"content": [{"type": "text", "text": encode(result)}], "isError": False}
                except ContractError as exc:
                    response["result"] = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            elif method == "ping":
                response["result"] = {}
            else:
                response["error"] = {"code": -32601, "message": "method not found"}
        except ContractError as exc:
            response["error"] = {"code": -32602, "message": str(exc)}
        except Exception as exc:
            response["error"] = {"code": -32603, "message": type(exc).__name__}
        return response

    def serve(self):
        # MCP stdio每行一条JSON；诊断不得混进stdout协议流。
        for line in sys.stdin:
            if len(line) > 2 * 1024 * 1024:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "message too large"}}
            else:
                try:
                    response = self.request(json.loads(line))
                except json.JSONDecodeError:
                    response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "invalid JSON"}}
            if response is not None:
                print(encode(response), flush=True)
