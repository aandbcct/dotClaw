# Web 请求接收确认与失败场景测试：P1 开发方案

日期：2026-10-28。前置：三个 P0 修复已经落地。本轮只补运行接收确认和测试缺口。

## 1. 交付目标与边界

用户发送消息后，在首个模型 token 之前收到真实运行 ID，页面区分“正在发送”和“已接收，正在处理”。确认表示运行已持久化，可以按 ID 查询；不表示模型供应商已接收或已开始生成。

- 仅新消息创建运行时发确认；审批、恢复使用已有运行 ID，不重发确认。
- 不增加 HTTP 接口、请求去重、自动重发、事件重放、逐字续流或新持久化状态。
- 不修改已完成 P0 的空输出判定、日志和错误码，不更改既有重试、取消、审批语义。
- 创建运行前的校验、排队或拒绝不能伪造确认；HTTP 200 也不等于运行已创建。
- 本轮不能覆盖“确认发出但客户端未收到”的不确定窗口；此时仍沿用 P0 的结果待确认处理，不能自动重发。

## 2. 数据模型变更

| 文件 | 类/模型 | 新增或修改 |
|---|---|---|
| `src/dotclaw/runtime/application/dto.py` | 新增 `RunAcceptedEvent`（运行接收确认数据） | frozen dataclass；字段仅 `session_id: str`、`run_id: str` |
| `src/dotclaw/runtime/application/ports.py` | `LLMOutputPort`（运行输出端口） | 新增 `async on_run_accepted(self, event: RunAcceptedEvent) -> None`；文档说明端口同时承载接收确认和模型增量，本轮不改接口名称 |
| `src/dotclaw/channel/web/schemas.py` | `SSEEventType`（网页流事件枚举） | 新增 `RUN_ACCEPTED = "run.accepted"` |
| `frontend/src/types.ts` | 新增 `RunAcceptedData`（接收确认载荷接口） | `session_id: string; run_id: string`；网络载荷仍需运行时校验，不能只做类型断言 |
| `frontend/src/useSessionWorkspace.ts` | `ConversationView`（单会话页面状态） | 新增 `submissionStage: "idle" \| "sending" \| "accepted" \| "streaming"`，默认 `"idle"` |

保持不变：`LLMOutputEvent`（文本增量数据）、`RunPhase`（运行阶段类型）、`WebRunStatus`（后端运行状态枚举）、会话/运行持久化结构。新确认是传输通知，不新增运行事实或数据库字段。

SSE 契约：

```text
event: run.accepted
data: {"session_id":"会话ID","run_id":"真实运行ID"}
```

成功创建的新运行事件顺序：`run.accepted → 零或多条 message.delta → 一个结束事件`。结束事件沿用当前流的终止白名单，包含挂起及错误；确认本身绝不是结束事件。

## 3. 后端类与方法改动

### 3.1 运行执行入口

文件：`src/dotclaw/runtime/application/engine.py`

修改 `RuntimeEngine`（运行执行引擎）的 `execute()`：

1. 保留现有策略解析、运行 ID 生成和 `create_run()`。
2. 保留取消令牌注册。
3. 在已有 `try/finally` 内、`_drive()` 前，若 `output_port` 非空，发送 `RunAcceptedEvent(session_id=request.session_id, run_id=run_id)`。
4. 继续现有执行和取消令牌释放流程。

发送确认不能因浏览器断连而中止运行。适配器应自行处理已断连情形；确认回调出现普通异常时，入口记录脱敏告警并继续执行，不能将已创建任务遗留为无人执行。不得吞掉任务取消异常。

只通知当前请求对应的新运行。不得为内部子运行复用父请求的确认订阅；沿实际调用路径检查输出端口传递，父请求 SSE 中只能确认自己的运行 ID。

### 3.2 SSE 适配器

文件：`src/dotclaw/channel/web/sse.py`

修改 `SSEOutputAdapter`（网页流输出适配器）：

- `__init__()` 新增 `_accepted_run_id: str | None = None`。
- 新增 `on_run_accepted(event)`：校验两个 ID 非空；已断连或已结束则直接返回；同运行重复确认忽略；不同运行 ID 抛明确的内部契约错误，不发第二个确认。
- 首次有效确认写入现有有界队列，事件类型为 `RUN_ACCEPTED`，随后记住运行 ID。
- `emit()` 继续只输出非空文本，不负责确认。
- `finish()` 继续维护唯一结束事件；`disconnect()` 保持只关闭订阅、不取消任务。

已有队列和断连处理必须用于确认事件，不能创建第二套推送任务或无限缓冲。

### 3.3 其他端口实现

文件：`src/dotclaw/channel/runtime_llm_output.py`

