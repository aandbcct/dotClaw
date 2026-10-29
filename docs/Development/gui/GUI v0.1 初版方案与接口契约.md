# dotClaw GUI v0.1 初版方案与接口契约

> 状态：后端 FastAPI/SSE 闭环与 React/Vite 前端已完成，待联调与最终审计。
> 本文是 GUI v0.1 讨论期唯一方案档案；实现与验收以本文为准。

## 1. 唯一目标

本 PR 交付一个仅监听 `127.0.0.1` 的本地浏览器 GUI，使用户能够查看、新建和切换 Session（会话），发送消息，实时查看思考/回答增量，显式停止当前运行，并在刷新后读取已完成的持久化历史；现有 CLI（命令行界面）行为保持不变。

完成后的主链：

```text
React/Vite 页面
→ FastAPI HTTP/SSE 适配层
→ ApplicationHost（应用生命周期与依赖装配入口）
→ SessionInteractionService（会话交互服务）
→ SessionRunCoordinator（会话运行协调器）
→ RuntimeEngine（运行时执行引擎）
```

## 2. 当前代码事实

- `ApplicationHost（应用生命周期与依赖装配入口）` 已是唯一组合根，负责装配和关闭运行资源。
- `SessionInteractionService（会话交互服务）` 已支持创建会话、提交消息、切换模型、审批恢复、取消运行和删除会话。
- `SessionManager（会话持久化管理器）` 已支持会话的创建、加载、列表和删除；`Session.conversations` 是刷新页面后读取已完成对话历史的现有投影。
- `LLMOutputEvent（模型增量输出事件）` 已携带 `session_id`、`run_id`、增量类别和文本，可直接适配为 SSE。
- Runtime 审计事件写入 `events.jsonl`，当前没有面向 GUI 的实时订阅出口；v0.1 不扩展该事件系统。
- 任务 3 已新增独立 `gui` 可选依赖与 `dotclaw.channel.web` 后端；任务 4 已新增 React/Vite 前端、POST SSE 客户端与构建后静态托管。

因此本 PR 不再抽取新的通用应用服务层，只增加 Web 适配边界和 GUI 所必需的最小会话取消能力。

## 3. 范围

### 3.1 包含

1. 会话列表、创建、详情与已完成历史查询。
2. 单次消息提交的 HTTP POST + SSE 流式响应。
3. reasoning/response 两类模型文本增量及规范化终态事件。
4. 按 `session_id` 显式取消当前活动 Run（一次运行）。
5. React/Vite 双栏页面：会话列表、对话区、发送和停止。
6. 后端契约测试、SSE 行为测试、前端关键交互测试和 CLI 回归。

### 3.2 明确不包含

- Tool Call（工具调用）实时卡片、审批按钮、完整 Runtime Debug Panel（运行时调试面板）。
- `events.jsonl` 实时广播、断线续传和多订阅者事件总线。
- WebSocket（双向长连接）、Tauri（桌面应用壳）、安装包和 `.exe`。
- 登录、多用户、远程访问、云同步和跨进程 Web Worker（后台执行进程）。
- Runtime 状态机、持久化格式及现有 CLI 命令重构。

## 4. 任务列表

1. **方案与契约（已完成）**：冻结本文的范围、接口、事件、并发和验收语义。
2. **后端契约测试（已完成）**：先写会话、历史、SSE、取消和错误映射测试。
3. **后端实现（已完成）**：实现 FastAPI 适配层和 SSE 消息闭环，使任务 2 的测试通过。
4. **前端实现（已完成）**：搭建 React/Vite 页面并接入接口。
5. **联调与审计**：验证浏览器闭环、断连语义、CLI 回归并同步使用文档。

## 5. 模块与文件计划

新增后端：

```text
src/dotclaw/channel/
├── cli/               # CLI 行为、Rich 样式与横幅
│   ├── __init__.py
│   ├── terminal.py
│   └── banner.py
└── web/               # HTTP/SSE 入站交互适配
    ├── __init__.py
    ├── app.py          # create_app()、lifespan、路由装配和静态资源入口
    ├── schemas.py      # HTTP 请求/响应及 SSE 数据模型
    └── sse.py          # SSE 编码与运行级输出适配

tests/channel/
├── cli/
│   └── test_channel.py
└── web/
    ├── test_api_contract.py
    └── test_sse_contract.py
```

新增前端（任务 4）：

