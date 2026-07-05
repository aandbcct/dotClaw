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

### Trace 除了“链路可视化”，还有什么核心作用？

在长任务 Agent 的生产环境中，Trace 本质上是系统的**“黑匣子”和“行车记录仪”**，它主要承担以下三大工程级作用：

1. **确定性状态恢复（Crash Recovery）**：
   长任务 Agent 极易遇到中断（如 Sandbox 崩溃、API 超时）。Trace 记录了每一步的输入、输出和错误码。当进程崩溃时，Harness 不需要从头重跑，而是通过读取 Trace 日志，精确地从最后一步（例如第 199 步）继续执行（Wake/Resume 机制）。
2. **合规审计与故障诊断（Audit & Debug）**：
   当 Agent 出现“幻觉”或输出错误时，由于上下文窗口可能已经压缩或截断了早期信息，开发者无法知道“模型当时看到了什么”。Trace 提供了不可篡改的因果链，允许开发者通过 SQL 或工具精确回溯：“在第 37 步调用工具时，返回了什么结果导致了后续的误判”。在金融、医疗等强监管行业，这是满足合规审查的硬性要求。
3. **成本核算与性能调优（Observability）**：
   Trace 记录了每次模型调用的 Token 消耗、工具调用的耗时等细粒度指标。这不仅用于计算单次任务的总成本，还能帮助系统识别性能瓶颈（例如某个工具调用耗时过长导致超时），从而触发熔断或重试策略。

### Trace 应该以哪个层级为界做储存？

**结论：必须以 Session 为界，采用“Session as Event Log（会话即事件日志）”的架构进行存储。**

1. **为什么不能以单次 AgentRun 为界？**
   如果以单次 AgentRun 存储，数据是碎片化的。当任务跨越多次 Run（例如用户中途打断，或者等待外部审批后恢复）时，你无法还原完整的任务因果链。
2. **为什么不能以整个 Conversation 为界？**
   上下文窗口（Conversation）是模型当前的“工作台”，它会被压缩、裁剪甚至卸载。如果 Trace 依赖 Conversation 存储，关键的历史证据就会随着上下文的压缩而“蒸发”。
3. **Session 作为边界的优势**：
   Session 是一个结构化的、带时间戳、只追加（append-only）的事件流总线。无论是用户输入、模型思考、工具调用（Tool Call）、工具返回（Tool Result），还是 Guardrail 拦截，都被序列化为标准化的独立事件。这种设计将“可恢复的历史”与“模型当前工作台”彻底拆开，保证了长任务越跑越稳。

### Trace 的持久化：交给专门的 Journal 模块，还是让 Session 负责？

**结论：强烈建议交给专门的 Journal 日志模块（或独立的 Session Store），而不是让 Session 的内存实例来负责。**

这正是 Harness 框架从“宠物（Pet）”向“牲畜（Cattle）”演进的核心设计：

1. **Harness 和 Session 实例必须是无状态的（Stateless）**：
   生产级 Harness 的执行器（Harness Executor）本身不持有任何 Trace 或 Session 数据。它的唯一职责是接收请求、调用模型、执行工具，然后将产生的事件（Event）写入外部存储。如果由 Session 实例负责持久化，一旦实例所在的容器崩溃或重启，Trace 就会丢失，导致整个任务不可恢复。
2. **独立 Journal 模块的工程收益**：
   将 Trace 持久化剥离到专门的 Journal 模块（如基于 RocksDB、Kafka Commit Log 或 Amazon QLDB 的定制引擎），可以实现**故障隔离**。即使 Harness 进程频繁重启、滚动更新，只要 Event Log 的 Schema 不变，任务就能无缝接续。
3. **读写分离与按需加载**：
   专门的 Journal 模块支持高效的查询（如 `getEvents(session_id)`）。Harness 在每次开启新的 AgentRun 时，不需要把整个 Trace 塞进内存，而是通过 Journal 模块按需拉取最近 N 条事件，动态组装成当前模型需要的上下文（Context Builder）。

**总结你的架构演进方向**：
你现在的思路（State 管状态，Session 管上下文）已经非常先进。下一步的优化是：**把 Session 彻底抽象为一个“事件账本（Event Log）”的接口**。Harness 只负责往这个账本里追加事件，并从账本里读取事件来组装 Prompt。所有的持久化工作，都下沉到独立的 Journal/Store 层去处理。这样你的 Agent 架构就真正具备了生产级的韧性。

### 存储粒度：按 Session 还是按 Conversation？

**结论：强烈建议以整个 Session 为一个文件（或一个连续的日志流），而不是按单次提问（Conversation）拆分。**

