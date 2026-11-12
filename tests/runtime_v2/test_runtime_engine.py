"""RuntimeEngine 的正常执行、审批恢复与取消验收测试。"""

from __future__ import annotations
from dotclaw.runtime.application.run_change_notifier import RunChangeNotifier

from dotclaw.runtime.domain.control import AgentAction
from dotclaw.runtime.domain.state import AgentRunState, Created, Running, Suspended, Ended, RunStage, SuspendReason, RunOutcome
from dotclaw.runtime.domain.events import RunEvent, RunEventType, ToolAuditStatus, ToolBatchFailed, is_terminal_tool_event

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from dotclaw.runtime.adapters import ApprovalRepositoryAdapter, CheckpointRepositoryAdapter, RunRepositoryAdapter
from dotclaw.runtime.application.approval_service import ApprovalService
from dotclaw.runtime.application.cancellation_service import CancellationService
from dotclaw.runtime.application.engine import RuntimeEngine, _scan_legacy_tool_activity
from tests.runtime_v2.tool_recovery_testkit import file_tool_recovery, ToolPortTestBase
from dotclaw.common.tool_replay import ToolExecutionEvidence
from dotclaw.runtime.application.ports import ContextPort, LLMPort, LLMOutputPort, RunPolicyPort, ToolPort
from dotclaw.runtime.application.session_run_coordinator import SessionRunCoordinator
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.dto import (
    ContextBundle, ContextMetadata, ConversationMessage, ConversationSnapshot,
    ContextRefreshSignal, LLMOutputEvent, LLMOutputKind, RunAcceptedEvent, RunRequest, RunResult, ToolInvocation, ToolResult, ToolResultStatus,
)
from dotclaw.runtime.domain.facts import (
    AgentPolicySnapshot, AgentRun, JSONMap, JSONValue, MessageRole, RunCheckpoint, RunError, RunErrorCode, RunMessage, RunMessageKind, ToolCall,
    require_json_map,
)
from dotclaw.context import (
    ContextCacheScope,
    ContextContribution,
    ContextDependencies,
    ContextOwnerPlanConfiguration,
    ContextPlanResolver,
    ContextProvider,
    ContextRefreshPolicy,
    ContextRefreshSignal,
    ContextSignalBus,
    ContextSlotBinding,
    ContextSlotDescriptor,
    ContextSlotManager,
    ContextSlotRegistry,
    HistoryCompressionsSlot,
    InMemoryContextPlanConfiguration,
    RunMessagesSlot,
    build_context_provider,
)
from dotclaw.runtime.domain.context import ContextContributionKind, ContextOwner, ContextPersistenceMode, ContextSlotStatus, ContextVersion, TextSlotContent
from dotclaw.runtime.domain.tool_recovery import ToolCallState, ToolEffectCertainty
from dotclaw.runtime.domain.state import transition
from tests.runtime_v2.context_budget_fakes import AlwaysWithinBudgetCounter, UnexpectedHistoryCompactor


def test_legacy_approval_completion_is_waiting_not_terminal() -> None:
    """旧审批假终态继续表示等待，只有明确执行终态才关闭调用。"""
    approval = RunEvent("run", 1, RunEventType.TOOL_COMPLETED, "now", data={
        "call_id": "call", "status": ToolAuditStatus.APPROVAL_REQUIRED.value,
    })
    completion = RunEvent("run", 2, RunEventType.TOOL_COMPLETED, "now", data={
        "call_id": "call", "status": ToolAuditStatus.COMPLETED.value,
    })

    assert not is_terminal_tool_event(approval)
    activity = _scan_legacy_tool_activity((approval,))
    assert "call" in activity.waiting
    assert "call" not in activity.terminal_call_ids
    assert is_terminal_tool_event(completion)


def test_tool_batch_failure_from_tool_stage_delegation_suspension_is_terminal() -> None:
    """旧委派恢复中的工具不确定结果可经状态机收口并保留 iteration。"""
    suspended = AgentRunState(
        Suspended(SuspendReason.DELEGATION, "child", RunStage.EXECUTING_TOOLS), iteration=3,
    )

    result = transition(suspended, ToolBatchFailed(RunError(RunErrorCode.TOOL_OUTCOME_UNCERTAIN, "结果待确认")))

    assert result.state.outcome() is RunOutcome.FAILED
    assert result.state.iteration == 3
    assert result.action is AgentAction.FINALIZE


class PolicyPort(RunPolicyPort):
    """返回固定冻结策略的测试 Port。"""

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        """构造最小策略快照。"""
        return AgentPolicySnapshot(
            request.agent_id,
            "identity-v1",
            "model-v1",
            8,
            policy_data={"context_window": 128, "tokenizer_encoding": "cl100k_base"},
        )


class ContextFake(ContextPort):
    """返回一条固定 system 消息的上下文替身。"""

    def __init__(self) -> None:
        self.released_scopes: list[tuple[ContextOwner, str]] = []

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        """构造完整模型请求的最小上下文。"""
        message = RunMessage("context", 1, RunMessageKind.LLM_REQUEST, MessageRole.SYSTEM, "系统提示")
        return ContextBundle((message,), (), ContextMetadata(estimated_tokens=1))

    async def release_scope(self, owner: ContextOwner, owner_key: str) -> None:
        """测试替身不缓存 Slot 实例。"""
        self.released_scopes.append((owner, owner_key))

    async def release_all(self) -> None:
        """测试替身不缓存 Slot 实例。"""

    def request_refresh(self, slot_id: str, owner: ContextOwner, owner_key: str) -> None:
        """测试替身不维护独立 Slot 刷新状态。"""

    def publish_signal(self, signal: ContextRefreshSignal) -> None:
        """测试替身不消费外部刷新信号。"""


class FinalLLM(LLMPort):
    """立即返回普通最终回答的 LLM 替身。"""

    async def complete(self, context: ContextBundle, execution: RunExecutionView, output_port: LLMOutputPort | None = None) -> RunMessage:
        """返回不含工具调用的最终回复。"""
        return RunMessage("answer", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "请补充信息")



class ToolFake(ToolPortTestBase):
    """正常文本路径无需调用的工具替身。"""

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """返回失败以防测试意外走入工具路径。"""
        return ToolResult(invocation.call.call_id, ToolResultStatus.FAILED, error=None)



