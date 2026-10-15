---
name: learning-panel
description: "打开 Agent Learning 对话控制卡，通过按钮查看项目状态、打开知识库与审批、确认管理员频率及历史归档；适用于支持 MCP Apps 的宿主。"
---

# Agent Learning 控制面板

用户请求可视化项目控制时，调用一次 `learning_panel`，显式传入本轮宿主 Hook 提供的真实 `context_handle`。没有本轮句柄时说明尚未验证，不猜测、搜索或复用历史句柄。

- 此调用只打开状态卡。用户在卡片内点击按钮后，宿主直接调用工具；无需模型代替点击。不调用 `learning_panel_action`，不索取或输出卡片的私有能力。
- 管理员频率和项目历史归档由用户在卡片中确认。历史归档本身不调用模型、不生成或发布知识；审批发布和详细管理员设置在独立知识库页面处理。
- 宿主未声明 MCP Apps 能力、工具缺失或卡片不可用时，简短说明实际原因；使用 [learning-debug](../learning-debug/SKILL.md) 检查接入，或按用户需求通过 [learning-browser](../learning-browser/SKILL.md)／[learning-review](../learning-review/SKILL.md) 打开详细页面。
- 成功只说明已返回可视化资源；未实际观察到界面时不声称按钮已在宿主渲染。不修改宿主安装，不生成调试文档。
