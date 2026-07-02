"""上下文工程模块 — Slot + Assembler

ContextSlot: 可插拔的上下文内容单元，内置缓存。
ContextAssembler: 管理 Slot 列表，按 tier 组装 system_prompt 文本。
SlotContext: 传给每个 Slot.load() 的输入参数篮。

数据流:
  Slot (tier 0-2) → Assembler.build_system_prompt() → system_prompt 纯文本
  _history (tier 3) → _build_messages() 处理
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config.settings import AgentConfig
    from ..journal import Journal
    from ..llm.base import ToolDefinition
    from ..skills.registry import SkillRegistry
    from ..orchestration.registry import AgentRegistry

logger = logging.getLogger("dotclaw.agent.context_slot")


# ======================== TierLevel ========================


class TierLevel(IntEnum):
    """上下文层级。数值越小越靠前，缓存持久性越高。

    间隔 10 便于将来在现有层级间插值。"""

    STATIC = 0          # 跨 session 不变 → 永久前缀缓存
    SESSION = 10        # 同一 session 内不变 → 按 session 缓存
    CONDITIONAL = 20    # 每次 request 可能变化 → 按 request 缓存
    DYNAMIC = 30        # 每 turn 都变 → 不进 Slot，存在 _history 中


# ======================== SlotContext ========================


@dataclass(frozen=True)
class SlotContext:
    """传给每个 ContextSlot.load() 的输入参数篮。

    替代旧的 AgentContext。只存 Slot 组装所需的输入，不存输出。
    """

    # ── 本次请求（必填）──
    query: str
    """用户当前输入（用于 memory/knowledge 检索）"""

    request_id: str
    """本次 request 的唯一标识"""

    session_id: str
    """当前会话 ID"""

    project_root: Path
    """项目根目录"""

    max_context_tokens: int
    """token 预算上限"""

    # ── Agent 配置（可选）──
    system_prompt: str = ""
    """已解析的最终 system prompt（由 Agent._resolve_system_prompt() 提供）"""

    agent_config: "AgentConfig | None" = None
    """Agent 级配置（供 model, rules 等其他字段使用）"""

    # ── 依赖注入 ──
    tool_definitions: list["ToolDefinition"] = field(default_factory=list)
    """可用工具定义列表"""

    skill_registry: "SkillRegistry | None" = None
    """技能注册表"""

    memory_manager: Any = None
    """记忆管理器（用于语义检索）"""

    knowledge_base: Any = None
    """知识库/RAG 引擎（可能为 None）"""

    user_profile: Any = None
    """用户档案（可能为 None）"""

    agent_registry: "AgentRegistry | None" = None
    """全局 Agent 目录（供 AvailableAgentsSlot 注入可用子 Agent 列表）"""

    # ── 观测 ──
    journal: "Journal | None" = None
    """日志观测实例"""


# ======================== ContextSlot ========================


class ContextSlot(ABC):
    """上下文槽位基类。

    子类实现 _produce() 定义内容来源，基类负责缓存逻辑。
    Assembler 调用 load() 获取内容。
    """

    name: str
    """槽位名称，仅用于标识和调试"""

    tier: TierLevel
    """所属层级，Assembler 据此排序"""

    cache_policy: str
    """缓存策略: "forever" | "session" | "request" """

    def __init__(self) -> None:
        self._cached: str | None = None
        self._cache_valid: bool = False

    # ── 子类必须实现 ──

    @abstractmethod
    async def _produce(self, ctx: SlotContext) -> str | None:
        """从来源加载内容。返回 None 表示本槽位无内容可产出。

        子类负责具体的数据来源：
        - 读配置对象 → 同步返回
        - 读文件 → asyncio
        - 读数据库/向量检索 → async
        """
        ...

    # ── 缓存管理（基类统一处理） ──

    async def load(self, ctx: SlotContext) -> str | None:
        """带缓存的加载入口。Assembler 调用此方法。"""
        if self._cache_valid:
            return self._cached
        content = await self._produce(ctx)
        self._cached = content
        self._cache_valid = True
        return content

    def invalidate(self) -> None:
        """标记缓存失效。下次 load() 会重新调用 _produce()。"""
        self._cache_valid = False


# ======================== ContextAssembler ========================


class ContextAssembler:
    """按 tier 组装 Slot 内容，输出 system_prompt 纯文本。

    不接触 Message 对象，不管理对话历史。
    生命周期 = Agent 生命周期，session 天然隔离。
    """

    def __init__(self, slots: list[ContextSlot]) -> None:
        """创建 Assembler。

        Args:
            slots: 所有 Slot 实例。Assembler 内部按 tier 排序。
        """
        self._slots = sorted(slots, key=lambda s: s.tier)

    def on_new_request(self) -> None:
        """每个新 request 开始时调用。

        使所有 cache_policy="request" 的 Slot 缓存过期。
        tier 0 (forever) 和 tier 1 (session) 不受影响。
        """
        for slot in self._slots:
            if slot.cache_policy == "request":
                slot.invalidate()

    async def build_system_prompt(self, ctx: SlotContext) -> str:
        """组装所有 Slot 内容为 system_prompt 文本。

        Args:
            ctx: 组装所需的输入参数

        Returns:
            拼接后的 system_prompt 文本，各 Slot 产出间用双换行分隔
        """
        parts: list[str] = []
        for slot in self._slots:
            try:
                content = await slot.load(ctx)
                if content:
                    parts.append(content)
            except Exception as e:
                logger.warning(
                    "Slot '%s' (tier=%s) 加载失败，跳过: %s",
                    slot.name, slot.tier.name, e,
                )
        return "\n\n".join(parts)
