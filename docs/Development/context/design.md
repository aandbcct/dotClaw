# 上下文工程模块设计

> **状态**: 设计阶段 | **日期**: 2026-06-17 | **讨论驱动**: Grill Me 方法逐层确认

---

## 一、背景与目标

### 1.1 為什麼要做上下文工程

当前 dotClaw 的上下文组装处于 Prompt Engineering 层：5 个 DataProvider 顺序拼接为 system prompt，再加 history + user_input 组成 messages。存在三个核心问题：

| 问题 | 现状 | 影响 |
|------|------|------|
| **无缓存优化** | 所有内容平铺在 system prompt 里，无前缀缓存意识 | LLM 每次调用都重新处理完整 prompt |
| **无分层管理** | 静态内容（角色定义）和动态内容（记忆检索）混在一起 | 无法区分哪些该缓存、哪些该过期 |
| **裁剪粗暴** | `msg_trim()` 对完整 messages 裁剪，可能裁掉当前 user_input | 极端情况下用户输入被丢弃 |

### 1.2 设计目标

- **前缀缓存友好**: 按变化频率分层，静态内容在最前，最大化 API 前缀缓存命中率
- **可扩展**: 新内容类型以 Slot 形式插入，不修改组装逻辑
- **缓存感知**: 不同层级的内容有不同缓存策略，避免重复读文件/数据库
- **边界清晰**: Assembler 只产出 system_prompt 文本，Message 构造归 _build_messages()

---

## 二、核心概念

### 2.1 三层抽象

```
ContextSlot    → 一个独立可插拔的上下文内容单元
Assembler      → 管理所有 Slot，按 tier 组装 system_prompt 文本
_build_messages() → 将 system_prompt 文本 + _history + user_input 拼成 Message[]
```

### 2.2 变化的层级

上下文内容按"变化频率"分为四层，数值越小越靠前，缓存越持久：

```
Tier 0 (STATIC)     跨 session 不变 → 永久前缀缓存
Tier 1 (SESSION)    同一 session 内不变 → 按 session 缓存
Tier 2 (CONDITIONAL) 每次 request 可能变化 → 按 request 缓存
Tier 3 (DYNAMIC)    每 turn 都变 → 不进 Slot，存在 _history 里
```

数据流：

```
  Slot (tier 0-2)               _history (tier 3)
       │                              │
       ▼                              │
  Assembler.build_system_prompt()     │  (Assembler 不碰 _history)
       │                              │
       ▼                              ▼
  _build_messages(system_prompt, user_input)
       │
       ▼
  Message[] → 发给 LLM
```

---

## 三、ContextSlot

### 3.1 接口

```python
from abc import ABC, abstractmethod

class ContextSlot(ABC):
    """上下文槽位基类。子类实现 _produce()，基类管缓存。"""

    name: str
    tier: TierLevel
    cache_policy: str                          # "forever" | "session" | "request"

    def __init__(self):
        self._cached: str | None = None
        self._cache_valid: bool = False

    @abstractmethod
    async def _produce(self, ctx: "SlotContext") -> str | None:
        """实际从来源加载内容。返回 None 表示本槽位无内容可产出。"""
        ...

    async def load(self, ctx: "SlotContext") -> str | None:
        """带缓存的加载入口。Assembler 调这个。"""
        if self._cache_valid:
            return self._cached
        content = await self._produce(ctx)
        self._cached = content
        self._cache_valid = True
        return content

    def invalidate(self):
        """标记缓存失效，下次 load() 会重新 _produce()。"""
        self._cache_valid = False
```

### 3.2 缓存策略

| 策略 | tier | 行为 | 代表 Slot |
|------|------|------|-----------|
| `forever` | 0 | 首次加载后永不过期 | IdentitySlot |
| `session` | 1 | 同 session 内不过期，切换 session 时 Assembler 整体重建 | ToolsSlot, SkillsSlot, WorkspaceSlot, UserInfoSlot |
| `request` | 2 | 每个 request 开始时 Assembler 调 `invalidate()` | MemorySlot, KnowledgeSlot, ProjectSlot |

同一 request 内的多个 turn，tier 2 的 `_produce()` 只调一次（首次 load 后命中缓存）。

### 3.3 Tier 枚举

```python
from enum import IntEnum

class TierLevel(IntEnum):
    """层级数值越小越靠前。间隔 10 便于将来插值。"""
    STATIC      = 0
    SESSION     = 10
    CONDITIONAL = 20
    DYNAMIC     = 30   # 不进 Slot，历史消息存在 _history 中
```

### 3.4 初始 Slot 清单

