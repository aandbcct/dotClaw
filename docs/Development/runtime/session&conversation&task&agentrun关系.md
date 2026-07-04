### 四个核心概念的关系

这四个部分呈现出从“宏观容器”到“微观执行单元”的层级嵌套关系：

1. **Session（会话）**：是最高层级的容器，相当于一个“事件账本”。它记录了任务从开始到结束的所有历史（append-only 日志），负责跨请求、跨进程的状态连续与持久化。
2. **Conversation（对话/提问）**：是 Session 内部的一个逻辑分段，代表用户的一次具体提问或交互。在 Harness 的持久化机制中，一次 Conversation 的完整对话历史会被记录在 JSONL 文件中，并经过提炼后沉淀到记忆系统（Memory）中。
3. **Task（任务）**：是 Agent 对复杂问题的拆解单元。当模型进入计划模式（如 BUILD 阶段）时，会通过 `todo_write` 等工具将方案拆分为有序的任务清单。Task 保存在会话状态中，拥有独立的生命周期状态（如 PENDING、IN_PROGRESS、COMPLETED）。
4. **AgentRun（原子调用）**：是执行层面的最小原子单元。它指代 Agent 未被结束、未被打断的一次底层推理与工具调用过程。当遇到用户输入、子 Agent 调用或外部中断时，当前的 AgentRun 会暂停或结束，其状态会被快照保存，以便后续恢复。

**总结关系**：一个 **Session** 包含多次 **Conversation**；在一次 Conversation 的推理过程中，Agent 可能会拆解出多个 **Task**；而驱动这些 Task 推进的底层执行动作，则是由一个个 **AgentRun** 组成的。

### State 状态机如何控制各部分

Harness 的状态机控制核心在于“解耦”与“持久化”，它将状态（State）与计算（Compute）分离，确保系统具备高可用性和可恢复性。

#### 1. 控制 Session 与 AgentRun：双路径持久化与快照恢复

状态机通过两条并行的路径来维护 Session 和 AgentRun 的连续性：

- **状态快照（Context）**：每次 AgentRun（即 `call()`）结束后，框架会将当前的运行状态（对话记忆、工具执行上下文等）序列化为 JSON 文件存入 `context/` 目录。下次使用相同 sessionId 发起调用时，状态机会自动加载这份快照，恢复到上次结束的位置，实现“关掉再打开仍然记得上次”。
- **事件流（Event Log）**：Session 被设计为结构化、可追溯的只追加（append-only）事件总线。每一次工具调用、模型生成或中断都被记录为带时间戳的标准化事件。如果 AgentRun 在执行中崩溃，调度层（Orchestration）可以通过读取事件日志重建现场，并触发新的 Sandbox 继续执行。

#### 2. 控制 Task：独立的状态枚举与上下文隔离

Task 拥有自己独立的状态机，不受底层 AgentRun 频繁启停的直接影响：

- **状态流转**：Task 的状态通过枚举严格管理，通常包括 `PENDING`（待处理）、`IN_PROGRESS`（进行中）和 `COMPLETED`（已完成）。
- **状态存储**：Task 的状态并不依赖内存，而是保存在会话的 `AgentState → TaskContextState` 中。每次会话调用结束，AgentState 会自动持久化，确保即使进程重启，任务进度也不会丢失。

#### 3. 控制 Conversation：记忆提炼与上下文压缩

状态机通过记忆管理系统来控制 Conversation 的生命周期，防止上下文无限膨胀：

- **双层分离**：Conversation 结束后，状态机会触发 LLM 提炼“新增事实”追加到每日流水账（保证不丢）；后台调度器再周期性地将流水账合并、精炼为长期记忆 `MEMORY.md`（保证可用）。
- **自动压缩**：当 Conversation 的消息数或 Token 数超过阈值，状态机会捕获异常或触发阈值，强制将旧对话压缩成摘要并卸载到 JSONL 文件，仅保留最近的消息供模型“看到”，从而维持系统的高效运转。

