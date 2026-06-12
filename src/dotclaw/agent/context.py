"""AgentContext — 不可变的 Agent 上下文快照

在 AgentLoop.run() 开头创建，一次调用全程不变。
frozen=True 确保并发安全和不可变性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..llm.base import ToolDefinition
    from ..channel.base import Channel
    from ..skills.registry import SkillRegistry  # P7 新增


@dataclass(frozen=True)
class AgentContext:
    """一次 Agent.run() 调用的不可变上下文快照"""

    session_id: str
    """当前会话 ID"""

    workspace: Path
    """Agent 操作目录（运行时产物），默认 = project_root"""

    project_root: Path
    """dotClaw 根目录（config.yaml / data/ / skills/ 所在）"""

    model: str
    """当前选用的模型名"""

    system_prompt: str
    """config.agent.system_prompt"""

    available_tools: list[str] = field(default_factory=list)
    """已注册工具的名称列表"""

    tool_definitions: list["ToolDefinition"] = field(default_factory=list)
    """完整工具定义（供 PromptBuilder 生成工具描述）"""

    request_id: str = ""
    """本次 run() 调用的唯一标识"""

    purpose: str = "chat"
    """本次请求的用途（当前固定 "chat"）"""

    max_context_tokens: int = 8000
    """config.agent.max_context_tokens"""

    rules: str = ""
    """config.agent.rules，追加到 system prompt 的行为规则"""

    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    """快照创建时间"""

    channel: "Channel | None" = None
    """通信通道（可为 None，如 Scheduler 触发）"""

    memory_summary: str = ""
    """P4 新增：语义检索结果文本，由 _build_context() 异步填充，MemoryProvider 读取"""

    skill_registry: "SkillRegistry | None" = None
    """P7 新增：Skill 注册表（skill_enabled=False 时为 None）"""

    journal: "Any | None" = None
    """Journal 观测实例（None 时跳过埋点，有实例时自动生效）"""

    def __post_init__(self):
        """设置 workspace 默认值（绕过 frozen 限制）"""
        if str(self.workspace) == ".":
            object.__setattr__(self, "workspace", self.project_root)
