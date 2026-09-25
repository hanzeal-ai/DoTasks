# CLI 注册与共享云端

默认云端为 `https://dotasks.hanzeal.com`。在线安装及升级入口已于 2026-09-24 部署。

## macOS 新用户

本地 Codex CLI（包括可识别的 Codex 桌面应用内置 CLI）需要已安装。DoTasks 使用你的本机 Codex 和登录状态，不捆绑另一份 Codex，也不修改全局 Codex 配置。

### 在线安装

```sh
curl -fsS https://dotasks.hanzeal.com/install.sh | sh
```

安装器使用 macOS 系统工具选择 Apple Silicon 或 Intel 包，校验大小、SHA-256 与解压路径，再运行包内 Python。无需预装 Python、Node.js 或 uv。安装成功后自动进入初始化：设置账号密码、检查 Codex 授权、启动本机服务，等待真实云端连接，然后展示账号密码并打开登录页。Codex 未登录时进入官方授权，需要本人完成。

### 下载后直接使用

下载 [Apple Silicon 包](https://dotasks.hanzeal.com/downloads/cli/DoTasksCLI-macos-arm64.zip) 或 [Intel 包](https://dotasks.hanzeal.com/downloads/cli/DoTasksCLI-macos-x86_64.zip)。对应 `latest-macos-arm64.json` / `latest-macos-x86_64.json` 清单提供版本和 SHA-256。解压整个 ZIP 后，在终端执行包内 `./dotasks`，也可双击 `DoTasks.command`。不要只移动其中一个脚本：首次安装需要旁边的 runtime 目录；安装完成后可删除下载目录。

当前版本未使用 Developer ID 签名或 Apple 公证。浏览器下载的文件首次打开可能被 macOS 拦截，可先尝试打开，再到“系统设置 → 隐私与安全性”点击“仍要打开”，并在提示中点击“打开”（[Apple 官方说明](https://support.apple.com/en-us/102445)）；也可以使用上面的官方在线安装命令。不要求关闭系统安全检查。

### Homebrew

公开 Tap 发布后：

```sh
brew install hanzeal-ai/tap/dotasks
dotasks
```

Brew 自动提供 Python 3.14，安装过程不创建账号、不启动服务。运行 `dotasks`（等同于 `dotasks init`）后才配置本机服务并初始化。程序位于 Brew 的 libexec，launchd 引用稳定的 opt 路径；数据继续放在 `~/Library/Application Support/DoTasks`。

### 升级与迁移

任务空闲并暂停云端调度后升级。独立包使用 `dotasks update`，Brew 版本使用：

```sh
dotasks stop
brew upgrade dotasks
dotasks init
```

独立更新会检查个人及团队活动任务，失败恢复旧程序与启动配置；保留账号和任务数据。Brew 升级前必须停服，不能在任务执行期间让 Brew 清理旧版本；它不提供本项目的自动版本回退。卸载 Brew 程序前也先执行 `dotasks stop`，卸载不会清空账号数据。

两种安装不能静默接管对方。迁移时先用旧 CLI 执行 `stop`，然后用新包 `./dotasks install` 或 `$(brew --prefix dotasks)/bin/dotasks install` 注册当前版本，最后运行新 CLI 的 `init`。迁移至 Brew 时仅移除本安装器创建的旧 `~/.local/bin/dotasks` 入口；保留旧运行时与业务数据。旧 Helper 迁移使用包内 `install-cli --replace-helper`。

用户名为 3–64 位 ASCII 字母、数字、点、下划线或短横线，密码为 12–128 个字符。系统权限和 Codex 登录必须由本人确认；已有旧 Token 绑定不能自动转换成新账号，初始化会拒绝覆盖它。重复运行已完成的初始化不会重复注册。

## 查看本机账号

生产 CLI 已包含此命令。

```sh
dotasks account
```

显示本机绑定的账号、密码、云端和设备 ID。密码从 macOS 钥匙串读取，不向云端请求可恢复密码，也不输出 Agent Token。系统可能要求允许访问钥匙串。旧版本未保存过的密码无法自动取回，命令会明确提示。旧 Token 配置没有用户名时提示先完成账号初始化；绑定不一致时拒绝读取钥匙串。

首次注册若钥匙串保存失败，仍保留已完成的云端绑定，成功提示会展示本次输入的密码并提醒妥善保管；以后 `account` 无法取回未保存的密码。自动打开浏览器失败时保留初始化成功状态，并显示可手动访问的地址。终端输出会包含密码，请留意终端录屏和重定向输出。

## 注册和隔离契约

- `POST /api/cli/init` 接收 `username`、`password`、`device_id`、`device_token`。CLI 在首个请求前生成 256 位随机本机密钥并以 0600 权限保存恢复记录。
- 云端使用独立盐和 scrypt 保存密码摘要，仅保存本机 Token 的 SHA-256 摘要。密码、Token 原文不进入云端账号库或请求日志。
- 同一用户名、密码和安装凭证的重试返回同一绑定，不新建账号，不恢复已经被用户暂停的调度；另一设备或不匹配的凭证不能接管已有绑定。
- `accounts/accounts.db` 是唯一身份数据源。账号 ID 由云端生成，所有业务请求都通过 Cookie 或 Bearer 解析账号，再选择 `tenants/<account-id>/` 下的任务库、relay 库、Vault 和文件目录。
- 浏览器 Cookie 与 Agent Token 不能互换；旧版 Basic Auth 在 multi 模式下无效。浏览器登录和退出仍要求受信任 Origin，Cookie 为 Secure、HttpOnly、SameSite=Strict，可注销并有 12 小时有效期。
- 注册和登录共用实例级限流：每分钟 20 次。不能把这个限流当成大规模公网防滥用系统；部署时应结合入口代理的连接和请求限制。
- 共享云端不接收旧本地数据库快照，不读取请求提供的服务器文件路径，不支持共享 `DOTASKS_OBSIDIAN_VAULT`。
- 自动验证命令经所属账号的持久 relay 队列交给本机 Agent 执行，单条最多 60 秒（低于 relay 的 90 秒重领窗口）；共享云端不执行租户的 shell 命令。交付/审查工具等待上限为 600 秒。
- `GET /_agent/v1/status` 是 Agent 认证的只读端点，用真实 WSS 注册状态支持初始化的就绪检查。

本机 `onboarding.json` 保存恢复绑定所需的安装 ID、设备凭证和账号信息；`cloud-agent.json` 保持既有 Agent 契约。两者权限均为 0600，不包含账号密码。新注册密码仅通过系统 Security Framework 存入 macOS 钥匙串，并按云端地址、账号和设备绑定区分；密码不通过命令行参数传递。网络失败或启动超时可重新执行 init；不会因请求超时重复注册。保管设备凭证文件，丢失后当前版本不支持自动恢复绑定。

## 构建与部署准备

维护者在 macOS 运行：

```sh
./scripts/build-cli
```

输出 `dist/DoTasksCLI.zip`，包含 Python 源码运行时、静态前端和安装器；不包含 `.app`，不需要 Node/npm 或 Swift 编译器来安装。发布清单为 `dist/cli/latest.json`，包含版本、大小和 SHA-256。安装和升级只从同一 HTTPS 云端下载，拒绝重定向、校验错误和不安全 ZIP 路径。摘要随 HTTPS 清单发布，不是独立的发布者签名。

云端镜像构建时生成客户端包并提供 `/install.sh` 和 `/downloads/cli/`。日常发布统一使用 GitHub 托管构建与受限 SSH 接收器，详见 [部署说明](../deployment/README.md)。`DEPLOY_ENABLED=true` 时主分支推送触发发布；手动工作流还需选中 deploy。旧 `deploy-aliyun-cli.py` 已退役，始终拒绝执行，不再生成可手工执行的旧发布计划。

发布前备份配置和停止写入后的持久化数据。新代码运行后若失败，保留数据并前向修复；恢复旧版本必须单独审查匹配的代码、数据和备份后新增写入。

共享云端配置：

```dotenv
DOTASKS_ACCOUNT_MODE=multi
DOTASKS_PUBLIC_URL=https://dotasks.hanzeal.com
DOTASKS_BIND_ADDRESS=127.0.0.1
DOTASKS_BIND_PORT=8765
```

multi 模式无需静态 `DOTASKS_HTTP_USER`、`DOTASKS_HTTP_PASSWORD` 或 `DOTASKS_AGENT_TOKEN`。TLS 反向代理需转发 WebSocket，数据库与 tenants 目录需持久化。使用已有脚本时，首次部署增加 `--account-mode multi`；后续 `--reuse-env` 会保留模式。模式切换不隐式迁移账号或业务库，脚本拒绝通过 reuse 参数静默切换。

示例（只有获批发布时执行；镜像标签须替换成已验证版本）：

```sh
./scripts/deploy-cloud-ip --public-url https://dotasks.hanzeal.com \
  --image ghcr.io/hanzeal-ai/dotasks:<verified-tag> --account-mode multi
```

## 验证、恢复和审查

本次按 R2 管理：新增用户身份、设备授权和租户边界，已在保留旧数据和完整备份的前提下切换为共享账号模式。实施者的自动测试和代码复查不能替代独立审查。


```sh
./scripts/test tests.test_accounts tests.test_cli_onboarding tests.test_cli tests.test_cli_update tests.test_deploy_cloud_ip
./scripts/test
./scripts/build-cli
```

2026-09-24 独立发布工作区验证：`./scripts/test` 完整 373 项通过；最终 CLI 变更另跑 27 项通过。`./scripts/build-cli` 构建成功，ZIP 解包后已验证文件摘要、模块导入、升级帮助和工作线程回调脚本。版本 `0.4.0-a51ae023a2a6`，ZIP SHA-256 为 `5cd20e3a8716ab3b55a190c65b1881f9b8c6009d7be9ff0c3421efd35b4fc81f`。

阿里云 CLI 发布已完成，命令记录 `t-hz06y0myzvgueps`。生产镜像为 `ghcr.io/hanzeal-ai/dotasks:c2a634c39b1a282d31f4253d1a1f3c4493acf13a`；后续主分支 `927f1f7` 仅修正发布脚本对旧系统 Python 的兼容及对应测试。生产原镜像为 `3b9b235f4fb470c49092c3540b8102582eb28a88`，配置和全量数据备份位于服务器 `/home/admin/dotasks/backups/cli-20260924T085117Z/`。镜像通过同提交的 Actions 归档校验传输，未向服务器发送 GitHub Token。

公网 HTTPS 已验证：安装脚本与源码一致，升级归档摘要及内部文件校验通过，未登录的 `/api/board` 返回 401，GET `/api/cli/init` 返回 405。线上 ZIP SHA-256 为 `a8bc29e57c617562f953bec974a8f10f81b4b9bc7781f412d55d78cbb198c2b4`；其运行时版本与本地构建一致，ZIP 容器摘要会随构建元数据变化。

覆盖注册并发与重试、密码和凭证存储、浏览器登录退出、Cookie/Token 互换拒绝、跨账号任务读取/Agent ID 伪造/文件路径拒绝、真实 HTTP/WSS Agent 连接、本机命令回传、init 失败恢复、PATH 安装以及共享云端配置保留。CLI 的 launchd 安装/启动测试使用隔离夹具和模拟，未重启当前用户服务。尚需新用户机器上验证系统权限、真实 Codex 开发闭环。

独立审查应从真实请求与源文件检查：每个 API/工具/文件/WSS 路由是否只使用当前身份的 runtime；注册重试是否可冒领；密码与 Token 是否泄漏；本机验证是否可能在云端执行；安装失败是否可重试，以及老实例回退是否保留原有数据。

上线前备份整个持久卷，并记录原镜像和账号模式。single 的旧数据库保留在原位置，multi 的账户与业务库另存；不自动导入旧实例。新模式发生问题时停止新用户注册流量并回滚镜像/模式，保留 multi 数据供修复，不能把租户库合并进旧单账号库。CLI 连接失败可以 stop/start 或重试 init，不删除账号、凭证和任务数据。

用户针对此前已发布版本于 2026-09-24 明确反馈“审查通过”，并授权合入主分支。最终 Linux CI 运行 377 项通过（7 项 macOS 专用检查跳过），记录 [35976966446](https://github.com/hanzeal-ai/DoTasks/actions/runs/35976966446)。归档传输与部署相关 16 项检查通过，旧 Python 语法兼容另行验证通过。生产部署和公网分发已验证；新用户机器上的真实 Codex 任务开发验收尚未执行。

本次 account 与初始化展示变更：隔离工作区完整 384 项测试通过，安装包 `0.4.0-21059e2df63a` 已解包验证账户命令和运行时摘要。存储作用域由云端地址、用户名、设备 ID 共同确定；现有密码哈希与设备认证契约保持不变。钥匙串失败不删除或重建已注册账号，浏览器失败不回退已成功初始化。系统钥匙串授权交互尚未实测；测试使用真实 CoreFoundation 编解码并替换 Security 存取调用，不接触用户钥匙串。新增可恢复密码存储属于 R2，独立审查待完成；回退 CLI 不删除钥匙串条目或现有绑定。
