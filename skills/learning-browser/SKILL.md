---
name: learning-browser
description: "启动或查看本机只读可视化知识库浏览器（127.0.0.1），在浏览器中搜索和筛选正式知识，查看正文、适用条件、限制、来源健康、修订历史、事件时间线与管理员待办。"
---

# 可视化知识库浏览器

本机只读知识浏览界面仅监听 `127.0.0.1`，以 SQLite 只读快照展示当前项目的正式知识、来源健康、修订历史、事件时间线与管理员待办；不调用模型、不修改数据库、不登记模型展示（`exposures`）。

- **DSH 宿主**：可直接调用原生工具 `learning_browser(action="start"|"status"|"stop", open=true)`，或在聊天框输入斜杠命令 `/learning-browser [start|status|stop]`。
- **CLI 显式启动**：在终端执行 `python -B scripts/learning.py --workspace "<项目绝对路径>" browser --open`（若自定义数据目录，附带一致的 `--home PATH`）。
- **安全边界**：临时访问码通过 URL fragment（`#token=...`）传入并在页面加载时立即从地址栏清除，仅保留在页面内存中，不写入磁盘。页面仅提供只读查看，不执行发布、撤销或任务审批。
