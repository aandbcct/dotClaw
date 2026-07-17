"""将 Slot、缓存和冻结运行输入组装为 ContextBundle。"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from dotclaw.runtime.application.execution import RunExecutionView
from ..runtime.domain.models import (
    ConversationMessage,
    ContextBundle,
    ContextMetadata,
    JSONMap,
    JSONValue,
    MessageRole,
    RunMessage,
    RunMessageKind,
    RunRequest,
    ToolDefinition,
    get_integer,
    get_string,
    require_json_map,
)
from .ports import ContextDependencies
from .scoped_cache import ScopedCache
from .slot_context import ContextProfile, SlotContext
from .slots import ContextSlot


class ContextBudgetPolicy(StrEnum):
    """超出 token 预算时的上下文处理策略。"""

    TRUNCATE_HISTORY = "truncate_history"
    REJECT = "reject"


class ContextTokenBudgetExceeded(ValueError):
    """保留必要消息后仍超过 token 预算时抛出的异常。"""


class SlotContextProvider:
    """ContextPort 实现：按作用域缓存 Slot 产物并生成完整模型上下文。"""

    def __init__(
        self,
        slots: tuple[ContextSlot, ...],
        dependencies: ContextDependencies,
        cache: ScopedCache | None = None,
        budget_policy: ContextBudgetPolicy = ContextBudgetPolicy.TRUNCATE_HISTORY,
    ) -> None:
        """初始化 Slot 列表、外部内容来源和作用域缓存。"""
        self._slots: tuple[ContextSlot, ...] = slots
        self._dependencies: ContextDependencies = dependencies
        self._cache: ScopedCache = cache if cache is not None else ScopedCache()
        self._budget_policy: ContextBudgetPolicy = budget_policy

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        """从冻结请求和执行视图构造实际发送给模型的完整消息。"""
        profile: ContextProfile = _profile_from_execution(request, execution)
        context: SlotContext = SlotContext(request, execution, profile, self._dependencies)
        slot_texts: list[str] = []
        source_names: list[str] = []
        failed_slots: list[str] = []
        slot: ContextSlot
        for slot in self._slots:
            if slot.name in profile.excluded_slot_names:
                continue
            try:
                content: str | None = await self._load_slot(slot, context)
            except Exception:
                failed_slots.append(slot.name)
                continue
            if content:
                slot_texts.append(content)
                source_names.append(slot.name)
        system_content: str = "\n\n".join(slot_texts)
        messages: tuple[RunMessage, ...] = _build_messages(
            request,
            execution,
            system_content,
        )
        budget_result: tuple[tuple[RunMessage, ...], bool] = self._apply_budget(
            messages,
            profile.max_context_tokens,
        )
        budgeted_messages: tuple[RunMessage, ...]
        truncated: bool
        budgeted_messages, truncated = budget_result
        metadata: ContextMetadata = ContextMetadata(
            estimated_tokens=_estimate_tokens(budgeted_messages),
            source_names=tuple(source_names),
            truncation_applied=truncated,
            details={
                "failed_slots": failed_slots,
                "budget_policy": self._budget_policy.value,
                "max_context_tokens": profile.max_context_tokens,
            },
        )
        return ContextBundle(messages=budgeted_messages, tools=profile.tools, metadata=metadata)

    async def _load_slot(self, slot: ContextSlot, context: SlotContext) -> str | None:
        """按槽位声明的作用域读取或生成缓存内容。"""
        cache_key = self._cache.build_key(
            slot_name=slot.name,
            scope=slot.scope,
            agent_id=context.profile.agent_id,
            identity_version=context.profile.identity_version,
            session_id=context.request.session_id,
            run_id=context.execution.run_id,
        )
        if cache_key is None:
            return await slot.produce(context)
        lookup = self._cache.get(cache_key)
        if lookup.found:
            return lookup.content
        content: str | None = await slot.produce(context)
        self._cache.set(cache_key, content)
        return content

    def _apply_budget(
        self,
        messages: tuple[RunMessage, ...],
        max_context_tokens: int,
    ) -> tuple[tuple[RunMessage, ...], bool]:
        """按策略裁剪最旧历史，必要消息无法容纳时明确失败。"""
        if max_context_tokens <= 0:
            raise ContextTokenBudgetExceeded("Context token 预算必须为正数")
        if _estimate_tokens(messages) <= max_context_tokens:
            return messages, False
        if self._budget_policy is ContextBudgetPolicy.REJECT:
            raise ContextTokenBudgetExceeded("Context 超出 token 预算")
        system_messages: tuple[RunMessage, ...] = tuple(
            message for message in messages if message.role is MessageRole.SYSTEM
        )
        non_system_messages: list[RunMessage] = [
            message for message in messages if message.role is not MessageRole.SYSTEM
        ]
        while len(non_system_messages) > 1:
            candidate_messages: tuple[RunMessage, ...] = system_messages + tuple(non_system_messages)
            if _estimate_tokens(candidate_messages) <= max_context_tokens:
                return _resequenced(candidate_messages), True
            non_system_messages.pop(0)
        remaining_messages: tuple[RunMessage, ...] = system_messages + tuple(non_system_messages)
        if _estimate_tokens(remaining_messages) > max_context_tokens:
            raise ContextTokenBudgetExceeded("必要 system prompt 与当前用户输入已超出 token 预算")
        return _resequenced(remaining_messages), True


def _profile_from_execution(request: RunRequest, execution: RunExecutionView) -> ContextProfile:
    """从 AgentPolicySnapshot 的 JSON 策略解析上下文构建配置。"""
    policy_data: JSONMap = execution.policy.policy_data
    raw_tools: JSONValue | None = policy_data.get("tools")
    tools: list[ToolDefinition] = []
    if isinstance(raw_tools, list):
        raw_tool: JSONValue
        for raw_tool in raw_tools:
            tool_data: JSONMap = require_json_map(raw_tool)
            tools.append(ToolDefinition(
                name=get_string(tool_data, "name"),
                description=get_string(tool_data, "description"),
                parameters=_json_map_or_empty(tool_data.get("parameters")),
            ))
    raw_excluded: JSONValue | None = policy_data.get("excluded_slot_names")
    excluded_names: frozenset[str] = frozenset(
        value for value in raw_excluded if isinstance(value, str)
    ) if isinstance(raw_excluded, list) else frozenset()
    project_root: Path = Path(get_string(policy_data, "project_root", ".")).resolve()
    max_context_tokens: int = get_integer(policy_data, "max_context_tokens", 8000)
    return ContextProfile(
        agent_id=request.agent_id,
        identity_version=execution.policy.identity_version,
        system_prompt=get_string(policy_data, "system_prompt"),
        tools=tuple(tools),
        project_root=project_root,
        max_context_tokens=max_context_tokens,
        excluded_slot_names=excluded_names,
    )


def _build_messages(
    request: RunRequest,
    execution: RunExecutionView,
    system_content: str,
) -> tuple[RunMessage, ...]:
    """将会话快照、当前输入及本 Run 的 ReAct 证据转换为完整 LLM 请求消息。"""
    messages: list[RunMessage] = []
    sequence: int = 1
    messages.append(_run_message(execution.run_id, sequence, MessageRole.SYSTEM, system_content))
    sequence += 1
    conversation_message: ConversationMessage
    for conversation_message in request.conversation.messages:
        messages.append(_run_message(
            execution.run_id,
            sequence,
            conversation_message.role,
            conversation_message.content,
        ))
        sequence += 1
    messages.append(_run_message(
        execution.run_id,
        sequence,
        request.user_message.role,
        request.user_message.content,
    ))
    sequence += 1
    run_message: RunMessage
    for run_message in execution.run_messages:
        if run_message.kind not in {
            RunMessageKind.LLM_RESPONSE,
            RunMessageKind.TOOL_RESULT,
        }:
            continue
        messages.append(RunMessage(
            message_id=f"context-{execution.run_id}-{sequence}",
            sequence=sequence,
            kind=run_message.kind,
            role=run_message.role,
            content=run_message.content,
            tool_call_id=run_message.tool_call_id,
            name=run_message.name,
            tool_calls=run_message.tool_calls,
            metadata=run_message.metadata,
        ))
        sequence += 1
    return tuple(messages)


def _run_message(run_id: str, sequence: int, role: MessageRole, content: str) -> RunMessage:
    """创建一条 ContextPort 输出的实际 LLM 请求消息。"""
    return RunMessage(
        message_id=f"context-{run_id}-{sequence}",
        sequence=sequence,
        kind=RunMessageKind.LLM_REQUEST,
        role=role,
        content=content,
    )


def _estimate_tokens(messages: tuple[RunMessage, ...]) -> int:
    """使用保守的字符估算计算上下文 token 数。"""
    character_count: int = sum(len(message.content) for message in messages)
    return max((character_count + 3) // 4, 1)


def _resequenced(messages: tuple[RunMessage, ...]) -> tuple[RunMessage, ...]:
    """裁剪后重新生成连续消息序号和稳定标识。"""
    resequenced: list[RunMessage] = []
    sequence: int
    message: RunMessage
    for sequence, message in enumerate(messages, start=1):
        resequenced.append(RunMessage(
            message_id=f"context-{message.message_id.rsplit('-', 1)[0]}-{sequence}",
            sequence=sequence,
            kind=message.kind,
            role=message.role,
            content=message.content,
            tool_call_id=message.tool_call_id,
            name=message.name,
            tool_calls=message.tool_calls,
            metadata=message.metadata,
        ))
    return tuple(resequenced)


def _json_map_or_empty(value: JSONValue | None) -> JSONMap:
    """将 JSON 值收窄为对象，非对象时返回空对象。"""
    return value if isinstance(value, dict) else {}
