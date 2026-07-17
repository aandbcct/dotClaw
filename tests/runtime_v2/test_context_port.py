"""Runtime v2 ContextPort、token 预算与作用域缓存测试。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from dotclaw.context import (
    ContextDependencies,
    SlotCacheScope,
    SlotContext,
    SlotContextProvider,
)
from dotclaw.context.slots import ContextSlot, IdentitySlot, MemorySlot, ProjectSlot
from dotclaw.runtime.domain.execution import RunBudget, RunExecution
from dotclaw.runtime.domain.models import (
    AgentPolicySnapshot,
    ConversationMessage,
    ConversationSnapshot,
    MessageRole,
    RunRequest,
)
from dotclaw.runtime.domain.state import AgentState


@dataclass
class CountingSlot(ContextSlot):
    """记录各缓存作用域实际构造次数的测试 Slot。"""

    name: str
    scope: SlotCacheScope
    content: str
    call_count: int = 0

    async def produce(self, context: SlotContext) -> str | None:
        """每次未命中缓存时递增计数并返回固定内容。"""
        self.call_count += 1
        return self.content


class FailingMemoryManager:
    """用于验证 MemorySlot 失败只降级 Context 构造的替身。"""

    async def search(self, query: str) -> tuple[()]:
        """始终模拟记忆服务不可用。"""
        raise RuntimeError("记忆服务不可用")


def _request(session_id: str, content: str = "当前问题") -> RunRequest:
    """构造冻结的用户请求与历史会话。"""
    history: ConversationMessage = ConversationMessage(
        message_id="history-1",
        role=MessageRole.ASSISTANT,
        content="历史回答",
        created_at="2026-07-16T00:00:00+00:00",
    )
    user_message: ConversationMessage = ConversationMessage(
        message_id="user-1",
        role=MessageRole.USER,
        content=content,
        created_at="2026-07-16T00:00:01+00:00",
    )
    return RunRequest(
        session_id=session_id,
        lease_id=f"lease-{session_id}",
        agent_id="agent-1",
        user_message=user_message,
        conversation=ConversationSnapshot(session_id, (history,), 1),
    )


def _execution(request: RunRequest, run_id: str, identity_version: str = "identity-v1") -> RunExecution:
    """构造包含 Context 策略的只读执行视图来源。"""
    policy: AgentPolicySnapshot = AgentPolicySnapshot(
        agent_id=request.agent_id,
        identity_version=identity_version,
        model_id="model-v1",
        max_iterations=8,
        policy_data={
            "system_prompt": "你是测试助手。",
            "project_root": str(Path.cwd()),
            "max_context_tokens": 100,
        },
    )
    return RunExecution(
        run_id=run_id,
        request=request,
        policy=policy,
        state=AgentState(),
        budget=RunBudget(max_iterations=8),
    )


async def test_context_port_returns_full_messages_and_isolates_scope_cache() -> None:
    """不同 Agent、Session 与 Run 不会错误复用 Static、Session、Conditional 缓存。"""
    static_slot: CountingSlot = CountingSlot("static", SlotCacheScope.STATIC, "静态上下文")
    session_slot: CountingSlot = CountingSlot("session", SlotCacheScope.SESSION, "会话上下文")
    run_slot: CountingSlot = CountingSlot("run", SlotCacheScope.CONDITIONAL, "运行上下文")
    provider: SlotContextProvider = SlotContextProvider(
        slots=(static_slot, session_slot, run_slot),
        dependencies=ContextDependencies(),
    )
    first_request: RunRequest = _request("session-1")
    await provider.build(first_request, _execution(first_request, "run-1").view())
    await provider.build(first_request, _execution(first_request, "run-1").view())
    second_request: RunRequest = _request("session-2")
    bundle = await provider.build(second_request, _execution(second_request, "run-2").view())
    third_request: RunRequest = _request("session-2")
    await provider.build(third_request, _execution(third_request, "run-3", "identity-v2").view())

    assert static_slot.call_count == 2
    assert session_slot.call_count == 3
    assert run_slot.call_count == 3
    assert [message.role for message in bundle.messages] == [
        MessageRole.SYSTEM,
        MessageRole.ASSISTANT,
        MessageRole.USER,
    ]
    assert bundle.messages[0].content == "静态上下文\n\n会话上下文\n\n运行上下文"


async def test_memory_failure_degrades_current_context_only() -> None:
    """MemorySlot 抛出异常时仍保留其他 Slot 产物并记录失败来源。"""
    provider: SlotContextProvider = SlotContextProvider(
        slots=(IdentitySlot(), MemorySlot()),
        dependencies=ContextDependencies(memory_manager=FailingMemoryManager()),
    )
    request: RunRequest = _request("session-memory")
    bundle = await provider.build(request, _execution(request, "run-memory").view())

    assert bundle.messages[0].content == "你是测试助手。"
    assert bundle.metadata.details["failed_slots"] == ["memory"]


async def test_token_budget_truncates_oldest_history() -> None:
    """预算不足时保留 system 和当前用户输入，并裁剪最旧历史。"""
    request: RunRequest = _request("session-truncate", "当前输入")
    policy: AgentPolicySnapshot = AgentPolicySnapshot(
        agent_id=request.agent_id,
        identity_version="identity-v1",
        model_id="model-v1",
        max_iterations=8,
        policy_data={
            "system_prompt": "身份提示词",
            "project_root": str(Path.cwd()),
            "max_context_tokens": 3,
        },
    )
    execution: RunExecution = RunExecution(
        run_id="run-truncate",
        request=request,
        policy=policy,
        state=AgentState(),
        budget=RunBudget(max_iterations=8),
    )
    provider: SlotContextProvider = SlotContextProvider(
        slots=(IdentitySlot(),),
        dependencies=ContextDependencies(),
    )

    bundle = await provider.build(request, execution.view())

    assert bundle.metadata.truncation_applied
    assert [message.content for message in bundle.messages] == ["身份提示词", "当前输入"]


async def test_project_slot_has_read_limit_and_budget_failure_is_explicit(tmp_path: Path) -> None:
    """ProjectSlot 不会无上限读入文件，必要消息超预算时明确失败。"""
    project_file: Path = tmp_path / "AGENTS.md"
    project_file.write_text("项目规则" * 100, encoding="utf-8")
    request: RunRequest = _request("session-budget", "当前输入")
    execution: RunExecution = _execution(request, "run-budget")
    policy: AgentPolicySnapshot = AgentPolicySnapshot(
        agent_id=request.agent_id,
        identity_version="identity-v1",
        model_id="model-v1",
        max_iterations=8,
        policy_data={
            "system_prompt": "身份",
            "project_root": str(tmp_path),
            "max_context_tokens": 20,
        },
    )
    constrained_execution: RunExecution = RunExecution(
        run_id=execution.run_id,
        request=request,
        policy=policy,
        state=AgentState(),
        budget=RunBudget(max_iterations=8),
    )
    provider: SlotContextProvider = SlotContextProvider(
        slots=(IdentitySlot(), ProjectSlot()),
        dependencies=ContextDependencies(),
    )

    with pytest.raises(ValueError, match="必要 system prompt"):
        await provider.build(request, constrained_execution.view())
