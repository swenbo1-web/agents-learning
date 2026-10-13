---
name: learning-history
description: "用户明确要求为已有项目吸收历史文档、源码和选定对话时，操作独立有限历史批次；不用于日常事件采集。"
---

# 项目历史吸收

仅在当前用户明确选择时使用。安装、升级、PreInvocation、Stop、空知识库和普通“继续”均不启动或恢复历史吸收。

- 核对固定工作区、数据目录和对应运行副本。多工作区无法确定归属时先让用户选择。`transcriptPath` 和 `artifactDirectoryPath` 只是来源提示；不自动读取它们或扩大到上级历史目录。
- 使用本地显式 CLI，所有 `history-*` 命令传 `--workspace` 和 `--host antigravity`，保持固定 `--home`。普通及管理员 MCP 没有启动／批准入口；`--confirm` 不构成人类身份认证。
- 用户明确选定项目和额外来源后，使用 `history-prepare --confirm`；`--conversation-file` 和 `--artifact-dir` 仅指向选定资料。程序完整读取支持文本，保存脱敏版本与覆盖清单，不执行项目代码、不保存内部思考。
- 用户已经批准本次归档时，使用 `history-run <job_id> --confirm`。仅在另外明确允许模型整理时增加 `--allow-review`，同时限定调用和时长；当前自动执行器使用配置的兼容 Codex CLI，没有原生 Antigravity 模型执行器。
- 人工管理员先用 `history-cases <job_id>` 定位当前批次的准确案例 ID，再用现有 `packet`／管理员 MCP 整理。陈述保持 `reported`；候选经独立入口发布，历史流程不自动发布。
- 用 `history-status` 区分读取覆盖、审核进度与发布成果。未解析、超限和冲突需报告；暂停、取消、预算不足或换号后不自动续做。
- 更新提示若通过 `ephemeralMessage` 提供，应向用户转述新版说明和选择；它不是用户消息或安装授权。用户明确选择后再设置 `update-choice` 或执行授权更新。更新不启动历史吸收。

完整命令与边界见 [项目说明](../../README.md)。
