# Phase 3 详细开发文档：Agent 内部基础设施

> 创建时间：2026-05-30
> 状态：规划完成，待实施

---

## 一、开发目的

建立 Agent 核心基础设施层，为后续记忆注入（P4）、工具动态注册（P5）、Skill 注入（P7）提供统一的数据通道和接口规范。

**核心目标**：
1. 统一上下文对象（AgentContext）——替代散落在 config/session/channel 各处的零散字段
2. 模块化 Prompt 构建器（PromptBuilder）——按 section 组装 system prompt，每个 section 由 DataProvider 填充
3. 标准化返回类型（AgentResult）——替代当前 `run()` 返回的裸 `str`
4. 消息工具函数（message_utils）——消息验证、裁剪、清理，为 P4 上下文压缩提供基础
5. 结构化日志系统（AgentLogger）——request_id 全链路追踪

**设计原则**：
- AgentContext 为不可变快照（frozen=True），在 `AgentLoop.run()` 开头创建，一次调用全程不变
- PromptBuilder 通过 DataProvider 接口解耦数据源，P4/P7 新增 section 时只需新增 Provider，不改 Builder
- AgentResult 通过 `__str__` 保持 `str` 兼容，渐进式迁移不破坏现有调用

---

## 二、模块层级与依赖关系

Phase 3 新增和修改的模块按依赖关系分为 5 层。

```
Layer 1: AgentResult, message_utils    ← 零 dotClaw 内部依赖
   ↓
Layer 2: AgentLogger                    ← 依赖 Python logging
   ↓
Layer 3: AgentContext                   ← 依赖 llm.base.Message
   ↓
Layer 4: DataProviders + PromptBuilder  ← 依赖 AgentContext + message_utils
   ↓
Layer 5: AgentLoop（修改）              ← 依赖以上全部
```

### 2.1 Layer 1 — 纯数据结构 / 纯函数层

| 文件 | 状态 | 描述 |
|------|------|------|
| `agent/result.py` | **新增** | AgentResult 纯 dataclass，含 `__str__` 兼容 |
| `agent/message_utils.py` | **新增** | 纯函数集合：validate / trim / clean |

**为什么放 `agent/` 下**：三个函数中 `trim()` 和 `clean()` 是 Agent 层的策略决策（上下文窗口管理、消息预处理），`validate()` 是 LLM 格式契约验证。三者的唯一调用方均为 `AgentLoop._build_messages()`，放在 `agent/` 下避免 LLM 层感知 Agent 策略。

### 2.2 Layer 2 — 日志系统

| 文件 | 状态 | 描述 |
|------|------|------|
| `agent/logger.py` | **新增** | AgentLogger：结构化日志，按模块分级，request_id 全链路追踪 |

与 P1 `debug/logger.py` 的关系：`debug/logger.py` 保留不动（TraceRecord + DebugManager），AgentLogger 回写 TraceRecord 保持 `/debug` 命令可用。

> ⚠️ **已知技术债**：P3 保留 DebugManager 是为了最小化 AgentLoop 变更范围。两个日志系统（AgentLogger + DebugManager）的并存是临时状态，计划在 P5 完成后将 DebugManager 的 TraceRecord 能力合并到 AgentLogger，删除 `debug/logger.py`。

### 2.3 Layer 3 — AgentContext

| 文件 | 状态 | 描述 |
|------|------|------|
| `agent/context.py` | **新增** | AgentContext 不可变 dataclass（frozen=True） |

**AgentContext 字段定义**：

| 字段 | 类型 | 描述 |
|------|------|------|
| `session_id` | `str` | 当前会话 ID |
| `workspace` | `Path` | Agent 操作目录（运行时产物），默认等同于 project_root |
| `project_root` | `Path` | dotClaw 安装目录（config.yaml / data/ / skills/ 所在） |
| `model` | `str` | 当前选用的模型名 |
| `system_prompt` | `str` | config.agent.system_prompt |
| `available_tools` | `list[str]` | 已注册工具的名称列表 |
| `tool_definitions` | `list[ToolDefinition]` | 完整工具定义（供 PromptBuilder 生成工具描述） |
| `request_id` | `str` | 本次 run() 调用的唯一标识（uuid4 前 8 位） |
| `purpose` | `str` | 本次请求的用途（当前固定 "chat"，P5+ 扩展） |
| `max_context_tokens` | `int` | config.agent.max_context_tokens，消息裁剪阈值 |
| `created_at` | `datetime` | 快照创建时间 |
| `channel` | `Channel \| None` | 通信通道（可为 None，如 Scheduler 触发） |

