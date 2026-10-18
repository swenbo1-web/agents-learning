"""管理员调用的租约、启动边界与显式重试；不启动或终止外部进程。"""
from __future__ import annotations

import contextlib
import time
import uuid

from .runtime import ContractError, encode


ACTIVE_STATES = ("reserved", "launch_intent", "started")
UNKNOWN_STATES = ("timeout_unknown", "account_changed_unknown", "orphaned_unknown")
RETRYABLE_STATES = ("failed", *UNKNOWN_STATES)
TERMINAL_STATES = ("not_started", "candidate", "needs_evidence", "skip", "governance", "failed", *UNKNOWN_STATES)


def _transaction(runtime):
    return contextlib.nullcontext() if runtime.db.in_transaction else runtime.transaction()


def _require_transaction(runtime):
    if not runtime.db.in_transaction:
        raise ContractError("administrator claim requires an existing write transaction")


def install_lifecycle(runtime):
    """由迁移入口在已有写事务内调用；旧活动记录不能证明未启动。"""
    _require_transaction(runtime)
    additions = {"started": "REAL", "finished": "REAL", "usage": "TEXT", "fingerprint": "TEXT",
                 "owner": "TEXT", "lease_until": "REAL", "launch_intent": "REAL", "pid": "INTEGER",
                 "retry_allowed": "INTEGER NOT NULL DEFAULT 0", "retry_reason": "TEXT", "retry_requested": "REAL"}
    existing = {row[1] for row in runtime.db.execute("PRAGMA table_info(admin_calls)")}
    for column, declaration in additions.items():
        if column not in existing:
            runtime.db.execute(f"ALTER TABLE admin_calls ADD COLUMN {column} {declaration}")
    # 老执行器可能在 Popen 后、started 落盘前崩溃；升级必须保留未知额度。
    runtime.db.execute("UPDATE admin_calls SET lease_until=0,launch_intent=COALESCE(launch_intent,started,created) "
                       "WHERE owner IS NULL AND state IN ('reserved','launch_intent','started')")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS admin_calls_active ON admin_calls(project,state,lease_until)")


def recover_calls(runtime, project=None, now=None):
    """释放过期调度占用；只有新协议严格启动意图前的预留才免费回收。"""
    current = time.time() if now is None else now
    recovered = []
    with _transaction(runtime):
        rows = runtime.db.execute(
            "SELECT * FROM admin_calls WHERE state IN ('reserved','launch_intent','started') "
            "AND COALESCE(lease_until,0)<=? AND (? IS NULL OR project=?) ORDER BY created,id",
            (current, project, project)).fetchall()
        for row in rows:
            state = ("not_started" if row["state"] == "reserved" and row["owner"]
                     and row["launch_intent"] is None and row["started"] is None else "orphaned_unknown")
            runtime.db.execute("UPDATE admin_calls SET state=?,finished=?,lease_until=NULL WHERE id=?",
                               (state, current, row["id"]))
            recovered.append({"call_id": row["id"], "project": row["project"], "state": state})
    return {"recovered": recovered, "count": len(recovered)}


def claim_call(runtime, project, problem, revision, fingerprint, host_epoch, timeout, *, reserved_tokens=None):
    """取包后的同一 BEGIN IMMEDIATE 内认领项目槽与全局 UTC 日额度。"""
    _require_transaction(runtime)
    runtime.assert_host_epoch(host_epoch)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 600:
        raise ContractError("invalid administrator lease timeout")
    # 日额度跨项目共享，安全释放也必须先覆盖全部项目。
    recover_calls(runtime)
    active = runtime.db.execute(
        "SELECT id,state FROM admin_calls WHERE project=? AND state IN ('reserved','launch_intent','started') "
        "ORDER BY created,id LIMIT 1", (project,)).fetchone()
    if active:
        return {"status": "waiting_for_review", "call_id": active["id"], "active_state": active["state"]}
    from .admin_usage import admin_admission_status, reserve_usage
    current = time.time()
    budget = admin_admission_status(runtime, project, now=current)
    if budget["admission_status"] != "available":
        return {"status": budget["admission_status"], "blocking_scope": budget["blocking_scope"], "budget": budget}
    if reserved_tokens is not None and budget["remaining_reported_tokens"] is not None and budget["remaining_reported_tokens"] < reserved_tokens:
        return {"status": "token_budget_exhausted", "reason": "insufficient_token_reservation", "budget": budget}
    day = budget["utc_day"]
    call_id, owner = str(uuid.uuid4()), str(uuid.uuid4())
    lease_until = current + timeout + 30
    runtime.db.execute(
        "INSERT INTO admin_calls(id,project,problem,revision,day,state,created,fingerprint,host_epoch,owner,lease_until) "
        "VALUES(?,?,?,?,?,'reserved',?,?,?,?,?)",
        (call_id, project, problem, revision, day, current, fingerprint, host_epoch, owner, lease_until))
    reserve_usage(runtime, call_id, reserved_tokens=reserved_tokens)
    runtime.db.execute("UPDATE admin_calls SET retry_allowed=2 WHERE project=? AND problem=? AND revision=? "
                       "AND retry_allowed=1 AND id!=?", (project, problem, revision, call_id))
    return {"status": "reserved", "call_id": call_id, "owner": owner, "lease_until": lease_until,
            "host_epoch": host_epoch}


