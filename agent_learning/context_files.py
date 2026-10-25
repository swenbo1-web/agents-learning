"""File-change notices for exposed formal knowledge, independent of optional recall."""
from __future__ import annotations

import json
import sqlite3
import time


FILE_CHECK_LIMIT = 16


def delivered_state(state, observation):
    if observation:
        state["_files"] = {"checkout": observation["checkout"], "version": observation["version"],
                           "checked": time.time()}
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
        needs_notice = observation["changed"] and any(
            previous.get(field) != observation[field] for field in ("checkout", "version"))
        if needs_notice:
            note = (f"知识 {row['knowledge_id']}@{row['revision']} 关联文件已变化；"
                    "旧摘要仅供参考，使用前 knowledge_read 核对。")
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
