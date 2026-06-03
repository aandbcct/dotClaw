# Phase 5 详细开发文档：工具层架构重构

> 创建时间：2026-06-02
> 状态：已完成 ✅（2026-06-03）
> 依赖：Phase 1-4 已完成
> 变更日志：[docs/phase5-record.md](./phase5-record.md)

---

## 一、开发目的

Phase 5 是工具层的架构重构，为后续 MCP 集成（Phase 6+）和 Skill 工具化（Phase 7+）提供统一的注册、调度和管理基础设施。

**核心目标**：
1. 解耦工具注册与执行——ToolRegistry 纯注册，ToolExecutor 负责调度+审批+超时
2. 统一工具抽象——ToolHandler ABC 屏蔽 builtin/MCP/Skill 执行差异
3. 支持外部工具注入——ToolProvider ABC 为 MCP/Skill 预留标准注入入口
4. 结构化错误处理——ToolResult 扩展 error_code/error_type 等字段
5. 可维护的审批机制——config.yaml approval_commands + ToolDefinition.needs_approval 双重控制

**设计原则**：
- 工具无状态单例——ToolExecutionContext 只传 timeout，不含 session/workspace 等状态
- 后注册覆盖——同名工具后注册的覆盖先注册的，不使用命名空间前缀
- source 级启停——config.yaml 按 builtin_enabled/mcp_enabled/skill_enabled 控制
- 最小改动 AgentLoop——仅 _tool_registry.execute() -> _tool_executor.execute()，无其他侵入

---

## 二、架构总览

```
+----------------------------------------------------------+
|                      AgentLoop                           |
|  run() -> _build_messages() -> LLM -> tool_calls        |
|            |                                             |
|            v                                             |
|    ToolExecutor.execute(name, args, channel)             |
|            |                                             |
|    +-------+----------------------------+               |
|    |  1. registry.get(name) -> handler  |               |
|    |  2. approval.check(definition)     |               |
|    |  3. handler.execute(args, ctx)     |               |
|    |  4. timeout + error handling       |               |
|    +-------+----------------------------+               |
|            |                                             |
|            v                                             |
|       ToolResult (structured)                            |
+------+---------------------------------------------------+
       |
       v
+------------------------------+
|        ToolRegistry          |
|  pure registry: register /   |
|  get / list                  |
+------+-----------------------+
       |
  +----+----------+
  v    v          v
Builtin MCP     Skill
Handler Handler Handler
(P5)   (reserved) (reserved)
  |
  +-- exec_tool.py
  +-- file_tool.py
  +-- memory_tool.py
  +-- system_tool.py
       |
       v
  ToolProvider ABC (reserved)
  +-- MCPToolProvider    (Phase 6+)
  +-- SkillToolProvider  (Phase 7+)
```

**数据通路**：

```
启动阶段：
  main.py
    +-- ToolRegistry() -> register builtin handlers
    +-- ToolExecutor(registry, approval_mgr)
    +-- AgentLoop(tool_executor=...)

运行时：
  AgentLoop.run()
    +-- LLM returns tool_calls
         +-- for each tool_call:
              tool_executor.execute(name, args, channel)
                +-- registry.get(name) -> handler
                +-- approval.check(handler.definition)
                +-- handler.execute(args, ToolExecutionContext(timeout=...))
                +-- return ToolResult
```

---

## 三、模块层级与依赖关系

Phase 5 修改和新增的模块按依赖关系分为 5 层。

```
Layer 1: ToolDefinition + ToolResult + ToolExecutionContext  <- 纯数据结构，零依赖
   |
Layer 2: ToolHandler ABC + BuiltinToolHandler                <- 依赖 Layer 1
   |
Layer 3: ToolRegistry                                        <- 纯注册表，依赖 ToolHandler
   |
Layer 4: ToolExecutor + ApprovalManager                      <- 调度层，依赖 Registry + Approval
   |
Layer 5: AgentLoop + main.py (修改)                          <- 集成新架构
```

---

## 四、各模块开发要点

### 4.1 tools/base.py — 重构（修改）

**现状**：ToolDefinition + ToolResult + 全局 _registry + register_tool 装饰器 + ToolRegistry（含执行）

**Phase 5 改造**：

```python
# tools/base.py

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("dotclaw.tools")


class ToolSource(str, Enum):
    BUILTIN = "builtin"
    MCP = "mcp"
    SKILL = "skill"
    CUSTOM = "custom"


@dataclass
class ToolDefinition:
    """工具定义（增强版）"""
    name: str
    description: str
    parameters: dict = field(default_factory=dict)   # JSON Schema
    source: ToolSource = ToolSource.BUILTIN
    needs_approval: bool = False                    # 声明式审批需求
    timeout: float = 60.0                          # 执行超时（秒）
    metadata: dict = field(default_factory=dict)     # 扩展字段


@dataclass
class ToolResult:
    """工具执行结果（结构化扩展）"""
    output: str = ""
    is_error: bool = False
    error_code: str | None = None      # 如 TIMEOUT / PROCESS_ERROR / HTTP_ERROR
    error_type: str | None = None      # 如 timeout / execution / parsing
    metadata: dict = field(default_factory=dict)  # 扩展字段（如 MCP error detail）


@dataclass
class ToolExecutionContext:
    """工具执行时的运行时上下文（最小集）"""
    timeout: float = 60.0              # 执行超时，来自 ToolDefinition.timeout
```

