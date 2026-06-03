# dotClaw 工具层架构文档

> **版本**: v1.0 | **对应 Phase**: P5 | **更新日期**: 2026-06-03
> **维护说明**: 本文档随工具层演进持续更新，每次架构变更请同步更新版本号和变更说明。

---

## 1. 架构总览

dotClaw 工具层采用 **Registry-Executor-Handler 三层分离架构**，将注册、调度、执行三个关注点完全解耦，为 MCP 集成（Phase 6）和 Skill 工具化（Phase 7）提供统一扩展基础设施。

```
┌─────────────────────────────────────────────────────────────────────┐
│                      AgentLoop                                     │
│    run() → LLM → tool_calls                                       │
│              │                                                     │
│              ▼                                                     │
│    ┌─────────────────────┐                                         │
│    │   ToolExecutor      │  ← 调度层：审批 + 超时 + 错误             │
│    │  .execute(name,     │                                         │
│    │   args, channel)    │                                         │
│    └──┬──────────┬───────┘                                         │
│       │          │                                                  │
│       ▼          ▼                                                  │
│  ┌─────────┐ ┌──────────────┐                                      │
│  │ToolRegistry│ │ApprovalManager│ ← 审批：needs_approval +          │
│  │ 纯注册表   │ │双重门控制      │    approval_commands AND 关系      │
│  └─────┬─────┘ └──────────────┘                                      │
│        │                                                            │
│   ┌────┴────────────┐                                              │
│   ▼     ▼           ▼                                              │
│ Builtin  MCP       Skill                                           │
│ Handler Handler   Handler                                           │
│ (P5)   (reserved) (reserved)                                        │
│   │                                                                 │
│   └── ToolHandler ABC（统一接口）                                    │
│        ├─ definition() → ToolDefinition                            │
│        └─ execute(args, ctx) → ToolResult                          │
│                                                                     │
│  +────────────────────────────────────────+                         │
│  |          builtin/ 子包（内置工具）       |                         │
│  |  exec_tool / file_tool / memory_tool / |                         │
│  |  system_tool — 工厂函数 → BuiltinToolHandler |                   │
│  +────────────────────────────────────────+                         │
│                                                                     │
│  +────────────────────────────────────────+                         │
│  |  ToolProvider ABC（骨架 — MCP/Skill 预留）|                       │
│  |  discover_and_register(registry)       |                         │
│  +────────────────────────────────────────+                         │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. 三层架构数据流

```
AgentLoop.run()
  │
  │  LLM 返回 tool_calls
  ▼
┌──────────────────────────────────────────────────────────┐
│ Layer 3: ToolExecutor（调度层）                          │
│                                                          │
│ execute(name, arguments, channel) → ToolResult           │
│   │                                                      │
│   ├─ 1. registry.get(name) → ToolHandler | None         │
│   │     └─ None → ToolResult(TOOL_NOT_FOUND)            │
│   │                                                      │
│   ├─ 2. handler.definition().needs_approval?            │
│   │     └─ True → approval.check(name, args, channel)   │
│   │           ├─ tool_name in _approval_commands?       │
│   │           │  └─ Yes → channel.ask_user() → y/n     │
│   │           │       ├─ n → ToolResult(APPROVAL_DENIED)│
│   │           │       └─ y → 继续执行                   │
│   │           └─ No → 放行                              │
│   │                                                      │
│   └─ 3. asyncio.wait_for(                              │
│          handler.execute(arguments, ctx),                │
│          timeout=definition.timeout)                     │
│        ├─ 正常返回 → ToolResult                          │
│        ├─ TimeoutError → ToolResult(TIMEOUT)            │
│        └─ Exception → ToolResult(EXECUTION_ERROR)        │
│                                                          │
│ 返回: ToolResult(output, is_error, error_code, error_type)│
└──────────────────────┬───────────────────────────────────┘
                       │
  ┌────────────────────┴────────────────────┐
  │                                         │
  ▼                                         ▼
