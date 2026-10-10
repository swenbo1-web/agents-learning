from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .admin import admin_once
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
    task = sub.add_parser("task")
    task.add_argument("--conversation", required=True)
    task.add_argument("--goal", required=True)
    task.add_argument("--scope", default="{}", help="JSON对象")
    task.add_argument("--expected-revision", type=int)
    task.add_argument("--new-task", action="store_true")
    task.add_argument("--status", choices=["active", "delivered", "cancelled", "interrupted"], default="active")
    feedback = sub.add_parser("feedback")
    feedback.add_argument("--conversation", required=True)
    feedback.add_argument("--summary", required=True)
    feedback.add_argument("--category", default="unknown")
    feedback.add_argument("--event-ref", action="append", default=[])
    feedback.add_argument("--resolution", default="")
    feedback.add_argument("--knowledge-id")
    sub.add_parser("problems")
    sub.add_parser("packet").add_argument("problem_id")
    sub.add_parser("propose").add_argument("file")
    publish = sub.add_parser("publish")
    publish.add_argument("candidate_id")
    publish.add_argument("--expected-generation", type=int)
    sub.add_parser("revoke").add_argument("knowledge_id")
    sub.add_parser("search").add_argument("query")
    sub.add_parser("read").add_argument("knowledge_id")
    admin = sub.add_parser("admin-once")
    admin.add_argument("--problem")
    sub.add_parser("sync").add_argument("--retry", help="先人工核对远端结果，再显式重试此知识操作ID")
    sub.add_parser("install").add_argument("destination")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "install":
        try:
            print(encode(install(args.destination)))
            return 0
        except (ContractError, OSError) as exc:
            print(encode({"status": "error", "reason": str(exc)}), file=sys.stderr)
            return 1
    runtime = None
    try:
        runtime = Runtime(args.home)
        project = runtime.project(args.workspace)
        if args.command == "serve":
            Server(runtime, project, args.role).serve()
            return 0
        if args.command == "status": result = runtime.status(project)
        elif args.command == "task": result = runtime.task_update(project, args.conversation, args.goal,
                                                json.loads(args.scope), args.expected_revision, args.status, args.new_task)
        elif args.command == "feedback": result = runtime.feedback(project, args.conversation, args.summary,
                                                args.category, args.event_ref, args.resolution, args.knowledge_id)
        elif args.command == "problems": result = runtime.problems(project)
        elif args.command == "packet": result = runtime.review_packet(project, args.problem_id)
        elif args.command == "propose": result = runtime.submit_candidate(project, **json.loads(Path(args.file).read_text(encoding="utf-8-sig")))
        elif args.command == "publish": result = runtime.publish(project, args.candidate_id, args.expected_generation)
        elif args.command == "revoke": result = runtime.revoke(project, args.knowledge_id)
        elif args.command == "search": result = runtime.search(project, args.query)
        elif args.command == "read": result = runtime.read(project, args.knowledge_id)
        elif args.command == "admin-once": result = admin_once(runtime, project, args.problem)
        elif args.command == "sync": result = runtime.sync_once(project, args.retry)
        else: raise ContractError("unknown command")
        print(encode(result))
        return 0
    except (ContractError, OSError, ValueError) as exc:
        print(encode({"status": "error", "reason": str(exc)}), file=sys.stderr)
        return 1
    finally:
        if runtime:
            runtime.close()
