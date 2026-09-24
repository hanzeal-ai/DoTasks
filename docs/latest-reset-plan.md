# DoTasks 最新版本更新与数据清空执行单

2026-09-24。状态：准备中；未执行删除、安装切换或云端发布。

## 目标与授权

使用当前工作区源码更新本机与 https://dotasks.hanzeal.com，清空账号和任务数据，不新建备份。不可恢复删除按 R3 执行；用户已给出目标授权，具体清单待第二次人类批准。源码提交和推送不在本次默认授权内。

## 原始状态

- 本机服务健康，运行 Helper 内旧版本；当前任务、运行和需求分解记录均为 0。无 onboarding.json、团队目录。
- 云端容器 dotasks-cloud-dotasks-1 的 /data/dotasks 对应专属卷 dotasks-cloud_dotasks-data。账号 0，当前任务 8，历史运行 18，活动运行 0；另有两份历史数据库。无 tenants、teams。只读查询记录 t-hz06y0vxwju1o1s。
- 当前本机 projectless-workspaces 下 TASK-0002 与 TASK-0004 均为空目录。
- CLI 0.4.0-57e9ffbd1d98 已构建并解包校验 66 个运行时文件。
- 本轮 ./scripts/test：432 项，431 通过，1 跳过，162.652 秒；独立审查聚焦测试 29 项通过；git diff --check 通过。

## 待批准删除范围

1. 本机 /Users/sanmws/Library/Application Support/DoTasks 下 data、artifacts、logs、native-dispatch-locks、projectless-workspaces、mobile-worker.lock，以及 cloud-agent.json 和 cloud-agent.pre-https-20260910.json。这包含旧任务库及同目录历史数据库、附件、日志、旧设备绑定。
2. CLI 安装验证成功后，删除同目录 DoTasks Helper.app 与 /Users/sanmws/Library/LaunchAgents/local.sanmws.dotasks-helper.plist，旧服务先禁用。
3. 云端专属卷 dotasks-cloud_dotasks-data 内全部内容：旧账号/会话、任务/运行/分解记录、relay、附件、内置 Obsidian 数据及历史数据库。保留卷本身，按新版本重建空库。
4. 不新建备份。现存本机 migration-backups、云端 /home/admin/dotasks/backups 暂不删除；“不需要备份”是否包含这些既存恢复副本，需在第二批准明确。外部项目仓库、全局 Codex 配置与会话、外部 Obsidian vault 不属于任务库清空范围。

## 执行门禁与顺序

- 完成完整测试、前端/CLI 构建、Linux amd64 镜像构建与空数据健康校验，记录产物摘要。
- 审查结论：有条件接受本次专用清空方案；现存发布脚本会自动备份且遗漏团队执行检查，不直接用于本次清空。
- 取得具体清单第二次批准后冻结调度/入口，停止本机 Helper/Agent 和云端容器，重新检查全部任务、需求分解及团队运行与本机执行进程。发现新活动、团队会话或未知状态即停止，不删除。
- 删除批准范围，安装已校验 CLI 和云端镜像；保持域名、TLS、独立服务配置。无备份时数据无法回滚；运行失败保持停机并修复代码，不伪造恢复成功。
- 验证实际源码/镜像身份、服务健康、空账号及空任务、未认证访问受限和 CLI 分发包。用户重新注册账号，旧账号/设备绑定失效。

## 尚未执行

第二次人类批准、数据清空、本机切换、镜像上传与生产切换、新账号注册。