┌─────────────────────┐          ┌─────────────────────┐
│ Layer 1:            │          │ Layer 2:            │
│ ToolRegistry        │          │ ApprovalManager     │
│ (纯注册表)           │          │ (审批控制)           │
│                     │          │                     │
│ register(handler)   │          │ _approval_commands  │
│ get(name)           │          │   ⊂ config.yaml      │
│ unregister(name)    │          │ check(name,args,ch) │
│ get_definitions()   │          │ set_enabled(bool)   │
│ list_by_source(src) │          │ set_approval_       │
│ clear()             │          │   commands(list)    │
└─────────────────────┘          └─────────────────────┘
```

---

## 3. 模块详解

### 3.1 `ToolDefinition` + `ToolResult` + `ToolExecutionContext` — 基础数据类型

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/tools/base.py` |
| **职责** | 工具层纯数据结构，零外部依赖 |

**`ToolDefinition` — 工具定义**

```python
@dataclass
class ToolDefinition:
    name: str                      # 工具名（唯一标识）
    description: str               # 描述（传给 LLM 生成 tool_calls）
    parameters: dict               # JSON Schema（参数规范）
    source: ToolSource             # BUILTIN | MCP | SKILL | CUSTOM
    needs_approval: bool           # 声明式审批需求
    timeout: float = 60.0          # 执行超时（秒）
    metadata: dict = {}            # 扩展字段
```

> **Phase 5 关键变化**：新增 `source`（来源分类）、`needs_approval`（审批声明）、`timeout`（每工具独立超时）、`metadata`（扩展预留）四个字段。

**`ToolSource` 枚举**：

| 值 | 含义 | Phase |
|----|------|-------|
| `BUILTIN` | 内置工具（exec / file / memory / system） | P5 |
| `MCP` | MCP 协议工具 | P6（预留） |
| `SKILL` | Skill 脚本工具 | P7（预留） |
| `CUSTOM` | 用户自定义工具 | P7+（预留） |

**`ToolResult` — 执行结果**

```python
@dataclass
class ToolResult:
    output: str = ""               # 输出文本（传给 LLM）
    is_error: bool = False         # 是否错误
    error_code: str | None = None  # TOOL_NOT_FOUND | TIMEOUT | APPROVAL_DENIED | EXECUTION_ERROR
    error_type: str | None = None  # not_found | timeout | approval | execution
    metadata: dict = {}            # 扩展字段
```

> **Phase 5 关键变化**：新增 `error_code`（结构化错误码）、`error_type`（错误分类）、`metadata`（扩展字段）。现有逻辑只消费 `output`，向后兼容。

**`ToolExecutionContext` — 运行时上下文**

```python
@dataclass
class ToolExecutionContext:
    timeout: float = 60.0          # 执行超时（来自 ToolDefinition.timeout）
```

> **设计原则**：工具无状态单例——context 只含 timeout，不含 session/workspace 等状态。工具从 arguments 获取所需信息。

---

### 3.2 `ToolHandler` ABC + `BuiltinToolHandler` — 执行抽象

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/tools/handler.py` |
| **职责** | 统一所有工具的执行接口，屏蔽 builtin/MCP/Skill 执行差异 |

**类层次**：

```
ToolHandler (ABC)
  ├─ definition() → ToolDefinition        [抽象]
  ├─ execute(args, ctx) → ToolResult      [抽象]
  └─ name (property) → str               [具体]
        │
        ├── BuiltinToolHandler            [P5 实现]
        │    └─ 包装现有 async 函数，Adapter 模式
        │
        ├── McpToolHandler                [P6 预留]
        │    └─ MCP 协议调用
        │
        └── SkillToolHandler              [P7 预留]
             └─ Skill 脚本执行
