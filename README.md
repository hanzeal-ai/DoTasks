# DoTasks

一个由云端管理、本地执行的 Codex 任务调度系统：用户显式调用 DoTasks 时补全并确认需求，确认后进入任务队列；Local DoTasks Agent 根据云端 WSS 事件创建或恢复 Codex CLI/App Server 工作线程，DoTasks 保存队列、状态机、运行审计和验收结果。普通开发请求不会自动创建 DoTasks 任务。

## 当前能力

- 手动触发的最小边界确认闸门：显式要求使用 DoTasks 处理会话、PRD、变更或缺陷时，只对影响最小实现的必要边界一次性提问；回答足够后进入任务队列；
- 建立项目任务时使用已定位的文件、符号和修改动作执行一次项目级 Obsidian 历史检索，并按 CodeGraph、GitNexus、直接源码匹配的顺序定位目标；每条验收标准必须映射到具体目标与检查方式；不选择项目的新任务直接创建为可调度的无项目 Codex 会话；
- 任务状态机：待就绪、任务队列、调查、实现、验收、返工、待确认、暂停、完成、阻塞；
- 原子状态更新，避免多个执行器重复领取；
- Local Agent 事件驱动地调用 Codex CLI/App Server 创建、恢复和等待独立工作线程；不依赖 Codex Desktop 活跃窗口，也不创建周期性调度任务；
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
- 每个执行任务均是 Codex CLI/App Server 工作线程；真实 `threadId` 回写 DoTasks 后用于审计与失败重试，但不会显示为 Codex Desktop 原生侧栏任务；
- Token预算、上下文数量限制和会话摘要字段；完整历史对话默认不加载；看板分别展示原始 Token 与有效预算 Token，并按阶段展示输入、缓存输入、输出和推理 Token。

## 多会话执行

Codex Skill 充当编排器，本地服务作为任务系统记录：

1. Skill 先确认需求边界，判断应修订现有任务、创建单个新任务，还是拆成多个可独立交付和验收的任务；
2. 每个任务通过 `prepare_task_location` 建立一次受限定位计划，按 CodeGraph、GitNexus、直接源码匹配选择首个可用方式，并保存精确的 `{file, mode, symbols, tasks}` 执行目标和验收方式；
3. `finalize_task_intake` 是唯一任务创建入口：接收一次性定位分析包，由服务端根据精确文件/符号/动作查询项目历史图谱，形成 `depends_tasks`、`conflicts_tasks`、`history_tasks` 和 `history_edges` 后原子创建任务；
4. 显式 DoTasks intake 创建 `auto_dispatch=true` 的 ready 实体后会写入持久化调度唤醒；云端命令触发 WSS 通知，本地 Agent 按 Review 优先、返工优先和开发并发容量创建或恢复 Codex CLI/App Server 工作线程；页面“新增任务”未选择项目时跳过代码定位，在 Agent 管理的临时工作区创建无项目会话，并在会话落盘后同步到 Codex App“最近”；
5. 执行会话实现并用 `submit_task_delivery` 提交真实改动清单及逐条验收证据；
6. 代码类任务由独立验证会话完成 Code Review：服务端先执行确定性检查，Review Agent 只根据目标、约束和真实 Git Diff 判断正确性、安全、权限边界、回归与无关修改；不通过则恢复原开发会话返工，通过后直接完成；
7. 文档、调研、文案、规划和无运行时影响的元数据任务不创建 Code Review 会话；开发交付的逐条证据全部通过后直接完成；
8. 完成任务通过页面右上角“完成任务”入口查看；执行失败、待确认、阻塞、暂停，以及自动 Code Review 熔断后的任务显示在看板末列“待处理”，等待人工重试、确认、解除阻塞或恢复调度。仍处于 `ready`/`rework` 的任务会直接显示依赖任务、冲突文件、项目排他、容量、重试退避或 Controller 等待原因。