**为什么不能按单次提问（Conversation）拆分？**

1. **因果链断裂**：Agent 的决策往往是跨轮次的。比如用户在第 1 次提问时设定了全局约束，在第 3 次提问时触发了工具调用报错。如果按提问拆分文件，排查问题时需要手动拼接多个文件，极其痛苦。
2. **状态恢复的连续性**：正如你所说，任务恢复依赖状态机。状态机需要知道“上一步到底发生了什么”才能决定“下一步怎么走”。如果 Trace 被切碎，恢复时的链路是不完整的。

**为什么按整个 Session 存储更好？**
在业界最佳实践中（例如 Claude Code 等框架），Session 被设计为一个**追加优先（Append-only）的 JSONL 文件**。

- 每一条消息、每一次工具调用、每一个压缩边界标记，都按时间顺序逐行追加到文件末尾。
- 这个文件就是整个 Session 的“事实来源（Source of Truth）”。
- 当需要恢复会话时，系统只需读取这个文件，重放（Replay）记录来重建完整的对话状态即可。

### 关于 `agentrun_id` 与状态机恢复的机制

你提到：“任务恢复的话是读状态机状态，会先按 agentrun 恢复，然后我再筛选 agentrun_id 取相应的 trace”。

这个思路在**逻辑上是完全正确的**，但在**工程实现上需要微调**。我们不需要在物理上把 Trace 按 `agentrun_id` 拆分成不同的文件，而是通过**元数据（Metadata）和 ID 映射**来实现精准筛选。

**正确的工程实现路径如下：**

1. **Session 级持久化，Run 级标记**：
   所有的 Trace 依然写入同一个 Session 文件中。但是，写入的每一条 Trace 记录（Span/Event）都必须带上 `session_id` 和 `agentrun_id` 的标签（Tags）。
2. **状态机恢复（State Recovery）**：
   当发生中断需要恢复时，Harness 首先读取的是**状态快照（State Snapshot）**。状态快照里记录了上一次执行到的 `agentrun_id` 以及任务进度（TaskState）。
3. **Trace 按需检索（Trace Retrieval）**：
   状态机恢复后，如果 Harness 需要回溯上一次 `agentrun` 的具体执行细节（比如为了诊断为什么失败），它不需要遍历整个 Session 文件，而是通过查询引擎（或解析 JSONL）利用 `agentrun_id` 作为过滤条件，精准提取出属于那一次 Run 的 Trace 链路。

### 如何设计你的存储架构？

结合你之前的架构（State 管状态，Session 管上下文），你的持久化层可以这样设计：

- **State Store（状态存储）**：存储结构化的状态机快照（JSON）。恢复时，直接读取最新的 State 快照，获取当前的 `agentrun_id` 和任务进度。
- **Session Journal（会话日志存储）**：存储整个 Session 的 Trace 流水账（JSONL）。所有的 Trace 都带 `agentrun_id` 标签。
- 恢复逻辑：
  1. 加载 State -> 知道上次执行到哪了。
  2. 开启新的 AgentRun -> 生成新的 `agentrun_id`。
  3. 组装上下文 -> 从 Session Journal 中按需读取最近的对话和 Trace 摘要。
  4. 执行并追加 -> 将新的 Trace 带上新的 `agentrun_id`，追加写入 Session Journal。

### Trace 里储存的内容应该包括什么？有哪些字段？

Trace 的核心目的是“事后复盘”和“系统级恢复”。一个标准的 Agent Trace（通常是一个 JSONL 文件中的一行）建议包含以下核心字段：

- 基础元数据（Metadata）：
  - `trace_id` / `span_id`：全局唯一追踪 ID。
  - `session_id`：所属的会话。
  - `agentrun_id`：所属的原子调用批次（用于关联）。
  - `timestamp`：事件发生的时间戳。
- 事件类型（Event Type）：
  - 例如：`USER_INPUT`（用户输入）、`LLM_REQUEST`（发给模型的请求）、`LLM_RESPONSE`（模型返回）、`TOOL_CALL`（工具调用）、`TOOL_RESULT`（工具结果）、`STATE_CHANGE`（状态机变更）。
- 事件载荷（Payload）：
  - 对于 `LLM_REQUEST`：记录完整的 Prompt（包括 System Prompt、历史对话、Tools 列表）。
  - 对于 `TOOL_CALL`：记录工具名称、入参。
  - 对于 `TOOL_RESULT`：记录工具返回的内容、是否报错。
- 资源消耗（Metrics）：
  - `token_in` / `token_out`：用于成本核算。
  - `latency_ms`：耗时，用于性能分析。

