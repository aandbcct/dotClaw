<div align="center">

# 🐾 dotClaw

**以声明式 Agent、可恢复 Runtime 和可插拔基础设施构建的轻量级 Agent Harness框架**

声明式角色 · 模型路由容错 · 工具与 MCP · 上下文与记忆 · 可恢复执行 · 运行观测 · 多 Agent 协作

[![Python](https://img.shields.io/badge/Python-3.13+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/aandbcct/dotClaw?color=orange)](https://github.com/aandbcct/dotClaw)

</div>

---

## dotClaw 是什么

dotClaw 是一个面向 AI Agent 应用开发的 Python 框架。它既提供 Agent 的角色声明、模型与工具接入、上下文和记忆构建，也提供把一次请求可靠地运行、暂停、恢复、审计和委派出去的执行底座。

项目把 Agent 系统拆成两类关注点：

- **能力平面**回答“Agent 能做什么、此刻应该看到什么”：声明式角色、模型路由、工具与 MCP、Skills、Memory、Workspace；
- **执行平面**回答“这次请求如何可靠地完成”：Session、Runtime v4、运行仓储、审批、取消与多 Agent 委派。

Runtime v4 是 dotClaw 的关键执行底座，而不是项目的全部。它将每条用户输入处理为独立 `AgentRun`，让运行中的上下文、状态机、取消令牌与消息证据都归属于该 Run，而不是挂在全局 Runtime 或 Agent 实例上。

```mermaid
flowchart TB
    User["用户 / CLI / Web Channel"] --> Entry["SessionInteractionService\n按 Session 路由"]
    Config["config.yaml + Agent YAML"] --> Entry

    subgraph Capability["能力平面：Agent 可以做什么"]
        Identity["AgentIdentity\n角色、模型、工具约束"]
        Router["LLM\nRouter / Proxy / 熔断 / 限流"]
        Tools["Tools + MCP\n@tool Discovery / Schema / Policy"]
        Context["Context\n多 Owner Slots / Version / Budget"]
        Knowledge["Memory / Skills / Workspace\n项目与知识来源"]
        Identity --> Context
        Knowledge --> Context
    end

    subgraph Execution["执行平面：一次请求如何可靠完成"]
        Session["Session\n会话语义历史"]
        Coordinator["SessionRunCoordinator\n同 Session 串行"]
        Engine["RuntimeEngine\n共享、业务无状态"]
        Run["RunExecution + AgentState\n局部事务与状态机"]
        Facts["Run Storage\nConversation / AgentRun / Event / Message / Checkpoint"]
        Coordinator --> Engine --> Run
        Engine --> Facts
    end

    Entry --> Session
    Entry --> Coordinator
    Engine --> Context
    Engine --> Router
    Engine --> Tools
    Engine --> Delegation["DelegationPort"]
    Delegation --> Orchestration["orchestration\nDispatcher / Broker / 子 Agent"]
    Router --> Provider["多模型 Provider"]
    Tools --> External["内置工具 / MCP Server"]
    Journal["Journal\n可选观测与诊断"] -. "不作为恢复事实源" .-> Facts
```

## 核心亮点

| 领域 | 设计 | 工程价值 |
|---|---|---|
| 声明式角色 | `AgentIdentity` + YAML Agent 配置 | 角色、模型偏好、工具约束与执行基础设施解耦 |
| 模型调用韧性 | `ModelRouter` + `LLMProxy` + 限流/熔断/降级 | 将模型选择与失败编排从 Agent 逻辑中移出 |
| 工具与 MCP | `@tool` 自动发现 + Pydantic Schema + Registry / Executor / Handler，MCP 统一命名空间接入 | 新工具无需维护注册列表；本地与 MCP 共享校验、超时、错误与审计契约 |
| 工具安全护栏 | Capability Broker → Policy Engine → 审批 → 受批准路径执行 | 默认拒绝、路径逃逸防护、MCP server allowlist、无交互审批即拒绝，并支持 Agent 按 Run 收窄权限 |
| 上下文工程 | 多 Owner Slot、Context Version、动态事实引用、精确 Token 预算 | 稳定上下文可审计，多轮工具 ReAct 不产生冗余快照 |
| 记忆与技能 | Memory、Knowledge、Skill Registry 作为 Context 来源 | 不让 Runtime 直接耦合检索和提示词细节 |
| 会话与执行分离 | Conversation 与 AgentRun / RunMessage / RunEvent 分容器 | 对话语义保持干净，执行细节可审计、可排障 |
| Run 级隔离 | 每个请求创建独立 `RunExecution` | 多个 Session 可并行，不会共享“当前 Agent / 当前消息 / 当前状态” |
| 可恢复提交 | `run.json.SuccessCommitIntent` + 幂等补偿 | 防止 Run 已完成但 Conversation 缺少最终回答的半提交状态 |
| 历史压缩 | 调用前精确计数、最旧 75% Conversation、Run 内 staged 候选 | 超限不静默裁剪；取消/失败/中断不污染 Session 历史 |
| 工具审计 | `TOOL_STARTED` / `TOOL_COMPLETED` 成对事件 | 成功、审批、失败、取消与委派工具调用均可追溯 |
| 安全控制协议 | `approval_id → run_id`、checkpoint、run 级取消 | 审批在原 Run 恢复；有副作用工具不盲目重放 |
| 多 Agent 解耦 | `RuntimeDelegationAdapter` 封装 Dispatcher/Broker | Runtime 只处理提交、结果和取消，不重新耦合编排细节 |

## 模块版图：从角色到外部能力

### 声明式 Agent 与会话

`AgentIdentity` 是声明式角色边界（角色、模型、工具约束、策略收窄），不持有运行时对象、不是可执行门面。运行时入口 `SessionInteractionService` 读取 `Session.agent_id` 校验 Identity，再以冻结的 `RunRequest` 直接提交 `SessionRunCoordinator` 并返回结构化 `RunResult`；Channel 负责按本次提交的输出端口渲染结果。同一套执行基础设施因此可以服务多个不同 Agent 身份，而无需任何 Agent 实例。

```mermaid
flowchart LR
    YAML[".dotclaw/agentConfig/*.yaml"] --> Identity["AgentIdentity\n纯配置与约束"]
    Session["Session\nConversation 历史"] --> Request["冻结 RunRequest"]
    Identity --> Service["SessionInteractionService\n按 Session 路由"]
    Service --> Request
    Request --> Runtime["Runtime v4"]
```

### 模型、工具、上下文与知识

| 模块 | 当前职责 | 与执行内核的关系 |
|---|---|---|
| `llm/` | 模型选择、限流、熔断、重试和跨模型降级 | 经 `LLMPort` 返回标准化模型响应 |
| `tools/` | 装饰器发现、Schema 校验、资源策略、审批、超时与 Handler 执行 | 经 `ToolPort` 暴露受安全策略约束的调用能力 |
| `mcp/` | MCP Server 连接、快照发现与工具适配 | 仅将 MCP tools 作为命名空间工具来源接入 |
| `context/` | Slot 组装、缓存、token 预算和降级 | 实现 `ContextPort`，产出完整 `ContextBundle` |
| `memory/` / `skills/` | 检索、技能目录、项目/知识补充 | 作为 Context Slot 的依赖，不侵入状态机 |
| `journal/` | 可选 trace、报告和调试观测 | 不承担 checkpoint 或 Runtime 恢复事实 |
| `orchestration/` | AgentRegistry、Dispatcher、Broker 等编排语义 | 经 `DelegationPort` 被 Runtime 使用 |

这种模块化使“换一个模型提供商”“增加一个 MCP 工具”“新增一个记忆来源”和“替换运行存储”成为局部接入工作，而不是重写 Agent 主流程。

### 一条请求如何穿过系统

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant A as Agent / Channel
    participant S as Session
    participant R as Runtime v4
    participant C as Context
    participant L as LLM 路由与模型
    participant T as 工具 / MCP
    participant P as 持久化

    U->>A: 用户消息
    A->>S: 读取对话语义历史
    A->>R: RunRequest（冻结快照）
    R->>C: 构造 ContextBundle
    C-->>R: messages + tools + metadata
    R->>L: 模型调用
    L-->>R: 最终回答或 ToolCall
    alt ToolCall
        R->>T: 统一工具执行 / 审批检查
        T-->>R: ToolResult 或 ApprovalRequired
        R->>P: 保存消息、事件、必要 checkpoint
        R->>L: 携带工具结果继续推理
    else 最终回答
        R->>P: 原子化成功提交与 Conversation 投影
    end
    R-->>A: RunResult
    A-->>U: 最终回答、等待审批或错误结果
```

## Runtime：可靠执行底座

### 分层与依赖边界

```mermaid
flowchart TB
    Bootstrap["bootstrap/application_host.py\nApplicationHost（唯一组合根）\nruntime_factory 为其内部装配"]

    subgraph Runtime["runtime/"]
        subgraph Domain["domain：事实与规则"]
            Facts["facts.py / context.py\nAgentRun / RunMessage / ContextVersion"]
            Events["events.py\nDomainEvent / RunEvent / ToolAuditStatus"]
            Machine["state.py\nAgentState"]
        end
        subgraph App["application：用例与编排"]
            Engine["RuntimeEngine"]
            Execution["RunExecution"]
            Coordinator["SessionRunCoordinator"]
            Port["Ports / DTO / Budget / Approval / Cancellation"]
        end
        subgraph Adapter["adapters：具体接入"]
            Store["文件仓储 Adapter"]
            LLM["LLMProxyAdapter"]
            Tool["ToolExecutorAdapter"]
            Policy["AgentPolicyResolver"]
        end
    end

    Bootstrap --> App
    Bootstrap --> Adapter
    App --> Domain
    App -->|"只依赖 Protocol"| Port
    Adapter -->|"实现 Protocol"| Port
    Adapter --> Facts
    Adapter --> Existing["Session / LLM / Tool / MCP / Memory / orchestration"]
```

- `domain`：稳定运行事实、领域事件、状态枚举与状态机规则；不依赖外部技术实现。
- `application`：一次执行如何创建、循环、恢复、取消和提交；不直接调用具体 SDK，也不读写具体文件。
- `adapters`：将文件仓储、LLMProxy、ToolExecutor、SessionManager 和既有编排系统翻译为 Application Port。
- `bootstrap/application_host.py`：唯一公开组合根与生命周期宿主，创建并持有全部应用级资源、装配 Runtime，并在 `shutdown()` 中按依赖逆序关闭；`runtime_factory.py` 是其私有的 Runtime 内部装配函数。

### 状态机与运行控制

```mermaid
stateDiagram-v2
    [*] --> IDLE
    IDLE --> WAITING_LLM: RunStarted
    WAITING_LLM --> WAITING_TOOLS: LLM 返回 ToolCall
    WAITING_LLM --> FINALIZING: LLM 返回最终回答
    WAITING_TOOLS --> WAITING_LLM: 工具批次完成
    WAITING_TOOLS --> WAITING_APPROVAL: 工具需要审批
    WAITING_APPROVAL --> WAITING_TOOLS: 审批通过
    WAITING_APPROVAL --> CANCELLED: 审批拒绝 / 取消
    WAITING_TOOLS --> WAITING_DELEGATION: 提交子 Agent
    WAITING_DELEGATION --> WAITING_LLM: 子 Agent 成功返回
    WAITING_DELEGATION --> FAILED: 子 Agent 失败
    FINALIZING --> COMPLETED: 成功提交
    WAITING_LLM --> FAILED: 模型 / Context 异常
    WAITING_TOOLS --> FAILED: 工具异常
    WAITING_LLM --> CANCELLED: 取消请求
    WAITING_TOOLS --> CANCELLED: 取消请求
    COMPLETED --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
```

普通用户消息总是创建新 Run。模型若需要补充信息，会正常生成澄清回复并完成当前 Run；下一条消息再由 Conversation 提供语义连续性。只有审批等结构化控制事件会恢复已有 Run。

## 运行事实、恢复与观测

```mermaid
flowchart TB
    Session["Session"] --> Conversation["Conversation\n用户可见的成功语义历史"]
    Session --> Run["AgentRun\n运行摘要与索引"]
    Run --> Message["RunMessage\n动态模型/工具证据"]
    Run --> Version["ContextVersion\n稳定 Slot 快照"]
    Run --> Event["RunEvent\n追加式审计事实"]
    Run --> Checkpoint["Checkpoint\n安全恢复点"]
    Message -. "message_id 引用" .-> Event
    Version -. "context_version 引用" .-> Event
    Run -. "成功后才投影" .-> Conversation
    Intent["run.json.SuccessCommitIntent\n临时可恢复提交意图"] -. "仅成功提交期间" .-> Run
```

```text
data/sessions/{session_id}/
├── session.json              # Conversation 与已提交 history_compressions
└── agent_runs/{run_id}/
    ├── run.json                 # AgentRun、活动版本、staged 候选与成功意图
    ├── messages.json            # v4：RunMessage 与 ContextVersion
    ├── events.jsonl             # RunEvent：按 sequence 追加的事实
    ├── checkpoint.json          # 审批等待等安全边界的最小恢复快照
```

这里有三个重要的工程约束：

1. **快照与事实分离**：`ContextVersion` 只保存 Snapshot Slot；Run Message 由 `LLM_STARTED.incremental_message_ids` 引用，工具结果会进入下一轮输入而不会不断创建版本。
2. **成功提交可补偿**：`RUN_COMPLETED`、Conversation/历史摘要投影和 `run.json=COMPLETED` 由 `SuccessCommitIntent` 组成可恢复提交协议；恢复器会幂等补齐未完成步骤。
3. **只从安全边界恢复**：Checkpoint 保存状态、游标、预算、活动版本和 pending 控制引用，不复制完整 prompt 或工具结果。正在执行且有副作用的工具不会被 Runtime 盲目重放。

成功才写入 Conversation；失败、取消和等待审批只保存运行事实。`Journal` 可以提供诊断和观测，但不再作为 Runtime 的状态恢复来源。

## 上下文、审批与多 Agent 的关键细节

### Context Slot：隔离、降级与预算

`ContextProvider` 是 `ContextPort` 的实现。它以 `ContextPlan` 组合 Agent、Session、Run、Global 四类 Owner 的 Slot；Snapshot Slot 形成可审计的 `ContextVersion`，`run_messages` 作为事实引用型 Slot 注入动态 ReAct 证据。

每次业务 LLM 调用前由 tiktoken 精确统计实际输入。超限时压缩最旧 75% 完整 Conversation，候选先暂存当前 Run，只有 Run 成功才提交到 Session；取消、失败或中断不会污染长期历史。更多设计见[上下文工程说明](docs/wiki/上下文工程说明.md)。

### 工具审批、取消与委派

```mermaid
sequenceDiagram
    participant R as RuntimeEngine
    participant T as ToolPort
    participant A as ApprovalRepository
    participant C as CheckpointRepository
    participant UI as 审批交互层

    R->>T: execute(tool_call)
    T-->>R: APPROVAL_REQUIRED
    R->>A: 保存 approval_id → run_id
    R->>C: 保存最小 Checkpoint
    R-->>UI: WAITING_APPROVAL(approval_id)
    UI->>R: resolve_approval(approval_id, decision)
    R->>A: 原子消费审批记录
    R->>C: 读取 Checkpoint + RunMessage
    R->>R: 在原 run_id 继续或取消
```

- 取消是 run 级控制信号：Engine 向 LLMPort、ToolPort 发出 best-effort 取消，并在安全点收口终态；
- `RuntimeDelegationAdapter` 将 Dispatcher、Broker 和子 Session 封装成 `DelegationPort`，父子关系通过 `parent_run_id`、`root_run_id` 和事件表达；
- 父 Run 取消时，取消信号可以沿 DelegationPort 传播到当前子 Run。

### Tool：声明式注册、安全执行与 MCP 连接

本地工具采用装饰器 @tool 声明：开发者只需描述工具名称、参数和所触及的资源，框架会在启动时自动发现它并生成模型可用的调用 Schema，不再需要维护分散的注册函数与集中列表。

每次工具调用都会先校验参数，再识别它要访问的文件、进程、网络或 MCP 资源，由统一策略决定允许、请求确认或拒绝。文件路径会处理用户目录、相对跳转和符号链接；真正执行时只使用已经批准的目标路径。不同 Agent 还可以在全局安全上限内进一步收紧权限，且规则随每个 Run 隔离。

内置通用工具当前包含：`builtin.files.*`（文件读写/列举）、`builtin.process.execute`（进程执行，需审批）、`builtin.memory.*`（记忆读写）、`builtin.system.*`（信息/时间）、`builtin.web.search`（固定调用 Tavily 搜索）、`builtin.weather.get_forecast`（固定调用 Open-Meteo 预报）与 `builtin.math.calculate`（本地受限表达式计算）。网络类工具通过**静态声明**的固定 Provider 访问预授权主机，不存在可由 Agent 控制的任意 URL/endpoint，也没有任意网页抓取、正文提取或爬虫能力；`HttpClient` 是仅供 Provider 使用的受控内部客户端，不进入工具注册表与提示词。

MCP 只将标准工具能力接入系统，并以“服务名 + 工具名”的稳定命名空间展示，避免与内置工具或其他服务冲突。服务连接和调用都需要显式授权；首次发现完成后，每个 Run 使用固定的工具快照，运行中不会因服务状态变化而改变模型看到的工具集。

详见 [Tool 模块总体说明](docs/wiki/Tool%20模块总体说明.md)。

## 项目结构

```text
dotClaw/
├── src/dotclaw/
│   ├── agent/           # AgentIdentity 与身份声明（无运行时门面）
│   ├── runtime/         # Runtime v4：domain / application / adapters
│   ├── context/         # 多 Owner Slot、Context Version、Token 预算
│   ├── llm/             # ModelRouter、LLMProxy、熔断与限流
│   ├── tools/           # Registry、Executor、Handler、审批与工具定义
│   ├── mcp/             # MCP 提供者与工具适配
│   ├── memory/          # 记忆检索、存储与蒸馏
│   ├── skills/          # Skill 扫描、注册与解析
│   ├── session/         # Session 与 Conversation 语义存储
│   ├── orchestration/   # AgentRegistry、Dispatcher、Broker、委派适配
│   ├── journal/         # 可选诊断、报告与观测数据
│   ├── channel/         # CLI 等交互通道
│   ├── config/          # YAML 配置与环境变量展开
│   ├── bootstrap/       # ApplicationHost（唯一组合根）与 Runtime 内部装配
│   └── scheduler/       # 调度能力
├── .dotclaw/agentConfig/# Agent 角色 YAML 定义
├── config.yaml          # 全局配置
├── model_router_config.yaml
├── scripts/             # 迁移等维护脚本
└── tests/
```

## 快速开始

### 环境要求

- Python 3.13+
- 已配置模型访问所需的环境变量与 `config.yaml`

### 安装与启动

```bash
pip install -e .
python -m dotclaw
```

也可以使用安装后的命令：

```bash
dotclaw
```

`.dotclaw/agentConfig/*.yaml` 用于定义 Agent 身份；`config.yaml` 和 `model_router_config.yaml` 用于配置会话、模型和路由策略。CLI 支持 `/new`、`/list`、`/switch`、`/tools`、`/mcp`、`/skills`、`/cancel <run_id>`、`/model` 等命令。

## 验证

```powershell
# 默认运行当前架构测试；自动跳过 legacy 测试
.\.venv\Scripts\python.exe -m pytest

# Runtime v4 架构边界与持久化护栏
.\.venv\Scripts\python.exe -m pytest tests/runtime_v2/test_architecture_contract.py tests/runtime_v2/test_phase6_finalization.py
```

## 可靠性与演进边界

当前 Runtime v4 已提供运行隔离、成功提交补偿、审批恢复、Context Version 与动态事实审计、调用前历史压缩和 Port 解耦。这是可靠单进程运行与可演进架构的基础。

当前 Session 租约是进程内 `asyncio.Lock`，运行仓储是本地文件；它**不是多节点分布式高可用实现**。后续可在既有 Port 边界替换为分布式 Session 租约、PostgreSQL/对象存储、后台 Worker、OpenTelemetry 投影等能力，而无需让 `RuntimeEngine` 直接依赖数据库、队列或监控 SDK。

## 文档导航

- [Runtime 模块总体说明](docs/wiki/Runtime%20模块总体说明.md)：Runtime v4 的分层、状态机、提交协议、可靠性与排障
- [上下文工程说明](docs/wiki/上下文工程说明.md)：多 Owner Slot、Context Version、动态事实引用与历史压缩


---

<div align="center">

**🐾 dotClaw · 用工程化边界组织 Agent 能力与可靠执行**

</div>
