# dotClaw 开发者 Wiki

> 本 Wiki 面向参与 dotClaw 开发、扩展和排障的开发者。  
> 阅读顺序遵循“项目地图 → 核心链路 → 模块 → 逻辑组件 → 核心类 → 修改入口”，不要求读者先理解源码目录。

> 审计基准：`2426220`（2026-08-05）
> 当前已完成 13 篇核心模块 Wiki；Channel 待补充，Journal 需先完成与 RunEvent 的边界审计，Scheduler 当前代码与配置存在但未进入 ApplicationHost。

## 1. 项目代码地图

dotClaw 是一个本地 Agent Harness。外部输入首先进入交互和应用服务，随后由 Runtime 驱动一次独立的 AgentRun；Context、LLM、Tool 和 Delegation 作为执行能力接入，Memory、Skills、MCP 等模块位于这些能力之后。`ApplicationHost` 负责装配和生命周期，不参与正常请求处理；Journal 属于侧向观测，不是运行恢复事实源。

```mermaid
flowchart TB
    Bootstrap["Bootstrap / Composition<br/>ApplicationHost<br/>对象装配与生命周期"]

    subgraph Entry["交互与应用服务"]
        Channel["Channel<br/>输入、审批交互与 reasoning/response 输出"]
        Interaction["SessionInteractionService<br/>Session 路由与应用用例"]
        Session["Session<br/>成功对话语义与会话元数据"]
        Agent["Agent<br/>Identity 与运行约束"]
    end

    subgraph RuntimeCore["执行内核"]
        Coordinator["SessionRunCoordinator<br/>同 Session 串行"]
        Engine["RuntimeEngine<br/>一次 Run 的执行协调"]
        Execution["RunExecution<br/>单 Run 内存上下文"]
        State["AgentRunState<br/>transition() / AgentRunEvent / AgentAction"]
        RunFacts["Run Repository<br/>消息、事件、快照与恢复事实"]
    end

    subgraph Evaluation["追踪与评测控制面"]
        Trace["Trace<br/>Run 事实的只读重建与显式导出"]
        Eval["Eval<br/>Fixture、评分、Dataset 与回归 Gate"]
    end

    subgraph Capabilities["Agent 能力"]
        Context["Context<br/>Slot Plan 与 ContextBundle"]
        LLM["LLM<br/>路由、限流、熔断与调用"]
        Tool["Tool<br/>注册、安全决策与执行"]
        Delegation["Delegation<br/>子 Agent 委派端口"]
    end

    subgraph Sources["能力来源与扩展"]
        Memory["Memory<br/>同步、检索与蒸馏"]
        Skills["Skills<br/>扫描、注册与上下文暴露"]
        MCP["MCP<br/>连接、发现与 Tool 适配"]
        Providers["Fixed Providers<br/>受控 HTTP 外部服务"]
        Orchestration["Orchestration<br/>Task、Broker 与子 Run 映射"]
    end

    subgraph Support["支撑设施"]
        Config["Config<br/>配置模型与加载"]
        Journal["Journal<br/>代码与配置存在，未进入 Runtime 主链"]
        Scheduler["Scheduler<br/>代码与配置存在，当前未装配"]
    end

    Channel --> Interaction
    Interaction --> Coordinator
    Interaction --> Session
    Interaction --> Agent

    Coordinator --> Engine
    Engine --> Execution
    Execution <--> State
    Engine --> RunFacts
    RunFacts -.只读事实.-> Trace
    Trace --> Eval

    Engine --> Context
    Engine --> LLM
    Engine --> Tool
    Engine --> Delegation

    Context --> Memory
    Context --> Skills
    Context --> Agent
    Context --> Session

    Tool --> MCP
    Tool --> Providers
    Delegation --> Orchestration

    Bootstrap -.装配.-> Channel
    Bootstrap -.装配.-> Interaction
    Bootstrap -.装配.-> Coordinator
    Bootstrap -.装配.-> Engine
    Bootstrap -.装配.-> Context
    Bootstrap -.装配.-> LLM
    Bootstrap -.装配.-> Tool
    Bootstrap -.装配.-> MCP
    Bootstrap -.装配按需入口.-> Eval

    Config -.提供配置.-> Bootstrap
```

从这张图应当先建立四个判断：

