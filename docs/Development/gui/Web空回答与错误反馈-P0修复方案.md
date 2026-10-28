# Web 空回答、日志与错误反馈：三个 P0 修复方案

日期：2026-10-28。范围：仅修复空输出假成功、统一日志、前端异常处理。第 1–5 节为开发契约，实施结果见第 6 节。

## 1. 边界与统一行为

- 空流、仅用量、仅思考、纯空白正文且没有工具调用：判为失败。
- 没有正文但存在工具调用：有效，继续现有工具流程。
- 空输出不自动重试、不切换供应商、不成功投影到会话历史。
- 原有网络故障恢复、取消、审批及其他异常的重试规则保持不变。
- 不改历史压缩、模型选择策略、工具执行机制；不修复或删除旧空答案。
- 本轮不增加逐字续流、请求去重、自动重发、历史重生成或新 SSE 事件。
- 这三个修复不证明上游空输出的具体成因；补齐诊断后再用授权的真实请求确认。

## 2. P0-1：空输出不得成功

### 类、方法与字段

| 文件 | 类/方法 | 修改 |
|---|---|---|
| `src/dotclaw/llm/base.py` | 新增 `EmptyModelResponseError`（空模型输出异常） | 继承 `RuntimeError`，固定脱敏消息；不携带原始响应、密钥或请求正文 |
| `src/dotclaw/llm/drivers/openai_chat_completions.py` | `_StreamParseState`（单请求解析状态）`__init__()` | `finish_reason: str \| None = None`，不再默认 `"stop"` |
| 同上 | `OpenAIChatCompletionsClient`（Chat Completions 协议客户端）`chat()` | 删除首个可交付数据包之前遇到流结束就伪造成功结束包的分支；正常结束时只保留真实结束原因和已收到的用量 |
| `src/dotclaw/llm/proxy.py` | `LLMProxy`（模型调用代理）`chat()` | 每次尝试新增局部变量 `has_response_text=False`、`has_tool_calls=False`；首包和后续包统一统计；只有 RESPONSE 增量的非空白内容算正文 |
| 同上 | 同一方法的成功/异常分支 | 删除 `StopAsyncIteration → report_success → return`；空结果记一次 `report_failure()` 并抛空输出异常；普通完成也先检查有效输出，再记成功 |
| `src/dotclaw/runtime/application/ports.py` | 新增 `InvalidLLMResponseError`（无效模型结果异常） | 本轮仅表示无正文且无工具调用，不扩展为通用错误框架 |
| `src/dotclaw/runtime/adapters/llm_proxy_adapter.py` | `LLMProxyAdapter`（运行时模型适配器）`complete()` | 在通用异常捕获之前单独捕获空输出异常，转换为无效模型结果异常，使用 `raise ... from error` 保留原因链 |
| `src/dotclaw/runtime/domain/facts.py` | `RunErrorCode`（运行错误码枚举） | 新增 `EMPTY_MODEL_RESPONSE = "empty_model_response"` |
| `src/dotclaw/runtime/application/engine.py` | `RuntimeEngine`（运行执行引擎）`_invoke_llm_action()` | 单独捕获无效模型结果异常，调用现有 `_fail(..., code=EMPTY_MODEL_RESPONSE)`；正常返回后、写响应消息和成功迁移之前，再校验正文/工具是否均为空 |

### 必须注意的控制流

1. 驱动首包读取的 `StopAsyncIteration` 应被捕获并正常结束生成器，不能让其逃逸变成生成器运行错误。空结果由代理统一判定。
2. 代理抛出的空输出异常必须绕过通用 `except Exception` 重试分支和候选降级分支；保持一次失败计数，不能重复计入熔断统计。
3. 适配器不能把空输出包装成现有 `LLMUnavailableError`（模型暂不可用异常），否则进入可恢复挂起而非明确失败终态。
4. 运行引擎优先保留用户取消语义；空输出检查不能把已取消请求改写成失败。
5. 成功前的兜底条件为 `not response.content.strip() and not response.tool_calls`。检查使用 strip，保存正常正文时不裁剪原文。
6. 有正文但缺结束原因，或上游返回其他特殊结束原因，不在本轮重新定义协议语义；只记录真实信息供后续诊断。

