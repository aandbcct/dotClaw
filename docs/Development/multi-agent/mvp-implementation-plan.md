# Multi-Agent Delegation 可靠持久化实施计划（V1）

> 状态：后续阶段；以前置的同进程 MVP 闭环完成为条件
> 日期：2026-07-14  
> 定位：本计划为 delegation 增加持久化 Task、checkpoint/resume 与重启恢复；它不替代第一阶段的同进程 MVP。第一阶段见 [同进程 Multi-Agent MVP 计划](../../../multi-agent/in-process-mvp-plan.md)。

## 1. 目标与范围

本次只交付一个可靠的本地 delegation 闭环：源 Agent 把一项任务委托给目标 Identity；目标在独立 Session 中完成任务；双方经由持久化消息流沟通；目标最终把结果交还源 Agent；任一端等待时可安全挂起，并可在进程重启后恢复。

系统中没有独立的“子 Agent”概念。一次 delegation 的本质是：

```text
source Session + source AgentIdentity
  -- Task -->
target Session + target AgentIdentity
```

`Agent` 仍是 `AgentIdentity + Runtime` 的可执行绑定；Session 才是上下文隔离边界。

### MVP 约束

- 仅支持本地目标 Identity；不实现 Remote/A2A Runner。
- 每次 delegation 必定创建新的 target Session；不能复用 source Session 的完整历史。
- 每个 source Session 同时最多一个活跃 Task；多个委托必须串行。
- 不支持嵌套 delegation、后台长期任务、多目标广播和跨 Task 树直连通信。
- source Run 在结束前必须等待其活跃 Task 进入终态，或显式取消它。
- Task 的初始任务契约不可变；实质性改目标应取消旧 Task 并创建新 Task。

## 2. 领域模型与职责边界

### 2.1 Task：委托实体

`Task` 是可持久化的聚合，不是普通消息，也不是 Runtime 上下文。它保存：

- 身份与关系：`task_id`、`source/target_session_id`、`source/target_identity_id`、预留的 `parent_task_id`；
- 不可变 `TaskSpecification`：任务标题、目标、初始材料引用、约束、预期交付物；
- 当前投影：状态、最后消息序号、结果消息索引、时间戳；
- 调度索引：当前应行动的端点与恢复所需的关联 ID。

`Task` 不保存完整 prompt、Memory 检索结果、Session 历史或任何一次拼装完成的 RuntimeContext。

### 2.2 TaskMessage：双向通信记录

一个 Task 只有一条顺序递增的 append-only 消息流。每条消息至少包含：Task ID、全局 sequence、发送端点、接收端点、发送 Session/Run、消息类型、payload、时间戳和幂等 ID。

首版消息类型：`request`、`progress`、`question`、`reply`、`context_update`、`result`、`failed`、`cancelled`。

消息是已经发生的通信事实，不修改、不覆盖。`Task` 是根据消息流维护的当前状态与结果索引；需要时可以重建。

### 2.3 Task 状态机

Task 状态表示下一步由谁行动，而不是笼统的“工作中”。

```text
创建 Task + request  -> READY_TARGET
READY_TARGET          -> RUNNING_TARGET
RUNNING_TARGET        -> WAITING_SOURCE  (target 写入 question)
WAITING_SOURCE        -> READY_TARGET    (source 写入 reply/context_update)
RUNNING_TARGET        -> COMPLETED       (target 写入 result)
任意非终态           -> FAILED / CANCELLED
```

`COMPLETED` 只有在 `result` 消息与结果索引在同一事务提交后才能进入；`FAILED` 和 `CANCELLED` 也必须有对应终态消息。终态后拒绝普通业务消息。

### 2.4 RuntimeCheckpoint：通用暂停/恢复内核

暂停和恢复不属于 Task 的专属能力。它应成为 Runtime 的通用基础设施，供审批、Task 消息和未来定时器共享。

