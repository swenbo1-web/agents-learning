from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
import sys

from .admin import admin_batch, admin_once
from .install import install
from .mcp import Server
from .runtime import ContractError, Runtime, encode


def parser():
    p = argparse.ArgumentParser(description="Agent Learning 本地插件运行端")
    p.add_argument("--home", help="持久数据目录（默认ALP_HOME或用户数据目录）")
    p.add_argument("--workspace", default=str(Path.cwd()), help="当前项目目录")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    serve = sub.add_parser("serve")
    serve.add_argument("--role", choices=["worker", "admin"], default="worker")
    serve.add_argument("--context-handle", default=os.environ.get("ALP_CONTEXT_HANDLE"),
                       help="宿主已登记的会话句柄；也可由ALP_CONTEXT_HANDLE提供")
    bind = sub.add_parser("session-bind", help="本地受信任入口登记宿主会话，不读取账户凭证")
    bind.add_argument("--conversation", required=True)
    bind.add_argument("--agent-id")
    changed = sub.add_parser("account-changed", help="处理真实换号通知，失效旧连接但保留共享知识")
    changed.add_argument("--notification-id", help="可选通知ID，用于重复通知去重")
    task = sub.add_parser("task")
    task.add_argument("--conversation", required=True)
    task.add_argument("--goal", required=True)
    task.add_argument("--scope", help="JSON对象；省略时保留现有范围")
    task.add_argument("--expected-revision", type=int)
    task.add_argument("--expected-task-id")
    task.add_argument("--expected-write-version", type=int)
    task.add_argument("--resume", action="store_true", help="明确恢复非活动任务；不自动恢复执行许可")
    task.add_argument("--new-task", action="store_true")
    task.add_argument("--status", choices=["active", "delivered", "cancelled", "interrupted", "paused"])
    approval = sub.add_parser("execution-approve", help="本地受信入口签发当前任务的执行范围，MCP 不提供此工具")
    approval.add_argument("--conversation", required=True)
    approval.add_argument("--expected-task-id", required=True)
    approval.add_argument("--expected-write-version", required=True, type=int)
    approval.add_argument("--rules", required=True, help="执行规则 JSON 文件")
    feedback = sub.add_parser("feedback")
    feedback.add_argument("--conversation", required=True)
    feedback.add_argument("--summary", required=True)
    feedback.add_argument("--category", default="unknown")
    feedback.add_argument("--event-ref", action="append", default=[])
    feedback.add_argument("--resolution", default="")
    feedback.add_argument("--knowledge-id")
    feedback.add_argument("--knowledge-revision", type=int)
    feedback.add_argument("--feedback-kind", choices=["observation", "counterexample"])
    feedback.add_argument("--idempotency-key", help="同一次反馈重试沿用此键；实际再次发生使用新键")
    feedback.add_argument("--expected-task-id")
    feedback.add_argument("--expected-task-revision", type=int)
    sub.add_parser("problems")
    packet = sub.add_parser("packet")
    packet.add_argument("problem_id")
    packet.add_argument("--offset", type=int, default=0)
    packet.add_argument("--expected-revision", type=int)
    packet.add_argument("--max-chars", type=int)
    packet.add_argument("--section", choices=["events", "task", "target"], default="events")
    sub.add_parser("propose").add_argument("file")
    publish = sub.add_parser("publish")
    publish.add_argument("candidate_id")
    publish.add_argument("--expected-generation", type=int)
    publish.add_argument("--knowledge-id", help="更新已有知识时指定稳定ID")
    publish.add_argument("--expected-revision", type=int, help="更新已有知识时校验当前active修订")
    revoke = sub.add_parser("revoke", help="按精确修订撤销知识并保存动作回执")
    revoke.add_argument("knowledge_id")
    revoke.add_argument("--expected-revision", type=int, required=True)
    revoke.add_argument("--expected-generation", type=int)
    revoke.add_argument("--request-id")
    revoke.add_argument("--reason", default="explicit revoke")
    restore = sub.add_parser("restore", help="显式恢复末版知识；重核证据，不由候选重试自动恢复")
    restore.add_argument("knowledge_id")
    restore.add_argument("--expected-revision", type=int, required=True)
    restore.add_argument("--expected-generation", type=int)
    restore.add_argument("--request-id")
    restore.add_argument("--reason", default="explicit restore")
    restore.add_argument("--event-ref", action="append", default=[])
    actions = sub.add_parser("knowledge-actions", help="查看知识生命周期动作摘要")
    actions.add_argument("--knowledge-id")
    actions.add_argument("--limit", type=int, default=20)
    actions.add_argument("--offset", type=int, default=0)
    sub.add_parser("knowledge-action").add_argument("request_id")
    maturity = sub.add_parser("knowledge-maturity", help="查看成熟度与晋升所缺依据")
    maturity.add_argument("knowledge_id")
    maturity.add_argument("--expected-revision", type=int, required=True)
    promote = sub.add_parser("knowledge-promote", help="按确定性门槛晋升Stable；不调用模型")
    promote.add_argument("knowledge_id")
    promote.add_argument("--expected-revision", type=int, required=True)
    promote.add_argument("--request-id")
    promote.add_argument("--reason", default="explicit promotion")
    prepare = sub.add_parser("knowledge-release-prepare", help="准备当前知识的不可变发布清单")
    prepare.add_argument("--reason", default="")
    prepare.add_argument("--max-items", type=int, default=500)
    daily = sub.add_parser("knowledge-daily-release", help="有变化才准备并提交知识批次，不新增模型调用")
    daily.add_argument("--reason", default="Daily Release")
    daily.add_argument("--max-items", type=int, default=500)
    sub.add_parser("knowledge-release-commit", help="复查版本并原子提交清单").add_argument("release_id")
    releases = sub.add_parser("knowledge-releases", help="查看知识发布批次摘要")
    releases.add_argument("--limit", type=int, default=20)
    releases.add_argument("--offset", type=int, default=0)
    release = sub.add_parser("knowledge-release", help="按页读取发布清单")
    release.add_argument("release_id")
    release.add_argument("--limit", type=int, default=20)
    release.add_argument("--offset", type=int, default=0)
    rollback = sub.add_parser("knowledge-release-rollback", help="安全回退分发清单，不恢复已撤销知识")
    rollback.add_argument("release_id")
    rollback.add_argument("--expected-head", required=True)
    rollback.add_argument("--request-id")
    rollback.add_argument("--reason", default="explicit release rollback")
    sub.add_parser("doctor", help="按需检查存储和索引一致性，不更改知识")
    sub.add_parser("rebuild-index", help="从当前有效知识原子重建可派生索引")
    sub.add_parser("backup", help="使用SQLite在线备份，目标必须不存在").add_argument("destination")
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--filters", help="分类、知识用途、工具、版本、错误码、环境、验证方式的精确筛选JSON对象")
    sub.add_parser("read").add_argument("knowledge_id")
    admin = sub.add_parser("admin-once")
    admin.add_argument("--problem")
    batch = sub.add_parser("admin-batch", help="按案例数、调用数与时长运行有限审核批次")
    batch.add_argument("--dispatch-token", help="由本地Hook预留的后台派发身份")
    retry = sub.add_parser("admin-retry", help="核对失败或未知调用后明确允许一次再审，原额度记录保留")
    retry.add_argument("call_id")
    retry.add_argument("--reason", required=True)
    reviews = sub.add_parser("reviews", help="查看持久审核原因、缺证需求与候选引用")
    reviews.add_argument("--problem")
    reviews.add_argument("--limit", type=int, default=20)
    governance = sub.add_parser("governance-propose", help="从JSON文件提交治理提议，不执行")
    governance.add_argument("file")
    execute = sub.add_parser("governance-execute", help="受信入口校验并执行治理提议，保存幂等回执")
    execute.add_argument("proposal_id")
    sync = sub.add_parser("sync")
    sync.add_argument("--retry", help="先人工核对远端结果，再显式重试此知识操作ID")
    sync.add_argument("--backfill", action="store_true", help="先将现有正式知识加入同步队列，再同步一项")
    sync_list = sub.add_parser("sync-list", help="查看分发队列的安全摘要")
    sync_list.add_argument("--state")
    sync_list.add_argument("--limit", type=int, default=20)
    sync_list.add_argument("--offset", type=int, default=0)
    sub.add_parser("sync-read", help="查看单条分发的诊断与回执").add_argument("operation_id")
    sync_batch = sub.add_parser("sync-batch", help="有界处理pending分发，blocked仍待对账")
    sync_batch.add_argument("--max-items", type=int, default=20)
    sync_batch.add_argument("--max-seconds", type=float, default=30)
    export = sub.add_parser("install")
    export.add_argument("destination")
    export.add_argument("--runtime-store", help="版本化稳定运行目录，不随插件缓存清理")
    export.add_argument("--hook-mode", choices=["observe", "enforce"], help="可信Hook故障策略；省略按配置")
    return p