### 多agent中任务流转

在 Harness 框架的工程化设计中，当主 Agent 需要将一个 Task 移交给另一个 Agent 时，确实会涉及到跨 Session 的交互。这种机制通常通过以下两种主流架构模式来解释和实现：

#### 1. 子 Agent 模式（Subagents / 中央协调）

在这种模式下，Task 的移交并不是传统意义上的“跨会话独立运行”，而是**“主会话内的临时委派”**。

- **执行逻辑**：主 Agent 遇到耗时长、上下文重或可并行的子任务时，会将其委派给子 Agent。子 Agent 虽然是一个独立的 Agent 实例，拥有自己的 System Prompt 和 Memory，但它**不共享主 Agent 的对话历史**。
- **状态流转**：子 Agent 执行完毕后，会将结果作为一条“工具结果（Tool Result）”返回给主 Agent。对主 Agent 而言，这就像调用了一个复杂的工具一样，Task 的状态依然保留在主 Session 中。
- **适用场景**：适合多域任务需要并行执行，且不需要子 Agent 直接与用户交互的场景。

#### 2. 状态驱动移交模式（Handoffs / 状态机转交）

如果你所说的“移交”是指控制权彻底转移给另一个 Agent，且需要保持多轮对话的连贯性，这属于 **Handoffs 模式**。

- **执行逻辑**：活跃的 Agent 根据对话上下文动态切换，通过工具调用触发状态转换，决定下一个接管的 Agent。
- **跨 Session 的上下文同步**：在这种模式下，不同的 Agent **并不共享同一个会话实例**（因为不同的 Agent 类型可能有不同的抽象实现）。为了实现跨 Session 的上下文一致性，框架会采用**广播机制**：每当一个 Agent 生成响应或处理用户输入时，它会向工作流中的所有参与者广播其响应，确保所有 Agent 在下一轮接管时都能获取到最新的完整对话历史。
- **适用场景**：适合需要按阶段收集信息的客服流程、多阶段对话体验等。

#### 3. 工程化保障：状态与计算的分离

无论是哪种模式，Harness 框架在底层都通过**状态机（State Machine）**和**持久化存储**来确保跨 Task、跨 Session 的可靠性：

- **状态持久化**：框架将状态（State）与计算（Compute）分离。每次 AgentRun 结束后，运行状态（包括 Task 进度、对话记忆等）都会被序列化为快照并持久化（如存入 Redis 或本地文件系统）。
- **跨节点恢复**：当 Task 从一个 Agent 移交给另一个 Agent，或者服务发生滚动发布、扩缩容时，只要传入相同的 `sessionId`，新的 Agent 实例就能自动从持久化存储中加载状态快照，恢复工作现场，确保“对话不会断”。

**总结来说**：跨 Task 跨 Session 的本质，是框架通过“工具结果回传”（Subagents）或“上下文广播与状态持久化”（Handoffs）这两种机制，打破了单一会话的物理边界，实现了多智能体之间的无缝协作与状态流转。

### Agent 什么时候拆解出 Task？

Agent 拆解 Task 通常发生在进入**“计划模式（Plan Mode）”**或面对复杂长流程时。
当主 Agent 接收到一个耗时长、上下文重或可并行的复杂目标时，它不会直接盲目执行，而是先进行规划。在这个阶段，Agent 会通过特定的工具（如 `todo_write`）将方案拆解为有序的任务清单。这些 Task 拥有独立的生命周期状态（如 `PENDING`、`IN_PROGRESS`、`COMPLETED`），并被持久化在会话状态中，以便分步推进。

### AgentRun 是关联 Conversation 还是 Task？

**AgentRun 在物理上关联 Conversation，在逻辑上服务于 Task。**