1. `RuntimeEngine` 是一次运行的执行协调器，不是整个应用的组合根。
2. `SessionRunCoordinator` 处理多个 Run 之间的 Session 占用；`AgentRunState` 是单 Run 唯一的持久化控制状态，`transition()` 根据 `AgentRunEvent` 产出下一状态和 `AgentAction`。
3. Memory、Skills 和 Agent Directory 主要通过 Context 进入模型输入；MCP 主要通过 Tool 进入工具注册表。
4. Bootstrap 与 Config 是装配和配置支撑；Journal、Scheduler 当前没有进入 ApplicationHost 主链。
5. Trace / Eval 是后置控制面：Trace 不回写 Runtime，Eval 不进入普通请求主链；它们由 Channel、CLI 或 CI 显式触发。

---

## 2. 系统核心链路

### 2.1 一次普通用户请求

```mermaid
sequenceDiagram
    actor User as 用户
    participant Channel as Channel
    participant App as SessionInteractionService
    participant Coord as SessionRunCoordinator
    participant Engine as RuntimeEngine
    participant Context as ContextPort
    participant LLM as LLMPort
    participant Tool as ToolPort
    participant Repo as RunRepository
    participant Session as Session/Conversation

    User->>Channel: 输入消息
    Channel->>App: submit(session, message)
    App->>App: 校验 session.agent_id
    App->>Coord: submit_prepared(session_id)
    Coord->>Coord: 获取 Session 租约
    Coord->>Engine: execute(RunRequest)
    Engine->>Repo: 创建 AgentRun 与输入事实
    Engine->>Context: build(request, execution)
    Context-->>Engine: ContextBundle
    Engine->>LLM: complete(messages, tools)

    alt 模型返回最终回答
        LLM-->>Engine: final response
    else 模型返回工具调用
        LLM-->>Engine: tool calls
        Engine->>Tool: execute(tool invocation)
        Tool-->>Engine: completed / approval required / failed
        Engine->>Context: 构造下一轮输入
        Engine->>LLM: 继续调用
    end

    Engine->>Repo: 写入终态与成功提交事实
    Repo->>Session: 仅成功后投影 Conversation
    Engine-->>Coord: RunResult
    Coord-->>App: RunResult
    App-->>Channel: 结构化结果
    Channel-->>User: 流式文本或最终状态
```

### 2.2 审批、取消和委派分支

普通消息总是创建新的 Run。已有 Run 的结构化控制包括审批恢复、delegation 恢复、`resume_run()`、`abandon_run()` 和 `cancel()`；CLI 的 `/retry` 只是 `resume_run()` 的命令名。

```mermaid
flowchart LR
    ToolCall["工具调用"] --> Decision{"Tool 安全决策"}
    Decision -->|allow| Execute["执行 ToolHandler"]
    Decision -->|ask| Approval["保存审批记录与 Checkpoint"]
    Decision -->|deny| Fail["返回拒绝结果"]

    Approval --> Wait["Run 进入 Suspended(APPROVAL)"]
    Wait --> Resolve["resolve_approval(approval_id)"]
    Resolve -->|通过| Resume["在原 run_id 恢复"]
    Resolve -->|拒绝| Cancelled["收口为 CANCELLED"]

    Execute --> DelegationCheck{"是否为委派"}
    DelegationCheck -->|否| NextLLM["结果进入下一轮 LLM"]
    DelegationCheck -->|是| Child["创建目标 Session 与子 Run"]
    Child --> Suspend["父 Run 进入 Suspended(DELEGATION)"]
    Suspend --> Inspect["外部检查子 Run"]
    Inspect --> ResumeDelegation["resume_delegation(child_run_id)"]
    ResumeDelegation --> ChildResult["写入 DELEGATION_RESULT"]
    ChildResult --> NextLLM
```

完整状态迁移、Checkpoint 和恢复不变量由 [Runtime 模块](./Runtime%20模块总体说明.md) 维护；工具为何产生审批由 [Tool 模块](./Tool%20模块总体说明.md) 维护。

---

## 3. 逻辑子系统与模块索引

这里的“子系统”用于建立阅读地图，不要求源码存在同名目录。状态列区分“代码存在”和“当前已进入生产主链”。

状态含义：

