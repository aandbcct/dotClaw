# Multi-Agent Delegation Dispatcher 开发计划

> 版本: v0.1
> 状态: Draft
> 日期: 2026-07-10
> 关联: [PRD](./PRD.md), [DESIGN](./DESIGN.md)

## 1. 目标

Multi-agent 主路径定义为 delegation/spawn，而不是 handoff。

父 Agent 将一个隔离上下文的任务委托给目标 Agent 执行。目标 Agent 在本地或远程独立运行，完成后返回结构化 `TaskResult`。父 Agent 通过 `wait_agent` 消费结果，把结果注入当前推理上下文，然后继续推理、汇总并回复用户。

本计划覆盖以下路径:

| 路径 | 语义 | 结果归属 |
|---|---|---|
| `spawn/delegate` | 父 Agent 委托本机子 Agent 执行隔离任务 | 返回父 Agent，由父 Agent 继续推理 |
| `delegate_remote` | 父 Agent 委托网络外部 Agent 执行远程任务 | 返回父 Agent，由父 Agent 继续推理 |
| `handoff` | 当前会话控制权转交目标 Agent | 目标 Agent 直接面向用户 |

`handoff` 是窄路径，只用于路由、转接、接管会话。外部 Agent 场景不走 handoff，而走远程任务委托。

## 2. 核心原则

1. `task_id` 是主索引，面向父 Agent、工具调用和用户可见结果。
2. `handle_id` 是运行实例索引，主要用于调试、trace 和底层取消映射。
3. `Task` 表示业务委托单元，描述委托了什么、目标是谁、结果是什么。
4. `AgentHandle` 表示运行实例句柄，描述任务当前由哪个本地或远程执行实例承接，以及如何等待和取消。
5. `AgentDispatcher` 是 delegation 生命周期入口，但不吞掉 registry、messaging、instance manager、runner 的职责。
6. `TaskResult` 是结构化结果，不是简单字符串。
7. `wait_agent` 是结果消费位置，不是结果产生位置。
8. 子 Agent 通信和用户 session 分离。用户 session 只保存用户输入和父 Agent 最终回复，delegation 细节进入 task/event/journal/trace。

## 3. 架构总览

```text
spawn_agent / wait_agent / kill_agent / list_agents
        |
        v
AgentDispatcher
        |
        +-- AgentRegistry.resolve(target_agent_id) -> target endpoint
        |
        +-- AgentMessaging
        |     route target
        |     track/untrack task
        |     task lookup
        |
        +-- AgentInstanceManager
        |     register handle
        |     get by handle_id
        |     get by task_id
        |     list active
        |
        +-- LocalAgentRunner
        |     submit -> asyncio.create_task(child_agent.execute(...))
        |     wait   -> await local task/result
        |     cancel -> cancel asyncio.Task
        |
        +-- RemoteAgentRunner
              submit -> HTTP/A2A/MCP/custom tasks/send
              wait   -> poll/webhook/stream receive
              cancel -> remote cancel best effort
```

`AgentDispatcher` 根据 registry 中的目标类型选择 runner。它维护本地 `task_id` / `handle_id` 映射和 delegation 生命周期，不知道本地或远程 runner 的具体执行细节。

## 4. 模块职责

### 4.1 AgentDispatcher

统一的 delegation 生命周期入口。

职责:

- 创建本地 `Task`。
- 根据目标 Agent 类型选择 `AgentRunner`。
- 调用 runner submit/wait/cancel。
- 注册和更新 `AgentHandle`。
- 写入 delegation lifecycle event。
- 维护 `Task` 状态和 `TaskResult`。
- 为工具层提供 `spawn/wait/cancel/list` 能力。

不负责:

- 不直接保存 registry 数据。
- 不直接实现本地 Agent 执行细节。
- 不直接实现远程协议细节。
- 不把子 Agent 结果写进用户 session conversation。

### 4.2 AgentMessaging

通信与任务追踪账本。

职责:

- `route(agent_id)` 查找目标 Agent。
- `track(task)` 注册活跃任务。
- `untrack(task_id)` 清理任务追踪。
- `get_task(task_id)` 查询任务。
- `list_active_tasks()` 列出活跃任务。

调整点:

