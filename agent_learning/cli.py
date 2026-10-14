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
    p.add_argument("--workspace", help="当前项目目录；Antigravity首次安装必须显式指定")
    p.add_argument("--host", choices=["codex", "antigravity", "dsh"], default="codex", help="宿主会话与安装配置")
    p.add_argument("--host-instance", default="default", help="受信Runtime命令的宿主实例；安装导出当前固定为default")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    updates = sub.add_parser("update-check", help="检查 GitHub Release 版本，只读取公开元数据，不安装代码")
    updates.add_argument("--force", action="store_true", help="跳过常规检查间隔；仍遵守服务端限流")
    updates.add_argument("--reservation", help=argparse.SUPPRESS)
    sub.add_parser("update-status", help="只查看缓存的插件版本状态，不访问网络或知识数据库")
    sub.add_parser("update-notice", help="只查看版本提醒状态，不领取通知或访问知识数据库")
    choice = sub.add_parser("update-choice", help="明确设置稍后提醒、忽略本版或重新提醒；不会执行更新")
    choice.add_argument("choice", choices=["snooze", "ignore", "reset"])
    choice.add_argument("--version", help="选择缓存中的准确新版本")
    choice.add_argument("--snooze-seconds", type=int, default=86400)
    history = sub.add_parser("history-prepare", help="用户显式选择项目历史，读取支持文本并冻结来源；不调用模型")
    history.add_argument("--confirm", action="store_true", help="确认读取明确工作区及选择的额外来源")
    history.add_argument("--conversation-file", action="append", default=[], help="明确选择的对话导出或受支持原生记录")
    history.add_argument("--artifact-dir", action="append", default=[], help="明确选择的项目外 Agent 产物目录")
    history.add_argument("--scan-options", help="扫描上限 JSON 文件；省略使用有限默认值")
    run = sub.add_parser("history-run", help="用户启动或明确续做历史整理；不自动发布知识")
    run.add_argument("job_id")
    run.add_argument("--confirm", action="store_true", help="确认本次有限归档或整理")
    run.add_argument("--allow-review", action="store_true", help="允许已配置管理员执行器调用模型；省略只归档")
    run.add_argument("--max-calls", type=int, default=2)
    run.add_argument("--max-seconds", type=int, default=180)
    state = sub.add_parser("history-status", help="查看来源覆盖、审阅进度和用量，不启动吸收")
    state.add_argument("--job-id")
    sub.add_parser("history-jobs", help="列出当前项目的有限历史任务摘要")
    sources = sub.add_parser("history-sources", help="分页查看完整来源、未解析及排除清单，不读取原始正文")
    sources.add_argument("job_id")
    sources.add_argument("--section", choices=["sources", "pending", "skipped"], default="sources")
    sources.add_argument("--offset", type=int, default=0)
    sources.add_argument("--limit", type=int, default=20)
    cases = sub.add_parser("history-cases", help="分页定位当前历史批次的案例，避免被旧案例列表遮挡")
    cases.add_argument("job_id")
    cases.add_argument("--offset", type=int, default=0)
    cases.add_argument("--limit", type=int, default=20)
    for name in ("history-cancel", "history-pause"):
        command = sub.add_parser(name, help="停止后续历史整理与回写，保留已发布知识")
        command.add_argument("job_id")
    sub.add_parser("history-skip", help="记录该项目暂不吸收历史；以后仍可明确启动")
    browser = sub.add_parser("browser", help="启动本机知识浏览器；--review 明确开启候选审批")
    browser.add_argument("--port", type=int, default=0)
    browser.add_argument("--open", action="store_true", help="在默认浏览器打开页面")
    browser.add_argument("--project-id", help="选择已有项目；省略按已登记workspace解析")
    browser.add_argument("--review", action="store_true", help="开启候选预览与明确批准、拒绝、暂缓；不自动批准")
    browser.add_argument("--language", choices=["auto", "zh-CN", "en"], default="auto",
                         help="界面语言；auto 跟随浏览器，知识与证据保持原文")
    sub.add_parser("semantic-status", help="查看可选本地向量索引状态，不调用模型")
    semantic = sub.add_parser("semantic-refresh", help="显式调用已配置的本地嵌入模型，增量刷新派生索引")
    semantic.add_argument("--limit", type=int, default=100)
    semantic.add_argument("--rebuild", action="store_true", help="清除此项目当前配置的旧向量后重建")
    protocol = sub.add_parser("evaluation-protocol", help="从评估说明冻结任务、模型、环境与两组执行顺序")
    protocol.add_argument("--spec", required=True)
    protocol.add_argument("--output", required=True)
    evaluation = sub.add_parser("evaluation-run", help="显式运行用户提供的评估adapter；该命令可能调用adapter中的模型")
    evaluation.add_argument("--protocol", required=True)
    evaluation.add_argument("--adapter", required=True)
    evaluation.add_argument("--output", required=True)
    evaluation.add_argument("--timeout-seconds", type=float, default=300)
    report = sub.add_parser("evaluation-report", help="检查成对任务记录并汇总时间、验收与完整用量")
    report.add_argument("--protocol", required=True)
    report.add_argument("--records", required=True)
    report.add_argument("--output")
    effects = sub.add_parser("learning-effects", help="按时间窗查看声明采用与实际工具观察，不推断净节省")
    effects.add_argument("--days", type=int, default=30)
    use = sub.add_parser("knowledge-use", help="记录一次知识采用声明，不增加执行权限")
    use.add_argument("--context-handle", required=True)
    use.add_argument("--knowledge-id", required=True)
    use.add_argument("--knowledge-revision", required=True, type=int)
    use.add_argument("--expected-task-id", required=True)
    use.add_argument("--expected-task-revision", required=True, type=int)
    use.add_argument("--expected-write-version", required=True, type=int)
    use.add_argument("--operation-id")
    use.add_argument("--idempotency-key")
    for name in ("scheduler-status", "scheduler-tick", "scheduler-run"):
        scheduled = sub.add_parser(name, help="持久调度的状态、有界单轮或有界续跑；默认关闭")
        scheduled.add_argument("--all", action="store_true", help="处理同一数据目录已登记项目")
        if name != "scheduler-status":
            scheduled.add_argument("--max-seconds", type=float)
        if name == "scheduler-tick":
            scheduled.add_argument("--max-jobs", type=int)
        if name == "scheduler-run":
            scheduled.add_argument("--worker-token", help=argparse.SUPPRESS)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", choices=["codex", "antigravity", "dsh"], default=argparse.SUPPRESS)
    serve.add_argument("--role", choices=["worker", "admin"], default="worker")
    serve.add_argument("--context-handle", help="受信显式绑定句柄；Codex可使用ALP_CONTEXT_HANDLE，Antigravity不继承环境句柄")
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
    work_search = sub.add_parser("work-search", help="检索本项目已观察的工作历史索引，不扫描文件或调用模型")
    work_search.add_argument("query", nargs="?", default="")
    work_search.add_argument("--file-path")
    work_search.add_argument("--limit", type=int, default=5)
    work_search.add_argument("--max-chars", type=int, default=3000)
    work_read = sub.add_parser("work-read", help="分页读取已保存的工作摘要或可见证据投影，不恢复截断原文")
    work_read.add_argument("memory_id")
    work_read.add_argument("--expected-revision", type=int)
    work_read.add_argument("--offset", type=int, default=0)
    work_read.add_argument("--max-chars", type=int, default=6000)
    work_read.add_argument("--section", choices=["summary", "evidence"], default="summary")
    work_rebuild = sub.add_parser("work-rebuild", help="显式为已有事件及工作摘要有界重建索引；不扫描项目或宿主对话，不调用模型")
    work_rebuild.add_argument("--cursor", help="沿用前页next_cursor")
    work_rebuild.add_argument("--limit", type=int, default=100)
    admin = sub.add_parser("admin-once")
    admin.add_argument("--problem")
    batch = sub.add_parser("admin-batch", help="按案例数、调用数与时长运行有限审核批次")
    batch.add_argument("--dispatch-token", help="由本地Hook预留的后台派发身份")
    configure = sub.add_parser("admin-configure", help="明确开启或关闭当前项目/宿主的自动知识审核；不自动发布")
    configure.add_argument("--enabled", choices=["on", "off"], required=True)
    configure.add_argument("--provider", choices=["host", "codex", "antigravity", "dsh"], help="首次默认 host，后续省略保留")
    configure.add_argument("--model-mode", choices=["current", "default", "fixed"], help="首次默认 current，后续省略保留")
    configure.add_argument("--model", help="fixed 模式的模型标识")
    configure.add_argument("--command-file", help="可选 JSON 可执行参数数组，不包含令牌或模型选项")
    configure.add_argument("--expected-policy-version", type=int)
    admin_status = sub.add_parser("admin-status", help="检查当前项目/宿主审核设置与预算，不调用模型")
    admin_status.add_argument("--conversation", help="可选宿主原生会话ID，用于核对当前模型")
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
    export.add_argument("--host", choices=["codex", "antigravity", "dsh"], default=argparse.SUPPRESS)
    export.add_argument("destination")
    export.add_argument("--runtime-store", help="版本化稳定运行目录，不随插件缓存清理")
    export.add_argument("--hook-mode", choices=["observe", "enforce"], help="可信Hook故障策略；省略按配置")
    export.add_argument("--upgrade", action="store_true", help="验证并覆盖已有插件导出；保留旧目录备份")
    export.add_argument("--expected-release-id", help="覆盖时校验旧安装的固定运行版本身份")
    native_hook = sub.add_parser("antigravity-hook", help="接收固定安装工作区的Antigravity原生事件")
    native_hook.add_argument("--event", required=True, choices=["PreInvocation", "PreToolUse", "PostToolUse", "Stop"])
    native_hook.add_argument("--hook-mode", choices=["observe", "enforce"])
    reset = sub.add_parser("antigravity-session-reset", help="受信入口重置无法辨别的原生轨迹；暂停任务并失效旧句柄与许可")
    reset.add_argument("--conversation", required=True, help="原生conversationId，不含宿主前缀")
    dsh_hook = sub.add_parser("dsh-hook", help="接收DSH原生事件")
    dsh_hook.add_argument("--event", required=True, choices=[
        "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
        "Stop", "Interrupt", "SubagentStart", "SubagentStop",
    ])
    dsh_hook.add_argument("--hook-mode", choices=["observe", "enforce"])
    dsh_reset = sub.add_parser("dsh-session-reset", help="受信入口重置指定DSH会话轨迹；暂停任务并失效旧句柄与许可")
    dsh_reset.add_argument("--conversation", required=True, help="原生DSH sessionId，不含宿主前缀")
    return p


