# DoTasks MarkFix 修复记录（2026-09-20）

来源：`https://markfix.hanzeal.com`，项目 `23178a81-7dad-4742-98ed-b0b8139e33c4`。本次为本地实现与验证，未提交、推送或部署；线上验收及用户人工抽查尚未执行。风险按 R1 处理：前端组件与展示行为调整，账号状态接口仅在有效已登录会话中增加用户名字段，不改变认证规则。

| 标注 ID | 修复结果 | 验证证据 |
| --- | --- | --- |
| 515f0fe7-44e8-49ad-85b7-9dccd2fb1f71 | Button、Input、Textarea、NativeSelect、Card、Badge、Table、Checkbox、Dialog、SidebarMenu、DropdownMenu 统一组件；动态视图适配同一组件实现。移除重复英文标题、无内容的详情段落和冗余说明。 | 生产构建成功；任务、Token 图表/明细、日志、设置及弹窗浏览器检查；控制台无 warning/error。 |
| 80240134-a812-4b3b-a5db-9b35b5340022 | 设置移动到个人菜单。 | 顶部无设置按钮，个人菜单进入设置并成功保存。 |
| 4c6462d9-35f5-478d-a37b-7651a95c1b01 | 卡片不显示错误正文；失败/阻塞原因通过“查看日志”弹窗展示，需求下的任务也使用该入口。 | 失败任务卡片没有 ERROR_SENTINEL；点击后弹窗标题为对应 TASK-FAIL，展示该任务事件和错误；错误中的 script 标签以文本展示。 |
| 7d1fc653-6d17-41a7-b483-3201703c6ff4 | 左下角显示已登录用户名，菜单含设置和退出；本地免登录显示本地用户。 | 测试账号显示；菜单打开、Escape 关闭和焦点恢复；真实后端测试验证登录后返回用户名，未登录、注销和无效会话不返回用户名。 |
| 74273e43-46e1-4fc0-a916-e3a63e269cd0 | 计数括号改为英文括号。 | DOM 文本 `完成任务(1)`，完成任务弹窗同样使用英文括号。 |
| 1af98f1b-1eff-4113-982a-6a7ebad9c340 | 侧栏菜单组件化，宽度 248px → 192px，移除侧栏数字。 | DOM 实测宽度 192px，sidebar-count 数量 0；390×844 窄屏导航和个人菜单可用、菜单不超出视口。 |
| b1300ed8-36e0-4e11-98c4-cccea86787fb | 移除连接状态展示及其无用的前端更新逻辑。 | DOM 中 health 节点数量 0；保留调度按钮、连接失败反馈和认证失效处理。 |

## 自动验证

```sh
npm --prefix web run build
./scripts/test tests.test_static_frontend tests.test_web_auth tests.test_cloud
./scripts/test tests.test_cloud.RelayHTTPServerTest.test_browser_session_login_logout_and_replay
git diff --check
```

结果：生产构建成功；相关 52 项测试通过；新增无效会话用户名不可见断言后，单项会话回归通过；差异空白检查通过。已检查重新生成的 `static/index.html` 与资源引用。

## 浏览器复现

```sh
python3 tests/fixtures/markfix_preview.py
```

打开 `http://127.0.0.1:5188`。此服务仅绑定回环地址，使用内存中的合成任务与模拟接口，不连接真实数据库，不执行真实调度。它用于复现 UI，不替代真实后端集成测试。

已观察：任务错误转到日志、脚本文字正确转义、设置菜单和用户名、Token 页面切换、完成任务详情、新任务表单及取消、复选框原生表单值、并行设置开启/关闭保存、菜单/弹窗 Escape 和焦点恢复、390px 布局。实际认证与注销行为由上述 Python HTTP 测试验证。

前端保留现有控制器的数据属性和事件委派，`ui-markup.js` 只将已转义的展示模板适配为 React 组件；任务状态、权限及调度仍由原后端裁决。已有 App/AuthGate 用户修改保留。
