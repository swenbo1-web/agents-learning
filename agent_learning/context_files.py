"""File-change notices for exposed formal knowledge, independent of optional recall."""
from __future__ import annotations

import json
import sqlite3
import time


FILE_CHECK_LIMIT = 16


def delivered_state(state, observation):
    if observation:
        state["_files"] = {"checkout": observation["checkout"], "version": observation["version"],
                           "files": observation.get("files", []), "checked": time.time()}
    return state


def notifications(runtime, project, conversation, agent_id, workspace, max_chars):
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
        previous = state.get("_files", {})
        def save_state():
            runtime.db.execute("UPDATE context_exposures SET state=? WHERE project=? AND conversation=? "
                "AND agent=? AND knowledge_id=? AND revision=?",
                (encode(state), *key, row["knowledge_id"], row["revision"]))
        def rotate():
            state["_files"] = dict(previous, checked=time.time())
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
        different = any(previous.get(field) != observation[field] for field in ("checkout", "version"))
        prior_files = previous.get("files")
        previously_verified = {(item.get("checkout"), item.get("path")) for item in prior_files or []
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
        needs_notice = (different and observation["changed"]) or pending
        if needs_notice:
            if different and observation["changed"]:
                # The latest confirmed metadata change takes precedence over
                # an older unknown-scope warning; both require a source recheck.
                note = (f"知识 {row['knowledge_id']}@{row['revision']} 关联文件已变化；"
                        "旧摘要仅供参考，使用前 knowledge_read 核对。")
                if pending:
                    note += ("仍有关联文件当前无法核实。" if observation.get("unverified") else
                             "曾观察到核验范围或可用性变化，当前已恢复可核验。")
            elif observation.get("unverified"):
                note = (f"知识 {row['knowledge_id']}@{row['revision']} 关联文件当前无法核实（范围或可用性已变化）；"
                        "旧摘要仅供参考，使用前 knowledge_read 核对。")
            else:
                note = (f"知识 {row['knowledge_id']}@{row['revision']} 曾观察到关联文件核验范围或可用性变化，"
                        "当前已恢复可核验；使用前 knowledge_read 核对。")
            cost = len(note) + int(bool(notes))
            if cost <= remaining:
                notes.append(note)
                remaining -= cost
                delivered_state(state, observation)
            else:
                # Rotate without acknowledging the unreturned new version.
                state["_files"] = dict(previous, checked=time.time())
        else:
            delivered_state(state, observation)
        save_state()
    return notes
