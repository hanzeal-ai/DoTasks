# DoTasks

一个本地优先的 Codex 原生任务调度系统：用户显式调用 DoTasks 时补全并确认需求，确认后进入任务队列；原生 DoTasks Controller 为开发、Code Review、验收和返工创建或恢复可在 Codex App 中直接查看的任务，DoTasks 只保存队列、状态机、运行审计和验收结果。普通开发请求不会自动创建 DoTasks 任务。

## 当前能力

- 手动触发的最小边界确认闸门：显式要求使用 DoTasks 处理会话、PRD、变更或缺陷时，只对影响最小实现的必要边界一次性提问；回答足够后进入任务队列；
- 建立任务时使用已定位的文件、符号和修改动作执行一次项目级 Obsidian 历史检索，并按 CodeGraph、GitNexus、直接源码匹配的顺序定位目标；每条验收标准必须映射到具体目标与检查方式；
- 任务状态机：待就绪、任务队列、调查、实现、验收、返工、待确认、暂停、完成、阻塞；
- 原子状态更新，避免多个执行器重复领取；
- 原生 Controller Skill 通过 Codex App 自带的任务能力创建、恢复、等待独立任务；DoTasks 不启动 App Server；
- 多前置任务依赖闸门、目标位置锁、文件/符号冲突关系、可配置 Worktree 并行开发、串行补丁集成、自动续租和异常会话熔断；
- 调度开关与任务暂停状态持久化，重启后不会意外恢复领取；
- Code Review 不达标进入返工；普通任务验收不达标会创建关联 Bug，原任务等待 Bug 修复后重新验收；只有执行/返工会话意外退出、连接中断或未提交结果才进入执行失败；
- 每个任务关联需求确认、实现、返工和验证等 Codex 会话；默认由同一验证会话连续完成 Code Review 与验收；
- 独立实现/验收运行、交付摘要和验证结果；
- 执行、Code Review、验收、返工和重试均保留独立运行记录；会话可复用，但运行审计不会合并；
- 每个运行在首次调度时冻结版本化上下文和直接工具契约；开发 Agent 以自然语言任务说明作为唯一需求来源，只从 `targets[].file/mode/symbols` 获取修改位置锁，并接收验证命令，不接收内部定位动作、依赖、历史、定位证据或调度状态；Code Review Agent 只看到任务目标、约束、检查项和可信 Git Diff 范围；
- 交付时缓存受限 Git Diff；Code Review 根据服务端基线和真实改动文件自行读取 Git Diff，自动化验收命令由服务端按“交付 + 标准 + 命令 + 工作区指纹”执行和缓存，必需检查未通过时禁止完成任务；
- 实现提交必须记录实际改动文件/符号和逐条验收证据，并与领取时保存的真实 Git 工作区差异一致；验收前对实际改动执行第二次受限的 Obsidian/代码定位；
- 完成后自动生成受模块和项目约束的经验记录；
- 任务关系：变更自、依赖、缺陷来源、拆分、冲突等；
- MCP工具：在任意Codex会话创建已确认任务、操作任务和编译上下文；
- SQLite运行数据库；
- 按项目隔离的 Obsidian Markdown 历史图谱、增量同步与轻量检索；
- CodeGraph、GitNexus 自动探测与受限源码匹配查询计划；
- React + Vite 本地看板，生产资源构建到 `static/`；
- DoTasks 页面只保留任务看板、需求看板、Token、设置和调度开关；需求看板集中展示已确认需求及其拆分任务，不再维护项目或聊天会话；
- 隐藏的 hardened-runtime `DoTasks Helper.app` 提供打包运行时、登录自启和服务保活，不申请 Documents 或完全磁盘访问；
- 每个执行任务均是 Codex App 原生任务；真实 `threadId` 回写 DoTasks 后用于审计与失败重试；
- Token预算、上下文数量限制和会话摘要字段；完整历史对话默认不加载；看板分别展示原始 Token 与有效预算 Token，并按阶段展示输入、缓存输入、输出和推理 Token。

