# DoTasks

DoTasks 将明确提出的工作需求整理为可执行任务，通过云端看板协调任务状态，由开发者机器上的 LocalAgent 调用 Codex 执行、审查和回写结果。

代码、Git 操作和工作区保留在本机；云端负责协作、任务队列与审计。任务创建以用户明确提出的 DoTasks 请求为入口。

[快速开始](#快速开始) · [开发与验证](#开发与验证) · [文档](#文档) · [反馈与贡献](#反馈与贡献)

## 功能

- 将需求拆解为任务，记录依赖、验收条件和执行结果。
- 通过 LocalAgent 领取任务，在本机调用 Codex 执行。
- 使用任务租约、依赖关系及文件 / 符号锁协调执行。
- 支持审查、返工和可选的独立 Git worktree。
- 在 Web 看板中查看需求、任务、执行记录与用量。
- 支持团队分工、外部任务来源，以及 Codex 插件和 MCP 接入。
- 以 SQLite 保存业务状态，将 Obsidian 文件作为可读投影。

## 项目状态与运行条件

项目处于发布前阶段。LocalAgent 与独立 CLI 当前面向 macOS，实际执行任务需要本机安装并配置 Codex、Git 及目标项目所需工具。Web 看板可通过浏览器访问。

从源码开发需要：

- Python 3.14+。
- Node.js 24 与 npm，用于 Web 开发和构建。
- `uv`，用于项目统一测试入口。
- Git；实际执行任务时还需要 Codex。

独立 CLI 的安装与首次连接见 [CLI 上手指南](docs/cli-onboarding.md)，打包和分发方式见 [CLI 分发说明](docs/cli-distribution.md)。

## 快速开始

### 启动本地看板

```sh
git clone https://github.com/hanzeal-ai/DoTasks.git
cd DoTasks
npm --prefix web ci
npm --prefix web run build
./scripts/start
```

打开 <http://127.0.0.1:8765>。该入口启动本地看板服务；要让任务实际执行，还需按 [CLI 上手指南](docs/cli-onboarding.md)配置 LocalAgent 和 Codex。

若默认 Python 不符合要求，可显式指定解释器：

```sh
DOTASKS_PYTHON_BIN=/path/to/python3.14 ./scripts/start
```

### 开发 Web 界面

保持后端运行，在另一个终端执行：

```sh
npm --prefix web run dev
```

打开 <http://127.0.0.1:5173>。开发服务器默认将 API 请求转发到本机 `8765` 端口；前端构建结果写入 `static/`，不要直接编辑生成文件。

## 工作流程与数据边界

1. 明确需求，确认任务范围、依赖与验收条件。
2. 将任务交给调度器，由符合条件的 LocalAgent 领取。
3. 在本地项目或独立 worktree 中执行并记录结果。
4. 根据审查结果完成任务或进入返工，再由看板展示状态。

任务业务状态以 SQLite 为准，Obsidian 文件用于阅读和协作展示。云端和 LocalAgent 通过 HTTPS / WSS 协调；本机 Codex 的具体执行与接入方式见上手文档。

云端部署与本地看板启动是不同的运行场景。部署配置、凭证及运维步骤集中在[部署指南](deployment/README.md)。

## 项目结构

| 路径                              | 职责                               |
| --------------------------------- | ---------------------------------- |
| `core/`                           | 任务、调度、存储与执行相关业务逻辑 |
| `taskboard/`                      | 本地服务、CLI 与 LocalAgent 接入   |
| `taskboard/cloud/`                | 云端协作与连接服务                 |
| `web/`                            | React 看板源码                     |
| `static/`                         | Web 构建产物                       |
| `scripts/`                        | 启动、测试与打包入口               |
| `skills/`、`.codex-plugin/`       | Codex 技能与插件配置               |
| `services/laya-dotasks-decision/` | 可选决策服务                       |
| `deployment/`                     | 部署配置与说明                     |

## 开发与验证

项目统一测试入口：

```sh
./scripts/test
```

该脚本通过 `uv` 使用 Python 3.14 运行测试，并检查测试所需的 Node.js 环境。请复用此入口，避免不同解释器产生不一致结果。

构建 CLI 包：

```sh
./scripts/build-cli
```

该命令先构建 Web 资源，再生成 CLI 包。自带运行时的独立安装包另见 [CLI 分发说明](docs/cli-distribution.md)。

## 文档

| 场景                     | 文档                                                    |
| ------------------------ | ------------------------------------------------------- |
| CLI 安装、连接与日常使用 | [CLI 上手指南](docs/cli-onboarding.md)                  |
| 独立运行时、打包与分发   | [CLI 分发说明](docs/cli-distribution.md)                |
| 接入外部任务             | [任务来源](docs/task-sources.md)                        |
| 移动端消息接入           | [移动消息说明](docs/mobile-messaging.md)                |
| 团队协作实现与验收       | [团队协作交付说明](docs/team-collaboration-delivery.md) |
| 云端部署                 | [部署指南](deployment/README.md)                        |
| 可选决策服务             | [服务说明](services/laya-dotasks-decision/README.md)    |

## 反馈与贡献

通过 [Issues](https://github.com/hanzeal-ai/DoTasks/issues)反馈问题时，请提供系统与运行版本、复现步骤、任务所处状态及相关日志。请移除令牌、账号信息和私人项目内容。

提交改动时，请保持任务状态、调度规则和权限契约只有一个权威定义；复用现有模块，避免在 UI、投影或适配层复制业务规则。Pull Request 应说明行为变化、影响范围和验证结果，并通过相关检查。

## 许可证

仓库尚未提供 `LICENSE` 文件，当前没有明确授予开源使用、修改或再分发的许可。许可证确定后应以仓库中的正式许可证文件为准。