**关键变化**：
- 删除全局 _registry 和 register_tool 装饰器（迁移到 builtin/ 子包用显式注册）
- ToolDefinition 新增 source/needs_approval/timeout/metadata
- ToolResult 新增 error_code/error_type/metadata
- 新增 ToolExecutionContext（目前只含 timeout）
- 文件不再包含 ToolRegistry 类（拆到 registry.py）

---

### 4.2 tools/handler.py — 新增

**职责**：定义 ToolHandler ABC，统一所有工具的执行接口。

```python
# tools/handler.py

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Callable, Awaitable

from .base import ToolDefinition, ToolExecutionContext, ToolResult


class ToolHandler(ABC):
    """工具执行器的统一抽象接口"""

    @abstractmethod
    def definition(self) -> ToolDefinition:
        """返回工具定义"""
        ...

    @abstractmethod
    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        """执行工具，返回结构化结果"""
        ...

    @property
    def name(self) -> str:
        return self.definition().name


class BuiltinToolHandler(ToolHandler):
    """内置工具适配器——将现有异步函数包装为 ToolHandler"""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler_fn: Callable[..., Awaitable[Any]],
        needs_approval: bool = False,
        timeout: float = 60.0,
        metadata: dict | None = None,
    ):
        self._definition = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            source=ToolSource.BUILTIN,
            needs_approval=needs_approval,
            timeout=timeout,
            metadata=metadata or {},
        )
        self._handler_fn = handler_fn

    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        try:
            result = await self._handler_fn(**arguments)
            return ToolResult(output=str(result))
        except Exception as e:
            logger.exception(f"工具 {self._definition.name} 执行出错")
            return ToolResult(
                output=f"工具执行出错: {e}",
                is_error=True,
                error_code="EXECUTION_ERROR",
                error_type="execution",
            )
```

**设计要点**：
- BuiltinToolHandler 是 Adapter 模式——不改变现有 exec_tool.py 等的函数签名
- 超时控制不在 Handler 内做，由 ToolExecutor 统一负责（职责单一）
- MCP/Skill Handler 后续只需实现 ToolHandler ABC，不需要动 BuiltinToolHandler

---

### 4.3 tools/registry.py — 新增

**职责**：纯注册表，只负责工具的注册、查询、列举，不含执行逻辑。

```python
# tools/registry.py

from __future__ import annotations
from typing import Any

from .base import ToolSource
from .handler import ToolHandler


class ToolRegistry:
    """纯工具注册表——只注册和查询，不执行"""

    def __init__(self):
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, handler: ToolHandler) -> None:
        """注册工具。同名后注册覆盖前注册（静默覆盖）。"""
        self._handlers[handler.name] = handler

    def unregister(self, name: str) -> bool:
        """注销工具，返回是否成功删除。"""
        if name in self._handlers:
            del self._handlers[name]
            return True
        return False

    def get(self, name: str) -> ToolHandler | None:
        """按名称获取工具 Handler。"""
        return self._handlers.get(name)

    def get_definitions(self) -> list:
        """返回所有工具定义，用于传给 LLM 生成 tool_calls。"""
        return [h.definition() for h in self._handlers.values()]

    def list_by_source(self, source: ToolSource) -> list:
        """按来源列举工具。"""
        return [h for h in self._handlers.values() if h.definition().source == source]

    def all_names(self) -> list[str]:
        return list(self._handlers.keys())

    def clear(self) -> None:
        self._handlers.clear()
```

**设计要点**：
- 同名覆盖：不抛异常、不打警告——Phase 5 由 builtin 先注册，后续 Phase 6/7 覆盖场景由那时决定是否需要警告
- 不含 execute()——执行职责完全剥离到 ToolExecutor
- unregister() 为后续热加载预留

---

### 4.4 tools/executor.py — 新增

**职责**：工具执行调度层——从 Registry 取 Handler，做审批检查、超时控制、错误处理，返回 ToolResult。

**审批完整流程**：

```
1. ToolExecutor 检查 ToolDefinition.needs_approval
   - False → 跳过审批，直接执行
   - True  → 进入步骤 2
2. ApprovalManager.check() 检查 tool_name in approval_commands
   - 不在列表中 → 放行（信任工具不会被误标危险）
   - 在列表中   → 进入步骤 3
3. 通过 channel.ask_user() 请求用户确认
   - y/yes → 执行
   - 其他  → 返回 APPROVAL_DENIED
```

> 两道门的关系：`needs_approval` 是工具声明自己危险，`approval_commands` 是用户配置哪些工具需要确认。两者 AND 关系——必须 needs_approval=True **且** 在 approval_commands 列表中，才会触发用户确认。Phase 5 中 needs_approval 和 approval_commands 均已生效——needs_approval 是工具声明，approval_commands 是用户配置。

