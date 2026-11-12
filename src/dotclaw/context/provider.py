"""基于 Context Plan 的 ContextPort 实现。"""

from __future__ import annotations

from collections import Counter
from hashlib import sha256

from dotclaw.runtime.application.dto import ContextBundle, ContextMetadata, RunRequest, ToolDefinition
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.domain.context import (
    ContextContributionKind,
    ContextOwner,
    ContextPersistenceMode,
    ContextSlotSnapshot,
    ContextSlotStatus,
    ConversationMessagesSlotContent,
    RunMessageReferencesSlotContent,
    TextSlotContent,
    ToolDefinitionsSlotContent,
)
from dotclaw.runtime.domain.facts import JSONMap, JSONValue, MessageRole, RunMessage, RunMessageKind
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.control import AgentAction

from .contracts import ContextContribution, ContextOwnerSnapshot, ContextSlotBinding
from .plan_resolver import ContextPlanResolver
from .ports import ContextDependencies, MemorySearchRecord
from .signals import ContextRefreshSignal
from .slot_manager import ContextSlotManager


class ContextProvider:
    """从 Owner 快照解析 Plan 并物化为结构化模型上下文。"""

    def __init__(self, resolver: ContextPlanResolver, manager: ContextSlotManager, dependencies: ContextDependencies) -> None:
        self._resolver: ContextPlanResolver = resolver
        self._manager: ContextSlotManager = manager
        self._dependencies: ContextDependencies = dependencies

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        """加载当前有效 Slot，并把事实引用型消息直接还原到本次输入。"""
        if execution.replay_active_context and execution.active_context_version is not None:
            return _bundle_from_active_version(execution)
        owner_data: dict[ContextOwner, ContextOwnerSnapshot] = await self._owner_data(request, execution)
        plan = self._resolver.resolve(owner_data)
        contributions: tuple[ContextContribution, ...] = await self._manager.load_plan(plan)
        snapshots: tuple[ContextSlotSnapshot, ...] = _snapshots(plan.bindings, contributions)
        context_run_messages = _filter_legacy_approval_results(execution.run_messages, execution.run_events)
        messages: tuple[RunMessage, ...] = _messages_from_contributions(execution.run_id, contributions, context_run_messages)
        if execution.action is AgentAction.INVOKE_LLM:
            _validate_tool_message_pairs(messages)
        tools: tuple[ToolDefinition, ...] = _tools_from_contributions(contributions)
        visible_ids = {message.message_id for message in context_run_messages}
        fact_ids: tuple[str, ...] = tuple(message_id for message_id in _fact_reference_ids(contributions) if message_id in visible_ids)
        return ContextBundle(messages, tools, ContextMetadata(0, tuple(binding.descriptor.slot_id for binding in plan.bindings), slot_snapshots=snapshots, fact_reference_message_ids=fact_ids))

    async def release_scope(self, owner: ContextOwner, owner_key: str) -> None:
        """由 Owner 生命周期终点释放对应缓存实例。"""
        await self._manager.release_scope(owner, owner_key)

    async def release_all(self) -> None:
        """Host 关闭时释放全部缓存 Slot 实例。"""
        await self._manager.release_all()

    def request_refresh(self, slot_id: str, owner: ContextOwner, owner_key: str) -> None:
        """向 Manager 请求精确 Owner 的 Slot 在下一安全点刷新。"""
        self._manager.request_refresh(slot_id, owner, owner_key)

    def publish_signal(self, signal: ContextRefreshSignal) -> None:
        """通过 ContextPort 发布类型化刷新事件。"""
        self._manager.publish_signal(signal)

    async def _owner_data(self, request: RunRequest, execution: RunExecutionView) -> dict[ContextOwner, ContextOwnerSnapshot]:
        """在 Provider 边界读取 Owner 数据；Manager 永不读取外部领域数据。"""
        policy_tools: JSONValue | None = execution.policy.policy_data.get("tools")
        history_text: str = request.conversation.compressed_history.content if request.conversation.compressed_history is not None else ""
        return {
            ContextOwner.AGENT: ContextOwnerSnapshot(request.agent_id, {"system_prompt": _string_value(execution.policy.policy_data, "system_prompt"), "skills_text": _skills_text(self._dependencies), "tools": policy_tools if isinstance(policy_tools, list) else []}),
            ContextOwner.SESSION: ContextOwnerSnapshot(request.session_id, {"history_compression": history_text, "user_info_text": _user_info_text(self._dependencies)}),
            ContextOwner.RUN: ContextOwnerSnapshot(execution.run_id, {"conversation_messages": [message.to_dict() for message in request.conversation.messages], "message_ids": [message.message_id for message in execution.run_messages], "memory_text": await _memory_text(self._dependencies, request.user_message.content), "knowledge_text": await _knowledge_text(self._dependencies, request.user_message.content)}),
            ContextOwner.GLOBAL: ContextOwnerSnapshot("global", {"available_agents_text": _available_agents_text(self._dependencies)}),
        }


