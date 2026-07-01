# Multi-Agent 子系统设计文档

> **版本**: v1.0 | **状态**: Draft | **日期**: 2026-06-29
> **关联**: [PRD](./PRD.md) | [架构路线图](../architecture-and-roadmap.md)

---

## 1. 架构总览

### 1.1 从单体到 Multi-Agent

```
Before (单 Agent 单体):                 After (Multi-Agent):
                                       
┌──────────────┐                       ┌──────────────────────────────┐
│    Agent     │                       │        Parent Agent          │
│  ┌────────┐  │                       │  ┌──────────┐ ┌──────────┐  │
│  │ Loop   │  │                       │  │  Runtime │ │ Identity │  │
│  │ ReAct  │  │                       │  └────┬─────┘ └──────────┘  │
│  └────────┘  │                       │       │                     │
│  ┌────────┐  │                       │  ┌────▼──────────────────┐  │
│  │ Config │  │                       │  │    AgentLoop          │  │
│  └────────┘  │                       │  │  LLM ⇄ ToolExecutor   │  │
│  ┌────────┐  │                       │  │    spawn_agent() ──────┼──┼──┐
│  │Memory  │  │                       │  └───────────────────────┘  │  │
│  └────────┘  │                       │  ┌──────────────────────┐   │  │
│  ┌────────┐  │                       │  │  SubagentSpawner     │   │  │
│  │ Tools  │  │                       │  │  - spawn(task, cfg)  │   │  │
│  └────────┘  │                       │  │  - active_handles{}  │   │  │
│  ┌────────┐  │                       │  └──────────────────────┘   │  │
│  │Skills  │  │                       └──────────────────────────────┘  │
│  └────────┘  │                                                         │
└──────────────┘                          ┌──────────────────────────────┤
                                          │        Child Agent           │
                                          │  ┌──────────┐ ┌──────────┐  │
                                          │  │ Runtime  │ │ Identity │  │
                                          │  │(shared LLM│ │(scoped)  │  │
                                          │  │ + Tools)  │ │          │  │
                                          │  └────┬─────┘ └──────────┘  │
                                          │  ┌────▼──────────────────┐  │
                                          │  │    AgentLoop          │◄─┘
                                          │  │  ReAct with scoped    │
                                          │  │  tools + memory       │
                                          │  └───────────────────────┘  │
                                          └──────────────────────────────┘
```

### 1.2 核心原则

1. **Agent 即进程模型**：每个 Agent 是独立的执行单元，拥有独立的 Loop + Context + Session
2. **共享基础设施，隔离业务上下文**：LLMProxy / RateLimiter / MemoryStorage 共享；ToolExecutor / Context 可裁剪
3. **父 Agent 对子 Agent 只有 spawn/steer/kill 三个操作**，不侵入子 Agent 内部执行
4. **一切通过 Journal 可观测**：spawn/message/complete/error/kill 全量记录

---

## 2. 组件设计

### 2.1 AgentRuntime — 纯运行时

从 `Agent` 中抽离出与"身份/配置/会话"无关的纯运行时部分。

```python
# src/dotclaw/agent/runtime.py

@dataclass
class AgentRuntime:
    """Agent 纯运行时 —— 无身份、无配置、无会话。
    
    多个 Agent 可共享同一个 AgentRuntime（共享 LLM + ToolExecutor）。
    """
    llm: "LLMProxy"
    tool_executor: "ToolExecutor | None"
    channel: "Channel | None"
    
    def scoped_executor(self, allowed_tools: list[str] | None) -> "ToolExecutor | None":
        """返回一个工具白名单裁剪后的 ToolExecutor（浅拷贝 wrapper）。"""
        ...
```

**关键设计决策**：
- `AgentRuntime` 是 **可共享** 的：父 Agent 和所有子 Agent 共用同一个 LLMProxy，避免重复初始化
- `scoped_executor()` 不修改原 executor，返回 wrapper——子 Agent 的工具权限隔离通过这个实现
- `channel` 可为 None（子 Agent 不需要直接输出到用户）

### 2.2 AgentIdentity — 身份配置