def main(argv=None):
    # console入口也输出UTF-8 JSON，不依赖源码脚本的初始化。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    if args.command == "install":
        try:
            print(encode(install(args.destination, runtime_store=args.runtime_store, hook_mode=args.hook_mode)))
            return 0
        except (ContractError, OSError) as exc:
            print(encode({"status": "error", "reason": str(exc)}), file=sys.stderr)
            return 1
    runtime = None
    try:
        runtime = Runtime(args.home)
        if args.command == "account-changed":
            print(encode(runtime.account_changed(notification_id=args.notification_id)))
            return 0
        if args.command in {"doctor", "rebuild-index", "backup"}:
            result = (runtime.doctor() if args.command == "doctor" else runtime.rebuild_index()
                      if args.command == "rebuild-index" else runtime.backup(args.destination))
            print(encode(result))
            return 0
        project = runtime.project(args.workspace)
        if args.command == "serve":
            Server(runtime, project, args.role, context_handle=args.context_handle).serve()
            return 0
        if args.command == "status": result = runtime.status(project)
        elif args.command == "session-bind": result = runtime.register_context(project, args.conversation, agent_id=args.agent_id)
        elif args.command == "task": result = runtime.task_update(project, args.conversation, args.goal,
                                                json.loads(args.scope) if args.scope is not None else None,
                                                args.expected_revision, args.status, args.new_task,
                                                expected_task_id=args.expected_task_id,
                                                expected_write_version=args.expected_write_version, resume=args.resume)
        elif args.command == "execution-approve": result = runtime.approve_execution(
            project, args.conversation, args.expected_task_id, args.expected_write_version,
            json.loads(Path(args.rules).read_text(encoding="utf-8-sig")), workspace=str(Path(args.workspace).resolve()))
        elif args.command == "feedback": result = runtime.feedback(project, args.conversation, args.summary,
                                                args.category, args.event_ref, args.resolution, args.knowledge_id,
                                                expected_task_id=args.expected_task_id,
                                                expected_task_revision=args.expected_task_revision,
                                                knowledge_revision=args.knowledge_revision,
                                                feedback_kind=args.feedback_kind, idempotency_key=args.idempotency_key)
        elif args.command == "problems": result = runtime.problems(project)
        elif args.command == "packet": result = runtime.review_packet(project, args.problem_id,
                                                offset=args.offset, expected_revision=args.expected_revision,
                                                max_chars=args.max_chars, section=args.section)
        elif args.command == "propose": result = runtime.submit_candidate(project, **json.loads(Path(args.file).read_text(encoding="utf-8-sig")))
        elif args.command == "publish": result = runtime.publish(project, args.candidate_id,
                                                expected_generation=args.expected_generation,
                                                knowledge_id=args.knowledge_id, expected_revision=args.expected_revision)
        elif args.command == "revoke": result = runtime.revoke(project, args.knowledge_id,
                                                expected_revision=args.expected_revision,
                                                expected_generation=args.expected_generation,
                                                request_id=args.request_id, reason=args.reason)
        elif args.command == "restore": result = runtime.restore(project, args.knowledge_id,
                                                args.expected_revision, args.expected_generation,
                                                request_id=args.request_id, reason=args.reason, evidence_refs=args.event_ref)
        elif args.command == "knowledge-actions": result = runtime.knowledge_actions(project, args.knowledge_id, args.limit, args.offset)
        elif args.command == "knowledge-action": result = runtime.knowledge_action(project, args.request_id)
        elif args.command == "knowledge-maturity": result = runtime.knowledge_maturity(project, args.knowledge_id, args.expected_revision)
        elif args.command == "knowledge-promote": result = runtime.promote_knowledge(project, args.knowledge_id,
                                                args.expected_revision, request_id=args.request_id, reason=args.reason)
        elif args.command == "knowledge-release-prepare": result = runtime.prepare_knowledge_release(project,
                                                reason=args.reason, max_items=args.max_items)
        elif args.command == "knowledge-daily-release": result = runtime.daily_knowledge_release(project,
                                                reason=args.reason, max_items=args.max_items)
        elif args.command == "knowledge-release-commit": result = runtime.commit_knowledge_release(project, args.release_id)
        elif args.command == "knowledge-releases": result = runtime.list_knowledge_releases(project, limit=args.limit, offset=args.offset)
        elif args.command == "knowledge-release": result = runtime.read_knowledge_release(project, args.release_id,
                                                limit=args.limit, offset=args.offset)
        elif args.command == "knowledge-release-rollback": result = runtime.rollback_knowledge_release(project, args.release_id,
                                                expected_head=args.expected_head, request_id=args.request_id, reason=args.reason)
        elif args.command == "search": result = runtime.search(project, args.query,
                                                filters=json.loads(args.filters) if args.filters is not None else None)
        elif args.command == "read": result = runtime.read(project, args.knowledge_id)
        elif args.command == "admin-once": result = admin_once(runtime, project, args.problem)
        elif args.command == "admin-batch": result = admin_batch(runtime, project, dispatch_token=args.dispatch_token)
        elif args.command == "admin-retry":
            from .admin_lifecycle import retry_call
            result = retry_call(runtime, project, args.call_id, args.reason)
        elif args.command == "reviews":
            from .admin_decisions import get_reviews
            result = get_reviews(runtime, project, args.problem, args.limit)
        elif args.command == "governance-propose":
            from .admin_governance import propose_governance
            result = propose_governance(runtime, project, **json.loads(Path(args.file).read_text(encoding="utf-8-sig")))
        elif args.command == "governance-execute":
            from .admin_governance import execute_governance
            result = execute_governance(runtime, project, args.proposal_id)
        elif args.command == "sync":
            if args.backfill:
                result = {"backfill": runtime.enqueue_sync(project), "sync": runtime.sync_once(project, args.retry)}
            else:
                result = runtime.sync_once(project, args.retry)
        elif args.command == "sync-list": result = runtime.sync_list(project, state=args.state, limit=args.limit, offset=args.offset)
        elif args.command == "sync-read": result = runtime.sync_read(project, args.operation_id)
        elif args.command == "sync-batch": result = runtime.sync_batch(project, max_items=args.max_items, max_seconds=args.max_seconds)
        else: raise ContractError("unknown command")
        print(encode(result))
        return 0
    except (ContractError, OSError, ValueError, sqlite3.Error) as exc:
        print(encode({"status": "error", "reason": str(exc)}), file=sys.stderr)
        return 1
    finally:
        if runtime:
            runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