```

**`BuiltinToolHandler` 实现细节**：

```python
class BuiltinToolHandler(ToolHandler):
    def __init__(self, name, description, parameters, handler_fn,
                 needs_approval=False, timeout=60.0, metadata=None):
        self._definition = ToolDefinition(
            name=name, description=description, parameters=parameters,
            source=ToolSource.BUILTIN, needs_approval=needs_approval,
            timeout=timeout, metadata=metadata or {}
        )
        self._handler_fn = handler_fn  # 原始 async callable

    async def execute(self, arguments, context=None):
        try:
            result = await self._handler_fn(**arguments)
            return ToolResult(output=str(result))
        except Exception as e:
            return ToolResult(
                output=f"工具执行出错: {e}",
                is_error=True, error_code="EXECUTION_ERROR",
                error_type="execution"
            )
```

> **设计要点**：Adapter 模式——不改变现有 `exec_command()` 等函数签名。超时控制不在 Handler 内做，由 ToolExecutor 统一负责（职责单一）。

---

### 3.3 `ToolRegistry` — 纯注册表

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/tools/registry.py` |
| **职责** | 工具注册、查询、列举，不含执行逻辑 |

**对外方法**：

| 方法 | 输入 | 输出 | 说明 |
|------|------|------|------|
| `register(handler)` | `ToolHandler` | — | 同名静默覆盖（不抛异常、不打警告） |
| `unregister(name)` | `str` | `bool` | 注销成功/失败 |
| `get(name)` | `str` | `ToolHandler \| None` | 按名称获取 |
| `get_definitions()` | — | `list[ToolDefinition]` | 传给 LLM 生成 tool_calls |
| `list_by_source(source)` | `ToolSource` | `list[ToolHandler]` | 按来源过滤 |
| `all_names()` | — | `list[str]` | 所有已注册工具名 |
| `clear()` | — | — | 清空注册表 |

**覆盖策略**：

```
register(handler_A)  →  _handlers["exec"] = handler_A
register(handler_B)  →  _handlers["exec"] = handler_B  # 静默覆盖
```

> **设计要点**：Phase 5 使用静默覆盖——builtin 先注册，后续 MCP/Skill 如有同名需求由那时决定是否加日志。不含 `execute()`——执行职责完全剥离到 ToolExecutor。

---

### 3.4 `ToolExecutor` — 调度层

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/tools/executor.py` |
| **职责** | 工具执行调度：审批检查 + 超时控制 + 错误处理 |

**`execute(name, arguments, channel) → ToolResult` 完整逻辑**：

```
execute(name, arguments, channel)
  │
  ├─ 1. Registry 查找
  │     handler = registry.get(name)
  │     └─ None → TOOL_NOT_FOUND
  │
  ├─ 2. 审批检查（双重门）
  │     if handler.definition().needs_approval:       ← 第一道门（工具声明）
  │         if not await approval.check(name, args, channel):  ← 第二道门（用户配置）
  │             return APPROVAL_DENIED
  │
  ├─ 3. 超时控制
  │     ctx = ToolExecutionContext(timeout=definition.timeout)
  │     result = await asyncio.wait_for(
  │         handler.execute(arguments, ctx),
  │         timeout=definition.timeout
  │     )
  │     ├─ 正常返回 → result
  │     ├─ asyncio.TimeoutError → TIMEOUT
  │     └─ Exception → EXECUTION_ERROR
  │
  └─ 返回 ToolResult
```

**对外扩展方法**：

| 方法 | 说明 |
|------|------|
| `get_definitions()` | 转发 `registry.get_definitions()`，供 AgentLoop._build_context() 使用 |
| `get_handler(name)` | 转发 `registry.get(name)`，供 /tools 命令读取审批标记 |

> **设计要点**：超时直接杀（`asyncio.wait_for` 超时后 cancel task），子进程由 Handler 内部负责 kill（`exec_tool` 已有 `proc.kill()` + `CancelledError` 防护）。不含 MCP/HTTP 超时——MCP Handler 内部自行处理，此处只控制整体执行超时。

---

### 3.5 `ApprovalManager` — 审批控制

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/tools/approval.py` |
| **职责** | 危险工具执行前用户确认，双重门控制 |