```python
# tools/executor.py

from __future__ import annotations
import asyncio
import logging
from typing import Any

from .base import ToolDefinition, ToolExecutionContext, ToolResult
from .handler import ToolHandler
from .registry import ToolRegistry
from .approval import ApprovalManager

logger = logging.getLogger("dotclaw.tools.executor")


class ToolExecutor:
    """工具执行调度器——审批 + 超时 + 错误处理"""

    def __init__(
        self,
        registry: ToolRegistry,
        approval_manager: ApprovalManager | None = None,
    ):
        self._registry = registry
        self._approval = approval_manager or ApprovalManager()

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        channel: Any | None = None,
    ) -> ToolResult:
        handler = self._registry.get(name)
        if not handler:
            return ToolResult(
                output=f"错误：未找到工具 '{name}'",
                is_error=True,
                error_code="TOOL_NOT_FOUND",
                error_type="not_found",
            )

        definition = handler.definition()

        # 审批检查
        if definition.needs_approval:
            approved = await self._approval.check(
                tool_name=name,
                arguments=arguments,
                channel=channel,
            )
            if not approved:
                return ToolResult(
                    output=f"用户拒绝了 {name} 的执行",
                    is_error=True,
                    error_code="APPROVAL_DENIED",
                    error_type="approval",
                )

        # 超时控制 + 执行
        timeout = definition.timeout
        ctx = ToolExecutionContext(timeout=timeout)

        try:
            result = await asyncio.wait_for(
                handler.execute(arguments, ctx),
                timeout=timeout,
            )
            return result
        except asyncio.TimeoutError:
            logger.warning(f"工具 {name} 执行超时（{timeout}s）")
            return ToolResult(
                output=f"错误：工具执行超时（{timeout}秒）",
                is_error=True,
                error_code="TIMEOUT",
                error_type="timeout",
            )
        except Exception as e:
            logger.exception(f"工具 {name} 调度出错")
            return ToolResult(
                output=f"工具调度出错: {e}",
                is_error=True,
                error_code="EXECUTION_ERROR",
                error_type="execution",
            )
```

**设计要点**：
- 超时直接杀（asyncio.wait_for 超时后 cancel task，子进程由 Handler 内部负责 kill）
- 审批通过 definition.needs_approval 驱动，config 的 approval_commands 在 ApprovalManager 内消费
- 不含 MCP/HTTP 超时——MCP Handler 内部自行处理 HTTP timeout，此处只控制整体执行超时

---

### 4.5 tools/approval.py — 重构（修改）

**现状**：硬编码 NEEDS_APPROVAL = {"exec", "python"}

**Phase 5 改造**：

```python
# tools/approval.py

from __future__ import annotations
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..channel.base import Channel


class ApprovalManager:
    """
    危险工具执行前需要用户确认。

    审批策略（双重）：
    1. ToolDefinition.needs_approval 声明式（工具自己声明）
    2. config.tools.approval_commands 列表（用户配置覆盖）
    """

    def __init__(self, approval_commands: list[str] | None = None):
        self._enabled = True
        self._approval_commands = set(approval_commands or [])

    def set_enabled(self, enabled: bool):
        self._enabled = enabled

    def set_approval_commands(self, commands: list[str]):
        """从 config.yaml 加载需要审批的命令列表"""
        self._approval_commands = set(commands)

    async def check(
        self,
        tool_name: str,
        arguments: dict,
        channel: "Channel | None" = None,
    ) -> bool:
        """
        检查工具是否需要审批。

        逻辑：
        1. _enabled=False -> 全部放行
        2. tool_name 在 _approval_commands 中 -> 需要审批
        3. 否则放行
        """
        if not self._enabled:
            return True

        if tool_name not in self._approval_commands:
            return True

        if channel is None:
            # 无 channel 时默认放行（子 Agent 场景）
            return True

        # 通过 channel 向用户请求确认
        args_str = json.dumps(arguments, ensure_ascii=False, indent=2)
        confirm = await channel.ask_user(
            f"即将执行危险工具 `{tool_name}\n"
            f"参数：{args_str}\n"
            f"确认执行？(y/n): "
        )
        return confirm.strip().lower() in ("y", "yes")
```

**关键变化**：
- 删除硬编码 NEEDS_APPROVAL
- 新增 _approval_commands 集合，从 config.yaml 加载
- `NEEDS_APPROVAL` 常量已确认无外部引用（grep 确认），可直接删除
- needs_approval 字段在 ToolDefinition 上声明，但实际审批触发统一在 ApprovalManager.check() 内判断
- 注：如果后续需要 needs_approval 字段独立生效（不经过 config 列表），可以在 check() 中同时判断 definition.needs_approval

---

### 4.6 tools/builtin/ — 新增子包

**职责**：将现有 exec_tool.py/file_tool.py/memory_tool.py/system_tool.py 迁移到 builtin/ 子包，并注册为 BuiltinToolHandler。

目录结构：
```
tools/
+-- __init__.py          # 导出 ToolRegistry, ToolExecutor 等
+-- base.py              # ToolDefinition, ToolResult, ToolExecutionContext
+-- handler.py           # ToolHandler ABC, BuiltinToolHandler
+-- registry.py          # ToolRegistry
+-- executor.py          # ToolExecutor
+-- approval.py          # ApprovalManager（改造后）
+-- builtin/             # 内置工具子包
|   +-- __init__.py     # register_all() -> 统一注册入口
|   +-- exec_tool.py    # 原 tools/exec_tool.py（几乎不改）
|   +-- file_tool.py    # 原 tools/file_tool.py（几乎不改）
|   +-- memory_tool.py  # 原 tools/memory_tool.py（几乎不改）
|   +-- system_tool.py  # 原 tools/system_tool.py（几乎不改）
+-- provider.py          # ToolProvider ABC（预留，Phase 5 不实现）
```

builtin/__init__.py 内容：

```python
# tools/builtin/__init__.py

from __future__ import annotations
from .exec_tool import get_exec_handler
from .file_tool import get_read_file_handler, get_write_file_handler, get_list_dir_handler
from .memory_tool import get_memory_read_handler, get_memory_write_handler
from .system_tool import get_system_info_handler, get_time_handler


def register_all(registry):
    """
    注册所有内置工具到注册表。
    在 main.py 启动时调用。
    """
    handlers = [
        get_exec_handler(),
        get_read_file_handler(),
        get_write_file_handler(),
        get_list_dir_handler(),
        get_memory_read_handler(),
        get_memory_write_handler(),
        get_system_info_handler(),
        get_time_handler(),
    ]
    for handler in handlers:
        registry.register(handler)
