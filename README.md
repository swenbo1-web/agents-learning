# Agent Learning Plugin

面向项目 Agent 的本地经验记录、知识检索与候选审核工具。当前版本 0.10.0，使用 Python 3.10+ 和 SQLite，默认不调用付费模型，不连接 Hindsight，不自动发布知识，不启用后台调度。

0.10.0 完善第八模块的持久调度、全局准入预算、调用用量账本和知识采用回执，保留前七模块契约。数据库首次打开时将第八模块表在同一事务升级到版本 10，保留记录和知识 ID；不补造旧知识采用或历史用量。全文索引版本为 4，内部发布协议为 7，不能将新旧运行文件混用；旧运行端不能打开版本 10 数据库。常见凭证脱敏不能保证抹除已经存在的副本。

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

`install` 先验证资源，在临时目录完成复制后原子发布。MCP 和 Hook 均指向同一份固定运行副本，不依赖导出目录或宿主缓存中的另一份代码。发布清单记录版本、协议和逐文件摘要，入口核对内容一致性；这不是数字签名或操作系统权限隔离。它不更改既有 Codex 配置。导出副本含本地插件目录，随后运行：

```powershell
codex plugin marketplace add "D:\Plugins\agent-learning"
codex plugin add agent-learning@agent-learning-local
```