**双重门审批模型**：

```
                    ToolDefinition.needs_approval
                              │
                    ┌─────────┴─────────┐
                    │ True              │ False
                    ▼                   ▼
              approval_commands      直接执行（不审批）
                    │
              ┌─────┴─────┐
              │ 在列表中   │ 不在列表中
              ▼           ▼
          用户确认      直接执行
          (y/n)         （放行）
              │
        ┌─────┴─────┐
        │ y / yes   │ n / 其他
        ▼           ▼
       执行      APPROVAL_DENIED
```

> AND 关系：`needs_approval=True` **且** `tool_name in approval_commands` 才触发用户确认。任一条件不满足则放行。

**对外方法**：

| 方法 | 说明 |
|------|------|
| `__init__(approval_commands)` | 从 config 加载审批命令列表 |
| `set_enabled(bool)` | 全局启用/禁用审批 |
| `set_approval_commands(list)` | 热更新审批列表（无需重启） |
| `async check(tool_name, args, channel) → bool` | 执行审批检查 |

**`check()` 逻辑**：

```python
async def check(self, tool_name, arguments, channel) -> bool:
    if not self._enabled:          # 全局禁用 → 放行
        return True
    if tool_name not in self._approval_commands:  # 不在列表中 → 放行
        return True
    if channel is None:            # 无 channel（子 Agent）→ 放行
        return True
    confirm = await channel.ask_user(f"确认执行 `{tool_name}`？(y/n): ")
    return confirm.strip().lower() in ("y", "yes")
```

> **Phase 5 关键变化**：删除硬编码 `NEEDS_APPROVAL = {"exec", "python"}`，改用 `_approval_commands` 集合从 config.yaml 加载。

---

### 3.6 `builtin/` — 内置工具子包

| 属性 | 值 |
|------|-----|
| **文件夹** | `src/dotclaw/tools/builtin/` |
| **职责** | 内置工具实现 + 工厂函数 + 统一注册入口 |

**目录结构**：

```
builtin/
├── __init__.py        ← register_all(registry) 统一注册 8 个工具
├── exec_tool.py       ← get_exec_handler()       [needs_approval=True]
├── file_tool.py       ← get_read_file_handler()  [needs_approval=False]
│                        get_write_file_handler() [needs_approval=True]
│                        get_list_dir_handler()   [needs_approval=False]
├── memory_tool.py     ← get_memory_read_handler() [needs_approval=False]
│                        get_memory_write_handler()[needs_approval=True]
└── system_tool.py     ← get_system_info_handler() [needs_approval=False]
                         get_time_handler()        [needs_approval=False]
```

**工厂函数模式**（以 `exec_tool.py` 为例）：

```python
async def exec_command(command: str) -> str:
    # 原有实现，通过 asyncio.create_subprocess_shell 执行
    ...

def get_exec_handler() -> BuiltinToolHandler:
    return BuiltinToolHandler(
        name="exec",
        description="执行一条 Shell 命令。危险操作，执行前需用户确认。",
        parameters={"type": "object", "properties": {...}, "required": ["command"]},
        handler_fn=exec_command,
        needs_approval=True,   # 显式声明危险
        timeout=60.0,
    )
```

**注册入口**：

```python
# builtin/__init__.py
def register_all(registry: ToolRegistry) -> None:
    """在 main.py 启动时调用"""
    handlers = [
        get_exec_handler(),
        get_read_file_handler(), get_write_file_handler(), get_list_dir_handler(),
        get_memory_read_handler(), get_memory_write_handler(),
        get_system_info_handler(), get_time_handler(),
    ]
    for handler in handlers:
        registry.register(handler)
```

**内置工具审批状态一览**：

| 工具名 | `needs_approval` | 说明 |
|--------|------------------|------|
| exec | True | Shell 命令执行，高风险 |
| read_file | False | 只读操作，低风险 |
| write_file | True | 文件写入，中风险 |
| list_dir | False | 只读操作，低风险 |
| memory_read | False | 只读操作，低风险 |
| memory_write | True | 写入记忆，中风险 |
| system_info | False | 只读操作，低风险 |
| get_time | False | 只读操作，低风险 |