## 多会话执行

Codex Skill 充当编排器，本地服务作为任务系统记录：

1. Skill 先确认需求边界，判断应修订现有任务、创建单个新任务，还是拆成多个可独立交付和验收的任务；
2. 每个任务通过 `prepare_task_location` 建立一次受限定位计划，按 CodeGraph、GitNexus、直接源码匹配选择首个可用方式，并保存精确的 `{file, mode, symbols, tasks}` 执行目标和验收方式；
3. `finalize_task_intake` 是唯一任务创建入口：接收一次性定位分析包，由服务端根据精确文件/符号/动作查询项目历史图谱，形成 `depends_tasks`、`conflicts_tasks`、`history_tasks` 和 `history_edges` 后原子创建任务；
4. 显式 DoTasks intake 创建 `auto_dispatch=true` 的 ready 实体后，当前会话立即以 Controller kickoff 模式消费一次持久化调度唤醒；每次任务或 Run 状态流转都会再次写入唤醒，Controller 按 Review 优先、返工优先和开发并发容量创建或恢复原生工作任务；
5. 执行会话实现并用 `submit_task_delivery` 提交真实改动清单及逐条验收证据；
6. 代码类任务由独立验证会话完成 Code Review：服务端先执行确定性检查，Review Agent 只根据目标、约束和真实 Git Diff 判断正确性、安全、权限边界、回归与无关修改；不通过则恢复原开发会话返工，通过后直接完成；
7. 文档、调研、文案、规划和无运行时影响的元数据任务不创建 Code Review 会话；开发交付的逐条证据全部通过后直接完成；
8. 完成任务通过页面右上角“完成任务”入口查看；执行失败、待确认、阻塞、暂停，以及自动 Code Review 熔断后的任务显示在看板末列“待处理”，等待人工重试、确认、解除阻塞或恢复调度。

交付时，上报位置必须命中任务建立阶段保存的目标锁，并覆盖领取后产生的真实 Git 工作区差异。执行或返工会话异常结束但没有提交交付时，服务最多自动恢复两次并优先续接原开发会话；仍未成功则关闭自动调度并进入“待处理”。Code Review 会话中断则保留在待审查并按独立熔断上限重试。看板的暂停按钮只持久关闭新的调度领取，不中断活动运行，也不改变任何任务状态；恢复调度会先消费 Review/返工等已有唤醒并在并发未满时补齐开发任务，已有的单任务暂停仍需逐个恢复。

看板详情显示任务关联的原生 Codex 任务 ID 和运行轮次；完整执行过程在 Codex App 原生任务中查看。

## 代码结构

```text
core/       任务创建、调度、工作流与生命周期等核心业务
taskboard/  HTTP、MCP 与本地集成适配层
web/        React + Vite 前端源码
static/     Vite 生成的生产静态资源
macos/      隐藏 Helper 的 Swift 源码与 Info.plist
```

`core/` 不负责页面渲染，`web/` 只通过 `/api` 使用后端能力；任务创建与调度仍由 Python 核心层执行，不依赖 Vite 开发服务器。

## 启动

首次开发先安装 Web 依赖：

```bash
npm --prefix web install
```

启动后端：

```bash
cd /Users/sanmws/Documents/codex-taskboard
./scripts/start
```

打开：<http://127.0.0.1:8765>

开发前端时另开一个终端，通过 Vite 启动；`/api` 会代理到 `127.0.0.1:8765`：

```bash
npm --prefix web run dev
```

打开：<http://127.0.0.1:5173>

生产静态资源由以下命令生成到 `static/`；`build-helper` 会自动执行同一构建：

```bash
npm --prefix web run build
```

新建运行数据库默认关闭调度。需要执行队列时，先在看板中点击“恢复调度”。恢复操作及之后每个任务/Run 状态流转都会写入 `scheduler_state` 持久化唤醒；显式 DoTasks intake 会立即执行一次 Controller kickoff。Controller 只调度，不直接修改代码，工作任务通过 `$dotasks-lifecycle` 完成当前阶段并将结果回调 MCP。

