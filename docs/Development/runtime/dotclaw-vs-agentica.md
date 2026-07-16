# agentica 与 dotClaw 架构对比

> 状态：设计文档 | 日期：2026-07-06
>
> 对比对象：agentica（`D:\dev\agentica`）Runner + Agent 执行模型 vs dotClaw Runtime + AgentState 状态机模型。
> 对比维度：控制循环 / 工具执行 / 安全防护 / 上下文管理 / 多 Agent 协作 / 模型层 / 持久化恢复，共 7 个维度。

---

## 一、架构差异概述

| 维度 | agentica | dotClaw |
|------|----------|---------|
| **执行模型** | Runner 在 `_run_impl()` 内 while True 循环，一次 run = 多轮 LLM+Tool | AgentState 事件驱动 + TransitionTable 路由，一次 run = 多次 handle_event 调度 |
| **工具调用** | 同一 run 内同步执行，`continue` 下一轮 | TOOL_WAIT 结束当前 AgentRun → push TOOL_RESULT → 新 AgentRun 接管（更彻底的原子化） |
| **状态管理** | LoopState（turn_count, death_spiral 检测）+ Agent 直写变量 | AgentState dataclass（phase/iteration/tasks），事件驱动的声明式状态转换 |
| **可观测性** | event_callback 回调 + RunEventRecord | Journal 模块（trace.jsonl + state.json + report.json 双路径持久化） |

---

## 二、控制循环节点

| 节点 | agentica | dotClaw | 结论 |
|------|----------|---------|------|
| 事件驱动 while 循环 | `_run_impl()` while True | TurnLoop.run_forever() + asyncio.Queue | 架构等价 |
| Turn 计数跟踪 | LoopState.turn_count + max_turns | AgentState.iteration + max_iterations | 等价 |
| 工具触发循环继续 | 同一 run 内 `continue` | TOOL_WAIT 结束 AgentRun → 状态快照持久化 → 新 AgentRun 恢复 | **dotClaw 更彻底**：每次 LLM 推理 = 一个原子 AgentRun |
| 流式输出路径 | `_run_impl(stream=True)` 完整路径 | `_invoke_llm()` 取最终结果 | agentica 流式更完整 |

---

## 三、工具执行节点

| 节点 | agentica | dotClaw | 状态 |
|------|----------|---------|------|
| 解析 tool_calls | provider-specific `model.parse_tool_calls()` | 直接从 LLMResponse.tool_calls 取 | agentica 多 provider 兼容更好 |
| 并发工具执行 | async `_execute_tool_calls()` 逐个 yield 事件 | asyncio.gather 一次性并发 | dotClaw 无中间事件 |
| **工具结果累积错误跟踪** | `consecutive_all_error_turns` 计数器 → death spiral 检测 | 无 | ❌ 缺 |
| **stop_after_tool_call 信号** | Message.stop_after_tool_call 字段 | ToolsDoneEvent.stop_signal | agentica 更细粒度（per-message 级） |
| **工具结果智能截断** | CompressionManager 多级截断（保留 150 chars 首部摘要） | 无 | ❌ 缺 |
| 工具调用日志 | `[tool-calls]` + args 摘要 | Journal.tool_start 记录 | 等价 |

---

## 四、安全/防护节点 —— **最大差距区**

| 节点 | agentica | dotClaw | 状态 |
|------|----------|---------|------|
| **Death Spiral 检测** | `_check_death_spiral()` — 连续 N 次全 Error 轮次，检测系统性工具失败 | 仅检测同一工具+同一参数的死循环（MD5 hash） | ❌ 语义不同，dotClaw 漏检"不同工具但全部报错" |
| **Cost Budget 控制** | CostTracker + `_check_cost_budget()` 按 USD 预算截停 | 无 | ❌ 缺 |
| **HTTP 级重试/回退分类** | `RETRYABLE_SUBSTRINGS` vs `FALLBACK_ONLY_SUBSTRINGS` vs `CONTENT_FILTER_HINTS` | 无 | ❌ 缺 |
| **Input Guardrails** | `InputGuardrail` 装饰器 + tripwire 阻断机制 | 无 | ❌ 缺 |
| **Output Guardrails** | `OutputGuardrail` 装饰器 + tripwire 阻断机制 | 无 | ❌ 缺 |
| **ToolInput/Output Guardrails** | `ToolInputGuardrail` + `ToolOutputGuardrail` | 无 | ❌ 缺 |
| **User Cancellation** | `agent.cancel()` 跨线程取消 + `AgentCancelledError` | TurnLoop.stop() 有方法但状态机不感知 | ❌ 缺状态机集成 |
| **Timeout 控制（三维）** | `first_token_timeout` / `idle_timeout` / `run_timeout` | 无 | ❌ 缺 |
| **MAX_TURNS 限制** | LoopState.max_turns（子 Agent 默认 100） | AgentState.max_iterations（默认 10） | 等价 |