```python
# src/dotclaw/agent/identity.py

@dataclass
class AgentIdentity:
    """Agent 身份 —— 轻量、可序列化、可在 Agent 间传递。"""
    agent_id: str
    agent_name: str = "DotClaw"
    system_prompt_template: str = ""
    workspace: str = "."
    allowed_tools: list[str] = field(default_factory=list)
    registered_skills: list[str] = field(default_factory=list)
    model: str = ""  # "" = 继承父 Agent
    max_loop_steps: int = 10
    model_params: dict = field(default_factory=dict)
    description: str = ""
    tags: list[str] = field(default_factory=list)
    
    def derive(self, **overrides) -> "AgentIdentity":
        """基于当前 identity 创建派生 identity（子 Agent 用）。"""
        new = copy.copy(self)
        for k, v in overrides.items():
            setattr(new, k, v)
        return new
```

**关键设计决策**：
- `derive()` 是子 Agent 身份创建的唯一方式——保证继承链可追溯
- `agent_id` 由 spawner 自动生成（`{parent_id}.{uuid8}`），保证全局唯一
- `allowed_tools` 空列表 = 继承父 Agent 的全部工具（不是"无工具"）

### 2.3 Agent 改造

```python
# src/dotclaw/agent/agent.py（改造后）

class Agent:
    """Agent —— 身份 + 运行时 + 会话 + 记忆 + Skill 的装配体。
    
    变化：
    - runtime: AgentRuntime（新增，从自身属性中抽离）
    - identity: AgentIdentity（新增，替代零散的 agent_config 字段）
    - agent_config: 保留兼容，内部委托给 identity
    """
    
    def __init__(
        self,
        identity: AgentIdentity,
        runtime: AgentRuntime,
        config: "Config",
        session_mgr: "SessionManager",
        memory_mgr: "MemoryManager | None" = None,
        skill_registry: "SkillRegistry | None" = None,
        mcp_provider: Any = None,
        memory_dream: Any = None,
        mcp_task: Any = None,
        assembler: "ContextAssembler | None" = None,
        resume_manager: Any = None,
    ):
        self.identity = identity
        self.runtime = runtime
        # ... 其余不变
    
    @property
    def llm(self) -> "LLMProxy":
        return self.runtime.llm
    
    @property
    def tool_executor(self) -> "ToolExecutor | None":
        return self.runtime.tool_executor
    
    @property
    def channel(self) -> "Channel | None":
        return self.runtime.channel
```

**关键设计决策**：
- 保留 `Agent.agent_config` 属性做向后兼容（内部转发到 `identity`），避免全量重写
- `AgentLoop` 改为依赖 `AgentRuntime` 而非 `Agent`——这样子 Agent 可以用更轻量的 context 运行

### 2.4 SubagentSpawner — 子 Agent 孵化器

```python
# src/dotclaw/agent/subagent/spawner.py

class SpawnMode(Enum):
    ONE_SHOT = "one_shot"       # 执行完返回结果，回收
    PERSISTENT = "persistent"   # 长期存活，可多轮对话

@dataclass
class SpawnConfig:
    """子 Agent 孵化配置"""
    task: str                              # 子 Agent 的初始消息
    mode: SpawnMode = SpawnMode.ONE_SHOT
    identity_overrides: dict | None = None # 覆盖父 Agent 的 identity 字段
    inherit_memory: bool = True            # 是否继承父 Agent 的记忆管理器
    inherit_skills: bool = True            # 是否继承父 Agent 的 Skill 注册表
    tool_whitelist: list[str] | None = None # None=全部继承, [] = 无工具
    timeout_seconds: int = 300             # 子 Agent 最大执行时间

class SubagentSpawner:
    """子 Agent 孵化器 —— 由父 Agent 持有，负责 spawn/monitor 子 Agent。"""
    
    def __init__(
        self,
        parent_agent: "Agent",
        max_concurrent: int = 10,
        journal: "Journal | None" = None,
    ):
        self._parent = parent_agent
        self._max_concurrent = max_concurrent
        self._journal = journal
        self._handles: dict[str, "SubagentHandle"] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
    
    async def spawn(self, config: SpawnConfig) -> "SubagentHandle":
        """孵化一个子 Agent 并开始执行。"""
        ...
    
    def list_handles(self) -> list["SubagentHandle"]:
        """列出所有活跃的子 Agent handles。"""
        return list(self._handles.values())
    
    async def kill_all(self) -> None:
        """终止所有子 Agent。"""
        ...
```

**spawn 内部流程**：

