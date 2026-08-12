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
from dotclaw.runtime.application.dto import ContextBundle, ContextMetadata, ContextRefreshSignal, ConversationMessage, ConversationSnapshot, LLMOutputEvent, LLMOutputKind, RunRequest, ToolInvocation, ToolResult, ToolResultStatus
from dotclaw.runtime.application.engine import RuntimeEngine
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.history_compaction import HistoryCompactionRequest, HistoryCompactionResult
from dotclaw.runtime.application.ports import ContextPort, LLMOutputPort, LLMPort, RunPolicyPort, ToolPort
from dotclaw.runtime.application.session_run_coordinator import SessionRunCoordinator
from dotclaw.runtime.domain.context import ContextOwner
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, AgentRun, MessageRole, RunMessage, RunMessageKind, ToolCall
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
    outcome_warmup: int = 1
    outcome_repeat: int = 1
    cancellation_warmup: int = 5
    cancellation_repeat: int = 50
    concurrent_warmup: int = 5
    concurrent_repeat: int = 50

    def to_dict(self) -> Mapping[str, object]:
        """返回可写入配置工件的稳定字典。"""
        return {"fixture_version": self.fixture_version, "fake_delay_ms": self.fake_delay_ms,
                "concurrent_parents": self.concurrent_parents, "outcome_warmup": self.outcome_warmup,
                "outcome_repeat": self.outcome_repeat, "cancellation_warmup": self.cancellation_warmup,
                "cancellation_repeat": self.cancellation_repeat, "concurrent_warmup": self.concurrent_warmup,
                "concurrent_repeat": self.concurrent_repeat}


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

    def __init__(self) -> None:
        self.run_ids: set[str] = set()
        self.request_by_run: dict[str, str] = {}

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        """构造一个稳定的 system 消息。"""
        self.run_ids.add(execution.run_id)
        self.request_by_run[execution.run_id] = request.user_message.content
        return ContextBundle((RunMessage("system", 1, RunMessageKind.LLM_REQUEST, MessageRole.SYSTEM, f"system:{request.user_message.content}"),), (), ContextMetadata(1))

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

    def __init__(self, request_id: str, delay_ms: int, outcome: ChildOutcome = ChildOutcome.COMPLETED, delegate_all_parents: bool = False) -> None:
        self._request_id = request_id
        self._delay_seconds = delay_ms / 1000.0
        self._delegated_parent_runs: set[str] = set()
        self._delegate_all_parents = delegate_all_parents
        self.outcome = outcome
        self.child_started = asyncio.Event()
        self.child_run_id = ""
        self.parent_run_id = ""
        self.child_release = asyncio.Event()

    async def complete(self, context: ContextBundle, execution: RunExecutionView, output_port: LLMOutputPort | None = None) -> RunMessage:
        """按身份输出父委派、子结果和父整合结果。"""
        await asyncio.sleep(self._delay_seconds)
        request_id = context.messages[0].content.removeprefix("system:")
        if execution.policy.agent_id == "parent-agent" and execution.run_id not in self._delegated_parent_runs and (self._delegate_all_parents or not self._delegated_parent_runs):
            self._delegated_parent_runs.add(execution.run_id)
            self.parent_run_id = execution.run_id
            return RunMessage("delegate", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "", tool_calls=(ToolCall("call-delegate", "delegate", {"target_agent_id": "target-agent", "title": "固定子任务", "objective": request_id}),))
        if execution.policy.agent_id == "target-agent":
            self.child_run_id = execution.run_id
            self.child_started.set()
            if self.outcome is ChildOutcome.FAILED:
                raise RuntimeError("受控子 Run 失败")
            if self.outcome in (ChildOutcome.CANCELLED, ChildOutcome.ABANDONED):
                await self.child_release.wait()
        content = f"child:{request_id}" if execution.policy.agent_id == "target-agent" else f"parent:{request_id}"
        if output_port is not None:
            await output_port.emit(LLMOutputEvent(execution.session_id, execution.run_id, LLMOutputKind.RESPONSE_DELTA, content))
        return RunMessage("answer", 1, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, content)

    async def cancel(self, run_id: str) -> None:
        """固定替身没有远程取消资源。"""
        self.child_release.set()


@dataclass(frozen=True)
class DelegatedTaskFixture:
    """Harness 确定性委派的一条冻结子任务。"""

    target_agent_id: str
    title: str
    objective: str
    output: str