class BlockedLLM(FinalLLM):
    """手动释放首个输出，供引擎及真实 HTTP 测试观察确认时序。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_observed = asyncio.Event()
        self.cleanup_completed = asyncio.Event()

    async def complete(self, context: ContextBundle, execution: RunExecutionView, output_port: LLMOutputPort | None = None) -> RunMessage:
        self.started.set()
        try:
            await self.release.wait()
            if output_port is not None:
                await output_port.emit(LLMOutputEvent(execution.session_id, execution.run_id, LLMOutputKind.RESPONSE_DELTA, "请补充信息"))
            return await super().complete(context, execution, output_port)
        except asyncio.CancelledError:
            self.cancel_observed.set()
            raise
        finally:
            self.cleanup_completed.set()



class AcceptedOutput:
    """将确认和模型输出分别收集，以事件而非固定等待验证先后。"""

    def __init__(self) -> None:
        self.accepted: list[RunAcceptedEvent] = []
        self.deltas: list[LLMOutputEvent] = []
        self.notified = asyncio.Event()

    async def on_run_accepted(self, event: RunAcceptedEvent) -> None:
        self.accepted.append(event)
        self.notified.set()

    async def emit(self, event: LLMOutputEvent) -> None:
        self.deltas.append(event)


def _request(session_id: str) -> RunRequest:
    """构造一条普通用户请求。"""
    user = ConversationMessage("user-1", MessageRole.USER, "我需要帮助", "2026-07-17T00:00:00+00:00")
    return RunRequest(session_id, f"lease-{session_id}", "agent-1", user, ConversationSnapshot(session_id, (), 0))


def _engine(root: Path, *, run_change_notifier: RunChangeNotifier | None = None) -> RuntimeEngine:
    """使用真实文件仓储和 fake 外部端口构造 Engine。"""
    run_repository: RunRepositoryAdapter = RunRepositoryAdapter(root, approval_repository=ApprovalRepositoryAdapter(root))
    approval_repository: ApprovalRepositoryAdapter = ApprovalRepositoryAdapter(root)
    return RuntimeEngine(
        run_repository,
        CheckpointRepositoryAdapter(root),
        ContextFake(),
        FinalLLM(),
        ToolFake(),
        PolicyPort(),
        ApprovalService(approval_repository),
        CancellationService(),
        token_counter=AlwaysWithinBudgetCounter(),
        tool_recovery=file_tool_recovery(root),
        history_compactor=UnexpectedHistoryCompactor(),
        run_change_notifier=run_change_notifier,
    )


def test_engine_rejects_missing_context_budget_ports(tmp_path: Path) -> None:
    """Engine 未装配精确计数和摘要 Port 时必须在启动前明确失败。"""
    with pytest.raises(ValueError, match="TokenCounterPort 和 HistoryCompactorPort"):
        RuntimeEngine(
            RunRepositoryAdapter(tmp_path, approval_repository=ApprovalRepositoryAdapter(tmp_path)),
            CheckpointRepositoryAdapter(tmp_path),
            ContextFake(),
            FinalLLM(),
            ToolFake(),
            PolicyPort(),
            ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
            CancellationService(),
            tool_recovery=file_tool_recovery(tmp_path),
        )


async def test_engine_completes_clarification_as_normal_conversation(tmp_path: Path) -> None:
    """澄清回复是完成态，成功投影 Conversation 且不锁住 Session。"""
    engine = _engine(tmp_path)
    result = await engine.execute(_request("session-1"))

    assert result.state.outcome().value == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "请补充信息"
    run_repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path, approval_repository=ApprovalRepositoryAdapter(tmp_path))
    conversation = await run_repository.load_conversation("session-1")
    assert conversation[0].content == "请补充信息"


@pytest.mark.parametrize("tool_name", ["lookup", "delegate"])
async def test_legacy_unmatched_ordinary_and_delegation_outcomes_fail_uncertain_on_engine_resume(
    tmp_path: Path, tool_name: str,
) -> None:
    """旧批次结果证据不完整时，普通和委派调用都由 Engine 收口为真实 uncertain 失败。"""
    engine = _engine(tmp_path)
    run = AgentRun(
        run_id=f"legacy-{tool_name}", session_id="legacy-session", agent_id="agent-1",
        state=AgentRunState(Running(RunStage.EXECUTING_TOOLS)), started_at="2026-11-12T00:00:00+00:00",
        policy=AgentPolicySnapshot("agent-1", "identity-v1", "model-v1", 8), input_message_id="input",
    )
    call = ToolCall(
        "legacy-call", tool_name,
        {"target_agent_id": "agent-child", "title": "旧任务", "objective": "核验旧结果"} if tool_name == "delegate" else {"query": "x"},
    )
    input_message = RunMessage("input", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "恢复旧运行")
    source = RunMessage("assistant", 2, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "", tool_calls=(call,))
    repository = engine._run_repository
    await repository.create_run(run)
    await repository.save_messages(run.session_id, run.run_id, (input_message, source))
    await repository.append_context_version(run.session_id, run.run_id, ContextVersion(
        1, "2026-11-12T00:00:00+00:00", (), "content-hash", "schema-hash",
    ))
    await repository.set_active_context_version(run.session_id, run.run_id, 1)
    started = RunEvent(run.run_id, 1, RunEventType.TOOL_STARTED, "2026-11-12T00:00:01+00:00", (source.message_id,), data={
        "call_id": call.call_id, "tool_name": call.name, "source_response_message_id": source.message_id,
    })
    if tool_name == "delegate":
        legacy_event = RunEvent(run.run_id, 2, RunEventType.DELEGATION_REQUESTED, "2026-11-12T00:00:02+00:00", (source.message_id,), data={
            "tool_call_id": call.call_id, "target_agent_id": "agent-child",
        })
    else:
        legacy_event = RunEvent(run.run_id, 2, RunEventType.TOOL_COMPLETED, "2026-11-12T00:00:02+00:00", data={
            "call_id": call.call_id, "tool_name": call.name, "status": "completed", "result_message_id": "missing-result",
        })
    await repository.append_event(run.session_id, started)
    await repository.append_event(run.session_id, legacy_event)
    old_checkpoint = RunCheckpoint(
        f"checkpoint-{run.run_id}", run.run_id, run.session_id, 1, 2, 2,
        {"tool_calls": [call.to_dict()]}, {}, AgentAction.EXECUTE_TOOLS,
        active_context_version=1,
    )
    await engine._checkpoint_repository.save(old_checkpoint)

    result = await engine.resume_run(run.run_id)

    assert result.state.outcome() is RunOutcome.FAILED
    assert result.error is not None and result.error.code is RunErrorCode.TOOL_OUTCOME_UNCERTAIN
    assert await engine._checkpoint_repository.load(run.session_id, run.run_id) is None
    failed = await repository.load_run(run.session_id, run.run_id)
    assert failed is not None and failed.error is not None
    assert failed.error.code is RunErrorCode.TOOL_OUTCOME_UNCERTAIN
    ledger = await repository.load_tool_ledger(run.session_id, run.run_id)
    assert ledger is not None and ledger.calls[0].state is ToolCallState.UNCERTAIN
    await engine._tool_recovery.block_uncertain(failed, call.call_id, ledger.calls[0].uncertainty_reason or "legacy_execution_unproven", old_checkpoint)


async def test_acceptance_is_persisted_before_blocked_model_output(tmp_path: Path) -> None:
    """模型未释放时确认已送达，且确认 ID 已可在仓储读取。"""
    engine, output, model = _engine(tmp_path), AcceptedOutput(), BlockedLLM()
    engine._llm_port = model
    task = asyncio.create_task(engine.execute(_request("accepted-session"), output))
    try:
        await asyncio.wait_for(output.notified.wait(), 2)
        await asyncio.wait_for(model.started.wait(), 2)
        assert not task.done() and not model.release.is_set() and output.deltas == []
        accepted = output.accepted[0]
        run = await RunRepositoryAdapter(tmp_path, approval_repository=ApprovalRepositoryAdapter(tmp_path)).load_run(accepted.session_id, accepted.run_id)
        assert run is not None and run.run_id == accepted.run_id
        model.release.set()
        result = await asyncio.wait_for(task, 2)
        assert result.run_id == accepted.run_id and len(output.accepted) == 1
    finally:
        model.release.set()
        await task


async def test_create_failure_does_not_send_acceptance(tmp_path: Path) -> None:
    engine, output = _engine(tmp_path), AcceptedOutput()
    engine._run_repository.create_run = AsyncMock(side_effect=OSError("隔离存储失败"))
    with pytest.raises(OSError):
        await engine.execute(_request("create-failure"), output)
    assert output.accepted == []


async def test_acceptance_notification_failure_does_not_abandon_run(tmp_path: Path) -> None:
    engine, output = _engine(tmp_path), AcceptedOutput()
    output.on_run_accepted = AsyncMock(side_effect=RuntimeError("PRIVATE_BODY"))
    result = await engine.execute(_request("notify-failure"), output)
    assert result.state.outcome() is RunOutcome.COMPLETED
    assert len(await RunRepositoryAdapter(tmp_path, approval_repository=ApprovalRepositoryAdapter(tmp_path)).load_conversation("notify-failure")) == 1


async def test_acceptance_callback_cancellation_propagates_and_unregisters_token(tmp_path: Path) -> None:
    engine, output = _engine(tmp_path), AcceptedOutput()
    output.on_run_accepted = AsyncMock(side_effect=asyncio.CancelledError())
    request = _request("notify-cancel")
    with pytest.raises(asyncio.CancelledError):
        await engine.execute(request, output)
    run = (await engine._run_repository.list_active_runs("notify-cancel"))[0]
    assert run.run_id not in engine._cancellation_service._drivers
    assert run.run_id not in engine._cancellation_service._tokens


async def test_approval_resume_keeps_id_without_second_acceptance(tmp_path: Path) -> None:
    engine, output = _engine(tmp_path), AcceptedOutput()
    engine._llm_port, engine._tool_port = ApprovalLLM(), ApprovalTool()
    waiting = await engine.execute(_request("approval-confirm"), output)
    resumed = await engine.resolve_approval("approval-1", True, output)
    assert resumed.run_id == waiting.run_id and resumed.state.outcome() is RunOutcome.COMPLETED
    assert [event.run_id for event in output.accepted] == [waiting.run_id]


async def test_approval_same_decision_reentry_returns_terminal_and_opposite_conflicts(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._llm_port, engine._tool_port = ApprovalLLM(), ApprovalTool()
    waiting = await engine.execute(_request("approval-repeat"))
    completed = await engine.resolve_approval("approval-1", True)

    repeated = await engine.resolve_approval("approval-1", True)

    assert repeated.run_id == waiting.run_id
    assert repeated.state == completed.state
    assert await engine.get_approval_session_id("approval-1") == "approval-repeat"
    with pytest.raises(ValueError, match="相反|不可翻转"):
        await engine.resolve_approval("approval-1", False)


async def test_pending_success_result_still_checks_opposite_approval_decision(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._llm_port, engine._tool_port = ApprovalLLM(), ApprovalTool()
    waiting = await engine.execute(_request("approval-pending-success-conflict"))
    run = await engine._run_repository.load_run(waiting.session_id, waiting.run_id)
    checkpoint = await engine._checkpoint_repository.load(waiting.session_id, waiting.run_id)
    assert run is not None and checkpoint is not None
    async with engine._cancellation_service.gate(run.run_id):
        await engine._tool_recovery.decide_approval(run, "approval-1", True, checkpoint)
    engine._pending_success_result_locked = AsyncMock(return_value=RunResult(
        waiting.run_id,
        AgentRunState(mode=Ended(RunOutcome.FAILED)),
        error=RunError(RunErrorCode.PERSISTENCE_FAILURE, "结果仍待提交"),
    ))

    with pytest.raises(ValueError, match="不可翻转|相反"):
        await engine.resolve_approval("approval-1", False)


async def test_resume_completes_refusal_decision_left_before_cancel_request(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._llm_port, tool = ApprovalLLM(), ApprovalTool()
    engine._tool_port = tool
    waiting = await engine.execute(_request("approval-reject-recover"))
    run = await engine._run_repository.load_run(waiting.session_id, waiting.run_id)
    checkpoint = await engine._checkpoint_repository.load(waiting.session_id, waiting.run_id)
    assert run is not None and checkpoint is not None
    async with engine._cancellation_service.gate(run.run_id):
        await engine._tool_recovery.decide_approval(run, "approval-1", False, checkpoint)

    assert await engine._run_repository.load_cancellation(run.session_id, run.run_id) is None
    result = await engine.resume_run(run.run_id)

    assert result.state.outcome() is RunOutcome.CANCELLED
    assert tool._call_count == 0


async def test_accepted_cancellation_prevents_approved_tool_start(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._llm_port, tool = ApprovalLLM(), ApprovalTool()
    engine._tool_port = tool
    waiting = await engine.execute(_request("approval-cancel-priority"))
    await engine.request_cancel(waiting.run_id, "先受理取消")

    result = await engine.resolve_approval("approval-1", True)

    assert result.state.outcome() is RunOutcome.CANCELLED
    assert tool._call_count == 0


async def test_engine_releases_run_scope_only_after_terminal_result(tmp_path: Path) -> None:
    """Run 完成后 Engine 必须通过 ContextPort 释放对应 Run Owner 缓存。"""
    context_port = ContextFake()
    engine = RuntimeEngine(
        RunRepositoryAdapter(tmp_path, approval_repository=ApprovalRepositoryAdapter(tmp_path)),
        CheckpointRepositoryAdapter(tmp_path),
        context_port,
        FinalLLM(),
        ToolFake(),
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
        token_counter=AlwaysWithinBudgetCounter(),
        tool_recovery=file_tool_recovery(tmp_path),
        history_compactor=UnexpectedHistoryCompactor(),
    )

    result = await engine.execute(_request("session-release"))

    assert context_port.released_scopes == [(ContextOwner.RUN, result.run_id)]


async def test_engine_runs_different_sessions_concurrently(tmp_path: Path) -> None:
    """不同 Session 的 run 写入独立目录，不互相覆盖消息与事件。"""
    engine = _engine(tmp_path)
    first, second = await asyncio.gather(
        engine.execute(_request("session-a")),
        engine.execute(_request("session-b")),
    )

    assert first.run_id != second.run_id
    assert (tmp_path / "session-a" / "agent_runs" / first.run_id / "messages.json").is_file()
    assert (tmp_path / "session-b" / "agent_runs" / second.run_id / "events.jsonl").is_file()


class ApprovalLLM(LLMPort):
    """先请求工具、审批恢复后返回最终回答的 LLM 替身。"""

    def __init__(self) -> None:
        self._call_count: int = 0

    async def complete(self, context: ContextBundle, execution: RunExecutionView, output_port: LLMOutputPort | None = None) -> RunMessage:
        """第一次返回工具调用，第二次返回普通完成消息。"""
        self._call_count += 1
        if self._call_count == 1:
            return RunMessage("tool-request", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "", tool_calls=(ToolCall("call-1", "dangerous", {}),))
        return RunMessage("answer-after-approval", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "审批后完成")



class ApprovalTool(ToolPortTestBase):
    """首次要求审批、恢复后完成同一工具调用的替身。"""

    def __init__(self) -> None:
        self._call_count: int = 0

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """按调用次数模拟审批与批准后的真实执行。"""
        self._call_count += 1
        if self._call_count == 1:
            return ToolResult(invocation.call.call_id, ToolResultStatus.APPROVAL_REQUIRED, approval_id="approval-1")
        return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED, output="已执行",
            execution_evidence=ToolExecutionEvidence.RETURNED)

    async def preflight(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        return ToolResult(invocation.call.call_id, ToolResultStatus.APPROVAL_REQUIRED, approval_id="approval-1",
            execution_evidence=ToolExecutionEvidence.NOT_STARTED)



class ChangingSystemSlot:
    """每次生成不同文本，用于验证恢复路径必须重放首次冻结 Slot。"""

    def __init__(self) -> None:
        self.calls: int = 0

    async def load(self, binding: ContextSlotBinding) -> ContextContribution:
        """返回带调用序号的 system 内容。"""
        self.calls += 1
        return ContextContribution(ContextContributionKind.SYSTEM_CONTENT, ContextSlotStatus.INCLUDED, TextSlotContent(f"system-{self.calls}"))

    async def refresh(self, binding: ContextSlotBinding) -> None:
        """测试 Slot 不保存缓存。"""

    def should_refresh(self, binding: ContextSlotBinding, signal: ContextRefreshSignal) -> bool:
        """测试不发布事件，始终拒绝刷新。"""
        return False

    async def release(self) -> None:
        """测试 Slot 不持有资源。"""


def _changing_context_port(slot: ChangingSystemSlot) -> ContextProvider:
    """通过 Registry 装配可验证冻结语义的自定义 Slot。"""
    registry = ContextSlotRegistry()
    descriptor = ContextSlotDescriptor(
        "changing",
        ContextOwner.AGENT,
        ContextContributionKind.SYSTEM_CONTENT,
        ContextPersistenceMode.SNAPSHOT,
        ContextCacheScope.AGENT,
        ContextRefreshPolicy.SIGNAL,
        10,
    )
    registry.register(descriptor, lambda: slot)
    registry.register(ContextSlotDescriptor(
        "history_compressions",
        ContextOwner.SESSION,
        ContextContributionKind.HISTORY_COMPRESSIONS,
        ContextPersistenceMode.SNAPSHOT,
        ContextCacheScope.SESSION,
        ContextRefreshPolicy.SIGNAL,
        20,
    ), HistoryCompressionsSlot)
    registry.register(ContextSlotDescriptor(
        "run_messages",
        ContextOwner.RUN,
        ContextContributionKind.RUN_MESSAGE_REFERENCES,
        ContextPersistenceMode.FACT_REFERENCE,
        ContextCacheScope.RUN,
        ContextRefreshPolicy.SIGNAL,
        30,
    ), RunMessagesSlot)
    configuration: InMemoryContextPlanConfiguration = InMemoryContextPlanConfiguration((
        ContextOwnerPlanConfiguration(ContextOwner.AGENT, ("changing",)),
        ContextOwnerPlanConfiguration(ContextOwner.SESSION, ("history_compressions",)),
        ContextOwnerPlanConfiguration(ContextOwner.RUN, ("run_messages",)),
    ))
    return ContextProvider(
        ContextPlanResolver(registry, configuration),
        ContextSlotManager(registry, ContextSignalBus()),
        ContextDependencies(),
    )


async def test_approval_resume_reuses_run_id_and_keeps_event_sequence(tmp_path: Path) -> None:
    """审批恢复继续原 run，完整记录审批决议和恢复事件。"""
    run_repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path, approval_repository=ApprovalRepositoryAdapter(tmp_path))
    engine = RuntimeEngine(
        run_repository,
        CheckpointRepositoryAdapter(tmp_path),
        ContextFake(),
        ApprovalLLM(),
        ApprovalTool(),
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
        token_counter=AlwaysWithinBudgetCounter(),
        tool_recovery=file_tool_recovery(tmp_path),
        history_compactor=UnexpectedHistoryCompactor(),
    )
    waiting = await engine.execute(_request("session-approval"))
    completed = await engine.resolve_approval("approval-1", approved=True)

    assert waiting.state.is_waiting_approval()
    assert completed.state.outcome().value == "completed"
    assert completed.run_id == waiting.run_id
    events_path: Path = tmp_path / "session-approval" / "agent_runs" / waiting.run_id / "events.jsonl"
    event_types: list[str] = [
        json.loads(line)["event_type"]
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert event_types.index("approval_resolved") < event_types.index("run_resumed")
    assert event_types.index("run_resumed") < event_types.index("run_completed")


async def test_approval_events_carry_structured_fields(tmp_path: Path) -> None:
    """等待审批与审批解决事件分别携带 approval_id/call_id 与 approved（7.4）。"""
    run_repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path, approval_repository=ApprovalRepositoryAdapter(tmp_path))
    engine = RuntimeEngine(
        run_repository,
        CheckpointRepositoryAdapter(tmp_path),
        ContextFake(),
        ApprovalLLM(),
        ApprovalTool(),
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
        token_counter=AlwaysWithinBudgetCounter(),
        tool_recovery=file_tool_recovery(tmp_path),
        history_compactor=UnexpectedHistoryCompactor(),
    )
    waiting = await engine.execute(_request("session-fields"))
    completed = await engine.resolve_approval("approval-1", approved=True)

    assert waiting.state.is_waiting_approval()
    assert completed.state.outcome().value == "completed"
    events = await run_repository.load_events("session-fields", waiting.run_id)
    waiting_event = next(event for event in events if event.event_type is RunEventType.WAITING_APPROVAL)
    resolved_event = next(event for event in events if event.event_type is RunEventType.APPROVAL_RESOLVED)
    assert waiting_event.data.get("approval_id") == "approval-1"
    assert waiting_event.data.get("call_id") == "call-1"
    assert waiting_event.data.get("tool_event_semantics") == 2
    assert waiting_event.message_ids == ("tool-request",)
    messages = await run_repository.load_messages("session-fields", waiting.run_id)
    assert not any(message.kind is RunMessageKind.TOOL_RESULT for message in messages)
    assert resolved_event.data.get("approval_id") == "approval-1"
    assert resolved_event.data.get("approved") is True
    assert resolved_event.data.get("tool_event_semantics") == 2


async def test_rejected_approval_event_marks_approved_false(tmp_path: Path) -> None:
    """审批拒绝时 APPROVAL_RESOLVED 事件携带 approved=false（7.4）。"""
    run_repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path, approval_repository=ApprovalRepositoryAdapter(tmp_path))
    engine = RuntimeEngine(
        run_repository,
        CheckpointRepositoryAdapter(tmp_path),
        ContextFake(),
        ApprovalLLM(),
        ApprovalTool(),
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
        token_counter=AlwaysWithinBudgetCounter(),
        tool_recovery=file_tool_recovery(tmp_path),
        history_compactor=UnexpectedHistoryCompactor(),
    )
    await engine.execute(_request("session-reject"))
    result = await engine.resolve_approval("approval-1", approved=False)

    assert result.state.outcome().value == "cancelled"
    events = await run_repository.load_events("session-reject", result.run_id)
    resolved_event = next(event for event in events if event.event_type is RunEventType.APPROVAL_RESOLVED)
    assert resolved_event.data.get("approval_id") == "approval-1"
    assert resolved_event.data.get("approved") is False


class ReActLLM(LLMPort):
    """记录两轮模型上下文，以验证工具证据能够进入下一轮 ReAct 请求。"""

    def __init__(self) -> None:
        """初始化模型调用记录。"""
        self.contexts: list[ContextBundle] = []

    async def complete(self, context: ContextBundle, execution: RunExecutionView, output_port: LLMOutputPort | None = None) -> RunMessage:
        """首轮请求两个工具，次轮在断言上下文完整后返回最终回答。"""
        self.contexts.append(context)
        if len(self.contexts) == 1:
            return RunMessage(
                "tool-requests",
                1,
                RunMessageKind.LLM_RESPONSE,
                MessageRole.ASSISTANT,
                "我将查询两个来源。",
                tool_calls=(
                    ToolCall("call-1", "lookup", {"source": "first"}),
                    ToolCall("call-2", "lookup", {"source": "second"}),
                ),
            )
        return RunMessage("answer", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "两个来源均已查询。")



class CompletedTool(ToolPortTestBase):
    """记录所有工具调用并返回对应的确定性结果。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.calls: list[ToolInvocation] = []

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """保存调用顺序并返回调用标识对应的结果。"""
        self.calls.append(invocation)
        return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED, output=f"结果-{invocation.call.call_id}",
            execution_evidence=ToolExecutionEvidence.RETURNED)


