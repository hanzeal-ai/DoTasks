# Codex Taskboard

一个本地优先的 Codex 多会话任务调度系统：用户显式调用 Taskboard 时补全并确认需求，确认后进入任务队列；调度器为实现、验收和返工创建独立运行轮次，并使用 Obsidian 与可用的代码定位方式生成受预算约束的任务上下文。普通开发请求不会自动创建 Taskboard 任务。

## 当前能力

- 手动触发的最小边界确认闸门：显式要求使用 Taskboard 处理会话、PRD、变更或缺陷时，只对影响最小实现的必要边界一次性提问；回答足够后进入任务队列；
- 建立任务前强制执行 Obsidian 历史检索，并按 CodeGraph、GitNexus、直接源码匹配的顺序定位文件/符号；每条验收标准必须映射到具体目标与检查方式；
- 任务状态机：待就绪、任务队列、调查、实现、验收、返工、待确认、暂停、完成、阻塞；
- 原子状态更新，避免多个执行器重复领取；
- 内置常驻调度器，通过官方 Codex App Server 直接创建、命名和启动独立会话；
- 依赖闸门、目标位置锁、项目级串行、自动续租和异常会话熔断；
- 调度开关与任务暂停状态持久化，重启后不会意外恢复领取；
- Code Review 不达标进入返工；普通任务验收不达标会创建关联 Bug，原任务等待 Bug 修复后重新验收；只有执行/返工会话意外退出、连接中断或未提交结果才进入执行失败；
- 每个任务关联需求确认、实现、返工和验证等 Codex 会话；默认由同一验证会话连续完成 Code Review 与验收；
- 独立实现/验收运行、交付摘要和验证结果；
- 执行、Code Review、验收、返工和重试均保留独立运行记录；会话可复用，但运行审计不会合并；
- 每个运行在首次调度时冻结版本化上下文和直接工具契约；执行快照只注入目标/范围、文件/符号、实现步骤、验收命令、工作区指纹，并为可机械验证的单点字面量替换附带 40–80 行目标片段和文件哈希，后续不重复检索工具、任务详情、Obsidian 或代码定位工具；
- 交付时缓存受限 Git Diff，Code Review 与验收直接复用；验收自动化命令按“交付 + 标准 + 命令 + 工作区指纹”缓存成功结果，必需检查未通过时禁止完成任务；
- 实现提交必须记录实际改动文件/符号和逐条验收证据，并与领取时保存的真实 Git 工作区差异一致；验收前对实际改动执行第二次受限的 Obsidian/代码定位；
- 完成后自动生成受模块和项目约束的经验记录；
- 任务关系：变更自、依赖、缺陷来源、拆分、冲突等；
- MCP工具：在任意Codex会话创建已确认任务、操作任务和编译上下文；
- SQLite运行数据库；
- Obsidian Markdown同步与轻量检索；
- CodeGraph、GitNexus 自动探测与受限源码匹配查询计划；
- React + Vite 本地看板，生产资源构建到 `static/`；
- 独立项目侧栏：只从 Taskboard 独立 App Server 的会话工作目录同步项目，也可通过签名 Helper 的系统目录选择器加载文件夹；
- 隐藏的 hardened-runtime `Taskboard Helper.app` 持久保存 security-scoped bookmark；每个项目首次加载时单独授权，不申请 Documents 或完全磁盘访问；
- 项目下原位展开会话并在 Taskboard 内继续；Taskboard 会话不会进入 Codex App 的会话列表，也不提供接管入口；
- Token预算、上下文数量限制和会话摘要字段；完整历史对话默认不加载；看板分别展示原始 Token 与有效预算 Token，并按阶段展示输入、缓存输入、输出和推理 Token。

## 多会话执行

Codex Skill 充当编排器，本地服务作为任务系统记录：

