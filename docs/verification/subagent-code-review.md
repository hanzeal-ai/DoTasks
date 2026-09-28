# 执行会话内的子 agent Code Review

风险等级 R2：涉及审查回调、任务状态与执行会话恢复。用户授权本地实现及验证；不包含发布和生产任务改写。

## 行为与影响范围

- 看板保留任务队列、开发中、待处理三栏；`code_review` 是开发中的内部阶段。
- 开发下发提示明确由执行会话选择合适的独立子 agent 审查。交付后，审查调度恢复执行会话；子 agent 返回结论，执行会话提交 `review_code`。
- 保留现有交付证据、审查项分区校验与批任务传播契约。第一次失败可返工，第二次失败进入 `waiting_confirmation` 并关闭自动派发和自愈安排，适用于实现、项目及环境分类。
- 原执行 turn 完成前，后续同任务或同会话运行暂存。退出后重试，合并重复 run，尊重调度暂停及运行失效。
- 个人和团队执行绑定均支持由原会话协调审查；团队身份、版本及执行门禁保持原校验。

## 验证

2026-09-28，本地 Python 3.14、已安装 Node 与 Chrome：

- `./scripts/test`：491 项，成功，跳过 1 项。跳过项目保留原测试条件。
- 随后的执行器串行修复：`./scripts/test tests.test_local_executor -q`，16 项通过，覆盖回调早于 turn 完成、idle 周期恢复、取消、暂停、去重。
- `npm --prefix web run build`：成功；已有编辑器分包超过 500 kB 的构建提醒。
- `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' npm --prefix web run test:intake -- review-board.spec.js`：1 项通过。使用实际后端 workflow metadata，模拟任务数据，验证桌面三栏、审查与待处理归类和移动端筛选。
- `git diff --check`：通过。

独立审查：接受，无剩余阻断问题。审查者独立检查源文件并执行工作流/元数据/团队生命周期 42 项和最终执行器 16 项测试；提出的同会话并发、重复恢复及自愈标记问题均已修复并复核。

未对真实外部 Codex 任务进行下发联调；自动检查验证提示契约、调度/回调状态和模拟 App Server 时序，不证明模型一定遵守子 agent 指令。

## 恢复

没有 schema 迁移，也没有修改实际任务数据。发布前可仅撤回本次文件差异并重新构建静态资源，保留其他并行工作。若发布后恢复旧版本，应先暂停调度并等待活动 turn 结束，再恢复匹配的后端与静态资源；保留已记录的审查和待处理状态，由用户决定是否重新执行任务，不自动回写历史状态。
