---
name: learning-history
description: "用户明确要求为已有项目吸收历史文档、源码和选定的 DSH 对话记录时，操作独立有限历史批次；不用于日常事件采集。"
---

# 项目历史吸收（DSH）

仅在当前用户明确选择时使用。安装、升级、会话启动、停止、空知识库和普通“继续”均不启动或恢复历史吸收。

- 核对明确的项目工作区、数据目录和运行副本。所有 `history-*` 命令显式传入 `--workspace` 和 `--host dsh`，保持一致的 `--home`。普通及管理员 MCP 没有历史启动或批准工具。
- 用户明确选定项目和额外来源后，使用 `history-prepare --confirm`；可通过 `--conversation-file` 指定选定的 DSH 会话 JSONL 或标准消息记录，通过 `--artifact-dir` 指定额外产物目录。程序自动剔除 `ReasoningBlock` 等模型内部思考，保存脱敏版本与覆盖清单。
- 用户批准本次归档后，使用 `history-run <job_id> --confirm`。仅在明确允许模型整理时附加 `--allow-review` 并限定 `--max-calls` 与 `--max-seconds`。历史流程不自动发布正式知识。
- 使用 `history-status`、`history-sources`、`history-cases` 查看覆盖、案例与进度。

完整命令与边界见 [项目说明](../../README.md)。
