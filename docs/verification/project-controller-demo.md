# 按项目维护创建会话的 Demo

入口：`scripts/demo-project-controller.py`。本机 macOS、Codex CLI 0.155.1，复用本地 CarryOn 的桌面 IPC 适配器和前一个 Demo 的 App Server 客户端。仅 Demo，不接入 DoTasks 正式调度、不安装依赖、不改桌面配置。

## 当前契约

- 项目以 Codex 原生 projectId 隔离；同时保存规范化绝对路径并检查存储身份，避免同名目录串用。
- 每个项目只保存一个当前创建会话；旧会话进入 retired 历史。使用独立项目文件锁和原子写入。
- 可用且空闲则复用。未加载时先让桌面加载；不是仅凭侧边栏出现判定可用。
- 忙碌、等待批准时保留 ID 并停止新投递。连接错误、快照超时等未知状态不触发替换。
- 已归档/不存在的 ID 清退后补建；确认失效的未归档会话先归档，再创建替代。归档失败不继续创建。
- 新创建会话使用 on-request，保留原生批准门禁。没有自动批准代码。
- 创建任务固定使用对应 projectId、environment=local；不会自动创建工作树。
- 每个 request-id 只投递一次。重复相同请求查询结果；不同内容复用同一 ID 会报错。已有未确认请求时不轮换控制会话，也不接收另一个创建请求。
- 只接受参数匹配的唯一原生 create_thread 结果，并通过桌面快照核对任务 cwd。不能把模型文本中的 ID 当成成功证据。

命名：

```text
DoTasks Demo · DoTasks · 创建会话 · 001
DoTasks Demo · DoTasks · 链路验证 · demo-project-001
```

第一种末尾是项目内递增代数；第二种末尾是调用者提供的 request-id。桌面可能对标题做自己的规范化；脚本校验的是实际工具参数。

## 使用

先从 Codex `list_projects` 获取 projectId。DoTasks 项目示例：

```sh
python3 scripts/demo-project-controller.py submit \
  --project /Users/sanmws/Documents/DoTasks \
  --project-id 5816176c-5aef-4a88-ac51-4c295d376b66 \
  --carryon /Users/sanmws/Documents/CarryOn \
  --request-id demo-project-001 \
  --title 链路验证 \
  --prompt '仅回复 DOTASKS_PROJECT_CHILD_OK。不要调用工具，不要修改文件，不要创建其他会话。'
```

相同命令重复运行只核对同一请求。更换 request-id 才表示授权创建另一个任务。`ensure` 只创建/复用项目控制会话；`status` 只读取 Demo 存储。这两个操作省略 request-id/title/prompt。退出码 0 表示请求完成或 ensure/status 成功，2 表示请求尚未完成/失败状态，1 表示前置阻塞。

默认存储：`~/Library/Application Support/DoTasks/demo-project-controllers/`，文件名为 projectId 的 SHA-256。JSON 包含项目、当前 ID、代数、退役历史、请求和真实结果 ID。可通过 `--state-dir` 指定其他目录。状态包含任务文案，应保留在本机。

## 已观察的真实结果（2026-09-28）

- 首次 ensure 创建 `001`：`01a0e61b-1c7b-79c1-aafd-cebafe74841a`；第二次 ensure 返回 reused，ID 不变。
- 请求 `demo-project-001` 投递给该控制会话，桌面轮次 `01a0e61b-b971-7e70-a0c5-d45a25097c43`。曾观察到 waiting_approval，之后用同一请求核对到原生创建成功，没有再次投递。
- 任务会话 `01a0e61b-fb43-7951-9250-1404c1020b91`，标题 `DoTasks Demo · DoTasks · 链路验证 · demo-project-001`。独立 read_thread 返回 `DOTASKS_PROJECT_CHILD_OK`。桌面原生快照 cwd 为 `/Users/sanmws/Documents/DoTasks`，workspaceKind=project。
- 观察到部分桌面会话数据库 cwd 为空，read_thread 摘要也可能显示 `/`；因此结果校验使用原生快照 cwd，不用空路径的 resolve() 推断项目归属。
- 用独立 App Server 归档仍由桌面持有 writer 的控制会话遭到拒绝：`already has an active writer`。没有强行释放 writer。随后通过桌面归档工具将本 Demo 的 `001` 归档，触发 archived-ID 补建演练。
- 再次 ensure 自动创建 `002`：`01a0e61d-6650-7411-9d44-6a050942d0cf`，存储当前 ID 已切换，retired 中保留 `001`。随后 ensure 返回 reused。
- `002` 创建请求 `demo-project-002`，真实任务 ID `01a0e61f-05d6-7cc0-bf92-5a069640468f`，名称 `DoTasks Demo · DoTasks · 轮换验证 · demo-project-002`。原生结果参数核验通过，独立 read_thread 返回 cwd 为 DoTasks、状态 completed、回复 `DOTASKS_PROJECT_ROTATION_OK`。
- 最终保留一个未归档的项目创建会话 `002` 和两个测试任务会话；旧创建会话 `001` 已归档。两个请求均为 completed，没有待批准或待确认请求。

## 验证与边界

```sh
python3 -m unittest tests.test_demo_project_controller -v
```

11 项测试通过，覆盖持久化复用、项目隔离、失效先归档后创建、已归档/缺失补建、忙碌/等待/连接异常保留、幂等核对、发送结果不确定不重试、归档失败不替换、锁与身份校验，以及原生结果参数、真实项目和模型伪造 ID 校验。

真实任务创建和回复已验证；多项目隔离和“未归档但失效”的自动归档分支由测试替身验证。独立 App Server 无法归档桌面仍持有 writer 的会话，此时 Demo 停止并保留状态，需桌面完成归档后核对。它不会为了轮换打断正在执行的任务。

进程在控制会话创建/归档过程中退出、或发送后未收到 turnId 时保留中间状态，不自动重放。retiring 状态在桌面确认归档后可再次 ensure 完成补建；其余不确定状态未实现自动修复，需检查真实会话后再人工修复 Demo 记录。生产接入、内部 IPC 升级兼容和自动恢复不在本次范围内。