在 Codex 中开启新对话，审阅并信任 Hooks（CLI 可用 `/hooks`）。安装器不会代替用户设置 Hook 信任。固定运行副本默认位于 Windows 的 `%LOCALAPPDATA%\AgentLearning\runtime-releases` 或 POSIX 的 `~/.local/share/agent-learning/runtime-releases`。保留这个目录和对应解释器；移动导出源不影响已生成的运行入口。运行目录可通过 `install --runtime-store PATH` 或 `ALP_RUNTIME_STORE` 指定，不随临时 `ALP_HOME` 改变。Hook 的信任规则见 [官方插件说明](https://developers.openai.com/plugins/build/plugins)。

需要在配置损坏时也拒绝覆盖工具调用，导出时显式使用 `install PATH --hook-mode enforce`，将保护模式写入可信入口。`--hook-mode observe` 固定观察模式；省略时依据当前有效配置。更换入口命令后需由宿主重新审阅 Hook 信任。

源码中的 `.mcp.json` 是模板，安装副本会将路径渲染为实际绝对路径。本机已用隔离配置验证 Codex 原生加载与安装命令，未改当前用户的插件配置。

标准 wheel 包含导出资源，也支持 `python -m agent_learning.cli` 入口。运行时、CLI 和 MCP 均使用 UTF-8；无需把中文请求转义成 ASCII 来绕过系统编码。

也可以单独启动 stdio MCP 服务：

```powershell
python scripts/learning.py --workspace "D:\Projects\my-project" serve --role worker
python scripts/learning.py --workspace "D:\Projects\my-project" serve --role admin
```

| 身份 | MCP 工具 |
|---|---|
| 普通 Agent | `knowledge_search`、`knowledge_read`、`session_context`、`task_update`、`case_feedback`、`learning_status` |
| 管理员 | 上述工具，以及 `problem_list`、`review_packet`、`submit_candidate`、`review_list`、`propose_governance` |

MCP 不暴露发布或撤销工具。角色由启动入口指定，工具参数不能提升角色。技能位于 [learning-worker](skills/learning-worker/SKILL.md) 和 [learning-admin](skills/learning-admin/SKILL.md)。

这些权限限制覆盖本服务的入口。同一操作系统账户下的任意 Shell 和本地数据库访问，不属于 MCP 角色隔离保证；需要宿主权限与账户隔离配合。

### 会话身份与压缩恢复

`SessionStart` 和 `UserPromptSubmit` 注入 `context_handle=...`。普通 Agent 首次调用 `session_context(context_handle=...)`，或直接在 `task_update`／`case_feedback` 传这个句柄；之后当前 MCP 连接固定绑定该句柄，`conversation_id` 可省略。传入其他会话或句柄会被拒绝。写入身份和业务更新在同一事务内复核，失败不会留下错误项目绑定。

压缩后用 `session_context` 恢复任务、revision、write_version、状态和执行准备情况，无需重新总结历史。没有可信句柄时只能查询项目知识和项目状态，不能猜最近会话或登记任意会话。受信本地操作者可使用 `session-bind --conversation ID` 登记明确身份，再用 `serve --context-handle HANDLE` 启动绑定服务。手动 CLI 的 `task`／`feedback` 仍是显式本地操作入口。

`SubagentStart` 有可靠 `agent_id` 时，子 Agent 获得独立句柄、知识展示状态和固定 assignment，绑定派发时的父任务 ID、revision、状态版本与执行轮次。父任务改变后，旧反馈仍记入原任务，旧 assignment 不能使用新许可；`session_context` 同时返回派发任务和当前父任务，不能覆盖父任务。无可靠 ID 或升级前无 assignment 的子身份不猜归属，需要新的子 Agent 身份重新派发。若宿主让父子 Agent 共用一个已绑定的 MCP 连接，子 Agent 需要单独的绑定连接；当前不声称已验收宿主的父子 MCP 连接生命周期。

### Codex／ChatGPT 登录账户切换

同一项目、同一数据目录继续共享正式知识和历史证据，不按登录账户分库。换号通知推进本地 `host_epoch`，旧连接、旧句柄和旧管理员回写失效；新连接通过下一次 Hook 或明确的 `session-bind` 重新绑定。任务历史和每日调用计数不会清零，未确定结果的旧调用不会自动重试，已运行的项目命令也不会被当作重新执行的理由。

受信宿主适配器或操作者在确认切换后调用：

```powershell
python scripts/learning.py account-changed --notification-id "unique-login-event-id"
```

可用唯一通知 ID 去重，省略时每次调用均视为一次新边界。所有通知方必须指向相同 `ALP_HOME`／`--home`。这个入口不读取、保存或修改登录 token，不执行登录、退出或额度重置。

**当前没有自动订阅桌面端换号。** 官方 app-server 提供 `account/updated` 和登录完成通知，但普通插件不能据此假定已经接入桌面应用的内部通知通道。适配器应依据自己拥有的登录生命周期发出边界通知；仅比较 `authMode`／套餐不能识别同模式下的 A→B 换号，也不依赖 `auth.json` 修改时间。[官方 app-server 账户接口](https://learn.chatgpt.com/docs/app-server)

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
| `knowledge_kind`（可选） | `fact`（事实）、`solution`（具体解法）、`pattern`（可复用方法），默认 `solution` |

`reported` 表示有记录的陈述，不代表已经验证。`tool_success` 要求引用明确完成且成功的 `PostToolUse` 事件，只能支持具体工具的成功结果。标准 MCP 的 `isError:false` 记为 `call_completed`，仅表示调用协议完成，不能单独支持 `tool_success`。退出码为零不能单独证明专业根因、玩法结果或用户验收。

受信操作者使用 `publish CANDIDATE_ID` 发布，使用 `revoke KNOWLEDGE_ID --expected-revision N` 撤销。Runtime 检查证据引用、来源版本和允许的验证范围；它不能证明任意自然语言结论正确。知识审核状态与产品验收状态分别表达。

默认发布创建新条目，同名但适用范围不同的知识可以并存。相同正文、适用条件、限制、分类、用途和证据等级复用当前条目，并持久化每个来源候选、问题修订和证据引用；新增来源不制造正文修订。更新已有知识必须指定 `publish CANDIDATE_ID --knowledge-id KNOWLEDGE_ID --expected-revision N`；版本不符或条目已撤销时拒绝首次更新，不能仅凭标题覆盖。更新后若与另一个有效 ID 完全重复，会拒绝并提示已有条目。

每个候选只有一个发布回执。默认重试或使用原目标/CAS 参数重试显式更新时，直接返回第一次发布结果，不再次校验证据或推进版本；回执中的 `published` 表示历史发布已经完成，当前可用性用 `read` 核对。旧候选不能回滚较新知识，也不能复活已撤销知识。变更目标需提交新的候选。迁移仅恢复有明确对应依据的历史回执；对应目标无法确定时保留 `legacy_unresolved`，阻止自动重发并等待复核新候选。

显式恢复使用 `restore KNOWLEDGE_ID --expected-revision N`，只允许恢复末版、核对当前至少一份完整有效来源，并检查有效知识重复；可附 `--expected-generation N`。涉及未决或因撤销关闭的反例时，另需具体 `--reason` 与新的 `--event-ref` 支持：历史来源、反例自身、已绑定其他知识的观察不能作为恢复依据。恢复同版后重新打开原 revoked 反例，保留 dismissed/revised，不宣称反例已经解决。恢复保留原正文修订并降为 candidate。

发布、修订、附加来源、撤销、恢复和晋升保存统一生命周期动作及状态版本。撤销／恢复可传 `--request-id`，同一参数重试回放历史结果，不再次变更状态；原请求 ID 改参数会被拒绝。无请求 ID 会生成新 ID，响应给出该 ID。`knowledge-actions` 列有限摘要，`knowledge-action REQUEST_ID` 读取理由、状态和回执。历史发布回执继续保留；记录中的执行者类别不提供操作系统身份认证。

使用 `search QUERY` 查询当前已发布内容，使用 `read KNOWLEDGE_ID` 读取条目。撤销后不再返回该知识；当前对话在下一次提示或已覆盖的工具前置事件收到版本失效提示。这不能保证从模型内部删除已经读过的内容。

`knowledge_search` 的 `filters`、CLI 的 `search QUERY --filters JSON` 可按 `category`、`knowledge_kind` 及 metadata 中的已知字段精确筛选，未知筛选字段省略。自动检索可使用任务 scope 中明确的 `knowledge_filters` metadata 字段，不把允许工具或执行规则当成筛选依据。自动注入最多三条摘要，保留分类、条目 ID、适用条件、证据等级与当前来源状态；条件无法放入预算时退回 `read_required` 标识卡。匹配不足或来源冲突、拒绝、缺失时不自动注入。

MCP `knowledge_search` 默认返回最多 6000 个 JSON 正文字符的预览，`max_chars` 可设 512～20000，不展开完整正文或全部证据；返回 `match`、`summary_only`、`truncated` 和 `omitted_count`。`knowledge_read` 默认预算为 12000，可设 1024～50000；正文按字符 `offset` 分页，`truncated=true` 时继续传 `next_offset` 与上一页 `revision` 作为 `expected_revision`，防止跨修订拼接。来源只展示有限摘要，裁剪字段与省略来源都有标记。这些预算不包含 MCP 协议外壳及协议返回的 `structuredContent` 副本，不是精确 token 限制。CLI 与内部 Runtime 的 `search/read` 保留全文和证据接口。

搜索和读取可以传当前 `context_handle`，连接绑定后登记实际返回的知识卡或正文页；未绑定查询只读取项目知识，不猜会话归属。父/子 Agent 的展示台账分别保存。`source_version` 与实际展示状态同快照返回，读完后发生的变化不会被误记为已经通知。

“继续”等短回复沿用查询；任务、知识、来源版本及预算不变时复用查询计划，不重复检索。负结果随新知识发布失效，放不下的卡可以后续补发。压缩恢复优先当前问题的新命中，再补充有序的有效历史摘要；当前恢复集合保留最近 32 项，历史展示由独立台账追踪。切换任务或账户会重置当前集合和查询计划，同时保留历史失效追踪与项目共享知识。

正文与 `sources`、`evidence` 在同一 SQLite 快照中读取。`source_health` 可为 `valid`、`conflicted`、`denied` 或 `missing`；`current_tool_success` 表示当前引用中仍有可信的工具成功观察，`problem_changed` 单独提示来源问题已有新证据。历史 `validation_level` 不随回调变化改写，冲突由管理员复核，存储层不自动撤销专业结论。

全文投影只包含当前有效知识，支持 C++、C#、C、R、中文单字与二元片段，并检索限制条件。技术标识同时保留整体和组件；查询取末尾 8000 字符、最多 128 个有效词元，中文双字优先并抑制常见请求修饰词。FTS 召回后优先查询覆盖率，每个词只取最高字段权重，避免重复字段刷分，以 ID 打破并列。返回的 `match` 说明分数、覆盖率、最多 12 个命中词与自动注入资格；分数不是概率。单字符查询保留手动召回，不自动注入。排名不使用其他项目或历史知识的词频。

缓存仅保存 ID/修订组合，每次读取核对 active、筛选条件与词元交集；返回的 generation、正文与来源属于同一快照。项目变化通过自身 generation 失效旧缓存，不清空其他项目缓存；全局缓存最多保留 256 份。缓存和计数在读快照结束后写入，派生写入被锁住时可跳过，不阻断已完成的查询。Hook 检索不持写锁，最终状态提交重新核对宿主身份、任务/assignment、上下文版本、有效修订及来源版本。

全部已展示知识的来源状态由版本化台账追踪；问题版本、证据关联、操作冲突/拒绝或新增来源在同一事务内提升受影响知识的来源版本。Hook 每次最多处理 16 条撤销和 16 条来源变化，只有完整通知放入输出后才推进状态，预算不足或超批次数量留给后续事件；近期八份状态仅作兼容摘要。程序输出不等于模型已阅读，也不能清除模型内部旧内容。

实际工具失败会登记绑定任务合同与宿主版本的补查，每个任务合同、Agent 和宿主版本最多处理三个不同错误签名；重复回调、插件自身工具或无可验证任务不会触发。下一次续聊、恢复或工具前置事件进行本地检索；普通新提示优先查询当前问题，旧故障留到后续事件。预算不足时缓存有限引用，后续补发不重新搜索。晚到的旧任务失败不会混入新任务，子 Agent 按固定 assignment 处理。补查不调用管理员模型，也不新增执行权限。

## 按需存储维护

以下命令只在迁移、故障诊断或人工维护时使用，不加入每个任务的固定流程：

```powershell
python scripts/learning.py --home "D:\AgentLearningData" doctor
python scripts/learning.py --home "D:\AgentLearningData" backup "D:\Backups\agent-learning-2026-10-11.sqlite3"
python scripts/learning.py --home "D:\AgentLearningData" rebuild-index
```

`doctor` 检查数据库与发布/来源/索引关系，返回异常计数；`backup` 使用 SQLite 在线备份，目标必须是新文件；`rebuild-index` 在写事务内从有效正文重建投影并清缓存。索引可重建，正文、历史、回执和来源属于权威数据，重建索引不能修复缺失的原始证据。打开旧数据库前可先用原运行端做在线备份；本插件不自动覆盖数据库或执行备份还原。

使用正式知识后发现反例，可在 `case_feedback` 中关联 `knowledge_id`，形成待审问题；反馈本身不直接撤销知识。

同一对话开始真正的新任务时，使用 `task --new-task` 或 `task_update(new_task=true)`。已有任务的 MCP 更新必须传 `expected_task_id` 和 `expected_write_version`；仅比较 `expected_revision` 不能保护状态写入或新任务 revision 重用，因此不再接受旧的单字段更新。显式本地 CLI 可以省略并发条件；提供条件时也必须成对传入。目标或范围改变才增加 `revision`，每次实际状态/目标/执行边界变化增加 `write_version`，回执在同一事务内读取。

省略 `status` 保留状态；暂停、取消或已交付任务恢复需明确 `resume=true`／`--resume`，并不会恢复过期执行许可。父 Agent 的 `case_feedback` 同时传 `expected_task_id` 与 `expected_task_revision`，避免压缩前的反馈写进新任务。子反馈使用固定 assignment。合同快照与事件时点状态分别保存，后续交付不改写早期案例；管理员也不会因此重复预检未变的素材。

历史案例依据自己的任务快照审核，后续任务不提供历史执行权限。MCP 和 Hook 共用项目身份：仓库子目录使用仓库根目录，已登记工作区的普通子目录沿用该工作区；会话跨到不同项目时给出身份冲突。多目录共享知识可显式配置 `project_aliases`，不自动合并不同仓库。

普通 Agent 将明确任务更新为 `delivered` 后，下一次 `Stop` 按该次 delivered 的 `write_version` 保存交付结果，并附有限工具结果及对应前置记录。同轮重复 Stop 不重复采集；明确恢复后再次交付，追加到该合同已有案例，保留首次结果。旧交付轮次仅在 Stop 与历史状态均有明确依据时迁移。此记录只代表已报告交付，不证明专业结论或用户验收。`capture_deliveries=false` 可关闭该采集；专业问题继续用 `case_feedback` 简短补充，无需生成复盘文档。

## 案例采集与证据

工具返回先检查明确失败，再判断成功、协议完成或运行中；零退出码和会话 ID 不能覆盖失败标记。前后回调明确提供不同输入时记为冲突，完整脱敏输入签名独立存储，旧截短输入不猜。事件预算优先保留退出码、失败标记、错误码和冲突信息，其余日志保留首尾并标记截短。

失败案例关联该操作的前置记录与结果，保留实际命令。潜在后续成功限定任务、Agent、assignment、宿主轮次，并比较已知动作目标；修复命令可以不同，明确不同文件或资源不自动串联。已有案例审核后仍可补充真实新证据。`problem_events.role` 区分 observation、attempt、failure、conflict、potential_recovery、delivery、feedback；`status` 表示治理状态，`resolution_status` 为 open 或 recovery_observed，后者不等于修复验证通过。同类问题按确定的摘要身份归并，语义聚类仍由管理员判断。

`case_feedback` / CLI `feedback --idempotency-key KEY` 支持持久回执。同一次请求重试沿用原键和参数，返回首次结果，不增加事件或案例版本；同键不同参数拒绝。不同实际发生使用新键，省略键兼容旧入口且不保证重试去重。回执按项目、会话、Agent 和宿主版本隔离。

知识反例传 `knowledge_id`、`knowledge_revision`、`feedback_kind=counterexample`，必须引用本项目真实修订；兼容旧请求未传修订时绑定当前有效版并返回明确修订，已撤销或历史版应显式引用。反例独立关联对应知识修订并推进来源变化版本；`pending_counterexample_count` / `feedback_status=pending_counterexample` 会出现在知识响应和后续 Hook 提醒中。案例 reviewed、生成候选、发布新知识或挂接等价来源都不清除反例。准确修订被受信治理更新、撤销或依据额外真实证据明确驳回后才处置；新证据会重新打开反例。

## 任务范围与执行保护

`scope` 的已知字段会检查类型；项目附加字段仍可保存，但不提供权限：

| 字段 | 类型与含义 |
|---|---|
| `mode` | `analysis`、`read_only`、`implementation`、`execute` |
| `allowed_tools`、`allowed_paths`、`allowed_actions` | 字符串数组；动作是 `read/write/execute/network`，显式空数组限制为零 |
| `budget` | `{remaining: 非负数或null, unit: 可选字符串}`，0 阻止执行，null 表示未知 |
| `stop_conditions`、`verification` | 字符串数组，保存停止条件和验收要求，尚不自动解释任意自然语言条件 |

保护模式要求受信操作者或宿主先签发规则；普通 Agent 更新任务不能给自己签发许可。操作者将规则保存为 JSON 文件，并用当前任务回执中的真实 ID 和 write_version 调用：

```powershell
python scripts/learning.py --workspace "D:\Projects\my-project" execution-approve --conversation demo --expected-task-id "真实任务ID" --expected-write-version 1 --rules "D:\Policies\read-only.json"
```

规则文件示例（工具名和参数字段必须对应接入宿主的真实契约）：

```json
{
  "mode": "read_only",
  "allowed_tools": ["Read"],
  "tool_rules": {"Read": {"action": "read", "path_fields": ["file_path"]}},
  "allowed_paths": ["Docs"],
  "budget": {"remaining": null}
}
```

审批时固定 workspace 和许可资源根；工具资源相对实际 cwd 解析，再与固定根、任务 scope 和 assignment 范围取交集。缺少字段、越界、含 `..` 或符号链接逃逸时拒绝。有限的结构字段检查不能证明任意工具的全部副作用；任意 Shell 在只读模式下拒绝，不靠解析命令猜权限。FPS 等项目的 UE/PIE/训练动作需要项目适配器声明，不硬编码进通用插件。

新用户消息使旧执行轮次失效，完全匹配的“继续／继续吧／接着／continue／go on”仅保留已有范围，不扩大许可；无需额外模型分类。重复带同一 turn_id 的消息不重复失效。`Interrupt` 暂停父任务而非取消；清空上下文也暂停，压缩恢复保留现有版本。任务修正、规则替换和换号会失效旧许可，普通 Agent 不能靠恢复状态绕过。保护模式对其他新消息保守要求受信入口重新确认；自动接线需宿主适配器支持，目前 CLI 是可用的手动入口。

`PreToolUse` 记录许可决定，`PostToolUse` 记录观察结果，二者分别保存。已拒绝却收到完成、同一操作有矛盾终态等回调标为 conflict，不能晋升为 `tool_success`；这是异常回调处理，不代表真实宿主已经出现这类回调。明确的 Pre 绑定保留晚到结果；无 Pre 且无 assignment 的结果保持无任务归属。当前仍不能撤回已运行的外部命令，也没有完整执行租约。余额仅消费第八模块/宿主提供的快照，尚未实现原子扣费或动态资金预算。

## 可选管理员执行器

默认 `admin.enabled` 和 `admin.auto_publish` 都为 `false`。未启用执行器时，可用管理员技能人工整理候选。

需要自动提出候选时，配置可信的 Codex CLI 路径并启用 `admin.enabled`。未启用调度时，`Stop` Hook 先持久预留派发位置，再启动一个独立后台 `admin-batch` 短批次，不等待模型完成。没有可审增量、已有派发或审核在途、额度耗尽时不启动。启用 `scheduler.enabled` 后由下述持久调度安排有界续跑。也可以手动运行：

```powershell
python scripts/learning.py --workspace "D:\Projects\my-project" admin-once
python scripts/learning.py --workspace "D:\Projects\my-project" admin-batch
python scripts/learning.py --workspace "D:\Projects\my-project" reviews --problem CASE_ID
```

可用 `admin-once --problem ID` 限定问题。执行器在隔离临时目录使用只读沙箱、临时会话和结构化输出，并设置 `ALP_DISABLED=1` 避免学习递归。自动提出候选不等于自动发布；默认仍由受信操作者发布。

审核快照与调用预留在同一事务完成，模型调用在事务外执行。同一项目的模型预留在事务内互斥，全局并发默认为 1。调用持久经历 reserved → launch_intent → started → 终态，启动意图在创建进程前提交；超时、换号或启动窗口崩溃按未知保留额度。判断全局预算之前安全回收全部项目的过期预留，严格启动意图前的记录或明确未启动的进程可以释放；遗留活动记录不能证明未启动，升级后按 orphaned_unknown 处理。不会按进程名称杀其他任务。

审核决定与调用计量分开保存：`reviews` / 管理员 `review_list` 可查看理由、结构化缺证项、候选和提议引用。候选对应 candidate_ready，缺证对应 awaiting_evidence，生成提议对应 proposal_ready；与案例是否恢复、产品是否验收独立。精确复用同时检查业务证据及实际比较知识、候选、反例目标、来源健康和审核策略；失败或未知调用不复用为成功决定。自身证据产生的候选不形成再次审核循环，新相关资料才可能复审。发布或候选变化先唤醒有界比较，实际审核签名不变则不调用模型。

审核结果写入前及最终提交边界，在同一事务重核案例、宿主、租约及实际比较资料；变化则回滚候选、治理提议和决定回执，保留实际调用终态与用量。未知调用核对后，可由受信入口运行 `admin-retry CALL_ID --reason "已核对的原因"` 明确允许一次再审，原记录和额度保留。重复许可不会产生无限重试。截短证据会标明限制并阻止自动发布。

直接 MCP / CLI 取包同样使用一致快照。整包按实际管理员提示、任务、证据与比较材料计算预算，完整可容纳的 41 条事件可审核，不再固定丢弃第 41 条。单页最多读取 512 条，事件展示必要归属、角色及当前 health；比较资料只使用剩余空间。预算不足明确返回 incomplete、omitted_count、omission_reasons 和有限 missing_refs，不完整材料仍不自动调用模型。

管理员可用 `review_packet(problem_id, offset, expected_revision, max_chars, section)` 或 `packet ID --offset N --expected-revision R --max-chars N` 分页；续页必须沿用同一问题 revision，素材变化则重读。大任务完整 JSON 使用 `section=task`；反例的准确历史知识必读，不依赖关键词重合，即使已撤销仍可查。目标放不下时明确 target_omitted 和 incomplete，可使用 `section=target` / `--section target` 分页。若单条证据本身放不下，按返回的预算提示增加 max_chars；不得反复读取无进展的页。分页材料及当前健康状态不等于新的独立验证。

`admin-once` 一次最多扫描十个问题；`admin-batch` 默认总共扫描 20 个案例、最多启动 2 次审核、运行预算 180 秒，同时受全局 UTC 每日调用额度限制。预检和精确去重不消耗模型调用数，超限预检持久缓存并绕过阻塞项。问题版本、必要目标、预算或提示改变后可重检；可选候选变化不重新展开未变超限素材。无工作退出，坏启动配置停止当前批次。执行时间包含模型等待，进程清理和数据库收尾可能略超运行预算。

管理员 MCP 的 `propose_governance` 只能提出 publish/update/attach/revoke/dismiss_counterexample，不能执行。受信操作者用 `governance-propose FILE` 提交 JSON、`governance-execute PROPOSAL_ID` 执行：校验案例及目标修订、证据和宿主边界，治理变更与幂等回执同事务提交。update 必须产生真实新修订，等价内容应 attach；发布不同知识不处置旧反例。模型输出中的 proposal 同样只作为提议。

即使开启 `auto_publish`，也仅限无截短、具备真实工具成功证据的 operation/solution 新知识。已有知识治理、反例、professional、pattern 和未验证事实仍交受信入口处理；工具成功不证明专业结论正确。

`max_calls_per_day` 限制调用次数，`max_input_chars` 限制输入字符，`timeout_seconds` 限制单次时间；`max_cases_per_batch`、`max_calls_per_batch`、`max_batch_seconds` 控制批次。字符和次数不是 token 或费用，本工具不保证严格的 token 或美元预算。关闭或失败时保留待处理记录。后台摘要日志超过 1 MiB 后在下次派发轮换，保留一份旧日志；Windows活动句柄导致轮换失败时记录延迟并继续派发。单次审核日志受超时约束，不是磁盘写入硬上限。当前清理只保证直属审核进程，CLI自行启动的后代进程仍需宿主级约束。

是否降低消耗，需要结合实际调用和任务结果衡量。

`status` 提供检索次数、命中次数、注入条目次数、管理员状态、全局预算、调度等待原因和 `knowledge_sync_dirty` 数量；检索统计含自动注入和管理员查询。累计检索计数是运行观察，不作为计费账本。知识采用与用量完整度见第八模块。

真实付费模型调用与宿主端 Hook 启用须单独验收；本地离线检查不代表这两项已通过。

## 第七模块：成熟度与知识发布批次

`maturity` 与 active、正文 revision、validation_level 分别表达。`knowledge-maturity ID --expected-revision N` 给出当前 candidate/stable、资格及缺项；`knowledge-promote ID --expected-revision N --request-id REQUEST` 由受信入口晋升，不暴露到普通或管理员 MCP。当前默认规则仅允许 operation/solution：至少两个独立任务（同任务的重复案例不增加次数；无任务时按独立对话）、每份来源仍有真实工具成功且合同有效、来源无变化或冲突、无未决或原撤销反例。professional/pattern 需要专门验证规则，当前不会仅凭工具成功晋升。

Stable 认证绑定正文、来源与反例状态，变化后 effective maturity 返回 candidate；新修订、附加来源、撤销／恢复需重新晋升。读取、MCP 有限预览与 Hook 摘要显示成熟度，已展示知识的成熟度变化通过现有通知链提示。历史认证不证明产品已由用户验收。

`knowledge-release-prepare` 保存紧凑清单，引用 ID、正文修订、来源／生命周期版本、成熟度、健康和摘要哈希，不复制正文或日志。`knowledge-release-commit RELEASE_ID` 在同一事务内重核有效知识全集、来源快照和父批次，变化则拒绝并要求重新准备。完成批次不可改写，重复提交回放结果；`knowledge-releases` 和 `knowledge-release ID --offset N --limit N` 分页读取摘要及 added/updated/removed 差异。

`knowledge-daily-release` 将准备／提交组合成显式的一步入口，有变化才发布，无变化跳过，也不新增管理员模型调用。操作者可显式调用，也可启用第八模块的 `scheduler.auto_release` 安排正式知识批次；不会将候选自动转为正式知识。

`knowledge-release-rollback ID --expected-head CURRENT --request-id REQUEST` 只回退分发清单：历史目标必须仍为当前有效修订、来源有效且无反例，保存新的回退批次；不会复活撤销知识、倒退正文或撤销后来新增知识。当前 Runtime 检索仍使用本地 active，批次指针尚未作为检索可见性过滤器。知识批次与 `release.py` 管理的软件运行副本分别维护。

## 可选 Hindsight

默认使用本地 SQLite 全文检索。Hindsight 是可选的已发布知识同步目标，不是本地记录成功的前提，也不会自动成为 Codex 内置资料库。

启用前须配置 URL、Bank 和密钥环境变量，并在 Hindsight 端预先设置相应的 chunks 命名策略。默认 `strategy` 为 `alp_chunks`；每次新客户端写入前实际核对目标策略。旧配置里的 `chunks_strategy_verified` 不作为验证依据。

```powershell
python scripts/learning.py --workspace "D:\Projects\my-project" sync
```

同步不将候选案例自动晋升为正式知识。适配器会只读核对目标 Bank 的 chunks 命名策略，并要求关闭自动综合，避免同步意外触发额外分析；它不修改 Bank 配置。

同步失败与本地保存状态分开记录；本地仍可使用。崩溃后过期租约清除并标为远端未知，迟到结果单独留回执，不能覆盖待核对状态。`sync --retry OPERATION_ID` 显式重试：确定尚未发送的预检失败不消耗实际发送次数；未知写入先对账，最多三次实际尝试。后续 not_sent 失败不能清除此前 unknown，原目标配置变化也不能自动迁到新 Bank。

同步载荷含正文修订、来源状态版本、成熟度、健康／反例摘要和 active 状态；完整来源参与摘要哈希，输出只携带有限引用。来源／处置变化由程序登记增量，同一知识、修订和目标的确定未发送旧状态可被新状态替代，未知操作仍保留对账。旧版失效记录与新版有效记录分别发送。远端文档按不可变状态摘要区分，不使用本地回执声称远端恰好一次。

`sync --backfill` 补入各知识末版（含撤销状态）并同步一项，相同目标和状态不重复排队。`sync-list` / `sync-read OPERATION_ID` 提供有界操作 ID、目标摘要、次数、诊断和回执；`sync-batch --max-items N --max-seconds S` 只处理 pending，blocked 不自动重试。预算限制新操作并收紧 HTTP 超时，不保证强制取消已在运行的请求。调整连接超时不改变目标身份；目标变化须显式回填或实际新状态变更。成功响应只保留核实字段。当前没有常驻分发进程，本地撤销不删除远端旧副本，Runtime 尚未接入远端 recall。

## 自动化的实际边界

- 注入量由 `injection_max_chars` 限制，事件投影由 `event_max_chars` 限制；这些是字符限制。
- 宿主 Hook 的 `additionalContextLimit` 使用近似 token 阈值，当前设为 2500；它与上述字符预算单位不同。显著提高注入字符预算时应核对宿主预览行为，具体机制见 [官方 Hook 说明](https://learn.chatgpt.com/docs/hooks)。
- `enforce_task_policy` 默认关闭。启用保护时，项目工具必须有当前受信执行策略并满足状态、动作、资源与余额限制；普通学习 MCP 工具可用于恢复状态，不能签发许可。已确认保护模式的 `PreToolUse` 遇输入、配置、发布或数据库故障会拒绝；坏配置且未固定模式时只能报告保护未知。
- Hook 未被信任、进程无法启动、宿主超时、Hosted WebSearch 或未再触发 `PreToolUse` 的 `write_stdin`，不在上述拒绝保证内。当前不停止外部已运行的工具进程。[官方 Hook 覆盖说明](https://learn.chatgpt.com/docs/hooks)
- 子 Agent 知识展示按可靠 `agent_id` 分开；无该字段的工具事件不能保证及时通知仅对子 Agent 展示的知识撤销，读取正文仍核对当前有效版本。
- 任务目标或授权未知时，不推断为全权执行。用户修正后，以最新任务版本为准。
- 知识内容中的命令不能获得执行权限。项目主流程、UE、PIE、训练和用户验收遵守项目自身约定。
- 技能和检索提示属于软约束。证据引用、版本与服务角色可由程序校验；任意专业结论和全部模型遗漏不能由本工具保证。
- 不为经验留痕自动生成项目复盘文件，也不重写现有项目文档。

## 第八模块：调度、准入预算与效果回执

`scheduler.enabled` 默认关闭。启用后，`Stop` 和 `SessionStart` 检查持久待办；有近期可执行工作才派发独立有限 runner，同一数据目录只有一个所属调度执行器。待办按项目及 admin/release/sync 合并，保存 `next_due`、等待原因、租约、最近执行与有限摘要，不复制案例或日志。项目轮转和全局调用并发限制减少单项目挤占。

`scheduler-tick` 执行一轮，`scheduler-run` 在 `max_run_seconds` 内继续处理后续批次。单批达到调用上限后保存下一次执行时间；日额度不足等下一 UTC 周期，未变缺证等材料变化，未知调用及 blocked 同步仍待显式核对。启动失败采用有限退避。runner 退出后待办继续保留，后续 Hook 或显式命令可以续跑；本插件没有安装操作系统定时服务，不能保证退出后全天离线唤醒。

```powershell
python scripts/learning.py --workspace "D:\Projects\my-project" scheduler-status
python scripts/learning.py --workspace "D:\Projects\my-project" scheduler-tick --max-jobs 3 --max-seconds 60
python scripts/learning.py --workspace "D:\Projects\my-project" scheduler-run --all --max-seconds 300
```

`--all` 仅处理同一数据目录已经登记的项目。`scheduler.auto_release` 默认关闭，启用后按 `release_interval_seconds` 打包已正式知识；无变化跳过，容量不足保留诊断。`scheduler.sync_enabled` 默认关闭，并且必须同时启用 Hindsight 才分发。调度不替代治理授权，不执行管理员提出的撤销、修改或反例处置。

管理员预算独立于普通任务的余额快照和程序批次时长。`admin.max_concurrent_calls` 默认 1；`admin.max_reported_tokens_per_day` 默认 null，设置后按 `token_reservation_per_call` 先预留，再依据完整的 input+output 上报结算，缓存输入属于输入量而不重复相加。未知或部分上报保留预留，已知超量仍展示，不因失败、重试或换号清零。该参数是上报用量的准入门槛，执行器没有严格输出上限时，单次仍可能超过预留；本地账本不代表订阅余额或实际货币账单。

调用用量区分 complete/partial/unknown，流式累计与最近16条诊断预览分开；预览裁剪不丢累计。读取和行长仍有界，超过读取边界时标 partial；缺失不当零。用量观察追加幂等记录，空补量不覆盖已有数字，矛盾报告保留冲突和保守消费量，不将不同累计快照重复求和。原始临时调用日志不作为长期知识正文保存。

普通 Agent 真正使用已展示知识时，可调用 `knowledge_use` 提供准确知识修订、任务 ID/revision/write_version、幂等键和可选 `operation_id`。Runtime核句柄、当前任务或assignment、宿主版本及实际展示；关联操作必须有同边界的Pre记录，且展示发生在该操作之前。采用声明保持 reported，操作结果与来源健康查询时重算；成功后来转冲突或知识撤销会反映在观察中，不增加执行权限，也不要求复盘文档。

`learning-effects --days 30` 或 `status.learning_effects` 返回窗口内声明采用和首次唯一展示数，区分 Hook/MCP；动态操作与来源状态仅统计最近200个声明，显示样本量及截断。重复展示按准确任务版本、Agent、宿主、知识修订和方式去重，窗口使用首次展示时间。展示不表示阅读，声明采用不证明因果，工具成功不证明专业结论正确或用户验收；普通任务总用量未接入前，不能声称净节省百分比。

## CLI 入口

用 `python scripts/learning.py --help` 查看实际参数。全局参数 `--home`、`--workspace` 放在子命令之前。

| 子命令 | 用途 |
|---|---|
| `status` | 读取运行状态 |
| `learning-effects`、`knowledge-use` | 时间窗效果观察及准确任务下的简短采用声明 |
| `scheduler-status`、`scheduler-tick`、`scheduler-run` | 持久待办状态、有界单轮和有界续跑 |
| `session-bind` | 受信本地入口登记明确会话／子 Agent，返回可验证句柄 |
| `account-changed` | 通知登录边界变化，使旧连接失效；支持通知 ID 去重 |
| `task` | 保存明确任务，支持任务ID与write_version并发检查、`--new-task` 和 `--resume` |
| `execution-approve` | 受信本地入口为当前任务签发结构执行规则，不在 MCP 暴露 |
| `feedback` | 反馈问题及结果，支持重复 `--event-ref` |
| `problems`、`packet` | 列出问题、读取审核包 |
| `propose` | 提交结构化候选 JSON |
| `publish`、`revoke`、`restore` | 本地受信发布、撤销与显式恢复入口 |
| `knowledge-actions`、`knowledge-action` | 查询生命周期动作与幂等回执 |
| `knowledge-maturity`、`knowledge-promote` | 检查成熟度及按确定性门槛晋升 |
| `knowledge-release-prepare`、`knowledge-release-commit`、`knowledge-daily-release` | 准备、复查及提交知识批次，无变更跳过 |
| `knowledge-releases`、`knowledge-release`、`knowledge-release-rollback` | 查询知识清单、差异与安全回退 |
| `doctor`、`backup`、`rebuild-index` | 按需存储检查、在线备份和派生索引重建 |
| `search`、`read` | 查询和读取正式知识 |
| `admin-once`、`admin-batch` | 单次审核、受多项预算限制的后台短批次 |
| `reviews`、`admin-retry` | 查询持久回执、明确允许一次失败或未知调用再审 |
| `governance-propose`、`governance-execute` | 提出治理建议、通过受信入口执行并保存回执 |
| `sync`、`sync-list`、`sync-read`、`sync-batch` | 可选状态分发、对账与有限批次 |
| `serve` | 启动指定角色的 MCP 服务 |
| `install` | 创建待启用的独立安装副本 |

## 0.10.0 检查状态（2026-10-11）

594 项离线回归全套通过，耗时 117.935 秒；第八模块新增 105 项，覆盖跨项目安全回收、全局并发和token准入、完整/部分/未知用量、冲突重放、租约跨界回滚、持久续跑与公平调度、缺证和预算等待、迟到及禁用后的所有权、发布和同步启用条件、采用身份/任务/assignment隔离、动态工具冲突/来源/反例、窗口样本、schema9升级与迁移失败回滚、CLI/MCP和Windows日志降级。前七模块兼容回归继续通过。检查使用隔离SQLite、内存审核器与受控进程，不代表真实模型或服务验收。

配置示例与运行默认值一致，技能原有头字段及必需内容静态核对通过；辅助 `quick_validate.py` 因当前解释器缺少 PyYAML 未运行成功，未为此修改用户环境。

持久语义聚类、professional/pattern 专门验证、全天服务唤醒、普通任务用量与货币账单接入仍未实现；当前 Stable、发布、调度和效果观察使用上述明确规则。

真实桌面 Hook 信任、账户事件自动接线、父子 MCP 连接生命周期和项目主流程仍待宿主验收。当前测试不修改 FPS 项目、不执行登录切换、不启用真实模型，也不改当前用户插件配置。
