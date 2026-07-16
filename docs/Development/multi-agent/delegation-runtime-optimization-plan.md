# Multi-Agent Delegation 闭环优化开发计划

> 版本: v0.2
> 状态: Draft
> 日期: 2026-07-12
> 前置文档: [Delegation Dispatcher 开发计划](./delegation-dispatcher-plan.md)

## 1. 目的与范围

本计划只处理当前 delegation 实现中与既定计划不一致、或已经具备接口但尚未闭环的部分。目标是使本地异步 delegation 可以正确运行、取消、等待、追踪和嵌套委托。

本计划不重构为完整的分布式 Agent runtime，也不实现远程协议、持久化恢复、重试模型或 handoff 整改。这些内容仅作为后续演进记录，不阻塞本轮开发。

本轮完成后的可用边界：

- 支持本地 `spawn_agent + wait_agent + kill_agent + list_agents`。
- 父 Agent 与子 Agent 可以并发运行，且 trace / run 归属不串扰。
- 子 Agent 可以继续发起 delegation，归属到当前子 Agent。
- wait 超时不终止任务；取消后可以稳定得到结构化取消结果。
- 当前进程内可查询已完成但尚未消费的任务结果。

## 2. 当前未闭环项

| 编号 | 问题 | 影响 | 本轮处理目标 |
|---|---|---|---|
| C1 | derived Runtime 共享可变 Journal | 父子并发运行时 trace、session、AgentRun 可能串扰或丢失 | 每个 AgentRun 使用独立 Journal run scope |
| C2 | delegation 工具闭包绑定顶层 Agent | 子 Agent 的 nested delegation 归属错误 | 工具从当前执行上下文取得 Agent 和 run ID |
| C3 | `parent_run_id` 未从工具调用传入 | Task 与父 AgentRun 无法关联 | 在工具执行上下文中传递当前 `agentrun_id` |
| C4 | `wait_agent` timeout 会取消本地子任务 | “停止等待”错误变成“终止任务” | timeout 只结束本次 wait，不改变 Task |
| C5 | 已取消任务会传播 `CancelledError` | 父 AgentRun 可能被取消，无法消费结果 | 转换为结构化 cancelled TaskResult |
| C6 | `kill_agent` 直接写最终取消状态 | 无法区分取消请求与 coroutine 实际终止 | 增加 cancelling / cancelled 两阶段语义 |
| C7 | `AgentMessaging.cancel()` 仍可旁路取消 | 调用方可只改 Task 状态而不终止执行资源 | 移除或废弃该公开取消入口 |
| C8 | DelegationEvent 仅保存在 Dispatcher 内存列表 | 现有 trace 看不到 delegation 生命周期 | 先映射到当前 Journal / trace |
| C9 | 终态任务和 Handle 的责任未收口 | active tracking、历史查询和资源释放职责混杂 | Handle 终态后注销；Task 留在内存账本供查询 |
| C10 | 缺少真实 LocalRunner 端到端测试 | fake runner 不能验证并发和取消问题 | 补本地 runner、嵌套、timeout、取消测试 |

## 3. 修改后的模块形态

### 3.1 运行上下文

`ToolExecutionContext` 补充当前执行所必需的最小字段：

- 当前 `Agent`
- 当前 `Runtime`
- `session_id`
- 当前 `agentrun_id`
- 已有 timeout 与 channel 信息

Runtime 在执行每个工具调用时创建该上下文并传给 handler。`spawn_agent`、`wait_agent`、`kill_agent` 和 `list_agents` 不再通过工厂闭包捕获某个固定 Agent。

修改后，子 Agent 调用 `spawn_agent` 时会创建：

```text
requester_agent_id = 当前子 Agent ID
parent_run_id      = 当前子 AgentRun ID
```

### 3.2 Runtime 与 Journal

Runtime 派生子运行环境时，可以共享无状态基础设施，例如模型客户端、配置和 AgentRegistry；不能共享含有“当前 session / 当前 run / 当前事件列表”的 Journal 实例。

修改后，每次 `Runtime.run()` 持有独立的 Journal run scope，trace 记录显式关联 session、agentrun、task。父子并发执行时，子 Agent 的内部 trace 不会覆盖父 Agent 的 Journal 状态。

### 3.3 Task、Handle 与取消

本轮不引入 retry/attempt 模型，只补齐现有单 Task、单活跃 Handle 的取消状态：

```text
Task:   submitted -> working -> completed | failed | cancelling -> canceled
Handle: idle -> running -> completed | failed | cancelling -> killed
```

- `kill_agent` 只请求取消，Task/Handle 进入 `cancelling`。
- 本地 coroutine 捕获取消并结束后，Dispatcher 统一写入 `canceled` / `killed`。
- `wait_agent` 在取消确认后返回结构化 `TaskResult`，而不是抛出 `CancelledError`。
- `wait_agent` timeout 是 wait 结果，不是 Task 状态；任务继续运行，可稍后再次 wait。

### 3.4 生命周期归属