### 持久化与 SSE

复用 `RunError`（运行错误摘要）的三个字段，不新增结构：

```json
{"code":"empty_model_response","message":"模型未返回有效回答，请查看执行记录或稍后重新发送。","retryable":false}
```

- 空输出进入现有失败终态和 `run_failed` 事实，不调用 `_finalize_action()`，不追加成功会话记录。
- Web `_result_event()` 沿用现有 `run.failed`，携带 `session_id/run_id/error`。
- `retryable=false` 表示本次运行不支持原检查点恢复；用户仍可将问题恢复到输入框，作为新请求发送。
- 既有 `ChatChunk`（标准化模型数据包）的 `finish_reason` 已支持 null，无须改字段结构。
- 旧空答案只在前端标记，不回写修正历史。

## 3. P0-2：统一初始化日志并补关键节点

### 初始化改动

| 文件 | 方法/对象 | 修改 |
|---|---|---|
| 新增 `src/dotclaw/common/logging_setup.py` | `configure_logging(level, log_file, project_root)` | 配置 `dotclaw` 命名空间的控制台和 UTF-8 文件输出；相对日志路径按项目根解析，创建父目录 |
| 同上 | `bind_log_context(session_id, run_id)` | 使用上下文变量，返回可复位的上下文；退出时恢复旧值，隔离并发请求 |
| 同上 | `LogContextFilter`（日志关联字段过滤器） | 为记录补 `session_id/run_id`，无关联时填 `-`；只附加到本模块管理的处理器 |
| `src/dotclaw/bootstrap/application_host.py` | `ApplicationHost`（应用生命周期宿主）`initialize()` | 在 `_build_llm()` 等基础设施初始化之前调用日志初始化 |
| `src/dotclaw/main.py` | 模块顶层 | 删除旧 `logging.basicConfig()`、硬编码 `./data/dotclaw.log` 和重复日志设置，将必要的第三方日志级别设置集中到新模块 |
| `src/dotclaw/config/settings.py` | `DebugConfig`（日志配置） | 直接复用 `level`、`log_file`，不新增配置字段 |

初始化必须幂等：给自有处理器加固定标记，仅更新/替换自有处理器；更换文件时关闭旧处理器。`dotclaw` 设置 `propagate=False`，避免重复输出。不使用 `basicConfig(force=True)`，不清空根日志或 Uvicorn 处理器。日志文件无法创建时明确报启动配置错误，不静默缺失文件日志。

### 记录位置与字段

| 位置 | 记录事件 | 字段 |
|---|---|---|
| Web `submit_message()` | `web.message.received` | `session_id`、`content_length` |
| 模型适配器 `complete()` | 绑定日志上下文 | `session_id`、`run_id`；在 finally 复位 |
| 代理 `chat()`，真正调用客户端前 | `llm.call.started` | `provider`、`model`、`attempt`，自动带上下文 |
| 同一调用结束/异常 | `llm.call.finished` / `llm.call.failed` | `elapsed_ms`、`finish_reason`、`response_chars`、`tool_call_count`、`error_type`；HTTP 状态和上游请求 ID 仅在实际取得时记录 |
| Web `_run_stream().produce()` | `web.run.result` | `session_id`、`run_id`、`outcome`、`error_code`；结构化失败也要记，不能只记录抛出的异常 |
| 同上异常捕获 | `web.run.exception` | 会话标识及脱敏异常诊断，补齐当前仅打印异常类型的不足 |

成功为 INFO，空输出/失败为 WARNING 或 ERROR。计时使用单调时钟；缺失字段为 null，不用 0 假装测量值。用量未返回不能据此认定没有调用或没有计费。

### 日志边界

- 不开启 HTTP 请求/响应全文 DEBUG；不记录消息正文、Authorization、API Key、工具参数全文。
- 异常日志保留类型、调用位置、原因链；错误文本可能包含 URL 参数或响应正文，必须脱敏/截断，不能无条件 `logger.exception()` 输出原始第三方异常。
- 不新增日志平台、数据库、全局请求追踪系统或日志查询 API。
- Web 接收时尚无运行 ID，日志用会话 ID 关联；本轮不声称实现精确的请求去重或接收确认协议。