| 状态 | 含义 |
|---|---|
| 主链已装配 | ApplicationHost 创建，并被正常请求链消费 |
| 按需入口已装配 | Host 或 CLI 提供显式入口，但不属于普通请求主链 |
| 可选已装配 | 满足配置或资源条件时由 Host 创建 |
| 默认空能力 | 主链支持，但当前仓库默认没有可用资源 |
| 代码存在、未装配 | 类型或配置存在，但 Host 当前不创建 |
| 待补充 | 生产代码存在，尚未完成独立 Wiki |

### 3.1 交互与应用服务

| 模块 | 定位 | 主要入口 | 当前状态 | 详细文档 |
|---|---|---|---|---|
| Bootstrap 与应用入口 | 进程启动、对象装配、生命周期和 Session 级应用用例 | `ApplicationHost`、`SessionInteractionService` | 主链已装配 | [Bootstrap 与应用入口](./Bootstrap%20与应用入口模块总体说明.md) |
| Channel | 外部输入、审批交互和 reasoning/response 运行级输出适配 | `Channel`、`CLIChannel`、`ChannelLLMOutputAdapter` | 主链已装配；Wiki 待补充 | 待补充 |

### 3.2 身份与会话

| 模块 | 定位 | 主要入口 | 当前状态 | 详细文档 |
|---|---|---|---|---|
| Agent 与 Identity | 声明 Agent Identity、行为、模型、权限和 Context 计划 | `AgentIdentity`、`AgentRegistry` | 主链已装配 | [Agent 与 Identity](./Agent%20与%20Identity%20模块总体说明.md) |
| Session | 保存成功对话语义、历史压缩和会话元数据 | `Session`、`Conversation`、`SessionManager` | 主链已装配 | [Session](./Session%20模块总体说明.md) |

### 3.3 执行内核

| 模块 | 定位 | 主要入口 | 当前状态 | 详细文档 |
|---|---|---|---|---|
| Runtime | 驱动 AgentRun 状态、外部能力调用、恢复和可靠提交 | `SessionRunCoordinator`、`RuntimeEngine`、`RunExecution`、`AgentRunState`、`transition()` | 主链已装配 | [Runtime](./Runtime%20模块总体说明.md) |

### 3.4 追踪、评测与回归

| 模块 | 定位 | 主要入口 | 当前状态 | 详细文档 |
|---|---|---|---|---|
| Eval | 从 Runtime 事实构建只读 Trace，生成 / 审核 Case，以冻结 Playback 执行确定性评分与 CI Gate | `TraceService`、`EvalCaseDraftService`、`EvalRunner`、`PlaybackRunner` | 按需入口已装配；不进入普通请求主链 | [Eval](./Eval%20模块总体说明.md) |

### 3.5 Agent 能力系统

| 模块 | 定位 | 主要入口 | 当前状态 | 详细文档 |
|---|---|---|---|---|
| Context | 按 Owner 和 Slot Plan 构造模型上下文、工具快照和动态事实引用 | `ContextProvider`、`ContextPlanResolver`、`ContextSlotManager` | 主链已装配 | [Context](./Context%20模块总体说明.md) |
| LLM | Provider 接入、候选路由、限流、熔断、重试和降级 | `LLMProxy`、`ModelRouter` | 主链已装配 | [LLM](./LLM%20模块总体说明.md) |
| Tool | 工具声明、发现、注册、安全决策、审批和统一执行 | `ToolExecutor`、`ToolRegistry`、`CapabilityBroker`、`PolicyEngine` | 主链已装配 | [Tool](./Tool%20模块总体说明.md) |
| MCP | MCP Server 生命周期、能力发现和 ToolHandler 适配 | `McpClient`、`MCPToolProvider`、`McpToolAdapter` | 可选已装配；默认无 Server | [MCP](./MCP%20模块总体说明.md) |
| Memory | 文件同步、混合检索、日记忆写入和长期蒸馏 | `MemoryManager`、`MemoryStorage`、`DeepDream` | 可选已装配；Dream 仅手动入口 | [Memory](./Memory%20模块总体说明.md) |
| Skills | SKILL.md 扫描、元数据注册和 Context 暴露 | `SkillScanner`、`SkillRegistry` | 可选已装配；默认 Registry 为空 | [Skills](./Skills%20模块总体说明.md) |

### 3.6 多 Agent 编排