`ChannelLLMOutputAdapter`（命令行输出适配器）新增空操作 `on_run_accepted()`；CLI 原有“思考/回答”展示不变。

检索全部实现 `LLMOutputPort` 的生产对象和测试假对象，一并补齐方法。测试需要观察确认时保存到独立列表；其余空操作。不要用 `hasattr` 静默跳过未更新的内部实现。

### 3.4 明确不改的位置

- Web `submit_message()`、`_run_stream()` 不生成运行 ID、不主动发确认，只沿现有输出端口接收运行层通知。
- 模型驱动、代理、适配器不负责运行接收确认。
- 不把确认塞进空正文、思考增量、日志文本或现有终态载荷来代替新事件。

## 4. 前端方法改动

文件：`frontend/src/useSessionWorkspace.ts`、`frontend/src/types.ts`、`frontend/src/App.tsx`。

| 方法/位置 | 具体修改 |
|---|---|
| `emptyView()` | 初始化 `submissionStage="idle"` |
| `send()` | 完成可发送检查后清空旧 `runId`，设置 `"sending"`；保留 P0 的原问题保存和错误清理规则 |
| `runStream()` | 不得用通用初始化覆盖 send 设置的 sending；确认不影响 `receivedTerminal`；现有结束白名单不增加 run.accepted |
| `receive()` | 在未知事件过滤之前单独处理 run.accepted：校验载荷、会话 ID、非空运行 ID；保存 `runId`，设置 `"accepted"` 后立即返回 |
| `receive()` 增量分支 | 有效思考/正文增量到达后设 `"streaming"`；空增量不改变阶段 |
| 结束/明确拒绝分支 | `submissionStage="idle"`；保留 P0 的 phase、errorCode、retryable 等处理 |
| `readActivity()` | 不根据 activity 的 running 状态伪造本次接收确认；查询仍优先使用已知运行 ID |
| `control()` | 恢复/审批不进入“等待新运行确认”的 sending 阶段；沿现有状态展示，收到增量可设 streaming |
| 页面状态文案 | sending：“正在发送”；accepted：“已接收，正在处理”；streaming：沿现有生成文案 |

载荷不匹配当前会话、或同一请求突然变更运行 ID 时，保留已有正确 ID，显示协议异常/结果待确认；不得把错误 ID 写入其他会话。合法的重复确认不能把 streaming 降回 accepted。

文案优先级：错误/结果待确认/停止/审批/可恢复 > submissionStage。确认后断流保留已知运行 ID，按该 ID 查询；不能仍显示“正在发送”，也不能自动重新提交。

页面刷新后不从内存阶段推断之前收到过确认；按既有持久化状态展示。兼容旧服务未发确认但直接发增量：从 sending 进入 streaming，不强制要求先确认。

## 5. 删除与替换清单

- 替换“点击发送就统一显示正在生成”的文案逻辑；运行 phase 仍可为 running，用新字段区分传输展示阶段。
- 修改 `receive()` 的未知事件过滤，使其先处理 run.accepted，不能继续直接忽略该事件。
- 若测试断言“首事件必为 message.delta”，改成新运行首先确认，再检查原有增量顺序。
- 保留 P0 的结束事件白名单、空输出失败、异常分类、原问题恢复和状态查询逻辑。
- 不删除旧空答案、不迁移历史、不重写模型或运行状态机。

## 6. P1-2：按缺口补测试

已存在空输出、错误反馈和 SSE 跨层测试，优先扩展原文件，避免重复建设。

| 文件 | 新增/扩展测试 | 必须断言 |
|---|---|---|
| `tests/runtime_v2/test_runtime_engine.py` | 模型首包被事件阻塞 | 在释放模型前已收到确认；确认 ID 可读到已持久化运行 |
| 同上 | 创建失败、输出端口为空、确认通知异常 | 创建失败无确认；无端口正常执行；普通通知异常不遗留无人执行的运行 |
| `tests/channel/web/test_sse_contract.py` | 正常、空输出、确认后失败 | 确认在增量/结束之前；结束事件唯一；空输出仍失败 |
| 同上 | 重复/跨运行确认、结束后确认 | 同 ID 幂等；不同 ID 不污染流；结束后无新事件 |
| 同上 | 确认后断连 | 后台继续；确认的 ID 可用于查询原运行；不自动取消或重发 |
| 同上或现有 API 契约测试 | 参数错误、会话不存在、会话忙 | 不为被拒绝请求发送成功确认 |
| `tests/llm/test_openai_chat_completions_stream.py` | 首包超时、输出后超时/断流 | 保留流关闭、错误和既有重试边界断言；有覆盖则直接复用 |
| `tests/llm/test_empty_model_response.py` | 空流、仅用量、仅思考、纯空白、工具无正文 | 复用现有参数化测试；缺项才加；失败不成功投影，有效工具不误报 |
| `frontend/src/ErrorFeedback.test.tsx` | 确认到达但模型仍无输出 | sending → accepted，确认不计终态、不结束流 |
| 同上 | 确认后失败/断网/查询失败及恢复 | runId 保留；查询指定 ID；不重复发送；错误和结果待确认优先显示 |
| 同上 | 旧服务直接增量、重复/错误确认 | 兼容旧流；重复确认不退回等待态；错误 ID 不覆盖正确 ID |
| `frontend/src/SessionIsolation.test.tsx` | A/B 确认与增量交错 | ID、阶段、正文和错误互不串会话；A 完成不抢 B 焦点 |
| `tests/channel/test_llm_output_adapter.py` | CLI 接收确认 | 不打印多余内容，原增量显示不变 |