---

## 五、上下文管理节点 —— **第二大差距区**

| 节点 | agentica | dotClaw | 状态 |
|------|----------|---------|------|
| **上下文溢出检测** | `CompressionManager.should_compress()` token 预算检查 | `msg_trim()` 简单按 token 从头部裁剪 | ⚠️ dotClaw 仅做基础裁剪 |
| **LLM 自动摘要** | `CompressionManager.compress()` → 用 LLM 额外调用总结历史轮次 | 无 | ❌ 缺 |
| **工具结果智能裁剪** | `_truncate_oldest_tool_results()` → 保留前 150 chars + 文件路径 | 无 | ❌ 缺 |
| **旧轮次丢弃** | 基于规则丢掉最老 tool result 轮次 | `msg_trim()` 从头部裁剪 | agentica 语义保留更好 |
| **Max-Tokens Recovery** | `finish_reason="length"` → 自动注入 "Continue" + 裁剪历史 → 重试（最多 3 次） | TRUNCATED phase 直接 → DONE（v1 简单行为） | ❌ dotClaw 不做恢复 |
| **Mid-Run Steering** | `_inject_steering()` 运行中向消息列表注入指导信息 | 无 | ❌ 缺 |
| **Pre-Tool Hook** | 工具执行前的钩子（返回 True 可 skip 本轮工具调用） | 无 | ❌ 缺 |

---

## 六、多 Agent 协作节点

| 节点 | agentica | dotClaw | 状态 |
|------|----------|---------|------|
| **Handoff** | default_handoff_mapper + handoff 工具 | `Runtime._handle_handoff()` → 子 Runtime.run() 递归 | 等价 |
| **SubAgent 类型化** | EXPLORE / RESEARCH / CODE / CUSTOM 四类 + 详细 SubagentConfig | 无类型化约束 | ❌ 缺 |
| **SubAgent 深度限制** | MAX_DEPTH=2 防止无限嵌套 | 无 | ❌ 缺 |
| **SubAgent 超时** | per-subagent timeout（默认 300s，可配置） | 无 | ❌ 缺 |
| **SubAgent 工具隔离** | `BLOCKED_TOOLS` 集合 + `_select_child_tools()` + `Tool.clone()` 状态隔离 | Runtime.derive() 隔离 channel 但不隔离工具 | ⚠️ 缺工具级隔离 |
| **Swarm 并行协作** | coordinator → workers（并行）→ synthesizer（聚合） | 无 | ❌ 缺 |
| **Agent as Tool** | `agent.as_tool()` 将 Agent 包装为可调用工具 | 无 | ❌ 缺 |
| **并行批处理** | `spawn_batch()` with asyncio.Semaphore 控制并发 | 无 | ❌ 缺 |

---

## 七、模型层节点

| 节点 | agentica | dotClaw | 状态 |
|------|----------|---------|------|
| **Fallback Model Chain** | primary → [fallback_1, fallback_2, ...] 链式降级 | 无 | ❌ 缺 |
| **Break Recovery Model** | `fallback_on_break` → loop break 后最后一次无工具推理（给用户友好回复） | 无 | ❌ 缺 |
| **Content Filter 处理** | `finish_reason="content_filter"` → 自动切换到下一个 fallback model | 无 | ❌ 缺 |
| **Prompt Too Long 处理** | `PROMPT_TOO_LONG_HINTS` 检测 → 自动裁剪消息列表 | 无 | ❌ 缺 |