| 模块 | 定位 | 主要入口 | 当前状态 | 详细文档 |
|---|---|---|---|---|
| Orchestration 与 Delegation | 保存委派 Task 事实、传递消息并将委派映射为目标子 Run | `RuntimeDelegationAdapter`、`AgentDispatcher`、`TaskMessageBroker` | 主链已装配 | [Orchestration 与 Delegation](./Orchestration%20与%20Delegation%20模块总体说明.md) |

### 3.7 支撑设施

| 模块 | 定位 | 主要入口 | 当前状态 | 详细文档 |
|---|---|---|---|---|
| Config | 加载全局配置、模型路由配置、环境变量和兼容迁移 | `Config`、`RouterConfig`、`get_config` | 主链已装配 | [Config](./Config%20模块总体说明.md) |
| Journal | Trace、Report 和 Snapshot 观测代码 | `Journal`、`AgentEvent` | 代码存在、未进入 Runtime 主链 | 边界审计后决定是否建立 Observability Wiki |
| Scheduler | 进程内一次性提醒代码 | `ReminderManager` | 代码存在、当前未装配 | 暂不建立独立 Wiki |

---

## 4. 模块依赖关系

模块依赖必须区分两种关系：

- **运行调用关系**：一次请求执行时，哪个模块调用哪个能力。
- **源码与装配依赖**：哪个模块定义契约、哪个模块实现或装配具体对象。

如果把两者混在同一张图中，会错误地认为 Runtime 内核直接依赖全部具体模块。

### 4.1 运行调用关系

```mermaid
flowchart LR
    Channel["Channel"] --> App["应用入口"]
    App --> Runtime["Runtime"]
    App --> Session["Session"]
    App --> Agent["Agent Directory"]

    Runtime --> Context["Context"]
    Runtime --> LLM["LLM"]
    Runtime --> Tool["Tool"]
    Runtime --> Delegation["Delegation"]

    Runtime -.持久化事实.-> Trace["Trace"]
    Trace --> Eval["Eval / Regression"]
    Channel -.显式 /eval.-> Eval

    Context --> Session
    Context --> Agent
    Context --> Memory["Memory"]
    Context --> Skills["Skills"]

    Tool --> MCP["MCP"]
    Tool --> Providers["Fixed Providers"]

    Delegation --> Orchestration["Orchestration"]
    Orchestration --> Agent
    Orchestration --> Session
    Orchestration --> ChildRun["目标 Session 的子 Run"]
```

关键结论：

- Runtime 只应通过稳定 Port 使用 Context、LLM、Tool 和 Delegation。
- Context 是 Memory、Skills、Agent Directory 与模型输入之间的主要汇聚点。
- MCP 是工具来源，不应在 Runtime 中建立独立调用分支。
- Orchestration 管理 Task 事实，但子 Run 仍由同一个 Runtime/Coordinator 执行。
- Trace / Eval 使用已保存的运行事实，不插入 Runtime 的普通请求执行链；CI Gate 只消费冻结 Playback 结果。

### 4.2 源码依赖与 Port 边界

```mermaid
flowchart TB
    subgraph RuntimeModule["Runtime 模块"]
        Domain["runtime.domain<br/>事实、事件、状态机"]
        Application["runtime.application<br/>执行用例与 Ports"]
        Adapters["runtime.adapters<br/>具体能力适配"]
        Application --> Domain
        Adapters --> Application
        Adapters --> Domain
    end

    Bootstrap["Bootstrap<br/>唯一组合根"] --> Application
    Bootstrap --> Adapters
    Bootstrap --> Eval["Eval Draft / Playback 入口"]

    Context["ContextProvider"] --> Application
    Orchestration["RuntimeDelegationAdapter"] --> Application

    Adapters --> Session["Session"]
    Adapters --> Agent["Agent"]
    Adapters --> LLM["LLM"]
    Adapters --> Tool["Tool"]

    Context --> Session
    Context --> Agent
    Context --> Memory["Memory"]
    Context --> Skills["Skills"]

    MCP["MCP"] --> Tool
    Memory --> LLM

    Trace["Trace"] --> Domain
    Eval --> Trace
    Eval --> Application

    Config["Config"] -.配置输入.-> Bootstrap
    Config -.配置模型.-> LLM
    Config -.配置模型.-> Tool
    Config -.配置模型.-> MCP
    Config -.配置模型.-> Memory

```

