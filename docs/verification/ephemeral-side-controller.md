# 非持久化会话与临时侧边聊天验证

2026-09-28。用户指的是桌面临时侧边聊天，不是结束后归档的普通会话。本次仅探针验证，没有修改正式调度或会话权限。

## 已完成：独立 App Server 的 ephemeral 会话

- CLI 0.155.1，当前共享 Codex Home，`thread/start(ephemeral=true)`。
- 测试 ID：`01a0e62a-29d4-7463-abeb-816b3a3e956b`。
- 创建返回 `ephemeral=true`、`path=null`，固定文本轮次 completed。
- 独立 App Server 运行期间，CarryOn 桌面 IPC owner 查询超时；未证明可交接给桌面。
- 退出前、退出后：threads 表匹配记录数为 0；sessions/archived_sessions 中无文件名包含该 ID 的 rollout 文件；session_index.jsonl 无该 ID。
- 输出 `/tmp/dotasks-ephemeral-probe.json`。

这些检查证明未发现常规会话存储记录，不代表没有进程日志、诊断信息或服务端记录。也不证明普通 App Server ephemeral 会话等同于桌面临时侧边聊天。

## 已完成：真实临时侧边聊天作为创建入口

CarryOn 对该类型的实际判定为 `sideConversation=true`、`ephemeral=true`，并核对 `forkedFromId` 指向主会话。发现入口从桌面持久化的 client-thread binding 取候选 ID，再通过实时 IPC 快照确认，不能仅凭存储映射判断仍然存活。

`scripts/demo-side-controller.py` 严格检查上述三个字段，只对指定侧边聊天投递一次原生 create_thread 测试指令，保留实际工具调用及回复。相同 journal 重跑不会重复发送；`--after-close` 只检查关闭后的 owner 和常规存储。

用户手动打开并发送 SIDE_READY 后，检测到唯一匹配的侧边聊天 `01a0e62b-7635-7970-a402-e5e2d82c7c03`。实际快照确认 sideConversation=true、ephemeral=true、forkedFromId=`01a0e61d-6650-7411-9d44-6a050942d0cf`。

- IPC 投递成功，轮次 `01a0e62c-7c62-7c82-b5e6-6480583ceffe` completed。
- 原生快照只有一次 codex_app.create_thread 调用，参数匹配，工具状态 completed、无错误。
- 原生工具返回任务会话 `01a0e62c-8f96-7433-b0aa-8c2c6bfff5ec`，标题 `DoTasks Demo · 临时侧边创建验证`。
- 独立 read_thread 确认新任务 completed，回复 `SIDE_CONTROLLER_CHILD_OK`。用户关闭侧边聊天后仍能读取新任务同一结果。
- 侧边聊天打开时：threads 表记录 0、无匹配 ID 的 rollout 文件、session_index.jsonl 无该 ID。
- 用户关闭后：上述三项仍为 0/空/false；快照读取返回 no-client-found，后续单次 owner 查询超时，未获得可复用的实时 owner。
- **关闭后 desktop client-thread-bindings-v1 中仍有 1 条该 ID 的绑定。不能声称完全不留记录或痕迹。**
- 完整探针结果保存在 `/tmp/dotasks-side-controller.json`。最后的报告解析修正使用已保存的真实原生工具结果回放验证，不重发创建指令。

```sh
python3 scripts/demo-side-controller.py \
  --carryon /Users/sanmws/Documents/CarryOn \
  --parent-id 01a0e61d-6650-7411-9d44-6a050942d0cf \
  --side-id 01a0e62b-7635-7970-a402-e5e2d82c7c03 \
  --output /tmp/dotasks-side-controller.json --create-child
```

拿到返回后用相同参数、不带 `--create-child` 读取结果。用户关闭该侧边聊天后加 `--after-close` 检查。本例侧边聊天已关闭，不能再用该 ID 创建任务。任务会话是独立普通会话并保留；非持久化属性只属于临时控制会话。

自动化边界：电脑操作工具对 Codex 应用返回 `Computer Use is not allowed to use the app 'com.openai.codex' for safety reasons.`，没有尝试其他 UI 自动化绕过。本次由用户在 `DoTasks Demo · DoTasks · 创建会话 · 002` 中手动打开和关闭临时侧边聊天。现有 App Tools 与 CarryOn 适配器没有暴露创建侧边聊天的入口。

结论：存活的桌面临时侧边聊天可以通过 IPC 接收指令并调用原生工具创建普通任务会话；没有常规会话历史持久化，但存在桌面绑定元数据残留。不能把它等同于独立 App Server 的 ephemeral 会话，也不能把本次人工打开的验证等同于已实现自动创建临时侧边聊天。没有清理桌面元数据、重启桌面或核查所有诊断/服务端日志。

## 后续验证：App Server 分叉是否形成 ⌥⌘S 侧边聊天

用户明确目标为通过 App Server 打开已有会话的临时侧边聊天，而不是让侧边聊天创建普通会话。

本机 `codex app-server generate-json-schema --experimental` 导出的 ThreadForkParams 支持 `threadId`、`ephemeral`、`excludeTurns` 等字段，没有 `sideConversation` 或 `sideConversationParentNavigationPath`。只读检查桌面代码发现，侧边聊天使用临时分叉，但这两个侧边字段是在桌面会话状态构造中附加的，并非 thread/fork 的公开请求字段。

实际命令：

```sh
python3 scripts/demo-app-server-side-fork.py \
  --parent-id 01a0e61d-6650-7411-9d44-6a050942d0cf \
  --project /Users/sanmws/Documents/DoTasks \
  --carryon /Users/sanmws/Documents/CarryOn \
  --output /tmp/dotasks-app-server-side-fork.json
```

结果：

- 独立 App Server 调用 thread/fork(ephemeral=true, excludeTurns=true) 成功。
- 临时分叉 ID `01a0e632-2778-7960-9ad6-592a4a8a94d3`，forkedFromId 正确指向主会话，ephemeral=true、path=null。
- 分叉执行固定文本轮次成功，回复 `APP_SERVER_SIDE_FORK_OK`。
- 存活期间，数据库/rollout/session index 均无匹配记录；桌面 client-thread binding 数为 0。
- 对该分叉进行桌面 IPC owner discovery 超时，未取得 owner 或 sideConversation=true 的桌面快照。
- 主会话执行前后均 idle。独立 App Server 已退出，分叉没有常规持久化记录或桌面绑定。
- 语法与真实执行已验证；电脑操作工具仍不可控制 Codex，未将 IPC/元数据检查描述为 UI 截图验证。

结论：这次测试只创建了底层临时分叉，**没有打通外部 App Server → 桌面 ⌥⌘S 侧边聊天的链路**。桌面创建侧边聊天还执行关联与面板状态处理。不能将 thread/fork 成功称为打开侧边聊天成功；当前结果也不构成对所有其他内部入口均不可行的证明。