1. Skill 先确认需求边界，判断应修订现有任务、创建单个新任务，还是拆成多个可独立交付和验收的任务；
2. 每个任务通过 `prepare_task_location` 查询 Obsidian，按 CodeGraph、GitNexus、直接源码匹配选择首个可用定位方式，并保存文件、符号、实现步骤和验收方式；
3. `finalize_task_intake` 是唯一任务创建入口：接收一次性定位分析包，由服务端复用历史候选、推导重复字段并完成定位、依赖判断和任务创建；
4. 调度器开启后，通过 `dispatch_next_task` 原子领取满足依赖且目标位置未锁定的任务，并创建或恢复独立执行会话；
5. 执行会话实现并用 `submit_task_delivery` 提交真实改动清单及逐条验收证据；
6. 独立验证会话读取交付 Diff 完成 Code Review；不通过则恢复原开发会话返工，通过则进入功能验收；
7. 纯自动化验收由本地服务直接执行；包含人工或静态判断时，默认复用验证会话。普通任务验收不通过时创建关联 Bug，Bug 完成开发、Review 和验收后，原任务重新验收；
8. 验收通过后任务进入 `done` 并显示在“完成”列，同时自动沉淀经验；
9. 执行失败、待确认、阻塞、暂停，以及自动 Review/验收熔断后的任务统一出现在“待处理”入口，等待人工重试、确认、解除阻塞或恢复调度。

交付时，上报位置必须命中任务建立阶段保存的目标锁，并覆盖领取后产生的真实 Git 工作区差异。执行或返工会话结束但没有提交交付时进入执行失败；验收会话中断则保留在待验收。看板的暂停按钮会中断活动运行、保存任务原状态并持久关闭调度；恢复调度不会隐式恢复暂停任务，任务可逐个恢复。失败任务会暂时锁住项目，明确重试时优先恢复原执行会话，避免其他任务读取半成品改动。

看板中的“查看对话”会显示一个任务的所有会话和运行轮次，并可直接在 Taskboard 自己的会话区打开。

## 代码结构

