---
name: learning-review
description: "在 DSH 中打开当前项目的本机可视化知识审批台，查看候选和证据，由用户在页面批准发布、拒绝或暂缓；不用于任务执行审批。"
---

# 可视化知识审批（DSH）

用户在本机页面预览候选后作出决定。打开审批台不表示批准任何候选；不替用户发布全部候选，不调用模型重复整理知识。

- 核对用户当前项目的绝对路径、宿主及知识目录；从本安装的 `runtime-release.json` 中读取 `python`、`runtime_directory`，或使用已生效 MCP 配置的同一固定入口。不要依赖终端 cwd、开发源码或其他项目的安装。沿用安装中的自定义 `ALP_CONFIG`。
- 使用已核实的实际路径运行：

  ```text
  <PYTHON> -B <RUNTIME>/scripts/learning.py --workspace <PROJECT_ABSOLUTE_PATH> --home <KNOWLEDGE_HOME> --host dsh browser --review --open --language zh-CN
  ```

  英文界面使用 `--language en`。先检查该入口的 `browser --help` 是否支持 `--review`；旧版本需要更新，不擅自改用其他源码。
- 保持临时本机服务在工具支持的持续终端或进程会话中运行；后台启动 Windows 进程时隐藏窗口。访问码不写入项目文档或持久日志。
- 用户查看候选正文、适用条件、限制、来源、证据等级及发布检查后，在页面选择单条或固定清单批准并发布、拒绝或暂缓。按钮由程序处理，不额外调用模型；发布结果以逐条回执为准。
- `reported` 资料发布后不升级为已验证；候选、来源或版本变化时刷新详情并重新选择，不覆盖检查或规避冲突。知识批准不授予工具执行权限，不启动历史吸收或管理员模型，也不撤销正式知识。
- DSH 的 `learning_browser` 原生工具及 `/learning-browser` 仍只读。审批使用上方 `browser --review`；仅查看正式知识时使用 `learning-browser`。
