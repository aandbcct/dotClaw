# dotClaw 架构设计文档

> 一个轻量级 AI Agent 框架，用于学习 AI 应用开发的核心工程实现。

## 1. 项目概述

**dotClaw** 是一个用 Python 实现的轻量级 AI Agent 框架，目标是吃透 AI Agent 应用的工程实现。采用"全而浅"策略——所有核心模块都实现基础版，后续可逐个深化。

### 核心设计原则

1. **架构清晰 > 功能完备** —— 每个模块职责单一、边界明确
2. **可扩展 > 刚好够用** —— 预留接口，但不提前实现
3. **可观测 > 黑盒** —— 日志 + 调试命令，让内部过程可见
4. **学习导向** —— 每个设计决策都标注"为什么这么做"

---

## 2. 模块架构总览

```
┌─────────────────────────────────────────────────────┐
│                     dotClaw                         │
│                                                     │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐       │
│  │ Channel  │    │ Session  │    │  Agent   │       │
│  │ (CLI/..) │◄──►│ Manager  │◄──►│  Loop    │       │
│  └──────────┘    └──────────┘    └────┬─────┘       │
│                                       │             │
│                    ┌──────────────────┼────────┐    │
│                    │                  │        │    │
│              ┌─────▼─────┐    ┌──────▼───┐ ┌──▼──┐  │
│              │LLM Proxy  │    │  Tool    │ │Skill│  │
│              │(Retry/    │    │ Registry │ │Loadr│  │
│              │ Fallback) │    │          │ │     │  │
│              └─────┬─────┘    └────┬─────┘ └──┬──┘  │
│                    │               │          │     │
│              ┌─────▼─────┐    ┌────▼────┐  ┌──▼───┐ │
│              │  Qwen     │    │ Tools   │  │SKILL │ │
│              │  Client   │    │ (exec/  │  │.md + │ │
│              │           │    │  file/  │  │script│ │
│              │           │    │  ...)   │  │      │ │
│              └───────────┘    └─────────┘  └──────┘ │
│                                                     │
│     ┌──────────┐  ┌──────────┐  ┌──────────┐        │
│     │  Memory  │  │ Scheduler│  │  Config  │        │
│     │(Store/   │  │(Reminder)│  │ (YAML)   │        │
│     │Compress/ │  │          │  │          │        │
│     │ LongTerm)│  │          │  │          │        │
│     └──────────┘  └──────────┘  └──────────┘        │
│                                                     │
│  ┌──────────┐                                       │
│  │  Debug   │  (logging + /debug)                   │
│  └──────────┘                                       │
└─────────────────────────────────────────────────────┘
```

---

## 3. 核心数据流

### 3.1 主循环（Agent Loop）

```
用户输入
    │
    ▼
Channel.receive() ──────────────────────────────┐
    │                                            │
    ▼                                            │
SessionManager.get_session(id)                   │
    │                                            │
    ▼                                            │
AgentLoop.run(user_message, session)             │
    │                                            │
    ├─ 1. 构建 messages                          │
    │     system_prompt = 配置 + skills描述       │
    │     + session.history                      │
    │     + 用户消息                             │
    │                                            │
    ├─ 2. 检查上下文长度                         │
    │     如果超限 → ContextCompressor.compress() │
    │     (截断旧消息 + LLM摘要)                  │
    │                                            │
    ├─ 3. LLMProxy.chat(messages, tools) ──────┐ │
    │     │                                     │ │
    │     │  流式返回 chunks:                    │ │
    │     │  ├─ 文本内容 → Channel.stream()     │ │
    │     │  └─ tool_calls → 收集完整调用       │ │
    │     │                                     │ │
    │     ▼                                     │ │
    │  4. 如果有 tool_calls:                    │ │
    │     ├─ ApprovalManager.check(tool, args)  │ │
    │     │   └─ 需要审批 → Channel.ask_user() │ │
    │     ├─ ToolRegistry.execute(name, args)   │ │
    │     ├─ 将工具结果追加到 messages           │ │
    │     └─ 回到步骤 3 ───────────────────────┘ │
    │                                            │
    ├─ 5. 如果是最终文本回复:                     │
    │     └─ SessionManager.save_message()       │
    │                                            │
    └─ 6. 返回 ─────────────────────────────────┘
```

