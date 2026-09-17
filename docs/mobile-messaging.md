# 手机原会话追加

服务端 `core/service/mobile.py` 保持 task/thread 绑定权威；`taskboard/mobile_bridge.py` 通过现有个人 Agent 认证调用服务端；原执行机器运行 `python3.14 -m taskboard.mobile_worker`。移动操作只挂载在已认证的 Agent HTTP 通道，不作为自主生命周期 MCP 工具发布。私有配置复用原 cloud-agent 配置，移动入口要求 HTTPS。

## 调度和失败行为

手机请求入队时必须匹配已有 Codex app-server 原会话及 host；同消息 ID 重放不会再次执行。worker 只领取本机的请求，恢复原 thread，不覆盖 cwd、model、sandbox 或 approval 设置。无法恢复时失败，不新开替代会话。

第一版手机轮次与开发、审查、需求分解全局串行。已有普通任务运行时手机请求等待；starting/running/uncertain 手机记录阻止新调度。调度关闭时不会领取。线程可能被多个任务复用，因此目前不采用仅 task 级锁。

执行提交后连接异常、超时或重启会进入 uncertain；不自动重放。追加结果不改变原已结束 run 的生命周期。追加 Token 消耗复用原 run 的权威核算方法，计入原任务有效预算；到限中断追加并拒绝后续入队。任务本身的完成状态不倒退；缺少原会话日志用量基线时不会开始执行，结束未收到用量则保留待核对状态。

## 数据升级与回退

本次 schema 22 -> 23 仅新增 mobile_messages 表及索引（包含运行归属和提交前预算基线），保留任务、run、事件和原线程映射。健康 v22 数据库不执行旧版本归一化。2026-09-10 已授权迁移生产数据库，完整性及原业务行不变已验证，保留部署前备份。

部署授权后：暂停调度，停止所有写入进程，使用 SQLite backup API 创建一致备份，验证 integrity_check 并记录 user_version、任务/run/事件数量。部署后检查 version=23、原业务数量与关联不变、表可入队，并先完成受控测试再恢复调度。不要复制单个活动 WAL 数据库文件作为备份。

回退优先关闭网关和 worker，保留新表及数据进行前向修复。需要降级代码时，先确认无 starting/running/uncertain 轮次、备份升级后数据库，验证旧代码兼容性；不要直接修改 user_version 或删除表。恢复升级前备份仅限确认可丢弃其后写入的维护窗口，否则会丢失业务数据，需要单独批准。

## 结果不明的人工恢复

1. 停止手机 worker，暂停普通调度；保留数据库备份。
2. 在原执行机器检查记录的 thread_id、turn_id 和 app-server 日志，确认该轮已结束或明确中断。缺少 turn_id 时仍须核对原线程没有遗留执行。无法确认时保留 uncertain。
3. 仅在确认原轮停止后，在服务端数据目录上调用 `TaskboardService(data_home).resolve_mobile_message(message_id, thread_id, turn_id, evidence, final_usage=final_usage)`。参数必须来自实际记录；evidence 记录核对依据。已准备执行的消息必须提供与持久化基线处于同一计数空间的最终累计用量 final_usage（token_used、input_tokens、cached_input_tokens、output_tokens、reasoning_output_tokens）；恢复会先结算原任务遗漏用量。无法核对用量时不能解除锁。该方法要求调度暂停和状态 uncertain，不暴露给手机或自主 MCP 工具。
4. 恢复操作将记录结束为 failed 并记录审计事件，保留原请求，不自动重发。人工决定是否需要新消息，再恢复调度与 worker。

验证命令：`python3.14 -m unittest tests.test_mobile`，然后 `python3.14 -m unittest discover -s tests`。真实跨机器执行、HTTPS 部署与钉钉手机展示仍需目标环境验收。

## 2026-09-10 联调核对

BUG-0002 的手机消息“回复你好”已进入原 thread，生成“你好！”且未调用工具。恢复 app-server 后 Token 总计归零，旧实现误将历史累计量相减，触发 uncertain。已根据该 turn 唯一 token_count 及终止记录核对，消耗 217514 Token（input 217508、cached 6528、output 6、reasoning 0），按历史持久化基线加本轮实际用量归一化后调用 operator recovery；原消息不重放，回复已补发并被钉钉接受。生产任务预算 60000，后续追加应保持 budget_exceeded。

新 worker 忽略与历史基线完全相同的限额通知快照；仅接受 total-last 等于历史基线的连续计数，或 total 等于 last 的新计数空间。随后累计单调核算，无法证明计数起点时保留 uncertain，不猜测用量。恢复时若计数空间重置，必须保留本轮原始用量证据，再以持久化基线加已核实本轮消耗归一化 final_usage，不能直接提交重置后的 total。

预算中断依赖模型用量通知，一次推理可能超过剩余额度；加载长会话上下文也计费。真实复测应选择有预算且获得用户允许的会话，不自动重放已执行消息。
