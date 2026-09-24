# Laya 决策服务交付记录

## 当前范围与风险

用户授权：本地部署 Laya，建立 `laya-dotasks-decision` 独立服务，支持阿里云轻量服务器部署，并适配 DoTasks 的有限决策架构。包含新增隔离运行依赖与本地服务安装；不包含 Git 提交/推送、云端发布、既有 Helper/插件升级。

风险等级 R2：新 HTTP 服务、鉴权、模型依赖和业务调用边界。状态机、依赖关系、修改目标锁、审查放行仍由既有 DoTasks 契约负责。模型输出只提供历史候选排序与异常诊断；审计持久化属于观测，不得成为业务决策来源。

## 影响范围

- `services/laya-dotasks-decision/`：独立 Python 3.12、固定 Laya/模型版本、HTTP API、本地 LaunchAgent 与 Linux CPU 容器。
- `taskboard/decision_client.py`：标准库 HTTP 适配、严格响应校验、off/shadow/rank 模式及回退。
- `taskboard/obsidian.py`：既有直接候选选定后调用建议；候选集合、原分数、历史祖先及项目检索范围保留。
- `core/service/__init__.py`：在状态所有者初始化一个适配客户端。
- `core/service/execution.py`：完成既有 blocked 转换后，记录未识别原因的模型诊断；不会用模型分类进入自愈分支。
- 无数据库 Schema 迁移，无前端变更，无新增 Codex 工作线程，无空队列周期推理。

云模式的调用发生在云端状态服务。仅部署本地决策服务不意味着云端 DoTasks 已启用；模型输入只发往管理员明确配置的 endpoint。服务日志不保存输入文本，DoTasks 原有审计保存分类概率与版本。

## 验证记录

- 最终 `./scripts/test`：343 项通过（包含可选审计锁竞争回归）。
- 调整异常诊断为先完成业务转换、后进行网络调用后，`./scripts/test tests.test_workflow tests.test_decision_client`：37 项通过。
- 服务 `uv run --locked python -m unittest discover -s tests -v`：5 项通过。
- 服务 `uv lock --check`：通过；Linux 解析为 CPU Torch，不含 CUDA 运行依赖。
- 两种 Compose 配置校验通过；`git diff --check` 通过。
- 模型官方 LFS SHA-256 验证通过：`9d628fd971b700382ac6f65920a86f149777b2e748e0c955fb3b19695aa8f204`。
- 本地 LaunchAgent 常驻于 `127.0.0.1:8791`，健康检查、401 拒绝、真实客户端 shadow 调用、停止时回退、离线重启均通过。
- Linux amd64 镜像 `laya-dotasks-decision:0.1.0` 构建通过；在 Apple Silicon Docker Desktop 模拟运行，以非 root、断网、只读根目录/模型/令牌、2 CPU/4 GiB 上限完成健康检查和真实 HTTP 推理。检查时内存约 2.311 GiB；验证容器完成后停止，本地原生服务继续运行。
- 本地 8 次样例调用中位耗时 108.5 ms；Linux amd64 模拟环境中位 2724 ms。不是性能基准，不能作为阿里云主机性能承诺。
- 最终中文诊断集：异常分类 4 条命中 3 条，历史排序 4 条命中 3 条。历史分数是三个等级期望值除以 2，**不是概率**；问题模板版本 `dotasks-decisions-v2`。本地配置继续 shadow，未开启自动排序。样例数量不足以证明领域准确率。
- 完整本地/容器响应、固定模型和镜像信息见 [验证数据](laya-decision-verification.json)。

## 独立审查

按项目规范使用 GPT-5.6 Luna 审查原始代码、测试及事件消费者，审查者有要求修改或拒绝权限。

- 首轮发现 Linux CPU 锁文件未更新：已修复，锁校验通过。
- 审查确认 `failure_decision_advisory` 当前只作为观测事件读取，不触发调度、目标扩展、自愈或其他业务转换。
- 结合运行证据的最终独立结论：接受，保留真实阿里云部署环境边界。复审未发现剩余 P1/P2。
- 补充落实审查中的稳健性建议：可选诊断审计写入失败只记录警告，不把已完成的业务转换报告成失败；有对应锁竞争测试。

## 恢复方案

设置 DoTasks 决策模式 off 并重启对应进程，恢复既有判断路径；停止 LaunchAgent 或 Compose 服务。无需回滚数据库。保留旧模型、镜像和依赖锁，升级按匹配版本切换，禁止覆盖运行中的模型目录。服务不可用时，客户端保留原候选和既有失败处理逻辑并记录原因。

阿里云部署尚需目标机信息与发布授权；真实业务分类准确率、模型概率校准和领域收益需独立标注评测，不能由部署冒烟检查代替。
