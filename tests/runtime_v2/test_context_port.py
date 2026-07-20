"""E3 多 Owner Context Slot 的 Port、生命周期与接入契约测试。"""

from __future__ import annotations

from dataclasses import dataclass, replace

from dotclaw.context import (
    ContextCacheScope,
    ContextContribution,
    ContextDependencies,
    ContextPlanResolver,
    ContextProvider,
    ContextRefreshPolicy,
    ContextRefreshSignal,
    ContextSignalBus,
    ContextSlotBinding,
    ContextSlotDescriptor,
    ContextSlotManager,
    ContextSlotRegistry,
    build_context_provider,
)
from dotclaw.runtime.application.dto import ConversationMessage, ConversationSnapshot, RunRequest
from dotclaw.runtime.application.execution import RunBudget, RunExecution
from dotclaw.runtime.domain.context import ContextContributionKind, ContextOwner, ContextSlotStatus
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, MessageRole, RunMessage, RunMessageKind
from dotclaw.runtime.domain.state import AgentState


@dataclass
class RecordingSlot:
    """记录加载、刷新和释放次数的 InMemory Slot。"""

    contribution: ContextContribution
    loads: int = 0
    refreshes: int = 0
    releases: int = 0

    async def load(self, binding: ContextSlotBinding) -> ContextContribution:
        """返回预设贡献并记录加载。"""
        self.loads += 1
        return self.contribution

    async def refresh(self, binding: ContextSlotBinding) -> None:
        """记录下一安全点的定向刷新。"""
        self.refreshes += 1

    async def release(self) -> None:
        """记录 Owner 生命周期释放。"""
        self.releases += 1


class FailingSlot:
    """模拟 Slot 加载失败，验证 Provider 返回结构化失败快照。"""

    async def load(self, binding: ContextSlotBinding) -> ContextContribution:
        """抛出可被 Manager 隔离的异常。"""
        raise RuntimeError("来源不可用")

    async def refresh(self, binding: ContextSlotBinding) -> None:
        """失败 Slot 不保存刷新状态。"""

    async def release(self) -> None:
        """失败 Slot 不持有资源。"""


def _descriptor(slot_id: str, owner: ContextOwner, kind: ContextContributionKind, scope: ContextCacheScope, order: int) -> ContextSlotDescriptor:
    """构造测试使用的精确 Slot 描述符。"""
    return ContextSlotDescriptor(slot_id, owner, kind, scope, ContextRefreshPolicy.SIGNAL, order)


def _request() -> RunRequest:
    """构造含一条历史消息的冻结请求。"""
    history = ConversationMessage("history-1", MessageRole.ASSISTANT, "历史回答", "")
    user = ConversationMessage("user-1", MessageRole.USER, "当前问题", "")
    return RunRequest("session-1", "lease-1", "agent-1", user, ConversationSnapshot("session-1", (history,), 1))


def _execution(request: RunRequest, run_messages: tuple[RunMessage, ...] = ()) -> RunExecution:
    """构造带工具 Schema 的冻结执行态。"""
    policy = AgentPolicySnapshot(
        request.agent_id,
        "identity-v1",
        "model-v1",
        8,
        policy_data={
            "system_prompt": "身份提示词",
            "tools": [{"name": "lookup", "description": "查询", "parameters": {"type": "object"}}],
        },
    )
    return RunExecution("run-1", request, policy, AgentState(), RunBudget(8), run_messages=run_messages)


async def test_bound_slots_are_snapshotted_and_unenabled_slot_is_absent() -> None:
    """已绑定 Slot 全部写入 INCLUDED、EMPTY 或 FAILED；未启用的不出现。"""
    registry = ContextSlotRegistry()
    included = RecordingSlot(ContextContribution(ContextContributionKind.SYSTEM_CONTENT, ContextSlotStatus.INCLUDED, "自定义内容"))
    empty = RecordingSlot(ContextContribution(ContextContributionKind.SYSTEM_CONTENT, ContextSlotStatus.EMPTY))
    registry.register(_descriptor("custom", ContextOwner.AGENT, ContextContributionKind.SYSTEM_CONTENT, ContextCacheScope.AGENT, 10), lambda: included)
    registry.register(_descriptor("empty", ContextOwner.SESSION, ContextContributionKind.SYSTEM_CONTENT, ContextCacheScope.SESSION, 20), lambda: empty)
    registry.register(_descriptor("failed", ContextOwner.RUN, ContextContributionKind.SYSTEM_CONTENT, ContextCacheScope.RUN, 30), FailingSlot)
    registry.register(_descriptor("unenabled", ContextOwner.GLOBAL, ContextContributionKind.SYSTEM_CONTENT, ContextCacheScope.AGENT, 40), lambda: empty)
    provider = ContextProvider(ContextPlanResolver(registry), ContextSlotManager(registry, ContextSignalBus()), ("custom", "empty", "failed"), ContextDependencies())
    request = _request()

    bundle = await provider.build(request, _execution(request).view())

    assert [snapshot.slot_id for snapshot in bundle.metadata.slot_snapshots] == ["custom", "empty", "failed"]
    assert [snapshot.status for snapshot in bundle.metadata.slot_snapshots] == [ContextSlotStatus.INCLUDED, ContextSlotStatus.EMPTY, ContextSlotStatus.FAILED]
    assert "unenabled" not in bundle.metadata.source_names
    assert [message.content for message in bundle.messages] == ["自定义内容", "历史回答", "当前问题"]