```
spawn(config)
    │
    ├─ 1. 生成 child_id = f"{parent_id}.{uuid8}"
    ├─ 2. 基于 parent.identity.derive(**overrides) 创建 child_identity
    ├─ 3. 裁剪 ToolExecutor（如果有 whitelist）
    ├─ 4. 构建 child_runtime（共享 LLM、裁剪后的 ToolExecutor、channel=None）
    ├─ 5. 创建子 Agent 实例
    ├─ 6. 如果 inherit_memory: 共享 MemoryManager
    ├─ 7. 如果 inherit_skills: 共享 SkillRegistry
    ├─ 8. 创建 SubagentHandle，注册到 self._handles
    ├─ 9. Journal: agent_spawn 事件
    ├─ 10. asyncio.create_task(child.chat(config.task))
    └─ 11. 返回 SubagentHandle
```

### 2.5 SubagentHandle — 子 Agent 句柄

```python
# src/dotclaw/agent/subagent/handle.py

class SubagentStatus(Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"
    TIMEOUT = "timeout"

class SubagentHandle:
    """子 Agent 句柄 —— 父 Agent 通过此句柄与子 Agent 交互。"""
    
    def __init__(self, agent_id: str, task: asyncio.Task, agent: "Agent"):
        self.agent_id = agent_id
        self._task = task
        self._agent = agent
        self._result_future: asyncio.Future = asyncio.Future()
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._status = SubagentStatus.RUNNING
        self._spawned_at = time.time()
    
    @property
    def status(self) -> SubagentStatus:
        return self._status
    
    @property
    def elapsed_seconds(self) -> float:
        if self._status == SubagentStatus.RUNNING:
            return time.time() - self._spawned_at
        return self._completed_at - self._spawned_at
    
    async def result(self, timeout: float | None = None) -> "AgentResult":
        """等待子 Agent 完成并返回结果。"""
        return await asyncio.wait_for(self._result_future, timeout=timeout)
    
    async def send(self, message: str) -> None:
        """向子 Agent 发送 steering 消息（仅 PERSISTENT 模式有效）。"""
        await self._message_queue.put(message)
    
    def kill(self) -> None:
        """终止子 Agent。"""
        if not self._task.done():
            self._task.cancel()
        self._status = SubagentStatus.KILLED
```

### 2.6 AgentMessage — 通信协议

```python
# src/dotclaw/agent/subagent/message.py

class AgentMessageType(Enum):
    TASK_ASSIGN = "task_assign"
    RESULT = "result"
    STEER = "steer"
    HEARTBEAT = "heartbeat"
    ERROR = "error"

@dataclass
class AgentMessage:
    """Agent 间消息 —— 通信协议的最小单元。"""
    msg_id: str          # uuid
    from_id: str         # 发送方 agent_id
    to_id: str           # 接收方 agent_id（"*" = broadcast）
    msg_type: AgentMessageType
    payload: dict        # 消息体
    timestamp: float     # time.time()
    reply_to: str | None = None  # 回复的 msg_id
```

**通信拓扑**：

```
                    ┌──────────┐
                    │  Parent  │
                    └────┬─────┘
           TASK_ASSIGN   │   RESULT/HEARTBEAT/ERROR
           STEER         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
         ┌────────┐ ┌────────┐ ┌────────┐
         │ Child  │ │ Child  │ │ Child  │
         │   A    │ │   B    │ │   C    │
         └────────┘ └────────┘ └────────┘
```

### 2.7 AgentLoop 改造

```python
# src/dotclaw/agent/loop.py（改造后）

class AgentLoop:
    """Agent 主循环 —— 纯执行引擎。
    
    变化：
    - 构造函数改为接收 AgentRuntime + AgentIdentity（而非 Agent）
    - 新增 _process_subagent_spawn() 处理 spawn_agent tool call
    - 子 Agent 实例化时复用同一个 AgentLoop 类
    """
    
    def __init__(self, runtime: AgentRuntime, identity: AgentIdentity, 
                 context_provider: "ContextProvider"):
        self.runtime = runtime
        self.identity = identity
        self.context_provider = context_provider
        self._history: list[Message] = []
        self._spawner: "SubagentSpawner | None" = None
        self._running = False
    
    async def run(self, user_message: str) -> AgentResult:
        """处理一条消息。支持子 Agent 路径。"""
        # ... 大部分逻辑不变 ...
        
        # 在 tool execution 阶段增加 spawn_agent 的特殊处理
        for tc in llm_resp.tool_calls:
            if tc.name == "spawn_agent":
                handle = await self._handle_spawn_agent(tc)
                # 不立即 await result，收集到 pending list
                pending_handles.append(handle)
            elif tc.name == "list_agents":
                result = await self._handle_list_agents(tc)
                tool_messages.append(result)
            elif tc.name == "kill_agent":
                result = await self._handle_kill_agent(tc)
                tool_messages.append(result)
            else:
                # 正常工具执行
                ...
        
        # 等待所有子 Agent 完成
        if pending_handles:
            sub_results = await asyncio.gather(*[
                h.result() for h in pending_handles
            ])
            # 将子 Agent 结果注入 messages，让 LLM 在下一轮汇总
            ...
```