**字段取舍说明**：
- `session.history` **不放入** Context——AgentContext 是不可变快照，消息列表在 `run()` 过程中动态增长（tool 结果追加），应留在 `AgentLoop._messages`
- `workspace` 与 `project_root` 的区别：P1 CLI 场景下两者相同，P7 MCP/P10 多渠道时 workspace 可能是远程目录，与 dotClaw 安装目录无关
- `user_info` **不放入**——P3 无多用户概念，P10 多渠道时通过 Context 扩展字段加入

**AgentContext 组装方式**：
- 由 `AgentLoop._build_context()` 方法在 `run()` 开头调用
- 收集各数据源：`self.session`、`self.model`、`self.config`、`self._tool_registry`、`self.channel`
- 后续 P4/P7 扩展时只需在 `_build_context()` 中追加字段赋值

### 2.4 Layer 4 — PromptBuilder

| 文件 | 状态 | 描述 |
|------|------|------|
| `agent/prompt/providers.py` | **新增** | DataProvider 抽象接口 + RoleProvider / ToolsProvider / RulesProvider 实现 |
| `agent/prompt/builder.py` | **新增** | PromptBuilder：维护 providers 列表，调用 build() 按顺序拼接 |

**DataProvider 接口**：

```
DataProvider(ABC)
  ├── section_name() -> str        # "tools" / "memory" / "skills" 等
  └── provide(context) -> str|None # 返回该 section 内容，None 表示跳过
```

**P3 实现的 3 个 Provider**：

| Provider | 数据来源 | 产出内容 |
|----------|---------|---------|
| `RoleProvider` | `context.system_prompt` | 角色定义（system prompt 主体） |
| `ToolsProvider` | `context.tool_definitions` | 格式化的工具列表描述（名称 + 参数 schema + 描述） |
| `RulesProvider` | `context.system_prompt` 中的 `config.agent.rules` 字段 | 行为规则约束文本。`rules` 为空时返回 None（跳过 section） |

**`config.agent.rules` 新增字段**（需在 `config/settings.py` 的 `AgentConfig` 和 `config.yaml` 中补充）：

```yaml
# config.yaml
agent:
  system_prompt: |
    你是一个有用、诚实且友好的 AI 助手。
  rules: |
    # 可选，追加在 system_prompt 之后的额外行为规则
  max_context_tokens: 8000
```

```python
# config/settings.py — AgentConfig 新增字段
@dataclass
class AgentConfig:
    system_prompt: str = "..."
    max_context_tokens: int = 8000
    keep_recent_messages: int = 10
    rules: str = ""   # P3 新增：额外行为规则，追加到 system prompt
```

> P4 引入 MemoryProvider 后 system prompt 可能超过 2000 token。届时需在 PromptBuilder 中增加跨 section 去重和压缩逻辑。当前 P3 范围暂不处理。

**P4/P7 预留接口**（Provider 骨架，不实现逻辑）：
- `SkillsProvider`：从 `SkillLoader.load_all()` 获取技能描述，`provide()` 返回 None → P7 实现
- `MemoryProvider`：从 `MemoryFlushManager.get_context()` 获取记忆摘要，`provide()` 返回 None → P4 实现

**PromptBuilder.build(context) 产出结构**：

```
[RoleProvider.provide()]        ← 角色定义
[RulesProvider.provide()]       ← 行为规则（可选）
[ToolsProvider.provide()]       ← 可用工具列表
[SkillsProvider.provide()]      ← P7：技能描述
[MemoryProvider.provide()]      ← P4：记忆上下文
```