```

每个内置工具文件新增一个工厂函数，返回配置好的 BuiltinToolHandler：

```python
# tools/builtin/exec_tool.py

from dotclaw.tools.handler import BuiltinToolHandler

# 保留原有 exec_command 函数不变
async def exec_command(command: str) -> str:
    ...  # 原有实现不变

def get_exec_handler() -> BuiltinToolHandler:
    return BuiltinToolHandler(
        name="exec",
        description="执行一条 Shell 命令。危险操作，执行前需用户确认。",
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的命令",
                }
            },
            "required": ["command"],
        },
        handler_fn=exec_command,
        needs_approval=True,
        timeout=60.0,
    )
```

---

### 4.7 tools/provider.py — 新增（骨架，不实现）

**职责**：定义 ToolProvider ABC，为 MCP/Skill 的工具注入预留标准接口。

```python
# tools/provider.py

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class ToolProvider(ABC):
    """
    工具源抽象接口。

    MCP/Skill/Custom 各自实现 discover_and_register()，
    在 main.py 启动时调用，将工具注册到 ToolRegistry。

    Phase 5 只定义接口，不实现具体 Provider。
    """

    @abstractmethod
    async def discover_and_register(self, registry: "ToolRegistry") -> list[str]:
        """
        发现工具并注册到 registry。
        返回本次注册的工具名称列表。
        """
        ...
```

Phase 5 不实现的内容：
- MCPToolProvider — Phase 6+ 实现
- SkillToolProvider — Phase 7+ 实现
- CustomToolProvider — 用户自定义工具注入，Phase 7+ 考虑

---

### 4.8 debug/logger.py — 删除 + agent/logger.py — 修改（日志合并）

**现状（P3 遗留技术债）**：
- `debug/logger.py` 包含 TraceRecord + DebugManager，负责记录追踪和 /debug 命令
- `agent/logger.py` 包含 AgentLogger，负责 request_id 全链路追踪，内部 import TraceRecord
- AgentLoop 同时持有 `_debug_manager`（DebugManager）和 `_logger`（AgentLogger），双向同步
- /debug 命令通过 `agent.debug_trace(channel)` -> `_debug_manager.get_last_trace()` 工作

**Phase 5 改造**：将 DebugManager 的 TraceRecord + get_last_trace 能力合并到 AgentLogger，删除 debug/ 子包。

```python
# agent/logger.py（修改后）

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

logger = logging.getLogger("dotclaw.agent")


@dataclass
class TraceRecord:
    """一次完整推理的追踪记录（从 debug/logger.py 迁移）"""
    timestamp: str
    session_id: str
    user_message: str
    messages_sent: list[dict] = field(default_factory=list)
    llm_responses: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    final_response: str = ""
    duration_ms: int = 0

    def format_summary(self) -> str:
        """格式化摘要（供 /debug 命令展示）"""
        lines = [
            "─── 最近一次推理过程 ───",
            f"用户: {self.user_message[:80]}",
            f"LLM 响应次数: {len(self.llm_responses)}",
            f"工具调用: {len(self.tool_calls)} 次",
        ]
        if self.tool_calls:
            for tc in self.tool_calls[:3]:
                lines.append(f"  - {tc.get('name', '?')}({str(tc.get('arguments', ''))[:40]})")
        if self.final_response:
            lines.append(f"最终回复: {self.final_response[:80]}")
        lines.append(f"耗时: {self.duration_ms}ms")
        lines.append("──" * 10)
        return "\n".join(lines)


class AgentLogger:
    """
    结构化日志系统（合并 DebugManager 能力后）。

    合并内容：
    - TraceRecord 从 debug/logger.py 迁移到此处
    - _last_trace 直接由 AgentLogger 管理，不再委托 DebugManager
    - 日志初始化（_setup_logging）从 DebugManager 迁移到此处
    """

    def __init__(self, level: str = "INFO", log_file: str | None = None):
        self._current_request_id: str | None = None
        self._last_trace: TraceRecord | None = None
        self._setup_logging(level, log_file)

    def _setup_logging(self, level: str, log_file: str | None):
        """日志初始化（从 DebugManager 迁移）"""
        handlers: list[logging.Handler] = [logging.StreamHandler()]
        if log_file:
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=handlers,
        )

    def new_request(self) -> str:
        """为新的 run() 调用生成唯一 request_id"""
        self._current_request_id = uuid4().hex[:8]
        return self._current_request_id

    @property
    def request_id(self) -> str | None:
        return self._current_request_id

    def record(self, trace: TraceRecord) -> None:
        """记录 TraceRecord"""
        self._last_trace = trace
        logger.debug(
            f"[{self._current_request_id}] request completed: "
            f"duration={trace.duration_ms}ms, "
            f"iterations={len(trace.llm_responses)}, "
            f"tool_calls={len(trace.tool_calls)}",
            extra={"request_id": self._current_request_id},
        )

    def get_last_trace(self) -> TraceRecord | None:
        """获取最近一次推理追踪（原由 DebugManager 提供）"""
        return self._last_trace

    def log_tool_call(self, tool_name: str, arguments: dict) -> None:
        logger.info(f"[{self._current_request_id}] tool call: {tool_name}")

    def log_tool_result(self, tool_name: str, result_len: int) -> None:
        logger.info(f"[{self._current_request_id}] tool result: {tool_name} ({result_len} chars)")

    def log_error(self, error: str) -> None:
        logger.error(f"[{self._current_request_id}] error: {error}")
