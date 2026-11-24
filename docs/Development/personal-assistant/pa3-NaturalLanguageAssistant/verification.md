# PA-03 实施与验证记录

日期：2026-11-23

最新状态：用户后续已授权动态修复和验收；本轮改动未提交。首次静态交付记录保留为历史，最新结论见末节。
分支：`feat/assistant`
业务实现基线：`9c328f9`（T6）。T7部署与文档整改已通过独立静态复核；其提交与本记录一同保存，可通过 Git 日志定位。

## 提交基线

| 阶段 | 提交 | 内容 |
|---|---|---|
| T1 | `6f788e9` | 严格领域模型、SQLite v3迁移、事务/请求/回执/结果存储 |
| T2 | `7577f2e` | 助理应用服务、业务操作、请求与批量提案 |
| T3 | `5d6e23f` | 助理工具、能力档案、可信来源与批准传递 |
| T4 | `bcd424e` | 固定会话、身份、上下文和Host装配 |
| T5 | `c1ab930` | 助理入口与只读结果/提案API |
| T6 | `9c328f9` | 双长驻工作区、结果卡片和批准预览 |

T7的改动仅含Docker打包/身份示例和本文档，不涉及业务生产代码。

## A1—A9 静态实现证据

下表“静态依据”表示对应代码/配置位置存在相关实现；不表示该验收组运行通过。运行、故障、迁移和用户体验验证均未执行。

| 组 | 静态依据 | 运行验证 |
|---|---|---|
| A1 固定入口与普通会话兼容 | `src/dotclaw/assistant/entry_service.py`；`src/dotclaw/bootstrap/session_interaction.py`；`src/dotclaw/channel/web/app.py`；`frontend/src/App.tsx`、`frontend/src/AssistantWorkspace.tsx` | 未执行并发创建、重启、误删保护、切模型/审批或浏览器验证 |
| A2 事务创建/查询/修改/状态 | `src/dotclaw/tools/assistant_tools.py`；`src/dotclaw/assistant/service.py`；现有 `src/dotclaw/personal_tasks/service.py`；`frontend/src/AssistantResults.tsx` | 未执行SQLite业务操作或页面同源核对 |
| A3 提醒与通知边界 | `src/dotclaw/tools/assistant_tools.py`、`src/dotclaw/assistant/service.py`；现有 `src/dotclaw/personal_tasks/reminder_service.py`、`notification_service.py`、`src/dotclaw/scheduler/reminder_scheduler.py`、`src/dotclaw/channel/web/notification_routes.py`；`README.md`、`docs/docker.md` 对扫描和浏览器权限的说明 | 未创建/触发提醒，未验证浏览器通知、网页关闭或调度行为 |
| A4 请求板、澄清与来源隔离 | `src/dotclaw/assistant/models.py`、`src/dotclaw/assistant/service.py`、`src/dotclaw/assistant/context.py`；`src/dotclaw/runtime/application/engine.py` 两个USER_INPUT写入点 | 未验证多轮澄清、跨午夜、压缩/重启恢复；没有真实模型验证 |
| A5 有序查询快照和对象定位 | `src/dotclaw/assistant/service.py` 结果快照读写；`src/dotclaw/channel/web/assistant_routes.py`；`frontend/src/AssistantResults.tsx`、`frontend/src/PersonalTasks.tsx` | 未验证服务端排序、删除事务定位或真实页面跳转 |
| A6 批量批准和权限 | `src/dotclaw/tools/capability.py`、`src/dotclaw/tools/executor.py`、`src/dotclaw/tools/assistant_tools.py`、`frontend/src/useAssistantResults.ts`、`frontend/src/RunActivityPanel.tsx` | 未运行批准/拒绝、过期预览、全局DENY或unattended场景 |
| A7 原子写入、幂等和恢复 | `src/dotclaw/assistant/sqlite_repository.py`、`src/dotclaw/assistant/service.py`、`src/dotclaw/personal_tasks/sqlite_repository.py` | 未做提交前/后故障注入、取消、回执丢失或真实恢复验证 |
| A8 v0/v1/v2到v3迁移 | `src/dotclaw/personal_tasks/sqlite_repository.py` 的连续迁移入口；`src/dotclaw/assistant/sqlite_repository.py` 的助理schema迁移 | 未创建数据库、运行迁移、检查数据保留或验证回滚 |
| A9 有界上下文和来源时间 | `src/dotclaw/assistant/context.py`；`src/dotclaw/context/defaults.py`、`src/dotclaw/context/provider.py`、`src/dotclaw/context/slot_manager.py`；`src/dotclaw/assistant/service.py` 的历史来源验证 | 未运行上下文构造/压缩/分页，未验证时间解释或真实模型轨迹 |

## 本次已裁决的文档同步

