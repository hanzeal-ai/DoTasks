# 新增任务 / 新增需求：Tiptap 官方 Simple Editor

## 当前决策与范围

用户确认采用开源 Tiptap 官方 Simple Editor，使用常规富文本交互，不将图片、文件、录音全部正文块化。两入口共享 IntakeEditor + IntakeSimpleEditor。编辑器本体、工具栏、下拉菜单、链接弹层及图片上传节点来自官方组件；项目仅适配表单、Markdown、受控上传与本地样式。

- React 19.2.8；Tiptap 相关依赖固定 3.31.3，官方 UI 模板 MIT，未引入 Pro 服务或付费扩展。
- 组件来源、原始 SHA-256 与许可：`web/src/registry/{NOTICE.md,upstream-manifest.json,LICENSE}`。模板来源 https://tiptap.dev/docs/ui-components/templates/simple-editor ，公开 registry 下载日期 2026-09-28。
- 启用标准 Markdown 可保存的标题、加粗、斜体、代码、引用、列表、链接、图片、历史。未启用字体颜色、对齐、下划线等无法由现有 Markdown 契约无损表达的工具，避免显示能编辑、提交却丢失的能力。
- 图片在正文上传节点中处理，成功成为正文图片；普通文件和录音沿用原有附件区。无新增录音采集。
- 旧编辑器依赖、专属 CSS 和底部图片上传状态实现已移除；未改部署产物、核心调度或其他页面业务。

## 选型依据

| 方案 | 维护与许可 | 现成 UI / 接入成本 | 本项目结论 |
|---|---|---|---|
| Tiptap Simple Editor | 活跃生态；本模板及所用组件 MIT | 官方标准工具栏、图片上传节点；需接后端和样式，源码随项目维护 | 用户确认；常规富文本且无需商业服务 |
| CKEditor 5 Classic | 成熟维护；GPL / 商业授权 | 成品型 UI；官方 Markdown 输出；需评估项目授权 | 若接受相应许可可用，不擅自引入 |
| TinyMCE | 成熟维护；GPL / 商业授权 | 传统完整工具栏；HTML 保存与 Markdown 之间增加转换 | 当前不选 |
| MDXEditor | MIT，React / Markdown 直接集成 | Markdown 体验便捷，但通用富文本定位较窄 | 本轮已替换，不保留兼容分支 |
| BlockNote / Plate | 主体分别 MPL-2.0 / MIT，部分资源另有许可 | 块编辑或可组合组件，当前无相应额外需求 | 不引入额外交互 |

官方依据：
- https://tiptap.dev/docs/ui-components/templates/simple-editor
- https://tiptap.dev/docs/editor/markdown （目前标注 Beta，必须以回归实证约束边界）
- https://ckeditor.com/docs/ckeditor5/latest/features/markdown.html
- https://ckeditor.com/docs/ckeditor5/latest/getting-started/licensing/license-and-legal.html
- https://www.tiny.cloud/docs/tinymce/latest/basic-setup/
- https://www.tiny.cloud/docs/tinymce/latest/license-key/
- https://mdxeditor.dev/editor/docs/overview
- https://www.blocknotejs.org/docs/foundations/supported-formats
- https://platejs.org/docs/media

体积以当前实际构建为准，不以 npm 包体积猜测：编辑器懒加载 JS 约 615 kB（gzip 192 kB）、编辑器 CSS 79 kB（gzip 10 kB）；共享 Markdown 内容模块约84 kB（gzip24 kB）。保留 >500 kB chunk 提醒；不是轻量化优化项目，也未宣称体积更小。

## 契约、安全与生命周期（R2）

- `title`：首行纯文本，最多120字符；纯附件回退文件名。`goal`：Markdown，纯附件回退既有提示；`visual_references` 从正文 Markdown AST 收集成功图片并去重，加原有文件附件。
- 图片实际 POST `/api/visual-artifacts`，读取 XHR 上传进度，返回的 artifact ID 必须符合受控格式。提交不重复上传正文图片。最多8个正文图片/附件，每个非空且不超过10 MiB；图片仅 PNG/JPEG/GIF/WebP。
- 使用官方上传节点标准布局，对真实请求错误提供重试、移除；多个图片部分失败时保留失败项和正确文件名。清空、关闭、重置会取消请求；批次令牌阻止迟到响应回插。忙时重试不删除排队文件。未完成上传节点阻止提交。
- 官方图片 Markdown 序列化在特殊文件名边界不够安全，局部使用 mdast 序列化图片，确保引号、方括号被正确转义。其他富文本由官方 Markdown 扩展序列化。
- ManagedImage 限制 HTML/Markdown 图片解析入口，并禁用外部图片输入规则，避免粘贴或键入内容触发第三方请求。普通文本粘贴不启用格式 paste rules；格式化 HTML 由 schema 解析。提交再次拒绝原始 HTML 和不安全链接。
- 图片 GET `/api/visual-artifacts/content` 继承现有认证、Host 和租户边界，仅受控图片 ID，正确 MIME、nosniff、private/no-store；任务详情与需求卡片展示保存图片。
- 本地 Vite 代理原先只改变 Host，不改浏览器 Origin，导致上传403。现在仅对 `Origin == http://<loopback Host:port>` 转换为后端 Origin；外部来源、null、不同端口保留并由后端拒绝。不修改生产来源校验。
- 沿用原附件生命周期：终态释放可能使图片不再可用；上传后放弃草稿不主动物理删除，避免删除被复用资源。无数据迁移、存储清理、调度重启。

