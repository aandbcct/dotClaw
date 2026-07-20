"""基于 Context Plan 的 ContextPort 实现。"""

from __future__ import annotations

from hashlib import sha256

from dotclaw.runtime.application.dto import ContextBundle, ContextMetadata, RunRequest, ToolDefinition
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.domain.context import ContextContributionKind, ContextOwner, ContextSlotSnapshot, ContextSlotStatus
from dotclaw.runtime.domain.facts import JSONMap, JSONValue, MessageRole, RunMessage, RunMessageKind

from .contracts import ContextContribution, ContextOwnerSnapshot, ContextSlotBinding
from .plan_resolver import ContextPlanResolver
from .ports import ContextDependencies, MemorySearchRecord
from .signals import ContextRefreshSignal
from .slot_manager import ContextSlotManager


class ContextProvider:
    """从 Owner 快照解析 Plan 并物化为结构化模型上下文。"""
    def __init__(
        self,
        resolver: ContextPlanResolver,
        manager: ContextSlotManager,
        dependencies: ContextDependencies,
    ) -> None:
        self._resolver: ContextPlanResolver = resolver
        self._manager: ContextSlotManager = manager
        self._dependencies: ContextDependencies = dependencies
    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        """加载已绑定 Slot；未启用 Slot 不进入本次上下文。"""
        if execution.replay_active_context and execution.active_context_version is not None:
            return _bundle_from_active_version(request, execution)
        owner_data: dict[ContextOwner, JSONMap] = await self._owner_data(request, execution)
        plan = self._resolver.resolve(owner_data)
        contributions: tuple[ContextContribution, ...] = await self._manager.load_plan(plan)
        messages: list[RunMessage] = []
        sequence: int = 1
        index: int
        for index, contribution in enumerate(contributions):
            if contribution.kind is ContextContributionKind.SYSTEM_CONTENT and contribution.status is ContextSlotStatus.INCLUDED:
                messages.append(RunMessage(f"context-{execution.run_id}-{sequence}", sequence, RunMessageKind.LLM_REQUEST, MessageRole.SYSTEM, contribution.content))
                sequence += 1
        for message in request.conversation.messages:
            messages.append(RunMessage(f"context-{execution.run_id}-{sequence}", sequence, RunMessageKind.LLM_REQUEST, message.role, message.content))
            sequence += 1
        messages.append(RunMessage(f"context-{execution.run_id}-{sequence}", sequence, RunMessageKind.LLM_REQUEST, request.user_message.role, request.user_message.content))
        sequence += 1
        for run_message in execution.run_messages:
            if run_message.message_id == request.user_message.message_id:
                continue
            messages.append(_context_message(execution.run_id, sequence, run_message))
            sequence += 1
        tools: tuple[ToolDefinition, ...] = _tools(execution.policy.policy_data)
        snapshots: tuple[ContextSlotSnapshot, ...] = _snapshots(plan.bindings, contributions)
        return ContextBundle(
            tuple(messages),
            tools,
            ContextMetadata(0, tuple(binding.descriptor.slot_id for binding in plan.bindings), slot_snapshots=snapshots),
        )

    async def release_scope(self, owner: ContextOwner, owner_key: str) -> None:
        """由 Owner 生命周期终点释放对应缓存实例。"""
        await self._manager.release_scope(owner, owner_key)

    def request_refresh(self, slot_id: str, owner: ContextOwner, owner_key: str) -> None:
        """向 Manager 请求精确 Owner 的 Slot 在下一安全点刷新。"""
        self._manager.request_refresh(slot_id, owner, owner_key)

    def publish_signal(self, signal: ContextRefreshSignal) -> None:
        """通过 ContextPort 发布类型化刷新事件，避免外部接触内部总线。"""
        self._manager.publish_signal(signal)

    async def _owner_data(self, request: RunRequest, execution: RunExecutionView) -> dict[ContextOwner, ContextOwnerSnapshot]:
        """在 Provider 边界读取 Owner 数据；Manager 永不读取外部领域数据。"""
        skills_text: str = _skills_text(self._dependencies)
        user_info_text: str = _user_info_text(self._dependencies)
        available_agents_text: str = _available_agents_text(self._dependencies)
        memory_text: str = await _memory_text(self._dependencies, request.user_message.content)
        knowledge_text: str = await _knowledge_text(self._dependencies, request.user_message.content)
        system_prompt_value: JSONValue | None = execution.policy.policy_data.get("system_prompt")
        system_prompt: str = system_prompt_value if isinstance(system_prompt_value, str) else ""
        return {
            ContextOwner.AGENT: ContextOwnerSnapshot(request.agent_id, {"system_prompt": system_prompt, "skills_text": skills_text}),
            ContextOwner.SESSION: ContextOwnerSnapshot(request.session_id, {"conversation": request.conversation.to_dict(), "user_info_text": user_info_text}),
            ContextOwner.RUN: ContextOwnerSnapshot(execution.run_id, {"message_ids": [message.message_id for message in execution.run_messages], "memory_text": memory_text, "knowledge_text": knowledge_text}),
            ContextOwner.GLOBAL: ContextOwnerSnapshot("global", {"available_agents_text": available_agents_text}),
        }


def _tools(policy_data: JSONMap) -> tuple[ToolDefinition, ...]:
    """从 Agent 冻结策略读取实际工具 Schema，绝不写入 system 文本。"""
    raw_tools = policy_data.get("tools")
    if not isinstance(raw_tools, list):
        return ()
    tools: list[ToolDefinition] = []
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, dict):
            continue
        name = raw_tool.get("name")
        description = raw_tool.get("description")
        parameters = raw_tool.get("parameters")
        if isinstance(name, str) and isinstance(description, str) and isinstance(parameters, dict):
            tools.append(ToolDefinition(name, description, parameters))
    return tuple(tools)


