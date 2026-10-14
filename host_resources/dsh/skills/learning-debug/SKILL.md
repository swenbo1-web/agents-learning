---
name: learning-debug
description: "在 DSH 中只读检查 Agent Learning 连接、本轮会话绑定、项目知识目录和管理员模型、预算及阻塞原因，适用于接入调试。"
---

# Agent Learning 调试（DSH）

DSH 也可用已注册的 `/learning-debug` 快速查看本地状态；原生命令不验证 Hook 会话。完整的会话连接检查使用本技能及现有 MCP 工具、真实句柄；技能菜单是否显示由宿主决定。

- 调用一次 `learning_status`，仅提取项目 ID、`home`、`host_scope`、数据库版本、管理员路由和 `admin_budget`。不输出完整响应、知识正文、句柄或访问码。缺少工具时报告未接入，不改安装和配置。
- 使用本轮 DSH Hook 提供的真实 `context_handle` 调用 `session_context`，不猜测原生或带宿主前缀的会话 ID。无句柄时仅查状态并标明“本轮 Hook 未验证／缺少句柄”，不无参调用后借用连接中的旧绑定；不查找最新会话或复用历史句柄。项目不符、失效或需重连时停止绑定检查。
- 简短报告 MCP 是否响应、当前 Hook 是否绑定、项目与知识目录、实际开关 `admin_admission_enabled`、`admin_routing.executor.model` 和模式、`admin_budget.remaining_calls` 共享日额度剩余及 `admin_routing.reason` 阻塞原因。无有效模型观察就报告等待，不猜模型。旧安装无实际开关字段时把 `admin_enabled` 标为“策略开关，实际准入未提供”。开关或 CLI 可用不证明审核器正在运行或常驻。
- MCP 工具连通不等于 Hook 接通；只有本轮真实句柄绑定成功才确认本对话。旧记录、已有知识数量和空库都不是接入验收依据。
- 不更新任务、反馈、配置或知识，不启动或重试管理员、历史吸收或安装。详细查看使用 [learning-browser](../learning-browser/SKILL.md)；审批和管理员设置使用 [learning-review](../learning-review/SKILL.md)，用户明确要求后再打开。

使用用户当前语言输出简短状态清单和一个相关下一步，不生成调试文档。
