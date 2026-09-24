# CLI 注册与共享云端

默认云端为 `https://dotasks.hanzeal.com`。在线安装及升级入口已于 2026-09-24 部署。

## 新用户

1. 安装 Python 3.14+ 和 Codex CLI。当前安装包不捆绑或自动下载这两个运行时。
2. 在线下载安装器后执行（命令如下），或解压 `DoTasksCLI.zip` 运行 `sh ./install-cli`。安装器配置 zsh/bash 的 PATH 和两个 launchd 服务，不安装 Helper App。
3. 在线安装器安装成功后自动执行 `dotasks init`，输入用户名和密码；离线安装后需自行执行该命令。如果 Codex 尚未登录，会进入官方登录流程，需要用户本人完成该授权。
4. CLI 自动注册账号、绑定本机凭证、保存配置并执行 `start`。本地服务健康且云端确实收到 Agent 连接后才显示初始化成功，随后显示账号与密码，并自动在默认浏览器打开云端。
5. 打开默认云端，在现有登录页输入刚才创建的用户名和密码。新账号默认启用任务调度。

```sh
curl -fsS https://dotasks.hanzeal.com/install.sh | sh
```

安装器依次显示检查环境、下载、校验、安装与初始化进度。请在交互式终端执行；账号输入与授权从终端读取，不会把管道中的脚本当作输入。单独执行 `curl -fsS URL` 只显示脚本，不会安装。

无需手动执行 `export`，无需复制 Agent Token。后台服务由 CLI 直接通过 macOS launchd 管理；目前只支持 macOS，仍需 Python 3.14 和已授权的 Codex CLI。

```sh
dotasks update --check  # 下载并校验最新版本，不切换
dotasks update          # 校验、切换版本、恢复原有运行状态
```

升级保留账号、设备凭证和任务数据，拒绝已检测到的活动任务；新版本启动失败会恢复旧版本及服务配置。升级期间请暂停云端调度，避免空闲检查后有新任务进入。旧 Helper 用户在任务空闲时可运行 `sh /tmp/dotasks-install.sh --replace-helper`，关闭旧启动项并安装 CLI，随后执行 `dotasks start`；旧 App 文件保留供人工恢复。

用户名为 3–64 位 ASCII 字母、数字、点、下划线或短横线，大小写统一为小写；密码为 12–128 个字符。DoTasks 自动绑定不替代 Codex 登录或 macOS 的系统授权。

`dotasks init --cloud-url https://other.example` 支持其他共享云端。当前每个账号绑定一个安装；不提供改绑、密码找回或跨账号迁移。已有旧版 Agent 配置不会被 init 自动覆盖。

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

云端镜像构建时生成客户端包并提供 `/install.sh` 和 `/downloads/cli/`。发布使用阿里云 CLI 的轻量服务器远程命令接口；先运行下列命令检查计划，独立审查通过后才加 `--execute`：

```sh
python3 scripts/deploy-aliyun-cli.py \
  --region cn-hangzhou \
  --instance-id 768b01b0e1b44e1b8aa750cdcffd13e6 \
  --image ghcr.io/hanzeal-ai/dotasks:<40位已验证提交SHA>
```

脚本保留旧配置及持久卷备份，停止 DoTasks 后切换到共享账号模式，验证下载入口和未登录访问限制。失败恢复旧镜像配置；数据备份保留供人工恢复，不自动覆盖新产生的数据。主分支推送仅构建镜像，不自动部署。发布可增加 `--github-artifact-id <id>` 使用同一提交的 Actions 镜像归档：本地认证获取短期下载地址，校验归档 SHA-256 后在阿里云服务器导入镜像，不向服务器传输 GitHub Token。

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