```text
core/       任务创建、调度、工作流与生命周期等核心业务
taskboard/  HTTP、MCP、Codex App Server、工作区等适配层
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

新建运行数据库默认关闭调度；需要执行队列时，在看板中显式点击“恢复调度”。

Taskboard 的 App Server 默认使用独立目录：

```text
$CODEX_TASKBOARD_HOME/codex-home
```

它只复用主 Codex 目录中的认证、`AGENTS.md`、模型和服务等级，并只安装内部生命周期 Skill；不会继承通用 Skills、Plugins、Rules、MCP 配置、`sessions`、`state_*.sqlite` 或其他会话状态。开发阶段只暴露 3 个生命周期工具（含阻塞/待确认安全出口），Code Review 与验收共享 4 个验证工具；动态任务上下文置于稳定指令之后，以便复用提示缓存。可用 `CODEX_TASKBOARD_CODEX_HOME` 指定另一个独立目录；设置 `CODEX_TASKBOARD_SHARE_CODEX_CONFIG=0` 后连认证和主目录配置也完全隔离。不要把 `CODEX_TASKBOARD_CODEX_HOME` 指向主 Codex 的 `CODEX_HOME`。

指定端口：

```bash
./scripts/start --port 8877
```

HTTP 服务只允许绑定 `localhost` 或回环 IP。API 会校验 `Host` 与浏览器 `Origin`；带请求体的写操作只接受不超过 1 MiB 的 JSON 对象。Taskboard 不提供未经认证的局域网监听模式，如需跨设备访问，应在具备认证和 TLS 的受控代理后单独设计部署边界。

## Obsidian

默认使用项目内的测试Vault：

```text
/Users/sanmws/Documents/codex-taskboard/data/obsidian-vault
```

连接现有Vault：

```bash
export CODEX_TASKBOARD_OBSIDIAN_VAULT="/absolute/path/to/your/vault"
./scripts/start
```

插件MCP进程也会读取这个环境变量。

## 代码定位

任务上下文接口接收项目绝对路径，由 Codex Agent 自动探测定位能力：优先使用已有且索引有效的 CodeGraph，其次使用已有且已索引目标仓库的 GitNexus；两者都不可用时，直接通过受限文件名和源码文本匹配定位定义、调用方及相关测试。图索引不是需求确认的前置条件。

系统不复制完整代码图。它把查询种子和预算交给独立执行会话；图工具返回当前源码、符号关系、影响范围和直接相关测试，源码匹配则从需求词、界面文案、路由、配置键和模块名逐步收窄。所有方式都必须上报项目、受限查询、非空文件列表和稳定符号；CLI 证据还必须包含精确 argv 与成功退出码。系统不会自行安装工具、初始化或刷新索引。

## Codex插件

插件入口位于当前目录：

```text
.codex-plugin/plugin.json
.mcp.json
skills/codex-taskboard/SKILL.md
skills/codex-taskboard-lifecycle/SKILL.md
```

`codex-taskboard` 禁止模型自动调用。安装后，需要显式调用 `$codex-taskboard`、明确提及 Codex Taskboard，或选择插件入口，例如：

```text
整理这个需求并向我确认，确认后加入任务队列：我想在A页面增加一个导入。
打开任务看板。
显示等待我验收的任务。
把TASK-0012标记为TASK-0004的变更任务。
```

后台调度器生成的执行、返工和验收提示会显式调用 `$codex-taskboard-lifecycle`。该内部 Skill 同样禁止语义自动调用，并要求提示中同时包含任务 ID 与运行 ID。

插件只提供 Skill、MCP 与会话工具，不再向 Codex 左侧面板注入入口。生产方式使用隐藏的签名 Helper 启动 Taskboard 服务：

```bash
./scripts/build-helper --install
```

Helper 安装在：

```text
~/Library/Application Support/Codex Taskboard/Taskboard Helper.app
```

它设置了 `LSUIElement`，不会出现在 Dock 或 Codex 左侧面板。首次启动会要求选择当前 Taskboard 项目；以后从页面添加其他项目时，每个项目只在首次加载时弹出一次系统目录授权。授权书签保存在：

```text
~/Library/Application Support/Codex Taskboard/authorized-projects.json
```

安装脚本同时创建用户级 LaunchAgent；登录后会自动恢复 Helper、书签和服务。Helper 保持 security-scoped resource 存活，并以父进程启动 Python 服务和 Codex App Server。打包运行时，核心项目目录校验还会拒绝没有书签的路径，因此任务创建、定位、调度与验收不会绕过首次授权。服务仍只监听：

```text
http://127.0.0.1:8765
```

开发时仍可直接运行 `./scripts/start`，此模式不启用打包态的 Helper 授权闸门。

默认构建使用 ad-hoc 签名和 hardened runtime；如有长期稳定的 macOS 代码签名证书，可通过 `CODEX_TASKBOARD_CODESIGN_IDENTITY` 指定签名身份后重新安装。

## Workflow v2

任务创建必须先完成受限代码定位；随后由 `finalize_task_intake` 在一次模型可见调用中复用历史候选、判断依赖、保存契约并创建任务及关系。新任务按以下阶段运行：

```text
development -> code_review -> acceptance -> done
```

执行模型只接收目标、范围、定位目标、实现步骤、验收命令和可选目标片段；相同类型、命令与超时的验收项会合并为一个命令组，命令只需运行一次，组内仍保留逐条验收标准与预期结果。工作区基线、缓存身份和工具契约仍完整保存在服务端，不注入模型。执行阶段只暴露 `report_run_blocked`、`submit_task_delivery` 两个工具。Code Review 只接收需求、实现契约、审查项和压缩后的交付 Diff；纯自动化任务只暴露 `review_code`。功能验收只接收逐条验收计划与交付证据，并按是否复用 Review 会话暴露 `run_acceptance_checks`、`accept_task`。

`review_code` 只检查实现和代码规范；失败恢复原开发会话。通过后，纯自动化验收不会创建或恢复模型会话；其他验收默认复用刚完成的 Code Review 会话，因此常规任务最多只新建开发和验证两个会话。安全、支付、数据迁移等需要额外隔离的任务可在 `review_contract` 中设置 `"separate_acceptance_session": true`。`accept_task` 只按验收标准判定；普通任务验收失败会原子创建 `type=bug` 且 `defect_of` 原任务的 Bug，Bug 复用原开发线程并重新经过 Review、验收。任务使用 `token_used` 保留原始统计，使用 `effective_token_used` 控制 `token_budget`；新建任务预算可在页面右上角“设置”中配置，默认 60,000，已创建任务不随配置变化。默认近似公式为“非缓存输入 + 缓存输入 × 0.1 + 输出”，缓存权重可通过 `CODEX_TASKBOARD_CACHED_TOKEN_WEIGHT` 调整。有效 Token 达到预算时，活动运行会进入 `waiting_confirmation` 并关闭自动调度，避免无上限消耗。`depends_on` 只等待并新建线程，`continues_from` 和 `defect_of` 等待后复用前置开发线程。通用状态迁移不能绕过 Review 或验收门禁。

## 测试

### Python runtime and dependency contract

开发与验收统一使用 `.python-version` 锁定的 Python 3.9.25；项目兼容下限由
`pyproject.toml` 声明。Python 3.9/3.10 通过 `requirements.lock` 中精确锁定且带
官方纯 Python wheel SHA-256 的 tomli 2.4.1 提供 `tomllib` 兼容层。Helper 构建会
把同一锁文件安装到 `runtime/vendor`，启动脚本只在该目录存在时加入
`PYTHONPATH`，不会弱化项目授权环境。

唯一受支持的 Python 测试入口是：

```bash
./scripts/test
```

升级 Python 或依赖时，必须同时更新 `.python-version`、`pyproject.toml`、
`requirements.lock` 及 `tests/test_dependency_contract.py` 中的版本和官方 wheel
哈希，然后通过 `./scripts/test` 与 `./scripts/build-helper` 验证。其他独立校验：

```bash
uv run --with pyyaml /Users/sanmws/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/codex-taskboard
uv run --with pyyaml /Users/sanmws/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/codex-taskboard-lifecycle
python3 /Users/sanmws/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
```

真实模型 Token 回归（会创建隔离临时项目并实际消耗 Token）：

```bash
./scripts/token-regression --output /tmp/codex-taskboard-token-regression.json
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
POST   /api/tasks/{id}/review
POST   /api/tasks/{id}/relations
GET    /api/tasks/{id}/context
POST   /api/dispatcher/claim
POST   /api/conversations/bind
GET    /api/codex/projects
POST   /api/codex/projects/pick
GET    /api/codex/threads?project=/absolute/path
GET    /api/codex/threads/{id}
POST   /api/codex/threads
POST   /api/codex/threads/{id}/turns
POST   /api/runs/{id}/delivery
POST   /api/tasks/{id}/prepare-review
```

## 集成边界

- 显式调用 Taskboard 时解析PRD附件；目前通过会话 Skill 整理并确认后创建结构化任务；
- 新会话由本地调度器调用官方 Codex App Server 创建；Taskboard 不修改 Codex 客户端，也不使用 CDP 或页面注入；
- 项目自动同步依据 Taskboard 独立 App Server 会话的 `cwd`，不是 Codex 桌面端项目注册表；两个服务不共享会话存储，也没有释放或接管流程；
- 打包态项目目录访问由 Helper 保存的逐项目书签控制；项目首次授权只扩大到用户选择的目录；
- CodeGraph、GitNexus 或源码匹配由独立执行会话完成，本地服务只生成受限查询计划并验证 Agent 上报状态；
- 经验会自动创建并受限检索，跨任务去重和失效替换仍需显式维护；