交付时，上报位置必须命中任务建立阶段保存的目标锁，并覆盖领取后产生的真实 Git 工作区差异。执行或返工会话异常结束但没有提交交付时，服务最多自动恢复两次并优先续接原开发会话；仍未成功则关闭自动调度并进入“待处理”。Code Review 会话中断则保留在待审查并按独立熔断上限重试。看板的暂停按钮只持久关闭新的调度领取，不中断活动运行，也不改变任何任务状态；恢复调度会先消费 Review/返工等已有唤醒并在并发未满时补齐开发任务，已有的单任务暂停仍需逐个恢复。

看板详情显示任务关联的原生 Codex 任务 ID 和运行轮次；完整执行过程在 Codex App 原生任务中查看。

## 代码结构

```text
core/       任务创建、调度、工作流与生命周期等核心业务
taskboard/  HTTP、MCP 与本地集成适配层
taskboard/cloud/  云端任务控制面、安全入口、WSS 通知与 Obsidian 图谱镜像
web/        React + Vite 前端源码
static/     Vite 生成的生产静态资源
macos/      隐藏 Helper 的 Swift 源码与 Info.plist
```

`core/` 不负责页面渲染，`web/` 只通过 `/api` 使用后端能力；任务创建与调度仍由 Python 核心层执行，不依赖 Vite 开发服务器。

## 全新 Mac 安装与首次使用

Codex 工作线程、Git/Worktree 和 Helper 运行在 Mac 上；启用云端后，任务数据库、状态机和
调度器由云端持有，Mac 通过 Local Agent 领取任务并执行。云端不能替代本机 Codex，因此
一台全新的执行电脑至少需要完成下面五步。

### 1. 准备运行环境

- 安装并登录 Codex 桌面应用，确认同一用户下可以执行 `codex` CLI；
- 安装 Git、Python 3.14、Node.js 24（包含 npm）和 `uv`；
- 执行 `xcode-select --install` 安装 Xcode Command Line Tools，Helper 构建需要其中的
  `/usr/bin/swiftc` 和代码签名工具。

安装后先确认命令实际可用：

```bash
codex --version
git --version
python3 --version
node --version
npm --version
uv --version
/usr/bin/swiftc --version
```

`python3` 必须解析到 Python 3.14 或更高版本。临时 shell alias 不会被登录自启的 Helper
继承；如果机器上有多个 Python，应确保 PATH 中的 `python3` 本身满足版本要求。

### 2. 获取源码并安装前端依赖

```bash
git clone https://github.com/hanzeal-ai/DoTasks.git "$HOME/Documents/DoTasks"
cd "$HOME/Documents/DoTasks"
npm --prefix web ci
./scripts/test
```

仓库可以放在其他位置，但后续 personal marketplace 必须指向它的真实绝对路径。

### 3. 将 DoTasks 注册为本地 Codex 插件

Codex 从 personal marketplace 安装本地插件。全新用户先创建目录，并让 marketplace 中的
`./plugins/dotasks` 指向当前仓库：

```bash
mkdir -p "$HOME/plugins" "$HOME/.agents/plugins"
ln -s "$PWD" "$HOME/plugins/dotasks"
```

然后创建 `~/.agents/plugins/marketplace.json`：

```json
{
  "name": "personal",
  "interface": {
    "displayName": "Personal"
  },
  "plugins": [
    {
      "name": "dotasks",
      "source": {
        "source": "local",
        "path": "./plugins/dotasks"
      },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Productivity"
    }
  ]
}
```

如果该文件已经存在，不要覆盖；只需把上面的 `dotasks` 对象合并进现有 `plugins` 数组。
随后安装插件：

```bash
codex plugin marketplace list
codex plugin add dotasks@personal
```