### 2.8 内置工具实现

```python
# src/dotclaw/tools/builtin/subagent_tool.py

# spawn_agent tool definition
SPAWN_AGENT_DEF = ToolDefinition(
    name="spawn_agent",
    description="孵化一个子 Agent 来执行独立任务。子 Agent 可以并行运行，完成后返回结果。"
                "适用于：多步骤并行任务、需要不同专长的子任务、大任务的分解执行。",
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "子 Agent 要执行的任务描述"
            },
            "agent_name": {
                "type": "string",
                "description": "子 Agent 的显示名称（如 '代码审查员'）。不填则自动生成"
            },
            "allowed_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "子 Agent 可用的工具白名单。不填则继承全部工具"
            },
            "system_prompt_append": {
                "type": "string",
                "description": "追加到子 Agent system prompt 末尾的指令"
            },
        },
        "required": ["task"]
    }
)

# 执行逻辑（在 ToolExecutor 中注册）：
async def _execute_spawn_agent(args, agent_loop):
    spawner = agent_loop.spawner
    config = SpawnConfig(
        task=args["task"],
        identity_overrides={
            "agent_name": args.get("agent_name", "SubAgent"),
            "system_prompt_template": args.get("system_prompt_append", ""),
        },
        tool_whitelist=args.get("allowed_tools"),
    )
    handle = await spawner.spawn(config)
    
    # ONE_SHOT 模式：等待完成
    result = await handle.result()
    return ToolResult(output=json.dumps({
        "agent_id": handle.agent_id,
        "status": handle.status.value,
        "result": result.final_text,
        "tool_calls": result.tool_calls_count,
        "iterations": result.iterations,
        "duration_ms": result.duration_ms,
        "error": result.error,
    }, ensure_ascii=False))
```

---

## 3. 数据流

### 3.1 端到端流程（树形委托）

```
User: "审查这个仓库的代码质量和安全性"

Parent AgentLoop:
  │
  ├─ [Turn 1] LLM 决策
  │   └─ tool_calls: [
  │       spawn_agent(task="审查代码安全性", agent_name="安全审查员", 
  │                    allowed_tools=["read_file", "exec_command"]),
  │       spawn_agent(task="审查代码质量", agent_name="质量审查员",
  │                    allowed_tools=["read_file", "exec_command"]),
  │     ]
  │
  ├─ [Tool Execution] SubagentSpawner.spawn() × 2
  │   ├─ Child-A (安全审查): AgentLoop.run("审查代码安全性...")
  │   └─ Child-B (质量审查): AgentLoop.run("审查代码质量...")
  │
  ├─ [Wait] asyncio.gather(handle_a.result(), handle_b.result())
  │   │
  │   ├─ Child-A ReAct: read_file → analyze → 发现 SQL 注入风险
  │   └─ Child-B ReAct: read_file → analyze → 发现循环嵌套过深
  │
  ├─ [Turn 2] LLM 汇总
  │   └─ 将两个子 Agent 结果注入 context
  │   └─ LLM 输出最终汇总报告
  │
  └─ [Response] 返回给 User
```

### 3.2 上下文继承链

```
Parent Agent                          Child Agent
┌──────────────────┐                 ┌──────────────────┐
│ ContextAssembler │                 │ ContextAssembler │
│  IdentitySlot    │                 │  IdentitySlot    │ ← child identity
│  ToolsSlot       │                 │  ToolsSlot       │ ← 裁剪后的 tools
│  SkillsSlot      │                 │  SkillsSlot      │ ← 共享 skill_registry
│  WorkspaceSlot   │                 │  WorkspaceSlot   │ ← 继承 workspace
│  UserInfoSlot    │                 │  UserInfoSlot    │ ← 继承 user info
│  MemorySlot      │                 │  MemorySlot      │ ← 共享 MemoryManager
│  KnowledgeSlot   │                 │  KnowledgeSlot   │ ← 继承
│  ProjectSlot     │                 │  ProjectSlot     │ ← 继承
│                  │                 │                  │
│  + spawner_ctx   │                 │  + parent_ctx    │ ← 父 Agent 任务摘要
│    (子任务列表)    │                 │    (父任务上下文)  │
└──────────────────┘                 └──────────────────┘
```