Intake Agent 在创建任务后立即调用 Codex App 原生 `create_thread`，以完整派发 Prompt 作为新任务的首条消息；每个成功创建的任务都会显示在 Codex App 左侧任务栏并独立执行。生命周期 Agent 在成功提交拆解、交付、阻塞或 Code Review 结果后执行一次 handoff，通过全局租约消费调度代次：先恢复 Review，再按返工优先顺序填满 `development` 槽位，并在绑定导致新状态变化时继续消费有限轮次。每个派发都有独立 `dispatch_attempt_id`，过期回调不能绑定到新的派发尝试。只有续接已记录的开发、返工或 Review 任务时才调用 `send_message_to_thread`。设置页可开启并行开发并配置 1–8 个开发槽位（默认关闭、默认并发数 2）；安全并行只用于目标文件互不重叠、无显式依赖且没有项目级排他目标的任务，每个任务从固定的 DoTasks 集成分支 Revision 创建独立 Codex Worktree。同文件任务、迁移/Schema、依赖清单和锁文件保持串行；原工作区的未托管改动只阻塞目标文件与其重叠的任务，不影响其他任务并行。Review 通过后先串行提交到集成分支，再在不覆盖用户改动的前提下同步回原工作区；冲突时只延迟同步。任务领取时保持 `claimed`，只有真实 `threadId` 成功绑定后才进入 `implementing`。MCP 是客户端拉取协议，因此没有活跃 Agent turn 时，本地服务只能持久记录唤醒，不能自行调用 Codex App；下一次显式 DoTasks、手动 Controller，或用户明确授权的唯一全局 Controller heartbeat 会恢复遗留工作。禁止为单个任务创建定时器。

指定端口：

```bash
./scripts/start --port 8877
```

HTTP 服务只允许绑定 `localhost` 或回环 IP。API 会校验 `Host` 与浏览器 `Origin`；带请求体的写操作只接受不超过 1 MiB 的 JSON 对象。DoTasks 不提供未经认证的局域网监听模式，如需跨设备访问，应在具备认证和 TLS 的受控代理后单独设计部署边界。

## Obsidian

默认使用项目内的测试Vault：

```text
/Users/sanmws/Documents/codex-taskboard/data/obsidian-vault
```

连接现有Vault：

```bash
export DOTASKS_OBSIDIAN_VAULT="/absolute/path/to/your/vault"
./scripts/start
```

插件MCP进程也会读取这个环境变量。

任务投影按稳定项目键写入 `DoTasks/Projects/<project-key>/Tasks/`。SQLite 任务和关系表是调度真源，Obsidian 保存可检索、可阅读的项目历史图谱；Vault 暂时不可用只会留下可重试的 Outbox 记录，不会回滚任务。

## 代码定位

任务上下文接口接收项目绝对路径，由 Codex Agent 自动探测定位能力：优先使用已有且索引有效的 CodeGraph，其次使用已有且已索引目标仓库的 GitNexus；两者都不可用时，直接通过受限文件名和源码文本匹配定位定义、调用方及相关测试。图索引不是需求确认的前置条件。

系统不复制完整代码图。它把查询种子和预算交给独立执行会话；图工具返回当前源码、符号关系、影响范围和直接相关测试，源码匹配则从需求词、界面文案、路由、配置键和模块名逐步收窄。所有方式都必须上报项目、受限查询、非空文件列表和稳定符号；CLI 证据还必须包含精确 argv 与成功退出码。系统不会自行安装工具、初始化或刷新索引。

## Codex插件

插件入口位于当前目录：

```text
.codex-plugin/plugin.json
.mcp.json
skills/dotasks/SKILL.md
skills/dotasks-controller/SKILL.md
skills/dotasks-lifecycle/SKILL.md
```

`dotasks` 禁止模型自动调用。安装后，需要显式调用 `$dotasks`、明确提及 DoTasks，或选择插件入口，例如：

