"""个人助理上下文进入模型输入的回归验证。"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from dotclaw.assistant.models import AssistantInputRef
from dotclaw.context import (
    ContextCacheScope,
    ContextContribution,
    ContextDependencies,
    ContextOwnerPlanConfiguration,
    ContextPlanResolver,
    ContextRefreshPolicy,
    ContextSignalBus,
    ContextSlotDescriptor,
    ContextSlotBinding,
    ContextSlotManager,
    ContextSlotRegistry,
    ContextProvider,
    build_context_provider,
    default_context_plan_configuration,
)
from dotclaw.context.slots import AssistantContextSlot
from dotclaw.runtime.application.dto import ConversationMessage, ConversationSnapshot, RunRequest
from dotclaw.runtime.application.execution import RunBudget, RunExecution
from dotclaw.runtime.domain.context import ContextContributionKind, ContextOwner, ContextPersistenceMode, ContextVersion
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, MessageRole, RunMessage, RunMessageKind, ToolCall
from dotclaw.runtime.domain.state import AgentRunState, RunStage, Running


@dataclass
class AssistantSummaryReader:
    """返回本轮助理业务摘要并记录读取参数。"""

    summary: str = '{"version":1,"summary":"助理摘要"}'
    reads: list[tuple[str, str, str]] | None = None

    async def read(self, session_id: str, input_ref: AssistantInputRef) -> str:
        if self.reads is None:
            self.reads = []
        self.reads.append((session_id, input_ref.run_id, input_ref.message_id))
        return self.summary


class FailedSummarySlot:
    """模拟已绑定但加载失败的助理摘要 Slot。"""

    async def load(self, binding: ContextSlotBinding) -> ContextContribution:
        raise RuntimeError("摘要来源不可用")


def _execution() -> RunExecution:
    """构造包含当前输入及工具往返消息的真实执行快照。"""
    user = ConversationMessage("input-1", MessageRole.USER, "现在查一下", "2026-11-23T09:30:00+08:00")
    history = ConversationMessage("history-1", MessageRole.ASSISTANT, "之前的回答", "2026-11-23T09:00:00+08:00")
    request = RunRequest(
        "personal-assistant",
        "lease-1",
        "personal-assistant",
        user,
        ConversationSnapshot("personal-assistant", (history,), 1),
        run_id="run-assistant-1",
    )
    messages = (
        RunMessage("input-1", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "现在查一下"),
        RunMessage("assistant-call-1", 2, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "", tool_calls=(ToolCall("call-1", "lookup", {}),)),
        RunMessage("tool-result-1", 3, RunMessageKind.TOOL_RESULT, MessageRole.TOOL, "查询结果", tool_call_id="call-1"),
    )
    policy = AgentPolicySnapshot(
        "personal-assistant",
        "identity-v1",
        "model-v1",
        8,
        policy_data={
            "system_prompt": "助理身份",
            "tools": [{"name": "lookup", "description": "查询", "parameters": {"type": "object"}}],
        },
    )
    return RunExecution(
        "run-assistant-1",
        request,
        policy,
        AgentRunState(mode=Running(RunStage.CALLING_LLM)),
        RunBudget(8),
        run_messages=messages,
    )


@pytest.mark.asyncio
async def test_assistant_context_includes_current_input_history_summary_and_tool_result() -> None:
    """助理本轮摘要注入后，所有必要运行事实都进入实际模型上下文。"""
    reader = AssistantSummaryReader()
    provider = build_context_provider(ContextDependencies(assistant_context_reader=reader))
    execution = _execution()

    bundle = await provider.build(execution.request, execution.view())

    contents = [message.content for message in bundle.messages]
    assert "之前的回答" in contents
    assert "现在查一下" in contents
    assert any("助理摘要" in content for content in contents)
    assert "查询结果" in contents
    assert bundle.tools[0].name == "lookup"
    assert reader.reads == [("personal-assistant", "run-assistant-1", "input-1")]


@pytest.mark.asyncio
async def test_assistant_context_fails_when_required_summary_slot_is_not_enabled() -> None:
    """个人助理的必需摘要 Slot 未配置时不得只带身份继续调用模型。"""
    defaults = default_context_plan_configuration()
    configurations = tuple(
        ContextOwnerPlanConfiguration(item.owner, tuple(slot_id for slot_id in item.slot_ids if slot_id != "assistant_context"))
        if item.owner is ContextOwner.RUN
        else item
        for item in defaults.default_configurations
    )
    provider = build_context_provider(ContextDependencies(
        assistant_context_reader=AssistantSummaryReader(),
        plan_configuration=replace(defaults, default_configurations=configurations),
    ))
    execution = _execution()

    with pytest.raises(ValueError, match="assistant_context"):
        await provider.build(execution.request, execution.view())


@pytest.mark.asyncio
async def test_assistant_context_fails_when_required_summary_slot_load_fails() -> None:
    """Slot Manager 将必需摘要加载失败归类后，Provider 必须拒绝模型输入。"""
    registry = ContextSlotRegistry()
    registry.register(
        ContextSlotDescriptor("assistant_context", ContextOwner.RUN, ContextContributionKind.SYSTEM_CONTENT,
                              ContextPersistenceMode.SNAPSHOT, ContextCacheScope.NONE, ContextRefreshPolicy.SIGNAL, 65),
        FailedSummarySlot,
    )
    configuration = replace(
        default_context_plan_configuration(),
        default_configurations=(ContextOwnerPlanConfiguration(ContextOwner.RUN, ("assistant_context",)),),
    )
    provider = ContextProvider(
        ContextPlanResolver(registry, configuration),
        ContextSlotManager(registry, ContextSignalBus()),
        ContextDependencies(assistant_context_reader=AssistantSummaryReader()),
    )
    execution = _execution()

    with pytest.raises(ValueError, match="assistant_context"):
        await provider.build(execution.request, execution.view())


@pytest.mark.asyncio
async def test_assistant_context_fails_when_current_input_slot_is_missing() -> None:
    """有效摘要不能掩盖当前用户消息事实的缺失。"""
    registry = ContextSlotRegistry()
    registry.register(
        ContextSlotDescriptor("assistant_context", ContextOwner.RUN, ContextContributionKind.SYSTEM_CONTENT,
                              ContextPersistenceMode.SNAPSHOT, ContextCacheScope.NONE, ContextRefreshPolicy.SIGNAL, 65),
        AssistantContextSlot,
    )
    configuration = replace(
        default_context_plan_configuration(),
        default_configurations=(ContextOwnerPlanConfiguration(ContextOwner.RUN, ("assistant_context",)),),
    )
    provider = ContextProvider(
        ContextPlanResolver(registry, configuration),
        ContextSlotManager(registry, ContextSignalBus()),
        ContextDependencies(assistant_context_reader=AssistantSummaryReader()),
    )
    execution = _execution()

    with pytest.raises(ValueError, match="当前用户输入"):
        await provider.build(execution.request, execution.view())


@pytest.mark.asyncio
async def test_assistant_frozen_context_replay_keeps_summary_and_current_tool_evidence() -> None:
    """审批或恢复重放沿用已冻结摘要，并重新接回当前 Run 消息事实。"""
    reader = AssistantSummaryReader()
    provider = build_context_provider(ContextDependencies(assistant_context_reader=reader))
    execution = _execution()
    original = await provider.build(execution.request, execution.view())
    replay_view = replace(
        execution.view(),
        active_context_version=ContextVersion(1, "2026-11-23T01:30:00Z", original.metadata.slot_snapshots, "content-hash", "tool-hash"),
        replay_active_context=True,
    )

    replayed = await provider.build(execution.request, replay_view)

    contents = [message.content for message in replayed.messages]
    assert any("助理摘要" in content for content in contents)
    assert "现在查一下" in contents
    assert "查询结果" in contents
    assert replayed.tools[0].name == "lookup"
    assert len(reader.reads or []) == 1