## 4. P0-3：前端错误、原问题与处理入口

### 数据模型

| 文件/对象 | 增改字段 | 用途 |
|---|---|---|
| `frontend/src/useSessionWorkspace.ts` 的 `ConversationView`（单会话视图状态） | 新增 `errorCode: string`、`retryable: boolean`、`lastSubmittedText: string` | 保留结构化错误和原提问；默认 `""/false/""` |
| 同上 | 复用 `error/runId/detached/phase` | 不创建第二套运行状态机 |
| `frontend/src/types.ts` 的 `ErrorEnvelope`（错误响应接口） | `error.retryable?: boolean` | 兼容现有 HTTP 错误不带该字段 |
| `frontend/src/api.ts` 的 `ApiError`（HTTP 请求异常） | 新增 `retryable: boolean = false` | 保留后端明确返回的可恢复标识；不从 HTTP 状态自行猜测可重发 |

本轮不新增持久化草稿模型或失败消息数据库。新字段是当前页面内存状态；刷新后按已有历史/运行证据展示，不能承诺恢复所有失败请求正文。

### 方法修改

**`useSessionWorkspace.ts`：**

- `send()`：提交前保存 `lastSubmittedText`；只有确实开始发送才清除旧错误字段。
- `receive()`：解析 `error.code/message/retryable`；终态只接受 `run.completed/run.failed/run.cancelled/run.suspended/stream.error`，未知事件不当失败或完成。
- `runStream()`：删除 `event.event !== "message.delta"` 即终态的判断；仅用上述白名单。异常分类如下表。
- `readActivity()`：已知运行 ID 时必须查该 ID，不能用最近/其他运行的状态覆盖本次结果；刷新状态不清除已确认错误。
- 新增对外方法 `checkRunStatus(id)`：复用现有 activity 查询；查询失败展示“状态查询失败”；只更新查询结果，不触发发送。
- 现有后台轮询失败时保留正文和已有业务错误；没有业务错误时可显示状态查询失败，查询恢复后只清除查询错误，不能把模型错误一起清掉。

| 情况 | 页面状态和行为 |
|---|---|
| 明确 HTTP 4xx 拒绝 | 结束发送态，展示后端错误；409 再核对已有活动运行 |
| `run.failed` 且不可恢复 | `phase=failed`，保留 runId 和错误；不自动重发 |
| `run.failed` 且可恢复 | 保存错误和 retryable，查询指定运行；仅确认存在可恢复运行后显示现有恢复入口 |
| 网络中断、HTTP 5xx、流无终态结束 | `detached=true`，标记结果待确认，查询状态；不因为 activity 返回 null 就认定请求一定没执行 |
| 查询到 running / 等待审批 | 保持对应状态，提供已有停止/审批操作 |
| 查询到 completed | 读取完成历史；空正文显示异常提示，不展示空白成功气泡 |

注意：当前不带 run_id 的 activity 只返回活动运行，已完成时也可能是 null。没有运行 ID且结果无法对应时，保持“结果待确认”，不得自动放开一键重试。

**`api.ts`：**

- `responseError()` 保留错误码、消息和可恢复标识；非 JSON 错误保留 HTTP 状态，不伪装成正常业务响应。
- `streamRequest()` 在开始读取前校验响应为 `text/event-stream`；HTML/JSON 误响应给出协议错误，不等待到流结束才解释。
- 不新增无限重试、自动重连或固定超时后自动重发。

**`App.tsx`：**

- `submit()` 将 `void workspace.send(...)` 改为 `await` 并捕获异常；发送函数内部应正常处理预期错误，外层兜底意外异常。
- 当前 `submitting` 是全局 ref：调整为会话级提交保护（已有 streams 可复用），创建会话仍用独立 creating 标记，避免 await 后阻塞其他会话发送。
- 错误区展示可理解的消息和可复制的运行 ID，不展示堆栈。
- 提供“查看执行记录”“恢复原问题到输入框”；已有未发送草稿不得静默覆盖，可在末尾追加或提示用户选择。
- 结果未知提供“查询运行状态”；可恢复运行使用现有 `resumeRun()`；不增加“自动重试”按钮。
- 历史空答案显示“该次回答为空”，仅展示，不篡改后端完成状态或记录。