- 将当前 `send(task, target)` 改名为 `track(task, target)`。
- `cancel(task_id)` 不再负责真实取消，只可作为兼容层转发或移除。

### 4.3 AgentInstanceManager

运行实例索引。

职责:

- 注册 `AgentHandle`。
- 按 `handle_id` 查询 handle。
- 按 `task_id` 查询当前 active handle。
- 列出 active handles。
- 注销终止实例。

不负责:

- 不创建 Agent。
- 不启动 `asyncio.Task`。
- 不执行 wait/cancel 逻辑。

### 4.4 AgentRunner

本地和远程执行能力的统一抽象。

```python
class AgentRunner:
    async def submit(task: Task, context: SpawnContext) -> AgentHandle: ...
    async def wait(handle_id: str, timeout: float | None = None) -> TaskResult: ...
    async def cancel(handle_id: str) -> bool: ...
```

#### LocalAgentRunner

职责:

- 派生 child runtime。
- 创建 child Agent。
- 使用 `asyncio.create_task(child_agent.execute(...))` 启动隔离执行。
- 保存底层 `asyncio.Task`。
- 支持真实取消。

#### RemoteAgentRunner

职责:

- 调用远程 submit 接口。
- 保存 `remote_task_id`。
- 通过 poll、webhook 或 streaming receive 获取结果。
- 调用远程 cancel API，取消是 best effort，不能保证强杀。

第一版可以先实现 `LocalAgentRunner`，保留 `RemoteAgentRunner` 接口和字段。

### 4.5 Task

业务委托单元。

核心字段方向:

- `task_id`
- `requester_agent_id`
- `target_agent_id`
- `target_kind`: `local` / `remote`
- `description`
- `context`
- `constraints`
- `status`
- `result`
- `error`
- `active_handle_id`
- `remote_task_id`
- `parent_run_id`
- `sub_run_id`

`Task` 不直接持有底层 `asyncio.Task`。

### 4.6 TaskResult

子 Agent 完成后的结构化结果。

核心字段方向:

- `summary`: 给父 Agent 快速理解和上下文压缩。
- `content`: 完整结果正文。
- `artifacts`: 文件、数据、补丁、报告等产物引用。
- `metadata`: token、duration、trace、remote protocol、warnings、confidence 等扩展信息。

不建议只保留 `final_result: str`，否则父 Agent 后续推理、trace 和远程协议扩展都会受限。

### 4.7 AgentHandle

运行实例句柄。

核心字段方向:

- `handle_id`
- `task_id`
- `agent_id`
- `runner_kind`: `local` / `remote`
- `status`
- `remote_task_id`
- `asyncio_task`
- `created_at`
- `updated_at`
- `metadata`

命名调整:

- 将 `orchestration.handle.AgentStatus` 改为 `AgentInstanceStatus`。
- `runtime.agent_state.AgentStatus` 保持不变，用于单次 AgentRun 状态机。

### 4.8 DelegationEvent

delegation 生命周期事件，作为内部事实来源之一。

事件类型方向:

- `submitted`
- `started`
- `completed`
- `failed`
- `cancelled`
- `timeout`

事件字段方向:

- `event_id`
- `task_id`
- `handle_id`
- `parent_agent_id`
- `target_agent_id`
- `target_kind`
- `event_type`
- `timestamp`
- `payload`

Delegation event 用于审计、恢复、trace、debug 和未来异步通知。它不等同于父 Agent 的 tool result。

## 5. 端到端流转

### 5.1 Spawn / Delegate

```text
Parent Agent
    |
    | tool call: spawn_agent(target_agent_id, description, context, constraints)
    v
spawn_agent tool
    |
    v
AgentDispatcher.spawn
    |
    +-- create Task
    +-- AgentMessaging.track(task)
    +-- select runner by target_kind
    +-- runner.submit(task, SpawnContext)
    +-- AgentInstanceManager.register(handle)
    +-- emit DelegationEvent(submitted/started)
    |
    v
tool result:
{
  "task_id": "...",
  "handle_id": "...",
  "target_agent_id": "...",
  "status": "submitted|working"
}
```

`spawn_agent` 默认异步返回，不等待子 Agent 完成。

### 5.2 Local Runner Execution