```text
整理这个需求并向我确认，确认后加入任务队列：我想在A页面增加一个导入。
打开任务看板。
显示等待我验收的任务。
把TASK-0012标记为TASK-0004的变更任务。
```

Controller 生成的需求拆解、执行、返工和 Code Review 提示会显式调用 `$dotasks-lifecycle`。该内部 Skill 同样禁止语义自动调用，并要求提示包含对应的实体 ID 与运行 ID。Controller 会等待已绑定的原生任务结束并复核持久化 Dispatch；若任务已经结束却没有提交生命周期回调，会中断未提交的 Run 并按现有重试策略恢复，而不是让任务永久停留在执行或 Code Review 状态。

插件只提供 Skill、MCP 与会话工具，不再向 Codex 左侧面板注入入口。生产方式使用隐藏的签名 Helper 启动 DoTasks 服务：

```bash
./scripts/build-helper --install
```

Helper 安装在：

```text
~/Library/Application Support/DoTasks/DoTasks Helper.app
```

它设置了 `LSUIElement`，不会出现在 Dock 或 Codex 左侧面板。Helper 只负责提供打包运行时、启动并保活 DoTasks HTTP 服务；启动时不会弹出项目文件夹选择窗口，也不维护逐项目 allowlist。

安装脚本同时创建用户级 LaunchAgent；登录后会自动恢复 Helper 和 DoTasks HTTP 服务。Helper 不启动 Codex App Server。项目路径仍必须是存在的合法绝对目录；实际访问能力由当前用户的文件系统权限、macOS TCC 与 Codex 工作区边界共同约束。服务仍只监听：

```text
http://127.0.0.1:8765
```

开发时仍可直接运行 `./scripts/start`。插件 MCP 与打包态 HTTP 服务使用同一个 `DOTASKS_HOME` 数据目录和项目路径基础校验，不依赖 Helper 应用环境或逐项目授权状态。

源码修改完成后统一执行一次全量更新，避免源码、已安装插件缓存和 Helper 运行时版本不一致：

```bash
./scripts/full-update
```

该命令依次运行完整测试、构建并安装 Helper、重启 HTTP 服务、更新插件 cachebuster、重新安装 personal marketplace 插件，并校验源码、插件缓存与打包运行时一致。更新完成后使用新的 Codex 任务加载最新 Skill 和 MCP 工具。

默认构建使用 ad-hoc 签名和 hardened runtime；如有长期稳定的 macOS 代码签名证书，可通过 `DOTASKS_CODESIGN_IDENTITY` 指定签名身份后重新安装。

## Workflow

任务创建必须先完成受限代码定位；随后由 `finalize_task_intake` 在一次模型可见调用中复用历史候选、判断依赖、保存契约并创建任务及关系。新任务按以下阶段运行：

```text
development -> code_review -> done
```

执行模型把任务标题、目标、范围和验收标准作为原生 Codex 会话的首要自然语言输入；结构化上下文只保留修改目标、验证命令、执行环境和可选批次，随后给出完成与阻塞的状态上报入口。工作区基线、调度降级原因、缓存身份和工具参数细节保存在服务端或生命周期 Skill 中，不重复注入开发 Prompt。相同类型、命令与超时的验证项会合并为一个命令组，命令只需运行一次，组内仍保留逐条验收标准与预期结果。执行阶段只暴露 `report_run_blocked`、`submit_task_delivery` 两个工具。代码类任务的 Code Review 只接收审查项和可信 Git Diff 范围，并只通过 `review_code` 提交结果；Diff 只能由现成 Git 命令获取，随后使用当前环境及项目已经配置的相关源码导航、lint、类型检查、静态分析、安全扫描和聚焦测试工具，不安装工具或编写临时扫描器。默认只检查代码质量、安全漏洞和高内聚低耦合，不检查任务目标或验收标准；非阻断风格建议不能触发返工。任务完成度继续由现有人工验收负责，不新增状态阶段。非代码类任务跳过 Code Review。若原生任务未加载 DoTasks MCP 工具，派发提示会提供同一本地服务的一次性 CLI 回退入口；执行异常最多自动恢复两次，Review 异常遵循独立中断上限，状态流转都会产生新的持久化调度唤醒。

