#!/usr/bin/env python3
"""固定 DSH 工具与斜杠命令执行入口；核对发布副本后执行工具或显式历史/管理命令。"""
from __future__ import annotations

import json
from pathlib import Path
import sys


MAX_INPUT_BYTES = 2 * 1024 * 1024
_HISTORY_ACTIONS = frozenset({
    "status", "jobs", "prepare", "run", "auto",
    "sources", "cases", "pause", "cancel", "skip",
})


def _emit(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _handle_learning_history(runtime, project, workspace, args):
    from agent_learning.runtime import ContractError

    action = args.get("action", "status")
    if not isinstance(action, str) or action not in _HISTORY_ACTIONS:
        raise ContractError("learning_history action must be one of: " + ", ".join(sorted(_HISTORY_ACTIONS)))
    job_id = args.get("job_id")
    if job_id is not None and (not isinstance(job_id, str) or not job_id.strip()):
        raise ContractError("job_id must be a nonempty string when supplied")
    job_id = job_id.strip() if isinstance(job_id, str) else None

    if action == "status":
        return runtime.history_status(project, job_id)
    if action == "jobs":
        return runtime.history_jobs(project, limit=int(args.get("limit", 20)))
    if action == "skip":
        return runtime.history_skip(project)
    if action == "pause":
        if not job_id:
            raise ContractError("learning_history pause requires job_id")
        return runtime.history_pause(project, job_id)
    if action == "cancel":
        if not job_id:
            raise ContractError("learning_history cancel requires job_id")
        return runtime.history_cancel(project, job_id)
    if action == "sources":
        if not job_id:
            raise ContractError("learning_history sources requires job_id")
        return runtime.history_source_report(
            project,
            job_id,
            section=args.get("section", "sources"),
            offset=int(args.get("offset", 0)),
            limit=int(args.get("limit", 20)),
        )
    if action == "cases":
        if not job_id:
            raise ContractError("learning_history cases requires job_id")
        return runtime.history_cases(
            project,
            job_id,
            offset=int(args.get("offset", 0)),
            limit=int(args.get("limit", 20)),
        )

    # prepare / run / auto 均必须显式传入 confirm=True
    if args.get("confirm") is not True:
        raise ContractError("history reading and processing require explicit user confirmation (confirm=true)")

    extra_sources = []
    for conv_file in args.get("conversation_files") or []:
        if not isinstance(conv_file, str) or not conv_file.strip():
            raise ContractError("conversation_files must contain nonempty path strings")
        extra_sources.append({"path": str(Path(conv_file).absolute()), "kind": "conversation"})
    for art_dir in args.get("artifact_dirs") or []:
        if not isinstance(art_dir, str) or not art_dir.strip():
            raise ContractError("artifact_dirs must contain nonempty path strings")
        extra_sources.append({"path": str(Path(art_dir).absolute()), "kind": "artifacts"})
    scan_options = args.get("scan_options")

    if action == "prepare":
        return runtime.history_prepare(
            project, workspace, extra_sources=extra_sources or None, scan_options=scan_options
        )

    allow_review = bool(args.get("allow_review", False))
    max_calls = int(args.get("max_calls", 2))
    max_seconds = int(args.get("max_seconds", 180))

    if action == "auto":
        prepared = runtime.history_prepare(
            project, workspace, extra_sources=extra_sources or None, scan_options=scan_options
        )
        target_job_id = prepared["job_id"]
        ran = runtime.history_run(
            project, target_job_id,
            allow_review=allow_review, max_calls=max_calls, max_seconds=max_seconds,
        )
        return {
            "mode": "auto_prepare_and_run",
            "prepared_job_id": target_job_id,
            "reused_preparation": bool(prepared.get("reused", False)),
            **ran,
        }

    # action == "run"
    if not job_id:
        status_info = runtime.history_status(project)
        latest_job = status_info.get("job")
        if latest_job and latest_job.get("state") in {"prepared", "paused"}:
            job_id = latest_job["job_id"]
        else:
            raise ContractError("learning_history run requires job_id (or use action='auto' to prepare and run)")
    return runtime.history_run(
        project, job_id,
        allow_review=allow_review, max_calls=max_calls, max_seconds=max_seconds,
    )


def _handle_slash_command(runtime, project, workspace, command_name, raw_input):
    from agent_learning.admin import admin_batch, admin_once

    tokens = (raw_input or "").strip().split()
    if tokens and tokens[0].lstrip("/").lower() == command_name.lower():
        tokens = tokens[1:]

    if command_name == "learning-status":
        status = runtime.status(project)
        onboarding = status.get("history_import", {}).get("onboarding", {}).get("state", "unoffered")
        work_mem = status.get("work_memory", {})
        counts = status.get("counts", {})
        lines = [
            f"### Agent Learning 项目状态 (`{workspace}`)",
            f"- **Project ID**: `{status['project_id']}` | **Host Scope**: `{status['host_scope']}` | **Schema**: `{status['storage_schema_version']}`",
            f"- **数据目录 (`ALP_HOME`)**: `{status['home']}`",
            f"- **正式知识 (`active`)**: `{counts.get('active', 0)}` 条 | **待审问题 (`pending_problems`)**: `{counts.get('pending_problems', 0)}` 条 | **候选 (`candidates`)**: `{counts.get('candidates', 0)}` 条",
            f"- **1.0.0 工作历史 (`work_memory`)**: `{work_mem.get('memories', 0)}` 条观察 | `{work_mem.get('file_relations', 0)}` 条文件关联 | `{work_mem.get('model_summaries', 0)}` 条 AI 工作摘要",
            f"- **项目历史吸收 (`onboarding`)**: `{onboarding}`"
            + ("（可执行 `/learning-history` 一键吸收已有项目文档与源码，或 `/learning-history skip` 跳过）" if onboarding == "unoffered" else ""),
            f"- **自动管理员 (`admin_enabled`)**: `{'开启' if status.get('admin_enabled') else '关闭'}`（模式：`{status.get('admin_routing', {}).get('policy', {}).get('model_mode', 'default')}`）",
        ]
        return {"text": "\n".join(lines), "data": status}

    if command_name == "learning-history":
        sub = tokens[0].lower() if tokens else "auto"
        if sub == "status":
            res = runtime.history_status(project, tokens[1] if len(tokens) > 1 else None)
            job = res.get("job")
            text = (
                f"### 项目历史吸收状态\n- **Onboarding**: `{res['onboarding']['state']}`\n"
                + (
                    f"- **最近批次 (`{job['job_id']}`)**: 状态 `{job['state']}` | 来源 `{job['counts']['materialized_sources']}/{job['counts']['sources']}` | 归档案例 `{job['counts']['cases']}` 条 | 候选 `{job['counts']['candidates']}` 条 | 已发布 `{job['counts']['published_cases']}` 条"
                    if job else "- 当前尚无历史吸收批次。运行 `/learning-history` 或 `/learning-history prepare` 开始。"
                )
            )
            return {"text": text, "data": res}
        if sub == "skip":
            res = runtime.history_skip(project)
            return {"text": "已将当前项目标记为跳过首次历史吸收（`onboarding=skipped`）；后续仍可随时通过 `/learning-history` 启动。", "data": res}
        if sub == "prepare":
            res = runtime.history_prepare(project, workspace)
            return {
                "text": (
                    f"### 历史来源扫描完成（未调用模型）\n"
                    f"- **批次 ID (`job_id`)**: `{res['job_id']}`\n"
                    f"- **可吸收来源数**: `{res['counts']['sources']}` 个文件（跳过 `{res['coverage']['skipped_count']}`，待处理二进制/未知 `{res['coverage']['pending_count']}`）\n"
                    f"- 下一步：执行 `/learning-history run {res['job_id']}` 完成归档。"
                ),
                "data": res,
            }
        if sub in {"run", "auto"}:
            allow_review = "--allow-review" in tokens or "--review" in tokens
            explicit_job = next((t for t in tokens[1:] if not t.startswith("-")), None)
            if sub == "auto" or not explicit_job:
                prep = runtime.history_prepare(project, workspace)
                explicit_job = prep["job_id"]
            res = runtime.history_run(project, explicit_job, allow_review=allow_review)
            job = res["job"]
            return {
                "text": (
                    f"### 项目历史吸收已完成\n"
                    f"- **批次 ID (`job_id`)**: `{job['job_id']}` | **终态**: `{job['state']}`\n"
                    f"- **已归档来源**: `{job['counts']['materialized_sources']}/{job['counts']['sources']}` 个文件\n"
                    f"- **生成历史案例 (`reported`)**: `{job['counts']['cases']}` 条（待审 `{job['counts']['pending_cases']}` 条，候选 `{job['counts']['candidates']}` 条）\n"
                    f"- 可在对话中通过 `learning_history(action=\"cases\", job_id=\"{job['job_id']}\")` 查看案例清单。"
                ),
                "data": res,
            }
        if sub == "pause" and len(tokens) > 1:
            res = runtime.history_pause(project, tokens[1])
            return {"text": f"已暂停历史批次 `{tokens[1]}`。", "data": res}
        if sub == "cancel" and len(tokens) > 1:
            res = runtime.history_cancel(project, tokens[1])
            return {"text": f"已取消历史批次 `{tokens[1]}`（已发布知识保留）。", "data": res}
        return {
            "text": "用法：`/learning-history [auto | prepare | run [job_id] | status | skip | pause <job_id> | cancel <job_id>]`"
        }

    if command_name == "learning-admin":
        sub = tokens[0].lower() if tokens else "status"
        if sub == "status":
            routing = runtime.admin_routing_status(project)
            from agent_learning.admin_control import control_status
            control = control_status(runtime, project)
            budget = control["project_usage"]
            global_usage = control["global_usage"]
            p = routing["policy"]
            return {
                "text": (
                    f"### DSH 管理员状态\n"
                    f"- **启用状态**: `{'on' if p['enabled'] else 'off'}` | **Provider**: `{p['provider']}` | **模型模式**: `{p['model_mode']}` | **模型**: `{p['model'] or '(默认/跟随会话)'}`\n"
                    f"- **路由就绪 (`routing_ready`)**: `{routing['routing_ready']}`（原因：`{routing['reason'] or '就绪'}`）\n"
                    f"- **项目今日审核尝试 / 有效结论 / 失败**: `{budget['counted_attempts']} / {budget['completed_reviews']} / {budget['failed_attempts']}`\n"
                    f"- **项目剩余额度**: `{budget['remaining_calls']}/{budget['max_calls_per_day']}` 次（项目独立，各宿主共用）\n"
                    f"- **所有项目合计尝试 / 全局保护剩余**: `{global_usage['counted_attempts']} / {global_usage['remaining_calls']}`"
                ),
                "data": {**routing, "budget": control["usage"], "project_usage": budget, "global_usage": global_usage, "admission": control["admission"]},
            }
        if sub == "off":
            policy = runtime.admin_policy(project)
            res = runtime.admin_configure(
                project, False, policy["provider"], policy["model_mode"], policy["model"],
                expected_policy_version=policy["policy_version"],
            )
            return {"text": "已关闭当前项目的 DSH 自动管理员审核。", "data": res}
        if sub == "on":
            policy = runtime.admin_policy(project)
            mode = tokens[1].lower() if len(tokens) > 1 and tokens[1].lower() in {"current", "fixed", "default"} else (
                policy["model_mode"] if policy["policy_version"] else "current"
            )
            model = tokens[2] if len(tokens) > 2 and mode == "fixed" else (policy["model"] if mode == "fixed" else "")
            res = runtime.admin_configure(
                project, True, "host", mode, model,
                expected_policy_version=policy["policy_version"],
            )
            return {
                "text": f"已开启当前项目的 DSH 管理员审核（`model_mode={res['model_mode']}`, `model={res['model'] or 'auto'}`）。",
                "data": res,
            }
        if sub == "once":
            res = admin_once(runtime, project, tokens[1] if len(tokens) > 1 else None)
            return {"text": f"单次管理员审核执行结果：`status={res.get('status')}`", "data": res}
        if sub == "batch":
            res = admin_batch(runtime, project)
            return {"text": f"批量管理员审核执行结果：`status={res.get('status')}`", "data": res}
        return {"text": "用法：`/learning-admin [status | on [current|fixed|default] [model_id] | off | once [problem_id] | batch]`"}

    return {"text": f"未知命令：{command_name}"}


def main():
    try:
        raw_bytes = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw_bytes) > MAX_INPUT_BYTES:
            _emit({"ok": False, "error": "tool input exceeded byte limit"})
            return 0
        payload = json.loads(raw_bytes.decode("utf-8-sig"))
        if not isinstance(payload, dict):
            _emit({"ok": False, "error": "tool input must be a JSON object"})
            return 0
    except (UnicodeError, ValueError, OSError, RecursionError) as exc:
        _emit({"ok": False, "error": f"invalid tool input: {exc}"})
        return 0

    plugin_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(plugin_root))
    try:
        from agent_learning.release import validate_entry
        validate_entry(__file__)
    except Exception as exc:
        _emit({"ok": False, "error": f"release verification failed: {exc}"})
        return 0

    runtime = None
    try:
        from agent_learning.mcp import Server
        from agent_learning.runtime import Runtime
        workspace = payload.get("workspace")
        if not isinstance(workspace, str) or not workspace.strip():
            _emit({"ok": False, "error": "workspace is required"})
            return 0
        if payload.get("check_registered_only") is True:
            import os
            from agent_learning.knowledge_browser import resolve_browser_project
            default_home = (Path(os.environ["LOCALAPPDATA"]) / "AgentLearning"
                            if os.environ.get("LOCALAPPDATA") else Path.home() / ".local/share/agent-learning")
            home = Path(payload.get("home") or os.environ.get("ALP_HOME") or default_home).resolve()
            db_file = home / "runtime.sqlite3"
            if not db_file.is_file():
                _emit({"ok": True, "result": {"registered": False}})
                return 0
            try:
                resolve_browser_project(db_file, workspace)
            except Exception:
                _emit({"ok": True, "result": {"registered": False}})
                return 0
        runtime = Runtime(home=payload.get("home"), host="dsh")
        project = runtime.project(workspace)

        if "slash_command" in payload:
            cmd_res = _handle_slash_command(
                runtime, project, workspace,
                str(payload["slash_command"]),
                str(payload.get("raw_input", "")),
            )
            _emit({"ok": True, "result": cmd_res})
            return 0

        if "admin_preset" in payload:
            import uuid
            from agent_learning.admin_control import apply_control, control_status
            preset_name = str(payload["admin_preset"]).lower()
            from agent_learning.admin_presets import PRESETS as presets
            if preset_name != "off" and preset_name not in presets:
                _emit({"ok": False, "error": "unsupported admin preset"})
                return 0
            st = control_status(runtime, project)
            enabled = preset_name != "off"
            limits = presets.get(preset_name) or {
                k: st["project_budget_policy"]["values"][k]
                for k in ("max_calls_per_day", "max_calls_per_batch", "max_batch_seconds", "max_run_seconds")
            }
            mode = st["policy"]["model_mode"] if st["policy"]["policy_version"] else "first"
            model = st["policy"]["model"] if mode == "fixed" else ""
            req_body = {
                "request_id": uuid.uuid4().hex,
                "expected_policy_version": st["policy"]["policy_version"],
                "expected_project_budget_version": st["project_budget_policy"]["version"],
                "expected_settings_version": st["settings_version"],
                "expected_session_epoch": st["session_epoch"],
                "expected_governance_epoch": st["governance_epoch"],
                "enabled": enabled,
                "model_mode": mode,
                "model": model,
                "project_limits": limits,
                "start": enabled,
            }
            applied = apply_control(runtime, project, req_body)
            _emit({"ok": True, "result": {"preset": preset_name, "enabled": enabled, "project_limits": limits, "applied": applied}})
            return 0

        tool_name = payload.get("tool")
        tool_args = payload.get("args")
        if not isinstance(tool_name, str) or not isinstance(tool_args, dict):
            _emit({"ok": False, "error": "tool name and object args are required"})
            return 0

        if tool_name == "learning_history":
            result = _handle_learning_history(runtime, project, workspace, tool_args)
            _emit({"ok": True, "result": result})
            return 0

        if tool_name == "learning_browser":
            action = tool_args.get("action", "status")
            if action not in {"start", "status", "stop"}:
                _emit({"ok": False, "error": "learning_browser action must be start, status or stop"})
                return 0
            _emit({
                "ok": True,
                "result": {
                    "action": action,
                    "project_id": project,
                    "workspace": workspace,
                    "read_only": True,
                    "host_managed": True,
                },
            })
            return 0

        native_session = payload.get("session_id")
        agent_id = payload.get("agent_id")
        if "context_handle" not in tool_args and isinstance(native_session, str) and native_session.strip():
            conversation = runtime.host_conversation(native_session.strip())
            access = runtime.register_context(project, conversation, agent_id=agent_id)
            tool_args = {**tool_args, "context_handle": access["context_handle"]}
        server = Server(runtime, project, "worker", workspace=workspace)
        result = server.call(tool_name, tool_args)
        _emit({"ok": True, "result": result})
    except Exception as exc:
        _emit({"ok": False, "error": str(exc)})
    finally:
        if runtime is not None:
            runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
