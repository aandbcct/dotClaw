# dotclaw AgentRuntime/Loop 与工业级 Runtime 对比审查

> 日期: 2026-07-01 | 对比对象: OpenClaw Agent Runtime, agentica Runner

---

## 1. 三者的抽象层定位

| 概念 | dotclaw | agentica | OpenClaw |
|---|---|---|---|
| **"Runtime" 含义** | 纯能力引用集合 (LLM/Tool/Session/Memory 等服务引用) | 执行引擎 (Runner 驱动 ReAct 循环) | 完整执行引擎 (~150 文件, ~1200 行入口函数) |
| **Identity/配置** | AgentIdentity (frozen dataclass, 白名单/模板/模型) | Agent 自身持有 model/tools/instructions | 动态组装的 system prompt + 9 层策略过滤 |
| **执行循环** | AgentLoop (~450 行, 基础 ReAct) | Runner._run() (~2500 行, 完整引擎) | runEmbeddedPiAgent (~1200 行入口 + 大量子模块) |
| **状态快照** | ❌ 无 | RunContext (run_id/status/task_anchor) | Session 文件锁 + 压缩快照 |
| **Run 标识** | AgentRun (结果记录, 非执行标识) | RunContext (执行生命周期标识) | 内建在 Session/Conversation 中 |

**核心结论**: dotclaw 的 "AgentRuntime" 命名有误导性——它是 **服务定位器 (Service Locator)**，不是执行引擎。真正的执行逻辑在 AgentLoop 里。agentica 和 OpenClaw 的 "Runtime/Runner" 才是执行引擎。

---

## 2. dotclaw AgentLoop 与 agentica Runner 逐项对比

### 2.1 LLM 调用与容错

| 能力 | dotclaw AgentLoop | agentica Runner | OpenClaw |
|---|---|---|---|
| **重试机制** | LLMProxy 有基础重试，Loop 层无感知 | Model 层 retry + `_call_with_retry` 在 Runner 内编排 | 动态重试次数 = min(160, max(32, 24+profiles×8)) |
| **错误分类恢复** | ❌ catch-all Exception | CallSetupError / NonRetryableStreamError 分类 | 6 类错误码驱动恢复 (402/429/401/403/408/404) |
| **模型降级 (Fallback)** | ❌ | ✅ `_recover_with_fallback` + fallback_models 链 | ✅ 6 层认证 Fallback + Profile 轮转 |
| **流式中断处理** | ❌ | NonRetryableStreamError 直接抛 | 流式状态机 + 三重去重 |
| **熔断 (Circuit Breaker)** | LLM 层有 circuit_breaker.py | ❌ (依赖 provider) | ✅ 标记 Profile 不可用 |

**差距**: dotclaw 的容错集中在 LLMProxy/ModelRouter 层，Loop 层完全无感知。agentica 把容错逻辑提升到 Runner 层做编排（分类 → 降级 → 恢复），更可控。

### 2.2 工具执行

| 能力 | dotclaw | agentica | OpenClaw |
|---|---|---|---|
| **并发工具执行** | `asyncio.gather(*[...])` 无限制 | 通过 Model.max_concurrent_tools 控制 | 每 Agent 并行上限 4 |
| **工具策略过滤** | ❌ | ❌ (无此层) | ✅ 9 层策略过滤 (Global→Provider→Agent→Group→Sandbox→Sub-agent) |
| **循环检测** | ❌ | ❌ | ✅ 10/20/30 次重复警告 |
| **工具结果预算** | ❌ | ✅ `enforce_tool_result_budget` | ✅ 压缩诊断日志 |
| **工具审批** | ApprovalManager (独立模块) | ❌ (无此层) | ✅ Owner-Only 过滤 |
| **危险命令拦截** | ✅ approval_commands 配置 | ❌ | ✅ Schema 层硬编码拒绝 |

**差距**: dotclaw 的 `asyncio.gather` 无并发上限——如果一个 LLM 调用返回 50 个 tool_calls，会同时发起 50 个子进程。agentica 有 max_concurrent_tools 限制，OpenClaw 有明确的并发上限。这是 **生产稳定性隐患**。

