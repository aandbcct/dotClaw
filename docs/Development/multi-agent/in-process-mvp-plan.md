# 同进程 Multi-Agent MVP 实施计划

> 状态：已冻结范围，待实施  
> 日期：2026-07-14  
> 后续阶段：[可靠持久化与 checkpoint/resume 计划](mvp-implementation-plan.md)。

## 1. 目标

本阶段只验证本地、同进程 delegation 闭环：源 Agent 委托目标 Identity 在独立 Session 中执行；双方通过内存消息队列通信；目标返回结果后，源 Agent 汇总并回答用户。

本阶段不将 Runtime 改造成可恢复工作流引擎。进程退出、崩溃或重启会终止所有活动 Task；不承诺恢复未完成 delegation。

```text
source Session / source Identity
  └─ delegate(TaskSpecification, target Identity)
       └─ target Session / target Identity
            ↕ in-memory TaskMessageBroker
       source 收到 result 后完成对用户的回答
```

## 2. 已冻结的 MVP 规则

- 只有 `delegation` 一种协作模式；没有独立的“子 Agent”执行模型。
- AgentIdentity 是身份与能力声明；Agent 是 Identity 与 Runtime 的可执行绑定；Session 是上下文隔离边界。
- 每次 delegation 创建新的 target Session，不能复用 source Session 的完整历史。
- source 只传递 TaskSpecification 和精选材料；target Runtime 仍在 `run()` 开始时通过 Slot 组装完整 RuntimeContext。
- 每个 source Session 同时最多一个活动 Task，多个委托串行。
- 首版仅一层 delegation；target Agent 不能继续委托。
- 一个 Task 只连接一个 source 和一个 target；双方只围绕该 Task 点对点通信。
- source Run 结束前必须等待 Task 终态或显式取消；没有后台任务。
- 仅支持本地、同进程执行；没有远程 Agent、外部队列、跨进程 worker 或 Web 通知。

## 3. 领域模型

### Task

Task 是进程内的委托实体，保存：Task ID、source/target Session 和 Identity、不可变 TaskSpecification、当前状态、最终 TaskResult/错误、target Session/运行句柄关联、消息消费位置和取消标记。Task 应保持可序列化，但本阶段不是恢复对象。

### TaskSpecification

TaskSpecification 保存稳定任务契约：标题、目标、精选材料/引用、约束和预期交付物。它不是完整 prompt，也不保存 Session 历史、Memory 检索和 Slot 输出。

target 首次运行时，TaskSpecification 渲染为 `role=user` 的任务请求消息；材料不能提升为 system prompt。

### TaskMessage

每个 Task 有一条 sequence 递增的内存消息流。消息携带 Task ID、发送/接收端点、发送 Session/Run、类型、payload 和时间戳。

首版消息类型：`request`、`progress`、`question`、`reply`、`context_update`、`result`、`failed`、`cancelled`。

### 状态机

```text
SUBMITTED -> RUNNING_TARGET
RUNNING_TARGET -> WAITING_SOURCE   (target 发出 question)
WAITING_SOURCE -> RUNNING_TARGET   (source 发出 reply/context_update)
RUNNING_TARGET -> COMPLETED         (target 发出 result)
任意非终态 -> FAILED / CANCELLED
```

终态后拒绝普通业务消息；`result`、`failed`、`cancelled` 与 Task 状态同步更新。

## 4. 执行与通信

### TaskMessageBroker

新增进程内 `TaskMessageBroker`，以 `(task_id, recipient_endpoint)` 管理消息队列、消费位置和 `asyncio.Condition/Event` 通知，提供追加消息、读取未消费消息、等待下一条消息、关闭和取消 Task 的能力。

Broker 是唯一通信通道。source 与 target 不得直接调用对方 Runtime，也不得读取对方 Session。

### 等待方式

`wait_task` 是普通异步工具等待：等待新消息或终态后，将消息作为该 tool call 的正常结果交给发起方 LLM。target 先发 `question` 再等待 `reply`；source 收到问题后推理、发送回复；target 随后继续。

等待中的 Run 保持在当前进程内活动。超时仅返回当前状态给模型，由模型继续等待或取消。MVP 不使用 `WAIT_SENTINEL`、StateStore、Run checkpoint 或自动恢复。

### Session 与 Slot 隔离

target 必须使用独立 Session、派生 Runtime 和独立的 Assembler/Slot 实例。可共享 LLMProxy、限流和工具注册表，但不同 Identity/Session 的 system prompt、工具白名单和 Session 级 Slot 缓存不得互相复用。

## 5. 模块变更