这张图中的箭头表示源码层面的主要依赖或装配关系：

1. `runtime.application` 只依赖 Domain 和自己定义的 Protocol，不应导入具体 LLM、Tool、Session 或 MCP 实现。
2. `runtime.adapters` 依赖具体模块，将它们翻译为 Runtime Port。
3. ContextProvider 和 RuntimeDelegationAdapter 实现 Runtime 定义的能力边界，因此它们可以依赖 Runtime 契约；Runtime 内核不反向依赖它们的具体类。
4. Bootstrap 可以依赖各具体模块，因为组合根的职责就是创建对象并连接依赖。
5. Config 向装配和各模块提供数据，但不应反向调用业务模块。
6. Trace 只读取 Runtime Domain Facts；Eval 可以依赖 Trace 与 Runtime Application 契约，Runtime 不反向依赖它们。

### 4.3 模块主归属规则

跨目录类型按职责确定文档主归属，而不是按文件路径机械归类：

| 类型或机制 | 完整说明主归属 | 其他模块如何处理 |
|---|---|---|
| `AgentRegistry` | Agent | Orchestration、Context 和 Bootstrap 只说明使用关系 |
| `LLMProxyAdapter`、`ToolExecutorAdapter` | Runtime | LLM、Tool 文档只保留 Runtime 接入摘要 |
| 审批产生与安全决策 | Tool | Runtime 说明审批恢复和状态收口 |
| 审批恢复与 Checkpoint | Runtime | Channel 只说明如何提交结构化决定 |
| Session 删除规则 | Bootstrap 与应用入口 | Session 只说明存储删除能力 |
| MCP 工具注册 | MCP | Tool 说明其作为 ToolProvider 的接入边界 |
| Memory/Skills 注入模型输入 | Context | Memory、Skills 说明对外提供的数据能力 |

---

## 5. 常见修改入口

| 开发目标 | 首先阅读 | 主要代码入口 | 需要同时关注 |
|---|---|---|---|
| 新增一种交互通道 | Runtime 4.7、Bootstrap 与应用入口；Channel Wiki 待补充 | `channel/base.py`、新 Channel 实现 | `main.py`、`LLMOutputPort`、`LLMOutputEvent` |
| 修改 Session 到 Agent 的路由 | [Bootstrap 与应用入口](./Bootstrap%20与应用入口模块总体说明.md) | `SessionInteractionService` | AgentRegistry、Session.agent_id |
| 修改 Run 状态或状态迁移 | [Runtime](./Runtime%20模块总体说明.md) | `runtime/domain/state.py`、`events.py`、`control.py` | Engine 驱动逻辑、Checkpoint |
| 修改一次 Run 的执行顺序 | [Runtime](./Runtime%20模块总体说明.md) | `runtime/application/engine.py` | Ports、RunExecution、持久化事实 |
| 修改同 Session 的并发规则 | [Runtime](./Runtime%20模块总体说明.md) | `session_run_coordinator.py` | 活跃 Run 查询、取消和审批恢复 |
| 新增 Context Slot | [Context](./Context%20模块总体说明.md) | `context/slots.py`、`registry.py`、默认计划 | Owner、缓存范围、刷新和快照模式 |
| 修改历史压缩 | [Runtime](./Runtime%20模块总体说明.md) + [Context](./Context%20模块总体说明.md) | `context_budget.py`、`history_compaction.py` | Session 压缩版本、成功提交 |
| 新增 LLM Provider | [LLM](./LLM%20模块总体说明.md) | `llm/providers/`、Provider 注册表 | RouterConfig、重试和错误分类 |
| 修改模型路由或降级 | [LLM](./LLM%20模块总体说明.md) | `model_router.py`、`proxy.py` | RateLimiter、CircuitBreaker |
| 新增 builtin 工具 | [Tool](./Tool%20模块总体说明.md) | `tools/builtin/`、`@tool` | Schema、Capability、Policy 和测试 |
| 新增 ToolPolicy 或资源类型 | [Tool](./Tool%20模块总体说明.md) | `decorator.py`、`capability.py`、`policy.py` | Config、Agent 级策略收窄 |
| 新增固定网络 Provider | [Tool](./Tool%20模块总体说明.md) | `tools/providers/`、`network.py` | HttpClient、服务开关和主机白名单 |
| 接入新的 MCP Server | [MCP](./MCP%20模块总体说明.md) | MCP 配置、`MCPToolProvider` | ToolRegistry、连接策略和命名空间 |
| 修改记忆同步或检索 | [Memory](./Memory%20模块总体说明.md) | `memory/manager.py`、`storage.py` | LLM embedding、Context 注入 |
| 新增或修改 Skill | [Skills](./Skills%20模块总体说明.md) | SKILL.md、`SkillScanner`、`SkillRegistry` | Context SkillsSlot、Tool SkillParser |
| 修改子 Agent 委派 | [Orchestration 与 Delegation](./Orchestration%20与%20Delegation%20模块总体说明.md) | `runtime_delegation_adapter.py`、`dispatcher.py` | Runtime DelegationPort、Session、取消传播 |
| 修改配置结构 | [Config](./Config%20模块总体说明.md) | `config/settings.py` | ApplicationHost Builder、目标模块 |
| 排查 Run 执行事实 | [Runtime](./Runtime%20模块总体说明.md) | Session 下 `agent_runs/{run_id}/` | RunMessage、RunEvent、Checkpoint |
| 新增确定性评测或修改 Fixture | [Eval](./Eval%20模块总体说明.md) | `eval/models.py`、`fixtures.py`、`scorers/` | Playback STRICT、隔离边界与 Trace 证据 |
| 修改回归 Dataset 或 CI Gate | [Eval](./Eval%20模块总体说明.md) | `draft_service.py`、`playback.py`、`gate.py` | Draft 审核、仅 Playback 可入 Gate、三态报告 |
| 排查观测输出 | [Runtime](./Runtime%20模块总体说明.md) + [Bootstrap 与应用入口](./Bootstrap%20与应用入口模块总体说明.md) | `runtime` RunEvent、`journal/` | Journal 当前未进入 Runtime 主链 |
| 评估提醒能力 | [Bootstrap 与应用入口](./Bootstrap%20与应用入口模块总体说明.md) + [Config](./Config%20模块总体说明.md) | `scheduler/reminder.py` | 当前 Host 未装配、无持久化和关闭管理 |