```text
frontend/
├── package.json
├── vite.config.ts
├── index.html
└── src/
    ├── App.tsx       # 会话导航、对话流、输入与停止交互
    ├── api.ts       # HTTP 客户端与 POST SSE 增量解析
    ├── types.ts     # 前端边界类型
    └── styles.css   # 桌面及移动端布局与状态反馈
```

修改现有代码：

- `pyproject.toml`：增加 GUI 可选依赖和本地 Web 启动命令；核心安装不被强制引入前端依赖。
- `bootstrap/session_interaction.py`：增加按 Session 取消当前活动 Run 的窄方法，不改变现有 `cancel(run_id, reason)`。
- `README.md`：任务 5 才补充启动与范围说明。

不新增 `ChatService`、通用 Event Bus（事件总线）或新的 Session/Run 持久化实体。

## 6. 必要类型与职责

- `CreateSessionRequest（创建会话请求模型）`：可选 `title`、可选 `agent_id`；只用于 HTTP 输入校验。
- `SubmitMessageRequest（提交消息请求模型）`：必填非空 `content`；只用于 HTTP 输入校验。
- `SessionSummaryResponse（会话摘要响应模型）`：暴露 `id/title/agent_id/model/created_at/updated_at`。
- `SessionDetailResponse（会话详情响应模型）`：包含会话摘要和按持久化顺序展开的 user/assistant 历史消息。
- `SSEEvent（SSE 传输事件模型）`：内存传输对象，仅包含 `event` 与 JSON `data`，不是 Runtime 审计事实，不落盘。
- `SSEOutputAdapter（SSE 输出适配器）`：实现现有 `LLMOutputPort（模型流式输出端口协议）`，把 `LLMOutputEvent（模型增量输出事件）` 放入单次请求队列；不读取仓储、不改变 Runtime 状态。

## 7. HTTP 接口契约

所有接口使用 `/api/v1` 前缀；生产形态由同一 FastAPI 进程托管前端静态文件，开发形态由 Vite 代理 `/api`，不开放宽泛 CORS（跨域资源共享）。

### 7.1 `GET /api/v1/sessions`

- 输出：`200`，按 `updated_at` 倒序的 `SessionSummaryResponse（会话摘要响应模型）` 数组。
- 空数据：返回 `[]`。
- 状态：只读，不创建目录或修改会话。

### 7.2 `POST /api/v1/sessions`

- 输入：`CreateSessionRequest（创建会话请求模型）`。
- 输出：`201`，新建会话摘要。
- 失败：非法 `agent_id` 返回 `422`；无法确定默认 Identity（身份声明）返回 `409`。
- 状态：调用现有会话交互入口创建并持久化 Session。

### 7.3 `GET /api/v1/sessions/{session_id}`

- 输出：`200`，`SessionDetailResponse（会话详情响应模型）`。
- 历史来源：只读取现有 `Session.conversations`；每条 Conversation（一次完整问答记录）展开为一条 user 消息和一条 assistant 消息。
- 消息标识：user 使用 `conversation_id`，assistant 使用 `{conversation_id}:assistant`，与现有 Runtime 历史快照保持一致。
- 失败：不存在或不可读取返回 `404`。
- 边界：只保证已成功提交到 Session 投影的完整问答；不拼接未完成 Run 的临时消息。

### 7.4 `POST /api/v1/sessions/{session_id}/messages`

- 输入：`SubmitMessageRequest（提交消息请求模型）`。
- 输出：`200 text/event-stream; charset=utf-8`。
- 前置失败：空消息返回 `422`；Session 不存在返回 `404`。
- 流内失败：响应开始后的 Session 占用、模型、工具、持久化和内部错误必须用终态 SSE 事件表达，不能以 HTTP `200` 冒充成功。
- 客户端：使用 `fetch()` 读取响应流；原生 `EventSource` 不支持本接口所需的 POST 请求。

### 7.5 `POST /api/v1/sessions/{session_id}/cancel`

- 输入：可选 `reason`，缺省为“用户从 GUI 停止运行”。
- 输出：`202`，`{"session_id": "...", "run_id": "...", "status": "cancelling"}`。
- 失败：Session 不存在返回 `404`；没有活动 Run 返回 `409`。
- 实现：`SessionInteractionService（会话交互服务）` 增加 `cancel_session(session_id, reason) -> str | None`，从现有运行仓储读取该 Session 的活动 Run，再调用现有 `cancel(run_id, reason)`。
- 不变量：同一 Session 按现有协调器契约最多应有一个活动 Run；若读到多个，视为持久化一致性错误，不猜测取消对象。