### 3.2 消息格式（预留多模态）

```python
@dataclass
class Message:
    role: str                          # "system" | "user" | "assistant" | "tool"
    content: str | list[ContentPart]   # 文字 或 多模态内容列表
    tool_calls: list[ToolCall] | None  # assistant 消息中的工具调用
    tool_call_id: str | None           # tool 消息对应的调用 ID

@dataclass
class ContentPart:
    type: str        # "text" | "image_url"
    text: str | None
    image_url: str | None
```

基础版 `content` 只用 `str`，但类型标注预留了 `list[ContentPart]`，后续加图片不用改结构。

---

## 4. 模块详细设计

### 4.1 LLM 客户端层（llm/）

#### 架构：代理模式

```
AgentLoop
    │
    ▼
LLMProxy ──────────────────────────────┐
    │                                   │
    ├─ retry 逻辑（指数退避）            │
    ├─ fallback 逻辑（主模型失败切备用）  │
    ├─ 流式/非流式统一接口               │
    │                                   │
    ▼                                   │
LLMClient (抽象基类)                    │
    │                                   │
    ├─ QwenClient                       │
    │   └─ 千问 API 实现                 │
    │                                   │
    ├─ (预留) OpenAIClient              │
    ├─ (预留) ClaudeClient              │
    └─ (预留) OllamaClient              │
```

#### 关键类

```python
class LLMClient(ABC):
    """LLM 客户端抽象基类"""
    
    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[ChatChunk]:
        """流式聊天，返回 chunk 迭代器"""
        ...

class LLMProxy:
    """LLM 代理：重试 + 降级 + 流式"""
    
    def __init__(self, config: Config):
        self.clients: dict[str, LLMClient] = {}  # 模型名 → 客户端
        self.primary: str = ""                     # 主模型
        self.fallbacks: list[str] = []             # 备用模型列表
        self.max_retries: int = 3
    
    async def chat(self, messages, tools=None, stream=True):
        # 1. 尝试主模型（含重试）
        # 2. 主模型全部重试失败 → 依次尝试 fallback
        # 3. 全部失败 → 抛出异常
        ...
```

#### 千问 API 适配

千问 API 兼容 OpenAI 格式，关键点：
- Base URL: `https://dashscope.aliyuncs.com/compatible-mode/v1`
- 用 `openai` Python SDK，改 `base_url` 和 `api_key`
- 流式：`stream=True`，返回 SSE
- Function calling：直接用 OpenAI 格式的 `tools` 参数

### 4.2 工具系统（tools/）

#### 工具注册表

```python
class ToolDefinition:
    """工具定义（对应 LLM 的 function schema）"""
    name: str
    description: str
    parameters: dict  # JSON Schema

class ToolResult:
    """工具执行结果"""
    output: str
    is_error: bool = False

class ToolRegistry:
    """工具注册与执行"""
    
    def __init__(self):
        self._tools: dict[str, tuple[ToolDefinition, Callable]] = {}
    
    def register(self, name: str, definition: ToolDefinition, handler: Callable):
        self._tools[name] = (definition, handler)
    
    def get_definitions(self) -> list[ToolDefinition]:
        """返回所有工具定义，用于传给 LLM"""
        return [def_ for def_, _ in self._tools.values()]
    
    async def execute(self, name: str, arguments: dict) -> ToolResult:
        definition, handler = self._tools[name]
        return await handler(**arguments)
```

#### 基础工具列表

| 工具名 | 功能 | 审批 | 实现要点 |
|--------|------|------|----------|
| `exec` | 执行 shell 命令 | ✅ 需要 | asyncio.create_subprocess_shell |
| `read_file` | 读文件 | ❌ | aiofiles |
| `write_file` | 写文件 | ❌ | aiofiles |
| `list_dir` | 列目录 | ❌ | os.scandir |
| `python` | 执行 Python 代码 | ✅ 需要 | RestrictedPython 或 subprocess |
| `system_info` | 时间/目录/环境 | ❌ | os/datetime |
| `memory_read` | 读长期记忆 | ❌ | 读 MEMORY.md |
| `memory_write` | 写长期记忆 | ❌ | 追加到 MEMORY.md |
| `web_search` | 网页搜索 | ❌ | 预留接口，基础版不实现 |