### 2.3 循环控制

| 能力 | dotclaw | agentica | OpenClaw |
|---|---|---|---|
| **最大迭代** | max_loop_steps (参数传入) | LoopState.max_turns | 内建 |
| **Token 预算** | ❌ | ✅ LoopState 追踪 + COST_BUDGET 断点 | ✅ |
| **Max-tokens 恢复** | ❌ | ✅ 检测 finish_reason=="length" → 注入 "Continue" | ❓ |
| **stop_after_tool_call** | ❌ | ✅ 消息级标记 | ❓ |
| **Steering 注入** | ❌ | ✅ `_inject_steering` (折叠进最新 tool result) | ❓ |
| **Fallback-on-break** | ❌ | ✅ `_recover_with_fallback` 循环中断后做一次无工具推理 | ❓ |

**差距**: dotclaw 的循环控制只有一个 `max_loop_steps`。agentica 的 LoopState + 多断点机制（max_turns/cost_budget/stop_after_tool_call）让循环终止有多个安全阀。

### 2.4 上下文管理

| 能力 | dotclaw | agentica | OpenClaw |
|---|---|---|---|
| **Token 裁剪** | 简单 trim (按 token 数截断历史) | micro_compact (摘要压缩) | LLM 驱动的历史摘要压缩 |
| **压缩策略** | 无 (直接丢弃旧消息) | 微压缩保留关键信息 | 完整压缩管线 (检测→判断→LLM 摘要→替换→诊断) |
| **压缩去重** | ❌ | ❌ | ✅ "是否最近已压缩过" 检测 |
| **压缩超时** | ❌ | ❌ | ✅ 300s 硬超时 |
| **System prompt 大小控制** | ❌ | ❌ | ✅ 动态组装, 按需注入 |

**差距**: dotclaw 的上下文管理最弱——直接用 token 数截断丢弃旧消息，没有压缩/摘要机制。长对话会丢失关键上下文。

### 2.5 可观测性

| 能力 | dotclaw | agentica | OpenClaw |
|---|---|---|---|
| **执行追踪** | Journal (文件级 trace) | RunEventRecord + Langfuse | 压缩诊断 + Token 追踪 |
| **事件系统** | Journal.sink 模式 | `_emit_event()` + callback | 内建 |
| **Langfuse 集成** | ❌ | ✅ `langfuse_trace_context` | ❓ |
| **Run 生命周期** | AgentRun (结果记录) | RunContext (完整生命周期) | Session/Conversation 内建 |
| **Guardrails** | ❌ | ✅ input/output guardrails | ✅ 多层策略 |

**差距**: dotclaw 的 Journal 是文件级 trace，适合调试但不适合生产监控。agentica 的 RunEventRecord + Langfuse 是标准的生产可观测方案。

### 2.6 多 Agent / 并发

| 能力 | dotclaw | agentica | OpenClaw |
|---|---|---|---|
| **Sub-agent spawn** | 设计文档中 (Phase 13-14) | ✅ SubagentRegistry + spawn_batch | ✅ sessions_spawn |
| **嵌套深度限制** | 设计文档中 (MAX_DEPTH=2) | 设计文档中 | ✅ 可配置 (默认 1) |
| **并发控制 (Semaphore)** | 设计文档中 | ✅ asyncio.Semaphore(MAX_CONCURRENT=3) | ✅ Lane 队列隔离 |
| **Session 并发锁** | ❌ | ❌ | ✅ 分布式文件锁 (PID+时间戳+看门狗) |
| **子 Agent 工具黑名单** | 设计文档中 | ✅ BLOCKED_TOOLS | ✅ 深度感知策略 |

**差距**: dotclaw 的 multi-agent 还在设计阶段。agentica 已实现 SubagentRegistry + spawn_batch。OpenClaw 最成熟——有 Lane 隔离防止死锁，有分布式文件锁保证 Session 并发安全。dotclaw 的 Session 目前无并发保护。

---

## 3. dotclaw 缺失的关键能力（按优先级排序）

### 🔴 P0 — 生产稳定性

