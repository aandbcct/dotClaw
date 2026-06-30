"""AgentRuntime — Agent 纯能力引用集合。

纯 dataclass，持有 Agent 执行所需的所有运行时服务引用。
不包含任何业务逻辑或状态，只是一个服务定位器 / 依赖容器。

AgentRuntime 回答"Agent 能调用什么"：
- LLM 调用 (llm)
- 工具执行 (tool_executor)
- 记忆读写 (memory_mgr)
- Skill 注入 (skill_registry)
- MCP 工具 (mcp_provider)
- 上下文组装 (assembler)
- 通信通道 (channel)
- 对话持久化 (conversation_mgr)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..llm.proxy import LLMProxy
    from ..tools.executor import ToolExecutor
    from ..memory.manager import MemoryManager
    from ..skills.registry import SkillRegistry
    from .slotContext import ContextAssembler
    from ..channel.base import Channel
    from ..storage.conversation import ConversationManager
    from ..config import Config


@dataclass
class AgentRuntime:
    """Agent 纯能力引用集合 —— 零业务逻辑，纯执行设施。

    每项都直接持有运行时可执行的对象引用。
    Identity 通过 Agent 方法约束这些 Runtime 能力（如用 allowed_tools 过滤 tool_executor）。
    """

    # ── 核心执行 ──
    llm: "LLMProxy"
    """LLM 调用代理（含 ModelRouter + RateLimiter + CircuitBreaker）。"""

    tool_executor: "ToolExecutor | None"
    """工具执行器。None 表示工具系统未初始化（降级模式）。"""

    # ── 上下文与观测 ──
    assembler: "ContextAssembler | None"
    """上下文 Assembler。None 表示回退到旧 Provider 模式。"""

    # ── 持久化 ──
    conversation_mgr: "ConversationManager"
    """对话管理器。AgentLoop 在 finalize 时通过它自动持久化 Conversation。"""

    # ── 通信 ──
    channel: "Channel | None"
    """通信通道（CLI / WebSocket / ...）。None 表示无人机模式（如 Scheduler）。"""

    # ── 可选能力 ──
    memory_mgr: "MemoryManager | None" = None
    """记忆管理器。None 表示记忆系统未启用。"""

    skill_registry: "SkillRegistry | None" = None
    """Skill 注册表。None 表示 Skill 系统未启用。"""

    mcp_provider: object = None
    """MCP 工具提供器。None 表示 MCP 未启用。"""

    # ── 配置 ──
    config: "Config | None" = None
    """项目级全局配置（config.yaml）。用于 journal_config / agent 默认值等。"""