#### 审批机制

```python
class ApprovalManager:
    """简单审批：危险工具执行前确认"""
    
    NEEDS_APPROVAL = {"exec", "python"}  # 需要审批的工具
    
    async def check(self, tool_name: str, arguments: dict, channel: Channel) -> bool:
        if tool_name not in self.NEEDS_APPROVAL:
            return True  # 直接放行
        
        # 通过 channel 问用户
        answer = await channel.ask_user(
            f"⚠️ 即将执行 {tool_name}: {arguments}\n确认执行？(y/n): "
        )
        return answer.lower() in ("y", "yes")
```

### 4.3 Skill 系统（skills/）

#### 目录结构

```
skills/
├── weather/
│   ├── SKILL.md        # 技能描述 + 使用指南
│   └── scripts/
│       └── fetch.py    # 实际执行脚本
├── xlsx/
│   ├── SKILL.md
│   └── scripts/
│       └── create.py
└── ...
```

#### SKILL.md 格式

```markdown
---
name: weather
description: "查询天气和天气预报。当用户问天气时触发。"
---

# 天气查询技能

## 使用方式
当用户询问天气时，执行 `scripts/fetch.py --city <城市名>` 获取天气数据。

## 参数
- `--city`: 城市名（必填）
- `--days`: 预报天数，默认 1

## 输出
JSON 格式的天气数据，包含温度、天气状况、风力等。
```

#### 加载逻辑

```python
class SkillLoader:
    """加载 skills 目录下的所有 SKILL.md"""
    
    def load_all(self, skills_dir: Path) -> list[Skill]:
        skills = []
        for skill_dir in skills_dir.iterdir():
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                skills.append(self._parse(skill_md))
        return skills
    
    def _parse(self, path: Path) -> Skill:
        # 解析 YAML 头部（name + description）
        # 正文作为 skill_instructions
        ...
    
    def build_skill_prompt(self, skills: list[Skill]) -> str:
        """生成注入 system prompt 的技能描述段"""
        # 类似 OpenClaw: "以下技能提供专门指令..."
        # 把每个 skill 的 description 列出来
        # 把匹配到的 skill 的完整 instructions 追加
        ...
```

### 4.4 记忆系统（memory/）

#### 三层记忆

```
┌─────────────────────────────────────┐
│  工作记忆 (Working Memory)           │
│  = 当前对话的 messages 列表          │
│  存储位置: data/sessions/<id>.json   │
│  特点: 完整对话历史，LLM 直接可见    │
└─────────────────┬───────────────────┘
                  │ 超过窗口时压缩
                  ▼
┌─────────────────────────────────────┐
│  压缩记忆 (Compressed Memory)        │
│  = 旧消息的 LLM 摘要                 │
│  存储位置: session JSON 内            │
│  特点: 保留关键信息，大幅减少 token   │
└─────────────────┬───────────────────┘
                  │ 跨 session 持久化
                  ▼
┌─────────────────────────────────────┐
│  长期记忆 (Long-Term Memory)         │
│  = MEMORY.md 文件                    │
│  存储位置: data/memory/MEMORY.md     │
│  特点: Agent 主动读写，跨会话持久    │
└─────────────────────────────────────┘
```

#### 上下文压缩策略

```python
class ContextCompressor:
    """上下文压缩：截断 + LLM 摘要"""
    
    async def compress(self, messages: list[Message], max_tokens: int) -> list[Message]:
        # 1. 估算当前 token 数
        current_tokens = self._estimate_tokens(messages)
        if current_tokens <= max_tokens:
            return messages
        
        # 2. 保留 system prompt + 最近 N 条消息
        system_msgs = [m for m in messages if m.role == "system"]
        recent_msgs = messages[-self.keep_recent:]  # 保留最近 10 条
        
        # 3. 旧消息送去 LLM 做摘要
        old_msgs = messages[len(system_msgs):-self.keep_recent]
        summary = await self._summarize(old_msgs)
        
        # 4. 组装: system + summary + recent
        summary_msg = Message(
            role="system",
            content=f"[之前的对话摘要]\n{summary}"
        )
        return system_msgs + [summary_msg] + recent_msgs
    
    async def _summarize(self, messages: list[Message]) -> str:
        """调用 LLM 对旧消息做摘要"""
        # 用一个简单的 prompt: "请简要总结以下对话的关键信息"
        ...
```

