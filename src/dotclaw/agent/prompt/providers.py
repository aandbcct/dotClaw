"""DataProvider — system prompt 各 section 的数据源

抽象 DataProvider 接口 + P3 的三个具体实现：
- RoleProvider：角色定义
- RulesProvider：行为规则（可选，rules 为空时跳过）
- ToolsProvider：工具列表描述
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..context import AgentContext

logger = logging.getLogger("dotclaw.agent.prompt")


class DataProvider(ABC):
    """system prompt 单个 section 的数据源"""

    @property
    @abstractmethod
    def section_name(self) -> str:
        """section 名称，如 "tools" / "rules" / "memory" / "skills" """
        ...

    @abstractmethod
    def provide(self, context: "AgentContext") -> str | None:
        """返回该 section 的内容，None 表示跳过"""
        ...


class RoleProvider(DataProvider):
    """角色定义：system prompt 主体"""

    @property
    def section_name(self) -> str:
        return "role"

    def provide(self, context: "AgentContext") -> str | None:
        return context.system_prompt


class RulesProvider(DataProvider):
    """行为规则：追加在 role 之后的规则约束"""

    @property
    def section_name(self) -> str:
        return "rules"

    def provide(self, context: "AgentContext") -> str | None:
        if not context.rules or not context.rules.strip():
            return None
        return f"## 行为规则\n\n{context.rules.strip()}"


class ToolsProvider(DataProvider):
    """工具列表：格式化的可用工具描述"""

    @property
    def section_name(self) -> str:
        return "tools"

    def provide(self, context: "AgentContext") -> str | None:
        if not context.tool_definitions:
            return "## 可用工具\n\n(无可用工具)"

        lines = ["## 可用工具\n"]
        for t in context.tool_definitions:
            lines.append(f"### {t.name}")
            lines.append(f"描述: {t.description}")
            if t.parameters:
                import json
                params_str = json.dumps(t.parameters, ensure_ascii=False, indent=2)
                lines.append(f"参数: {params_str}")
            lines.append("")
        return "\n".join(lines)


# ---- P4/P7 预留 Provider 骨架 ----

class MemoryProvider(DataProvider):
    """记忆上下文（P4 激活）。从 context.memory_summary 读取，纯同步。"""

    @property
    def section_name(self) -> str:
        return "memory"

    def provide(self, context: "AgentContext") -> str | None:
        if not context.memory_summary:
            return None
        return f"## 相关记忆\n\n{context.memory_summary}"


class SkillsProvider(DataProvider):
    """技能描述（Phase 7 实现）— 从 context.skill_registry 读取"""

    @property
    def section_name(self) -> str:
        return "skills"

    def provide(self, context: "AgentContext") -> str | None:
        registry = context.skill_registry
        if not registry:
            return None

        descriptions = registry.get_descriptions_block(max_desc_len=20)
        if not descriptions:
            return None

        return (
            "## 技能系统（mandatory）\n\n"
            "如果有技能的描述与用户需求匹配：使用 `read_file` 工具读取其路径的 SKILL.md 文件，\n"
            "然后严格遵循文件中的指令。\n\n"
            "**重要**: 技能不是工具，不能直接调用。使用技能的唯一方式是用 `read_file` 读取 SKILL.md 文件，\n"
            "然后按文件内容操作。\n\n"
            "### 可用技能\n\n"
            f"{descriptions}"
        )