### 3.3 Journal Trace 链路

```
Trace: 2026-06-29/abc123/
├── parent/
│   ├── trace.jsonl
│   │   ├── session_start (agent_id="default", request_id="req001")
│   │   ├── prompt_built (...)
│   │   ├── loop_start
│   │   ├── llm_response_end (...)
│   │   ├── agent_spawn (parent_id="default", child_id="default.a1b2", task="...")
│   │   ├── agent_spawn (parent_id="default", child_id="default.c3d4", task="...")
│   │   ├── agent_complete (agent_id="default.a1b2", result="...")
│   │   ├── agent_complete (agent_id="default.c3d4", result="...")
│   │   ├── loop_end ("response")
│   │   └── session_end ("success")
│   └── report.json
├── default.a1b2/
│   ├── trace.jsonl
│   │   ├── session_start (agent_id="default.a1b2", parent_request_id="req001")
│   │   ├── ... (ReAct 循环详情)
│   │   └── session_end ("success")
│   └── report.json
└── default.c3d4/
    ├── trace.jsonl
    └── report.json
```

---

## 4. 与现有模块的集成

### 4.1 Memory 集成

```
┌─────────────────────────────────────────────────┐
│              MemoryManager (共享)                │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │ Parent   │  │ Child-A  │  │ Child-B  │      │
│  │ Writes   │  │ Writes   │  │ Writes   │      │
│  │ memory   │  │ memory   │  │ memory   │      │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘      │
│       │             │             │             │
│       ▼             ▼             ▼             │
│  ┌─────────────────────────────────────────┐    │
│  │           MemoryStorage (SQLite)         │    │
│  │  chunk_id │ agent_id │ content │ ...     │    │
│  │  ─────────┼──────────┼─────────┼─────    │    │
│  │  ck001    │ default  │ ...     │         │    │
│  │  ck002    │ a1b2     │ ...     │         │    │
│  │  ck003    │ c3d4     │ ...     │         │    │
│  └─────────────────────────────────────────┘    │
└─────────────────────────────────────────────────┘

搜索时默认按 agent_id 过滤当前 Agent 自己的记忆。
子 Agent 可通过参数搜索父 Agent 的记忆（继承链）。
```

**MemoryStorage 改造**：`chunks` 表新增 `agent_id` 列，支持按 agent 过滤记忆搜索范围。

### 4.2 Eval 集成

新增 3 个评测 case，注册到 `Eval/cases/`：

```
Eval/cases/
├── subagent_dispatch.py    # 子 Agent spawn 延迟 & 并发吞吐
├── multi_agent_collab.py   # N 子 Agent 协作准确率
└── context_inherit.py      # 上下文继承正确性
```

### 4.3 ResumeManager 集成

```
中断场景：
  Parent Agent 在执行中崩溃
    ├─ 检查是否有未完成的子 Agent（从 Journal trace 中读取）
    ├─ 恢复父 Agent 的 _history
    ├─ 对每个未完成的子 Agent:
    │   ├─ 如果子 Agent 有完整的 trace：从 checkpoint 恢复
    │   └─ 如果子 Agent 无 trace：重新 spawn
    └─ 继续执行

ResumeManager 扩展：
  - 新增 get_pending_subagents(session_id) → list[child_agent_id]
  - 新增 restore_subagent(child_id) → Agent
```

### 4.4 ApprovalManager 集成

子 Agent 的工具审批独立于父 Agent：
- 子 Agent 可以有自己的审批配置
- 或者继承父 Agent 的审批策略（默认）
- 子 Agent 的敏感操作可以向上 escalate 到父 Agent（未来的 HITL 方向）

---

## 5. 文件结构