#### 会话持久化

```python
@dataclass
class Session:
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[Message]
    summary: str | None       # 压缩后的摘要
    model: str                # 使用的模型
    system_prompt: str         # 系统提示词

class SessionManager:
    """多会话管理"""
    
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir / "sessions"
    
    async def create(self, title: str = "新对话") -> Session: ...
    async def load(self, session_id: str) -> Session: ...
    async def save(self, session: Session) -> None: ...
    async def list_all(self) -> list[SessionMeta]: ...  # 轻量列表
    async def delete(self, session_id: str) -> None: ...
```

### 4.5 通道系统（channel/）

#### 抽象接口

```python
class Channel(ABC):
    """消息通道抽象基类"""
    
    @abstractmethod
    async def receive(self) -> str:
        """接收用户输入"""
        ...
    
    @abstractmethod
    async def send(self, message: str) -> None:
        """发送消息给用户"""
        ...
    
    @abstractmethod
    async def stream(self, chunk: str) -> None:
        """流式输出一个 chunk"""
        ...
    
    @abstractmethod
    async def ask_user(self, prompt: str) -> str:
        """向用户提问（用于审批等）"""
        ...
```

#### CLI 实现

```python
class CLIChannel(Channel):
    """命令行通道"""
    
    # receive() → input() 或 async input
    # send() → print()
    # stream() → print(chunk, end="", flush=True)
    # ask_user() → input(prompt)
```

后续加 Telegram 时，只需实现 `TelegramChannel(Channel)` 即可，Agent Loop 不用改。

### 4.6 定时提醒（scheduler/）

```python
class ReminderManager:
    """最简一次性提醒"""
    
    def __init__(self, channel: Channel):
        self._tasks: dict[str, asyncio.Task] = {}
        self._channel = channel
    
    async def set_reminder(self, id: str, delay_seconds: float, message: str):
        """设置一个提醒"""
        async def _remind():
            await asyncio.sleep(delay_seconds)
            await self._channel.send(f"⏰ 提醒: {message}")
        
        self._tasks[id] = asyncio.create_task(_remind())
    
    async def cancel_reminder(self, id: str):
        if id in self._tasks:
            self._tasks[id].cancel()
            del self._tasks[id]
```

需要注册为 Agent 工具，让 LLM 可以调 `set_reminder` 和 `cancel_reminder`。

### 4.7 多 Agent（sub.py）

```python
class SubAgent:
    """最简子 Agent：跑独立任务，返回结果"""
    
    async def spawn(self, task: str, model: str | None = None) -> str:
        """
        派生一个子 Agent 执行任务:
        1. 创建新的 session
        2. 用独立的 AgentLoop 跑
        3. 返回最终结果文本
        """
        sub_session = await self.session_mgr.create(title=f"子任务: {task[:20]}")
        sub_loop = AgentLoop(
            llm=self.llm_proxy,
            tools=self.tool_registry,
            session=sub_session,
            channel=SilentChannel(),  # 不输出到用户界面
        )
        result = await sub_loop.run(task)
        return result
```

### 4.8 配置系统（config/）

#### config.yaml 示例

```yaml
# dotClaw 配置文件

llm:
  default_model: qwen-plus
  clients:
    qwen-plus:
      provider: qwen
      api_key: sk-xxx
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      model: qwen-plus
    qwen-turbo:
      provider: qwen
      api_key: sk-xxx
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      model: qwen-turbo
  fallbacks:
    - qwen-turbo
  retry:
    max_retries: 3
    base_delay: 1.0  # 秒，指数退避基数
  stream: true

agent:
  system_prompt: "你是一个有用的 AI 助手。"
  max_context_tokens: 8000
  keep_recent_messages: 10

tools:
  exec:
    enabled: true
    needs_approval: true
  python:
    enabled: true
    needs_approval: true
    timeout: 30
  web_search:
    enabled: false  # 基础版不启用

skills:
  directory: ./skills

memory:
  long_term_file: ./data/memory/MEMORY.md

session:
  directory: ./data/sessions

scheduler:
  enabled: true

debug:
  level: INFO        # DEBUG | INFO | WARNING | ERROR
  log_file: ./data/dotclaw.log
```

