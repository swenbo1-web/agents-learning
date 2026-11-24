---
name: learning-worker
description: "在已接入 Agent Learning 的项目中登记实质任务、按需查阅经验和记录实际结果。"
---

# 项目任务与知识

直接推进用户授权的工作。确认当前项目已接入且收到本轮 Hook 的 `context_handle` 后使用插件。缺少绑定时继续任务，用户明确要求查询或诊断时使用对应的只读入口。

## 登记与交付

- 实质新任务在首次写入、执行或委派前用 `task_update(new_task=true)` 登记。活动任务的普通续聊沿用原任务，普通问答直接回复。继续已暂停或交付的原任务时先核对用户需求，再用 `resume=true`。
- 状态调用使用本轮句柄。写入已有任务（含替换）时，从最近的任务回执取得 `expected_task_id` 和 `expected_write_version`，缺少版本或遇到冲突时用 `session_context` 核对后再决定。登记前的事件和委派保留原归属。遇到 `reconnect_required` 由宿主重新绑定，结果未知的写入先核对回执。子 Agent 只读父任务边界，按自己的委派范围反馈。
- 完成实际交付时上报 `task_update(status="delivered")`，再正常回复用户。开启阶段审核时，父级 `Stop` 封存这次交付，由 Runtime 后续处理。

## 按需查阅

- 优先用当前已有的相关材料。按适用条件、修订和来源健康判断，`reported` 表示来源陈述。当前任务确实需要更多信息时，用 `knowledge_search` 找正式知识或 `work_memory_search` 找工作历史。
- 索引不足以判断时才打开正文。`read_required` 提示索引的范围。用 `knowledge_read` 或 `work_memory_read` 展开，续页沿用 `section`、`expected_revision` 和 `next_offset`。同一已读版本可复用，遇到来源变化、冲突或文件未完整核验时，先核对与当前决定有关的资料。
- 已启用的待定候选只提供线索。按卡片的 `candidate_id` 和 `reference_version` 用 `candidate_reference_read` 展开，版本变化后读取最新状态，发布后按返回映射查正式知识。

## 关键结果

- 用 `case_feedback` 补充尚未记录的关键结果，引用真实 `event_refs`。实际采用知识才用 `knowledge_use`，按工具契约传准确修订和任务版本。反馈和采用重试沿用原幂等键与参数。来源陈述、工具结果和用户验收分别判断。
- 任务登记和知识只提供上下文，执行权限取自用户与宿主。观察模式下服务不可用时继续已授权工作，必要时注明待同步。执行保护拒绝时停下相应操作并说明原因。发布、撤销和历史吸收各走专用入口。

细节见[任务范围与执行保护](../../README.md#任务范围与执行保护)、[渐进读取](../../README.md#先索引再详情再可见证据)和[案例采集与证据](../../README.md#案例采集与证据)。