```text
LocalAgentRunner.submit
    |
    +-- derive child Runtime
    +-- create child Agent
    +-- create asyncio.Task(child_agent.execute(...))
    +-- attach task to AgentHandle
    |
    v
Child Agent isolated execution
    |
    v
TaskResult
    |
    v
Dispatcher updates Task.result and emits DelegationEvent(completed|failed)
```

本地 runner 可以真实 cancel 底层 `asyncio.Task`。

### 5.3 Remote Runner Execution

```text
RemoteAgentRunner.submit
    |
    +-- call remote tasks/send
    +-- receive remote_task_id
    +-- store remote_task_id on handle/task metadata
    |
    v
Remote Agent isolated execution
    |
    v
Remote result via poll/webhook/stream
    |
    v
TaskResult
    |
    v
Dispatcher updates local Task.result and emits DelegationEvent(completed|failed)
```

远程 Agent 不拥有本地 session、父 Agent context 或 task mapping。远程 Agent 只拥有自己的远程任务上下文。

### 5.4 Wait

```text
Parent Agent
    |
    | tool call: wait_agent(task_id)
    v
wait_agent tool
    |
    v
AgentDispatcher.wait(task_id)
    |
    +-- find active handle by task_id
    +-- runner.wait(handle_id)
    +-- read TaskResult
    +-- mark result consumed if needed
    |
    v
tool result as structured delegation result message
    |
    v
Runtime.context_messages
    |
    v
Parent Agent continues reasoning and produces final user response
```

`wait_agent` 的工具返回必须包含结构化 `TaskResult`，使父 Agent 能继续推理。

### 5.5 Kill / Cancel

```text
Parent Agent
    |
    | tool call: kill_agent(task_id)
    v
kill_agent tool
    |
    v
AgentDispatcher.cancel(task_id)
    |
    +-- find active handle by task_id
    +-- LocalAgentRunner.cancel -> asyncio.Task.cancel()
    +-- RemoteAgentRunner.cancel -> remote cancel API best effort
    +-- update Task/Handle status
    +-- emit DelegationEvent(cancelled)
```

本地取消应真实取消运行中的 coroutine。远程取消只能表示已经请求远端取消，结果需要按远端协议确认。

### 5.6 List

```text
list_agents
    |
    v
AgentDispatcher.list
    |
    +-- AgentInstanceManager.list_active()
    +-- join Task summary
    |
    v
[
  {
    "task_id": "...",
    "handle_id": "...",
    "target_agent_id": "...",
    "target_kind": "local|remote",
    "status": "...",
    "description": "..."
  }
]
```

## 6. 与 Handoff 的边界

Delegation/spawn:

- 父 Agent 保留当前用户 session。
- 子 Agent 隔离执行任务。
- 结果回到父 Agent。
- 父 Agent 继续推理并最终回复用户。

Handoff:

- 当前会话控制权转交目标 Agent。
- 目标 Agent 直接面向用户。
- 父 AgentRun 以 handoff 结束。
- 不用于远程任务委托。

当前 multi-agent orchestration 的主线是 `spawn_agent + wait_agent + kill_agent + list_agents`。

## 7. 开发步骤

### Phase 1: 模型与命名收敛

- 引入 `TaskResult`。
- 扩展 `Task` 字段，补齐 requester、target、target_kind、active_handle_id、remote_task_id、result。
- 将 `orchestration.handle.AgentStatus` 改名为 `AgentInstanceStatus`。
- 扩展 `AgentHandle`，使其表达 local/remote runner metadata。
- 为 `AgentInstanceManager` 增加按 `task_id` 查询能力。
- 将 `AgentMessaging.send()` 改名为 `track()`。

### Phase 2: Dispatcher 与 Runner 抽象

- 新增 `AgentDispatcher`。
- 新增 `AgentRunner` 抽象。
- 新增 `SpawnContext`，承载父 Agent、runtime、parent_run_id、上下文摘要等提交所需信息。
- 实现 `LocalAgentRunner` 的 submit/wait/cancel 主链路。
- 预留 `RemoteAgentRunner` 接口和字段，不要求第一版接入真实远程协议。

### Phase 3: 工具语义调整