def _messages_from_contributions(run_id: str, contributions: tuple[ContextContribution, ...], run_messages: tuple[RunMessage, ...]) -> tuple[RunMessage, ...]:
    """按 Slot 顺序将快照内容和事实引用还原为实际 LLM 输入。"""
    messages: list[RunMessage] = []
    sequence: int = 1
    contribution: ContextContribution
    for contribution in contributions:
        if contribution.status is not ContextSlotStatus.INCLUDED:
            continue
        content = contribution.content
        if contribution.kind is ContextContributionKind.SYSTEM_CONTENT and isinstance(content, TextSlotContent):
            messages.append(_context_message(run_id, sequence, MessageRole.SYSTEM, content.text))
            sequence += 1
        elif contribution.kind is ContextContributionKind.HISTORY_COMPRESSIONS and isinstance(content, TextSlotContent):
            messages.append(_context_message(run_id, sequence, MessageRole.SYSTEM, f"以下是此前对话的压缩摘要：\n{content.text}"))
            sequence += 1
        elif contribution.kind is ContextContributionKind.CONVERSATION_MESSAGES and isinstance(content, ConversationMessagesSlotContent):
            for conversation_message in content.messages:
                messages.append(_context_message(run_id, sequence, conversation_message.role, conversation_message.content))
                sequence += 1
        elif contribution.kind is ContextContributionKind.RUN_MESSAGE_REFERENCES and isinstance(content, RunMessageReferencesSlotContent):
            indexed: dict[str, RunMessage] = {message.message_id: message for message in run_messages}
            for message_id in content.message_ids:
                message: RunMessage | None = indexed.get(message_id)
                if message is not None:
                    messages.append(_replay_message(run_id, sequence, message))
                    sequence += 1
    return tuple(messages)


def _tools_from_contributions(contributions: tuple[ContextContribution, ...]) -> tuple[ToolDefinition, ...]:
    """仅从 tools Slot 的实际筛选 Schema 构造 ContextBundle.tools。"""
    for contribution in contributions:
        if contribution.kind is ContextContributionKind.TOOL_DEFINITIONS and isinstance(contribution.content, ToolDefinitionsSlotContent):
            return tuple(ToolDefinition(item.name, item.description, item.parameters) for item in contribution.content.tools)
    return ()


def _fact_reference_ids(contributions: tuple[ContextContribution, ...]) -> tuple[str, ...]:
    """提取本轮 LLM_STARTED 必须记录的动态事实引用。"""
    for contribution in contributions:
        if contribution.kind is ContextContributionKind.RUN_MESSAGE_REFERENCES and isinstance(contribution.content, RunMessageReferencesSlotContent):
            return contribution.content.message_ids
    return ()