def validate_call(runtime, call_id, owner, host_epoch):
    """响应接受前与候选提交同事务校验，失去租约的执行器不能晚到回写。"""
    runtime.assert_host_epoch(host_epoch)
    row = runtime.db.execute("SELECT * FROM admin_calls WHERE id=?", (call_id,)).fetchone()
    if (row is None or row["owner"] != owner or row["host_epoch"] != host_epoch
            or row["state"] not in ACTIVE_STATES or row["lease_until"] is None
            or row["lease_until"] <= time.time()):
        raise ContractError("administrator call ownership or lease changed")
    return dict(row)


def mark_launch_intent(runtime, call_id, owner, host_epoch):
    """Popen 前持久化；调用者必须先提交此事务再启动进程。"""
    with _transaction(runtime):
        row = validate_call(runtime, call_id, owner, host_epoch)
        if row["state"] != "reserved" or row["launch_intent"] is not None:
            raise ContractError("administrator call already launched")
        runtime.db.execute("UPDATE admin_calls SET state='launch_intent',launch_intent=? WHERE id=? AND owner=?",
                           (time.time(), call_id, owner))
        return dict(runtime.db.execute("SELECT * FROM admin_calls WHERE id=?", (call_id,)).fetchone())


def mark_started(runtime, call_id, owner, pid=None):
    """只登记所属进程的观察；PID 不能用于回收判断或跨进程终止。"""
    if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
        raise ContractError("invalid administrator process identity")
    with _transaction(runtime):
        row = runtime.db.execute("SELECT * FROM admin_calls WHERE id=?", (call_id,)).fetchone()
        if row is None:
            raise ContractError("unknown administrator call")
        row = validate_call(runtime, call_id, owner, row["host_epoch"])
        if row["state"] != "launch_intent" or row["launch_intent"] is None:
            raise ContractError("administrator launch intent required")
        runtime.db.execute("UPDATE admin_calls SET state='started',started=?,pid=? WHERE id=? AND owner=?",
                           (time.time(), pid, call_id, owner))
        return dict(runtime.db.execute("SELECT * FROM admin_calls WHERE id=?", (call_id,)).fetchone())


def finish_call(runtime, call_id, state, usage=None, response=None):
    """结束并补充真实计量；决策回执由决策模块保存，未知结果不被晚到响应覆盖。"""
    if state not in TERMINAL_STATES:
        raise ContractError("invalid administrator terminal state")
    with _transaction(runtime):
        row = runtime.db.execute("SELECT * FROM admin_calls WHERE id=?", (call_id,)).fetchone()
        if row is None:
            raise ContractError("unknown administrator call")
        final = row["state"]
        known_no_start = state == "not_started" and row["started"] is None
        if final in ACTIVE_STATES:
            final = ("account_changed_unknown" if row["host_epoch"] != runtime.host_epoch() and not known_no_start
                     else "orphaned_unknown" if row["lease_until"] is not None and row["lease_until"] <= time.time()
                     and not known_no_start else state)
        elif final == "account_changed_unknown" and known_no_start and row["launch_intent"] is None:
            final = "not_started"
        # 已完成或已回收状态只能补计量，不能由旧执行器改变结论或再次开始。
        from .admin_usage import record_usage
        encoded_usage = None if usage is None else encode(record_usage(runtime, call_id, usage))
        runtime.db.execute("UPDATE admin_calls SET state=?,finished=COALESCE(finished,?),lease_until=NULL,"
                           "usage=CASE WHEN ? IS NULL THEN usage ELSE ? END WHERE id=?",
                           (final, time.time(), encoded_usage, encoded_usage, call_id))
        return dict(runtime.db.execute("SELECT * FROM admin_calls WHERE id=?", (call_id,)).fetchone())


def retry_call(runtime, project, call_id, reason):
    """受信任显式再审，只打开资格；旧记录、未知结果与原日额度始终保留。"""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ContractError("administrator retry requires a reason of 1..2000 characters")
    with _transaction(runtime):
        row = runtime.db.execute("SELECT * FROM admin_calls WHERE id=? AND project=?", (call_id, project)).fetchone()
        if row is None:
            raise ContractError("unknown administrator call in project")
        if row["state"] not in RETRYABLE_STATES:
            raise ContractError("administrator call is not retryable")
        if row["retry_allowed"] == 2:
            return {"status": "retry_consumed", "call_id": call_id, "previous_state": row["state"],
                    "problem_id": row["problem"], "problem_revision": row["revision"]}
        from .phase_review import retry as retry_phase
        retry_phase(runtime, project, row["problem"])
        if not row["retry_allowed"]:
            runtime.db.execute("UPDATE admin_calls SET retry_allowed=1,retry_reason=?,retry_requested=? WHERE id=?",
                               (reason.strip(), time.time(), call_id))
        return {"status": "retry_allowed", "call_id": call_id, "previous_state": row["state"],
                "problem_id": row["problem"], "problem_revision": row["revision"]}
