# CLI 分发与验证

当前目标：macOS Apple Silicon 和 Intel 下载包自带 Python，使用本机 Codex；Homebrew 提供同一 CLI 的源码运行包并自动安装 Python 依赖。用户名、密码、Codex 官方授权及系统权限仍由用户本人确认。Windows/Linux 本地执行不属于本次支持范围。

## 产物与权威来源

- `scripts/package-cli.py` 生成运行时文件摘要与版本；源码 ZIP 使用固定时间与无压缩存储，确保 macOS 和 Linux 产生同一摘要。
- `scripts/build-portable-cli` 使用 uv 管理的 python-build-standalone CPython 3.14.4，仅是构建时工具；最终用户无需 uv、Python 或 Node。保留完整 Python 发行版及附带许可证。
- 源码包清单为 `latest.json`；原有源码 CLI 升级保持可用。自带 Python 的 CLI 只请求对应 `latest-macos-arm64.json` 或 `latest-macos-x86_64.json`。
- 两个版本化原生 ZIP 和两个直接下载别名在同一镜像发布。清单 SHA-256、字节大小、内部文件摘要、架构及可执行权限一起验证。
- `Formula/dotasks.rb` 由源码包清单生成，使用固定 URL 与摘要。公开 Tap 只包含此 Formula 和用户说明，不公开主仓库。

## 开发构建

```sh
npm --prefix web ci
./scripts/build-cli
python3 scripts/generate-brew-formula.py
./scripts/build-portable-cli
python3 scripts/verify-portable-cli.py dist/DoTasksCLI-macos-arm64.zip
# 可选：使用本机 Codex 做 app-server 握手；不启动模型任务
python3 scripts/verify-portable-cli.py dist/DoTasksCLI-macos-arm64.zip --codex
```

Intel 在对应机器使用同一脚本。构建器核对实际解释器架构，不接受错误标记。每次影响源码包的修改后重新生成 Formula；发布时仍需递增应用版本，不能依赖哈希的字典序作为 Brew 的版本顺序。

## 发布

生产 workflow 先在 `macos-15` 和 `macos-15-intel` 构建、搬移并执行 CLI/MCP，再将两个已校验原生包交给 Linux 构建。Linux 执行项目回归、构建源码包、检查 Formula 是否对应本批产物，然后构建镜像并通过既有 SSH receiver 发布。任一架构失败都不能部署半套产物。

本地 Docker 构建前，需将两种原生包的 `dist/cli` 内容合并至被忽略的 `portable-cli/`；Dockerfile 拒绝缺少任何原生清单的发布。`portable-cli/.gitkeep` 只是目录占位，不是可发布产物。

目标版本的下载地址在线验证完成后，再将 `Formula/dotasks.rb` 同步到公开 `hanzeal-ai/homebrew-tap`。创建仓库、代码提交推送及生产部署需对应授权；本文件不授予执行权限。Tap 的 `brew install hanzeal-ai/tap/dotasks` 自动提供依赖，但不在安装阶段初始化或启动后台。

## 运行与恢复边界

`dotasks` 默认初始化；`dotasks install` 显式注册当前安装的后台服务。两个 launchd 项必须指向相同运行时和解释器。Brew 使用 `opt` 稳定路径，独立安装使用用户目录中的 `cli/current`，两者不能静默接管对方。切换安装方式先停止旧服务，再显式安装，保留账号、任务和旧运行时；不清空业务数据。

原生升级校验包后才切换，保留原版本及启动配置以便失败恢复。Brew 升级由 Brew 管理：空闲时先 `dotasks stop`，升级后 `dotasks init`。运行中不得执行 Brew upgrade/cleanup；也不要在未停服时卸载。Brew 安装的 `dotasks update` 只显示正确升级步骤，不写入 Cellar。

当前无 Developer ID 签名或 Apple 公证。浏览器下载后的第一次启动可能需要在系统“隐私与安全性”中确认；不得以关闭全局 Gatekeeper 作为安装步骤。`curl … | sh` 使用同源 HTTPS、大小/摘要校验及受限 ZIP 解压，使用 `/dev/tty` 读取交互，不会把脚本文本读作账号。

## 验证边界

需要同时报告：真实 Brew 程序安装、直接命令检查、原生搬移/删除下载目录后执行、TLS 信任与 MCP、实际本机 Codex 握手、单元与集成回归、双架构 CI，以及线上下载 SHA/版本一致性。

初始化/launchd 单测使用隔离目录或替身；没有实际注册新账号或完成 OAuth 时，不能将其写成已通过首次用户端到端验收。Intel 的本地 Rosetta 检查不代替 CI 的原生 Intel 检查。升级失败的数据恢复仍遵循既有发布规范。