---

## 6. 推荐阅读路径

### 6.1 第一次理解项目

```text
项目代码地图
→ 系统核心链路
→ Runtime
→ Context
→ Tool
→ Bootstrap 与应用入口
```

这条路径先建立一次请求如何执行，再理解上下文和工具能力如何接入，最后查看系统如何装配。

### 6.2 理解可靠执行

```text
Runtime：Run / Session 边界
→ AgentRunState / transition() 状态机
→ RunExecution 与 RuntimeEngine
→ RunRepository / Checkpoint
→ 审批、恢复、取消与成功提交
```

### 6.3 理解工具安全

```text
Tool：声明与注册
→ CapabilityBroker
→ PolicyEngine
→ ApprovalManager
→ Builtin / MCP / Fixed Provider
→ Runtime ToolPort 接入
```

### 6.4 理解上下文工程

```text
Context：Owner 与 Slot
→ PlanResolver
→ SlotManager
→ ContextProvider
→ ContextVersion 与动态事实引用
→ Memory / Skills / Tools 来源
```

### 6.5 理解多 Agent 委派

```text
AgentIdentity / AgentRegistry
→ Runtime DelegationPort
→ RuntimeDelegationAdapter
→ AgentDispatcher / TaskMessageBroker
→ 目标 Session 与子 Run
→ 结果和取消传播
```

### 6.6 理解追踪、评测与回归

```text
Runtime：Run / Event / Message 权威事实
→ Eval：Trace 重建
→ EvalCase 与 Fixture
→ EvalRunner 与九类确定性 Scorer
→ Draft 审核与 Dataset
→ Playback / RegressionGate
```

这条路径区分“运行事实”“评测资产”和“CI 回归结论”：Trace 不是第二事实源，Re-execution 也不进入 Gate。

### 6.7 开发新能力

```text
常见修改入口
→ 对应模块的“组件总览”
→ “各组件的类与职责”
→ “对外契约”
→ “修改入口与不变量”
→ 源码索引
```

---

## 7. 当前架构边界

