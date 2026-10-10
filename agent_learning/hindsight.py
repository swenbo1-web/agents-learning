"""Hindsight 的可选 HTTP 适配；发布与检索有效性仍由本地 Runtime 决定。

合同来源：https://hindsight.vectorize.io/developer/api/retain
及 https://hindsight.vectorize.io/developer/api/recall 。请求字段另核对官方
https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/static/openapi.json 。

目标 bank 必须由管理员预先配置命名 chunks 策略并关闭自动 consolidation。
首个 retain 只读核对一次配置，不修改远端设置；每个 HTTP 操作不重试、不跟随
重定向。同步 operation_id 仅作 metadata，不声称具有异步 API 的幂等保证。
调用者提供不可变修订 document_id，并负责持久队列与重试决定。
"""

from __future__ import annotations

import http.client
import json
import math
import os
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


_MAX_RESPONSE_BYTES = 1024 * 1024


class HindsightError(RuntimeError):
    """可由本地 Runtime 记录的安全错误；不包含凭证或远端原始响应。"""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "request_failed",
        status_code: int | None = None,
        retryable: bool = False,
        write_outcome: str = "not_sent",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.retryable = retryable
        # 未尝试写请求可直接修配置；尝试后必须先核对远端结果。
        self.write_outcome = write_outcome


@contextmanager
def _safe_errors(kind: str, write_outcome: str = "not_sent"):
    """公共边界不透传可能包含凭证、输入或远端响应的底层异常。"""
    try:
        yield
    except HindsightError:
        raise
    except Exception:
        raise HindsightError(
            "Hindsight 本地配置、请求或响应处理失败。",
            kind=kind,
            write_outcome=write_outcome,
        ) from None


