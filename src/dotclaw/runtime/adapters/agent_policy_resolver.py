"""从既有 AgentIdentity 和配置冻结 Runtime v4 执行策略。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ...agent.identity import AgentIdentity
from ...config.settings import Config
from ...config.settings import RouterConfig
from ...orchestration.registry import AgentRegistry
from ...tools.base import ToolDefinition as LegacyToolDefinition
from ...tools.executor import ToolExecutor
from ..application.dto import RunRequest, ToolDefinition
from ..application.ports import RunPolicyPort
from ..domain.facts import AgentPolicySnapshot


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
        router_config: RouterConfig | None = None,
    ) -> None:
        """绑定一个 Agent 的身份、配置和工具注册表。"""
        self._identity: AgentIdentity = identity
        self._config: Config = config
        self._executor: ToolExecutor = executor
        self._project_root: Path = project_root
        self._agent_registry: AgentRegistry | None = agent_registry
        self._router_config: RouterConfig | None = router_config

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        """冻结身份、模型、工具定义、提示词和上下文预算。"""
        identity: AgentIdentity = self._resolve_identity(request.agent_id)
        tools: tuple[ToolDefinition, ...] = tuple(
            ToolDefinition(definition.name, definition.description, definition.parameters)
            for definition in self._allowed_definitions(identity)
        )
        identity_version: str = _identity_version(identity)
        model_name: str = identity.resolve_model(self._config.llm.default_model)
        context_window, tokenizer_encoding = self._model_budget_settings(model_name)
        compaction_model, compaction_tokenizer = resolve_compaction_settings(
            model_name, self._router_config, self._config.llm.default_model
        )
        return AgentPolicySnapshot(
            agent_id=identity.agent_id,
            identity_version=identity_version,
            model_id=model_name,
            max_iterations=identity.max_loop_steps,
            policy_data={
                "system_prompt": identity.resolve_system_prompt() or self._config.agent.system_prompt,
                "tools": [tool.to_dict() for tool in tools],
                "project_root": str(self._project_root),
                "max_context_tokens": self._config.agent.max_context_tokens,
                "context_window": context_window,
                "tokenizer_encoding": tokenizer_encoding,
                "context_compaction_model": compaction_model,
                "context_compaction_tokenizer_encoding": compaction_tokenizer,
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
        """按 Agent 白名单过滤工具，并排除 v2 未承载的旧 Task 协议工具。

        工具集在每次 Run 解析时通过 snapshot_definitions() 捕获一次不可变快照，
        Run 内不再读取动态 Registry（总体设计 §9 / 开发计划阶段四）。
        """
        definitions: list[LegacyToolDefinition] = self._executor.snapshot_definitions()
        if not identity.allowed_tools:
            return [definition for definition in definitions if definition.name not in LEGACY_TASK_TOOL_NAMES]
        allowed: set[str] = set(identity.allowed_tools)
        return [
            definition
            for definition in definitions
            if definition.name in allowed and definition.name not in LEGACY_TASK_TOOL_NAMES
        ]

    def _model_budget_settings(self, model_name: str) -> tuple[int, str]:
        """冻结模型窗口与显式 Tokenizer 编码；未配置时拒绝由后续预算端口处理。"""
        if self._router_config is None:
            return self._config.agent.max_context_tokens, ""
        model = self._router_config.models.get(model_name)
        if model is None:
            return self._config.agent.max_context_tokens, ""
        return model.context_window, model.tokenizer_encoding


def _identity_version(identity: AgentIdentity) -> str:
    """根据身份约束生成稳定版本，供 scoped cache 隔离。"""
    source = repr(identity).encode("utf-8")
    return hashlib.sha256(source).hexdigest()[:16]


def resolve_compaction_settings(
    model_name: str,
    router_config: RouterConfig | None,
    default_model: str,
) -> tuple[str, str]:
    """确定性解析上下文压缩模型与 Tokenizer 编码（开发计划阶段4 修改项5）。

    优先级：RouterConfig 中该模型项 -> 回退到请求模型名 -> 无 RouterConfig 时回退默认模型；
    Tokenizer 编码缺失时回退空串，由预算端口在真正需要时拒绝。去除原硬编码的魔法字符串。
    """
    if router_config is None:
        return default_model, ""
    model = router_config.models.get(model_name)
    if model is None:
        return model_name, ""
    return model.model_id, model.tokenizer_encoding
