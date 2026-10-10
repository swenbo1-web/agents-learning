# Agent Learning Plugin

面向项目 Agent 的本地经验记录、知识检索与候选审核工具。当前版本 0.2.0，使用 Python 3.10+ 和 SQLite，默认不调用付费模型，不连接 Hindsight，不自动发布知识。

0.2.0 修复 Windows 中文通信、长日志结构丢失、历史任务无法整理、同名知识覆盖、注入恢复和管理员记账问题。数据库首次打开时在事务内升级到版本 2，保留既有记录和知识 ID；旧任务快照优先从明确的任务更新事件恢复，缺失信息不会补猜。常见凭证脱敏用于减少后续记录与外发，不能保证抹除已经存在的副本。

## 首版流程

```text
用户当前任务 → 有限前置检索 → 普通 Agent 工作
                             ↓
可观察事件、交付结果与问题反馈 → 本地持久记录 → 管理员提出候选
                                          ↓
                         人工发布入口校验 → 已发布知识
```

普通续聊不要求完成整条学习流水线。管理员只处理新增或变化的问题，复用已有案例和证据；工作任务交付不等待后台整理完成。

本项目记录消息、工具事件和实际结果，不能读取或保存模型内部思考。知识来源不是新任务授权，历史知识也不能替代当前环境、实时预算、租约和用户验收。

## 本地运行

在仓库目录运行以下命令。示例中的 `python` 应指向实际的 Python 3.10+ 解释器。

```powershell
python scripts/learning.py status
python scripts/learning.py --workspace "D:\Projects\my-project" status
```

默认数据位置为 Windows 的 `%LOCALAPPDATA%\AgentLearning`，或 POSIX 的 `~/.local/share/agent-learning`。使用 `--home PATH` 指定独立数据目录；使用 `--workspace PATH` 指定项目范围。

使用自定义配置时，复制 [config.example.json](config.example.json)，再通过 `ALP_CONFIG` 指向该 JSON 文件。例如在 PowerShell 中：

```powershell
Copy-Item -LiteralPath .\config.example.json -Destination .\config.local.json
$env:ALP_CONFIG = (Resolve-Path .\config.local.json).Path
python scripts/learning.py status
```

配置文件与状态库分离。不要在配置文件写入 API 密钥；可选 Hindsight 从配置指定的环境变量读取密钥。

## 接入插件与 MCP

```powershell
python scripts/learning.py install "D:\Plugins\agent-learning"
```

`install` 创建独立安装副本，将实际 Python 与 MCP 脚本绝对路径写入配置。Hook 使用宿主提供的 `PLUGIN_ROOT` 执行当前安装缓存中的脚本。它不更改既有 Codex 配置。副本含本地插件目录，随后运行：

```powershell
codex plugin marketplace add "D:\Plugins\agent-learning"
codex plugin add agent-learning@agent-learning-local
```