#### 配置加载

```python
@dataclass
class Config:
    llm: LLMConfig
    agent: AgentConfig
    tools: ToolsConfig
    skills: SkillsConfig
    memory: MemoryConfig
    session: SessionConfig
    scheduler: SchedulerConfig
    debug: DebugConfig

def load_config(path: Path = Path("config.yaml")) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    # 递归转 dataclass，带默认值
    ...
```

### 4.9 调试系统（debug/）

```python
class DebugManager:
    """调试管理：logging + /debug 命令"""
    
    def __init__(self, config: DebugConfig):
        # 配置 Python logging
        self.logger = logging.getLogger("dotclaw")
        self._last_trace: TraceRecord | None = None
    
    def record_trace(self, trace: TraceRecord):
        """记录一次完整的推理过程"""
        self._last_trace = trace
    
    def format_trace(self) -> str:
        """格式化最近一次推理过程，供 /debug 命令展示"""
        ...

@dataclass
class TraceRecord:
    """一次完整推理的追踪记录"""
    timestamp: datetime
    session_id: str
    user_message: str
    messages_sent_to_llm: list[dict]   # 发给 LLM 的完整 messages
    llm_responses: list[dict]          # LLM 的每次响应
    tool_calls: list[dict]             # 工具调用记录
    tool_results: list[dict]           # 工具返回结果
    final_response: str                # 最终回复
    token_usage: dict                  # token 消耗
    duration_ms: int                   # 总耗时
```

---

## 5. 项目目录结构

```
D:\dev\dotClaw\
├── pyproject.toml                # 项目配置
├── README.md                     # 项目说明
├── config.yaml                   # 运行配置
├── .gitignore
│
├── src/
│   └── dotclaw/
│       ├── __init__.py
│       ├── __main__.py           # python -m dotclaw 入口
│       ├── main.py               # CLI 启动
│       │
│       ├── agent/
│       │   ├── __init__.py
│       │   ├── loop.py           # Agent 核心循环
│       │   └── sub.py            # 子 Agent
│       │
│       ├── llm/
│       │   ├── __init__.py
│       │   ├── base.py           # LLMClient 抽象基类
│       │   ├── qwen.py           # 千问实现
│       │   └── proxy.py          # 代理模式（重试+降级）
│       │
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── base.py           # ToolRegistry + ToolDefinition
│       │   ├── approval.py       # 审批管理
│       │   ├── exec_tool.py      # Shell 执行
│       │   ├── file_tool.py      # 文件读写 + 列目录
│       │   ├── code_tool.py      # Python 代码执行
│       │   ├── system_tool.py    # 系统信息
│       │   ├── memory_tool.py    # 记忆读写
│       │   └── search_tool.py    # 网页搜索（预留）
│       │
│       ├── skills/
│       │   ├── __init__.py
│       │   ├── loader.py         # SKILL.md 解析 + 加载
│       │   └── runner.py         # 脚本执行器
│       │
│       ├── memory/
│       │   ├── __init__.py
│       │   ├── store.py          # 对话持久化 (Session/SessionManager)
│       │   ├── longterm.py       # 长期记忆 (MEMORY.md)
│       │   └── compressor.py     # 上下文压缩
│       │
│       ├── channel/
│       │   ├── __init__.py
│       │   ├── base.py           # Channel 抽象基类
│       │   └── cli.py            # CLI 通道实现
│       │
│       ├── scheduler/
│       │   ├── __init__.py
│       │   └── reminder.py       # 一次性提醒
│       │
│       ├── config/
│       │   ├── __init__.py
│       │   └── settings.py       # YAML 配置加载
│       │
│       └── debug/
│           ├── __init__.py
│           └── logger.py         # 日志 + /debug 命令
│
├── skills/                       # 技能目录
│   └── _example/
│       ├── SKILL.md
│       └── scripts/
│           └── hello.py
│
├── data/                         # 运行时数据（.gitignore）
│   ├── sessions/
│   └── memory/
│       └── MEMORY.md
│
└── tests/                        # 测试（后续补充）
    └── __init__.py
```