class _HarnessDelegatingLLM(LLMPort):
    """只负责驱动生产委派状态机的确定性 Harness 替身。"""

    def __init__(self, tasks: tuple[DelegatedTaskFixture, ...], delay_ms: int) -> None:
        self._tasks = tasks
        self._delay_seconds = delay_ms / 1000.0
        self._next_task_by_parent: dict[str, int] = {}
        self._output_by_target = {task.target_agent_id: task.output for task in tasks}

    async def complete(self, context: ContextBundle, execution: RunExecutionView, output_port: LLMOutputPort | None = None) -> RunMessage:
        """父 Run 顺序提交全部冻结子任务，子 Run 返回对应冻结结果。"""
        await asyncio.sleep(self._delay_seconds)
        if execution.policy.agent_id == "parent-agent":
            index = self._next_task_by_parent.get(execution.run_id, 0)
            if index < len(self._tasks):
                task = self._tasks[index]
                self._next_task_by_parent[execution.run_id] = index + 1
                return RunMessage(
                    f"delegate-{index}",
                    1,
                    RunMessageKind.LLM_RESPONSE,
                    MessageRole.ASSISTANT,
                    "",
                    tool_calls=(ToolCall(
                        f"call-delegate-{index}",
                        "delegate",
                        {
                            "target_agent_id": task.target_agent_id,
                            "title": task.title,
                            "objective": task.objective,
                        },
                    ),),
                )
            content = "harness delegation completed"
        else:
            content = self._output_by_target[execution.policy.agent_id]
        if output_port is not None:
            await output_port.emit(LLMOutputEvent(execution.session_id, execution.run_id, LLMOutputKind.RESPONSE_DELTA, content))
        return RunMessage("answer", 1, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, content)

    async def cancel(self, run_id: str) -> None:
        """冻结完成态委派不持有远程资源。"""


class _NoTools(ToolPort):
    """无工具端口（delegate 必须经过 DelegationPort）。"""

    def __init__(self) -> None:
        self.run_ids: list[str] = []
        self.contents_by_run: dict[str, list[str]] = {}

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """任何普通工具调用都视为 Fixture 配置错误。"""
        self.run_ids.append(execution.run_id)
        return ToolResult(invocation.call.call_id, ToolResultStatus.FAILED)

    async def cancel(self, run_id: str) -> None:
        """没有可取消的工具调用。"""


class _RecordingOutput(LLMOutputPort):
    """记录输出端口实际收到的 Run 归属。"""

    def __init__(self) -> None:
        self.run_ids: list[str] = []
        self.contents_by_run: dict[str, list[str]] = {}

    async def emit(self, event: LLMOutputEvent) -> None:
        """保存真实输出事件所属 Run。"""
        self.run_ids.append(event.run_id)
        self.contents_by_run.setdefault(event.run_id, []).append(event.content)


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
    context, tools, output = _FixedContext(), _NoTools(), _RecordingOutput()
    engine = RuntimeEngine(repository, CheckpointRepositoryAdapter(root), context, llm, tools, _FixedPolicy(), ApprovalService(ApprovalRepositoryAdapter(root)), CancellationService(), delegation_port=adapter, token_counter=_AlwaysWithinBudgetCounter(), history_compactor=_UnexpectedHistoryCompactor())
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
        submitted = await coordinator.submit(request, output)
    suspended_at = time.perf_counter()
    child_run_id = submitted.child_run_id
    if child_run_id is None:
        raise RuntimeError("委派 Fixture 未返回子 Run 标识")
    await adapter.result(child_run_id)
    await _wait_for_terminal(repository, child_run_id)
    resumed = await coordinator.resume_delegation(child_run_id)
    ended = time.perf_counter()
    parent_events = await repository.load_events(request.session_id, submitted.run_id)
    parent_messages = await repository.load_messages(request.session_id, submitted.run_id)
    parent_contexts = await repository.load_context_versions(request.session_id, submitted.run_id)
    task = await dispatcher.broker.latest_task_for_source(request.session_id)
    child_run = await _wait_for_terminal(repository, child_run_id)
    child_messages = () if child_run is None else await repository.load_messages(child_run.session_id, child_run_id)
    allowed_run_ids = {submitted.run_id, child_run_id}
    message_run_ids = [submitted.run_id for _ in parent_messages] + [child_run_id for _ in child_messages]
    foreign_message = sum(run_id not in allowed_run_ids for run_id in message_run_ids)
    foreign_context = sum(run_id not in allowed_run_ids for run_id in context.run_ids)
    foreign_tool = sum(run_id not in allowed_run_ids for run_id in tools.run_ids)
    foreign_stream = sum(request_id not in content for run_id, contents in output.contents_by_run.items() if run_id in allowed_run_ids for content in contents)
    misdelivery = foreign_message + foreign_context + foreign_tool + foreign_stream
    return {"parent_run_id": submitted.run_id, "child_run_id": child_run_id, "task_id": None if task is None else task.task_id,
            "parent_session_id": request.session_id, "child_session_id": None if child_run is None else child_run.session_id,
            "target_agent_id": "target-agent", "parent_outcome": resumed.state.outcome().value,
            "child_outcome": "completed" if child_run is None else child_run.state.outcome().value,
            "delegation_submit_count": sum(event.event_type.value == "delegation_submitted" for event in parent_events),
            "result_backfill_count": sum(message.kind is RunMessageKind.DELEGATION_RESULT for message in parent_messages),
            "delegation_submitted_event_count": sum(event.event_type.value == "delegation_submitted" for event in parent_events),
            "delegation_completed_event_count": sum(event.event_type.value == "delegation_completed" for event in parent_events),
            "context_version_count": len(parent_contexts),
            "tool_fact_count": sum(message.kind.value.startswith("tool_") for message in parent_messages),
            "stream_fact_count": sum(message.kind is RunMessageKind.LLM_RESPONSE for message in parent_messages),
            "cross_chain_message_count": foreign_message, "cross_chain_context_count": foreign_context,
            "cross_chain_tool_count": foreign_tool, "cross_chain_stream_count": foreign_stream,
            "misdelivery_count": misdelivery,
            "observed_context_run_ids": tuple(context.run_ids), "observed_tool_run_ids": tuple(tools.run_ids), "observed_stream_run_ids": tuple(output.run_ids),
            "message_contents": tuple(message.content for message in (*parent_messages, *child_messages)),
            "suspend_to_backfill_ms": (ended - suspended_at) * 1000.0, "parent_end_to_end_ms": (ended - started) * 1000.0}


