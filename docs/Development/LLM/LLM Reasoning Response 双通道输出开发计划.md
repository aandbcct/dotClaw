# LLM Reasoning / Response 双通道输出开发计划

> 前提：总体设计已确认；同一功能分支完成完整目标，按阶段验证，最终一次性切换内部 Python 协议。

## 1. 实施基线与迁移原则

当前代码基线：`ChatChunk(content, tool_call, is_final)`、`TextStreamPort.emit(run_id, chunk)`、`ChannelTextStreamAdapter` 与 Client 实例级 `_pending_tool_calls` / `_stream_finish_reason`。

本任务没有数据迁移或持久化格式迁移：reasoning 不写入任何持久化容器。YAML 缺少 `reasoning` 时加载为 `mode=none`；内部 Python API 直接替换，不提供旧别名或双签名。

每阶段完成后运行相关测试；最终删除旧路径前，必须先确保调用方与测试全部迁移。

## 2. 阶段一：冻结公共 LLM 数据契约

### 目标

建立能表达一个标准化流数据包的公共类型，避免把单一 `kind` 错放到 `ChatChunk` 上。

### 修改

- 修改 `src/dotclaw/llm/base.py`：新增 `TextDeltaKind`、不可变 `ChatTextDelta` 与新 `ChatChunk`。
- `ChatChunk` 使用 `text_deltas`、`tool_calls`、`finish_reason`、`usage`；删除 `content`、`tool_call`、`is_final`、离散 token 字段。
- 明确 `LLMUsage` 的现有路由用途枚举与新的 token usage 值对象不得混名；新增 token 用量 DTO 时使用清晰名称，例如 `TokenUsage`。

### 测试与门槛

- 新增 `tests/llm` 契约测试：空包、单文本 delta、同包 reasoning→response、多个工具调用、finish/usage-only 包。
- 更新所有 Fake/Mock，使每处 Chunk 构造显式使用新字段。
- `rg -n "\.content|\.tool_call|\.is_final" src tests` 的结果仅允许非 `ChatChunk` 语义引用；确认后再删除旧字段。

## 3. 阶段二：加入模型级 reasoning 配置与解析器

### 目标

让模型明确选择 `none/native/tags`，并实现请求级、跨 Chunk 的标签解析。

### 新增与修改

- 在 `src/dotclaw/config/settings.py` 新增 `ModelReasoningConfig`，为 `ModelConfig.reasoning` 提供 `default_factory`。
- 扩展 `load_router_config()`：解析 mode 与可选标签；非法 mode、空标签或相同起止标签直接配置加载失败。
- 新增 `src/dotclaw/llm/reasoning.py`：`ReasoningMode`、`ReasoningPolicy`、`ReasoningStreamParser`。
- 修改 `model_router_config.yaml`：仅给 `qwen3.7-max` 配置 `reasoning.mode: native`；其他模型不写或显式 `none`。

### 标签状态机规则

- `tags` 模式下，`<think>` 中文本为 reasoning，`<response>` 中文本为 response，标签外文本为 response。
- 标签可跨 raw chunk；标签自身不产生 delta。
- 未匹配结束标签原样作为 response；reasoning 区内嵌套开始标签原样作为 reasoning。
- `flush()` 输出已确认正文，不输出协议标签；未闭合 `<think>` 的正文仍为 reasoning。

### 测试与门槛

- 覆盖 `none/native/tags` 配置、默认兼容、完整与跨 chunk 标签、同包多段、未闭合、异常结束标签和嵌套开始标签。
- 解析器测试只依赖 LLM 公共 DTO，不模拟 Provider SDK。
- 配置加载测试证明旧 YAML 的行为仍为 `none`。

## 4. 阶段三：标准化 Provider 流并消除共享请求状态

### 目标

以调用局部状态替换 `OpenAICompatibleClient` 的实例级流状态，并生成新的 `ChatChunk`。

### 修改

- 在 `src/dotclaw/llm/openai_compat.py` 新增仅服务单次调用的 `StreamParseState`，包含 pending tool calls、finish reason 与 token usage。
- 删除 `_reset_stream_state()`、`_pending_tool_calls`、`_stream_finish_reason`。
- 每次 `chat()` 创建 `StreamParseState`；仅在 `ReasoningMode.TAGS` 创建 `ReasoningStreamParser`。
- `native` 模式提取 `delta.reasoning_content`；`tags` 模式只解析 `delta.content`；`none` 模式将 `content` 直接归为 response。模式不自动猜测或回退。
- 多个完成的工具调用一次写入 `ChatChunk.tool_calls`；finish 与 usage 作为平行字段输出。
- 在 Provider/Proxy 边界追踪 `visible_output_started`：只有实际产生 reasoning/response delta 后，流异常才不得重试或降级；此前失败允许既有重试与候选切换。

### 测试与门槛

- 原生 `reasoning_content`、普通 content、同包双字段、usage-only、finish-only、多个工具调用。
- 两个交错 `chat()`：工具参数、finish reason、reasoning parser 互不串线；一个调用异常不污染另一个。
- 测试“无可见输出失败可降级”与“已展示 reasoning/response 后失败不可降级”。
- 不得通过给共享 Client 加锁或每次新建 Client 规避并发问题。

## 5. 阶段四：替换 Runtime 输出契约

### 目标

将 LLM 内部的数据包映射为 Runtime 的单类型展示事件，并只将 response 聚合为最终消息。

### 修改