| Slot | tier | cache_policy | 来源 | 产出条件 |
|------|------|-------------|------|---------|
| IdentitySlot | STATIC | forever | `agent_config.agent_prompt` → fallback `config.agent.system_prompt` | 必产出 |
| ToolsSlot | SESSION | session | `ctx.tool_definitions` | 必产出 |
| SkillsSlot | SESSION | session | `ctx.skill_registry` | 必产出 |
| WorkspaceSlot | SESSION | session | `ctx.project_root` | 必产出 |
| UserInfoSlot | SESSION | session | `ctx.user_profile` | 必产出 |
| MemorySlot | CONDITIONAL | request | `ctx.memory_manager.search(ctx.query)` | 无结果返回 None |
| KnowledgeSlot | CONDITIONAL | request | `ctx.knowledge_base.search(ctx.query)` | 无结果或未配置返回 None |
| ProjectSlot | CONDITIONAL | request | `ctx.project_root / "AGENTS.md"` 等文件 | 文件不存在返回 None |

### 3.5 tier 3 不经过 Slot

对话历史（user/assistant/tool 消息）属于 tier 3，不在 Slot 体系中。它们存储在 Agent 的 `_history` 列表里，由 Loop 每 turn 追加，由 `_build_messages()` 读取。原因是：

- 它们每 turn 都变，不需要 Slot 的缓存语义
- LLM API 要求扁平 Message 数组，而 Slot 产出的是纯文本
- 将来压缩/裁剪只对 `_history` 做，不影响 system prompt 结构

---

## 四、Assembler

### 4.1 职责

Assembler 是 system_prompt 纯文本的生产者。不接触 Message 对象，不管理对话历史。

```python
class ContextAssembler:
    """按 tier 排序，依次 load 每个 Slot，输出 system_prompt 文本。"""

    def __init__(self, slots: list[ContextSlot]):
        self._slots = sorted(slots, key=lambda s: s.tier)

    def on_new_request(self):
        """每个新 request 开始时调用。使 tier 2 缓存过期。"""
        for slot in self._slots:
            if slot.cache_policy == "request":
                slot.invalidate()

    async def build_system_prompt(self, ctx: "SlotContext") -> str:
        parts = []
        for slot in self._slots:
            content = await slot.load(ctx)
            if content:
                parts.append(content)
        return "\n\n".join(parts)
```

### 4.2 生命周期

```
Agent.__init__()
  └─ Assembler(slots=[IdentitySlot, ToolsSlot, ..., ProjectSlot])

每次 Request:
  assembler.on_new_request()          ← tier 2 缓存过期
  └─ Turn N:
       system_prompt = await assembler.build_system_prompt(ctx)
       ↑ tier 0/1 永远命中缓存，tier 2 首次 load 后命中缓存

新 Session (Agent 重建):
  新 Agent → 新 Assembler → 新 Slots → 所有缓存清空
```

- 一个 Agent 一个 Assembler，一个 Agent 一个 session
- 天然 session 隔离，别的 session 看不到此 Assembler 的缓存
- 当前无并发场景（同一 session 同时只会一次对话），未来如需并发安全再加锁

---

## 五、_build_messages()

### 5.1 职责

接收 Assembler 产出的 system_prompt 纯文本，组装为最终 Message[] 发给 LLM。

```python
def _build_messages(self, system_prompt: str, user_input: str) -> list[Message]:
    system_msg = Message(role="system", content=system_prompt)
    user_msg = Message(role="user", content=user_input)

    # 历史 + user_input 的 token 预算（system 固定不动，不可裁）
    budget = self._max_context_tokens - _msg_tokens(system_msg) - _msg_tokens(user_msg)

    # 只裁 _history，system 和当前 user_input 永远保留
    history = msg_trim(self._history.copy(), budget)

    return [system_msg] + history + [user_msg]
```

### 5.2 与旧实现的区别

| 维度 | 旧 `_build_messages()` | 新 `_build_messages()` |
|------|----------------------|----------------------|
| system_prompt 来源 | PromptBuilder 组装（含 5 个 Provider） | Assembler 产出纯文本 |
| 裁剪对象 | 完整 messages 数组 | 只裁 `_history` |
| 裁剪风险 | 可能裁掉 user_input | system + user_input 永远不动 |

### 5.3 _history 管理

```python
# Agent 持有
self._history: list[Message] = []

# Loop 每 turn 结束后追加
self.agent._history.append(asst_msg)
for result in tool_results:
    self.agent._history.append(result)
```

---

## 六、SlotContext

### 6.1 定义

SlotContext 是传给每个 Slot.load() 的输入参数篮。只存输入，不存输出。替代旧的 AgentContext。

