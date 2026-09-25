# 当前代码规范审查与清理

日期：2026-09-25。基线：`5d249b2`，开始时工作区无未提交改动。
依据：本机 `/Users/sanmws/.codex/standards/vibe-coding.md` v2.2。

## 结论与范围

发现并修复了凭证重定向缺陷、废弃交付路径、无运行消费者的私有代码、重复路径规则及反向模块依赖。本次已审差异经独立审查接受；不能据此宣称全仓已满足规范全部要求。

审查覆盖源码入口、HTTP/MCP 路由、CLI 安装与更新、前端导入、服务 mixin、兼容与数据库迁移边界、测试和打包产物。使用源文件、引用检索及 AST 候选检查定位问题；未发现 CodeGraph 索引。动态注册、公共入口、人工操作接口不能仅凭静态引用数量判断无用。

本次按 R2 管理：修复涉及 Agent 凭证传输边界。用户授权代码审查、规范化和删除无用代码；本次未安装 CLI、重启用户服务、迁移运行数据、提交、推送或部署。新平台支持及业务流程改造不在本次范围。

## 已处理问题

| 问题 | 原始证据与处理 | 对应规范 |
| --- | --- | --- |
| Agent Token 可随重定向转发 | `RemoteToolClient.call` 和 `RelayAgent._cloud_request` 使用默认 `urlopen`；独立审查以虚构 Token 验证标准库会向另一 origin 携带 Authorization。现统一使用 `http_client.NoRedirect`，同时关闭包装异常持有的 HTTP 响应。 | 7.6、8.2 |
| 两套交付路径并存 | CLI 已直接管理 launchd 服务，README 和 `full-update` 仍引导构建安装 Swift Helper。删除 `macos/` 的两个源文件、`build-helper`、`full-update` 及失效忽略项；README 改为现行 CLI 流程。 | 5.1、6.4、7.4 |
| CLI 入口承担底层传输 | onboarding/update 反向导入 cli 中的请求函数；团队请求又从 decision_client 导入重定向规则。共享传输及策略归入 `taskboard/http_client.py`，初始化直接依赖 cli_service。独立在线安装脚本需在包下载前运行，其引导代码仍自包含。 | 3.3、7.1、7.2 |
| 数据目录规则重复 | Agent 与 RemoteToolClient 分别维护数据及配置路径；移动端依赖远端模块的私有路径函数。统一到 `taskboard/runtime_paths.py`，保留原有环境变量和默认目录语义。 | 3.3、7.1 |
| 无路由调用的旧云端方法 | `RelayHandler._serve_relay_events`、`_proxy` 全仓仅定义，GET/POST/PATCH 实际通过当前处理器路由。删除这两个私有方法；保留仍有消费者的 relay 队列。 | 3.1、3.5 |
| 无运行消费者的工具代码 | 删除前端 `LatestRequest`、`relativeTime`、`expandDoTasksSlashCommand`；删除后端 `infer_environment_repair_command`。后者实际修复命令由验收计划提供；只针对已删闲置工具的测试一并移除，保留真实流程测试。 | 3.1、5.1 |
| 测试与实现归属不一致 | Helper 测试中的有效项目路径与脚本启动检查分别移到 `test_project_guard.py`、`test_runtime_scripts.py`；JSON 大小限制测试从实际所有者 http_base 导入。移除确证未使用的 import。 | 7.1 |

前端构建产物与基线相同：已删导出原先未进入生产引用，Vite 已将其消除。

## 保留项与尚未关闭的治理问题

| 项目 | 保留依据／未满足条件 |
| --- | --- |
| Helper 迁移检测及 `--replace-helper` | docs/cli-onboarding.md 有旧安装迁移入口；cli_install/cli_service 在切换及启动时防止双执行，并保留失败恢复路径。不是另一套当前安装实现。维护者确认不再支持旧安装后才能移除检测、迁移参数和对应测试；当前未核实所有存量安装已迁移，支持周期及删除批准仍待维护者确定。 |
| `Database._migrate` | 已持久化数据库的集中迁移边界，有回填、幂等和现有数据库测试。不能因新库不需要而删除，也未操作真实数据。 |
| CLI 无参数及旧参数启动服务 | `cli.main` 和 `test_legacy_server_and_serve_arguments_are_preserved` 明确保留旧入口；当前仓库没有足够真实外部调用方证据。暂保留，需维护者确认支持策略及删除条件，不能宣称已满足 7.4 全部门禁。 |
| `pause_all_tasks`、`pause_run_for_budget` | 仍有关键状态行为测试，但未确认生产入口或外部消费者；本次未删除或重定义这些状态契约。需要确认是否继续支持后再决定去留。 |
| `resolve_mobile_message`、`wait_for_workers` | 前者是 mobile-messaging.md 明确的人工恢复入口，后者用于多项真实线程执行测试；均有消费者。 |
| core/service mixin 和 workflow 重导出 | 按业务能力拆分，有结构及行为测试；domain 的 `__all__` 和动态调用均实际使用。不按文件行数机械拆分，也不把重导出误判为无用 import。 |