```

AgentLoop 修改：

```python
# agent/loop.py（修改片段）

class AgentLoop:
    def __init__(self, ...):
        # 删除：self._debug_manager = DebugManager(...)
        # 改为：self._logger 在 main.py 中初始化时传入 level/log_file
        self._logger: AgentLogger | None = logger or AgentLogger(
            level=config.debug.level,
            log_file=config.debug.log_file,
        )

    def debug_trace(self, channel: "Channel"):
        """输出最近一次推理过程（供 /debug 命令调用）"""
        # 改为从 AgentLogger 获取
        trace = self._logger.get_last_trace() if self._logger else None
        if trace:
            channel.print_info(trace.format_summary())
        else:
            channel.print_info("(no trace yet)")
```

main.py 修改：

```python
# main.py（修改片段）

# 删除：from dotclaw.debug.logger import DebugManager
# /debug 命令改为：
elif cmd == "/debug":
    agent.debug_trace(channel)  # 接口不变，内部从 AgentLogger 获取
```

删除文件：
- `debug/logger.py` — TraceRecord + DebugManager 已合并到 AgentLogger
- `debug/__init__.py` — 空文件，一并删除

**关键变化**：
- TraceRecord 从 debug/logger.py 迁移到 agent/logger.py，消除跨模块依赖
- AgentLogger 直接管理 _last_trace，不再委托 DebugManager
- AgentLoop 不再持有 _debug_manager，统一用 _logger
- /debug 命令接口不变（agent.debug_trace(channel)），内部改为从 AgentLogger 取数据
- debug/ 子包整体删除

---

### 4.9 agent/loop.py — 修改

**修改范围**：

1. __init__ 参数：tool_registry: ToolRegistry -> tool_executor: ToolExecutor
2. __init__ 删除：_debug_manager，改用 _logger 统一管理
3. run() 内工具调用：self._tool_registry.execute(...) -> self._tool_executor.execute(...)
4. debug_trace() 改为从 self._logger 获取 trace

```python
# agent/loop.py (修改片段)

class AgentLoop:
    def __init__(
        self,
        ...
        tool_executor: "ToolExecutor | None" = None,  # 改参数名
        logger: "AgentLogger | None" = None,
        ...
    ):
        ...
        self._tool_executor = tool_executor  # 改属性名
        # 删除：self._debug_manager = DebugManager(...)
        # 改为：_logger 直接管理 trace
        self._logger = logger or AgentLogger(
            level=config.debug.level,
            log_file=config.debug.log_file,
        )
        ...

    async def run(self, user_message: str) -> AgentResult:
        ...
        # 工具调用部分
        for tc in tool_calls_pending:
            ...
            if self._tool_executor:
                self.channel.print_info(f"调用工具: {tc.name}(...)")
                result = await self._tool_executor.execute(
                    name=tc.name,
                    arguments=args,
                    channel=self.channel,
                )
                ...

    def debug_trace(self, channel: "Channel"):
        """输出最近一次推理过程（供 /debug 命令调用）"""
        trace = self._logger.get_last_trace() if self._logger else None
        if trace:
            channel.print_info(trace.format_summary())
        else:
            channel.print_info("(no trace yet)")
```

**兼容性保证**：
- ToolExecutor.execute() 的签名与旧 ToolRegistry.execute() 完全兼容（name, arguments, channel）
- ToolResult 新增字段不影响 LLM 消费（只使用 output 字段）
- AgentLoop 其他逻辑不变

---

### 4.10 main.py — 修改

**修改范围**：初始化新架构，组装 ToolRegistry -> ToolExecutor -> AgentLoop，删除 DebugManager 引用。

```python
# main.py (修改片段)

async def _run_cli():
    ...
    # 删除：from dotclaw.debug.logger import DebugManager

    # 初始化工具层（Phase 5）
    from dotclaw.tools.registry import ToolRegistry
    from dotclaw.tools.executor import ToolExecutor
    from dotclaw.tools.approval import ApprovalManager
    from dotclaw.tools.builtin import register_all

    # 1. 创建注册表
    tool_registry = ToolRegistry()

    # 2. 注册内置工具（仅在 builtin_enabled 为 true 时）
    if config.tools.builtin_enabled:
        register_all(tool_registry)

    # 2b. 根据配置禁用指定工具（向后兼容旧 config.exec.enabled: false）
    for tool_name in config.tools.disabled_tools:
        tool_registry.unregister(tool_name)

    # 3. 创建审批管理器（从 config 加载）
    approval_mgr = ApprovalManager(
        approval_commands=config.tools.approval_commands,
    )

    # 4. 创建执行器
    tool_executor = ToolExecutor(
        registry=tool_registry,
        approval_manager=approval_mgr,
    )

    # 5. 创建 AgentLogger（合并 DebugManager 能力）
    from dotclaw.agent.logger import AgentLogger
    agent_logger = AgentLogger(
        level=config.debug.level,
        log_file=config.debug.log_file,
    )

    # 6. 传入 AgentLoop
    agent = AgentLoop(
        ...
        tool_executor=tool_executor,  # 改参数名
        logger=agent_logger,          # 传入 AgentLogger（含 TraceRecord）
        ...
    )
    ...

# /tools 命令更新：审批标记从 ToolDefinition.needs_approval 读取
def _cmd_tools(channel, tool_registry):
    definitions = tool_registry.get_definitions()
    for d in definitions:
        handler = tool_registry.get(d.name)
        mark = ""
        if handler and handler.definition().needs_approval:
            mark = " [需审批]"
        channel.print_info(f"  {d.name}{mark}: {d.description}")
