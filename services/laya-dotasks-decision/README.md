# laya-dotasks-decision

独立的 Laya 文本决策服务。DoTasks 保留 Python 3.14 与零第三方运行依赖；本服务使用独立 Python 3.12 环境。

## 决策契约

| 接口 | 输入 | 输出 | 边界 |
| --- | --- | --- | --- |
| `POST /v1/history/rank` | 当前请求、1–8 个 `{id,text}` 历史候选 | 全部候选的相关性分数，降序排列 | 不召回新候选、不删除候选、不裁决依赖 |
| `POST /v1/failure/classify` | 一段失败日志或描述 | `environment/project/implementation/unknown` 与概率分布 | 仅诊断，不生成命令、不触发修复 |
| `GET /healthz` | 无 | 服务与模型是否就绪 | 完成权重校验和首次推理后才 ready |

决策请求必须带 `Authorization: Bearer <token>`。不支持任意 Prompt、模型选择、工具调用、浏览器 Origin 请求。请求体最多 96 KiB；query/候选各最多 2000 字符，失败描述最多 6000 字符。模型上限 2048 tokens，预留问题头后超出证据预算返回 422，**不静默截断**。服务一次只运行一个推理，忙时 503，不无限排队。一个候选逐次推理，控制 CPU 内存峰值。

输出包含 `schema_version=1`、问题模板版本、模型 ID/固定 revision、Laya 版本、request_id、耗时、`advisory_only=true`、`truncated=false`。概率尚未在 DoTasks 领域校准，不是自动审批阈值。推理/输出无效返回 503，非法请求返回 422，缺少或错误令牌返回 401。

历史评分使用“不相关 / 部分相关 / 直接相关”三个等级的期望分数，除以 2 归一化为 0–1，返回 `score_kind=ordinal_0_1`；**它不是相关性概率**，不应解释为“正确率”或用于放行阈值。问题模板版本为 `dotasks-decisions-v3`。

固定模型：`convaiinnovations/laya-multilingual`，revision `e4e9ddf21a7b1903b7acffd8814ad4307bf63a67`；运行库 `laya==0.3.20`。依赖由本目录 `uv.lock` 锁定，Linux 显式使用 PyTorch CPU wheel。下载器只下载必要文件，复制后应用 Laya 的 tokenizer 兼容转换，并保存逐文件 SHA-256。每次启动验证本地产物；服务运行时离线，不把输入发送到 Hugging Face。

## 本地部署（macOS）

在本目录执行：

```sh
uv sync --locked
uv run --locked python manage.py init
uv run --locked python manage.py download
uv run --locked python manage.py install
curl --fail http://127.0.0.1:8791/healthz
```

权重和随机令牌保存到 `~/Library/Application Support/laya-dotasks-decision/`，令牌文件权限 0600。下载需访问 Hugging Face；遇到 Xet 网络不可达，可用 `HF_HUB_DISABLE_XET=1` 执行下载命令。已存在的 model 目录不会被覆盖。默认 CPU/2 线程，前台 `run` 可通过 `LAYA_DECISION_DEVICE=mps` 显式选用可用的 Apple GPU。

`install` 注册 `local.laya-dotasks-decision` LaunchAgent，绑定 `127.0.0.1:8791`，登录后启动，异常退出重启；代码和虚拟环境仍位于本目录，不要移动或删除。日志是服务 home 下的 `service.log`，只记录 request_id、操作、状态和耗时，不记录任务正文和令牌。

```sh
uv run --locked python manage.py status
uv run --locked python manage.py stop
# 更新代码/依赖后重新启动：先 stop，再 install。
```

## DoTasks 适配

在**持有 DoTasks 状态的服务**上设置环境变量，并在下次重启时生效：

```sh
export DOTASKS_DECISION_MODE=shadow
export DOTASKS_DECISION_URL=http://127.0.0.1:8791
export DOTASKS_DECISION_TOKEN_FILE="$HOME/Library/Application Support/laya-dotasks-decision/token"
export DOTASKS_DECISION_TIMEOUT=5
```

也可在实际 `DOTASKS_HOME` 下创建权限 0600 的 `decision-service.json`，格式：

```json
{
  "mode": "shadow",
  "url": "http://127.0.0.1:8791",
  "token_file": "/absolute/path/to/token",
  "timeout_seconds": 5
}
```

环境变量优先；无配置默认为 off。配置错误、服务超时、非完整候选响应、无效概率、长输入都保留原流程并记录原因。客户端禁止重定向与隐式代理；远程连接必须 HTTPS。

- `off`：不调用。
- `shadow`：保留候选顺序，附加 `decision_advice`，用于对比评测。
- `rank`：只重排现有最多 8 个直接命中候选；词法分数、候选集合、项目隔离和历史祖先保留。依赖/冲突仍由原有契约决定。
- 两种启用模式都只对未由既有规则/显式分类解释的 `report_run_blocked` 记录分类建议，写入 `failure_decision_advisory` 审计事件；不会用该建议修改 category、self_heal、目标锁或任务状态。

**云模式中状态在云端，因此本机配置不等于云端已启用。** 云端 DoTasks 应连接部署在云端或可通过 HTTPS 访问的决策服务。本任务不会自动重启既有 DoTasks、更新已安装 Helper 或切换云端配置。新的源码启动使用上述配置；打包更新继续遵循项目现有发布流程。