`RuntimeCheckpoint` 保存某个暂停 Run 的状态机快照、消息历史、待补齐的工具调用 ID、等待条件、Session/Identity/Run 关系和恢复游标。`WaitCondition` 的一种类型是 `task_message`；它携带 Task ID、端点、消息 sequence 和唤醒条件。

TaskCoordinator 只负责写消息、更新 Task 投影并寻找被唤醒端点；它不重建 AgentState 或 LLM 上下文。通用 ResumeCoordinator 才负责抢占 checkpoint、恢复 Runtime、追加对应 tool result 并继续推理。

## 3. 持久化与恢复

以 SQLite（WAL）作为下列数据的唯一恢复权威：

- Task、TaskSpecification；
- TaskMessage 与端点消费游标；
- RuntimeCheckpoint、WaitCondition；
- Session lease（保证同一 Session 任一时刻只有一个活跃 Runtime Run）。

关键转换必须在一个 SQLite 事务内完成。例如：写入 `result` 消息、设置 `result_message_id`、更新 Task 为 `COMPLETED`。

现有 JSON Session 可以继续保存用户可见的 Conversation；创建 target Session 时先持久化 Session，再原子提交 Task 与初始 `request`，崩溃产生的孤立 Session 由启动时清理。JSON AgentRun/StateSnapshot 在迁移期仅作为审计或兼容导出，不再作为恢复真相。

进程启动后，Coordinator 必须扫描并恢复：

- `READY_TARGET` 的目标 Session；
- `WAITING_SOURCE` 的源 Session；
- 已满足等待条件但尚未消费的 RuntimeCheckpoint；
- 过期 Session lease。

## 4. Runtime 与上下文接入

### 4.1 挂起与恢复

新增通用 `WAITING_TASK`（或通用 `WAITING_EXTERNAL`）状态与对应恢复事件。`wait_task` 不再在 ToolExecutor 中长时间阻塞，也不返回“仍在等待”的普通文本；它向 Runtime 返回结构化挂起控制信号。

恢复一个等待 Task 消息的 Run 时，Runtime 必须先追加与原 `wait_task` tool call 对应的 `role="tool"` 消息，内容为未消费的 TaskMessage，再进行下一次 LLM 调用。不能把消息直接当成新的 user 输入，否则工具调用协议不完整。

### 4.2 SlotContext

为委托运行增加 Task 元数据输入和相应 Slot，用于告知 Agent 当前 Task、对端身份、消息工具和通信规则。Task 的正文、材料和补充内容应以 target Session 的 `user` 消息或 `tool` 消息进入上下文，不能提升为 system prompt。

必须修复当前 ContextAssembler 的缓存隔离：派生 Runtime 不得与父 Runtime 共享带缓存的 Slot 实例，或缓存必须显式按 `agent_id + session_id + task_id + request_id` 分区。不同 Identity 的 system prompt 和工具白名单不能相互复用。

## 5. 需要修改的现有模块