- 在 `runtime/application/dto.py` 增加 `LLMOutputKind`、`LLMOutputEvent`；`RunResult.has_streamed_text` 改为 `has_streamed_response`。
- 在 `runtime/application/execution.py` 改为 `has_streamed_response` 与 `mark_response_streamed()`；`RunExecutionView` 增加 `session_id`，由 `RunExecution.request.session_id` 填充。
- 在 `runtime/application/ports.py` 用 `LLMOutputPort.emit(event)` 替换 `TextStreamPort`，并将 `LLMPort.complete()` 参数改为 `output_port`。
- 逐层迁移 Engine、Coordinator、SessionInteractionService、审批恢复与中断重试的参数名和类型。
- 改造 `runtime/adapters/llm_proxy_adapter.py`：顺序映射每个 `ChatTextDelta`；reasoning 只 emit，response emit 并聚合；工具、finish、usage 保持既有 Runtime 语义。
- Adapter 的 metadata 改为 `has_streamed_response`；Engine 仅据此标记执行态，避免 reasoning 造成最终回答去重。

### 测试与门槛

- `CollectingLLMOutputPort` 断言 event 含 `session_id/run_id/kind/content`。
- reasoning 不进入 `RunMessage`、Conversation 或下一轮 Context；response 正确聚合与持久化。
- reasoning-only 且最终 response 未流式发送时，CLI 仍会打印最终回答。
- 审批恢复、工具调用后的第二次 LLM 调用与中断重试仍能传递同一个输出端口。

## 6. 阶段五：升级 CLI 展示

### 目标

让入口层按语义展示，而不把 reasoning 专有方法加入通用 `Channel`。

### 修改

- 用 `src/dotclaw/channel/runtime_llm_output.py` 中的 `ChannelLLMOutputAdapter` 替换 `runtime_text_stream.py` 的 `ChannelTextStreamAdapter`。
- Adapter 实现 `LLMOutputPort`，按 `run_id` 保存上次展示 kind；切换到 reasoning/response 时打印一次“思考”/“回答”标题，连续同类不重复。
- 修改 `src/dotclaw/main.py`：每次交互构建 `output_port`，向普通提交、审批恢复和重试透传；`_render_result()` 改查 `has_streamed_response`。
- 模型文本按安全的纯文本路径输出，不作为 Rich markup 解释。

### 测试与门槛

- response-only、reasoning-only、reasoning→response、response→reasoning、连续同类、空文本、多次 LLM 调用。
- CLI 仅在 response 已流式展示时抑制最终消息；reasoning-only 不抑制。
- 并发 Session 使用独立输出收集器时不串流。

## 7. 阶段六：统一切换、删除与文档收敛

### 删除清单（已在前序阶段完成删除，阶段六只核验删除结果）

> 原计划把删除集中在阶段六，是为了保证依赖顺序与可验证性，并不要求把"已迁移、零调用方"的旧
> 代码硬留到阶段六。实际执行采用 Ralph Loops 后压原则——迁移全部调用方 → 验证 → 立即删除旧路径——
> 因此各旧路径在其对应替代阶段便已物理删除，阶段六自然收缩为：文档收敛、src 与 tests 全量零引用
> 搜索、相关测试与最终验收。这属于"执行时机提前"，不是设计范围漂移。

- `TextStreamPort`、`ChannelTextStreamAdapter`、`runtime_text_stream.py`：已于阶段四/五替代并物理删除。
- `ChatChunk.content` / `ChatChunk.tool_call` / `ChatChunk.is_final` 及 Client 实例流状态
  （`_pending_tool_calls` / `_stream_finish_reason` / `_reset_stream_state`）：已于阶段一/三替代并物理删除。
- `has_streamed_text`、`mark_text_streamed()` 及相关 metadata/测试断言：已于阶段四替代并物理删除。
- 约束（始终坚持，无任何兼容垫片）：不得创建 `TextStreamPort = LLMOutputPort`、旧 `emit()` 重载或旧字段兼容属性。

### 搜索验证

```powershell
rg -n "TextStreamPort|ChannelTextStreamAdapter|has_streamed_text|mark_text_streamed" src tests
rg -n "_pending_tool_calls|_stream_finish_reason|_reset_stream_state" src tests
rg -n "ChatChunk\([^\n]*(content|tool_call|is_final)" src tests
```

三个搜索均应无有效旧实现或旧调用；如仅保留迁移说明，必须明确不在运行代码或测试中。

### 最终验证

```powershell
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe -m pytest -q tests\llm tests\runtime_v2
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
```

如完整测试集包含已知历史迁移失败，应单独报告当前架构相关测试结果，不恢复已删除接口来迎合历史测试。

## 8. 推荐提交顺序

1. `refactor(llm): introduce structured chat chunk deltas`
2. `feat(llm): add model reasoning policy and tag parser`
3. `fix(llm): isolate provider stream state per request`
4. `refactor(runtime): replace text stream port with llm output events`
5. `feat(cli): render reasoning and response deltas separately`
6. `docs(llm): document reasoning streaming boundaries`

## 9. 最终验收清单

- [ ] `qwen3.7-max` 的 `reasoning_content` 能实时显示在“思考”区。
- [ ] response 实时显示在“回答”区，并且只它进入最终消息、Conversation 与 Context。
- [ ] `mode=none` 的旧模型行为保持普通文本输出。
- [ ] 标签跨 chunk、未闭合和异常标签不吞正文、不显示协议标签。
- [ ] 两个并发相同模型调用不串工具参数、finish reason 或 reasoning 状态。
- [ ] 无可见输出前允许降级；已有可见输出后不拼接其他模型的输出。
- [ ] 工具调用、审批恢复、第二次 LLM 调用、中断重试和最终消息去重均通过回归测试。
- [ ] 旧接口与旧状态字段已物理删除，搜索验证为零。
