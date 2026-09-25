# 服务器优化交付记录（2026-09-25）

## 范围与授权

用户授权处理服务器架构/部署优化；随后明确不需要业务数据或异地备份。此次不建立业务备份任务，不删除既有数据。保留配置旧副本仅用于撤销本次同镜像变更。MarkFix 转正式运营及 Resend 接入已授权准备，发件 noreply@hanzeal.com、Reply-To hanzeal.ai@gmail.com；API Key 尚未配置，域名验证和真实投递未验证。

## 已在服务器生效

- DoTasks 256 MiB、MarkFix API 384 MiB、PostgreSQL 384 MiB、dashboard 64 MiB 内存上限；设置软保留、CPU shares 与进程数上限。CarryOn 保留既有 256 MiB 上限。
- 四个容器 json-file 日志限制为每文件 10 MiB、最多 3 文件。
- 8765、8766 仅绑定 127.0.0.1，Nginx 原 HTTPS 域名和 IP 入口保留。
- 每五分钟采集资源与健康，保留 14 天指标；低内存、低磁盘、缺失/异常容器和 CarryOn 不可用写入本地告警并使 systemd job 失败。没有外部通知。
- 更新服务器两套 operator-owned Compose 与 MarkFix 当前 release 配置；未构建、拉取、替换业务镜像，未执行迁移。

配置恢复副本：服务器 `/var/lib/hanzeal-ops/config-y8ul1fju`，恢复方法见 [运维说明](../deployment/host/README.md)。

## 线上证据

2026-09-25 13:29–13:33（Asia/Shanghai）核验：四容器健康，无 OOM，重启计数均 0；DoTasks 未登录 board 返回 401，MarkFix/CarryOn health 返回 200，DoTasks auth/status HTTPS 检查通过。检查实际 HostConfig，内存、CPU shares、进程上限、日志和回环绑定均符合配置。宿主机可用内存约 1066 MiB。

镜像前后完全相同：

| 服务 | 镜像 ID |
| --- | --- |
| DoTasks | sha256:47a95c1cb627feb258d49e349a8a9637650751e90d517cc33b1129f6627698d0 |
| MarkFix API | sha256:1cb8941a3d34e36239db20985d28dbc49b1d7165d2f257da66084e1d7e9397fe |
| PostgreSQL | sha256:b07129cc272f688c98f5b343138a0a52fa45b3d82f50d7a53ff441330624cd2e |
| dashboard | sha256:5aefab0345110cef609cf74300d5d13fb10171c144b00369091b874d0448a22f |

PostgreSQL 参数 shared_buffers=128MB、max_connections=100、work_mem=4MB，初查11连接。切换后16次只读查询（8并发）通过；这是基本连接检查，不证明生产高峰容量。后续按监测结果调整初始预算。

阿里云调用记录：切换 `t-hz06y3pw9vqeww0`；详细验收 `t-hz06y3q27k37nk0`；复查 `t-hz06y3qe6orb4sg`。本机原始详细输出 `/tmp/host-post-evidence.txt`；长期指标在服务器 `/var/lib/hanzeal-ops/metrics/`。

## 本地完成

- 退役危险的旧 `scripts/deploy-aliyun-cli.py`，任何调用只输出受限 SSH 发布入口指引，不生成或执行旧脚本。同步调用文档及副作用拒绝测试。该修改在工作区，未提交/推送，不表示所有外部副本已消失。
- DoTasks Compose 和首次部署生成器、MarkFix Compose 同步资源/日志配置。
- MarkFix Resend 适配、生产启动门禁、生产配置文件及接入说明已完成；无新增依赖。仍需配置密钥、域名验证、审查既有演示账号、按已审变更构建发布与投递验收，才能宣布正式运营。
- 旧镜像候选清单见 [JSON](host-image-cleanup-plan.json)。保留容器、历史配置引用、每仓最近两版及七天内镜像；用户随后明确授权删除，11个候选已在持有两项目部署锁并重新核对引用后定向删除（调用 t-hz06y3qsbkhmv40），镜像数47降为36，Docker报告镜像占用约减少18MB；服务仍健康。没有执行全局 prune 或删除数据卷。

## 验证与独立审查

DoTasks 全量440项通过、1项显式平台探针跳过；后续运维/退役/部署聚焦29项通过，补充缺失容器监测测试通过。MarkFix format、lint、typecheck、全库测试、build通过；邮件最终小修后API119项通过，并重跑format/lint/typecheck/build。

独立审查者直接检查原始配置和代码，要求修复恢复循环提前退出及邮件JSON异常泄漏，两者均已修复并补测试。审查接受同镜像配置切换与Resend本地准备；未将生产邮件或镜像删除计入接受范围。

未提交、推送本地变更，也未发布其他正在进行的工作区应用改动。此次仅完成已明确可执行的运维项；不宣称已完成真实业务峰值、生产邮件投递或正式运营验收。

## 用户自行填写 Resend Key

服务器已创建 `/home/admin/markfix/resend.env`，权限0600、所有者继承app.env，Key留空，发送/回复地址预填。生产overlay已暂存至 `/usr/local/libexec/markfix-deploy/compose.production.yaml`，只做Compose合并校验，不激活、不重启。调用 `t-hz06y3qyhipc3y8`。用户填写Key后仍需域名验证和发布已准备的邮件接入，不能仅凭填入Key宣称已启用正式邮件。