class PreflightFailedTool(ToolPortTestBase):
    """在处理函数启动前返回确定失败的工具替身。"""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        self.execute_calls = 0

    async def preflight(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        return ToolResult(
            invocation.call.call_id, ToolResultStatus.FAILED, output="预检拒绝",
            execution_evidence=ToolExecutionEvidence.NOT_STARTED, tool_error_code=self.error_code,
        )

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        self.execute_calls += 1
        return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED, output="不应执行",
            execution_evidence=ToolExecutionEvidence.RETURNED)


class NotStartedExecutionFailureTool(ToolPortTestBase):
    """处理函数明确报告尚未开始副作用的失败结果。"""

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        return ToolResult(
            invocation.call.call_id, ToolResultStatus.FAILED, output="执行前拒绝",
            error=RunError(RunErrorCode.TOOL_FAILURE, "执行前拒绝"),
            execution_evidence=ToolExecutionEvidence.NOT_STARTED,
        )


@pytest.mark.parametrize("error_code", ["INVALID_ARGUMENTS", "POLICY_DENIED"])
async def test_preflight_not_started_failure_is_committed_without_handler(error_code: str, tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._llm_port = ApprovalLLM()
    tool = PreflightFailedTool(error_code)
    engine._tool_port = tool

    result = await engine.execute(_request(f"preflight-{error_code}"))

    assert result.state.outcome() is RunOutcome.FAILED
    assert result.error is not None and result.error.code is RunErrorCode.TOOL_FAILURE
    run = await engine._run_repository.find_run(result.run_id)
    ledger = await engine._run_repository.load_tool_ledger("preflight-" + error_code, result.run_id)
    assert run is not None and run.error is not None and run.error.code is RunErrorCode.TOOL_FAILURE
    assert ledger is not None and ledger.calls[0].state is ToolCallState.FAILED
    assert ledger.calls[0].result is not None and ledger.calls[0].result.tool_error_code == error_code
    assert tool.execute_calls == 0


async def test_execute_failed_not_started_is_committed_as_reliable_failure(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._llm_port = ApprovalLLM()
    engine._tool_port = NotStartedExecutionFailureTool()

    result = await engine.execute(_request("execute-not-started-failure"))

    assert result.state.outcome() is RunOutcome.FAILED
    assert result.error is not None and result.error.code is RunErrorCode.TOOL_FAILURE
    ledger = await engine._run_repository.load_tool_ledger("execute-not-started-failure", result.run_id)
    assert ledger is not None and ledger.calls[0].state is ToolCallState.FAILED
    assert ledger.calls[0].result is not None
    assert ledger.calls[0].result.effect_certainty.value == "known"



def _request_with_history(session_id: str) -> RunRequest:
    """构造含既有 Conversation 的请求，用于审批恢复上下文回放测试。"""
    history: ConversationMessage = ConversationMessage(
        "history-1",
        MessageRole.ASSISTANT,
        "这是之前的回答。",
        "2026-07-16T00:00:00+00:00",
    )
    user: ConversationMessage = ConversationMessage(
        "user-1",
        MessageRole.USER,
        "请查询资料。",
        "2026-07-17T00:00:00+00:00",
    )
    return RunRequest(
        session_id,
        f"lease-{session_id}",
        "agent-1",
        user,
        ConversationSnapshot(session_id, (history,), 1),
    )


async def test_react_context_contains_all_tool_calls_and_results_in_next_llm_round(tmp_path: Path) -> None:
    """首轮的 assistant 工具调用及全部 tool result 必须进入第二轮模型上下文。"""
    llm: ReActLLM = ReActLLM()
    tool_port: CompletedTool = CompletedTool()
    engine: RuntimeEngine = RuntimeEngine(
        RunRepositoryAdapter(tmp_path, approval_repository=ApprovalRepositoryAdapter(tmp_path)),
        CheckpointRepositoryAdapter(tmp_path),
        build_context_provider(ContextDependencies()),
        llm,
        tool_port,
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
        token_counter=AlwaysWithinBudgetCounter(),
        tool_recovery=file_tool_recovery(tmp_path),
        history_compactor=UnexpectedHistoryCompactor(),
    )

    result: RunResult = await engine.execute(_request_with_history("session-react"))

    assert result.state.outcome() is RunOutcome.COMPLETED
    assert [invocation.call.call_id for invocation in tool_port.calls] == ["call-1", "call-2"]
    second_context: ContextBundle = llm.contexts[1]
    assert [message.content for message in second_context.messages] == [
        "这是之前的回答。",
        "请查询资料。",
        "我将查询两个来源。",
        "结果-call-1",
        "结果-call-2",
    ]
    assert second_context.messages[2].tool_calls == (
        ToolCall("call-1", "lookup", {"source": "first"}),
        ToolCall("call-2", "lookup", {"source": "second"}),
    )
    assert [message.tool_call_id for message in second_context.messages[3:]] == ["call-1", "call-2"]


async def test_engine_appends_context_versions_and_audits_llm_calls_without_request_message_copies(tmp_path: Path) -> None:
    """动态 Run Message 不改变快照版本，事件仍精确引用全部动态输入。"""
    llm: ReActLLM = ReActLLM()
    engine: RuntimeEngine = RuntimeEngine(
        RunRepositoryAdapter(tmp_path, approval_repository=ApprovalRepositoryAdapter(tmp_path)),
        CheckpointRepositoryAdapter(tmp_path),
        build_context_provider(ContextDependencies()),
        llm,
        CompletedTool(),
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
        token_counter=AlwaysWithinBudgetCounter(),
        tool_recovery=file_tool_recovery(tmp_path),
        history_compactor=UnexpectedHistoryCompactor(),
    )

    result: RunResult = await engine.execute(_request_with_history("session-context-version"))

    messages_path: Path = tmp_path / "session-context-version" / "agent_runs" / result.run_id / "messages.json"
    events_path: Path = tmp_path / "session-context-version" / "agent_runs" / result.run_id / "events.jsonl"
    payload: JSONMap = require_json_map(json.loads(messages_path.read_text(encoding="utf-8")))
    raw_versions: JSONValue | None = payload.get("context_versions")
    assert isinstance(raw_versions, list)
    assert [require_json_map(item)["version"] for item in raw_versions] == [1]
    first_version: JSONMap = require_json_map(raw_versions[0])
    raw_slots: JSONValue | None = first_version.get("slots")
    assert isinstance(raw_slots, list)
    history_slot: JSONMap = next(
        require_json_map(item) for item in raw_slots if require_json_map(item)["slot_id"] == "conversation"
    )
    raw_history_messages: JSONValue | None = history_slot.get("content")
    assert isinstance(raw_history_messages, list)
    assert require_json_map(raw_history_messages[0])["content"] == "这是之前的回答。"
    raw_stored_messages: JSONValue | None = payload.get("messages")
    assert isinstance(raw_stored_messages, list)
    stored_messages: list[JSONValue] = raw_stored_messages
    assert [message["kind"] for message in stored_messages if isinstance(message, dict)] == [
        "user_input",
        "llm_response",
        "tool_result",
        "tool_result",
        "final_response",
    ]
    assert all(message["kind"] != "llm_request" for message in stored_messages if isinstance(message, dict))

    events: list[JSONMap] = [
        require_json_map(json.loads(line))
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    llm_started_events: list[JSONMap] = [
        event for event in events if event["event_type"] == "llm_started"
    ]
    assert len(llm_started_events) == 2
    raw_first_data: JSONValue | None = llm_started_events[0].get("data")
    raw_second_data: JSONValue | None = llm_started_events[1].get("data")
    assert isinstance(raw_first_data, dict)
    assert isinstance(raw_second_data, dict)
    first_data: JSONMap = raw_first_data
    second_data: JSONMap = raw_second_data
    assert first_data["context_version"] == 1
    assert second_data["context_version"] == 1
    assert first_data["incremental_message_ids"] == ["user-1"]
    assert second_data["incremental_message_ids"] == [
        "user-1",
        f"response-{result.run_id}-2",
        f"tool-{result.run_id}-3",
        f"tool-{result.run_id}-4",
    ]
    tool_events: list[JSONMap] = [event for event in events if event["event_type"] in {"tool_started", "tool_completed"}]
    # 每个真实开始的工具单独写开始事件和恢复检查点，后续未执行工具不能伪造开始事实。
    assert [event["event_type"] for event in tool_events] == ["tool_started", "tool_completed", "tool_started", "tool_completed"]
    assert [require_json_map(event["data"])["call_id"] for event in tool_events] == ["call-1", "call-1", "call-2", "call-2"]
    assert all(event["event_type"] != "context_built" for event in events)


async def test_approval_resume_rebuilds_conversation_and_react_context(tmp_path: Path) -> None:
    """审批批准后应保留首轮 Conversation、assistant 工具调用和工具执行结果。"""
    llm: ReActLLM = ReActLLM()
    approval_tool: ApprovalTool = ApprovalTool()
    engine: RuntimeEngine = RuntimeEngine(
        RunRepositoryAdapter(tmp_path, approval_repository=ApprovalRepositoryAdapter(tmp_path)),
        CheckpointRepositoryAdapter(tmp_path),
        build_context_provider(ContextDependencies()),
        llm,
        approval_tool,
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
        token_counter=AlwaysWithinBudgetCounter(),
        tool_recovery=file_tool_recovery(tmp_path),
        history_compactor=UnexpectedHistoryCompactor(),
    )

    waiting: RunResult = await engine.execute(_request_with_history("session-approval-context"))
    completed: RunResult = await engine.resolve_approval(waiting.approval_id or "", approved=True)

    assert completed.state.outcome() is RunOutcome.COMPLETED
    second_context: ContextBundle = llm.contexts[1]
    assert [message.content for message in second_context.messages] == [
        "这是之前的回答。",
        "请查询资料。",
        "我将查询两个来源。",
        "",
        "已执行",
        "已执行",
    ]
    assert second_context.messages[2].tool_calls == (
        ToolCall("call-1", "lookup", {"source": "first"}),
        ToolCall("call-2", "lookup", {"source": "second"}),
    )


async def test_approval_resume_replays_frozen_system_slots(tmp_path: Path) -> None:
    """审批恢复不得重新计算动态 Slot，后续上下文必须使用首次冻结文本。"""
    llm: ReActLLM = ReActLLM()
    changing_slot: ChangingSystemSlot = ChangingSystemSlot()
    engine: RuntimeEngine = RuntimeEngine(
        RunRepositoryAdapter(tmp_path, approval_repository=ApprovalRepositoryAdapter(tmp_path)),
        CheckpointRepositoryAdapter(tmp_path),
        _changing_context_port(changing_slot),
        llm,
        ApprovalTool(),
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
        token_counter=AlwaysWithinBudgetCounter(),
        tool_recovery=file_tool_recovery(tmp_path),
        history_compactor=UnexpectedHistoryCompactor(),
    )

    waiting: RunResult = await engine.execute(_request_with_history("session-frozen-slots"))
    completed: RunResult = await engine.resolve_approval(waiting.approval_id or "", approved=True)

    assert completed.state.outcome() is RunOutcome.COMPLETED
    assert changing_slot.calls == 1
    assert llm.contexts[0].messages[0].content == "system-1"
    assert llm.contexts[1].messages[0].content == "system-1"


async def test_rejected_approval_records_decision_and_cancels_without_conversation(tmp_path: Path) -> None:
    """拒绝审批应记录决议并结束原 Run，不投影 assistant Conversation。"""
    run_repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path, approval_repository=ApprovalRepositoryAdapter(tmp_path))
    engine = RuntimeEngine(
        run_repository,
        CheckpointRepositoryAdapter(tmp_path),
        ContextFake(),
        ApprovalLLM(),
        ApprovalTool(),
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
        token_counter=AlwaysWithinBudgetCounter(),
        tool_recovery=file_tool_recovery(tmp_path),
        history_compactor=UnexpectedHistoryCompactor(),
    )

    waiting: RunResult = await engine.execute(_request("session-rejected"))
    rejected: RunResult = await engine.resolve_approval("approval-1", approved=False)
    events_path: Path = tmp_path / "session-rejected" / "agent_runs" / waiting.run_id / "events.jsonl"
    event_types: list[str] = [
        json.loads(line)["event_type"]
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]

    assert rejected.state.outcome() is RunOutcome.CANCELLED
    assert rejected.run_id == waiting.run_id
    assert "approval_resolved" in event_types
    assert "run_cancelled" in event_types
    assert await run_repository.load_conversation("session-rejected") == ()


async def test_cancel_waiting_run_does_not_write_conversation(tmp_path: Path) -> None:
    """取消审批等待中的 run 会删除 checkpoint 且不投影 assistant 消息。"""
    run_repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path, approval_repository=ApprovalRepositoryAdapter(tmp_path))
    engine = RuntimeEngine(
        run_repository,
        CheckpointRepositoryAdapter(tmp_path),
        ContextFake(),
        ApprovalLLM(),
        ApprovalTool(),
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
        token_counter=AlwaysWithinBudgetCounter(),
        tool_recovery=file_tool_recovery(tmp_path),
        history_compactor=UnexpectedHistoryCompactor(),
    )
    waiting = await engine.execute(_request("session-cancel"))
    await engine.request_cancel(waiting.run_id, "用户取消")
    await engine.complete_cancellation(waiting.run_id)

    cancelled = await run_repository.load_run("session-cancel", waiting.run_id)
    assert cancelled is not None
    assert cancelled.state.outcome().value == "cancelled"
    ledger = await run_repository.load_tool_ledger("session-cancel", waiting.run_id)
    assert ledger is not None
    call = next(item for item in ledger.calls if item.call.call_id == "call-1")
    assert call.state is ToolCallState.CANCELLED
    assert call.result is not None and call.result.effect_certainty is ToolEffectCertainty.KNOWN
    assert await run_repository.load_conversation("session-cancel") == ()
    assert await CheckpointRepositoryAdapter(tmp_path).load("session-cancel", waiting.run_id) is None


async def test_cancel_started_ledger_call_keeps_unknown_effect_evidence(tmp_path: Path) -> None:
    run_repository = RunRepositoryAdapter(tmp_path, approval_repository=ApprovalRepositoryAdapter(tmp_path))
    engine = RuntimeEngine(
        run_repository, CheckpointRepositoryAdapter(tmp_path), ContextFake(), ApprovalLLM(), ApprovalTool(),
        PolicyPort(), ApprovalService(ApprovalRepositoryAdapter(tmp_path)), CancellationService(),
        token_counter=AlwaysWithinBudgetCounter(), tool_recovery=file_tool_recovery(tmp_path),
        history_compactor=UnexpectedHistoryCompactor(),
    )
    waiting = await engine.execute(_request("session-cancel-started"))
    run = await run_repository.load_run(waiting.session_id, waiting.run_id)
    checkpoint = await CheckpointRepositoryAdapter(tmp_path).load(waiting.session_id, waiting.run_id)
    assert run is not None and checkpoint is not None
    async with engine._cancellation_service.gate(run.run_id):
        await engine._tool_recovery.decide_approval(run, "approval-1", True, checkpoint)
    run = await run_repository.load_run(waiting.session_id, waiting.run_id)
    checkpoint = await CheckpointRepositoryAdapter(tmp_path).load(waiting.session_id, waiting.run_id)
    assert run is not None and checkpoint is not None
    async with engine._cancellation_service.gate(run.run_id):
        await engine._tool_recovery.begin_attempt(run, "call-1", checkpoint)

    await engine.request_cancel(waiting.run_id, "开始后取消")
    result = await engine.complete_cancellation(waiting.run_id)

    ledger = await run_repository.load_tool_ledger(waiting.session_id, waiting.run_id)
    assert ledger is not None
    call = next(item for item in ledger.calls if item.call.call_id == "call-1")
    assert result.state.outcome() is RunOutcome.CANCELLED
    assert call.state is ToolCallState.CANCELLED
    assert call.result is not None and call.result.effect_certainty is ToolEffectCertainty.UNKNOWN


async def test_cancel_waits_for_observed_tool_result_projection(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._llm_port = ReActLLM()
    engine._tool_port = CompletedTool()
    projection_started, allow_projection = asyncio.Event(), asyncio.Event()
    commit_outcome = engine._tool_recovery.commit_outcome

    async def blocked_commit(run, call_id, outcome, checkpoint):
        projection_started.set()
        await allow_projection.wait()
        return await commit_outcome(run, call_id, outcome, checkpoint)

    engine._tool_recovery.commit_outcome = blocked_commit
    task = asyncio.create_task(engine.execute(_request("session-observed-result-cancel")))
    try:
        await asyncio.wait_for(projection_started.wait(), 2)
        active = await engine.active_run("session-observed-result-cancel")
        assert active is not None
        cancel_task = asyncio.create_task(engine.request_cancel(active.run_id, "结果返回后取消"))
        await asyncio.sleep(0)
        assert not cancel_task.done()
        allow_projection.set()
        await cancel_task
        result = await task
        assert result.control_status == "cancelling"
        cancelled = await engine.complete_cancellation(active.run_id)
    finally:
        allow_projection.set()
        if not task.done():
            await task

    ledger = await engine._run_repository.load_tool_ledger(active.session_id, active.run_id)
    assert cancelled.state.outcome() is RunOutcome.CANCELLED
    assert ledger is not None
    calls = {item.call.call_id: item for item in ledger.calls}
    assert calls["call-1"].state is ToolCallState.SUCCEEDED
    assert calls["call-1"].result is not None and calls["call-1"].result.status.value == "completed"
    assert calls["call-2"].state is ToolCallState.CANCELLED


async def test_driver_cancellation_while_waiting_for_result_gate_keeps_observed_result(tmp_path: Path) -> None:
    """驱动在等待结果提交短锁时取消，也必须等账本投影完整提交。"""
    engine = _engine(tmp_path)
    engine._llm_port = ReActLLM()
    tool = CompletedTool()
    engine._tool_port = tool
    tool_started, allow_tool_return = asyncio.Event(), asyncio.Event()

    async def delayed_execute(invocation, execution):
        tool.calls.append(invocation)
        tool_started.set()
        await allow_tool_return.wait()
        return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED,
            output=f"结果-{invocation.call.call_id}", execution_evidence=ToolExecutionEvidence.RETURNED)

    tool.execute = delayed_execute
    task = asyncio.create_task(engine.execute(_request("session-result-gate-driver-cancel")))
    run_id = None
    gate = None
    try:
        await tool_started.wait()
        active = await engine.active_run("session-result-gate-driver-cancel")
        assert active is not None
        run_id = active.run_id
        gate = engine._cancellation_service.gate(run_id)
        await gate.acquire()
        allow_tool_return.set()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        gate.release()
        await task
    finally:
        allow_tool_return.set()
        if gate is not None and gate.locked():
            gate.release()
        if not task.done():
            await task

    ledger = await engine._run_repository.load_tool_ledger(active.session_id, run_id)
    assert ledger is not None
    assert next(item for item in ledger.calls if item.call.call_id == "call-1").state is ToolCallState.SUCCEEDED


class OrderedEngine:
    """记录协调器执行顺序的最小 Engine 替身。"""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.release: asyncio.Event = asyncio.Event()

    async def execute(self, request: RunRequest, output_port=None) -> RunResult:
        """记录请求顺序；首条请求等待以制造竞争窗口。"""
        self.started.append(request.lease_id)
        if request.lease_id == "lease-fifo-1":
            await self.release.wait()
        return RunResult(request.lease_id, AgentRunState(mode=Ended(RunOutcome.COMPLETED)))

    async def active_run(self, session_id: str) -> AgentRun | None:
        """测试替身不占用 Session。"""
        return None

    async def resume_run(self, run_id: str, output_port=None) -> RunResult:
        """测试替身不支持运行恢复。"""
        return RunResult(run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)))

    async def abandon_run(self, run_id: str) -> RunResult:
        """测试替身不支持运行放弃。"""
        return RunResult(run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)))