### 测试数据与执行约束

- 后端用可手动释放的异步事件控制首包和结束时机；前端用可控 Promise/流夹具，不靠固定 sleep 判断先后。
- 至少一项验证真实 HTTP 流可在模型释放前读到确认。若测试传输会缓存完整响应，使用隔离临时端口的本地服务加假模型；事后关闭服务。
- 断连后运行完成与停止须分别验证，不把断开浏览器等同于取消。
- 测试使用隔离目录，不删除用户会话、不连接真实模型、不修改正式配置。
- Windows 后端测试前先执行 `.venv\Scripts\python.exe -c "import sys; print(sys.executable)"`；pytest 全部用该解释器。前端执行 `npm test`、`npm run build`。

## 7. 交付与验收

实施顺序：新增数据/端口及全部实现 → 运行入口确认 → 前端状态和展示 → 补测试。

验收：模型尚未返回任何 token 时，页面已显示“已接收，正在处理”；对应运行 ID 可查询。后续失败、断连和双会话交错均不假成功、不串会话、不自动重发。CLI 不受影响。

交付列出修改文件、测试结果，并提供“发送中 → 已接收 → 输出或错误”的浏览器验证。仅更新测试不算完成接收确认；只断言完整响应中的事件顺序，也不足以证明首包前已推送。

## 8. 实施记录（2026-10-28）

### 8.1 完成情况与源码核对

P1-1、P1-2 已实现。开始实施时分支为 `feat/gui`，已有 P0 提交 `ab15290`；唯一未跟踪内容为本文档，未覆盖其他改动。本轮不提交或推送。

- `RunAcceptedEvent`（运行接收确认事件）只包含 `session_id`、`run_id`。`RuntimeEngine`（运行引擎）在运行持久化、取消令牌注册后通知输出端口，再驱动模型。普通通知异常仅记录脱敏诊断并继续；取消异常仍向外传播。
- `LLMOutputPort`（模型输出端口）的生产实现和测试假对象均补齐回调。`SSEOutputAdapter`（网页流输出适配器）复用原有有界队列，确认幂等且不属于终态；CLI 回调为空操作。
- 实际子运行经协调器提交时不携带父请求输出端口，现有链路无需修改；扩展真实子运行测试证明父订阅只有父运行的一次确认。审批、检查点恢复、委派恢复继续沿用已有 ID，不发送新确认。
- 前端保存 `submissionStage`，展示“正在发送 → 已接收，正在处理 → 生成中”。确认后立即提供运行 ID 及复制操作；失败和结果待确认优先显示。
- 确认后断流保留 ID，按指定 ID 查询；查询失败不冒充运行失败，不重新发送。错误会话/运行 ID、无效确认载荷保留已有正确 ID，并显示协议异常和结果待确认。合法重复确认不降低生成阶段。旧服务直接增量仍兼容。

与方案相比的最小补充：

1. `readActivity()` 在新请求尚未收到 ID、流仍打开时，忽略“最近活动运行”的投影，防止旧运行冒充当前确认；不会通过查询生成 accepted 阶段。
2. 协议异常后，即使查询仍返回 running，也保留结果待确认提示；串入其他运行的结束事件不得触发当前请求的历史刷新或结束判定。
3. 原有 ASGI 内存传输会缓存完整响应，不足以证明首包时序。因此在原 SSE 测试文件中增加临时回环 HTTP 服务，使用可阻塞假模型，结束后释放模型并关闭服务。未新增正式接口或配置。

### 8.2 修改文件

以下路径均相对仓库根目录：

