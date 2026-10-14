---
name: learning-browser
description: "启动或查看本机只读可视化知识库浏览器（127.0.0.1），在浏览器中搜索和筛选正式知识，查看正文、适用条件、限制、来源健康、修订历史、事件时间线与管理员待办。"
---

# 可视化知识库浏览器

本机只读知识浏览界面仅监听 `127.0.0.1`，以 SQLite 只读快照展示当前项目的正式知识、来源健康、修订历史、事件时间线与管理员待办；不调用模型、不修改数据库、不登记模型展示（`exposures`）。

- 核对当前项目、宿主及知识目录。从当前安装的 `runtime-release.json` 或生效的 MCP 配置确认实际解释器及固定运行入口，沿用安装的 `ALP_CONFIG`；不要依赖终端 cwd、开发源码或猜测默认知识目录。
- 使用该安装的实际值运行 `<PYTHON> -B <RUNTIME>/scripts/learning.py --workspace <PROJECT> --home <KNOWLEDGE_HOME> --host codex browser --open`；保持临时服务运行。Windows 后台启动隐藏窗口。
- **DSH 宿主**：可直接调用原生工具 `learning_browser(action="start"|"status"|"stop", open=true)`，或使用 `/learning-browser [start|status|stop]`。
- **安全边界**：让 `--open` 直接打开启动链接；不回显或保存含访问码的输出。访问码通过 URL fragment 传入，页面加载后清除，仅留在进程及页面内存。需要候选审批或管理员设置时使用 `learning-review`；只读浏览不增加 `--review` 或 `--admin-control`。