各 section 之间用两个换行分隔。provider 返回 None 时跳过该 section，不产生输出。

### 2.5 Layer 5 — AgentLoop 修改

| 文件 | 状态 | 描述 |
|------|------|------|
| `agent/loop.py` | **修改** | `_build_context()` 组装 AgentContext；`_build_messages()` 调用 PromptBuilder + message_utils；`run()` 返回 AgentResult |

**修改点**：

1. `__init__` 新增接收 `AgentLogger` 实例和 `PromptBuilder` 实例
2. 新增 `_build_context() -> AgentContext`：在 `run()` 开头调用，组装不可变快照
3. `_build_messages()` 改为：
   ```
   prompt = self._prompt_builder.build(context)
   messages = [Message("system", prompt)]
   messages += [历史消息] + [当前用户消息]
   messages = self._message_utils.trim(messages, context.max_context_tokens)
   messages = self._message_utils.clean(messages)
   return messages
   ```
4. `run()` 返回类型从 `str` 改为 `AgentResult`
5. `run()` 中所有异常都捕获并写入 `AgentResult.error`，finally 块中调用 `self._logger.record(result)`
6. `debug_trace()` 改为从 `self._logger.get_last_trace()` 读取（保持 `/debug` 命令可用）

**不变的部分**：工具调用循环逻辑、流式输出、会话保存机制完全保持 P1/P2 行为。

> **AgentResult 消费说明**：P3 中 AgentResult 的主要消费者是测试代码和日志系统（AgentLogger.record()）。`main.py` 的 CLI 循环暂不使用返回值（P5 调度器触发、P10 Web Channel 将作为消费者）。

> **前瞻建议**：P3 构造函数参数已达 8 个。P4 引入 MemoryFlushManager 时，考虑将 `prompt_builder, logger, memory_flush_manager, skill_loader` 等基础设施组件收拢为 `AgentServices` 对象，避免参数继续膨胀。

---

## 三、各模块开发要点

### 3.1 `agent/result.py` — AgentResult

**要点**：
- 纯 dataclass：`final_text`, `tool_calls_count`, `iterations`, `duration_ms`, `error`
- 实现 `__str__(self) -> str` 返回 `self.final_text`
- 零 dotClaw 内部依赖，确保任意模块可 import

### 3.2 `agent/message_utils.py` — 消息工具

**要点**：
- 三个纯函数，无内部状态，函数签名均为 `(list[Message], ...) -> list[Message] | list[str]`

| 函数 | 签名 | 职责 |
|------|------|------|
| `validate` | `(messages: list[Message]) -> list[str]` | 检查：tool_use/tool_result 配对完整性、角色顺序合法性、无连续相同角色消息。返回问题描述列表，空列表 = 合法 |
| `trim` | `(messages: list[Message], max_tokens: int) -> list[Message]` | 从旧到新逐条移除，保护 system 消息不被裁、保护 (assistant, tool_1, tool_2, ...) 配对组不被拆散。P3 用中英文差异化公式估算 token 数，P4 升级为 tiktoken |
| `clean` | `(messages: list[Message]) -> list[Message]` | 去除空 content、去除连续 system 消息（保留首条）、修复孤立的 tool 消息 |

**`trim()` 配对保护算法**：

```
trim(messages, max_tokens):
  1. 从 messages 末尾向前计算累计 token 数，确定裁剪边界
  2. 从 messages[0] 开始逐条标记可裁候选
  3. 遇到 assistant(tool_calls=[id_a, id_b, ...]) 时：
     a. 在后续消息中找到所有 tool_call_id in {id_a, id_b, ...} 的 tool 消息
     b. 将 assistant + 所有匹配的 tool 消息标记为"不可拆散的配对组"
     c. 裁剪边界不能切在这个组的中间（要么全保留，要么全裁）
  4. system 消息始终不可裁
  5. 裁剪到 token 预算内为止
```

**Token 估算公式**（P3 用中英文差异化估算，P4 替换为 tiktoken）：