async def run_completed_chain(root: Path, config: DelegationWorkloadConfig, request_id: str) -> Mapping[str, object]:
    """完成态快捷入口（保留给单链路和并发工作负载）。"""
    return await run_child_outcome(root, config, request_id, ChildOutcome.COMPLETED)


async def run_harness_delegations(
    root: Path,
    config: DelegationWorkloadConfig,
    request_id: str,
    tasks: tuple[DelegatedTaskFixture, ...],
) -> Mapping[str, object]:
    """由 Harness 驱动单个父 Run 完成全部子任务并验证结果回灌。"""
    if not tasks:
        raise ValueError("Harness 委派任务不能为空")
    if len({task.target_agent_id for task in tasks}) != len(tasks):
        raise ValueError("Harness 冻结子任务必须使用唯一目标 Agent，避免结果映射歧义")
    repository = RunRepositoryAdapter(root)
    registry = AgentRegistry()
    for target_agent_id in dict.fromkeys(task.target_agent_id for task in tasks):
        registry.register(AgentIdentity(agent_id=target_agent_id, agent_name=f"Benchmark {target_agent_id}", model="fixture-model"))
    dispatcher = AgentDispatcher(TaskMessageBroker())
    adapter = RuntimeDelegationAdapter(SessionManager(root), registry, dispatcher)
    context, tools, output = _FixedContext(), _NoTools(), _RecordingOutput()
    engine = RuntimeEngine(
        repository,
        CheckpointRepositoryAdapter(root),
        context,
        _HarnessDelegatingLLM(tasks, config.fake_delay_ms),
        tools,
        _FixedPolicy(),
        ApprovalService(ApprovalRepositoryAdapter(root)),
        CancellationService(),
        delegation_port=adapter,
        token_counter=_AlwaysWithinBudgetCounter(),
        history_compactor=_UnexpectedHistoryCompactor(),
    )
    coordinator = SessionRunCoordinator(engine)
    adapter.bind_coordinator(coordinator)
    session_id = f"parent-{request_id}"
    request = RunRequest(
        session_id,
        f"lease-{request_id}",
        "parent-agent",
        ConversationMessage("input", MessageRole.USER, request_id, ""),
        ConversationSnapshot(session_id, (), 0),
    )
    started = time.perf_counter()
    current = await coordinator.submit(request, output)
    parent_run_id = current.run_id
    child_run_ids: list[str] = []
    child_outcomes: list[str] = []
    while current.child_run_id is not None:
        child_run_id = current.child_run_id
        child_run_ids.append(child_run_id)
        await adapter.result(child_run_id)
        child_run = await _wait_for_terminal(repository, child_run_id)
        child_outcomes.append(child_run.state.outcome().value)
        current = await coordinator.resume_delegation(child_run_id)
    ended = time.perf_counter()
    parent_events = await repository.load_events(session_id, parent_run_id)
    parent_messages = await repository.load_messages(session_id, parent_run_id)
    result_messages = [message for message in parent_messages if message.kind is RunMessageKind.DELEGATION_RESULT]
    result_contents = tuple(message.content for message in result_messages)
    expected_contents = tuple(task.output for task in tasks)
    result_mismatch_count = abs(len(result_contents) - len(expected_contents)) + sum(
        actual != expected
        for actual, expected in zip(result_contents, expected_contents)
    )
    submitted_count = sum(event.event_type.value == "delegation_submitted" for event in parent_events)
    completed_count = sum(event.event_type.value == "delegation_completed" for event in parent_events)
    return {
        "parent_run_id": parent_run_id,
        "child_run_ids": tuple(child_run_ids),
        "child_outcomes": tuple(child_outcomes),
        "target_agent_ids": tuple(task.target_agent_id for task in tasks),
        "delegation_submit_count": submitted_count,
        "delegation_completed_event_count": completed_count,
        "result_backfill_count": len(result_messages),
        "result_contents": result_contents,
        "parent_outcome": current.state.outcome().value,
        "parent_consumed_results": result_mismatch_count == 0,
        "misdelivery_count": result_mismatch_count,
        "parent_end_to_end_ms": (ended - started) * 1000.0,
    }