使用项目根目录 Compose 运行 DoTasks 时，仅在宿主 shell 设置变量不会自动注入容器。可在其持久化 `/data/dotasks` 内放置上述 `decision-service.json` 和匹配的令牌文件（供 DoTasks 用户 UID 10001 读取），其中 `token_file` 使用**容器内绝对路径**，URL 使用决策服务 HTTPS 地址。随后在获准的发布窗口重启 DoTasks 容器。无需把服务权重或 Laya 依赖安装进 DoTasks 容器。

## 阿里云轻量服务器部署

已验证的容器目标是支持 Docker Compose 的 Linux amd64 主机；无需 GPU。ARM64 Linux 需另行构建和验证。Compose 对服务设置 2 CPU/4 GiB 内存上限，主机还需为操作系统和其他服务留余量。是否适合现有实例以目标机实测为准，模型下载约 650 MiB，依赖及镜像另需磁盘。不要把本地 `.venv` 复制到 Linux。

1. 在有网络的构建机按本目录 `Dockerfile` 构建目标架构镜像：

   ```sh
   docker build --platform linux/amd64 -t laya-dotasks-decision:0.1.0 .
   docker save -o laya-dotasks-decision-0.1.0.tar laya-dotasks-decision:0.1.0
   ```

2. 通过你已授权的 SSH/SCP 通道复制镜像、Compose 文件及**已验证完整的 model 目录**到服务器。国内实例无法直连 Hugging Face 时，优先传输已下载模型，不切换未经验证的模型镜像来源。目录布局：

   ```text
   /opt/laya-dotasks-decision/
     compose.yaml
     compose.https.yaml
     Caddyfile
     models/model/manifest.json
     models/model/model.safetensors
     models/model/encoder/...
     models/model/tokenizer/...
     secrets/decision-token
   ```

3. 在服务器生成独立令牌，确保 UID 10001 可读，其他用户不可读。不要把令牌写进命令参数或提交到 Git：

   ```sh
   sudo install -d -m 700 secrets
   sudo sh -c 'umask 077; openssl rand -hex 32 > secrets/decision-token'
   sudo chown 10001:10001 secrets/decision-token
   sudo chmod 400 secrets/decision-token
   # 模型无秘密；复制后确保容器非 root 用户能读取。
   sudo chmod -R a+rX models
   docker load -i laya-dotasks-decision-0.1.0.tar
   docker compose up -d --no-build
   docker compose ps
   curl --fail http://127.0.0.1:8791/healthz
   ```

4. 同机宿主进程可通过 loopback 访问。跨主机或其他容器访问应使用 HTTPS：将你拥有的域名 A/AAAA 记录指向服务器，在 `.env` 设置 `DECISION_DOMAIN=decision.example.com`，再执行：

   ```sh
   docker compose -f compose.yaml -f compose.https.yaml up -d --no-build
   ```

   Caddy 管理 TLS 证书，80/443 端口需可达且没有被占用；已有反向代理时只复用现有代理，不启动第二个 Caddy。阿里云轻量服务器防火墙与系统防火墙仅按需要开放 80/443，**不要开放 8791**。证书签发需域名/DNS与网络条件满足。DoTasks 连接 `https://decision.example.com`，把匹配令牌安全放入调用端的 token_file。Compose 只在本机发布推理端口。

云端操作需要目标服务器与发布授权；提供部署文件不代表已部署到阿里云。

## 验证与恢复

```sh
# 服务边界测试，使用可控模型替身，不下载模型
uv run --locked python -m unittest discover -s tests -v
# 模型部署后，执行真实 HTTP 决策检查和小型中文诊断集
uv run --locked python smoke.py --token-file "$HOME/Library/Application Support/laya-dotasks-decision/token"
# 项目根目录：项目既有全量测试入口
./scripts/test
```

小型诊断集只用于部署验收，不证明通用准确率，不用于校准或微调。真实业务应另行标注/划分评测集，比较 Recall@K、分类混淆、失败率、p95 耗时、内存和实际节省成本，再决定是否开启 rank。

恢复：将 DoTasks 模式设为 off 并重启对应进程，所有原业务规则继续执行；本地 `manage.py stop` 停止推理，云端 `docker compose stop`。没有数据库迁移。升级先保留旧镜像和模型目录，锁定新版本、跑相同评测后再切换；不要在运行目录覆盖权重。回退到匹配的旧镜像与旧模型，禁止混用。

## 来源

- [Laya 源码与许可证](https://github.com/NandhaKishorM/laya)（Apache-2.0；固定下载模型的使用条件另见其模型卡）
- [多语言模型](https://huggingface.co/convaiinnovations/laya-multilingual)
- [阿里云轻量服务器防火墙](https://help.aliyun.com/zh/simple-application-server/user-guide/manage-the-firewall-of-a-server)

团队负责人建议：`POST /v1/assignment/recommend` 接受 query、candidates、task_revision、candidate_version；只返回候选评分及原版本绑定。云端重新检查权限、容量与版本后，由产品或协调人确认派发。代码新增该接口不代表已有服务已升级。