以下内容是当前实现边界，不应在文档中包装成已经完成的分布式或生产级能力：

- dotClaw 当前定位为本地、单进程 Agent Harness。
- Session 协调当前使用进程内 `asyncio.Lock`，不是跨进程或多节点租约。
- Session、Run、Checkpoint 和审批数据当前主要使用本地文件存储。
- Tool 的安全链路提供参数校验、资源解释和 allow/ask/deny 决策，但不是 OS 级强沙箱。
- MCP 只将 tools 接入 Tool Registry；resources 和 prompts 当前不作为模型工具暴露。
- Journal 代码和配置存在，但当前没有进入 ApplicationHost 或 Runtime 主链；Runtime 的恢复依据是 Run Repository 和 Checkpoint。
- Scheduler 代码和配置存在，但 ApplicationHost 当前不创建 ReminderManager，也不管理其 Task 生命周期。
- MCP 主链接入存在，但当前默认 `mcp_servers=[]`，不会连接 Server 或注册 MCP Tool。
- Skills 主链接入存在，但当前默认跳过 `_example`，Registry 为空。
- Memory 主链接入存在；DeepDream 当前只由手动入口触发，不是普通 Run 自动阶段。
- `AgentRegistry` 的物理目录与逻辑归属不完全一致；Wiki 将其完整说明归入 Agent 与 Identity。
- Runtime Adapter 的完整说明主归属 Runtime，能力模块只保留接入摘要。
- Trace / Eval 是显式控制面，不提供运行期间进度 UI、自动 Trace 持久化、LLM Judge 或真实副作用回放；Re-execution 结果仅供人工观察。
- 13 篇核心模块 Wiki 已完成；Channel 待补充，Journal 需先完成 Observability 边界审计，Scheduler 暂不建立独立 Wiki。

---

## 8. 项目级问题索引

项目级问题只在这里建立统一索引；模块 Wiki 保留本模块影响和详细根因。

| 项目级 ID | 问题 | 完整说明主归属 |
|---|---|---|
| `SYS-CFG-01` | LLMProxy 与 AgentPolicyResolver 没有共享唯一 EffectiveRouterConfig | Config、Bootstrap |
| `SYS-CFG-02` | 配置字段存在声明、解析和消费三层漂移 | Config |
| `SYS-PATH-01` | project_root 与相对路径解析权威分散 | Config、Bootstrap |
| `SYS-ID-01` | Agent Registry 与 Tool Policy Identity 加载边界分裂 | Agent 与 Identity、Bootstrap |
| `SYS-OBS-01` | Journal 与 Runtime RunEvent 的观测边界尚未收口 | Bootstrap；后续 Observability 审计 |
| `SYS-SCH-01` | Scheduler 代码和配置存在但未进入组合根 | Bootstrap、Config |
| `SYS-OUT-01` | Channel 已进入主链，但缺少独立模块 Wiki | Runtime、Bootstrap；后续 Channel Wiki |
| `SYS-DEL-01` | 委派运行态等待关系不能跨进程重启恢复 | Orchestration 与 Delegation |
| `SYS-TOOL-01` | Tool、MCP 和 disabled_tools 的装配顺序影响最终注册结果 | Config、Bootstrap、MCP |
| `SYS-STORE-01` | Session 锁和文件事务仅满足本地单进程边界 | Runtime、Session |

---

## 9. 文档维护约定

为避免 Wiki 再次变成大量信息的堆积，各模块文档统一遵循以下结构：

```text
模块定位与边界
→ 模块在项目中的位置
→ 组件总览图与职责表
→ 各组件的核心类
→ 组件依赖和使用流程
→ 对外接口与数据契约
→ 常见修改入口
→ 当前设计 / 设计取舍 / 已知痛点 / 演进方向
→ 源码索引
```

同一事实只在一个主要文档中完整维护；其他文档保留理解本模块所需的摘要，并链接到主归属章节。


### 9.1 发布前自动验收

每次修改 Wiki 至少检查：

```text
相对链接全部存在
已删除接口零引用
当前架构不使用 Runtime v2 / Runtime v4 作为版本名
未装配模块不得画成运行调用关系
项目级问题使用 SYS-* 索引
模块 Wiki 标明扫描提交
```

当前审计基线：`2426220`（2026-08-05）。