## 5. 验收与实施顺序

顺序：空输出判定及测试 → 日志初始化与节点 → 前端错误处理 → 跨层验证。

| 验收场景 | 必须结果 |
|---|---|
| 空流、仅用量、仅思考、纯空白 | `empty_model_response`；失败计数一次；不降级、不重发、不成功投影 |
| 有工具调用且无正文 | 正常执行工具；不误报空回答 |
| 正常正文 | 流式/非流式正常；保留原始空白和代码缩进 |
| 取消与空输出同时发生 | 保持取消语义 |
| 非空输出后断流 | 保留已有中断规则，不重复发送已输出内容 |
| 空结果跨层 | 模型异常 → 运行失败 → SSE 错误 → 前端错误提示和原问题恢复 |
| CLI/Web 启动、重复初始化 | 两种入口写相同配置日志；不重复输出；不泄漏敏感值 |
| 两个并发会话 | 日志和页面错误不串会话；一个会话发送不锁死另一个 |
| HTTP 拒绝、网络中断、查询失败 | 三种提示不同；状态未知不盲目重发 |
| 旧空答案 | 明确提示，无历史数据修改 |

优先扩展现有测试：`tests/llm/test_openai_chat_completions_stream.py`、`tests/llm/test_model_router_contract.py`、`tests/runtime_v2/test_llm_proxy_adapter.py`、运行引擎契约测试、`tests/channel/web/test_sse_contract.py`、`frontend/src/SessionIsolation.test.tsx`、`App.test.tsx`。新增日志初始化测试和必要的代理空响应测试。旧测试若要求“空流成功”，须改为新业务契约，不为保留旧断言保留缺陷。

Windows 测试前先执行 `.venv\Scripts\python.exe -c "import sys; print(sys.executable)"`；后端使用同一解释器运行 pytest，前端执行 `npm test`、`npm run build`。测试使用假模型与隔离目录，不调用真实模型、不删除真实会话。

完成标准：三类 P0 均通过对应测试并有浏览器错误场景验证；单元测试通过不能写成上游 API 故障已解决。提交交付摘要时分别列出“已修复的错误处理”和“尚未复现确认的上游原因”。

## 6. 实施与验收记录（2026-10-28）

三个 P0 已实施，未自动提交或推送。开始时分支为 `feat/gui`，工作区只有本文尚未跟踪；未修改或删除用户真实会话。

### 完成结果

| 项目 | 实现 | 验收证据 |
|---|---|---|
| P0-1 | 空输出专用异常跨代理、适配器、引擎传递，失败错误码 `empty_model_response`，一次失败统计，不重试/降级/成功投影；取消优先 | 空流、用量、思考、空白、首包正文/工具、非流式工具、原因链、持久化、取消竞态、代理→引擎→SSE 测试 |
| P0-2 | 共用日志初始化在基础设施构建之前调用；上下文变量关联；调用与 Web 关键节点记录；保留类型、位置、原因链，不记录异常原文 | 幂等、换文件关闭旧处理器、日志路径失败、根/Uvicorn 处理器保留、两个宿主入口、并发上下文、真实取得的 HTTP 状态/请求 ID、敏感文本不入日志测试 |
| P0-3 | 明确失败与结果待确认分开，查询指定运行，保留原问题，草稿追加恢复，运行 ID 复制及详情入口；会话级提交保护；历史空答案仅提示 | HTTP 403/409/422、502、网络异常、非 SSE、未知事件、查询失败/恢复、错运行 ID、恢复状态确认、A/B 并发与旧空答案测试及浏览器验收 |

### 最小必要调整