```python
def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数。中文场景以字符数为主，英文以词数为主。"""
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    return chinese_chars + (other_chars // 4)   # 中文 ~1 char ≈ 1 token, 其他 ~4 chars ≈ 1 token
```

**开发注意事项**：
- P3 用上述中英文差异化公式估算 token，P4 升级为 tiktoken 精确计算
- 所有函数链式调用友好：接受 `list[Message]`，返回 `list[Message]`
- `trim()` 在极端情况下（单条消息超过 max_tokens）记录 warning 并保留该消息

### 3.3 `agent/logger.py` — AgentLogger

**要点**：
- `AgentLogger` 封装 Python `logging`，按模块分级：`dotclaw.agent`、`dotclaw.llm`、`dotclaw.tools`
- 每条日志记录携带 `request_id` 字段（通过 `logging.LogRecord` 的 extra 机制）
- `new_request() -> str`：为新的 `run()` 调用生成唯一 request_id（`uuid4().hex[:8]`），并设为当前活跃 ID
- `record(trace: TraceRecord)` 写入日志，同时回写到 `DebugManager`（保持 `/debug` 命令可用）
- `get_last_trace() -> TraceRecord | None` 委托给 `DebugManager`

**request_id 生成**：不在 `AgentLogger.__init__()` 中生成（Logger 是长生命周期单例），而是由 `AgentLoop._build_context()` 调用 `self._logger.new_request()` 为每次 `run()` 生成独立 ID。

**开发注意事项**：
- 与 `debug/logger.py` 的 DebugManager 保持双向同步：AgentLogger 写日志 → 同时写 TraceRecord 到 DebugManager
- 日志格式：`[YYYY-MM-DD HH:MM:SS] [LEVEL] [request_id] [module] message`

### 3.4 `agent/context.py` 和 `config/settings.py` — AgentContext + 配置扩展

**AgentContext 要点**：
- `@dataclass(frozen=True)`，所有字段在构造后不可变
- 无方法逻辑，纯数据容器
- `workspace` 默认值 = `project_root`（`__post_init__` 中通过 `object.__setattr__` 绕过 frozen 设置默认值）
- 依赖 `llm.base.Message`（仅 `tool_definitions: list[ToolDefinition]` 字段引用）

**AgentConfig 新增字段**（`config/settings.py`）：

```python
@dataclass
class AgentConfig:
    system_prompt: str = "你是一个有用、诚实且友好的 AI 助手。"
    max_context_tokens: int = 8000
    keep_recent_messages: int = 10
    rules: str = ""   # P3 新增：额外行为规则，追加到 system prompt 的 RulesProvider section
```

**config.yaml 对应配置**：

```yaml
agent:
  system_prompt: |
    你是一个有用、诚实且友好的 AI 助手。
  rules: ""
  max_context_tokens: 8000
  keep_recent_messages: 10
```

`rules` 为空字符串（默认值）时，`RulesProvider.provide()` 返回 `None`，PromptBuilder 跳过该 section。

**开发注意事项**：
- frozen 下设置默认值需用 `object.__setattr__(self, 'field', value)`，不能直接赋值
- 新增字段只需在此 dataclass 加一行 + `_build_context()` 加一行

### 3.5 `agent/prompt/providers.py` — DataProvider

**要点**：
- `DataProvider(ABC)`：两个抽象方法 `section_name()` 和 `provide(context)`
- P3 实现三个具体 Provider，均为无状态类（`provide()` 是纯函数）

**开发注意事项**：
- P4 在此文件新增 `MemoryProvider`，P7 新增 `SkillsProvider`，不修改 `builder.py`
- `provide()` 返回 `None` 时 PromptBuilder 跳过该 section

### 3.6 `agent/prompt/builder.py` — PromptBuilder

**要点**：
- `__init__` 接收 `providers: list[DataProvider]`
- `build(context: AgentContext) -> str`：按顺序调用 `p.provide(context)`，过滤 None，拼接为最终 system prompt
- 各 section 之间用 `\n\n` 分隔