---

## 6. CLI 交互设计

```
dotClaw v0.1.0 — 轻量级 AI Agent

命令:
  /new [标题]        新建对话
  /list              列出所有对话
  /switch <id>       切换到指定对话
  /delete <id>       删除对话
  /rename <标题>     重命名当前对话
  /debug             查看最近一次推理过程
  /model <名称>      切换模型
  /skills            列出已加载技能
  /help              显示帮助
  /quit              退出

示例对话:
  >>> 你好
  你好！我是 dotClaw，有什么可以帮你的？

  >>> 帮我看一下当前目录有什么文件
  ⚠️ 即将执行 exec: {"command": "ls -la"}
  确认执行？(y/n): y
  [工具执行结果]
  当前目录有以下文件：
  - src/
  - config.yaml
  - pyproject.toml
  ...

  >>> 20分钟后提醒我开会
  ⏰ 已设置提醒：20分钟后提醒"开会"

  >>> /debug
  ─── 最近一次推理过程 ───
  用户: 帮我看一下当前目录有什么文件
  发送给 LLM 的消息数: 5
  LLM 响应: 1次工具调用 (exec)
  工具调用: exec({"command": "ls -la"})
  工具结果: (324 字符)
  LLM 最终回复: (89 字符)
  Token 消耗: prompt=1234, completion=156
  耗时: 2340ms
  ─────────────────────────
```

---

## 7. 依赖清单

```toml
# pyproject.toml [project.dependencies]

dependencies = [
    "openai>=1.30.0",       # 千问兼容 OpenAI SDK
    "pyyaml>=6.0",          # YAML 配置
    "aiofiles>=23.0",       # 异步文件操作
    "rich>=13.0",           # CLI 美化输出（可选但推荐）
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]
```

---

## 8. 实施计划

### Phase 0: 项目骨架（0.5 天）

**目标：** 能 `pip install -e .` + `python -m dotclaw` 跑起来（什么都不干）

- [ ] 创建 D:\dev\dotClaw\ 完整目录结构
- [ ] pyproject.toml（包名、依赖、入口点）
- [ ] config.yaml 配置文件
- [ ] config/settings.py（YAML 加载 + dataclass）
- [ ] main.py（打印 "dotClaw 启动" 就行）
- [ ] \_\_main\_\_.py
- [ ] .gitignore
- [ ] git init + 首次 commit

**验收：** `pip install -e .` && `python -m dotclaw` 输出欢迎信息

---

### Phase 1: LLM 通信层（1.5 天）

**目标：** 能和千问 API 对话，流式输出

- [ ] llm/base.py — LLMClient 抽象基类 + 数据类（Message, ChatChunk, ToolCall）
- [ ] llm/qwen.py — 千问实现（用 openai SDK，改 base_url）
- [ ] llm/proxy.py — LLMProxy（重试 + fallback 预留）
- [ ] 集成到 main.py：CLI 输入 → 调千问 → 流式输出

**验收：** CLI 里输入问题，千问流式回答，不含工具调用

**async 速成点：** 这个 Phase 会接触 `async/await`、`async for`（流式）、`AsyncIterator`

---

### Phase 2: 工具系统（1.5 天）

**目标：** Agent 能调用工具

- [ ] tools/base.py — ToolRegistry + ToolDefinition + ToolResult
- [ ] tools/exec_tool.py — Shell 执行（asyncio.create_subprocess_shell）
- [ ] tools/file_tool.py — 文件读写 + 列目录
- [ ] tools/system_tool.py — 系统信息
- [ ] tools/approval.py — ApprovalManager
- [ ] 集成到 Agent Loop：LLM 返回 tool_calls → 执行 → 结果喂回

**验收：** 让 Agent 执行 `ls` 命令、读文件，审批能拦截危险操作

---

### Phase 3: Agent Loop 完善（1 天）

**目标：** Agent Loop 完整跑通