- 驱动原有非流式分支还会丢失工具调用、伪造 `stop`；已同时修复。解析状态增加可选 `usage`，未收到用量时不制造零用量；代理和适配器不再要求结束原因与用量必须同包存在。
- activity 原响应没有错误摘要，无法在查询指定失败运行后恢复错误码。`RunActivityResponse`（运行活动响应）及前端运行活动接口增加可选 `error`，复用 `RunError`（运行错误摘要），不新增错误结构或 SSE 事件。
- `ConversationView`（单会话视图状态）另加 `statusError` 保存查询错误；业务错误仍留在 `error/errorCode/retryable`。查询恢复只清除查询错误，避免抹掉已确认模型失败。
- 共用日志初始化会让原有组件启动告警进入日志文件；`_host_components.py` 的异常告警也改为脱敏诊断，避免第三方异常正文泄漏。既有重试、网络恢复和组件降级策略未修改。
- 原有部分测试替身缺少日志配置、SSE 响应头或有效调用输出，已补齐测试契约，没有在生产代码绕过初始化或继续接受空成功。

### 修改文件

- 空输出：`llm/base.py`、`llm/drivers/openai_chat_completions.py`、`llm/proxy.py`、`runtime/application/ports.py`、`runtime/application/engine.py`、`runtime/domain/facts.py`、`runtime/adapters/llm_proxy_adapter.py`。
- 日志与 Web：新增 `common/logging_setup.py`；修改 `bootstrap/application_host.py`、`bootstrap/_host_components.py`、`main.py`、`channel/web/app.py`、`channel/web/schemas.py`。上述路径以 `src/dotclaw/` 为根。
- 前端：`frontend/src/useSessionWorkspace.ts`、`App.tsx`、`api.ts`、`types.ts`、`RunActivityPanel.tsx`、`styles.css`。
- 测试：新增 `tests/llm/test_empty_model_response.py`、`tests/test_logging_setup.py`、`frontend/src/ErrorFeedback.test.tsx`；扩展 SSE、适配器、宿主和会话隔离测试。
- 浏览器：新增 `frontend/scripts/error-acceptance.mjs`；本节和截图记录同步更新。

### 验证结果与复现

- 使用项目解释器核对后运行 `.venv\Scripts\python.exe -m pytest -q`：**1280 passed**，70.70 秒。
- 在 `frontend` 执行 `npm test`：**40 passed，5 个测试文件**；`npm run build` 通过，仍有原有大包体积提示。
- 浏览器：21 张隔离夹具截图，覆盖桌面浅/深色、手机错误提示，以及失败详情、原问题追加恢复、运行 ID 复制、明确拒绝、连接未知、5xx、协议误响应、未知事件、查询失败恢复、旧空答案、审批/恢复、部分正文回填、并发会话；没有调用真实模型或修改真实会话。
- 浏览器完整输出位于 `tmp/gui-p0-errors/`（忽略目录）；关键截图与[验收记录](screenshots/2026-10-28-p0/acceptance.json)保存到文档目录。

PowerShell 浏览器复现（先启动本机 Vite，进入 `frontend`）：

```powershell
$env:PLAYWRIGHT_PACKAGE='C:\Users\atozn\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules\playwright'
node scripts/error-acceptance.mjs
```

`PLAYWRIGHT_PACKAGE` 按本机安装位置调整，可用 `CHROME_PATH`、`GUI_URL` 覆盖浏览器和页面地址。脚本拦截全部业务接口；浏览器验证不是对真实上游的请求测试。

截图：[明确空输出失败](screenshots/2026-10-28-p0/empty-1440-light.png)、[深色失败详情](screenshots/2026-10-28-p0/details-1440-dark.png)、[手机结果待确认](screenshots/2026-10-28-p0/network-390-light.png)、[手机明确拒绝](screenshots/2026-10-28-p0/rejected-390-light.png)。

### 未完成项和结论边界

**错误处理已修复：** 假模型及隔离浏览器证据表明空输出不再假成功，调用诊断和错误反馈链路可用。

**上游空输出原因未查明：** 本轮未调用真实外部模型，不能确认供应商返回、网关、请求参数或协议等哪一项导致了此前空输出；缺结束原因/用量也不能证明没有调用或计费。需要另行授权真实请求后结合新增诊断确认。

CLI/Web 共用初始化通过受控宿主测试，不表示已对真实模型/MCP 完整启动联调。真机软键盘、真实输入法及其他浏览器未验收。失败原问题只在当前页面内存保留，刷新后不承诺恢复所有失败请求；没有运行 ID 且查询不到对应运行时保留待确认，不提供盲目重试。旧空答案只提示，不修复历史。