## 验证

```sh
npm --prefix web run test:intake
node --test web/checks/dev-api-proxy.test.mjs
npm --prefix web run build -- --outDir /tmp/dotasks-tiptap-build
npm --prefix web audit --omit=dev
python3 -m unittest discover -s tests -p 'test_static_frontend.py'
python3 -m unittest discover -s tests -p 'test_server.py'
```

可通过 `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` 使用已有 Chromium，本次使用缓存的151版。浏览器写入均路由隔离；真实代理集成另启动临时目录后端，通过完整 Vite→HTTP 后端→图片读取链，不修改用户真实数据。

覆盖两入口：中文 CDP composition/commit、加粗斜体、历史重置、复制与格式/纯文本粘贴、实际 PNG 解码、文件选择/粘贴/拖拽、多图、失败重试、在途提交拦截、reset迟到响应、删除撤销、纯文件提交、8个/10MiB/空文件边界、录音预览控件、特殊文件名提交、外部图片无请求、部分成功清空、忙时重试、390px窄屏。

中文自动测试不代表所有系统输入法人工覆盖；录音使用测试文件验证控件和附件路径，未验证真实麦克风采集。真实代理测试要求同源上传201、图片读取200且字节一致，外部/null/错端口来源403。

独立审查 `review_complete_editor` 基于代码和独立 Markdown AST 证据要求修复外部图片规则、特殊文件名丢引用、清空批次回插、忙时重试丢文件四项问题；修复后复核接受实现，无新增阻断。

## 恢复与累计改动

本轮替换涉及 `web/package*.json`、共享编辑器、新增官方 registry / IntakeSimpleEditor、CSS、浏览器测试、选型文档；上传修复涉及 `web/vite.config.js`、`web/dev-api-proxy.js`、真实代理集成测试及静态检查适配。

本重试链此前已修改的 App 懒加载、intake-content、taskboard-app 图片提交/展示、server受控图片GET、server/cloud/accounts回归继续保留。与编辑器无关的 core/service、页面和 static 预存改动不覆盖。

不提交、不推送、不部署，构建输出仅 `/tmp`。撤回时按差异恢复编辑器与对应适配，不整体还原有其他改动的 App、taskboard-app、tests 文件，不删除附件。原DoTasks任务由用户删除，本次为用户直接授权继续开发，不另建任务。

本地测试入口 http://127.0.0.1:5173/ ，代理到本次源码后端8766；原8765服务保留，调度继续暂停。

### 最终实测结果

- 浏览器回归20/20通过（41.4秒），包含官方标题/列表菜单与链接弹层的实际点击。已检查桌面与390px截图，正文图片解码正常。
- 真实后端代理集成1/1通过，同源上传201、预览200；不可信来源仍403。
- Python相关检查：static_frontend 24/24、server 19/19通过；生产依赖 audit 0漏洞；构建通过，保留上述体积提醒；差异空白检查通过。
- 独立审查最终接受实现和归一化文档；审查指出的旧注释和无引用样式已清理。
- 本地5173仍返回200；同源空上传请求已到达业务验证（400而非来源403），健康检查显示调度仍暂停。

### 开发缓存失效修复

用户实际5173会话出现 `504 Outdated Optimize Dep`，旧依赖URL请求可复现504，导致React懒加载拒绝并使页面崩溃。开发服务与5190测试服务此前共享同一优化缓存，另加本轮更换依赖，旧会话资源版本失效。现在 Vite cacheDir 按 mode 隔离，Playwright明确使用 `intake-test`，日常服务使用 `development`。已强制重新预构建并重启5173；已有错误会话需刷新，不更改业务数据。

复验：保持真实5173浏览器会话期间运行5190的完整20项回归（全部通过），development缓存元数据前后完全一致；随后刷新5173，实际打开新增任务和新增需求成功，页面异常/5xx记录均为空。真实代理与缓存配置测试2/2、静态检查24/24和独立临时目录构建通过。未创建真实任务或需求。