**开发注意事项**：
- Builder 不关心 provider 的顺序语义，顺序由 AgentLoop 在传入 providers 列表时控制
- Provider 抛出异常时 Builder 记录 warning 日志并跳过该 section（不中断整体构建）

### 3.7 `agent/loop.py` — 修改

**要点**：
- `__init__` 新增参数：`prompt_builder: PromptBuilder`、`logger: AgentLogger`
- `_build_context()` 新增方法，在 `run()` 开头调用
- `_build_messages()` 改为调用 PromptBuilder + message_utils
- `run()` 返回类型改为 `AgentResult`，异常捕获写入 `AgentResult.error`
- `debug_trace()` 改为从 `logger.get_last_trace()` 读取

**与 P2 的兼容**：`self.llm.chat(purpose=context.purpose, model=context.model, ...)` 保持 P2 的 `purpose` 参数传递

---

## 四、开发实施顺序（按依赖关系）

```
Step 1: agent/result.py          (零依赖，先做)
Step 2: agent/message_utils.py    (依赖 llm.base.Message)
Step 3: agent/logger.py           (依赖 Python logging)
Step 4: agent/context.py          (依赖 llm.base.Message)
Step 5: agent/prompt/providers.py (依赖 AgentContext + DataProvider)
Step 6: agent/prompt/builder.py   (依赖 providers.py + AgentContext)
Step 7: 修改 agent/loop.py        (依赖以上全部)
Step 8: 更新 main.py              (初始化 PromptBuilder + AgentLogger)
Step 9: 验收测试
```

Step 1-3 可并行（互不依赖），Step 4 依赖 Step 1，Step 5-6 依赖 Step 4，Step 7 依赖 Step 5-6。

**main.py 修改范围**（Step 8）：

```python
# P2 版本（当前）
agent = AgentLoop(llm=llm_proxy, session=..., ...)

# P3 版本（新）
from dotclaw.agent.logger import AgentLogger
from dotclaw.agent.prompt.builder import PromptBuilder
from dotclaw.agent.prompt.providers import RoleProvider, ToolsProvider, RulesProvider

logger = AgentLogger()
prompt_builder = PromptBuilder([
    RoleProvider(),
    RulesProvider(),
    ToolsProvider(),
    # SkillsProvider(),    ← P7 激活
    # MemoryProvider(),    ← P4 激活
])

agent = AgentLoop(
    llm=llm_proxy,
    session=current_session,
    session_mgr=session_mgr,
    channel=channel,
    config=config,
    tool_registry=tool_registry,
    prompt_builder=prompt_builder,   # 新增
    logger=logger,                   # 新增
)
```

---

## 五、验收标准

### 5.1 功能验收

**场景 1：AgentResult 返回 + 兼容**
- 启动 `dotclaw`，输入 `你好`
- 预期：收到正常回复，无异常。在代码中验证 `result = await agent.run(...)` 返回的是 `AgentResult` 实例且 `str(result) == result.final_text`

**场景 2：PromptBuilder 正确拼接**
- 修改 `config.agent.rules` 添加一条规则（如 "始终使用中文回复"）
- 输入 `/debug` 查看 system prompt
- 预期：system prompt 包含角色定义、规则约束、工具列表三个 section，之间有明确分隔

**场景 3：message_utils 不破坏合法对话**
- 完成一次完整的带工具调用的对话（如 `现在几点了？`）
- 预期：`AgentLoop._build_messages()` 中 trim + clean 不报错，对话正常完成

**场景 4：request_id 追踪**
- 输入 `你好`，查看日志文件
- 预期：同一次 `run()` 调用的所有日志条目携带相同的 `request_id`

**场景 5：超长上下文裁剪**
- 发送大量消息使消息数超过 context 限制后，发送一条新消息
- 预期：旧消息被裁剪但最新消息保留，LLM 正常回复

**场景 6：/debug 兼容**
- 完成任意一次对话后输入 `/debug`
- 预期：显示最近推理过程（内容与 P1/P2 一致）

### 5.2 回归验收

