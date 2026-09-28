# App Server → 桌面 IPC Demo

2026-09-28，本机 Codex CLI 0.155.1。独立、可手动运行的 R1 探针，未修改 DoTasks 执行链路或配置，未添加依赖。

## 运行

在桌面宿主提供 `CODEX_APP_TOOLS_PIPE_PATH` 的环境中：

```sh
python3 scripts/demo-app-server-ipc.py --create-child --output /tmp/dotasks-ipc-demo-result.json
```

每次运行都会创建一个持久化测试会话 A。省略 `--create-child` 可禁用 B 的创建。脚本使用当前 Codex Home，只有 IPC 工具发现和读取 A 均成功才会尝试创建 B；创建调用不重试，超时不能等同于未创建，需人工核对。测试会话保留供查看，不自动删除。

脚本按已安装 codex-app-tools 0.1.5 的协议使用 Unix socket、4 字节小端长度和 JSON-RPC。由测试驱动转发真实 A 的 thread/turn ID，不借用当前聊天身份。它验证的是宿主 IPC 是否接受该上下文；即便成功，也不能替代“模型自行调用 MCP 工具”的进一步验证。此内部协议不作为稳定公开接口承诺。

## 实际结果

- App Server 初始化成功，返回 Codex Home `/Users/sanmws/.codex`。
- A：`01a0e60d-52c6-7843-a457-cec82ab2cfc5`，标题 `DoTasks IPC Demo A`。
- turn：`01a0e60d-5705-72d0-98f4-58d672d451b3`，状态 completed。
- 桌面 `list_threads` 独立查询返回 A，并将其列在 Tasks 侧边栏 section 的 itemKeys 中。未进行 UI 截图验证。
- App Server 退出后，桌面 `read_thread` 返回同一轮完整记录及 `DOTASKS_IPC_PARENT_OK`。
- 原生 IPC 在 `tools/list` 阶段返回 EOF；前置只读探针与完整脚本各一次，均由对端关闭连接。按照连续失败停止规则，未继续重试。
- B 未创建：失败发生在发送创建请求之前。

结论：共享存储下的 A 创建、桌面任务列表发现、退出后读取已验证。IPC 连接后的协议交互受阻，未到达携带 A 身份的 tools/call，因此不能据此判断 A 的调用权限，也不能声称 A → B 链路成功。

## 下一步与边界

核对当前运行桌面宿主的 IPC 端点类型、握手协议与已安装桥接版本是否匹配，再恢复实验。环境里存在的其他 socket 未被盲试。没有改用当前聊天的 create_thread 工具创建 B，以免掩盖目标链路失败。

语法解析和脚本真实执行已检查。桌面可视呈现仍可由用户查看 `DoTasks IPC Demo A` 抽查。生产迁移、并发控制、模型 MCP 调用、B 退出后持续执行均不在已验证范围内。