通用 JSON 错误格式：

```json
{"error": {"code": "session_not_found", "message": "Session 不存在"}}
```

错误响应不返回堆栈、文件绝对路径、模型密钥或工具参数。

## 8. SSE 事件契约

每个事件使用标准 SSE `event:` + 单行 JSON `data:`，事件间以空行分隔。单次请求内保持产生顺序；不承诺跨 Run 全局顺序，也不复用 `RunEvent.sequence`。

### 8.1 增量事件

```text
event: message.delta
data: {"session_id":"s1","run_id":"r1","kind":"response_delta","content":"你好"}
```

`kind` 直接复用现有 `reasoning_delta | response_delta`，空文本不发送。

### 8.2 终态事件

每条流必须且只能以以下一个事件结束：

- `run.completed`：运行成功，携带规范化 final message 和 `has_streamed_response`。
- `run.failed`：运行失败或 Session 忙，携带 `error.code/message/retryable`。
- `run.cancelled`：显式取消完成。
- `run.suspended`：运行等待审批或 delegation（委派）；v0.1 只展示状态，不提供恢复 UI。
- `stream.error`：Web 适配层自身发生未映射异常，返回脱敏错误。

终态 `data` 至少包含 `session_id`、`run_id`、`status`；`run.completed` 中的完整回答即使此前已流式发送也只作为终态校验数据，前端不得重复追加。

## 9. 并发、断连与一致性

- 同一 Session 的并发提交继续由 `SessionRunCoordinator（会话运行协调器）` 串行化；存在未终态 Run 时，第二次提交以 `run.failed/session_busy` 结束。
- 不同 Session 继续允许并行执行；Web 层不增加全局锁。
- SSE 队列只属于一次 HTTP 请求，禁止跨 Session 或跨 Run 复用。
- 浏览器断开只关闭该 SSE 订阅，不自动取消 Run；显式停止必须调用取消接口。
- 订阅断开后，`SSEOutputAdapter（SSE 输出适配器）` 停止缓存后续增量，后台 Run 继续按现有持久化与终态规则完成，避免无人消费的有界队列阻塞 Runtime。
- v0.1 不支持 SSE 重连或补发。刷新后只恢复已经写入 `Session.conversations` 的完整成功问答；未完成运行仍遵循现有恢复/占用规则。

## 10. 测试计划

任务 2 先写失败的契约测试，任务 3 再实现：

1. 会话列表为空、倒序返回、创建成功、未知 Identity、详情不存在和历史展开。
2. SSE 正确输出 reasoning/response 增量，保持顺序并忽略空增量。
3. 成功、失败、取消、挂起和适配层异常各自只产生一个终态事件。
4. HTTP `200` 但流内 `run.failed` 不被测试误判为成功。
5. 同 Session 忙、不同 Session 并行、显式取消和无活动 Run。
6. 客户端断连不取消 Run，且不因无人消费队列阻塞运行完成。
7. 响应不泄露堆栈、密钥、绝对路径或完整工具参数。
8. 回归 `tests/runtime_v2`、`tests/channel` 和 CLI 入口测试。

前端关键交互测试覆盖持久化历史加载、POST SSE 增量与成功终态去重，以及运行期间通过取消接口显式停止。

后端契约测试使用注入的 Fake（测试替身）Host/Service，不调用真实模型或网络；另设最小集成测试验证真实 `ApplicationHost（应用生命周期与依赖装配入口）` 的装配关系。

## 11. 验收标准

1. 仅绑定 `127.0.0.1` 后，可在浏览器完成会话列表、创建、切换、发送、流式查看和停止。
2. 刷新后可读取此前成功完成的问答历史。
3. 每次消息流只有一个终态事件，且失败不能因 HTTP `200` 被误判为成功。
4. 断开页面不会隐式取消或阻塞后台 Run。
5. GUI 与 CLI 共用现有 Application/Runtime 主链，没有复制业务逻辑或新增第二份持久化事实。
6. 不包含第 3.2 节列出的延后能力。
7. 后端、前端关键交互及指定回归测试全部通过。

## 12. 已确认决策

浏览器断开不自动取消 Run，停止运行必须显式调用取消接口。