- **物理层面**：AgentRun 是执行层面的最小原子单元，它由一次用户的输入（Conversation）触发。
- **逻辑层面**：在一次 AgentRun 内部，Agent 会根据当前的 Task 状态机来决定下一步动作。AgentRun 的流转是由 `AgentState` 根据操作节点执行后的 Phase 和 Action 来控制的。

### TaskState 和 AgentState 的关系是什么？

**AgentState 是宏观的容器，TaskState 是其中的核心子集。**

- `AgentState` 是框架内部的运行时对象，它包含了当前会话的所有状态信息。
- `TaskState` 是 `AgentState` 的一部分。在代码层面，你可以通过 `agent.getDelegate().getAgentState(userId, sessionId).getTasksContext().getTasks()` 来访问当前会话的任务清单及其状态。
- 每次会话调用结束，`AgentState`（包含 TaskState）会自动持久化，确保任务进度不会丢失。

### AgentState 如何既属于 Session，又控制 AgentRun？如果开了新的 AgentRun 怎么办？

这是 Harness 架构最核心的设计哲学：**状态外化（State Externalization）与无状态执行器（Stateless Executor）**。

#### 1. 状态属于 Session，而非 AgentRun

Harness 将 LLM 严格视为一个“无状态的计算单元（CPU）”，而将所有状态存储在 Harness 控制的外部上下文中。

- **Session 是事实来源（Source of Truth）**：`AgentState` 是绑定在 `Session` 上的。每次 `call()` 结束后，框架会将 `AgentState` 序列化为 JSON 快照，存入工作区的 `context/sessionId/` 目录下。
- **AgentRun 是无状态的**：Harness 执行器本身不持有任何 Session 数据。它的唯一职责就是接收执行请求，注入上下文，等待输出，并将结果写入 Session Log。

#### 2. 开启新 AgentRun 时的控制流转

当你开启一个新的 AgentRun（例如用户发起了新的提问，或者上一次执行被中断后恢复）时，状态控制流转如下：

- **自动恢复（Restore）**：新的 AgentRun 启动前，Harness 会根据传入的 `sessionId`，自动从持久化存储（如本地文件或 Redis）中加载上一次的 `AgentState` 快照。
- **上下文注入（Inject）**：Harness 会将恢复的 `AgentState`（包含历史对话、当前 Task 进度、长期记忆等）作为上下文注入到 System Prompt 中。
- **接管流转（Control）**：模型基于注入的上下文进行推理。`AgentState` 会根据模型返回的 Action 和当前 Phase，继续驱动新 AgentRun 内的操作节点流转。即使进程重启或节点漂移，只要 `sessionId` 一致，新的 AgentRun 就能无缝接续之前的工作现场。

**总结来说**：Harness 通过将状态从内存中剥离并持久化到 Session 级别，实现了“计算与状态的彻底解耦”。AgentRun 只是一个个短暂的执行切片，而真正的“大脑记忆”和“任务进度”始终安稳地躺在 Session 的状态机里，随时准备被下一个 AgentRun 唤醒并接管。

### 为什么agentstate包含历史对话和长期记忆？我现在是state管状态，session管上下文（conversation）和其他配置（agentid、model、），session实例不持有state实例。我需要改吗？需要怎么改？

你提到：“State 管状态，Session 管上下文（conversation）和其他配置（agentid、model），Session 实例不持有 State 实例”。这完美契合了现代 Harness 的**“无状态执行器（Stateless Executor）”**哲学。

1. **职责单一**：Session 负责承载多轮对话的上下文（Conversation）和基础配置；State 负责承载业务流转的进度。两者独立更新，互不干扰。
2. **避免上下文污染**：如果把 State（如复杂的 Task 树结构）直接塞进 Session 的对话上下文中，模型在推理时会被大量无关的 JSON 结构干扰，导致幻觉或注意力分散。
3. **独立持久化**：Session 的事件流可以存在 S3 或日志系统中，而 State 可以存在 Redis 或本地文件中。当节点崩溃时，只要通过 `sessionId` 就能同时找回对话和状态。