"""ContextSlot 的不可变输入模型。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotclaw.runtime.application.execution import RunExecutionView
from ..runtime.application.dto import RunRequest, ToolDefinition
from .ports import ContextDependencies


@dataclass(frozen=True)
class ContextProfile:
    """一次运行冻结的 Agent 上下文策略。"""

    agent_id: str
    identity_version: str
    system_prompt: str
    tools: tuple[ToolDefinition, ...]
    project_root: Path
    max_context_tokens: int
    excluded_slot_names: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SlotContext:
    """传给 ContextSlot 的只读运行输入，不包含 Journal 或持久化能力。"""

    request: RunRequest
    execution: RunExecutionView
    profile: ContextProfile
    dependencies: ContextDependencies

    @property
    def query(self) -> str:
        """返回当前用户输入，供检索类槽位使用。"""
        return self.request.user_message.content
