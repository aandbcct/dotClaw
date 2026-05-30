"""PromptBuilder — 模块化 system prompt 构建器

维护 providers 列表，按顺序调用 build() 拼接各 section。
Provider 异常时记录 warning 并跳过该 section，不中断整体构建。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .providers import DataProvider

if TYPE_CHECKING:
    from ..context import AgentContext

logger = logging.getLogger("dotclaw.agent.prompt")


class PromptBuilder:
    """
    模块化 system prompt 构建器。

    用法:
        builder = PromptBuilder([RoleProvider(), ToolsProvider()])
        prompt = builder.build(context)
        messages = [Message(role="system", content=prompt)]
    """

    def __init__(self, providers: list[DataProvider]):
        self._providers = providers

    def build(self, context: "AgentContext") -> str:
        """
        按 providers 顺序调用 provide()，拼接为最终 system prompt。

        - 每个 provider 返回 None → 跳过
        - provider 抛异常 → warning + 跳过
        - section 之间用 \\n\\n 分隔
        """
        sections: list[str] = []

        for provider in self._providers:
            try:
                content = provider.provide(context)
                if content:
                    sections.append(content)
            except Exception as e:
                logger.warning(
                    f"Provider '{provider.section_name}' 异常，跳过: {e}"
                )

        return "\n\n".join(sections)