---

## 八、持久化/恢复

| 节点 | agentica | dotClaw | 状态 |
|------|----------|---------|------|
| Checkpoint 系统 | `CheckpointManager` → 文件级快照/恢复（generated files） | StateStore → AgentState 快照（状态级） | 互补：agentica 文件级，dotClaw 状态级 |
| Resume | 无自动化（需手写 resume 逻辑） | ResumeManager.get_resume_context() 完整实现 | dotClaw 有但未接入 TurnLoop 主流程 |

---

## 九、dotClaw 相比 agentica 的独特优势

| 能力 | dotClaw | agentica |
|------|---------|----------|
| AgentRun 原子化 | 每次 LLM 推理 = 一个独立持久化的 AgentRun（TOOL_WAIT 机制） | 同一 run 内多轮，无中间持久化 |
| 事件驱动触发源 | USER_INPUT / TOOL_RESULT / RESUME / TIMER / APPROVAL_DONE 五种完整的 TriggerType | 无独立的触发源概念 |
| 双路径持久化 | StateStore（状态快照）+ Journal（trace.jsonl 事件流） | 仅有 event_callback，无结构化 trace |
| AgentState.restore() | 内置从快照恢复完整状态机 | 无内置恢复 |
| 声明式状态转换 | TransitionTable 驱动 | 硬编码 while True 循环 |

---

## 十、补充建议优先级

按重要性和与"状态机做任务流转中枢"的契合度排序：

### P0 — 安全保障（必须补，否则生产不可用）

| 序号 | 节点 | 实现方式 | 预估代码量 |
|------|------|---------|-----------|
| 1 | **Death Spiral 检测** | AgentState 加 `consecutive_error_turns` 计数器，新 guard `_guard_death_spiral`（连续 5 次全 Error 轮次即触发） | ~30 行 |
| 2 | **User Cancellation 集成** | AgentState 加 `is_cancelled` 字段，`_check_guards()` 增加取消检测，TurnLoop.stop() 设置该标志 | ~25 行 |
| 3 | **Cost Budget 控制** | AgentState 加 `max_total_tokens`，新 guard `_guard_cost_budget`（参考 Journal.token_accum） | ~20 行 |

### P1 — 健壮性（显著提升长链路可用性）

| 序号 | 节点 | 实现方式 | 预估代码量 |
|------|------|---------|-----------|
| 4 | **Max-Tokens Recovery** | TRUNCATED → 自动注入 "Continue" + 裁剪历史 → INVOKE_LLM（参考 agentica，最多 3 次重试） | ~50 行 |
| 5 | **工具结果智能裁剪** | 在 `_execute_current_tools()` 后对超过阈值的 tool result 保留前 150 chars 摘要 | ~20 行 |
| 6 | **上下文溢出 LLM 摘要** | 当 `_build_context_msgs()` 检测到 token 预算不足时，额外调用一次 LLM 对历史轮次做摘要 | ~60 行 |

### P2 — 多 Agent 增强（扩展协作能力）

| 序号 | 节点 | 实现方式 | 预估代码量 |
|------|------|---------|-----------|
| 7 | **SubAgent 工具隔离** | 在 `orchestration/registry.py` 中为每个 AgentIdentity 增加 `blocked_tools` / `allowed_tools` 配置 | ~40 行 |
| 8 | **SubAgent 类型化** | 定义 SubagentType 枚举 + SubagentConfig dataclass（参照 agentica 的 EXPLORE/RESEARCH/CODE） | ~60 行 |

### P3 — 锦上添花

| 序号 | 节点 | 实现方式 |
|------|------|---------|
| 9 | Input/Output Guardrails | AgentState 入口/出口加 guard decorator，tripwire 触发后直接 FAILED |
| 10 | Model Fallback Chain | LLMProxy 加 fallback_models 列表，按序降级 |
| 11 | Mid-Run Steering | TurnLoop 加 `inject_steering(text)` 方法，在下一次 LLM 调用时作为 system 消息注入 |
| 12 | Agent as Tool 包装 | Agent 实现 `__call__` 或 `as_tool()`，将子 Agent 包装为工具定义 |