在 Codex 中开启新对话，审阅并信任 Hooks（CLI 可用 `/hooks`）。安装器不会代替用户设置 Hook 信任。保留安装副本与对应解释器；移动目录后重新导出并安装。Hook 的路径与信任规则见 [官方插件说明](https://developers.openai.com/plugins/build/plugins)。

源码中的 `.mcp.json` 是模板，安装副本会将路径渲染为实际绝对路径。本机已用隔离配置验证 Codex 原生加载与安装命令，未改当前用户的插件配置。

标准 wheel 包含导出资源，也支持 `python -m agent_learning.cli` 入口。运行时、CLI 和 MCP 均使用 UTF-8；无需把中文请求转义成 ASCII 来绕过系统编码。

也可以单独启动 stdio MCP 服务：

```powershell
python scripts/learning.py --workspace "D:\Projects\my-project" serve --role worker
python scripts/learning.py --workspace "D:\Projects\my-project" serve --role admin
```

| 身份 | MCP 工具 |
|---|---|
| 普通 Agent | `knowledge_search`、`knowledge_read`、`task_update`、`case_feedback`、`learning_status` |
| 管理员 | 上述工具，以及 `problem_list`、`review_packet`、`submit_candidate` |

MCP 不暴露发布或撤销工具。角色由启动入口指定，工具参数不能提升角色。技能位于 [learning-worker](skills/learning-worker/SKILL.md) 和 [learning-admin](skills/learning-admin/SKILL.md)。

这些权限限制覆盖本服务的入口。同一操作系统账户下的任意 Shell 和本地数据库访问，不属于 MCP 角色隔离保证；需要宿主权限与账户隔离配合。

## 手动处理一个问题

下面的示例只记录明确任务和问题，不运行项目主流程：

```powershell
python scripts/learning.py --workspace "D:\Projects\my-project" task --conversation demo --goal "分析某工具错误，不修改项目"
python scripts/learning.py --workspace "D:\Projects\my-project" feedback --conversation demo --summary "工具错误再次出现，尚未解决" --category operation
python scripts/learning.py --workspace "D:\Projects\my-project" problems
```

用返回的真实问题 ID 读取 `packet PROBLEM_ID`。管理员依据该包整理候选 JSON，再调用 `propose FILE`。候选字段如下：

| 字段 | 含义 |
|---|---|
| `problem_id`、`problem_revision` | 问题标识字符串及审核依据的整数版本 |
| `title`、`content` | 标题和正文字符串 |
| `category` | `operation`、`professional`、`project`、`mixed` 或 `unknown` |
| `applicability`、`limitations` | 适用条件与限制字符串 |
| `evidence_refs` | 实际事件 ID 字符串列表 |
| `validation_level` | `reported` 或 `tool_success` |
| `metadata`（可选） | `tool`、`tool_version`、`error_code`、`environment`、`verification` 字符串；未知时省略或留空 |

`reported` 表示有记录的陈述，不代表已经验证。`tool_success` 要求引用明确完成且成功的 `PostToolUse` 事件，只能支持具体工具的成功结果。退出码为零不能单独证明专业根因、玩法结果或用户验收。

受信操作者使用 `publish CANDIDATE_ID` 发布，使用 `revoke KNOWLEDGE_ID` 撤销。Runtime 检查证据引用、来源版本和允许的验证范围；它不能证明任意自然语言结论正确。知识审核状态与产品验收状态分别表达。

默认发布创建新条目，同名但适用范围不同的知识可以并存。完全相同的结论复用当前正式条目，来源案例仍单独保留。更新已有知识必须指定 `publish CANDIDATE_ID --knowledge-id KNOWLEDGE_ID --expected-revision N`；版本不符或条目已撤销时拒绝更新，不能仅凭标题覆盖。

使用 `search QUERY` 查询当前已发布内容，使用 `read KNOWLEDGE_ID` 读取条目。撤销后不再返回该知识；当前对话在下一次提示或已覆盖的工具前置事件收到版本失效提示。这不能保证从模型内部删除已经读过的内容。

`knowledge_search` 的 `filters`、CLI 的 `search QUERY --filters JSON` 可按 metadata 中的已知字段精确筛选。自动注入最多三条摘要，保留条目 ID、适用条件和证据等级；长正文用 `knowledge_read` 获取。“继续”等短回复沿用任务查询，压缩后恢复有效摘要。切换任务会重置检索状态，但保留已展示知识的撤销追踪。

使用正式知识后发现反例，可在 `case_feedback` 中关联 `knowledge_id`，形成待审问题；反馈本身不直接撤销知识。

同一对话开始真正的新任务时，使用 `task --new-task`，或 MCP 的 `task_update(new_task=true)` 创建新任务标识。修改现有目标或范围时更新当前任务；仅将状态改为 `delivered` 不应使来源证据失效。已知任务版本时传 `--expected-revision`，避免覆盖其他更新。目标或范围改变才增加任务 revision。

历史案例依据自己的任务快照审核，后续任务不提供历史执行权限。MCP 和 Hook 共用项目身份：仓库子目录使用仓库根目录，已登记工作区的普通子目录沿用该工作区；会话跨到不同项目时给出身份冲突。多目录共享知识可显式配置 `project_aliases`，不自动合并不同仓库。

普通 Agent 将明确任务更新为 `delivered` 后，下一次 `Stop` 为该任务版本保存一次交付结果案例，并附上有限的工具结果。此记录只代表已报告交付，不证明专业结论或用户验收。`capture_deliveries=false` 可关闭该采集；尚未交付的专业问题继续用 `case_feedback` 简短补充，无需生成复盘文档。

## 可选管理员执行器

默认 `admin.enabled` 和 `admin.auto_publish` 都为 `false`。未启用执行器时，可用管理员技能人工整理候选。

需要自动提出候选时，配置可信的 Codex CLI 路径并启用 `admin.enabled`。此时 `Stop` Hook 启动一个独立后台 `admin-once` 短批次，不等待模型完成。它不是常驻守护进程，也不会持续轮询队列。也可以手动运行：

```powershell
python scripts/learning.py --workspace "D:\Projects\my-project" admin-once
```

可用 `admin-once --problem ID` 限定问题。执行器在隔离临时目录使用只读沙箱、临时会话和结构化输出，并设置 `ALP_DISABLED=1` 避免学习递归。自动提出候选不等于自动发布；默认仍由受信操作者发布。

审核快照与调用预留在同一短事务完成，模型调用在事务外执行。审核包包含原任务、实际事件及少量相关知识和候选摘要；相同项目、范围和精确素材可复用调用记录，语义相似仍需判断。预检失败不占模型额度，确定未启动的进程可以在修正配置后重试；已经启动但结果未知的调用不自动重试。截短证据会标明限制并阻止自动发布。

自动批次最多预检十个问题，保存超限预检结果，让后续批次继续处理后面的案例。问题证据版本、输入预算或审核提示变化后，旧预检失效；显式指定问题会重新检查。超限案例保持待处理并返回跳过的 ID，不占模型调用额度。

`max_calls_per_day` 限制调用次数，`max_input_chars` 限制输入字符，`timeout_seconds` 限制单次时间。字符和次数不是 token 或费用，本工具不保证严格的 token 或美元预算。关闭或失败时保留待处理记录。

是否降低消耗，需要结合实际调用和任务结果衡量。

`status` 提供检索次数、命中次数、注入条目次数与管理员状态计数；检索统计含自动注入和管理员查询。管理员记录真实耗时及 CLI 实际上报的 token 数字，缺失时不估算为零。命中和注入不代表已采用，实际节省的 token 和任务质量仍需使用反馈验证。

真实付费模型调用与宿主端 Hook 启用须单独验收；本地离线检查不代表这两项已通过。

## 可选 Hindsight

默认使用本地 SQLite 全文检索。Hindsight 是可选的已发布知识同步目标，不是本地记录成功的前提，也不会自动成为 Codex 内置资料库。

启用前须配置 URL、Bank 和密钥环境变量，并在 Hindsight 端预先设置相应的 chunks 命名策略。默认 `strategy` 为 `alp_chunks`；每次新客户端写入前实际核对目标策略。旧配置里的 `chunks_strategy_verified` 不作为验证依据。

```powershell
python scripts/learning.py --workspace "D:\Projects\my-project" sync
```

同步不将候选案例自动晋升为正式知识。适配器会只读核对目标 Bank 的 chunks 命名策略，并要求关闭自动综合，避免同步意外触发额外分析；它不修改 Bank 配置。

同步失败与本地保存状态分开记录；本地仍可使用。崩溃后过期的同步租约标为待核对，不自动重投。核实远端状态后，可用 `sync --retry OPERATION_ID` 显式重试，最多三次；原目标配置变化会阻止重投，不能自动迁到新 Bank。

`sync --backfill` 将启用前的当前正式知识补入队列并同步一项，重复补入不会创建相同目标和修订的重复任务。调整连接超时不改变目标身份；修改 URL、Bank、策略或密钥环境变量名仍须重新核对。失败保留安全的错误分类和写入阶段，区分尚未发送与远端结果未知；失去租约的旧进程返回 `lease_lost`，不宣称完成。本地撤销不删除已同步的远端副本；当前检索使用本地 active 清单，尚未接入远端召回。

## 自动化的实际边界

- 注入量由 `injection_max_chars` 限制，事件投影由 `event_max_chars` 限制；这些是字符限制。
- 宿主 Hook 的 `additionalContextLimit` 使用近似 token 阈值，当前设为 2500；它与上述字符预算单位不同。显著提高注入字符预算时应核对宿主预览行为，具体机制见 [官方 Hook 说明](https://learn.chatgpt.com/docs/hooks)。
- `enforce_task_policy` 默认关闭。启用保护时，应明确允许工具和当前任务边界；任务未知、已取消、已交付或中断时，覆盖的项目工具入口会拒绝调用，任务更新与查询入口仍可用。保护只覆盖实际接入并支持阻止动作的路径。
- 任务目标或授权未知时，不推断为全权执行。用户修正后，以最新任务版本为准。
- 知识内容中的命令不能获得执行权限。项目主流程、UE、PIE、训练和用户验收遵守项目自身约定。
- 技能和检索提示属于软约束。证据引用、版本与服务角色可由程序校验；任意专业结论和全部模型遗漏不能由本工具保证。
- 不为经验留痕自动生成项目复盘文件，也不重写现有项目文档。

## CLI 入口

用 `python scripts/learning.py --help` 查看实际参数。全局参数 `--home`、`--workspace` 放在子命令之前。

| 子命令 | 用途 |
|---|---|
| `status` | 读取运行状态 |
| `task` | 保存明确任务，支持 `--expected-revision` 检查版本和 `--new-task` 开始新任务 |
| `feedback` | 反馈问题及结果，支持重复 `--event-ref` |
| `problems`、`packet` | 列出问题、读取审核包 |
| `propose` | 提交结构化候选 JSON |
| `publish`、`revoke` | 本地受信发布入口 |
| `search`、`read` | 查询和读取正式知识 |
| `admin-once` | 有预算地处理一个管理员批次 |
| `sync` | 同步已发布知识到可选 Hindsight |
| `serve` | 启动指定角色的 MCP 服务 |
| `install` | 创建待启用的独立安装副本 |