class _NoRedirect(HTTPRedirectHandler):
    # 跟随重定向会增加隐式请求，也可能把 Authorization 带往另一服务。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HindsightClient:
    """固定配置 bank 的适配器；不提供任意 bank、Reflect 或发布接口。"""

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        with _safe_errors("configuration"):
            self._configure(config)

    def _configure(self, config: Mapping[str, Any] | None) -> None:
        if config is not None and not isinstance(config, Mapping):
            raise HindsightError("Hindsight 配置必须是 Mapping。", kind="configuration")
        values = dict(config or {})
        self._url = self._optional_text(values.get("url"), "url").rstrip("/")
        self._bank_id = self._optional_text(values.get("bank_id"), "bank_id")
        self._api_key_env = self._optional_text(values.get("api_key_env"), "api_key_env")
        self._strategy = self._optional_text(values.get("strategy", "alp_chunks"), "strategy")
        self._chunks_verified = False
        self._canonical_bank_id: str | None = None
        self._opener = build_opener(_NoRedirect())
        timeout = values.get("timeout_seconds", 10.0)
        try:
            if isinstance(timeout, bool):
                raise ValueError
            self._timeout = float(timeout)
        except (TypeError, ValueError, OverflowError):
            raise HindsightError("Hindsight timeout_seconds 必须是正数。", kind="configuration") from None
        if not math.isfinite(self._timeout) or self._timeout <= 0:
            raise HindsightError("Hindsight timeout_seconds 必须是有限正数。", kind="configuration")
        if self._url:
            try:
                parsed = urlsplit(self._url)
                valid = (
                    parsed.scheme in {"http", "https"}
                    and bool(parsed.hostname)
                    and parsed.username is None
                    and parsed.password is None
                    and not parsed.query
                    and not parsed.fragment
                    and parsed.port != 0
                )
            except ValueError:
                valid = False
            if not valid:
                raise HindsightError(
                    "Hindsight URL 必须为不含凭证、查询或片段的 HTTP(S) 服务地址。",
                    kind="configuration",
                )

    @staticmethod
    def _optional_text(value: Any, field: str) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise HindsightError(f"Hindsight {field} 必须是字符串。", kind="configuration")
        return value.strip()

    @staticmethod
    def _required_text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise HindsightError(f"Hindsight {field} 不能为空。", kind="invalid_argument")
        return value

    @property
    def configured(self) -> bool:
        """仅代表地址与 bank 已显式设置，不代表连接、凭证或远端策略已核实。"""
        return bool(self._url and self._bank_id)

    def _request(self, method: str, route: str, payload: dict | None = None) -> dict:
        # 只有 retain POST 写入知识；recall 虽为 POST，仍是只读调用。
        writing = method == "POST" and route == "memories"
        with _safe_errors("invalid_argument"):
            if not self.configured:
                raise HindsightError("Hindsight 未配置 URL 或 bank_id；保留本地功能。", kind="unconfigured")
            headers = {"Accept": "application/json"}
            if self._api_key_env:
                api_key = os.environ.get(self._api_key_env, "").strip()
                if not api_key:
                    raise HindsightError("Hindsight 凭证环境变量未设置。", kind="missing_credentials")
                # Bearer 凭证为 ASCII；提前拒绝控制字符，避免标准库错误回显密钥。
                if not api_key.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in api_key):
                    raise HindsightError("Hindsight 凭证包含非法头字符。", kind="invalid_credentials")
                headers["Authorization"] = "Bearer " + api_key
            data = None
            if payload is not None:
                data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
                headers["Content-Type"] = "application/json; charset=utf-8"
            endpoint = self._url + "/v1/default/banks/" + quote(self._bank_id, safe="") + "/" + route
            request = Request(endpoint, data=data, headers=headers, method=method)
        write_outcome = "unknown" if writing else "not_sent"
        with _safe_errors("request_failed", write_outcome):
            return self._perform_request(request, write_outcome)

    def _perform_request(self, request: Request, write_outcome: str) -> dict:
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            labels = {
                401: "身份验证失败",
                403: "访问被拒绝",
                404: "bank 或接口不存在",
                409: "远端记录冲突",
                422: "请求合同或命名策略被拒绝",
                429: "远端请求限额",
            }
            label = labels.get(status, "HTTP 请求失败")
            # 远端错误可能回显输入或 Authorization，不把响应正文放进错误/日志。
            raise HindsightError(
                f"Hindsight {label}（HTTP {status}）。",
                kind="http_error",
                status_code=status,
                retryable=status in {408, 425, 429} or 500 <= status < 600,
                write_outcome=write_outcome,
            ) from None
        except (URLError, TimeoutError, OSError, http.client.HTTPException):
            raise HindsightError(
                "Hindsight 连接失败或请求超时；" + (
                    "远端写入结果尚未确认。" if write_outcome == "unknown" else "尚未发出知识写请求。"
                ),
                kind="transport_error",
                retryable=True,
                write_outcome=write_outcome,
            ) from None
        except Exception:
            raise HindsightError(
                "Hindsight 请求处理失败。",
                kind="request_failed",
                write_outcome=write_outcome,
            ) from None
        with _safe_errors("invalid_response", write_outcome):
            if len(body) > _MAX_RESPONSE_BYTES:
                raise HindsightError("Hindsight 响应超过本地大小限制。", kind="invalid_response",
                                     write_outcome=write_outcome)
            try:
                result = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise HindsightError("Hindsight 未返回有效 UTF-8 JSON。", kind="invalid_response",
                                     write_outcome=write_outcome) from None
            if not isinstance(result, dict):
                raise HindsightError("Hindsight 响应必须是 JSON 对象。", kind="invalid_response",
                                     write_outcome=write_outcome)
        return result

    def _ensure_chunks_strategy(self) -> None:
        if self._chunks_verified:
            return
        if not self._strategy:
            raise HindsightError("Hindsight 命名 chunks 策略不能为空。", kind="configuration")
        response = self._request("GET", "config")
        resolved = response.get("config")
        if not isinstance(resolved, dict):
            raise HindsightError("Hindsight 未返回可核实的 bank 配置。", kind="invalid_response")
        strategies = resolved.get("retain_strategies")
        strategy = strategies.get(self._strategy) if isinstance(strategies, dict) else None
        if not isinstance(strategy, dict):
            raise HindsightError(
                "Hindsight bank 尚未配置指定的命名 chunks 策略；请由管理员配置。",
                kind="unsafe_strategy",
            )
        mode = strategy.get("retain_extraction_mode", resolved.get("retain_extraction_mode"))
        if mode != "chunks":
            raise HindsightError(
                "Hindsight 指定策略的有效 retain_extraction_mode 不是 chunks。",
                kind="unsafe_strategy",
            )
        auto_consolidation = strategy.get("enable_auto_consolidation", resolved.get("enable_auto_consolidation"))
        observations = strategy.get("enable_observations", resolved.get("enable_observations"))
        if auto_consolidation is not False and observations is not False:
            raise HindsightError(
                "Hindsight bank 必须关闭自动 consolidation 或 observations 后才可同步。",
                kind="unsafe_strategy",
            )
        bank_id = response.get("bank_id")
        if not isinstance(bank_id, str) or not bank_id:
            raise HindsightError("Hindsight 配置响应缺少 bank 身份。", kind="invalid_response")
        self._canonical_bank_id = bank_id
        self._chunks_verified = True

    def retain(
        self,
        document_id: str,
        content: str,
        project_id: str,
        revision: str | int,
        operation_id: str,
    ) -> dict:
        """同步审核后的不可变修订；成功响应也不是本地发布状态。"""
        with _safe_errors("invalid_argument"):
            self._required_text(document_id, "document_id")
            self._required_text(content, "content")
            self._required_text(project_id, "project_id")
            self._required_text(operation_id, "operation_id")
            if isinstance(revision, bool) or not isinstance(revision, (str, int)) or not str(revision).strip():
                raise HindsightError("Hindsight revision 必须是非空字符串或整数。", kind="invalid_argument")
            self._ensure_chunks_strategy()
            payload = {
                "items": [
                    {
                        "document_id": document_id,
                        "content": content,
                        "strategy": self._strategy,
                        "update_mode": "replace",
                        "timestamp": "unset",
                        "tags": ["project:" + project_id],
                        "metadata": {
                            "project_id": project_id,
                            "revision": str(revision),
                            "operation_id": operation_id,
                        },
                    }
                ],
                "async": False,
            }
        response = self._request("POST", "memories", payload)
        with _safe_errors("unconfirmed_write", "unknown"):
            return self._confirm_retain(response)

    def _confirm_retain(self, response: dict) -> dict:
        if response.get("success") is not True or response.get("async") is not False:
            raise HindsightError("Hindsight 未确认同步 retain 已成功完成。", kind="unconfirmed_write",
                                 write_outcome="unknown")
        if response.get("bank_id") != self._canonical_bank_id:
            raise HindsightError("Hindsight retain 确认的 bank 身份不一致。", kind="unconfirmed_write",
                                 write_outcome="unknown")
        count = response.get("items_count")
        if isinstance(count, bool) or not isinstance(count, int) or count != 1:
            raise HindsightError("Hindsight retain 未确认唯一条目已处理。", kind="unconfirmed_write",
                                 write_outcome="unknown")
        return response

    def recall(self, query: str, project_id: str, limit: int = 5) -> dict:
        """可选远端召回；调用方仍须核对本地 manifest、撤销和来源。"""
        with _safe_errors("invalid_argument"):
            return self._recall(query, project_id, limit)

    def _recall(self, query: str, project_id: str, limit: int) -> dict:
        self._required_text(query, "query")
        self._required_text(project_id, "project_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise HindsightError("Hindsight limit 必须是 1～100 的整数。", kind="invalid_argument")
        response = self._request(
            "POST",
            "memories/recall",
            {
                "query": query,
                "budget": "low",
                "max_tokens": min(4096, max(512, limit * 512)),
                "tags": ["project:" + project_id],
                "tags_match": "all_strict",
                "types": ["world", "experience"],
                "include": {"entities": None, "chunks": None, "source_facts": None},
            },
        )
        results = response.get("results")
        if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
            raise HindsightError("Hindsight recall 响应缺少有效 results 列表。", kind="invalid_response")
        result = dict(response)
        result["results"] = results[:limit]
        # 原始 HTTP 合同按 token 截断；不能据此声称检索已穷尽。
        result["client_truncated"] = len(results) > limit
        return result