- P1 的 5 个验收场景全部通过（纯文本对话、工具调用、审批、调试追踪、多轮对话）
- P2 的模型切换和降级功能正常
- `tests/test_phase1_acceptance.py` 全部通过
- `tests/test_phase2_acceptance.py` 全部通过

### 5.3 自动化测试

新增 `tests/test_phase3_acceptance.py`，覆盖以下场景（使用 MockLLM + FakeChannel 基础设施）：

| # | 测试场景 | 验证内容 |
|---|---------|---------|
| 1 | AgentResult.__str__ 兼容 | 验证 `str(result) == result.final_text`，现有代码不报错 |
| 2 | AgentContext 不可变 | 验证 frozen=True，修改字段抛出 FrozenInstanceError |
| 3 | PromptBuilder 拼接 | 注入 3 个 mock provider，验证输出顺序和分隔正确 |
| 4 | PromptBuilder 容错 | 注入一个抛异常的 provider，验证不中断整体构建 |
| 5 | message_utils.validate 合法对话 | 传入 P1 的标准对话，验证返回空列表 |
| 6 | message_utils.trim 配对保护 | 构造含 assistant(tool_calls) + tool 的消息列表，验证配对不被拆散 |
| 7 | message_utils.trim 中文估算 | 传入中文消息，验证估算值在合理范围（不要求精确） |
| 8 | request_id 每次不同 | 模拟两次 run() 调用，验证 request_id 不同 |

### 5.4 手动测试

- 按 5.1 的 6 个场景逐一在 CLI 中验证
- P1 回归：`pytest tests/test_phase1_acceptance.py -v` 全部通过
- P2 回归：`pytest tests/test_phase2_acceptance.py -v` 全部通过
- P3 自动化：`pytest tests/test_phase3_acceptance.py -v` 全部通过

### 5.5 代码质量

- AgentContext 通过 frozen=True 确保不可变
- message_utils 所有函数为纯函数，零副作用
- PromptBuilder 调用 provider 失败时不崩溃（warning 日志 + 跳过该 section）

---

## 六、开发注意事项

1. **P1/P2 回归优先**：Step 7 修改 AgentLoop 后必须立即运行 P1 和 P2 测试
2. **frozen dataclass 默认值**：AgentContext 的 `workspace` 默认值需用 `object.__setattr__` 绕过 frozen 限制
3. **Provider 失败容错**：PromptBuilder 调 `provide()` 时包 try/except，异常只记录不中断
4. **token 估算**：P3 用中英文差异化公式估算 token（见 §3.2），代码中加注释标明 P4 替换为 tiktoken
5. **日志格式**：AgentLogger 的日志格式与 P1 的 `debug/logger.py` 保持一致，不引入新的日志框架
6. **AgentResult 兼容**：所有引用 `agent.run()` 返回值的地方先确认代码不依赖返回类型为 `str`（大部分场景通过 `str()` 或打印使用，`__str__` 足以兼容）
7. **双日志系统技术债**：P3 保留 `debug/logger.py` 为临时状态，P5 后将 DebugManager 的 TraceRecord 能力合并到 AgentLogger 并删除旧文件
8. **中文 token 估算限制**：中英文差异化公式是近似值（中文 ~1 char/token），极端情况（如大量 emoji）可能偏差 2-3 倍。`max_context_tokens` 建议预留 20% 安全边界

---

## 七、文件清单

| 文件 | 状态 | 估计复杂度 |
|------|------|-----------|
| `agent/result.py` | 新增 | 低 |
| `agent/message_utils.py` | 新增 | 中 |
| `agent/logger.py` | 新增 | 中 |
| `agent/context.py` | 新增 | 低 |
| `agent/prompt/providers.py` | 新增 | 中 |
| `agent/prompt/builder.py` | 新增 | 低 |
| `agent/loop.py` | 修改 | 高 |
| `config/settings.py` | 修改 | 低 | AgentConfig 新增 `rules` 字段 |
| `config.yaml` | 修改 | 低 | agent 节新增 `rules` 配置项 |
| `main.py` | 修改 | 低 |
| `tests/test_phase3_acceptance.py` | 新增 | 中 |