def _bundle_from_active_version(request: RunRequest, execution: RunExecutionView) -> ContextBundle:
    """审批恢复时重放冻结 Slot，并追加当前 Run 的增量消息事实。"""
    active_version = execution.active_context_version
    if active_version is None:
        raise ValueError("缺少活动 Context Version")
    messages: list[RunMessage] = []
    sequence: int = 1
    for snapshot in active_version.slots:
        if snapshot.contribution_kind is not ContextContributionKind.SYSTEM_CONTENT:
            continue
        if snapshot.status is not ContextSlotStatus.INCLUDED:
            continue
        messages.append(RunMessage(f"context-{execution.run_id}-{sequence}", sequence, RunMessageKind.LLM_REQUEST, MessageRole.SYSTEM, snapshot.content))
        sequence += 1
    for message in request.conversation.messages:
        messages.append(RunMessage(f"context-{execution.run_id}-{sequence}", sequence, RunMessageKind.LLM_REQUEST, message.role, message.content))
        sequence += 1
    messages.append(RunMessage(f"context-{execution.run_id}-{sequence}", sequence, RunMessageKind.LLM_REQUEST, request.user_message.role, request.user_message.content))
    sequence += 1
    for run_message in execution.run_messages:
        if run_message.message_id == request.user_message.message_id:
            continue
        messages.append(_context_message(execution.run_id, sequence, run_message))
        sequence += 1
    return ContextBundle(
        tuple(messages),
        _tools(execution.policy.policy_data),
        ContextMetadata(0, tuple(snapshot.slot_id for snapshot in active_version.slots), slot_snapshots=active_version.slots),
    )


def _context_message(run_id: str, sequence: int, message: RunMessage) -> RunMessage:
    """将已持久化 Run Message 作为当前调用的增量输入重新编号。"""
    return RunMessage(
        f"context-{run_id}-{sequence}",
        sequence,
        RunMessageKind.LLM_REQUEST,
        message.role,
        message.content,
        tool_call_id=message.tool_call_id,
        name=message.name,
        tool_calls=message.tool_calls,
        metadata=message.metadata,
    )


def _snapshots(
    bindings: tuple[ContextSlotBinding, ...],
    contributions: tuple[ContextContribution, ...],
) -> tuple[ContextSlotSnapshot, ...]:
    """把每个绑定 Slot 的加载结果转换为可审计 E1 快照。"""
    snapshots: list[ContextSlotSnapshot] = []
    for index, contribution in enumerate(contributions):
        binding = bindings[index]
        content_hash: str = sha256(contribution.content.encode("utf-8")).hexdigest() if contribution.content else ""
        snapshots.append(ContextSlotSnapshot(
            slot_id=binding.descriptor.slot_id,
            owner=binding.descriptor.owner,
            contribution_kind=contribution.kind,
            status=contribution.status,
            injection_order=index,
            content=contribution.content,
            content_hash=content_hash,
            message_ids=contribution.message_ids,
            attributes=contribution.attributes,
            error_code=contribution.error_code,
        ))
    return tuple(snapshots)


def _skills_text(dependencies: ContextDependencies) -> str:
    """读取技能摘要并保持 Owner 数据读取位于 Provider。"""
    registry = dependencies.skill_registry
    if registry is None:
        return ""
    descriptions: str = registry.get_descriptions_block(max_desc_len=20)
    return f"## 可用技能\n\n{descriptions}" if descriptions else ""


def _user_info_text(dependencies: ContextDependencies) -> str:
    """格式化可选用户资料。"""
    profile = dependencies.user_profile
    if profile is None:
        return ""
    lines: list[str] = []
    if profile.name:
        lines.append(f"用户: {profile.name}")
    if profile.preferred_language:
        lines.append(f"偏好语言: {profile.preferred_language}")
    return "\n".join(lines)


def _available_agents_text(dependencies: ContextDependencies) -> str:
    """格式化全局可委托 Agent 目录。"""
    registry = dependencies.agent_registry
    if registry is None:
        return ""
    lines: list[str] = []
    for agent in registry.list_all():
        description: str = agent.description or agent.agent_name
        capabilities: str = ", ".join(agent.capabilities) if agent.capabilities else "通用"
        lines.append(f"- **{agent.agent_id}** ({agent.agent_name}): {description}。能力：{capabilities}")
    return "## 可用子 Agent\n" + "\n".join(lines) if lines else ""


async def _memory_text(dependencies: ContextDependencies, query: str) -> str:
    """读取本次 Run 的记忆检索结果。"""
    manager = dependencies.memory_manager
    if manager is None:
        return ""
    results = await manager.search(query)
    lines: list[str] = []
    record: MemorySearchRecord
    for record in results:
        title_prefix: str = f"[{record.title}] " if record.title else ""
        lines.append(f"- ({record.source}:{record.path}) {title_prefix}{record.snippet}")
    return "## 相关记忆\n\n" + "\n".join(lines) if lines else ""


async def _knowledge_text(dependencies: ContextDependencies, query: str) -> str:
    """读取本次 Run 的知识检索摘要。"""
    knowledge_base = dependencies.knowledge_base
    if knowledge_base is None:
        return ""
    result: str | None = await knowledge_base.search(query)
    return f"## 相关知识\n\n{result}" if result else ""