async def run_concurrent_completed(root: Path, config: DelegationWorkloadConfig, attempt: int) -> tuple[Mapping[str, object], ...]:
    """在一套共享存储、Broker、Adapter 与协调器内并发执行多个父 Session。"""
    repository = RunRepositoryAdapter(root)
    registry = AgentRegistry()
    registry.register(AgentIdentity(agent_id="target-agent", agent_name="Benchmark Target", model="fixture-model"))
    dispatcher = AgentDispatcher(TaskMessageBroker())
    adapter = RuntimeDelegationAdapter(SessionManager(root), registry, dispatcher)
    context, tools, output = _FixedContext(), _NoTools(), _RecordingOutput()
    engine = RuntimeEngine(repository, CheckpointRepositoryAdapter(root), context, _DelegatingLLM("shared", config.fake_delay_ms, delegate_all_parents=True), tools, _FixedPolicy(), ApprovalService(ApprovalRepositoryAdapter(root)), CancellationService(), delegation_port=adapter, token_counter=_AlwaysWithinBudgetCounter(), history_compactor=_UnexpectedHistoryCompactor())
    coordinator = SessionRunCoordinator(engine)
    adapter.bind_coordinator(coordinator)
    request_ids = [chain_request_id(index, attempt) for index in range(config.concurrent_parents)]
    requests = [RunRequest(f"parent-{request_id}", f"lease-{request_id}", "parent-agent", ConversationMessage("input", MessageRole.USER, request_id, ""), ConversationSnapshot(f"parent-{request_id}", (), 0)) for request_id in request_ids]
    started = time.perf_counter()
    submitted = await asyncio.gather(*(coordinator.submit(request, output) for request in requests))
    suspended_at = time.perf_counter()
    # 先并发等待适配器确认子执行任务已结束，再读取持久化事实。Repository 的
    # 恢复扫描会遍历共享根；不能在子 Run 仍写入时并发触发该只读观察。
    await asyncio.gather(*(adapter.result(result.child_run_id or "") for result in submitted))
    child_runs: list[AgentRun] = []
    for result in submitted:
        child_runs.append(await _wait_for_terminal(repository, result.child_run_id or ""))
    resumed = await asyncio.gather(*(coordinator.resume_delegation(result.child_run_id or "") for result in submitted))
    ended = time.perf_counter()
    facts: list[Mapping[str, object]] = []
    all_parent_ids = {result.run_id for result in submitted}
    all_child_ids = {result.child_run_id or "" for result in submitted}
    for request, request_id, submit_result, resume_result, child_run in zip(requests, request_ids, submitted, resumed, child_runs, strict=True):
        child_run_id = submit_result.child_run_id or ""
        parent_messages = await repository.load_messages(request.session_id, submit_result.run_id)
        child_messages = await repository.load_messages(child_run.session_id, child_run_id)
        parent_events = await repository.load_events(request.session_id, submit_result.run_id)
        task = await dispatcher.broker.latest_task_for_source(request.session_id)
        allowed = {submit_result.run_id, child_run_id}
        foreign_message = sum(any(other in message.content for other in request_ids if other != request_id) for message in (*parent_messages, *child_messages))
        foreign_context = sum(any(other in content for other in request_ids if other != request_id) for run_id, content in context.request_by_run.items() if run_id in allowed)
        foreign_tool = sum(run_id not in allowed for run_id in tools.run_ids)
        foreign_stream = sum(request_id not in content for run_id, contents in output.contents_by_run.items() if run_id in allowed for content in contents)
        duplicate = (len(all_parent_ids) != len(submitted)) or (len(all_child_ids) != len(submitted))
        facts.append({"parent_run_id": submit_result.run_id, "child_run_id": child_run_id, "task_id": None if task is None else task.task_id, "parent_session_id": request.session_id, "child_session_id": child_run.session_id, "target_agent_id": "target-agent", "parent_outcome": resume_result.state.outcome().value, "child_outcome": child_run.state.outcome().value, "delegation_submit_count": sum(event.event_type.value == "delegation_submitted" for event in parent_events), "result_backfill_count": sum(message.kind is RunMessageKind.DELEGATION_RESULT for message in parent_messages), "delegation_submitted_event_count": sum(event.event_type.value == "delegation_submitted" for event in parent_events), "delegation_completed_event_count": sum(event.event_type.value == "delegation_completed" for event in parent_events), "cross_chain_message_count": foreign_message, "cross_chain_context_count": foreign_context, "cross_chain_tool_count": foreign_tool, "cross_chain_stream_count": foreign_stream, "misdelivery_count": int(duplicate) + foreign_message + foreign_context + foreign_tool + foreign_stream, "suspend_to_backfill_ms": (ended - suspended_at) * 1000.0, "parent_end_to_end_ms": (ended - started) * 1000.0})
    return tuple(facts)


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
    parent_effect_at, parent_run = await _wait_for_cancelled(repository, llm.parent_run_id)
    child_effect_at, child_run = await _wait_for_cancelled(repository, llm.child_run_id)
    followup_started_at = time.perf_counter()
    followup = await asyncio.wait_for(coordinator.submit(RunRequest(session_id, f"followup-{request_id}", "parent-agent", ConversationMessage("followup", MessageRole.USER, "follow-up", ""), ConversationSnapshot(session_id, (), 0))), timeout=1.0)
    followup_ended_at = time.perf_counter()
    return {"parent_run_id": llm.parent_run_id, "child_run_id": llm.child_run_id,
            "parent_cancelled": parent_run is not None and parent_run.state.outcome().value == "cancelled",
            "child_cancelled": child_run is not None and child_run.state.outcome().value == "cancelled",
            "cancel_delivery_ms": (delivered_at - cancelled_at) * 1000.0,
            "parent_cancel_effect_ms": (parent_effect_at - cancelled_at) * 1000.0,
            "child_cancel_effect_ms": (child_effect_at - cancelled_at) * 1000.0,
            "followup_started": True, "followup_completed": followup.state.outcome().value == "completed",
            "followup_duration_ms": (followup_ended_at - followup_started_at) * 1000.0,
            "submitted_waiting": submitted.state.is_waiting_delegation()}


async def _wait_for_cancelled(repository: RunRepositoryAdapter, run_id: str) -> tuple[float, AgentRun]:
    """轮询持久化 Run 直到其真实进入取消终态，并返回独立观测时间。"""
    deadline = time.perf_counter() + 3.0
    while time.perf_counter() < deadline:
        run = await repository.find_run(run_id)
        if run is not None and run.state.outcome() is not None and run.state.outcome().value == "cancelled":
            return time.perf_counter(), run
        await asyncio.sleep(0.001)
    raise TimeoutError(f"Run {run_id} 未在 1 秒内进入取消终态")


async def _wait_for_terminal(repository: RunRepositoryAdapter, run_id: str) -> AgentRun:
    """在读取链路事实前等待子 Run 持久化为任意终态，避免并发竞态。"""
    deadline = time.perf_counter() + 3.0
    while time.perf_counter() < deadline:
        run = await repository.find_run(run_id)
        if run is not None and run.state.outcome() is not None:
            return run
        await asyncio.sleep(0.001)
    raise TimeoutError(f"Run {run_id} 未在 3 秒内收口")