def main(argv=None):
    # console入口也输出UTF-8 JSON，不依赖源码脚本的初始化。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    if args.command == "install":
        try:
            if args.host_instance != "default":
                raise ContractError("installation exports currently require host_instance=default")
            print(encode(install(args.destination, runtime_store=args.runtime_store, hook_mode=args.hook_mode,
                                 upgrade=args.upgrade, expected_release_id=args.expected_release_id,
                                 host=args.host, **({"workspace": args.workspace, "home": args.home}
                                                   if args.host in {"antigravity", "dsh"} else {}))))
            return 0
        except (ContractError, OSError) as exc:
            print(encode({"status": "error", "reason": str(exc)}), file=sys.stderr)
            return 1
    runtime = None
    try:
        if args.command in {"admin-configure", "work-rebuild"} and not args.workspace:
            raise ContractError(f"{args.command} requires an explicit --workspace")
        if args.command.startswith("history-"):
            if not args.workspace:
                raise ContractError("history commands require an explicit --workspace; installation cwd is not a project choice")
            if args.command in {"history-prepare", "history-run"} and not args.confirm:
                raise ContractError("history reading and processing require explicit user selection (--confirm)")
        args.workspace = args.workspace or str(Path.cwd())
        if args.command in {"update-check", "update-status", "update-notice", "update-choice"}:
            from .software_updates import (load_config, check_updates, update_status,
                                           notification_status, update_notice_choice)
            default_home = (Path(os.environ["LOCALAPPDATA"]) / "AgentLearning"
                            if os.environ.get("LOCALAPPDATA") else Path.home() / ".local/share/agent-learning")
            home = Path(args.home or os.environ.get("ALP_HOME") or default_home).resolve()
            settings = load_config(home)
            if args.command == "update-status":
                result = update_status(home, settings)
            elif args.command == "update-notice":
                result = notification_status(home, settings)
            elif args.command == "update-choice":
                result = update_notice_choice(home, settings, args.choice, args.version,
                                              snooze_seconds=args.snooze_seconds)
            else:
                result = check_updates(home, settings, force=args.force, reservation=args.reservation)
            print(encode(result))
            return 0
        if args.command.startswith("evaluation-"):
            from .task_evaluation import write_protocol, run_protocol, report_files
            if args.command == "evaluation-protocol":
                result = write_protocol(args.spec, args.output)
            elif args.command == "evaluation-run":
                result = run_protocol(args.protocol, args.adapter, args.output,
                                      timeout_seconds=args.timeout_seconds)
            else:
                result = report_files(args.protocol, args.records, args.output)
            print(encode(result))
            return 0
        if args.command == "browser":
            from .knowledge_browser import make_browser_server, resolve_browser_project
            default_home = (Path(os.environ["LOCALAPPDATA"]) / "AgentLearning"
                            if os.environ.get("LOCALAPPDATA") else Path.home() / ".local/share/agent-learning")
            home = Path(args.home or os.environ.get("ALP_HOME") or default_home).resolve()
            database = home / "runtime.sqlite3"
            project = resolve_browser_project(database, args.workspace, args.project_id)
            browser_server = make_browser_server(database, project, port=args.port,
                review=args.review, language=args.language, runtime_host=args.host,
                runtime_instance=args.host_instance)
            try:
                connection = {"status": "serving", "project": project, "url": browser_server.url,
                              "access_token": browser_server.access_token,
                              "read_only": not args.review, "language": args.language, "stop": "Ctrl+C"}
                if args.review:
                    connection.update(review_token=browser_server.review_token,
                                      review_url=browser_server.browser_url)
                print(encode(connection), flush=True)
                if args.open:
                    import webbrowser
                    webbrowser.open(browser_server.browser_url)
                try:
                    browser_server.serve_forever(poll_interval=0.25)
                except KeyboardInterrupt:
                    pass
            finally:
                browser_server.server_close()
            return 0
        if args.command in {"antigravity-hook", "antigravity-session-reset"}:
            args.host = "antigravity"
        if args.command in {"dsh-hook", "dsh-session-reset"}:
            args.host = "dsh"
        runtime = Runtime(args.home, host=args.host, host_instance=args.host_instance)
        if args.command == "antigravity-session-reset":
            from .antigravity import reset_session
            print(encode(reset_session(runtime, args.workspace, args.conversation)))
            return 0
        if args.command == "antigravity-hook":
            from .antigravity import AntigravityAdapter
            data = sys.stdin.buffer.read(Server.MAX_MESSAGE_BYTES + 1)
            if len(data) > Server.MAX_MESSAGE_BYTES:
                raise ContractError("hook input exceeds byte limit")
            print(encode(AntigravityAdapter(runtime, args.workspace, args.hook_mode).handle(
                args.event, json.loads(data.decode("utf-8-sig")))))
            return 0
        if args.command == "dsh-session-reset":
            from .dsh import reset_session as reset_dsh_session
            print(encode(reset_dsh_session(runtime, args.workspace, args.conversation)))
            return 0
        if args.command == "dsh-hook":
            from .dsh import DshAdapter
            data = sys.stdin.buffer.read(Server.MAX_MESSAGE_BYTES + 1)
            if len(data) > Server.MAX_MESSAGE_BYTES:
                raise ContractError("hook input exceeds byte limit")
            print(encode(DshAdapter(runtime, args.workspace, args.hook_mode).handle(
                args.event, json.loads(data.decode("utf-8-sig")))))
            return 0
        if getattr(args, "conversation", None) is not None:
            args.conversation = runtime.host_conversation(args.conversation)
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
            handle = args.context_handle
            if handle is None and args.host == "codex":
                handle = os.environ.get("ALP_CONTEXT_HANDLE")
            Server(runtime, project, args.role, context_handle=handle, workspace=args.workspace).serve()
            return 0
        if args.command == "status": result = runtime.status(project)
        elif args.command == "history-prepare":
            options = json.loads(Path(args.scan_options).read_text(encoding="utf-8-sig")) if args.scan_options else None
            sources = ([{"path": str(Path(path).absolute()), "kind": "conversation"} for path in args.conversation_file]
                       + [{"path": str(Path(path).absolute()), "kind": "artifacts"} for path in args.artifact_dir])
            result = runtime.history_prepare(project, args.workspace, extra_sources=sources, scan_options=options)
        elif args.command == "history-run": result = runtime.history_run(
            project, args.job_id, allow_review=args.allow_review, max_calls=args.max_calls, max_seconds=args.max_seconds)
        elif args.command == "history-status": result = runtime.history_status(project, args.job_id)
        elif args.command == "history-jobs": result = runtime.history_jobs(project)
        elif args.command == "history-sources": result = runtime.history_source_report(
            project, args.job_id, section=args.section, offset=args.offset, limit=args.limit)
        elif args.command == "history-cases": result = runtime.history_cases(
            project, args.job_id, offset=args.offset, limit=args.limit)
        elif args.command == "history-cancel": result = runtime.history_cancel(project, args.job_id)
        elif args.command == "history-pause": result = runtime.history_pause(project, args.job_id)
        elif args.command == "history-skip": result = runtime.history_skip(project)
        elif args.command == "semantic-status": result = runtime.semantic_status(project)
        elif args.command == "semantic-refresh": result = runtime.semantic_refresh(project, limit=args.limit, rebuild=args.rebuild)
        elif args.command == "learning-effects": result = runtime.learning_effects(project, days=args.days)
        elif args.command == "knowledge-use": result = runtime.knowledge_use(
            project, args.context_handle, args.knowledge_id, args.knowledge_revision,
            args.expected_task_id, args.expected_task_revision, args.expected_write_version,
            operation_id=args.operation_id, idempotency_key=args.idempotency_key)
        elif args.command == "scheduler-status": result = runtime.scheduler_status(None if args.all else project)
        elif args.command == "scheduler-tick": result = runtime.scheduler_tick(
            None if args.all else project, max_jobs=args.max_jobs, max_seconds=args.max_seconds)
        elif args.command == "scheduler-run": result = runtime.scheduler_run(
            None if args.all else project, max_seconds=args.max_seconds, worker_token=args.worker_token)
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
        elif args.command == "work-search": result = runtime.work_search(
            project, args.query, file_path=args.file_path, workspace=args.workspace,
            limit=args.limit, max_chars=args.max_chars)
        elif args.command == "work-read":
            if args.offset and args.expected_revision is None:
                raise ContractError("expected_revision is required to continue a section page")
            result = runtime.work_read(project, args.memory_id, expected_revision=args.expected_revision,
                                       offset=args.offset, max_chars=args.max_chars, section=args.section)
        elif args.command == "work-rebuild": result = runtime.work_rebuild(
            project, args.workspace, cursor=args.cursor, limit=args.limit)
        elif args.command == "admin-once": result = admin_once(runtime, project, args.problem)
        elif args.command == "admin-batch": result = admin_batch(runtime, project, dispatch_token=args.dispatch_token)
        elif args.command == "admin-configure":
            command = json.loads(Path(args.command_file).read_text(encoding="utf-8-sig")) if args.command_file else None
            policy = runtime.admin_policy(project)
            provider = args.provider or (policy["provider"] if policy["policy_version"] else "host")
            mode = args.model_mode or (policy["model_mode"] if policy["policy_version"] else "current")
            model = args.model if args.model is not None else policy["model"] if mode == policy["model_mode"] else ""
            result = runtime.admin_configure(project, args.enabled == "on", provider, mode, model, command,
                                             expected_policy_version=(args.expected_policy_version if args.expected_policy_version is not None
                                                                      else policy["policy_version"]))
        elif args.command == "admin-status":
            from .admin_usage import admin_budget_status
            result = {**runtime.admin_routing_status(project, args.conversation), "budget": admin_budget_status(runtime)}
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