Codex 官方的本地插件和 marketplace 说明见
[Package your plugin](https://developers.openai.com/plugins/build/plugins)。

### 4. 安装并启动本地 Helper

```bash
./scripts/build-helper --install
curl -fsS http://127.0.0.1:8765/api/health
```

安装命令会构建 Web 静态资源、打包 Helper，并注册当前 macOS 用户的 LaunchAgent。以后登录
系统时，本地服务会自动恢复，不需要长期打开终端。Helper 和运行数据位于：

```text
~/Library/Application Support/DoTasks/
```

看板地址为 <http://127.0.0.1:8765>。健康接口应返回包含 `"ok": true` 的 JSON。

### 5. 在 Codex 中首次使用

1. 完全退出并重新打开 Codex，或至少新建一个 Codex 任务，使新安装的 Skill 和 MCP 生效；
2. 打开一个本机存在、且当前用户和 Codex 都有权限访问的 Git 项目；
3. 显式输入 `$dotasks` 或提及 DoTasks 创建需求，也可以输入“打开任务看板”；
4. 首次数据库默认暂停调度，在看板右上角点击“恢复调度”后再执行队列。

不使用云端时，到这里即可完整使用本地模式。若已有云端服务，再按下方“云端部署与本地
Agent”配置连接；配置后以云端数据为准，网络中断期间已有云端任务会等待 Agent 重连。

### 更新已有安装

源码更新后执行：

```bash
git pull
npm --prefix web ci
./scripts/full-update
```

该命令会测试源码、重新安装 Helper 和插件，并检查源码、插件缓存与打包运行时是否一致。
更新完成后新建一个 Codex 任务。服务异常时优先检查：

```bash
curl -fsS http://127.0.0.1:8765/api/health
tail -n 100 "$HOME/Library/Application Support/DoTasks/logs/server.err.log"
tail -n 100 "$HOME/Library/Application Support/DoTasks/logs/agent.err.log"
```

## 源码开发启动

不安装 Helper 时，可以直接从仓库启动本地服务：

```bash
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

新建运行数据库默认关闭调度。需要执行队列时，在看板中点击“恢复调度”；HTTP 层会同时开启调度并写入持久化 Agent 信号。恢复操作及之后每个任务/Run 状态流转都会写入 `scheduler_state`。云端通过 WSS 通知常驻 Local Agent，Agent 只在收到真实事件或已有工作线程结束时领取需求拆解、开发、返工和 Code Review；没有周期性 heartbeat，也不会在空队列上消耗模型 Token。工作线程通过 `$dotasks-lifecycle` 完成当前阶段并将结果回调 MCP。

Local Agent 通过 `codex app-server` 的 `thread/start`、`thread/resume` 和 `turn/start` 创建或续接工作线程；拿到真实 `threadId` 后才调用 `bind_native_dispatch`，因此任务领取状态不会领先于真实执行会话。Worker 继续使用只暴露生命周期 MCP 的隔离 `CODEX_HOME`；进程停止并完成会话落盘后，Agent 会将对应会话文件原子同步到主 `CODEX_HOME` 并合并会话索引，使看板中的 `codex://threads/...` 链接可由 Codex 桌面端读取，同时避免两个 App Server 并发写同一会话。生命周期回调产生的新唤醒在当前工作线程结束后立即由 Agent 消费；线程异常退出且没有提交回调时，Agent 回写派发失败并交给现有重试/熔断策略。设置页可开启并行开发并配置 1–8 个开发槽位（默认关闭、默认并发数 2）；Worktree 模式仍从固定 `base_ref` 创建隔离工作区。每个派发保留独立 `dispatch_attempt_id`，过期回调不能绑定到新的派发尝试。

指定端口：

```bash
./scripts/start --port 8877
```

HTTP 服务只允许绑定 `localhost` 或回环 IP。API 会校验 `Host` 与浏览器 `Origin`；带请求体的写操作只接受不超过 1 MiB 的 JSON 对象。DoTasks 不提供未经认证的局域网监听模式，如需跨设备访问，应在具备认证和 TLS 的受控代理后单独设计部署边界。

### 云端部署与本地 Agent

云端模式沿用同一套页面和 `/api` 交互，并由云端 SQLite（后续可替换 PostgreSQL）保存需求、
任务、状态机、调度状态和 Dispatch Outbox。浏览器或手机直接读写云端数据，不再把 `/api`
请求转发到 Mac，因此 Local Agent 离线时仍可新增需求和管理任务；待执行项会保留在云端队列。

每次会影响调度的状态变化都会持久化唤醒信号，并通过 WSS 向 Local DoTasks Agent 发送轻量
通知。Agent 收到通知后通过 HTTPS 调用云端调度接口领取需求拆解、开发、返工或 Review，
再使用本地 Codex CLI/App Server 的 `thread/start`、`thread/resume` 和 `turn/start` 创建或
续接工作线程；生命周期 MCP 回调同样写回云端。Git、Worktree、Diff、项目源码和 Codex
执行器仍只在 Mac 上。WSS 仅作事件唤醒，不使用周期性模型任务，空队列不会消耗模型 Token。

首次连接新版空云端时，Agent 会把本地 SQLite 做一致性快照并上传一次；云端已有需求或任务
后不会再覆盖。配置了 Cloud Agent 的普通 DoTasks MCP 和工作线程 MCP 都会访问云端权威数据。

```text
浏览器/手机/MCP -> DoTasks Cloud
                   任务数据库 / 状态机 / Dispatch Outbox
                     | WSS 通知 + HTTPS 领取/回调
                     v
            Local DoTasks Agent
                     | event wake
                     v
            Codex CLI / App Server
            thread/start + turn/start
```

正式部署使用 GitHub Container Registry（GHCR）私有镜像，云服务器不需要保存源码。本地
手工发布时，先使用具备 `write:packages` 权限的 GitHub Token 登录，然后构建并直接推送
`linux/amd64` 镜像：

```bash
docker login ghcr.io
./scripts/publish-cloud-image \
  --image ghcr.io/hanzeal-ai/dotasks:<版本号>
```

`.github/workflows/deploy-cloud.yml` 会在 `main` 每次推送后自动运行完整测试，使用提交 SHA
构建不可变镜像并通过仓库自带的 `GITHUB_TOKEN` 推送 GHCR。构建完成后，ECS 上的 GitHub
Self-hosted Runner 只下载单文件部署脚本、拉取镜像并更新容器；不会下载项目源码，也不需要
从 GitHub 主动 SSH 进入 ECS。

Self-hosted Runner 使用 GitHub 默认标签 `self-hosted`、`Linux`、`X64`，以 `admin` 用户安装
为系统服务，并且该用户必须能直接执行 `docker info`。Runner 只需向 GitHub 和 GHCR 建立
出站 HTTPS 连接。GitHub 仓库的 Actions Secrets 只需配置：

- `ECS_PUBLIC_IP`：浏览器访问 DoTasks 使用的公网 IPv4。
- `DOTASKS_PUBLIC_URL`：可选；启用反向代理后填写 `https://tasks.example.com`，流水线会优先
  使用它并把应用端口收回到 `127.0.0.1`。

服务器只需 Runner、Docker、Docker Compose 和 `curl`，不需要代码仓库。两个流水线阶段
分别使用当次短期 `GITHUB_TOKEN` 发布和拉取 GHCR 私有镜像，不保存长期 GHCR Token。

也可以只把 `scripts/deploy-cloud-ip` 上传到云服务器手工部署。服务器先使用具备
`read:packages` 权限的 GitHub Token 登录 GHCR，然后执行：

```bash
docker login ghcr.io
chmod +x ./deploy-cloud-ip
./deploy-cloud-ip \
  --public-ip <服务器公网IPv4> \
  --image ghcr.io/hanzeal-ai/dotasks:<版本号>
```

服务器脚本会生成独立的网页登录密码和 Agent Token，以 `0600` 权限写入 `.env`，自动生成
运行所需的 Compose 配置，然后拉取镜像、启动容器并执行带认证的健康检查。服务器既不构建
镜像，也不需要 Dockerfile、前端产物或 Python 源码。

发布新版本后，使用新镜像标签重新执行并保留原有密码、Token 和数据卷：

```bash
./deploy-cloud-ip \
  --public-ip <服务器公网IPv4> \
  --image ghcr.io/hanzeal-ai/dotasks:<新版本号> \
  --reuse-env
```

不传新 `--image` 时，`--reuse-env` 会重新部署当前镜像。只有明确传入 `--force-env` 才会
替换 `.env` 和凭据。可通过 `--port`、`--http-user`、`--http-password`、`--agent-id` 和
`--agent-token` 覆盖默认值；回滚时传入旧版本镜像标签并使用 `--reuse-env`。

阿里云安全组仍需手动添加入方向规则：TCP `8765`（或 `--port` 指定端口），来源只填写
自己的出口公网 IP，不要对 `0.0.0.0/0` 开放。脚本结尾会输出网页地址、登录信息和 Mac
端 Agent 配置命令。IP 方案使用明文 HTTP，只适合短期联调；正式长期使用时应恢复仅本机
监听，并通过 HTTPS 反向代理暴露域名。网页密码与 Agent Token 不能复用。

切换 HTTPS 时，先让域名的 A 记录指向 ECS，并在安全组开放 TCP `80`、`443`。反向代理需
把域名转发到 `127.0.0.1:8765` 且支持 WebSocket。随后将服务器 `.env` 中的
`DOTASKS_PUBLIC_URL` 改为完整 HTTPS 地址、`DOTASKS_BIND_ADDRESS` 改为 `127.0.0.1`，在 GitHub
Actions Secrets 新增同值的 `DOTASKS_PUBLIC_URL`，再用 `--reuse-env` 部署。最后把 Mac Agent 的
`--cloud-url` 重新配置成 HTTPS 地址，并删除公网 `8765` 安全组规则。

在运行 DoTasks 的 Mac 上执行一次配置：

```bash
./scripts/start-agent configure \
  --cloud-url http://<服务器公网IP>:8765 \
  --agent-id default \
  --agent-token '<与云端 .env 完全一致的 Agent Token>'
```

源码运行时另开终端执行 `./scripts/start-agent`。通过 `./scripts/build-helper --install`
安装的 Helper 会同时保活本地服务和 Agent；未配置云端时 Agent 静默等待，不影响本地模式。
Agent 配置保存在 `~/Library/Application Support/DoTasks/cloud-agent.json`，文件权限为
`0600`。云端持久数据位于 Compose 的 `dotasks-data` 卷，图谱镜像位于卷内
`obsidian-vault/<agent-id>/DoTasks/`。

## Obsidian

默认使用仓库内的测试 Vault：

```text
<仓库目录>/data/obsidian-vault
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

Local Agent 生成的需求拆解、执行、返工和 Code Review 提示会显式调用 `$dotasks-lifecycle`。该内部 Skill 同样禁止语义自动调用，并要求提示包含对应的实体 ID 与运行 ID。Agent 会等待已绑定的 CLI 工作线程结束并复核持久化 Dispatch；若线程已经结束却没有提交生命周期回调，会中断未提交的 Run 并按现有重试策略恢复，而不是让任务永久停留在执行或 Code Review 状态。

插件只提供 Skill、MCP 与会话工具，不再向 Codex 左侧面板注入入口。生产方式使用隐藏的签名 Helper 启动 DoTasks 服务：

```bash
./scripts/build-helper --install
```

Helper 安装在：

```text
~/Library/Application Support/DoTasks/DoTasks Helper.app
```

它设置了 `LSUIElement`，不会出现在 Dock 或 Codex 左侧面板。Helper 负责提供打包运行时，并保活 DoTasks HTTP 服务和 Local Agent；启动时不会弹出项目文件夹选择窗口，也不维护逐项目 allowlist。

安装脚本同时创建用户级 LaunchAgent；登录后会自动恢复 Helper、DoTasks HTTP 服务和 Local Agent。Local Agent 仅在收到执行事件时为相应任务启动 Codex App Server 子进程。项目路径仍必须是存在的合法绝对目录；实际访问能力由当前用户的文件系统权限、macOS TCC 与 Codex 工作区边界共同约束。服务仍只监听：

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

会话和页面创建的需求/任务都支持最多 8 张 PNG、JPEG、GIF 或 WebP 图片，每张不超过 10 MiB。图片先以内容哈希写入服务器托管存储，需求拆解时自动关联到所有子任务；Local Agent 在派发前下载并校验图片，向 Codex 会话提供本机可读路径。独立任务完成后立即删除图片；需求拆出的多个任务共享图片时，在最后一个子任务完成或取消后删除服务器文件。

`review_code` 必须精确覆盖代码质量审查项，不包含验收标准：质量失败恢复原开发会话；完成三轮实现质量返工后再次失败会进入 `waiting_confirmation`，避免自动循环。通过时，Worktree 交付先在项目级集成锁内校验完整补丁和当前工作区指纹，串行应用成功后才进入 `done`。若项目工作区被 DoTasks 之外的操作改变，集成停止并保留可重试的 Review Run。是否属于代码任务由 intake 的 `quality_gates.code_review` 决定；可执行源码、测试、脚本、运行时配置、Schema/迁移、构建发布文件、依赖和共享契约均属于代码类。任务使用 `token_used` 保留原始统计，使用 `effective_token_used` 控制 `token_budget`；新建任务预算及并行配置可在页面右上角“设置”中调整，已创建任务的 Token 预算不随配置变化。默认近似公式为“非缓存输入 + 缓存输入 × 0.1 + 输出”，缓存权重可通过 `DOTASKS_CACHED_TOKEN_WEIGHT` 调整。有效 Token 达到预算时，活动运行会进入 `waiting_confirmation` 并关闭自动调度，避免无上限消耗。`depends_on` 只等待并新建线程，`continues_from` 和 `defect_of` 等待后复用前置开发线程。通用状态迁移不能绕过 Code Review 门禁。

## 测试

### Python runtime and dependency contract

开发、Code Review、Helper 和云端镜像统一使用 `.python-version` 与
`pyproject.toml` 声明的 Python 3.14 运行时。项目目前没有第三方 Python 运行依赖，
因此不携带旧版本标准库兼容层或独立 `vendor` 目录。

唯一受支持的 Python 测试入口是：

```bash
./scripts/test
```

升级 Python 时，必须同时更新 `.python-version`、`pyproject.toml`、Docker 基础镜像
及 `tests/test_dependency_contract.py`，然后通过 `./scripts/test` 与
`./scripts/build-helper` 验证。其他独立校验：

```bash
CODEX_SYSTEM_SKILLS="${CODEX_HOME:-$HOME/.codex}/skills/.system"
uv run --with pyyaml "$CODEX_SYSTEM_SKILLS/skill-creator/scripts/quick_validate.py" skills/dotasks
uv run --with pyyaml "$CODEX_SYSTEM_SKILLS/skill-creator/scripts/quick_validate.py" skills/dotasks-lifecycle
uv run --with pyyaml "$CODEX_SYSTEM_SKILLS/skill-creator/scripts/quick_validate.py" skills/dotasks-controller
python3 "$CODEX_SYSTEM_SKILLS/plugin-creator/scripts/validate_plugin.py" .
```

## API摘要

```text
GET    /api/health
GET    /api/board
POST   /api/visual-artifacts
POST   /api/task-intakes/enqueue
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
