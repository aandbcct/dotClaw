"""工具源抽象接口（Phase 5 新增骨架 — MCP/Skill 预留）"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .registry import ToolRegistry


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