1. **工具并发上限**: `asyncio.gather` 无限制 → 加 Semaphore
2. **循环安全阀**: 只有 max_loop_steps → 加 cost_budget + 循环检测
3. **Session 并发锁**: 无 → 多 Agent 同时写同一 Session 会损坏数据

### 🟠 P1 — 长程可靠性

4. **上下文压缩**: 简单截断 → 需要 LLM 驱动的摘要压缩
5. **Max-tokens 恢复**: finish_reason="length" 时注入 "Continue"
6. **错误分类恢复**: catch-all Exception → 分类处理 (可恢复 vs 致命)
7. **RunContext / TaskAnchor**: 缺 → 压缩/检索防漂移所需

### 🟡 P2 — 可观测性

8. **事件发射系统**: Journal 文件级 → RunEvent + callback
9. **Langfuse/ tracing 集成**: 缺
10. **Guardrails**: 缺 input/output guardrails

### 🟢 P3 — 高级能力

11. **Steering 注入**: 中途人工干预
12. **Fallback-on-break**: 循环中断后的优雅降级
13. **流式进度透传**: 子 Agent 进度回传父 Agent

---

## 4. 设计建议

### 4.1 AgentRuntime 改名或重新定位

当前 `AgentRuntime` 是 **ServiceLocator** 而非 Runtime。建议:
- **不改名**: 保持当前语义（纯能力引用），但文档明确标注"这不是执行引擎，是服务定位器"
- **或者**: 重命名为 `AgentServices` / `AgentContext`，把 "Runtime" 留给真正的执行引擎

### 4.2 AgentLoop 需要升级为完整的执行引擎

当前 AgentLoop (~450 行) 是一个 **最小可行 ReAct 循环**。对比 agentica Runner (~2500 行)，差距主要在:
- 容错编排 (重试/降级/恢复)
- 循环控制 (多断点安全阀)
- 上下文管理 (压缩/摘要)
- 事件系统

建议分阶段补强:
- **Phase 1**: 加 Semaphore 并发控制 + 循环检测
- **Phase 2**: 引入 LoopState (参考 agentica) + max_tokens 恢复
- **Phase 3**: RunContext + TaskAnchor + 事件系统
- **Phase 4**: 上下文压缩 + LLM 降级

### 4.3 新增 RunContext (执行标识层)

agentica 的 RunContext 是 SDK-first 设计，dotclaw 也需要这一层:
```
RunContext {
    run_id, session_id, parent_run_id, agent_id,
    source (sdk/cli/cron/subagent/workflow),
    status (created→running→completed/failed/cancelled),
    started_at, ended_at, duration_seconds,
    task_anchor: { goal, source_query, constraints, confirmed_facts },
    error, trace_id, metadata
}
```

这与现有的 AgentRun (结果记录) 不冲突——RunContext 管生命周期，AgentRun 管持久化结果。

### 4.4 Session 并发锁

OpenClaw 的分布式文件锁方案 (`.jsonl.lock` + PID + 时间戳 + 看门狗) 是轻量且有效的方案，适合 dotclaw 的文件存储架构。

---

## 5. 总评

| 维度 | dotclaw | agentica | OpenClaw |
|---|---|---|---|
| 架构清晰度 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ |
| 生产容错 | ⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| 上下文管理 | ⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ |
| 多 Agent | ⭐⭐ (设计中) | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| 可观测性 | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ |
| 安全性 | ⭐⭐⭐ | ⭐⭐ | ⭐⭐⭐⭐⭐ |
| 代码量/TPS | ~4K 行 (含设计) | ~15K+ 行 | ~150 文件 |

**结论**: dotclaw 的架构基础（Identity/Runtime 拆分、Session/Conversation/AgentRun 三层、Journal 追踪）设计质量很高——可能是三者中最干净的。但在 **生产容错、上下文管理、多 Agent 并发** 三个维度，与 agentica 和 OpenClaw 有明显差距。这不是设计问题，而是 **实现成熟度** 问题。

agentica 是 dotclaw 最合适的对标对象——两者都是 Python async 框架，都是 SDK-first 设计。OpenClaw 的 TypeScript 实现有很多工程实践值得借鉴（特别是工具策略管线、并发锁、沙箱隔离），但不需要照搬。