```

---

### 4.11 config/settings.py — 修改

ToolsConfig 改造：

```python
@dataclass
class ToolsConfig:
    # source 级启停
    builtin_enabled: bool = True
    mcp_enabled: bool = True       # Phase 5 预留，暂不消费
    skill_enabled: bool = True      # Phase 5 预留，暂不消费

    # 危险命令审批列表
    approval_commands: list[str] = field(default_factory=lambda: ["exec", "python"])

    # 单工具禁用列表（向后兼容旧 config.exec.enabled=false）
    disabled_tools: list[str] = field(default_factory=list)

    # exec 工具配置
    exec_timeout: float = 60.0

    # web_search 配置
    web_search_enabled: bool = False
```

_raw_to_config 解析适配：

```python
tools_raw = raw.get("tools", {})

# 向后兼容：合并新格式 + 旧格式（始终合并，防止混合格式丢数据）
approval_commands = list(tools_raw.get("approval_commands", []))
for tool_name in ("exec", "python"):
    if tools_raw.get(tool_name, {}).get("needs_approval", False):
        if tool_name not in approval_commands:
            approval_commands.append(tool_name)

# 向后兼容：合并新格式 + 旧格式
disabled_tools = list(tools_raw.get("disabled_tools", []))
for tool_name in ("exec", "python"):
    if not tools_raw.get(tool_name, {}).get("enabled", True):
        if tool_name not in disabled_tools:
            disabled_tools.append(tool_name)

tools = ToolsConfig(
    builtin_enabled=tools_raw.get("builtin_enabled", True),
    mcp_enabled=tools_raw.get("mcp_enabled", True),
    skill_enabled=tools_raw.get("skill_enabled", True),
    approval_commands=approval_commands,
    disabled_tools=disabled_tools,
    exec_timeout=tools_raw.get("exec_timeout") or
                  tools_raw.get("python", {}).get("timeout", 60.0),
    web_search_enabled=tools_raw.get("web_search", {}).get("enabled", False),
)
```

---

### 4.12 config.yaml — 修改

```yaml
# ============================================
# dotClaw 配置文件
# ============================================

tools:
  # source 级启停
  builtin_enabled: true
  mcp_enabled: true      # Phase 5 预留
  skill_enabled: true     # Phase 5 预留

  # 危险工具审批命令列表
  approval_commands:
    - exec
    - python

  # 单工具禁用列表（向后兼容旧 config.exec.enabled: false）
  disabled_tools: []       # 如 ["exec"] 禁用 exec 工具

  exec_timeout: 60

  web_search:
    enabled: false

# ... 其余配置不变
```

> **向后兼容说明**：旧格式 `tools.exec.needs_approval: true` 自动转换为 `tools.approval_commands: [exec]`。旧格式 `tools.exec.enabled: false` 自动转换为 `tools.disabled_tools: [exec]`。用户升级到 Phase 5 无需手动修改 config.yaml。

---

## 五、开发实施顺序

```
Step 1:  tools/base.py               <- 重构 ToolDefinition + ToolResult + ToolExecutionContext
Step 2:  tools/handler.py            <- 新增 ToolHandler ABC + BuiltinToolHandler
Step 3:  tools/registry.py           <- 新增 ToolRegistry（纯注册表）
Step 4:  tools/executor.py           <- 新增 ToolExecutor（调度+审批+超时）
Step 5:  tools/approval.py           <- 重构 ApprovalManager（删除硬编码）
Step 6:  tools/builtin/             <- 新建子包，迁移内置工具
Step 7:  tools/provider.py          <- 新增 ToolProvider ABC（骨架）
Step 8:  agent/logger.py            <- 修改：合并 DebugManager + TraceRecord
Step 9:  agent/loop.py              <- 修改：_tool_registry -> _tool_executor + 删除 _debug_manager
Step 10: main.py                     <- 修改：组装新架构 + 删除 DebugManager 引用
Step 11: config/settings.py          <- 修改：ToolsConfig 扩展
Step 12: config.yaml                 <- 修改：tools 段结构
Step 13: 删除旧文件                   <- 删除 tools/exec_tool.py 等 + debug/ 子包（先删再测，保证测试环境与部署一致）
Step 14: tests/test_phase5_acceptance.py <- 自动化测试（在无旧文件环境中运行）
Step 15: 回归验收
```

Step 1-2 互相独立可并行，Step 3 依赖 Step 2（ToolHandler），Step 4 依赖 Step 3+5，Step 6 依赖 Step 2+3，Step 8-10 依赖 Step 4+8。

---

## 六、目录结构变化

```
src/dotclaw/tools/
+-- __init__.py               # 导出 ToolRegistry, ToolExecutor, ToolProvider
+-- base.py                   # ToolDefinition, ToolResult, ToolExecutionContext（重构）
+-- handler.py                # ToolHandler ABC, BuiltinToolHandler（新增）
+-- registry.py               # ToolRegistry（新增，从旧 base.py 拆出）
+-- executor.py               # ToolExecutor（新增）
+-- approval.py               # ApprovalManager（重构）
+-- provider.py               # ToolProvider ABC（新增骨架）
+-- builtin/                  # 内置工具子包（新增）
|   +-- __init__.py          # register_all()
|   +-- exec_tool.py         # 迁移自 tools/exec_tool.py
|   +-- file_tool.py         # 迁移自 tools/file_tool.py
|   +-- memory_tool.py       # 迁移自 tools/memory_tool.py
|   +-- system_tool.py       # 迁移自 tools/system_tool.py
+-- exec_tool.py              # 删除（已迁移）
+-- file_tool.py              # 删除（已迁移）
+-- memory_tool.py            # 删除（已迁移）
+-- system_tool.py            # 删除（已迁移）