async def test_run_messages_slot_only_snapshots_identifiers_and_tools_stay_structured() -> None:
    """RunMessagesSlot 不复制正文，实际工具 Schema 仅进入 ContextBundle.tools。"""
    request = _request()
    run_message = RunMessage("tool-result-1", 2, RunMessageKind.TOOL_RESULT, MessageRole.TOOL, "私有工具输出", tool_call_id="call-1")
    bundle = await build_context_provider(ContextDependencies()).build(request, _execution(request, (run_message,)).view())

    run_snapshot = next(snapshot for snapshot in bundle.metadata.slot_snapshots if snapshot.slot_id == "run_messages")
    tools_snapshot = next(snapshot for snapshot in bundle.metadata.slot_snapshots if snapshot.slot_id == "tools")
    assert run_snapshot.message_ids == ("tool-result-1",)
    assert run_snapshot.content == ""
    assert tools_snapshot.status is ContextSlotStatus.EMPTY
    assert bundle.tools[0].name == "lookup"
    assert all("lookup" not in message.content for message in bundle.messages if message.role is MessageRole.SYSTEM)
    assert bundle.messages[-1].content == "私有工具输出"


async def test_signal_and_direct_refresh_take_effect_at_next_build() -> None:
    """外部只能请求刷新或发布信号，具体 Slot 在下一个安全点刷新。"""
    registry = ContextSlotRegistry()
    slot = RecordingSlot(ContextContribution(ContextContributionKind.SYSTEM_CONTENT, ContextSlotStatus.INCLUDED, "内容"))
    registry.register(_descriptor("refreshable", ContextOwner.AGENT, ContextContributionKind.SYSTEM_CONTENT, ContextCacheScope.AGENT, 10), lambda: slot)
    signals = ContextSignalBus()
    manager = ContextSlotManager(registry, signals)
    provider = ContextProvider(ContextPlanResolver(registry), manager, ("refreshable",), ContextDependencies())
    request = _request()

    await provider.build(request, _execution(request).view())
    manager.request_refresh("refreshable")
    await provider.build(request, _execution(request).view())
    signals.publish(ContextRefreshSignal("refreshable", "配置更新"))
    await provider.build(request, _execution(request).view())
    signals.publish(ContextRefreshSignal("unbound", "无效刷新"))
    await provider.build(request, _execution(request).view())

    assert slot.loads == 4
    assert slot.refreshes == 2


async def test_release_scope_releases_cached_owner_instances() -> None:
    """Run、Session 与 Agent 生命周期均可释放对应缓存，不混用旧 Scope 语义。"""
    registry = ContextSlotRegistry()
    agent_slot = RecordingSlot(ContextContribution(ContextContributionKind.SYSTEM_CONTENT, ContextSlotStatus.EMPTY))
    session_slot = RecordingSlot(ContextContribution(ContextContributionKind.SYSTEM_CONTENT, ContextSlotStatus.EMPTY))
    run_slot = RecordingSlot(ContextContribution(ContextContributionKind.SYSTEM_CONTENT, ContextSlotStatus.EMPTY))
    registry.register(_descriptor("agent", ContextOwner.AGENT, ContextContributionKind.SYSTEM_CONTENT, ContextCacheScope.AGENT, 10), lambda: agent_slot)
    registry.register(_descriptor("session", ContextOwner.SESSION, ContextContributionKind.SYSTEM_CONTENT, ContextCacheScope.SESSION, 20), lambda: session_slot)
    registry.register(_descriptor("run", ContextOwner.RUN, ContextContributionKind.SYSTEM_CONTENT, ContextCacheScope.RUN, 30), lambda: run_slot)
    provider = ContextProvider(ContextPlanResolver(registry), ContextSlotManager(registry, ContextSignalBus()), ("agent", "session", "run"), ContextDependencies())
    request = _request()

    await provider.build(request, _execution(request).view())
    await provider.release_scope(ContextOwner.RUN, "run-1")
    await provider.release_scope(ContextOwner.SESSION, "session-1")
    await provider.release_scope(ContextOwner.AGENT, "agent-1")

    assert (agent_slot.releases, session_slot.releases, run_slot.releases) == (1, 1, 1)


async def test_cache_instance_isolated_by_exact_owner_key() -> None:
    """相同 Slot 类型在不同 Agent Owner 下必须创建独立实例。"""
    registry = ContextSlotRegistry()
    instances: list[RecordingSlot] = []

    def create_slot() -> RecordingSlot:
        """为每个缓存键创建可观测的独立实例。"""
        slot = RecordingSlot(ContextContribution(ContextContributionKind.SYSTEM_CONTENT, ContextSlotStatus.EMPTY))
        instances.append(slot)
        return slot

    registry.register(_descriptor("agent_cache", ContextOwner.AGENT, ContextContributionKind.SYSTEM_CONTENT, ContextCacheScope.AGENT, 10), create_slot)
    provider = ContextProvider(ContextPlanResolver(registry), ContextSlotManager(registry, ContextSignalBus()), ("agent_cache",), ContextDependencies())
    first_request = _request()
    second_request = replace(first_request, agent_id="agent-2")

    await provider.build(first_request, _execution(first_request).view())
    await provider.build(second_request, _execution(second_request).view())

    assert len(instances) == 2