| 模块 | 本次修改目标 |
|---|---|
| `src/dotclaw/orchestration/task.py` | 重定义为持久化 Task 聚合，加入 TaskSpecification、状态投影、端点关系和结果索引；移除以进程内 `asyncio.Event` 为核心的真相来源。 |
| `src/dotclaw/orchestration/dispatcher.py` | 从内存 spawn/wait/cancel 分发器演进为 delegation 门面；委托、消息、等待、取消统一交给持久化仓储与 Coordinator。 |
| `src/dotclaw/runtime/runtime.py` | 支持显式 ResumeCommand、Task 挂起状态、恢复时补齐 tool result、Session lease 和通用 checkpoint。 |
| `src/dotclaw/runtime/agent_state.py` | 增加等待外部 Task 消息的状态、事件和合法转换；将等待原因建模为数据，而不是隐式 `ContinueEvent`。 |
| `src/dotclaw/runtime/state_store.py` | 从 JSON 单 Session 快照迁移为 SQLite checkpoint 仓储的兼容层；不再是恢复权威。 |
| `src/dotclaw/session/session.py`、`agent_run.py` | 支持 target Session 创建与委托关系展示；AgentRun/JSON 记录改为审计投影。 |
| `src/dotclaw/tools/builtin/spawn_tool.py` | 收敛为 delegation 工具入口；修复现有 `wait_agent/list_agents` 的未定义 `_resolve_agent` 调用；增加 Task 消息/等待/取消工具的契约。 |
| `src/dotclaw/tools/executor.py`、`handler.py` | 支持工具返回结构化控制信号，让 Runtime 能截获 suspend，而非把它字符串化为模型上下文。 |
| `src/dotclaw/agent/slotContext.py`、`slotContextImp.py` | 加入 Task 元数据槽，并实现正确的 scope-aware 缓存策略。 |
| `src/dotclaw/agent/factory.py` | 装配 SQLite 仓储、TaskCoordinator、ResumeCoordinator；子 Session 使用完整依赖组合，而不是回退到顶层 Agent。 |
| `src/dotclaw/agent/agent.py` | 收敛 delegation facade；不再保存或回退到顶层 Agent 的 dispatcher/messaging 以处理子 Session 的委托。 |

## 6. 建议新增模块

| 建议模块 | 职责 |
|---|---|
| `orchestration/task_repository.py` | SQLite 中 Task、TaskSpecification、TaskMessage、端点 cursor 的原子读写与投影更新。 |
| `orchestration/task_coordinator.py` | 创建委托、追加消息、改变 Task 状态、寻找满足唤醒条件的端点；不直接运行 LLM。 |
| `runtime/checkpoint_repository.py` | SQLite 中 RuntimeCheckpoint、WaitCondition 和 Session lease 的事务性读写。 |
| `runtime/resume_coordinator.py` | 认领满足条件的 checkpoint，调用 Runtime 的显式恢复入口；负责启动扫描与重试。 |
| `orchestration/models.py`（可选） | 集中 TaskSpecification、TaskMessage、WaitCondition 等跨模块模型，避免在工具/Runtime/仓储间循环依赖。 |

模块名可随目录布局调整，但职责边界不得合并为一个新的“大 Dispatcher”。

## 7. 本次改造后不再作为主路径的代码

以下代码可以在兼容期保留，但不得继续承担 delegation 的状态真相或恢复职责；稳定后应删除或归档。

| 现有代码/模块 | 处理方式 | 原因 |
|---|---|---|
| `src/dotclaw/agent/resume.py` 的 `ResumeManager` | 删除或重写为通用 ResumeCoordinator 的薄兼容层 | 当前未接入主执行路径，且与 Runtime 自行恢复逻辑重叠。 |
| `AgentMessaging` 的 `_active_tasks` 内存账本 | 移除为权威存储；可保留为只读缓存 | 不能承受重启、并发或终态归档。 |
| `AgentInstanceManager` | 不再作为持久化 Handle 索引；仅可作进程内诊断缓存 | Handle 生命周期随进程丢失。 |
| `AgentHandle.asyncio_task` 与 `LocalAgentRunner` 的 `asyncio.create_task` 主链路 | 由 checkpoint + coordinator 驱动的 Session 恢复替代 | 不能表达双向等待、重启恢复或可靠状态转换。 |
| Dispatcher 的 watcher、`_watchers`、内存 `_events` | 替换为数据库状态与 Journal 事件 | watcher 不能跨进程，现有 Journal 写入还是空实现。 |
| `Runtime.run()` 自动选取“最后一个 waiting AgentRun”恢复的逻辑 | 改为只接受 ResumeCoordinator 显式认领的 checkpoint | 不能区分审批、Task、用户输入等等待原因。 |
| JSON `StateStore` / `AgentRun` 作为恢复权威 | 降级为审计、导出或兼容读取 | 无法与 SQLite Task 消息完成原子提交。 |
| delegation 工具对工厂顶层 Agent 的 fallback | 删除 | 会让嵌套/目标 Session 的身份、父 Run 与权限归属错误。 |