**W1 修复 — CancelledError 防护**（`exec_tool.py`）：

```python
async def exec_command(command: str) -> str:
    proc = None
    try:
        proc = await asyncio.create_subprocess_shell(command, ...)
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            ...
        except asyncio.TimeoutError:
            proc.kill(); await proc.wait()
            return "命令执行超时"
        except asyncio.CancelledError:
            # ToolExecutor 超时 cancel → 必须 kill 子进程
            proc.kill(); await proc.wait()
            raise  # 重新抛出，让 ToolExecutor 正常捕获
    except asyncio.CancelledError:
        if proc is not None:
            proc.kill(); await proc.wait()
        raise
    except Exception as e:
        return f"错误：{e}"
```

**W2 修复 — 文件大小限制**（`file_tool.py`）：

```python
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

async def read_file(path: str) -> str:
    ...
    if file_path.stat().st_size > MAX_FILE_SIZE:
        return f"错误：文件过大（{file_path.stat().st_size} bytes），超过限制 {MAX_FILE_SIZE} bytes"
    ...
```

---

### 3.7 `ToolProvider` ABC — 外部工具注入接口

| 属性 | 值 |
|------|-----|
| **文件** | `src/dotclaw/tools/provider.py` |
| **职责** | 为 MCP/Skill/Custom 工具源预留标准注入入口（Phase 5 仅骨架） |

```python
class ToolProvider(ABC):
    @abstractmethod
    async def discover_and_register(self, registry: ToolRegistry) -> list[str]:
        """发现工具并注册到 registry。返回注册的工具名称列表。"""
        ...
```

**预留实现**：

| 类 | Phase | 说明 |
|----|-------|------|
| `MCPToolProvider` | P6 | MCP 协议：`initialize` → `tools/list` → 注册 → `tools/call` |
| `SkillToolProvider` | P7 | Skill 脚本：解析 SKILL.md → 生成 ToolHandler → 注册 |

**启动时调用**（`main.py` 预留模式）：

```python
# Phase 6 启动流程
if config.tools.mcp_enabled:
    mcp_provider = MCPToolProvider(config.mcp_servers)
    await mcp_provider.discover_and_register(tool_registry)
```

---

## 4. 完整调用链路时序图

### 4.1 正常工具调用 + 审批通过

```
用户             AgentLoop          ToolExecutor      ToolRegistry    ApprovalMgr   BuiltinToolHandler    OS
 │                  │                    │                │               │               │              │
 │── "列出文件" ──►│                    │                │               │               │              │
 │                  ├─ LLM.chat() ─────────────────────────────────────────────────────────────────────│
 │                  │                    │                │               │               │              │
 │                  │◄─ tool_call: list_dir(path=".")                                                   │
 │                  │                    │                │               │               │              │
 │                  ├─ execute("list_dir", {"path":"."}, channel) ─►                                    │
 │                  │                    │                │               │               │              │
 │                  │                    ├─ get("list_dir")──►                                           │
 │                  │                    │◄─── handler ──────│               │               │              │
 │                  │                    │                │               │               │              │
 │                  │                    ├─ handler.definition().needs_approval?                         │
 │                  │                    │  = False → 跳过审批                                           │
 │                  │                    │                │               │               │              │
 │                  │                    ├─ asyncio.wait_for(handler.execute({...}), timeout=10)        │
 │                  │                    │                │               │               │              │
 │                  │                    │                │               │               ├─ list_dir(".")│
 │                  │                    │                │               │               │── iterdir ─►│
 │                  │                    │                │               │               │◄── 结果 ────│
 │                  │                    │                │               │               │              │
 │                  │                    │◄── ToolResult(output="file1\nfile2\n...")                      │
 │                  │                    │                │               │               │              │
 │◄── "文件列表..." ──┤                    │                │               │               │              │
 │                  │                    │                │               │               │              │
 │                  ├─ 发送 tool result 给 LLM 继续对话                                                 │
```

