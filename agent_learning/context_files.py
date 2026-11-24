"""File-change notices for exposed formal knowledge, independent of optional recall."""
from __future__ import annotations

import json
import sqlite3
import time


FILE_CHECK_LIMIT = 16
MAX_FILE_VIEWS = 4


def _file_snapshot(state):
    return {key: state[key] for key in ("version", "files", "pending_unverified") if key in state}


def _file_views(state):
    result = {}
    views = state.get("views")
    if isinstance(views, list) and len(views) < MAX_FILE_VIEWS:
        for entry in views:
            if isinstance(entry, list) and len(entry) == 2:
                key, snapshot = entry
                if isinstance(key, str) and isinstance(snapshot, dict):
                    result[key] = _file_snapshot(snapshot)
    checkout = state.get("checkout")
    if isinstance(checkout, str):
        result.pop(checkout, None)
        result[checkout] = _file_snapshot(state)
    return result


def _save_file_view(state, checkout, view, *, acknowledge_overflow=False):
    previous = state.get("_files", {})
    views = _file_views(previous)
    views.pop(checkout, None)
    # Retain pending risks ahead of recently displayed observations. The
    # legacy flat fields always carry the current reader's view.
    ordered = sorted(views.items(), key=lambda pair: pair[1].get("pending_unverified") is True)
    keep = MAX_FILE_VIEWS - 1
    saved = {**_file_snapshot(view), "checkout": checkout, "checked": time.time()}
    if ordered:
        saved["views"] = [[key, item] for key, item in ordered[-keep:]]
    if ((previous.get("pending_view_overflow") and not acknowledge_overflow)
            or any(item.get("pending_unverified") is True for _, item in ordered[:-keep])):
        saved["pending_view_overflow"] = True
    state["_files"] = saved


def delivered_state(state, observation, *, acknowledge_pending=False):
    if observation:
        checkout = observation["checkout"]
        previous = state.get("_files", {})
        prior_view = _file_views(previous).get(checkout, {})
        view = {"version": observation["version"], "files": observation.get("files", [])}
        if not acknowledge_pending and prior_view.get("pending_unverified") is True:
            view["pending_unverified"] = True
        _save_file_view(state, checkout, view, acknowledge_overflow=acknowledge_pending)
    return state


def notifications(runtime, project, conversation, agent_id, workspace, max_chars, *, receipts=None):
    """A file snapshot is acknowledged only after its complete notice fits."""
    if not workspace:
        return []
    from .runtime import ContractError, encode
    from .work_files import knowledge_file_observation
    key = (project, conversation, agent_id or "")
    rows = runtime.db.execute("SELECT x.* FROM context_exposures x JOIN active a ON "
        "a.project=x.project AND a.id=x.knowledge_id AND a.revision=x.revision "
        "WHERE x.project=? AND x.conversation=? AND x.agent=? "
        "ORDER BY COALESCE(json_extract(x.state,'$._files.checked'),0),x.updated,x.knowledge_id LIMIT ?",
        (*key, FILE_CHECK_LIMIT)).fetchall()
    notes, remaining = [], max(0, max_chars)
    for row in rows:
        state = json.loads(row["state"])
        snapshot = state.get("_files", {})
        def save_state():
            runtime.db.execute("UPDATE context_exposures SET state=? WHERE project=? AND conversation=? "
                "AND agent=? AND knowledge_id=? AND revision=?",
                (encode(state), *key, row["knowledge_id"], row["revision"]))
        def rotate():
            state["_files"] = dict(snapshot, checked=time.time())
            save_state()
        try:
            item = runtime.read(project, row["knowledge_id"])
            observation = knowledge_file_observation(runtime, project, item, workspace)
        except ContractError:
            rotate()
            continue
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
            rotate()
            continue  # Optional metadata observation cannot disable source checks.
        if not observation:
            rotate()
            continue
        checkout = observation["checkout"]
        views = _file_views(snapshot)
        if checkout in views:
            previous = dict(views[checkout], checkout=checkout)
            observed_views = [previous]
        else:
            # A new reader can still lose verification compared with the
            # displayed snapshots. Only its own notice can clear pending.
            previous = {key: snapshot[key] for key in ("checkout", "version", "files") if key in snapshot}
            observed_views = list(views.values()) or [previous]
        different = any(previous.get(field) != observation[field] for field in ("checkout", "version"))
        prior_files = previous.get("files")
        previously_verified = {(item.get("checkout"), item.get("path"))
                               for view in observed_views for item in view.get("files", ()) or ()
                               if isinstance(item, dict) and item.get("verified") is True}
        scope_loss = any((item.get("checkout"), item.get("path")) in previously_verified
                         and item.get("scope") is False for item in observation.get("files", []))
        # Old snapshots lack the additive per-file field. Keep their first
        # unknown transition visible without claiming an observed change.
        unknown_transition = bool(observation.get("unverified") and previous.get("version") and different
                                  and (scope_loss or prior_files is None or observation.get("blocked")))
        # Keep one bounded risk bit on this exact owner/knowledge/revision row.
        # A restored scope cannot erase a warning that did not fit its budget.
        pending = previous.get("pending_unverified") is True
        if unknown_transition:
            pending = True
            previous = dict(previous, pending_unverified=True)
        pending_overflow = snapshot.get("pending_view_overflow") is True
        needs_notice = (different and observation["changed"]) or pending or pending_overflow
        if needs_notice:
            if different and observation["changed"]:
                # The latest confirmed metadata change takes precedence over
                # an older unknown-scope warning; both require a source recheck.
                note = (f"知识 {row['knowledge_id']}@{row['revision']} 关联文件已变化；"
                        "旧摘要仅供参考，使用前 knowledge_read 核对。")
                if pending:
                    note += ("仍有关联文件当前无法核实。" if observation.get("unverified") else
                             "曾观察到核验范围或可用性变化，当前已恢复可核验。")
            elif pending and observation.get("unverified"):
                note = (f"知识 {row['knowledge_id']}@{row['revision']} 关联文件当前无法核实（范围或可用性已变化）；"
                        "旧摘要仅供参考，使用前 knowledge_read 核对。")
            elif pending:
                note = (f"知识 {row['knowledge_id']}@{row['revision']} 曾观察到关联文件核验范围或可用性变化，"
                        "当前已恢复可核验；使用前 knowledge_read 核对。")
            else:
                note = ""
            if pending_overflow:
                overflow = "其他检出也有过文件核验变化，使用前复核。"
                note = f"{note}{overflow}" if note else f"知识 {row['knowledge_id']}@{row['revision']} {overflow}"
            cost = len(note) + int(bool(notes))
            if cost <= remaining:
                notes.append(note)
                remaining -= cost
                delivered_state(state, observation, acknowledge_pending=True)
                if receipts is not None:
                    receipts.add((row["knowledge_id"], row["revision"], observation["checkout"], observation["version"]))
            else:
                # Rotate without acknowledging the unreturned new version.
                _save_file_view(state, checkout, previous)
        else:
            delivered_state(state, observation)
        save_state()
    return notes