| 组件 | 本轮完成后的职责 |
|---|---|
| `AgentDispatcher` | 唯一的 spawn、wait、cancel 生命周期入口；完成状态和事件写入 |
| `LocalAgentRunner` | 启动、非破坏性等待、请求取消本地 coroutine；不直接决定最终业务状态 |
| `AgentMessaging` | Agent route 与当前进程内 Task 查询；不再提供取消能力 |
| `AgentInstanceManager` | 仅保存活跃 Handle 和底层 `asyncio.Task`；终态后注销 |
| `Task` | 当前进程内保存任务输入、状态、TaskResult、父/子 run 关联和消费状态 |
| `DelegationEvent` | 由 Dispatcher 创建，并写入现有 Journal / trace |

### 3.5 结果消费

本轮只增加最小消费记录：

- `result_consumed: bool`
- `consumed_at: str`

`wait_agent` 在读取终态结果后标记已消费，并返回现有 JSON 形式的结构化 `TaskResult`。Runtime 的通用工具消息机制继续将结果追加到当前父 AgentRun 的 `context_messages`，父 Agent 随后继续推理。重复 wait 返回同一结果，并明确标记该结果已经消费过。

不新增 `DelegationResultMessage` 类型；当前工具消息模型足以完成本轮闭环。

## 4. 开发步骤

### Phase 1：运行上下文与并发隔离

1. 盘点 Runtime、Journal、ToolExecutor 中的 per-run 可变状态。
2. 为每个 `Runtime.run()` 建立独立 Journal run scope，修正 `Runtime.derive()` 的共享边界。
3. 扩展 `ToolExecutionContext`，由 Runtime 传入当前 Agent、Runtime、session 和 AgentRun ID。
4. 改造 delegation 工具 handler，从执行上下文解析当前 Agent 和 `parent_run_id`；移除顶层 Agent 闭包绑定。
5. 验收父 Agent 与两个子 Agent 并发运行时，trace、session、AgentRun、requester 均正确。

### Phase 2：等待与取消语义

1. 为 Task 与 Handle 新增 `cancelling` 枚举状态和合法状态迁移。
2. 修改 LocalAgentRunner 的 wait：超时只停止当前等待，不取消底层 `asyncio.Task`。
3. 修改 LocalAgentRunner 和 Dispatcher：捕获并转换 `CancelledError`，对外返回 `TaskResult(status=canceled)` 语义，而非向父 Runtime 抛异常。
4. 修改 cancel：先写取消请求；在底层 coroutine 实际结束后由 Dispatcher 写最终终态与事件。
5. 移除或废弃 `AgentMessaging.cancel()`，确保 Dispatcher 是唯一真实取消入口。
6. 验收 timeout 后任务仍运行；kill 后 wait 返回取消结果；父 AgentRun 不被取消。

### Phase 3：任务账本、事件与结果消费

1. 明确 `AgentMessaging` 的当前进程内 Task 查询职责，终态 Task 仍可按 `task_id` 查询。
2. 终态 Handle 从 `AgentInstanceManager` 注销，避免活跃资源索引持续增长。
3. 为 Task 增加 `result_consumed` 与 `consumed_at`，并在 wait 时更新。
4. 将 submitted、started、completed、failed、cancelled、timeout 事件写入现有 Journal / trace。
5. 规范 `wait_agent` JSON 输出：至少稳定包含 task_id、status、TaskResult 与是否已消费。
6. 验收 completed-but-unconsumed 查询、重复 wait、事件可追溯和 list 只显示活跃 Handle。

### Phase 4：本地 delegation 端到端测试

1. 补真实 `LocalAgentRunner` 完成、失败、取消和 wait timeout 测试。
2. 补父 Agent 与多个 child Agent 并发时的 Journal / trace 隔离测试。
3. 补父 -> 子 -> 孙嵌套 delegation 的 requester、parent_run_id 与结果回传测试。
4. 补 `wait_agent` 结果进入父 Agent `context_messages` 后继续 LLM 推理的集成测试。
5. 运行现有单 Agent runtime、工具、handoff 回归测试。

## 5. 验收标准

- `spawn_agent` 异步返回，父 Agent 可继续执行；子任务不共享父 Agent 的用户 session。
- 父子并发运行不会覆盖彼此的 Journal、session、AgentRun 或 trace。
- 任意层级 Agent 调用 delegation 工具时，Task 的 requester 和 parent_run_id 都指向当前调用者。
- wait 超时后任务状态保持 `submitted`、`working` 或既有终态，不会被隐式取消。
- kill 后可观察到 cancelling，子 coroutine 结束后才进入 canceled；wait 返回稳定结果。
- 已取消的子任务不会取消父 AgentRun。
- completed-but-unconsumed 任务可按 task_id 查询；重复 wait 不重复执行任务。
- delegation lifecycle event 出现在现有 trace 中，且可按 task_id 关联。
- `list_agents` 仅返回活跃 Handle；终态结果仍可通过 task_id wait/query 获取。
- 所有新增流程均通过端到端测试，现有单 Agent 行为无回归。

## 6. 后续演进（不纳入本轮）

以下内容依赖本轮闭环完成，但不属于当前开发范围：

- 持久化 `TaskStore` / `EventStore` 与进程重启恢复。
- retry、attempt 历史、幂等 submit 和恢复策略。
- `RemoteAgentRunner`、remote endpoint registry、poll/webhook/streaming 协议。
- 更丰富的 TaskResult metadata、artifact 汇集和结果专用 Message 类型。
- handoff 的真实 session、结构化 HandoffContext 与会话接管语义整改。

本轮 Phase 1 至 Phase 4 完成后，再根据实际使用压力决定是否启动上述任一演进项。