### 既然恢复用 Trace，那就完全不需要 AgentRun 了吗？

**这是一个非常关键的误区！Trace 和 AgentRun 的职责完全不同，绝对不能互相替代。**

- **Trace 是“黑匣子（行车记录仪）”**：它记录的是客观发生的物理事实（谁在什么时间调用了什么，耗时多少）。它主要用于**事后排查**和**成本核算**。
- **AgentRun 是“执行上下文（工作台）”**：它是系统为了维持一次连贯执行而存在的**内存结构**。

**为什么不能只用 Trace 来恢复？**
因为 Trace 是一堆散乱的日志。如果发生崩溃，系统不可能去遍历几万行的 Trace 日志来“推导”出现在的状态。系统必须读取一个**高度浓缩的结构化状态（State Snapshot）**。
而 **AgentRun 正是承载这个 State Snapshot 的最佳容器**。当发生中断时，系统读取上一次 AgentRun 保存的 State，就知道下一步该干嘛了。

**你现在的 AgentRun 字段需要改吗？**
你提到：“现在的 agentrun 是存储的单次原子调用中的所有流转消息和部分元数据(工具调用次数、token_in、token_out等)”。
**建议进行以下微调（瘦身与职责分离）：**

1. **剥离冗余消息**：AgentRun 里**不要**存储“所有的流转消息（Messages）”。流转消息应该存在 Session Journal（Trace）里。AgentRun 里只需要存一个指针，或者只存最新的 State 快照。
2. **保留并强化状态**：AgentRun 的核心字段应该是 `current_state`（当前状态机快照）、`task_progress`（任务进度）、`pending_actions`（待处理动作）。
3. **保留统计元数据**：`tool_call_count`、`token_in`、`token_out` 这些字段保留在 AgentRun 里是非常好的设计，这相当于给这一次“原子调用”打了一个“性能与成本标签”，方便后续做单次调用的聚合分析。

**总结你的架构演进**：

- **Session Journal (Trace)**：记录所有的流转消息、工具调用细节、Token 消耗（Append-only 日志）。
- **AgentRun**：记录单次调用的结构化状态（State）、任务进度、聚合的元数据（Token 总数、工具调用次数）。
- **恢复机制**：读取 AgentRun 里的 State 恢复进度，读取 Session Journal 里的最近几条消息恢复对话记忆。两者配合，才是完美的 Harness 架构。

### 关于“流转消息”存哪里？（AgentRun vs Session Journal）

**结论：流转消息（Messages）应该存在 Session Journal（Trace）中，而不是 AgentRun 中。**

- **为什么不建议存在 AgentRun 中？**
  正如我们之前讨论的，AgentRun 代表的是“单次原子调用”。如果用户发起一个复杂任务，系统可能因为 API 超时、用户中途打断等原因，触发了 5 次 AgentRun。如果你把流转消息都存在 AgentRun 里，那么这 5 个 AgentRun 就会各自存一份消息列表。当你想要排查整个任务的上下文时，你需要去拼接这 5 份数据，这在工程上是非常痛苦的。
- **Session Journal 才是“消息账本”**：
  Session Journal（也就是你所说的 Trace 的底层）是一个只追加（Append-only）的日志流。每一次 LLM 的请求、工具的调用、工具的返回，都作为一条 Event 追加在这里。当你需要查中间消息时，你只需要通过 `session_id` 去这个 Journal 里按时间顺序读取即可。

### 目前的 Trace 只存事件，需要改成存消息吗？

**结论：是的，强烈建议将消息（Messages）作为事件（Event）的一种类型存入 Trace 中。**

在业界标准的 Agent Trace 设计中，Trace 不仅仅是调用链路，它是**“完整执行轨迹的归因基础”**。一次发给 LLM 的请求本质上是一个巨大的 JSON（包含系统指令、工具描述、历史消息等）。随着步骤增加，这个 JSON 会越来越大。因此，你需要把整个 Session 过程中的关键 JSON 存下来，因为它们反映了每一步执行的不同结果。

**建议的 Trace Event 类型扩展：**

- `LLM_REQUEST`：记录发给模型的完整 Payload（包含 Messages）。
- `LLM_RESPONSE`：记录模型返回的完整内容。
- `TOOL_CALL`：记录工具入参。
- `TOOL_RESULT`：记录工具返回结果。
- `STATE_CHANGE`：记录状态机的变更。

这样，Trace 既包含了“事件（发生了什么）”，也包含了“消息（具体说了什么）”，形成了一个完整的闭环。

### 状态机与 Session 关联，为什么 AgentRun 还要存 State？

