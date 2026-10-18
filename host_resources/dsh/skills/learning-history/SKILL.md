---
name: learning-history
description: "用户明确要求为已有项目吸收历史文档、源码和选定的 DSH 对话记录时，操作独立有限历史批次；不用于日常事件采集。"
---

# 项目历史吸收（DSH）

仅在当前用户明确选择时使用。安装、升级、会话启动、停止、空知识库和普通“继续”均不启动或恢复历史吸收。

- v1.1.4：先检查 `history-prepare --confirm` 的 `preparation.action`。`continue_existing` 表示前批主审未结束，未重新扫描；`no_changes` 表示未发现新增内容。不要因重复点击直接再次运行或创建批次，先报告现有进度，按当前明确授权续审；预算暂停、未知调用、旧账户与用户主动暂停仍核对原状态。
- 主审、补证和批准分开统计；待补证与待批准不阻挡新的增量。需切换范围时，由用户明确选择后用 `history-defer <job_id> --reason <原因> --confirm` 暂存旧主审，保留记录和访问边界。随后 `history-prepare --mode incremental --confirm` 只排入新增或变化来源；`--mode remaining` 优先补扫原选择中因文件数、字节数或分段预算遗漏的文件，沿用原上限，不用于未知格式或目录枚举上限；`--mode full` 仅用于用户明确要求的全量。新扫描不会自动启动模型。

- 核对明确的项目工作区、数据目录和运行副本。所有 `history-*` 命令显式传入 `--workspace` 和 `--host dsh`，保持一致的 `--home`。普通及管理员 MCP 没有历史启动或批准工具。
- 用户明确选定项目和额外来源后，使用 `history-prepare --confirm`；可通过 `--conversation-file` 指定选定的 DSH 会话 JSONL 或标准消息记录，通过 `--artifact-dir` 指定额外产物目录。程序自动剔除 `ReasoningBlock` 等模型内部思考，保存脱敏版本与覆盖清单。
- 用户批准本次归档后，使用 `history-run <job_id> --confirm`。仅在明确允许模型整理时附加 `--allow-review` 并限定 `--max-calls` 与 `--max-seconds`。历史流程不自动发布正式知识。
- `main` 的增强整理优先安排项目说明、Agent 总结、备忘等文档，在已批准且已归档的来源内补充有限相关资料。每次历史模型调用最多生成五条候选，实际数量由证据决定；旧 v1.0.0 发行包不含此能力。历史陈述保持 `reported`，相关参考不算独立主审核，多条候选也不增加主审核计数。
- 使用 `history-status`、`history-sources`、`history-cases` 分别报告归档覆盖、实际主审核、待补证／未触及单元、候选与发布，不声称已理解整个项目。暂停、预算不足或换号后不自动续跑；继续整理需显式 `history-run <job_id> --confirm`，按已授权范围限定调用和时长，并复用有效回执。
- 用户要审批候选时调用 `learning-review`，使用同一项目与数据目录打开可视化审批台；用户查看来源后选择批准发布、拒绝或暂缓。打开页面和生成候选均不等于批准发布。

完整命令与边界见 [项目说明](../../README.md)。