`review_code` 必须精确覆盖代码质量审查项，不包含验收标准：质量失败恢复原开发会话；通过时，Worktree 交付先在项目级集成锁内校验完整补丁和当前工作区指纹，串行应用成功后才进入 `done`。若项目工作区被 DoTasks 之外的操作改变，集成停止并保留可重试的 Review Run。是否属于代码任务由 intake 的 `quality_gates.code_review` 决定；可执行源码、测试、脚本、运行时配置、Schema/迁移、构建发布文件、依赖和共享契约均属于代码类。任务使用 `token_used` 保留原始统计，使用 `effective_token_used` 控制 `token_budget`；新建任务预算及并行配置可在页面右上角“设置”中调整，已创建任务的 Token 预算不随配置变化。默认近似公式为“非缓存输入 + 缓存输入 × 0.1 + 输出”，缓存权重可通过 `DOTASKS_CACHED_TOKEN_WEIGHT` 调整。有效 Token 达到预算时，活动运行会进入 `waiting_confirmation` 并关闭自动调度，避免无上限消耗。`depends_on` 只等待并新建线程，`continues_from` 和 `defect_of` 等待后复用前置开发线程。通用状态迁移不能绕过 Code Review 门禁。

## 测试

### Python runtime and dependency contract

开发与 Code Review 验证统一使用 `.python-version` 锁定的 Python 3.9.25；项目兼容下限由
`pyproject.toml` 声明。Python 3.9/3.10 通过 `requirements.lock` 中精确锁定且带
官方纯 Python wheel SHA-256 的 tomli 2.4.1 提供 `tomllib` 兼容层。Helper 构建会
把同一锁文件安装到 `runtime/vendor`，启动脚本只在该目录存在时加入
`PYTHONPATH`，不会改变项目路径校验规则。

唯一受支持的 Python 测试入口是：

```bash
./scripts/test
```

升级 Python 或依赖时，必须同时更新 `.python-version`、`pyproject.toml`、
`requirements.lock` 及 `tests/test_dependency_contract.py` 中的版本和官方 wheel
哈希，然后通过 `./scripts/test` 与 `./scripts/build-helper` 验证。其他独立校验：

```bash
uv run --with pyyaml /Users/sanmws/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/dotasks
uv run --with pyyaml /Users/sanmws/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/dotasks-lifecycle
uv run --with pyyaml /Users/sanmws/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/dotasks-controller
python3 /Users/sanmws/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
```

## API摘要

```text
GET    /api/health
GET    /api/board
POST   /api/task-intakes/finalize
GET    /api/integrations
GET    /api/tasks/{id}
GET    /api/tasks/{id}/details
POST   /api/tasks/{id}/transition
POST   /api/tasks/{id}/relations
GET    /api/tasks/{id}/context
POST   /api/conversations/bind
POST   /api/runs/{id}/delivery
```

## 集成边界

- 显式调用 DoTasks 时解析PRD附件；目前通过会话 Skill 整理并确认后创建结构化任务；
- 原生任务由 `$dotasks-controller` 使用 Codex App 自带能力创建或恢复；DoTasks 不修改 Codex 客户端，也不使用 CDP、页面注入或私有数据库写入；
- DoTasks 只保存原生 `threadId` 映射和阶段结果，不复制 Codex 会话内容；`clientThreadId` 仅表示异步创建中，绝不会被当成真实会话绑定；
- 打包态和开发态使用相同的绝对路径、存在性和目录类型校验；访问能力继续受当前用户文件系统权限、macOS TCC 与 Codex 工作区边界控制；
- CodeGraph、GitNexus 或源码匹配由独立执行会话完成，本地服务只生成受限查询计划并验证 Agent 上报状态；
- 经验会自动创建并受限检索，跨任务去重和失效替换仍需显式维护；
