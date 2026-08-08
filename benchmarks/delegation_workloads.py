"""PR7 固定委派工作负载定义；每轮配置独立临时存储根。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from dotclaw.agent.identity import AgentIdentity
from dotclaw.orchestration.dispatcher import AgentDispatcher
from dotclaw.orchestration.message_broker import TaskMessageBroker
from dotclaw.orchestration.registry import AgentRegistry
from dotclaw.orchestration.runtime_delegation_adapter import RuntimeDelegationAdapter
from dotclaw.runtime.adapters import ApprovalRepositoryAdapter, CheckpointRepositoryAdapter, RunRepositoryAdapter
from dotclaw.runtime.application.approval_service import ApprovalService
from dotclaw.runtime.application.cancellation_service import CancellationService
from dotclaw.runtime.application.context_budget import TokenCountRequest, TokenCountResult
from dotclaw.runtime.application.dto import ContextBundle, ContextMetadata, ContextRefreshSignal, ConversationMessage, ConversationSnapshot, RunRequest, ToolInvocation, ToolResult, ToolResultStatus
from dotclaw.runtime.application.engine import RuntimeEngine
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.history_compaction import HistoryCompactionRequest, HistoryCompactionResult
from dotclaw.runtime.application.ports import ContextPort, LLMOutputPort, LLMPort, RunPolicyPort, ToolPort
from dotclaw.runtime.application.session_run_coordinator import SessionRunCoordinator
from dotclaw.runtime.domain.context import ContextOwner
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, MessageRole, RunMessage, RunMessageKind, ToolCall
from dotclaw.session.session import SessionManager


class ChildOutcome(StrEnum):
    """受控子 Run 终态（完成、失败、取消、放弃）。"""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class DelegationWorkloadConfig:
    """一次 PR7 运行的固定 Fixture 配置（不含生产 Runtime 状态）。"""

    fixture_version: str = "delegation-fixture-v1"
    fake_delay_ms: int = 10
    concurrent_parents: int = 8

    def to_dict(self) -> Mapping[str, object]:
        """返回可写入配置工件的稳定字典。"""
        return {"fixture_version": self.fixture_version, "fake_delay_ms": self.fake_delay_ms,
                "concurrent_parents": self.concurrent_parents}


def chain_request_id(parent_index: int, attempt: int) -> str:
    """生成不含业务正文的固定链路标识。"""
    return f"parent-{parent_index}-attempt-{attempt}"


class _FixedPolicy(RunPolicyPort):
    """固定策略（隔离 Fixture 的最小可执行策略）。"""

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        """为父子 Agent 返回同口径的确定性策略。"""
        return AgentPolicySnapshot(request.agent_id, "delegation-fixture-policy", "fixture-model", 5,
                                   policy_data={"context_window": 128, "tokenizer_encoding": "cl100k_base"})


class _FixedContext(ContextPort):
    """固定上下文（不缓存、不读业务 Slot）。"""

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        """构造一个稳定的 system 消息。"""
        return ContextBundle((RunMessage("system", 1, RunMessageKind.LLM_REQUEST, MessageRole.SYSTEM, "system"),), (), ContextMetadata(1))

    async def release_scope(self, owner: ContextOwner, owner_key: str) -> None:
        """Fixture 没有缓存资源。"""

    async def release_all(self) -> None:
        """Fixture 没有缓存资源。"""

    def request_refresh(self, slot_id: str, owner: ContextOwner, owner_key: str) -> None:
        """Fixture 不支持 Slot 刷新。"""

    def publish_signal(self, signal: ContextRefreshSignal) -> None:
        """Fixture 不消费刷新信号。"""


class _DelegatingLLM(LLMPort):
    """委派模型替身（父 Run 发起一次委派，子 Run 与回灌后父 Run 均返回固定文本）。"""

    def __init__(self, request_id: str, delay_ms: int, outcome: ChildOutcome = ChildOutcome.COMPLETED) -> None:
        self._request_id = request_id
        self._delay_seconds = delay_ms / 1000.0
        self._parent_calls = 0
        self.outcome = outcome
        self.child_started = asyncio.Event()
        self.child_run_id = ""
        self.parent_run_id = ""
        self.child_release = asyncio.Event()

    async def complete(self, context: ContextBundle, execution: RunExecutionView, output_port: LLMOutputPort | None = None) -> RunMessage:
        """按身份输出父委派、子结果和父整合结果。"""
        await asyncio.sleep(self._delay_seconds)
        if execution.policy.agent_id == "parent-agent" and self._parent_calls == 0:
            self._parent_calls += 1
            self.parent_run_id = execution.run_id
            return RunMessage("delegate", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "", tool_calls=(ToolCall("call-delegate", "delegate", {"target_agent_id": "target-agent", "title": "固定子任务", "objective": self._request_id}),))
        if execution.policy.agent_id == "target-agent":
            self.child_run_id = execution.run_id
            self.child_started.set()
            if self.outcome is ChildOutcome.FAILED:
                raise RuntimeError("受控子 Run 失败")
            if self.outcome in (ChildOutcome.CANCELLED, ChildOutcome.ABANDONED):
                await self.child_release.wait()
        content = f"child:{self._request_id}" if execution.policy.agent_id == "target-agent" else f"parent:{self._request_id}"
        return RunMessage("answer", 1, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, content)

    async def cancel(self, run_id: str) -> None:
        """固定替身没有远程取消资源。"""
        self.child_release.set()


class _NoTools(ToolPort):
    """无工具端口（delegate 必须经过 DelegationPort）。"""

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """任何普通工具调用都视为 Fixture 配置错误。"""
        return ToolResult(invocation.call.call_id, ToolResultStatus.FAILED)

    async def cancel(self, run_id: str) -> None:
        """没有可取消的工具调用。"""


class _AlwaysWithinBudgetCounter:
    """固定计数器（保证本 Benchmark 不触发上下文预算分支）。"""

    async def count(self, request: TokenCountRequest) -> TokenCountResult:
        """返回最小输入 token 数。"""
        return TokenCountResult(input_tokens=1)


class _UnexpectedHistoryCompactor:
    """历史压缩替身（若被调用则说明 Fixture 超出范围）。"""

    async def compact_history(self, request: HistoryCompactionRequest) -> HistoryCompactionResult:
        """拒绝不应发生的历史压缩。"""
        raise AssertionError("委派固定 Fixture 不应触发历史压缩")


async def run_child_outcome(root: Path, config: DelegationWorkloadConfig, request_id: str, outcome: ChildOutcome) -> Mapping[str, object]:
    """执行真实委派链，并通过子 Run 终态驱动父侧回灌。"""
    repository = RunRepositoryAdapter(root)
    registry = AgentRegistry()
    registry.register(AgentIdentity(agent_id="target-agent", agent_name="Benchmark Target", model="fixture-model"))
    dispatcher = AgentDispatcher(TaskMessageBroker())
    adapter = RuntimeDelegationAdapter(SessionManager(root), registry, dispatcher)
    llm = _DelegatingLLM(request_id, config.fake_delay_ms, outcome)
    engine = RuntimeEngine(repository, CheckpointRepositoryAdapter(root), _FixedContext(), llm, _NoTools(), _FixedPolicy(), ApprovalService(ApprovalRepositoryAdapter(root)), CancellationService(), delegation_port=adapter, token_counter=_AlwaysWithinBudgetCounter(), history_compactor=_UnexpectedHistoryCompactor())
    coordinator = SessionRunCoordinator(engine)
    adapter.bind_coordinator(coordinator)
    request = RunRequest(f"parent-{request_id}", f"lease-{request_id}", "parent-agent", ConversationMessage("input", MessageRole.USER, request_id, ""), ConversationSnapshot(f"parent-{request_id}", (), 0))
    started = time.perf_counter()
    if outcome in (ChildOutcome.CANCELLED, ChildOutcome.ABANDONED):
        submission_task = asyncio.create_task(coordinator.submit(request))
        await asyncio.wait_for(llm.child_started.wait(), timeout=1.0)
        if outcome is ChildOutcome.CANCELLED:
            await coordinator.cancel(llm.child_run_id, "Benchmark 子 Run 取消")
        else:
            # 子 Run 正在持有其 Session 协调锁；放弃入口直接走 Engine，避免测试编排
            # 自身等待同一把锁，同时仍复用既有状态机与持久化语义。
            await engine.abandon_run(llm.child_run_id)
            llm.child_release.set()
        submitted = await asyncio.wait_for(submission_task, timeout=1.0)
    else:
        submitted = await coordinator.submit(request)
    suspended_at = time.perf_counter()
    child_run_id = submitted.child_run_id
    if child_run_id is None:
        raise RuntimeError("委派 Fixture 未返回子 Run 标识")
    resumed = await coordinator.resume_delegation(child_run_id)
    ended = time.perf_counter()
    parent_events = await repository.load_events(request.session_id, submitted.run_id)
    parent_messages = await repository.load_messages(request.session_id, submitted.run_id)
    task = await dispatcher.broker.latest_task_for_source(request.session_id)
    child_run = await repository.find_run(child_run_id)
    return {"parent_run_id": submitted.run_id, "child_run_id": child_run_id, "task_id": None if task is None else task.task_id,
            "parent_session_id": request.session_id, "child_session_id": None if child_run is None else child_run.session_id,
            "target_agent_id": "target-agent", "parent_outcome": resumed.state.outcome().value,
            "child_outcome": "completed" if child_run is None else child_run.state.outcome().value,
            "delegation_submit_count": sum(event.event_type.value == "delegation_submitted" for event in parent_events),
            "result_backfill_count": sum(message.kind is RunMessageKind.DELEGATION_RESULT for message in parent_messages),
            "delegation_submitted_event_count": sum(event.event_type.value == "delegation_submitted" for event in parent_events),
            "delegation_completed_event_count": sum(event.event_type.value == "delegation_completed" for event in parent_events),
            "suspend_to_backfill_ms": (ended - suspended_at) * 1000.0, "parent_end_to_end_ms": (ended - started) * 1000.0}


async def run_completed_chain(root: Path, config: DelegationWorkloadConfig, request_id: str) -> Mapping[str, object]:
    """完成态快捷入口（保留给单链路和并发工作负载）。"""
    return await run_child_outcome(root, config, request_id, ChildOutcome.COMPLETED)


async def run_concurrent_completed(root: Path, config: DelegationWorkloadConfig, attempt: int) -> tuple[Mapping[str, object], ...]:
    """并发执行多个独立父 Session，返回每条链路的持久化归属事实。"""
    tasks = [run_completed_chain(root / f"parent-{index}", config, chain_request_id(index, attempt)) for index in range(config.concurrent_parents)]
    return tuple(await asyncio.gather(*tasks))


async def run_parent_cancellation(root: Path, config: DelegationWorkloadConfig, request_id: str) -> Mapping[str, object]:
    """取消已委派父 Run，验证子取消、执行权释放与同 Session 后续请求。"""
    repository = RunRepositoryAdapter(root)
    registry = AgentRegistry()
    registry.register(AgentIdentity(agent_id="target-agent", agent_name="Benchmark Target", model="fixture-model"))
    dispatcher = AgentDispatcher(TaskMessageBroker())
    adapter = RuntimeDelegationAdapter(SessionManager(root), registry, dispatcher)
    llm = _DelegatingLLM(request_id, config.fake_delay_ms, ChildOutcome.CANCELLED)
    engine = RuntimeEngine(repository, CheckpointRepositoryAdapter(root), _FixedContext(), llm, _NoTools(), _FixedPolicy(), ApprovalService(ApprovalRepositoryAdapter(root)), CancellationService(), delegation_port=adapter, token_counter=_AlwaysWithinBudgetCounter(), history_compactor=_UnexpectedHistoryCompactor())
    coordinator = SessionRunCoordinator(engine)
    adapter.bind_coordinator(coordinator)
    session_id = f"parent-{request_id}"
    request = RunRequest(session_id, f"lease-{request_id}", "parent-agent", ConversationMessage("input", MessageRole.USER, request_id, ""), ConversationSnapshot(session_id, (), 0))
    parent_task = asyncio.create_task(coordinator.submit(request))
    await asyncio.wait_for(llm.child_started.wait(), timeout=1.0)
    cancelled_at = time.perf_counter()
    await coordinator.cancel(llm.parent_run_id, "Benchmark 父 Run 主动取消")
    delivered_at = time.perf_counter()
    submitted = await asyncio.wait_for(parent_task, timeout=1.0)
    parent_run = await repository.load_run(session_id, llm.parent_run_id)
    child_run = await repository.find_run(llm.child_run_id)
    followup_started_at = time.perf_counter()
    followup = await asyncio.wait_for(coordinator.submit(RunRequest(session_id, f"followup-{request_id}", "parent-agent", ConversationMessage("followup", MessageRole.USER, "follow-up", ""), ConversationSnapshot(session_id, (), 0))), timeout=1.0)
    followup_ended_at = time.perf_counter()
    return {"parent_run_id": llm.parent_run_id, "child_run_id": llm.child_run_id,
            "parent_cancelled": parent_run is not None and parent_run.state.outcome().value == "cancelled",
            "child_cancelled": child_run is not None and child_run.state.outcome().value == "cancelled",
            "cancel_delivery_ms": (delivered_at - cancelled_at) * 1000.0,
            "parent_cancel_effect_ms": (followup_started_at - cancelled_at) * 1000.0,
            "child_cancel_effect_ms": (followup_started_at - cancelled_at) * 1000.0,
            "followup_started": True, "followup_completed": followup.state.outcome().value == "completed",
            "followup_duration_ms": (followup_ended_at - followup_started_at) * 1000.0,
            "submitted_waiting": submitted.state.is_waiting_delegation()}