上述待确认事项是明确记录的未知，不构成已批准的长期兼容策略或永久例外。

## 验证证据

环境：本机 macOS、Python 3.14；测试使用项目唯一入口 `./scripts/test`。所有 HTTP 安全验证使用本机隔离服务与虚构 Token，不访问生产。

| 验证 | 执行与结果 | 状态 |
| --- | --- | --- |
| 修改前基线 | `./scripts/test`：437 项，436 通过、1 跳过 | 已验证 |
| 初次聚焦 | CLI、升级、初始化、HTTP、项目路径、脚本及前端：73 项通过 | 已验证 |
| 传输与路径聚焦 | http_client/runtime_paths、CLI、移动、团队、云端等：83 项通过 | 已验证 |
| 完整回归 | `./scripts/test`：431 项，430 通过、1 跳过，118.696 秒 | 已验证；沙箱探针除外 |
| 最终资源关闭增量 | `./scripts/test tests.test_http_client tests.test_team_transport tests.test_mobile tests.test_decision_client tests.test_self_healing`：32 项通过，无此前 HTTPError 资源警告 | 已验证 |
| 凭证负向路径 | 两个真实本地 HTTP origin；301/302/303/307/308 × RemoteToolClient、RelayAgent、CLI JSON、TeamClient，全部拒绝且目标服务未收到请求；正常请求认证及响应保持 | 已验证 |
| 前端及 CLI 构建 | `./scripts/build-cli` 成功；ZIP 解压后核验文件清单摘要、运行时版本、模块导入、`python -B -m taskboard.cli --help`，无 Helper/Swift 产物 | 已验证 |
| 最终差异 | `git diff --check` 通过；检查已删代码引用及打包内容 | 已验证 |

完整回归执行期间发现的 HTTPError 资源警告已修复，最后的资源关闭改动使用上述 32 项聚焦回归验证，没有重复运行全部测试。测试数量下降来自废弃 Helper 和无消费者工具的专属测试删除；有效脚本／路径测试已迁移，并新增跨 origin 凭证及共享路径测试。

最终产物：`dist/DoTasksCLI.zip`，版本 `0.4.0-3c0f479dca26`，70 个文件。
SHA-256：`370934f469f15be2afce82ca6279ad0a107a5a8cea3f037e8203fcde4923fb84`。

未验证：真实 Codex 沙箱探针（测试要求显式启用）、新用户机器上的 launchd/钥匙串/Codex 完整安装执行、生产发布、Windows。前端未改变可见页面行为，没有将构建通过描述为浏览器端到端验收。

## 独立审查、恢复及人类验收

独立审查者读取原始要求、代码与实际差异，首次以凭证问题给出“要求修改”；修复后独立运行 14 项传输／决策／配置测试，并复核最终异常资源关闭增量，结论为“本次已审差异可接受”。规范优先推荐的 `gpt-5.6-luna` 不在当前可用子代理模型列表，采用可用的继承模型；审查者有要求修改和拒绝的权限。

本次只有工作区源码修改及本地构建，没有运行数据格式变化，也未切换已安装版本。恢复边界是本次差异；如需撤回，应逐项反向应用已审差异，不能覆盖后续用户改动。已安装服务和数据保持当前状态。将来发布需独立授权和目标环境检查；凭证修复不宜通过回退到会跟随重定向的客户端来处理部署问题，应优先修正服务器地址或前向修复。

待用户验收本次差异，并确认上述旧入口与兼容支持策略。独立审查和自动测试不替代人类验收，也不代表全仓没有其他缺陷。