| 模块 | MVP 修改目标 |
|---|---|
| `orchestration/task.py` | 扩展为 TaskSpecification、TaskMessage、状态和结果视图；维持内存生命周期。 |
| `orchestration/dispatcher.py` | 继续作为本地 delegation 门面：创建 target Session、Task、Broker 绑定和 LocalAgentRunner；限制单活动 Task。 |
| `orchestration/runners/local.py` | 使用完整装配的 target Agent 与派生 Runtime 执行独立 target Session；传播完成、失败与取消。 |
| `orchestration/handle.py`、`instance_manager.py` | 继续作为本进程运行句柄和索引，补齐 target Session 关联与终态清理。 |
| `orchestration/messaging.py` | 收缩为 Identity 寻址和进程内 Task 查询；不承担持久化账本。 |
| `tools/builtin/spawn_tool.py`、`kill_tool.py` | 收敛为 delegate、消息发送、等待、查询、取消；修复 `wait_agent/list_agents` 未定义 `_resolve_agent`。 |
| `tools/executor.py` | 为等待工具配置合理超时，并把取消传递至等待协程。 |
| `agent/agent.py`、`agent/factory.py` | 完整装配 target Agent；删除回退到顶层 Agent dispatcher 的路径。 |
| `session/session.py` | 创建、命名并关联 target Session；用户主 Session 不写入 target 内部过程。 |
| `runtime/runtime.py` | 保持现有运行和审批恢复；只提供 ToolExecutionContext，不增加 Task checkpoint/resume。 |
| `agent/slotContext.py`、`slotContextImp.py` | 注入 Task 元数据/通信规则，并修复跨 Identity/Session Slot 缓存泄漏。 |

## 6. 新增模块

| 模块 | 职责 |
|---|---|
| `orchestration/message_broker.py` | 进程内 TaskMessage 队列、消费游标、等待通知、Task 关闭与取消传播。 |
| `orchestration/task_specification.py`（可选） | 集中 TaskSpecification、TaskMessage、端点和消息类型，避免循环依赖。 |

本阶段明确不新增 SQLite 仓储、CheckpointRepository、ResumeCoordinator、远程 Runner 或外部消息队列。

## 7. 不纳入 MVP 主路径的现有能力

- `runtime/state_store.py`、`agent/resume.py`、`Runtime.WAIT_SENTINEL` 继续只服务既有审批恢复，不承载 Task 等待；
- `TaskTargetKind.REMOTE`、`RunnerKind.REMOTE` 保留类型预留，不提供实现；
- `Runtime._handle_handoff()` 保留为旧会话控制权切换路径，delegation MVP 不调用它；
- SQLite Task/Message、checkpoint/resume、Session lease、重启扫描属于后续可靠持久化阶段；
- 多任务并行、嵌套 delegation、任务树聚合延后。

## 8. 实施阶段

### A. 修正现有 delegation 基线

- 修复未定义 `_resolve_agent`、失败 Task 无法查询、终态 handle 回查等直接缺陷；
- 明确 LocalAgentRunner 的取消和失败传播；
- 添加真实工具调用的集成测试。

### B. Task 与消息 Broker

- 落地 TaskSpecification、TaskMessage、状态机、TaskMessageBroker；
- 限制端点权限和单活动 Task；
- 覆盖双向消息、超时和取消。

### C. 独立 Session 与完整装配

- 委托时创建 target Session；
- 使用目标 Identity、独立 Runtime/Assembler/Journal；
- 修复 Slot 缓存隔离；
- 将初始 TaskSpecification 作为 user message 注入 target。

### D. 工具闭环与观测

- 对模型提供 `delegate`、`task_send_message`、`wait_task`、`task_status`、`cancel_task`；
- 将生命周期和消息写入 Journal；
- 验证 source 在 result 后汇总并结束。

## 9. 验收标准

1. source 委托后创建独立 target Session，target 只能看到 TaskSpecification、精选材料和自身 Session 内容。
2. target 可发送 `progress` 与 `question`；source 通过 `wait_task` 收到问题后发送 `reply`。
3. target 收到 reply 后继续执行、发送 result；source 收到结果后汇总并正常结束。
4. source 和 target 只能操作自身 Task 端点，不能影响无关 Task。
5. source 创建第二个活动 Task 时被拒绝，或必须先终止前一个 Task。
6. target 失败、超时或取消时，source 收到确定终态并正常收敛。
7. Identity 的 prompt、工具白名单和 Slot 内容不跨 Session 泄漏。
8. 应用退出或崩溃后，活动 Task 不恢复，重启后不自动重新执行。

## 10. 进入下一阶段的条件

仅当同进程闭环稳定且确实需要跨重启继续任务时，再实施可靠持久化：SQLite Task/Message、通用 checkpoint/resume、Session lease 与恢复扫描。它们不应提前混入本 MVP。