现有 handoff 可暂时保留为遗留的会话控制权切换能力，但本次 delegation MVP 不调用它，也不与它共享递归 Runtime 执行路径。

## 8. 分阶段实施计划

### 阶段 A：边界收口与回归基线

- 固化本计划的术语和状态机；将旧设计文档标记为历史参考。
- 补齐现有 delegation 工具的基础回归测试，先修复未定义函数和失败 Task 无法查询等直接缺陷。
- 修复 Slot 缓存跨 Identity/Session 污染的问题，作为 child Session 隔离前置条件。

**完成标志**：旧 delegation 不再有显式运行错误；现有单 Agent 与工具测试不回归。

### 阶段 B：持久化领域模型

- 建立 SQLite schema 与仓储，落地 Task、Specification、Message、Cursor、Checkpoint、Wait、Lease。
- 实现 Task 状态投影和事务性终态写入。
- 实现 target Session 的创建、初始 request 和孤立 Session 清理策略。

**完成标志**：不启动 LLM 也能创建 Task、读写双向消息、维护 cursor，并在重启后完整恢复。

### 阶段 C：Runtime 通用暂停/恢复

- 用通用 checkpoint 替换 JSON 恢复主路径。
- 实现 `WAITING_TASK`、显式 ResumeCommand 与 tool-call 协议补齐。
- 引入 Session lease，保证同一 Session 单飞。

**完成标志**：一个普通 Run 可因等待条件暂停，进程重启后被显式恢复并继续完成同一轮推理。

### 阶段 D：TaskCoordinator 与工具闭环

- 由 TaskCoordinator 将消息投递转换为 checkpoint 唤醒。
- 提供最小工具集：创建 delegation、发送 TaskMessage、等待 TaskMessage/终态、取消、查询状态。
- 目标提问、源回复、目标完成、源汇总四段链路全部通过 Coordinator 驱动。

**完成标志**：核心闭环端到端运行，不再依赖 `asyncio.Task` watcher 作为生命周期真相。

### 阶段 E：观测、迁移与清理

- 将 Task 生命周期和消息事件写入 Journal/trace；实现查询与故障诊断视图。
- 将旧 JSON resume 与内存 dispatcher 退为兼容层或删除。
- 归档/更新过期的 multi-agent 文档，避免与新模型并存。

**完成标志**：SQLite 数据、Journal trace 和用户可见 Session 能相互关联；旧主路径不再被调用。

## 9. MVP 验收标准

1. 源 Agent 创建 delegation 后，系统创建独立 target Session，目标只接收 TaskSpecification 和精选材料。
2. 目标发送 `question` 后进入 `WAITING_SOURCE`；源 Session 被恢复并能发送 `reply`。
3. 源回复后目标被恢复；目标写入 `result`，Task 与结果索引在同一事务中变为 `COMPLETED`。
4. 源被恢复并消费 result，生成对用户的最终回答；源 Run 结束时没有活动 Task。
5. 在目标等待、源等待和结果提交后三个时点强制重启，系统都能恢复并只消费一次消息。
6. 取消、失败、重复投递、重复恢复和并发恢复不会产生重复终态或两个同时运行的同 Session Run。
7. 不同 Identity/Session 的 Slot 缓存、工具白名单和消息上下文不会相互泄漏。

## 10. MVP 之外的后续方向

- 多个并行 Task、Task 树、递归预算和结果聚合；
- Remote/A2A Runner 与跨进程消息传输；
- 后台任务、用户通知和任务优先级；
- TaskSpecification 版本化、Artifact 生命周期和大结果的引用存储；
- 面向调试的 Task 树视图、重放和运维控制台。

在上述 MVP 验收完成前，不应以新增 Agent 身份、模型供应商或通道功能替代委托闭环的可靠性工作。
