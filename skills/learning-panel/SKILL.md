---
name: learning-panel
description: "打开 Agent Learning 项目控制面板；支持 MCP Apps 时使用对话卡片按钮，不支持时打开本机可视化知识库；用于查看状态、调试接入和进入审批设置。"
---

# Agent Learning 控制面板

用户请求可视化项目控制时，调用一次 `learning_panel`，显式传入本轮宿主 Hook 提供的真实 `context_handle`。没有本轮句柄时说明尚未验证，不猜测、搜索或复用历史句柄。

- 此调用只打开状态卡。用户在卡片内点击按钮后，宿主直接调用工具；无需模型代替点击。卡片的「检查接入」按钮显示当前项目、连接及管理员阻塞的有限诊断。不调用 `learning_panel_action`，不索取或输出卡片的私有能力。
- 管理员频率和项目历史归档由用户在卡片中确认。历史归档本身不调用模型、不生成或发布知识；审批发布和详细管理员设置在独立知识库页面处理。
- 收到 `panel_ui_unavailable` 和 `fallback` 时，按其中已验证的 `installation`、`workspace`、`home`、`host`、`host_instance` 复用 [learning-browser](../learning-browser/SKILL.md) 启动当前安装的 `browser --open`，保持实际 `ALP_CONFIG` 与宿主实例。不要停在「不支持卡片」；默认打开只读页面，页面提供知识浏览与待办查看，连接诊断使用 [learning-debug](../learning-debug/SKILL.md)。用户已明确请求审批或管理员设置时，改用 [learning-review](../learning-review/SKILL.md) 打开对应页面，打开不等于批准或开启。
- 启动链接由 `--open` 直接交给浏览器；只展示不含访问码的 `http://127.0.0.1:<实际端口>/` 页面地址，访问码、审批码和管理员控制码不回显、不保存。无访问码地址只供找到已打开页面，不能用于新页面自动认证；浏览器未能打开时，明确说明当前状态，不声称已渲染。
- 工具缺失时，通过当前生效安装配置核对项目后使用上述技能。若返回 `project_panel_not_installed`、`current_context_required` 或安装／会话已失效，先报告原因并用 `learning-debug` 只读诊断，不借其他项目或历史句柄继续启动。
- 成功只说明已返回可视化资源；未实际观察到界面时不声称按钮已在宿主渲染。不修改宿主安装，不生成调试文档。