def _bundle_from_active_version(execution: RunExecutionView) -> ContextBundle:
    """审批或中断恢复时以活动快照加当前事实重新构造输入。"""
    active: tuple[ContextSlotSnapshot, ...] = execution.active_context_version.slots if execution.active_context_version is not None else ()
    contributions: list[ContextContribution] = []
    for slot in active:
        contributions.append(ContextContribution(slot.contribution_kind, slot.status, slot.content, slot.error_code))
    context_run_messages = _filter_legacy_approval_results(execution.run_messages, execution.run_events)
    fact_content: RunMessageReferencesSlotContent = RunMessageReferencesSlotContent(tuple(message.message_id for message in context_run_messages))
    contributions.append(ContextContribution(ContextContributionKind.RUN_MESSAGE_REFERENCES, ContextSlotStatus.INCLUDED if fact_content.message_ids else ContextSlotStatus.EMPTY, fact_content))
    frozen: tuple[ContextContribution, ...] = tuple(contributions)
    messages = _messages_from_contributions(execution.run_id, frozen, context_run_messages)
    if execution.action is AgentAction.INVOKE_LLM:
        _validate_tool_message_pairs(messages)
    return ContextBundle(messages, _tools_from_contributions(frozen), ContextMetadata(0, tuple(slot.slot_id for slot in active), slot_snapshots=active, fact_reference_message_ids=fact_content.message_ids))


def _filter_legacy_approval_results(messages: tuple[RunMessage, ...], events: tuple[RunEvent, ...]) -> tuple[RunMessage, ...]:
    """仅从模型输入中排除旧审批事件精确引用的假工具结果。"""
    indexed = {message.message_id: message for message in messages}
    excluded: set[str] = set()
    for event in events:
        if (
            event.event_type is not RunEventType.TOOL_COMPLETED
            or event.data.get("status") != "approval_required"
            or event.data.get("tool_protocol_version") is not None
            or event.data.get("commit_id") is not None
        ):
            continue
        call_id = event.data.get("call_id")
        if not isinstance(call_id, str):
            raise ValueError("旧审批假结果事件缺少 call_id")
        source_id = event.data.get("source_response_message_id")
        if not isinstance(source_id, str):
            source_id = next((message.message_id for message in messages if message.role is MessageRole.ASSISTANT and any(call.call_id == call_id for call in message.tool_calls)), None)
        explicit_result_id = event.data.get("result_message_id")
        references = set(event.message_ids)
        if explicit_result_id is not None and explicit_result_id != "":
            if not isinstance(explicit_result_id, str):
                raise ValueError("旧审批假结果引用格式无效")
            references.add(explicit_result_id)
        for message_id in references:
            message = indexed.get(message_id)
            if message is None:
                if message_id != source_id:
                    raise ValueError("旧审批事件引用的消息不存在")
                continue
            if message.kind is RunMessageKind.TOOL_RESULT and message.role is MessageRole.TOOL:
                if message.tool_call_id != call_id:
                    raise ValueError("旧审批事件引用了其他工具调用的结果消息")
                excluded.add(message_id)
            elif message_id != source_id:
                raise ValueError("旧审批事件引用未匹配源助手或假工具结果消息")
    return tuple(message for message in messages if message.message_id not in excluded)


def _validate_tool_message_pairs(messages: tuple[RunMessage, ...]) -> None:
    """进入模型前核对助手工具调用与真实结果严格配对。"""
    call_ids = [call.call_id for message in messages if message.role is MessageRole.ASSISTANT for call in message.tool_calls]
    result_ids = [message.tool_call_id for message in messages if message.role is MessageRole.TOOL and message.tool_call_id]
    call_counts = Counter(call_ids)
    result_counts = Counter(result_ids)
    if any(count != 1 for count in call_counts.values()) or any(result_counts.get(call_id, 0) != 1 for call_id in call_counts):
        raise ValueError("模型上下文中的助手工具调用缺少唯一真实结果")
    if any(call_id not in call_counts for call_id in result_counts):
        raise ValueError("模型上下文包含没有来源助手调用的工具结果")