### 4.2 危险工具审批拒绝

```
用户             AgentLoop          ToolExecutor      ToolRegistry    ApprovalMgr         用户终端
 │                  │                    │                │               │                 │
 │── "删除文件" ──►│                    │                │               │                 │
 │                  ├─ LLM.chat() → tool_call: exec("rm -rf /")                                   │
 │                  │                    │                │               │                 │
 │                  ├─ execute("exec", {"command":"rm -rf /"}, channel) ─►                        │
 │                  │                    │                │               │                 │
 │                  │                    ├─ get("exec") → handler                                 │
 │                  │                    │                │               │                 │
 │                  │                    ├─ handler.definition().needs_approval = True            │
 │                  │                    │                │               │                 │
 │                  │                    ├─ check("exec", {...}, channel) ─►                      │
 │                  │                    │                │               │                 │
 │                  │                    │                │               ├─ "exec" in _approval_commands? → Yes
 │                  │                    │                │               │                 │
 │                  │                    │                │               ├─ ask_user("确认执行?")──►
 │                  │                    │                │               │                 │    │
 │                  │                    │                │               │◄── "n" ───────────│
 │                  │                    │                │               │                 │
 │                  │                    │◄── False ──────│               │                 │
 │                  │                    │                │               │                 │
 │                  │                    ├─ return ToolResult(APPROVAL_DENIED)                     │
 │                  │                    │                │               │                 │
 │                  ├─ 发送 "用户拒绝了 exec 的执行" 给 LLM                                        │
```

### 4.3 工具执行超时

```
AgentLoop           ToolExecutor         asyncio.wait_for     BuiltinToolHandler       OS
 │                      │                     │                     │                   │
 ├─ execute("exec", {"command":"sleep 100"}, channel) ─►           │                   │
 │                      │                     │                     │                   │
 │                      ├─ get("exec") → handler                                        │
 │                      ├─ definition.timeout = 60                                      │
 │                      │                     │                     │                   │
 │                      ├─ wait_for(handler.execute(...), timeout=60) ─►               │
 │                      │                     │                     │                   │
 │                      │                     │                     ├─ create_subprocess("sleep 100")
 │                      │                     │                     │── fork ──────►│
 │                      │                     │                     │               │  (running)
 │                      │                     │  ... 60 seconds ... │               │
 │                      │                     │                     │               │
 │                      │                     │  ◄─── TimeoutError ──│               │
 │                      │                     │       (cancel task)  │               │
 │                      │                     │                     │               │
 │                      │                     │  CancelledError → handler              │
 │                      │                     │                     ├─ proc.kill() ──►│
 │                      │                     │                     ├─ proc.wait() ◄──│
 │                      │                     │                     ├─ raise ────────►│
 │                      │                     │                     │                   │
 │                      │◄── TimeoutError ────│                     │                   │
 │                      │                     │                     │                   │
 │                      ├─ return ToolResult(TIMEOUT, "工具执行超时（60秒）")            │
 │                      │                     │                     │                   │
 ├─ 发送超时结果给 LLM                       │                     │                   │
```

---

## 5. 配置参考

`config.yaml` 中的 `tools:` 段：

```yaml
tools:
  # === source 级启停（Phase 5 新增） ===
  builtin_enabled: true              # 内置工具总开关
  mcp_enabled: true                  # MCP 工具（P6 启用）
  skill_enabled: true                # Skill 工具（P7 启用）

  # === 审批控制 ===
  approval_commands:                 # 需要用户确认的工具列表
    - exec
    - python

  # === 单工具禁用（向后兼容旧配置） ===
  disabled_tools: []                 # 如 ["exec"] 禁用 exec 工具

  # === exec 工具 ===
  exec_timeout: 60                   # 秒（浮点数）

  # === web 工具 ===
  web_search:
    enabled: false
```