- [ ] agent/loop.py — 完整的 while 循环（工具调用链）
- [ ] System prompt 构建（基础版）
- [ ] Skill 描述注入到 system prompt
- [ ] 流式输出中区分文本和工具调用
- [ ] 错误处理（工具执行失败、LLM 格式异常）

**验收：** 多轮对话 + 连续工具调用（如"读 A 文件，把内容写到 B 文件"）

---

### Phase 4: 会话与记忆（1.5 天）

**目标：** 对话持久化 + 多会话 + 长期记忆

- [ ] memory/store.py — Session 数据模型 + SessionManager（JSON 读写）
- [ ] memory/longterm.py — MEMORY.md 读写
- [ ] memory/compressor.py — 上下文压缩（截断 + LLM 摘要）
- [ ] memory_tool.py — 记忆读写工具
- [ ] CLI /new, /list, /switch, /delete 命令

**验收：** 重启程序后能接着聊；多个独立对话；超长对话自动压缩

---

### Phase 5: CLI 完善（0.5 天）

**目标：** CLI 体验打磨

- [ ] channel/base.py — Channel 抽象基类
- [ ] channel/cli.py — CLI 实现（用 rich 美化）
- [ ] /debug, /model, /skills, /help, /quit 命令
- [ ] 重构 main.py 使用 Channel 接口

**验收：** CLI 有颜色、有命令提示、/debug 能看推理过程

---

### Phase 6: Skill 系统（1 天）

**目标：** 能加载和执行 Skill

- [ ] skills/loader.py — SKILL.md 解析（YAML 头部 + 正文）
- [ ] skills/runner.py — 脚本执行器
- [ ] skill 描述注入到 system prompt
- [ ] 创建示例 skill（_example/）
- [ ] 支持加载 skills/ 目录下所有 skill

**验收：** 添加一个 skill 目录后，Agent 自动识别并按 skill 指令执行

---

### Phase 7: 提醒 + 子 Agent + 调试（1 天）

**目标：** 补全剩余功能

- [ ] scheduler/reminder.py — 一次性提醒
- [ ] 注册提醒相关工具（set_reminder, cancel_reminder）
- [ ] agent/sub.py — SubAgent spawn
- [ ] debug/logger.py — TraceRecord + /debug 命令完善
- [ ] code_tool.py — Python 代码执行

**验收：** "20分钟后提醒我"能工作；子 Agent 能跑任务；/debug 能看完整追踪

---

### Phase 8: 收尾（0.5 天）

- [ ] 边界情况处理（空输入、超长输入、网络断连）
- [ ] README.md（项目介绍 + 安装 + 使用）
- [ ] 代码清理 + 类型标注检查
- [ ] 最终 commit + tag v0.1.0

---

## 9. 时间估算

| Phase | 内容 | 天数 |
|-------|------|------|
| 0 | 项目骨架 | 0.5 |
| 1 | LLM 通信层 | 1.5 |
| 2 | 工具系统 | 1.5 |
| 3 | Agent Loop 完善 | 1 |
| 4 | 会话与记忆 | 1.5 |
| 5 | CLI 完善 | 0.5 |
| 6 | Skill 系统 | 1 |
| 7 | 提醒+子Agent+调试 | 1 |
| 8 | 收尾 | 0.5 |
| **合计** | | **9 天** |

预留缓冲 → **约 1.5-2 周**

---

## 10. 面试亮点标注

每个模块在面试时能聊的技术点：

| 模块 | 面试可聊的点 |
|------|-------------|
| Agent Loop | ReAct 模式、工具调用循环、流式处理 |
| LLM Proxy | 代理模式、指数退避重试、降级策略 |
| 工具系统 | JSON Schema 约束、审批机制、沙箱 |
| Skill 系统 | Prompt 注入、自然语言插件 vs 代码插件 |
| 上下文压缩 | 滑动窗口 + 摘要、token 估算、信息保留策略 |
| 多会话 | 会话隔离、持久化、上下文切换 |
| 多 Agent | 任务分解、子进程/协程编排、结果聚合 |
| 流式输出 | SSE、async iterator、实时渲染 |

---

*dotClaw v0.1.0 架构设计 | 2026-04-30*