- 相对时间来源为已验证用户输入RunMessage的 `metadata['created_at']`；历史值缺失、无时区或格式错误时报明确来源错误，不回退到Run开始时间。Engine在两个用户输入持久化点写入该metadata。
- Web路由直接复用 `SessionDetailResponse` 与 `assistant.models` 严格响应模型；没有新增仅转导出的 `assistant_schemas.py`。
- 普通身份空白工具白名单排除 `builtin.assistant.*`；个人助理的显式白名单保留。`cache_scope=NONE` 不缓存实例、binding或signal订阅；active context replay优先不变。
- 由于用户禁止动态测试，本次及各阶段均不将原计划测试门槛表述为已通过；T7不新增一次性集成测试文件。

## T7 静态核对

实际执行：

- `git rev-parse --short HEAD` → `9c328f9`（本次未提交前的代码基线）。
- `git log --oneline -6` → 上述T1—T6六个提交。
- `git diff --check` → 退出码0。
- `rg`/源码阅读核对：身份白名单、`.dockerignore`父目录放行/逐层排除与精确文件例外、Dockerfile单文件COPY、两份配置的 `tools.policy.rules`、两份Compose既有只读挂载，以及迁移入口/路由响应类型。未执行YAML解析器、Docker/Compose或应用。
- T1—T5的Python静态证据来自各阶段已验收开发记录，不是T7重跑。T1 `.agent-work/pa3-assistant/developer/remediation.md` 记录先运行 `.venv\Scripts\python.exe -c "import sys; print(sys.executable)"` 并确认路径 `D:\dev\dotClaw\.venv\Scripts\python.exe`，随后用同一解释器对5个授权Python文件进行纯 `ast.parse`，未导入应用；T2 `.agent-work/pa3-assistant/developer/T2-static-review.md` 记录对 `service.py/models.py/schemas.py` 的AST语法解析、方法清单和86个本地导入符号核对通过；T3 `.agent-work/pa3-assistant/developer/T3-static-review.md` 记录 `.venv\Scripts\python.exe -c $check` 对9个变更文件做纯AST/文本符号检查通过；T4 `.agent-work/pa3-assistant/developer/T4-static-review.md` 记录13个生产文件AST和本地导入符号检查通过；T5 `.agent-work/pa3-assistant/developer/T5-static-review.md` 整改记录4个生产文件AST和导入符号检查通过。各阶段均注明没有导入应用。
- T6前端静态命令来自 `.agent-work/pa3-assistant/developer/handoff.md` 与 `.agent-work/pa3-assistant/developer/T6-static-review.md`：在 `frontend` 运行 `& .\node_modules\.bin\tsc.cmd --noEmit -p .\tsconfig.app.json`，`TSC_EXIT=0`，无诊断；`git diff --check` 退出码0。该项是 TypeScript 静态类型检查，不是运行前端应用或动态测试。

T7部署事实：`.dockerignore` 原本全局忽略构建上下文；先放行 `.dotclaw/` 后排除 `.dotclaw/**`，再放行 `.dotclaw/agentConfig/` 后排除 `.dotclaw/agentConfig/**`，最后只放行 `personal-assistant.yaml`。PowerShell静态序列比较结果为 `DOCKERIGNORE_SEQUENCE=exact`，未运行Docker/BuildKit。Dockerfile在创建目标目录后通过单文件COPY并设置 `dotclaw` 所有者。`compose.yml` 和 `compose.dev.yml` 的只读身份目录挂载保持不变，挂载源缺少该身份时会遮住镜像文件；`docs/docker.md` 提供只复制该文件且目标已存在时不覆盖的步骤。

## 未执行与限制

用户要求禁止动态测试、应用/项目代码运行、项目脚本、npm测试/build、Docker/Compose运行、真实模型和浏览器。纯AST及TypeScript静态检查不在禁令内；T1—T6执行过的静态检查如上所列，T7没有重跑Python AST或TypeScript检查。T7未运行任何动态测试，也没有操作数据库、用户配置或容器。A1—A9全部保留运行验证未完成状态；静态源码位置不能证明功能在运行时通过。真实模型理解、通知送达、迁移回滚、线程取消收尾、Docker可启动和浏览器流程仍未验证。


## 动态修复与验收（2026-11-23，未提交）

用户后续要求修复真实使用问题并动态测试，覆盖首次实施的动态禁令；本轮不提交、不推送。代码基线仍为31a3e52，以下结果针对其上的当前工作区差异。

### 已修复的问题