**向后兼容**：

旧格式 `tools.exec.needs_approval: true` 和 `tools.exec.enabled: false` 在 `_raw_to_config()` 中自动转换为新格式：
- `exec.needs_approval: true` → `approval_commands: ["exec"]`
- `exec.enabled: false` → `disabled_tools: ["exec"]`
- `python.timeout: 30` → `exec_timeout: 30`
- 混合格式正确合并（不丢数据）

---

## 6. 初始化链路（`main.py`）

```
main.py / _run_cli()
  │
  ├─ 1. load_config() → Config 对象
  │     └─ ToolsConfig（builtin_enabled, mcp_enabled, skill_enabled,
  │                     approval_commands, disabled_tools, exec_timeout）
  │
  ├─ 2. ToolRegistry()
  │
  ├─ 3. [if config.tools.builtin_enabled]
  │     └─ builtin.register_all(tool_registry)
  │          ├─ get_exec_handler()         → BuiltinToolHandler("exec")
  │          ├─ get_read_file_handler()    → BuiltinToolHandler("read_file")
  │          ├─ get_write_file_handler()   → BuiltinToolHandler("write_file")
  │          ├─ get_list_dir_handler()     → BuiltinToolHandler("list_dir")
  │          ├─ get_memory_read_handler()  → BuiltinToolHandler("memory_read")
  │          ├─ get_memory_write_handler() → BuiltinToolHandler("memory_write")
  │          ├─ get_system_info_handler()  → BuiltinToolHandler("system_info")
  │          └─ get_time_handler()         → BuiltinToolHandler("get_time")
  │
  ├─ 4. [根据 disabled_tools 注销工具]
  │     for tool_name in config.tools.disabled_tools:
  │         tool_registry.unregister(tool_name)
  │
  ├─ 5. ApprovalManager(approval_commands=config.tools.approval_commands)
  │
  ├─ 6. ToolExecutor(registry=tool_registry, approval_manager=approval_mgr)
  │
  ├─ 7. AgentLogger(level=config.debug.level, log_file=config.debug.log_file)
  │     └─ TraceRecord + _setup_logging（Phase 5 合并 DebugManager 能力）
  │
  ├─ 8. AgentLoop(tool_executor=tool_executor, logger=agent_logger, ...)
  │
  └─ 9. 主循环：await channel.receive() → await agent.run(user_input)
        │
        └─ AgentLoop.run()
             └─ tool_executor.execute(name, args, channel)
```

---

## 7. 扩展预留（Phase 6/7 对接点）

| 对接点 | Phase 5 现状 | Phase 6/7 动作 |
|--------|-------------|---------------|
| `ToolHandler` ABC | 已定义 `definition()` + `execute()` | P6 实现 `McpToolHandler`；P7 实现 `SkillToolHandler` |
| `ToolRegistry` | 纯注册表（register/get/unregister） | P6/P7 通过 `register()` 动态添加 MCP/Skill 工具 |
| `ToolProvider` ABC | 已定义 `discover_and_register()` 接口 | P6 实现 `MCPToolProvider`；P7 实现 `SkillToolProvider` |
| `ToolSource.MCP` | 枚举值已定义 | P6 使用 |
| `ToolSource.SKILL` | 枚举值已定义 | P7 使用 |
| `config.tools.mcp_enabled` | 已预留字段 | P6 初始化时检查 |
| `config.tools.skill_enabled` | 已预留字段 | P7 初始化时检查 |
| `ApprovalManager.set_approval_commands()` | 已实现热更新 | P6 MCP 工具可动态加入审批列表 |
| `disabled_tools` → `unregister()` | 已实现管道 | P6 MCP 工具也可通过此机制禁用 |

---

*本文档由 dotClaw 开发工程师维护。架构变更后请同步更新此文档。*
开发日志见 `docs/phase5-record.md`。详细设计见 `docs/phase5-roadmap.md`。