```
src/dotclaw/agent/
├── __init__.py              # 导出: Agent, AgentConfig, AgentLoop, AgentResult
├── agent.py                 # Agent 类（改造：持有 runtime + identity）
├── identity.py              # [新增] AgentIdentity
├── runtime.py               # [新增] AgentRuntime
├── loop.py                  # AgentLoop（改造：依赖 runtime）
├── factory.py               # build_agent()（改造：创建 runtime + identity）
├── result.py                # AgentResult（不变）
├── message_utils.py         # 消息工具（不变）
├── slotContext.py           # ContextSlot / ContextAssembler（微调）
├── slotContextImp.py        # Slot 实现（微调）
├── resume.py                # ResumeManager（扩展子 Agent 恢复）
│
└── subagent/                # [新增] Multi-Agent 子系统
    ├── __init__.py
    ├── spawner.py            # SubagentSpawner + SpawnConfig + SpawnMode
    ├── handle.py             # SubagentHandle + SubagentStatus
    └── message.py            # AgentMessage + AgentMessageType

src/dotclaw/tools/builtin/
└── subagent_tool.py          # [新增] spawn_agent / list_agents / kill_agent

src/dotclaw/journal/
├── events.py                # [扩展] 新增 5 个 agent_* 事件

Eval/cases/
├── subagent_dispatch.py     # [新增]
├── multi_agent_collab.py    # [新增]
└── context_inherit.py       # [新增]

tests/
└── subagent/                # [新增]
    ├── test_spawner.py
    ├── test_handle.py
    ├── test_context_inherit.py
    └── test_multi_agent_collab.py
```

---

## 6. 实现顺序

```
Phase 12: Agent 原语重构
  ├── 12.1: 创建 AgentRuntime / AgentIdentity
  ├── 12.2: Agent 适配（向后兼容）
  ├── 12.3: AgentLoop 解耦
  ├── 12.4: 工厂适配
  └── 12.5: 全量回归测试通过

Phase 13: Subagent 核心链路
  ├── 13.1: SubagentSpawner + SpawnConfig
  ├── 13.2: SubagentHandle
  ├── 13.3: spawn_agent / list_agents / kill_agent 内置工具
  ├── 13.4: AgentLoop 集成 spawn 路径
  ├── 13.5: 上下文继承（Memory/Tools/Skills）
  ├── 13.6: 单元测试
  └── 13.7: 端到端验收测试

Phase 14: Multi-Agent 拓扑与通信
  ├── 14.1: AgentMessage 协议
  ├── 14.2: 树形委托（父 spawn N 子 → gather）
  ├── 14.3: HEARTBEAT 长时间进度汇报
  ├── 14.4: Journal 扩展（5 个新事件）
  └── 14.5: trace 目录隔离（子 Agent 独立 trace）

Phase 15: Eval + Resume + 稳定性
  ├── 15.1: subagent_dispatch 评测 case
  ├── 15.2: multi_agent_collab 评测 case
  ├── 15.3: context_inherit 评测 case
  ├── 15.4: ResumeManager 子 Agent 恢复
  ├── 15.5: 并发压力测试（100 子 Agent）
  └── 15.6: 内存泄漏检测

Phase 16: 文档与示例
  ├── 16.1: 设计文档定稿
  ├── 16.2: README 更新
  ├── 16.3: 使用示例（代码审查、任务分片）
  └── 16.4: Eval baseline 建立
```

---

## 7. 关键设计决策记录

| 决策 | 选项 | 选择 | 理由 |
|---|---|---|---|
| 子 Agent 是否共享 LLMProxy | 共享 / 各自创建 | **共享** | 节省连接资源，共用 RateLimiter，天然全局限流 |
| 子 Agent 工具权限模型 | 黑名单 / 白名单 | **白名单** | 默认安全：未指定 = 继承全部，指定 = 仅允许列表中的工具 |
| 子 Agent 记忆隔离 | 完全隔离 / 共享存储 | **共享存储 + agent_id 过滤** | 同一 SQLite DB，搜索时默认过滤自己的记忆，可主动跨 agent 搜索 |
| 子 Agent channel | 继承父 / 独立 / None | **None（默认）** | 子 Agent 不直接输出给用户，结果通过 handle 回传 |
| spawn_agent 是异步还是同步 | fire-and-forget / await 结果 / 混合 | **异步 spawn + 可选 await** | spawn 立即返回 handle，LLM 可决定在后续 turn 汇总或立即等待 |
| 子 Agent 的 ReAct 循环 | 复用 AgentLoop / 独立实现 | **复用 AgentLoop** | 子 Agent 就是 AgentLoop.run()，只是 runtime 不同 |
