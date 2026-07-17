"""从既有 AgentIdentity 和配置冻结 Runtime v2 执行策略。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ...agent.identity import AgentIdentity
from ...config.settings import Config
from ...tools.executor import ToolExecutor
from ..application.ports import RunPolicyPort
from ..domain.models import AgentPolicySnapshot, RunRequest, ToolDefinition


class AgentPolicyPort(RunPolicyPort):
    """将旧 Agent 配置转换为一次 Run 不可变的策略快照。"""

    def __init__(self, identity: AgentIdentity, config: Config, executor: ToolExecutor, project_root: Path) -> None:
        """绑定一个 Agent 的身份、配置和工具注册表。"""
        self._identity: AgentIdentity = identity
        self._config: Config = config
        self._executor: ToolExecutor = executor
        self._project_root: Path = project_root

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        """冻结身份、模型、工具定义、提示词和上下文预算。"""
        if request.agent_id != self._identity.agent_id:
            raise ValueError(f"策略端口未装配 Agent {request.agent_id}")
        tools: tuple[ToolDefinition, ...] = tuple(
            ToolDefinition(definition.name, definition.description, definition.parameters)
            for definition in self._allowed_definitions()
        )
        identity_version = _identity_version(self._identity)
        return AgentPolicySnapshot(
            agent_id=self._identity.agent_id,
            identity_version=identity_version,
            model_id=self._identity.resolve_model(self._config.llm.default_model),
            max_iterations=self._identity.max_loop_steps,
            policy_data={
                "system_prompt": self._identity.resolve_system_prompt() or self._config.agent.system_prompt,
                "tools": [tool.to_dict() for tool in tools],
                "project_root": str(self._project_root),
                "max_context_tokens": self._config.agent.max_context_tokens,
            },
        )

    def _allowed_definitions(self) -> list:
        """按 Agent 白名单过滤既有工具定义。"""
        definitions = self._executor.get_definitions()
        if not self._identity.allowed_tools:
            return definitions
        allowed = set(self._identity.allowed_tools)
        return [definition for definition in definitions if definition.name in allowed]


def _identity_version(identity: AgentIdentity) -> str:
    """根据身份约束生成稳定版本，供 scoped cache 隔离。"""
    source = repr(identity).encode("utf-8")
    return hashlib.sha256(source).hexdigest()[:16]
