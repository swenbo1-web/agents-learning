---
name: learning-browser
description: "在 DeepSeek Harness (DSH) 中启动、查看或关闭本机只读可视化知识库浏览器（127.0.0.1），搜索和筛选正式知识，查看正文、适用条件、限制、来源健康、修订历史、事件时间线与管理员待办。"
---

# 可视化知识库浏览器（DSH）

本机只读知识浏览界面仅监听 `127.0.0.1`，以一致的 SQLite 只读快照展示当前项目的正式知识、来源健康、修订历史、事件时间线与管理员待办；不会调用生成或嵌入模型、不会修改数据库、也不会将浏览记录计为模型上下文展示（`exposures`）。

## 在 DSH 中调用的三种方式

1. **直接调用 DSH 原生工具 `learning_browser`（推荐）**：
   - **启动并打开浏览器**：调用 `learning_browser(action="start", open=true)`（可指定可选 `port`）。该工具会在后台启动当前项目的只读知识库服务，自动在系统默认浏览器中打开带临时访问码（`#token=...`）的页面，并返回本机访问地址。页面加载后会自动清除 URL fragment，将访问码仅保存在页面内存中。
   - **查看运行状态**：调用 `learning_browser(action="status")`，查看当前项目的知识库浏览器是否正在运行及对应地址。
   - **停止浏览器服务**：调用 `learning_browser(action="stop")`，安全关闭后台知识库浏览器进程。
2. **使用 DSH 聊天框斜杠命令 `/learning-browser`**：
   - `/learning-browser`（或 `/learning-browser start`）：一键启动并打开当前项目的可视化知识库浏览器；
   - `/learning-browser status`：查看运行状态与访问链接；
   - `/learning-browser stop`：关闭后台浏览器服务。
3. **显式 CLI 命令（终端手动运行）**：
   - `python -B scripts/learning.py --workspace "<项目绝对路径>" browser --open`

## 边界与安全约定

- 访问码（`access_token`）仅属于当前运行中的浏览器服务进程与页面内存，绝不写入持久磁盘文件或知识库。
- 若项目尚未在知识库中登记，启动时会自动为当前工作区完成项目身份登记，再以只读模式打开。
- 浏览界面为纯只读视图；知识的正式发布（`publish`）、撤销（`revoke`）、恢复（`restore`）与任务执行审批（`execution-approve`）仍通过各自的显式受信入口执行。