- 上下文RUN绑定误传快照对象而非字典，导致用户输入、历史与助理摘要加载失败；普通构造及冻结重放现在检查助理必需输入，避免只剩身份说明仍正常回答。
- Schema去除title注释时误删properties.title业务字段；现在保留业务字段及默认映射数据。
- 审批/委派恢复丢失原始用户消息时间；现在读取持久USER_INPUT.metadata.created_at，不以当前时间或运行开始时间替代。上下文失败收尾使用CALLING_LLM阶段允许的失败事件。
- 批量prepare不发起审批，需同轮继续apply；补清工具说明及请求ID、版本、hash的实际返回路径，普通聊天仍不能代替批准。
- 请求ID/版本配对错误不可行动；字段说明和固定安全校验消息明确版本来源，不自动补版本、不放宽严格约束。草案工具说明补清嵌套必填数组和当前来源规则。
- 审批后模型仍沿用等待批准叙述；补充按最新回执区分成功、部分成功、失败及用户拒绝的通用规则。
- 双工作区输入框名称冲突；助理使用独立可访问名称，旧普通聊天测试按可见输入框查询。

### 自动化验证

测试前核对解释器为D:\dev\dotClaw\.venv\Scripts\python.exe。最终后端命令：

```powershell
.venv\Scripts\python.exe -m pytest tests/assistant tests/personal_tasks tests/tools tests/channel/web tests/runtime_v2/test_assistant_context_regression.py tests/runtime_v2/test_context_port.py tests/runtime_v2/test_context_plan_bootstrap.py tests/runtime_v2/test_phase2_application_host.py -q -ra --tb=short
```

结果：447 passed、3 skipped。三个跳过是Windows不适用的POSIX进程信号/进程组用例。覆盖真实临时SQLite的事务、提醒、草案、来源隔离、幂等与回滚、批量部分成功/重放、迁移、工具/Web，以及真实Runtime批准/拒绝恢复和上下文失败收尾。协议测试中的脚本模型不能证明自然语言能力。

前端npm test -- --reporter=dot：131个测试全部通过；npm run build成功。覆盖工作区隔离、原approval_id提交、预览禁批、失败/部分成功展示和迟到响应隔离。构建保留既有chunk大小及Markdown图片dialog警告，未作无关修改。git diff --check通过。

### 同配置真实模型验收

使用现有gpt-5.6-terra配置、完整15个工具，经真实Host、Runtime、工具执行器及隔离SQLite完成。数据仅位于.agent-work/pa3-assistant/isolated，不写正式事务库/会话，不启动通知调度。

| 场景 | 实际结果与证据 |
|---|---|
| 截图原问题 | 运行78465a94320d479e8a5cf9d34c78087e的模型输入事件含原始消息；返回针对周三初面/周六笔试的安排建议并询问可用时间，不再回复未收到需求。这只是一条成功样例。 |
| 创建与查询 | 自然语言创建两件事务并查询快照成功；另一轮为隔离事务添加每日09:00提醒并局部修改描述。 |
| 跨轮澄清 | 先保存awaiting_input草案且不创建正式事务；运行39a5fc2003434b53b705a4c5e2bdcdf0补齐时间后创建事务及2030-01-10 20:00 Asia/Shanghai单次提醒，同事务解决原草案，没有重复新建草案。 |
| 批量拒绝 | 运行fdefe5186f1346efbce4ef5e3940e798返回真实审批ID；结构化拒绝后两项目标仍pending，回答明确已拒绝、未执行。 |
| 批量批准 | 运行66a67034d60c4790977066c34e2a5a06重新发起审批，批准后两项均completed；回答明确批量操作已成功，不再沿用等待批准叙述。 |
| 数据库核对 | 只读验证5个隔离事务中4个完成、1个待办；原周期提醒取消，2030单次提醒active，原草案resolved。库内含两组批量验收对象；批量场景的初始目标由脚本种入，自然语言创建另有独立记录。 |

详细对话、运行事实、数据库核对及完整日志位于已忽略的.agent-work/pa3-assistant/，没有提交协作日志或验收数据。

### 更广回归与局限

扩大到整个runtime_v2/context的中间回归为845 passed、31 failed、3 skipped，不能称全套通过。按独立故障组抽取8个代表，在PA-03前87902de基线亦复现失败，涉及旧协议断言、旧恢复fixture和运行时恢复预期；没有逐个对照全部31项，也不能将其全部归因于本轮改动。本轮只修复真实助理链触发的恢复时间丢失和错误事件问题，没有为抹平旧失败而放宽断言。该扩大回归在最后几处修复前执行，不代表最终全仓失败数。

未执行浏览器端到端、真实通知送达、进程强杀/响应丢失注入、长期并发及真实旧生产库迁移演练，不能宣称通过。discriminator.mapping保留旧$defs字符串映射，但最终真实模型场景成功，没有拒绝证据，未推测性修改。完成结论限于本轮已验证核心链路，不承诺任意自然语言均能正确执行。