这是一个非常核心的概念误区。我们需要区分**“状态机（State Machine）”**和**“状态快照（State Snapshot）”**：

- **状态机（State Machine）**：
  它是**逻辑层面**的规则和流转图。它定义了“从 Planning 到 Executing 需要满足什么条件”、“遇到 Error 应该回退到哪个节点”。状态机是代码里的类（Class）或枚举，它确实属于 Session 级别，因为它指导着整个会话的走向。
- **状态快照（State Snapshot）**：
  它是**物理层面**的内存数据。它是状态机在某一时刻的“具体数值”。比如：`current_phase = "Executing"`, `completed_tasks = ["task_1", "task_2"]`, `current_task = "task_3"`。

**为什么 AgentRun 需要存状态快照（State）？**
因为 AgentRun 是**执行的最小原子单元**。当一次 AgentRun 结束时（无论是正常结束还是异常中断），系统必须把当前的“进度”保存下来。
如果不存，下一次开启新的 AgentRun 时，系统怎么知道当前执行到了哪一步？它难道要重新去遍历一遍 Trace 里的消息来“推导”出现在的进度吗？这显然不现实。

**总结这三者的关系：**

- **Session** 拥有一个**状态机（State Machine）**，它规定了整个会话该怎么走。
- **AgentRun** 是状态机的一次“心跳（Step）”。每次心跳结束，AgentRun 会把当前的**状态快照（State Snapshot）**持久化。
- **Session Journal (Trace)** 像行车记录仪一样，默默记录下这次心跳过程中的所有**流转消息（Messages）**和**事件（Events）**。

### agentrun的新建触发条件是什么？

你目前的触发条件（用户输入、子 Agent 调用）是**最基础的业务触发**，但在生产级 Harness 架构中，AgentRun 的触发条件远不止于此。

针对你的问题，我分两部分来解答：

#### 一、 API 超时、用户中途打断会触发新 AgentRun 吗？

**结论：是的，它们都会触发新的 AgentRun。**

- **API 超时（系统级中断）**：当 LLM 或工具调用超时，当前的 AgentRun 会被强制终止（状态标记为 `FAILED` 或 `TIMEOUT`）。如果 Harness 配置了自动重试（Retry）机制，系统会在短暂等待后，自动创建一个**新的 AgentRun** 来接续执行。
- **用户中途打断（用户级中断）**：当用户在 Agent 正在执行时发送了新消息，或者点击了“停止（Abort）”按钮，当前的 AgentRun 会被取消。用户的这次新消息，必然会触发一个新的 AgentRun。

**核心逻辑**：只要当前的 AgentRun 停止了（无论是因为正常完成、异常崩溃、超时，还是被外部打断），下一次任何需要模型继续思考的动作，都会开启一个**全新**的 AgentRun。

------

#### 二、 AgentRun 的新建触发条件全景图

在生产级 Agent 架构（如 Eino ADK 的 TurnLoop 或 Anthropic 的 Managed Agents）中，AgentRun 的新建触发条件通常包含以下几类：

##### 1. 业务级触发（你目前已有的）

- **用户输入（User Message）**：用户发起新的提问或提供补充信息。
- **子 Agent 结果回传（Subagent Result）**：子 Agent 完成任务，将结果作为工具返回值推给主 Agent，触发主 Agent 的新 AgentRun 继续推理。

##### 2. 系统级/异常恢复触发（你需要补充的）

- **断点续跑（Resume / Wake）**：Agent 在执行中遇到需要人工审批（Human-in-the-loop）的节点，主动挂起（Interrupt）。当用户审批通过后，系统会读取之前保存的 State Snapshot，触发一个新的 AgentRun 从断点处继续执行。
- **自动重试（Auto-Retry）**：遇到 API 限流（Rate Limit）、网络抖动、超时等可恢复错误，Harness 调度器自动触发新的 AgentRun。

##### 3. 内部循环触发（Internal Loop / 自动续跑）

这是 Agent 区别于普通聊天机器人的核心特征。很多时候，Agent 的推理不是一问一答的，而是**多步自循环**的。

- **工具调用后的自动续跑**：模型决定调用工具，Harness 执行完工具后，必须立刻触发一个新的 AgentRun，把工具结果喂给模型，让模型继续思考。
- **防“懒惰模型”的自动续跑（Auto-Continuation）**：有时候模型在没有任何工具调用的情况下突然停止输出（比如只输出了一半），但系统判断任务还没完成。此时 Harness 会自动注入一条系统提示（如 `[goal_check] Continue working...`），触发一个新的 AgentRun 让模型继续干活。

