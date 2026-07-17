"""从既有 AgentIdentity 和配置冻结 Runtime v2 执行策略。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ...agent.identity import AgentIdentity
from ...config.settings import Config
from ...orchestration.registry import AgentRegistry
from ...tools.base import ToolDefinition as LegacyToolDefinition
from ...tools.executor import ToolExecutor
from ..application.ports import RunPolicyPort
from ..domain.models import AgentPolicySnapshot, RunRequest, ToolDefinition


LEGACY_TASK_TOOL_NAMES: frozenset[str] = frozenset({
    "task_send_message",
    "wait_task",
    "task_status",
    "cancel_task",
})
"""仅供旧 Runtime / Dispatcher 兼容链使用的跨 Run Task 工具名称。"""


class AgentPolicyResolver(RunPolicyPort):
    """将 Agent 配置解析为一次 Run 不可变策略快照的适配器。"""

    def __init__(
        self,
        identity: AgentIdentity,
        config: Config,
        executor: ToolExecutor,
        project_root: Path,
        agent_registry: AgentRegistry | None = None,
    ) -> None:
        """绑定一个 Agent 的身份、配置和工具注册表。"""
        self._identity: AgentIdentity = identity
        self._config: Config = config
        self._executor: ToolExecutor = executor
        self._project_root: Path = project_root
        self._agent_registry: AgentRegistry | None = agent_registry

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        """冻结身份、模型、工具定义、提示词和上下文预算。"""
        identity: AgentIdentity = self._resolve_identity(request.agent_id)
        tools: tuple[ToolDefinition, ...] = tuple(
            ToolDefinition(definition.name, definition.description, definition.parameters)
            for definition in self._allowed_definitions(identity)
        )
        identity_version: str = _identity_version(identity)
        return AgentPolicySnapshot(
            agent_id=identity.agent_id,
            identity_version=identity_version,
            model_id=identity.resolve_model(self._config.llm.default_model),
            max_iterations=identity.max_loop_steps,
            policy_data={
                "system_prompt": identity.resolve_system_prompt() or self._config.agent.system_prompt,
                "tools": [tool.to_dict() for tool in tools],
                "project_root": str(self._project_root),
                "max_context_tokens": self._config.agent.max_context_tokens,
            },
        )

    def _resolve_identity(self, agent_id: str) -> AgentIdentity:
        """解析主 Agent 或登记的 delegation target Identity。"""
        if agent_id == self._identity.agent_id:
            return self._identity
        if self._agent_registry is None:
            raise ValueError(f"策略端口未装配 Agent {agent_id}")
        identity: AgentIdentity | None = self._agent_registry.get(agent_id)
        if identity is None:
            raise ValueError(f"未找到 delegation target Agent {agent_id}")
        return identity

    def _allowed_definitions(self, identity: AgentIdentity) -> list[LegacyToolDefinition]:
        """按 Agent 白名单过滤工具，并排除 v2 未承载的旧 Task 协议工具。"""
        definitions: list[LegacyToolDefinition] = self._executor.get_definitions()
        if not identity.allowed_tools:
            return [definition for definition in definitions if definition.name not in LEGACY_TASK_TOOL_NAMES]
        allowed: set[str] = set(identity.allowed_tools)
        return [
            definition
            for definition in definitions
            if definition.name in allowed and definition.name not in LEGACY_TASK_TOOL_NAMES
        ]


def _identity_version(identity: AgentIdentity) -> str:
    """根据身份约束生成稳定版本，供 scoped cache 隔离。"""
    source = repr(identity).encode("utf-8")
    return hashlib.sha256(source).hexdigest()[:16]
