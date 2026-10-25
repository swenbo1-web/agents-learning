---
name: learning-debug
description: "快速只读检查 Agent Learning 插件连接、当前对话绑定、项目知识目录及管理员模型、预算和阻塞原因；适用于调试接入，不用于知识浏览、发布或开启审核。"
---

# Agent Learning 调试

这是输入框技能菜单的调试快捷项。调用现有 MCP 工具检查当前项目，不打开控制页面或运行管理员。

- 按当前工具 schema 调用一次 `learning_status(view="summary")`，提取 `project_id`、`home`、`host_scope`、`storage_schema_version`、`version`、管理员状态及预算。旧安装尚无 `view` 参数时才使用无参调用；只有摘要缺少本次诊断必需字段或标记 `strings_truncated` 时才按需读 `view="full"`，不把缩短的路径或模型当完整身份。不回显完整响应、知识正文、会话句柄或访问码。工具缺失时报告尚未接入，不改安装或配置。
- 仅使用本轮宿主 Hook 注入的真实 `context_handle` 调用 `session_context`。没有句柄时仅查状态并报告“本轮 Hook 未验证／缺少句柄”，不无参调用后借用连接中的旧绑定。不查找“最新会话”、复用历史句柄或猜测身份。项目不符、句柄失效或需重连时停止绑定检查，给出对应原因。
- 简短列出：MCP 是否响应、Hook 对话是否绑定、当前项目及知识目录、管理员实际开关 `admin_admission_enabled`、模型模式与 `admin_routing.executor.model`、`project_usage.counted_attempts` 项目今日审核尝试、`completed_reviews` 有效结论、`failed_attempts` 失败、`project_usage.remaining_calls` 项目剩余额度；另列 `global_usage` 所有项目合计与全局保护余量以及 `admission.blocking_scope`，注明按 UTC 日统计与本地重置时间；缺少项目字段时报告“项目统计暂不可用”，不能把全局数当项目数；token 未上报应标“未知”。同时说明当前阻塞原因。无模型观察或路由未就绪时显示 `admin_routing.reason`，不猜默认模型。旧安装缺少有效开关字段时，将 `admin_enabled` 标注为“策略开关，实际准入未提供”。开关打开或 CLI 可用都不等于审核器正在运行或常驻。
- 工具响应只能证明工具连通；只有本轮真实句柄成功绑定才报告本对话 Hook 已接通。不要把已有知识数量、旧事件或空知识库当成连接成功或失败依据。
- 默认仅作上述读取，不修改任务、反馈、配置或知识，不启动／重试审核、历史吸收或安装。详细知识查看转到 [learning-browser](../learning-browser/SKILL.md)；候选审批与管理员设置转到 [learning-review](../learning-review/SKILL.md)，在用户明确要求时再打开。

用用户当前语言给出短状态清单和一个最相关的下一步，不生成调试文档。