| 范围 | 文件 |
|---|---|
| 运行数据、端口和通知入口 | `src/dotclaw/runtime/application/dto.py`、`ports.py`、`engine.py` |
| 网页事件、队列和 CLI | `src/dotclaw/channel/web/schemas.py`、`sse.py`、`src/dotclaw/channel/runtime_llm_output.py` |
| 前端阶段、ID 和展示 | `frontend/src/types.ts`、`useSessionWorkspace.ts`、`App.tsx`、`styles.css` |
| 前端交互回归 | `frontend/src/ErrorFeedback.test.tsx`、`SessionIsolation.test.tsx` |
| 引擎、恢复、子运行和端口替身 | `tests/runtime_v2/test_runtime_engine.py`、`test_recovery_boundary.py`、`test_delegation_port.py`、`test_llm_proxy_adapter.py` |
| CLI 和 HTTP/SSE 契约 | `tests/channel/test_llm_output_adapter.py`、`tests/channel/web/test_sse_contract.py` |
| 浏览器验收与交付 | `frontend/scripts/acknowledgment-acceptance.mjs`、本文档、`screenshots/2026-10-28-p1/` |

### 8.3 测试与证据

后端测试前已核对项目解释器为 `D:\dev\dotClaw\.venv\Scripts\python.exe`。执行方式：

```powershell
.venv\Scripts\python.exe -c "import sys; print(sys.executable)"
.venv\Scripts\python.exe -m pytest -q
cd frontend
npm test
npm run build
```

- 后端最终全量：1292 项通过，耗时 66.55 秒；覆盖新确认、通知异常、创建失败、入口拒绝、原有审批/取消/恢复和空输出规则。
- 前端：5 个测试文件、48 项通过。包括首增量前确认、重复与错误确认、确认后失败/断连/查询恢复、旧服务兼容，以及 A/B 会话 ID、状态和消息隔离。
- 构建通过；保留既有 JS 包超过 500 kB 的提示，本轮未扩大到拆包优化。
- 首包实证：真实 HTTP 客户端读取到 `run.accepted` 时，假模型释放事件仍未设置，运行已可从仓储及 activity 接口读取，后台尚未结束。随后分别验证正常输出、客户端断开后后台完成、断开后显式停止；不是收齐响应后才断言顺序。
- 首包超时、输出后超时、流关闭及重试边界直接复用 `tests/llm/test_openai_chat_completions_stream.py`；空流、仅用量、仅思考、空白和无正文工具调用复用 `tests/llm/test_empty_model_response.py`，未重复改写 P0。

### 8.4 浏览器验证

通过 `frontend/scripts/acknowledgment-acceptance.mjs` 验证 1440×900 浅色、深色及 390×844 手机页面；两条路径各验证发送中、已接收、生成中、失败或断连，共 24 张截图。全部业务请求由隔离夹具响应，不写真实会话、不调用外部模型；断言无页面脚本异常、无横向溢出、发送次数为 1、查询携带确认 ID。失败场景验证可恢复原问题；断连场景验证部分正文和待确认提示保留。

浏览器使用手动控制的可读流验证界面状态；真实 HTTP 传输时序由后端测试单独证明，二者不混同。

复现（本机 Playwright 位于已有工作区依赖中，可按环境调整）：

```powershell
cd frontend
$env:PLAYWRIGHT_PACKAGE='C:\Users\atozn\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules\playwright'
node scripts/acknowledgment-acceptance.mjs
```

需预先启动前端（默认 `http://127.0.0.1:5173`，可通过 `GUI_URL` 指定）。运行截图写入 `tmp/gui-p1-acknowledgment/`，本次交付副本及清单位于 [验收目录](screenshots/2026-10-28-p1/acceptance.json)。

代表截图：[发送中](screenshots/2026-10-28-p1/sending-disconnected-1440-light.png)、[已接收](screenshots/2026-10-28-p1/accepted-disconnected-1440-light.png)、[生成中](screenshots/2026-10-28-p1/streaming-disconnected-1440-light.png)、[确认后失败](screenshots/2026-10-28-p1/failed-failed-1440-light.png)、[深色确认](screenshots/2026-10-28-p1/accepted-disconnected-1440-dark.png)、[手机断连](screenshots/2026-10-28-p1/disconnected-disconnected-390-light.png)。

### 8.5 限制与边界

- 确认只证明 dotClaw 已持久化并接收运行，不证明供应商接收、模型成功或最终有回答。
- 浏览器在接收确认前断连仍可能不知道运行 ID；保留 P0 的结果待确认语义。未引入请求去重、自动重发、逐字续流或历史重生成。
- 本轮未调用真实供应商，不能据此判断真实上游曾经空输出的原因。
- 未调整既有 activity 轮询策略，未改变 P0 的空输出失败、重试、取消和审批语义。