```python
@dataclass(frozen=True)
class SlotContext:
    """Slot 组装所需的全部输入。"""

    # 本次请求
    query: str
    request_id: str

    # Agent 配置
    agent_config: "AgentConfig"

    # 运行时环境
    session_id: str
    project_root: Path
    max_context_tokens: int

    # 依赖注入
    tool_definitions: list[dict]
    skill_registry: "SkillRegistry"
    memory_manager: "MemoryManager"
    knowledge_base: "KnowledgeBase | None"
    user_profile: "UserProfile | None"

    # 观测
    journal: "Journal"
```

### 6.2 与 AgentContext 的关系

AgentContext 退役。SlotContext 接管其"输入参数"职能。

AgentContext 里被去掉的字段及原因：
- `system_prompt` → 现在是 Assembler 的输出，不在 SlotContext
- `memory_summary` → MemorySlot 内部管，不在 SlotContext
- `workspace` → 等于 `project_root`，冗余
- `model`, `purpose`, `channel`, `rules` → 在 agent_config 或不需要
- `available_tools` → 等于 tool_definitions 的 name 列表，冗余

---

## 七、Journal 适配

当前 `Journal.session_start()` 接收 AgentContext，只从中取三个字段：

```python
# 旧
def session_start(self, ctx: "AgentContext", config: Any) -> None:
    self._session_id = ctx.session_id
    self._request_id = ctx.request_id
    self._model = ctx.model

# 新
def session_start(self, session_id: str, request_id: str, model: str, config: Any) -> None:
    self._session_id = session_id
    self._request_id = request_id
    self._model = model
```

调用方（Loop）直接从 SlotContext/Agent 各取各的，不依赖中间对象。

---

## 八、Loop 中的完整流程

```python
# AgentLoop.run() 伪代码

async def run(self, user_input: str) -> AgentResult:
    journal = Journal()
    ctx = SlotContext(
        query=user_input,
        request_id=uuid4().hex[:8],
        agent_config=self.agent.agent_config,
        session_id=self.agent.session_id,
        project_root=self.agent.project_root,
        max_context_tokens=self.agent.max_context_tokens,
        tool_definitions=self.agent.tool_definitions,
        skill_registry=self.agent.skill_registry,
        memory_manager=self.agent.memory_manager,
        knowledge_base=self.agent.knowledge_base,
        user_profile=self.agent.user_profile,
        journal=journal,
    )

    journal.session_start(ctx.session_id, ctx.request_id, ctx.agent_config.model, config)
    self.agent._assembler.on_new_request()

    for _ in range(max_iterations):
        system_prompt = await self.agent._assembler.build_system_prompt(ctx)
        messages = self.agent._build_messages(system_prompt, user_input)

        llm_resp = await self.agent._invoke_llm(messages, ctx, journal)

        if not llm_resp.tool_calls:
            final_response = llm_resp.content
            break

        self.agent._history.append(Message(role="assistant", content=llm_resp.content, tool_calls=llm_resp.tool_calls))
        for tc in llm_resp.tool_calls:
            result = await self.agent._execute_single_tool(tc, journal)
            self.agent._history.append(Message(role="tool", content=result, tool_call_id=tc.id))

    await self.agent._finalize_round(user_input, final_response, journal)
    journal.finalize()
    return AgentResult(final_text=final_response)
```

---

## 九、决策记录

| # | 决策 | 理由 |
|---|------|------|
| 1 | Slot 抽象类，子类实现 `_produce()` | 多态适配不同来源（文件、数据库、配置对象） |
| 2 | TierLevel IntEnum，间隔 10 | 将来可在现有层级间插值 |
| 3 | Assembler 只产出 system_prompt 文本 | 职责单一，不与 Message 格式耦合 |
| 4 | `_build_messages()` 只裁 `_history` | system 和 user_input 永远不丢 |
| 5 | AgentContext 退役，SlotContext 接班 | 旧快照模式不适配"每 turn 动态组装" |
| 6 | tier 3 不进 Slot | 对话消息用 `_history` 列表，语义清晰 |
| 7 | Journal 改签名为直接收参数 | 解耦，零风险 |
| 8 | IdentitySlot 读 agent_config.agent_prompt → fallback config.agent.system_prompt | 保持现有两层配置优先级不变 |
| 9 | 暂不做上层调度（disable 机制） | tier 2 slot 内部自己判断是否产出，满足当前需求 |

---

## 十、待定项（后续讨论）

1. **压缩机制**: 对话历史超长时的摘要生成与位置（参考 Claude Code 四级压缩）
2. **工具裁剪**: ToolsSlot 按任务类型动态过滤工具列表
3. **自动 flush**: 裁剪前自动将重要信息写入长期记忆（参考 OpenClaw memory flush）
4. **上层调度**: 当 Agent 类型多样化后，是否需要 disable 白名单
5. **并发安全**: 未来多 turn 并行时 Assembler 的线程安全
6. **RulesSlot**: 是否需要将 behavior rules 从 IdentitySlot 中独立出来