def _snapshots(bindings: tuple[ContextSlotBinding, ...], contributions: tuple[ContextContribution, ...]) -> tuple[ContextSlotSnapshot, ...]:
    """只将 SNAPSHOT 绑定转换为可审计 Context Version Slot。"""
    snapshots: list[ContextSlotSnapshot] = []
    for index, contribution in enumerate(contributions):
        binding: ContextSlotBinding = bindings[index]
        if binding.descriptor.persistence_mode is not ContextPersistenceMode.SNAPSHOT:
            continue
        snapshots.append(ContextSlotSnapshot(binding.descriptor.slot_id, binding.descriptor.owner, contribution.kind, binding.descriptor.persistence_mode, contribution.status, binding.descriptor.order, contribution.content, _content_hash(contribution), contribution.error_code))
    return tuple(snapshots)


def _content_hash(contribution: ContextContribution) -> str:
    """为单个直接载荷生成审计哈希。"""
    from json import dumps
    content_json: str = dumps(_content_json(contribution), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(content_json.encode("utf-8")).hexdigest()


def _content_json(contribution: ContextContribution) -> JSONValue:
    """复用快照序列化以获得规范化内容。"""
    temporary: ContextSlotSnapshot = ContextSlotSnapshot("hash", ContextOwner.RUN, contribution.kind, ContextPersistenceMode.SNAPSHOT, contribution.status, 0, contribution.content)
    return temporary.to_dict()["content"]


def _context_message(run_id: str, sequence: int, role: MessageRole, content: str) -> RunMessage:
    """为快照内容建立仅供 LLM 调用的临时消息。"""
    return RunMessage(f"context-{run_id}-{sequence}", sequence, RunMessageKind.LLM_REQUEST, role, content)


def _replay_message(run_id: str, sequence: int, message: RunMessage) -> RunMessage:
    """将唯一真实 Run Message 重新编号后用于当前模型调用。"""
    return RunMessage(f"context-{run_id}-{sequence}", sequence, RunMessageKind.LLM_REQUEST, message.role, message.content, tool_call_id=message.tool_call_id, name=message.name, tool_calls=message.tool_calls, metadata=message.metadata)


def _string_value(data: JSONMap, key: str) -> str:
    """从冻结策略读取可选文本字段。"""
    value: JSONValue | None = data.get(key)
    return value if isinstance(value, str) else ""


def _skills_text(dependencies: ContextDependencies) -> str:
    """读取技能摘要并保持 Owner 数据读取位于 Provider。"""
    registry = dependencies.skill_registry
    descriptions: str = registry.get_descriptions_block(max_desc_len=20) if registry is not None else ""
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
    """格式化全局 Agent 目录摘要。"""
    registry = dependencies.agent_registry
    if registry is None:
        return ""
    lines: list[str] = []
    for agent in registry.list_all():
        lines.append(f"- **{agent.agent_id}** ({agent.agent_name}): {agent.description or agent.agent_name}。能力：{', '.join(agent.capabilities) if agent.capabilities else '通用'}")
    return "## 可用子 Agent\n" + "\n".join(lines) if lines else ""


async def _memory_text(dependencies: ContextDependencies, query: str) -> str:
    """读取本次 Run 的记忆检索结果。"""
    manager = dependencies.memory_manager
    if manager is None:
        return ""
    results: tuple[MemorySearchRecord, ...] = await manager.search(query)
    return "## 相关记忆\n\n" + "\n".join(f"- ({record.source}:{record.path}) {'[' + record.title + '] ' if record.title else ''}{record.snippet}" for record in results) if results else ""


async def _knowledge_text(dependencies: ContextDependencies, query: str) -> str:
    """读取 Run 检索出的知识摘要。"""
    knowledge_base = dependencies.knowledge_base
    result: str | None = await knowledge_base.search(query) if knowledge_base is not None else None
    return f"## 相关知识\n\n{result}" if result else ""