src/dotclaw/agent/
+-- logger.py                 # 修改：合并 TraceRecord + DebugManager 能力

src/dotclaw/debug/            # 整体删除（TraceRecord 迁移到 agent/logger.py）
+-- logger.py                  # 删除（已合并到 agent/logger.py）
+-- __init__.py                # 删除
```

---

## 七、自动化测试计划

新增 tests/test_phase5_acceptance.py，覆盖 10 个场景。

| # | 测试场景 | 验证内容 |
|---|---------|---------|
| 1 | ToolRegistry 注册/查询/覆盖 | register/get/all_names/同名覆盖 |
| 2 | ToolRegistry 注销 | unregister 成功/失败 |
| 3 | BuiltinToolHandler 执行 | 正常执行/异常捕获/ToolResult 结构 |
| 4 | ToolExecutor 审批流程 | needs_approval=True **且** tool_name 在 approval_commands 中时触发审批，拒绝返回 APPROVAL_DENIED |
| 5 | ToolExecutor 超时控制 | timeout 触发 TIMEOUT error_code |
| 6 | ToolExecutor 工具未找到 | 返回 TOOL_NOT_FOUND |
| 7 | AgentLoop 集成 | _tool_executor.execute() 调用成功，输出不变 |
| 8 | 配置加载 | config.yaml approval_commands 正确加载到 ApprovalManager |
| 9 | 日志合并 | AgentLogger 直接管理 _last_trace，/debug 命令正常工作，debug/ 子包已删除 |
| 10 | 旧 config 格式兼容 | 1. 旧格式 exec.needs_approval:true + python.needs_approval:true → approval_commands=["exec","python"]; 2. 旧格式 exec.enabled:false → disabled_tools=["exec"]; 3. 混合格式（新 approval_commands + 旧 per-tool needs_approval）正确合并; 4. exec_timeout 从旧 python.timeout:30 正确读取 |

---

## 八、验收标准

### 8.1 功能验收

**场景 1：内置工具全部可用**
- 启动 dotclaw，输入 /tools 列出所有内置工具（exec/read_file/write_file/list_dir/memory_read/memory_write/system_info/get_time）
- 执行任意工具，功能与 Phase 4 前完全一致
- 预期：工具名无命名空间前缀（如 builtin.exec），行为与之前一致

**场景 2：审批机制生效**
- config.yaml approval_commands: ["exec"]
- 执行 /exec ls，触发审批确认
- 输入 n，返回"用户拒绝了 exec 的执行"
- 输入 y，正常执行
- 预期：needs_approval + approval_commands 双重控制生效

**场景 3：超时控制**
- 修改 exec_tool.py 的 timeout 为 1 秒，执行 sleep 5
- 预期：返回"工具执行超时（1秒）"，error_code=TIMEOUT

**场景 4：结构化错误**
- 执行一个不存在的工具 nonexistent_tool
- 预期：返回 ToolResult，is_error=True，error_code=TOOL_NOT_FOUND

**场景 5：日志合并后 /debug 命令正常**
- 执行一次对话后输入 /debug
- 预期：显示最近推理过程摘要（用户消息、LLM 响应次数、工具调用次数、耗时）
- 预期：debug/ 子包不存在，import dotclaw.debug 报 ModuleNotFoundError

**场景 6：AgentLoop 行为不变**
- 正常对话，触发工具调用
- 预期：LLM 返回内容、工具调用次数、iterations 与 Phase 4 前一致

### 8.2 架构验收

- ToolRegistry 只含注册/查询方法，不含 execute()
- ToolExecutor 负责调度，从 ToolRegistry 获取 Handler
- ToolHandler ABC 定义清晰，BuiltinToolHandler 正确适配现有函数
- ToolProvider ABC 存在，MCPToolProvider/SkillToolProvider 可按接口实现
- 内置工具在 builtin/ 子包内，通过 register_all() 统一注册
- TraceRecord 在 agent/logger.py 中，AgentLoop 不再持有 _debug_manager
- debug/ 子包已删除，无残留引用

### 8.3 回归验收

- Phase 1 的 5 个验收场景全部通过
- Phase 2 的模型切换和降级功能正常
- Phase 3 的 6 个功能场景全部通过
- Phase 4 的 8 个功能场景全部通过（记忆系统不受影响）
- tests/test_phase1_acceptance.py 全部通过
- tests/test_phase2_acceptance.py 全部通过
- tests/test_phase3_acceptance.py 全部通过
- tests/test_phase4_acceptance.py 全部通过
- tests/test_phase5_acceptance.py 全部通过（10 个场景）

### 8.4 代码质量

- ToolRegistry 纯注册表，无副作用
- ToolExecutor 超时控制使用 asyncio.wait_for，超时直接 cancel
- ApprovalManager 无硬编码，approval_commands 从 config 加载
- ToolResult 结构化字段不影响现有逻辑（只消费 output）
- builtin/ 子包内的工具函数与旧版完全一致（通过 Adapter 调用）
- 删除旧文件 tools/exec_tool.py 等 + debug/ 子包，无残留引用
- AgentLogger 合并 TraceRecord + _setup_logging 后，/debug 命令功能等价

---

## 九、文件清单

| 文件 | 状态 | 复杂度 | 说明 |
|------|------|--------|------|
| tools/base.py | 修改 | 中 | ToolDefinition 扩展 + ToolResult 结构化 + 删除全局注册表 |
| tools/handler.py | 新建 | 中 | ToolHandler ABC + BuiltinToolHandler |
| tools/registry.py | 新建 | 低 | ToolRegistry 纯注册表 |
| tools/executor.py | 新建 | 高 | ToolExecutor 调度+审批+超时 |
| tools/approval.py | 修改 | 低 | 删除硬编码，支持 config 加载 |
| tools/provider.py | 新建 | 低 | ToolProvider ABC 骨架 |
| tools/builtin/__init__.py | 新建 | 低 | register_all() 统一注册入口 |
| tools/builtin/exec_tool.py | 新建 | 低 | 迁移自 tools/exec_tool.py |
| tools/builtin/file_tool.py | 新建 | 低 | 迁移自 tools/file_tool.py |
| tools/builtin/memory_tool.py | 新建 | 低 | 迁移自 tools/memory_tool.py |
| tools/builtin/system_tool.py | 新建 | 低 | 迁移自 tools/system_tool.py |
| tools/__init__.py | 修改 | 低 | 导出新模块 |
| agent/logger.py | 修改 | 中 | 合并 TraceRecord + DebugManager，删除跨模块依赖 |
| agent/loop.py | 修改 | 中 | _tool_registry -> _tool_executor + 删除 _debug_manager |
| main.py | 修改 | 中 | 组装新架构 |
| config/settings.py | 修改 | 中 | ToolsConfig 扩展 |
| config.yaml | 修改 | 低 | tools 段结构更新 |
| tests/test_phase5_acceptance.py | 新建 | 中 | 9 个自动化测试场景 |
| debug/logger.py | 删除 | - | TraceRecord 已合并到 agent/logger.py |
| debug/__init__.py | 删除 | - | debug 子包整体删除 |
| tools/exec_tool.py | 删除 | - | 已迁移到 builtin/ |
| tools/file_tool.py | 删除 | - | 已迁移到 builtin/ |
| tools/memory_tool.py | 删除 | - | 已迁移到 builtin/ |
| tools/system_tool.py | 删除 | - | 已迁移到 builtin/ |

---

## 十、开发注意事项

1. **最小改动 AgentLoop**：只改 _tool_registry -> _tool_executor，不引入其他变化
2. **后注册覆盖**：ToolRegistry.register() 同名直接覆盖，无警告——Phase 6/7 由那时决定是否需要日志
3. **工具无状态**：ToolExecutionContext 只含 timeout，不含 session_id/workspace——工具从 arguments 获取所需信息
4. **超时直接杀**：asyncio.wait_for 超时后 cancel task，子进程由 Handler 内部负责 kill（exec_tool 已有 proc.kill()）
5. **MCP 超时独立**：MCP HTTP 调用超时由 MCPToolHandler 内部处理（HTTP client timeout），ToolExecutor 只控制整体执行超时
6. **审批双重机制**：ToolDefinition.needs_approval 作为声明式标记（工具声明自己危险），approval_commands 作为用户配置（用户选择哪些工具需要确认）。两者 AND 关系——needs_approval=True 且在 approval_commands 列表中，才触发用户确认。内置工具在工厂函数中显式设置 needs_approval，开发者新增危险工具时必须同样显式设置
7. **ToolResult 结构化**：新增 error_code/error_type，但不强制消费——现有逻辑只使用 output，向后兼容
8. **内置工具函数不变**：通过 BuiltinToolHandler Adapter 调用，不修改原有 exec_command() 等函数签名
9. **needs_approval 默认值风险**：默认 False（不过度拦截），开发者新增危险工具时必须显式设置 needs_approval=True，建议 code review 时检查
9. **删除旧文件**：迁移完成后删除 tools/exec_tool.py 等，确保无残留引用（grep 确认）
10. **config.yaml 向后兼容**：旧格式 per-tool needs_approval 自动转换为 approval_commands，旧格式 per-tool enabled=false 自动转换为 disabled_tools 列表，用户无需手动迁移
11. **日志合并清理 P3 技术债**：TraceRecord 从 debug/logger.py 迁移到 agent/logger.py，AgentLoop 不再持有 _debug_manager，debug/ 子包整体删除
12. **Phase 5 不实现 MCP/Skill**：只预留接口，不写具体实现——避免范围蔓延
13. **测试覆盖**：test_phase5_acceptance.py 覆盖 8 个场景，重点验证架构拆分正确性和向后兼容性
14. **回归测试优先**：Phase 1-4 的所有验收测试必须在 Phase 5 完成后全部通过
15. **文档沉淀**：本文档（phase5-roadmap.md）作为 Phase 5 的详细设计文档，指导开发实施

---

## 十一、Phase 5 边界（Out of Scope）

以下内容**不在 Phase 5 范围内**，留待后续 Phase 实施：

- MCP 工具集成（Phase 6+）
- Skill 脚本式工具（Phase 7+）
- 用户自定义工具注入（Phase 7+）
- 工具热加载/运行时动态注册（后续 Phase）
- 工具调用链路 trace（已有 AgentLogger，暂不扩展）
- 工具权限细粒度控制（按用户/会话）（后续 Phase）

---

*文档版本：v1.3（审计2修正：builtin_enabled 接线 + compat 合并逻辑 + 测试覆盖 + needs_approval 文档对齐）*
*最后更新：2026-06-02*
*作者：dotclaw 开发工程师*