async def test_session_coordinator_serializes_same_session_fifo() -> None:
    """同 Session 请求严格串行，后请求不得在前请求完成前启动。"""
    notifier = RunChangeNotifier()
    engine = OrderedEngine()
    coordinator = SessionRunCoordinator(engine, run_change_notifier=notifier)
    first_request = _request("session-fifo")
    second_request = RunRequest("session-fifo", "lease-fifo-2", first_request.agent_id, first_request.user_message, first_request.conversation)
    first_request = RunRequest("session-fifo", "lease-fifo-1", first_request.agent_id, first_request.user_message, first_request.conversation)
    first_task = asyncio.create_task(coordinator.submit(first_request))
    await asyncio.sleep(0)
    second_task = asyncio.create_task(coordinator.submit(second_request))
    await asyncio.sleep(0)
    assert engine.started == ["lease-fifo-1"]
    engine.release.set()
    await asyncio.gather(first_task, second_task)
    assert engine.started == ["lease-fifo-1", "lease-fifo-2"]


class ControlOrderedEngine:
    """模拟审批恢复期间的 Session 控制入口，验证协调器复用租约。"""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.approval_entered: asyncio.Event = asyncio.Event()
        self.release_approval: asyncio.Event = asyncio.Event()

    async def execute(self, request: RunRequest, output_port=None) -> RunResult:
        """记录普通请求何时真正获得执行机会。"""
        self.started.append(f"submit:{request.lease_id}")
        return RunResult(request.lease_id, AgentRunState(mode=Ended(RunOutcome.COMPLETED)))

    async def get_approval_session_id(self, approval_id: str) -> str | None:
        """将测试审批固定映射到同一 Session。"""
        return "session-control" if approval_id == "approval-control" else None

    async def resolve_approval(self, approval_id: str, approved: bool, output_port=None) -> RunResult:
        """阻塞审批恢复，制造与普通消息竞争同一租约的窗口。"""
        self.started.append(f"approval:{approval_id}")
        self.approval_entered.set()
        await self.release_approval.wait()
        return RunResult("run-control", AgentRunState(mode=Ended(RunOutcome.COMPLETED)))

    async def get_run_session_id(self, run_id: str) -> str | None:
        """测试不触发取消路径。"""
        return None

    async def cancel(self, run_id: str, reason: str) -> None:
        """测试替身不需要取消行为。"""

    async def active_run(self, session_id: str) -> AgentRun | None:
        """测试替身不占用 Session。"""
        return None

    async def resume_run(self, run_id: str, output_port=None) -> RunResult:
        """测试替身不支持运行恢复。"""
        return RunResult(run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)))

    async def abandon_run(self, run_id: str) -> RunResult:
        """测试替身不支持运行放弃。"""
        return RunResult(run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)))


async def test_session_coordinator_serializes_approval_resume_with_new_message() -> None:
    """审批恢复与新消息必须竞争同一 Session 租约，避免并发执行。"""
    notifier = RunChangeNotifier()
    engine = ControlOrderedEngine()
    coordinator = SessionRunCoordinator(engine, run_change_notifier=notifier)
    approval_task: asyncio.Task[RunResult] = asyncio.create_task(
        coordinator.resolve_approval("approval-control", True),
    )
    await engine.approval_entered.wait()
    request: RunRequest = _request("session-control")
    submit_task: asyncio.Task[RunResult] = asyncio.create_task(coordinator.submit(request))
    await asyncio.sleep(0)

    assert engine.started == ["approval:approval-control"]
    engine.release_approval.set()
    await asyncio.gather(approval_task, submit_task)
    assert engine.started == ["approval:approval-control", f"submit:{request.lease_id}"]