- 改造 `spawn_agent` 为异步提交，返回 `task_id/handle_id/status`。
- 新增 `wait_agent`，按 `task_id` 等待结果并返回结构化 `TaskResult`。
- 新增或补齐 `list_agents`。
- 改造 `kill_agent`，按 `task_id` 优先取消，底层通过 dispatcher 找 active handle。
- 工具 schema 中鼓励使用 `task_id`，`handle_id` 作为可选调试参数。

### Phase 4: Delegation Event 与可观测性

- 新增 `DelegationEvent` 模型。
- 在 submitted、started、completed、failed、cancelled、timeout 节点产生事件。
- 将事件接入现有 journal/trace 体系。
- 区分 delegation event 和 wait_agent tool result。
- 确保子 Agent 内部执行细节不污染用户 session conversation。

### Phase 5: 父 Agent 上下文回灌

- 规范 `wait_agent` 工具返回格式。
- 将 `TaskResult` 作为结构化 tool message 进入父 Agent 当前 `context_messages`。
- 确保父 Agent 在收到结果后继续一轮推理，而不是直接把子 Agent 输出原样返回用户。
- 明确 completed-but-unconsumed 任务的状态记录方式，为恢复和异步通知预留空间。

### Phase 6: Handoff 收窄与修正

- 保留 handoff 窄路径，不作为外部 Agent 委托方案。
- 后续单独修正 handoff 使用 `_dummy_session()`、裸字符串上下文、结果不回注等问题。
- 在文档和工具描述中明确 delegation 与 handoff 的边界。

### Phase 7: Remote Runner 演进

- 定义 remote endpoint registry 结构。
- 实现远程 submit/wait/cancel 协议适配。
- 保存 `remote_task_id` 与本地 `task_id/handle_id` 映射。
- 明确远程 cancel 的 best-effort 语义。
- 支持 poll、webhook 或 streaming receive 中至少一种结果接收方式。

### Phase 8: 测试与验收

- 覆盖 `TaskResult` 序列化和状态流转。
- 覆盖 `AgentDispatcher.spawn/wait/cancel/list`。
- 覆盖 `LocalAgentRunner` 真实取消。
- 覆盖 `spawn_agent` 异步返回，不阻塞等待子 Agent。
- 覆盖 `wait_agent` 结果注入父 Agent 推理上下文。
- 覆盖 completed-but-unconsumed 任务查询。
- 保持现有单 Agent runtime 测试通过。

## 8. 已确认决策

| 决策点 | 结论 |
|---|---|
| multi-agent 主路径 | delegation/spawn，不是 handoff |
| 外部 Agent 场景 | delegate_remote，不是 handoff |
| 工具主线 | spawn_agent + wait_agent + kill_agent + list_agents |
| spawn_agent 默认行为 | 异步提交，返回 `task_id/handle_id/status` |
| 内部概念命名 | delegate 优先，spawn 可作为工具名保留 |
| 生命周期 owner | 新增 `AgentDispatcher` |
| runner 抽象 | `LocalAgentRunner` + `RemoteAgentRunner` |
| TaskResult 定位 | 子 Agent 完成后的结构化产物 |
| 结果产生位置 | Dispatcher/Runner 更新 Task 并产生 DelegationEvent |
| 结果消费位置 | wait_agent 返回结构化结果并注入父 Agent context |
| session 记录 | 用户 session 不记录内部 delegation 细节 |
| 主索引 | `task_id` |
| 运行实例索引 | `handle_id` |
| 当前 AgentStatus 冲突 | handle 层改为 `AgentInstanceStatus` |

## 9. 仍需后续细化的问题

以下问题不阻塞第一版 delegation dispatcher 开发，但需要在进入 remote runner 或恢复能力前继续细化:

1. `TaskStore` 和 `EventStore` 第一版是否只做内存态，还是直接接入现有 journal/state store。
2. completed-but-unconsumed 任务是否需要显式 `consumed_at` 或 `result_consumed` 字段。
3. remote endpoint 的 registry schema。
4. remote wait 优先选择 poll、webhook 还是 streaming receive。
5. retry 模型是否引入 `HandleAttempt`，以及何时从 `1 Task : 1 Handle` 演进到 `1 Task : N Attempts`。
6. delegation event 在 journal 中的事件命名和 report 展示格式。
