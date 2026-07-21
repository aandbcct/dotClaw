"""仅依赖 Ports 的 Runtime v4 执行引擎。"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from hashlib import sha256

from ..domain.events import (
    ApprovalResolved,
    DelegationCompleted,
    DelegationSubmitted,
    LLMCompleted,
    LLMCompletionKind,
    RunEvent,
    RunEventType,
    RunStarted,
    ToolCompleted,
    ToolCompletionKind,
)
from ..domain.context import (
    ContextContributionKind,
    ContextOwner,
    ContextPersistenceMode,
    ContextSlotSnapshot,
    ContextSlotStatus,
    ConversationMessagesSlotContent,
    ConversationSlotMessage,
    TextSlotContent,
    ContextVersion,
    StagedHistoryCompression,
    StagedHistoryCompressionStatus,
    SuccessCommitIntent,
    new_context_version,
)
from dotclaw.runtime.application.execution import RunBudget, RunExecution
from ..domain.facts import (
    AgentRun,
    ApprovalRecord,
    HistoryCompressionSnapshot,
    JSONMap,
    JSONValue,
    MessageRole,
    RunCheckpoint,
    RunError,
    RunErrorCode,
    RunMessage,
    RunMessageKind,
    RunStatistics,
    RunStatus,
    ToolCall,
    utc_now_iso,
)
from ..domain.state import AgentPhase, AgentState
from ..domain.control import AgentAction
from .approval_service import ApprovalService
from .cancellation_service import CancellationService
from .context_budget import ContextBudgetDecision, ContextBudgetPlanner, ContextBudgetStatus, TokenCountRequest
from .dto import (
    ContextBundle,
    ConversationMessage,
    ConversationSnapshot,
    DelegationRequest,
    DelegationResult,
    DelegationSubmission,
    RunRequest,
    RunResult,
    ToolInvocation,
    ToolResult,
    ToolResultStatus,
)
from .history_compaction import ConversationBatch, HistoryCompactorUnavailable, compact_in_batches, select_oldest_conversations
from .ports import CheckpointRepository, ContextPort, DelegationPort, HistoryCompactorPort, LLMPort, LLMUnavailableError, RunPolicyPort, RunRepository, TokenCounterPort, ToolPort


class ContextBudgetRejected(RuntimeError):
    """真实输入无法满足上下文窗口时携带失败枚举的确定性错误。"""

    def __init__(self, message: str, code: RunErrorCode = RunErrorCode.CONTEXT_BUDGET) -> None:
        """保存供失败结果使用的精确错误类别。"""
        super().__init__(message)
        self.code: RunErrorCode = code


@dataclass(frozen=True)
class _PreparedContext:
    """预算安全点准备出的实际输入与可选待持久化候选。"""

    context: ContextBundle
    candidate: StagedHistoryCompression | None = None


class RuntimeEngine:
    """创建局部 RunExecution 并以确定顺序驱动 Ports 的共享执行机。"""

    def __init__(
        self,
        run_repository: RunRepository,
        checkpoint_repository: CheckpointRepository,
        context_port: ContextPort,
        llm_port: LLMPort,
        tool_port: ToolPort,
        policy_port: RunPolicyPort,
        approval_service: ApprovalService,
        cancellation_service: CancellationService,
        delegation_port: DelegationPort | None = None,
        token_counter: TokenCounterPort | None = None,
        history_compactor: HistoryCompactorPort | None = None,
    ) -> None:
        """绑定执行所需 Ports；不保存任何单次运行的状态。"""
        self._run_repository: RunRepository = run_repository
        self._checkpoint_repository: CheckpointRepository = checkpoint_repository
        self._context_port: ContextPort = context_port
        self._llm_port: LLMPort = llm_port
        self._tool_port: ToolPort = tool_port
        self._policy_port: RunPolicyPort = policy_port
        self._approval_service: ApprovalService = approval_service
        self._cancellation_service: CancellationService = cancellation_service
        self._delegation_port: DelegationPort | None = delegation_port
        if token_counter is None or history_compactor is None:
            raise ValueError("RuntimeEngine 必须装配 TokenCounterPort 和 HistoryCompactorPort")
        self._budget_planner: ContextBudgetPlanner = ContextBudgetPlanner(token_counter)
        self._token_counter: TokenCounterPort = token_counter
        self._history_compactor: HistoryCompactorPort = history_compactor

    async def execute(self, request: RunRequest) -> RunResult:
        """创建新的 RunExecution，并执行到成功、失败、取消或审批等待。"""
        policy = await self._policy_port.resolve(request)
        run_id: str = request.run_id or uuid.uuid4().hex
        execution: RunExecution = RunExecution(
            run_id=run_id,
            request=request,
            policy=policy,
            state=AgentState(),
            budget=RunBudget(max_iterations=policy.max_iterations),
        )
        run: AgentRun = AgentRun(
            run_id=run_id,
            session_id=request.session_id,
            agent_id=request.agent_id,
            status=RunStatus.RUNNING,
            started_at=utc_now_iso(),
            policy=policy,
            input_message_id=request.user_message.message_id,
            parent_run_id=request.parent_run_id,
            root_run_id=request.root_run_id or request.parent_run_id,
        )
        await self._run_repository.create_run(run)
        self._cancellation_service.register(run_id, execution.cancellation)
        try:
            result: RunResult = await self._drive(execution, run, (), ())
            await self._release_run_context_if_terminal(result)
            return result
        finally:
            self._cancellation_service.unregister(run_id)

    async def resolve_approval(self, approval_id: str, approved: bool) -> RunResult:
        """消费审批记录，并在同一 run_id 上恢复等待中的执行。"""
        pending_record: ApprovalRecord | None = await self._approval_service.find_pending(approval_id)
        if pending_record is None:
            return RunResult("", RunStatus.FAILED, error=RunError(RunErrorCode.INVALID_STATE, "审批记录不存在或已消费"))
        context_versions: tuple[ContextVersion, ...] = await self._run_repository.load_context_versions(
            pending_record.session_id,
            pending_record.run_id,
        )
        run: AgentRun | None = await self._run_repository.load_run(
            pending_record.session_id,
            pending_record.run_id,
        )
        if run is None or run.active_context_version is None:
            return RunResult(
                pending_record.run_id,
                RunStatus.FAILED,
                error=RunError(RunErrorCode.PERSISTENCE_FAILURE, "Run 缺少活动 Context Version，拒绝恢复审批"),
            )
        active_context_version: ContextVersion | None = next(
            (item for item in context_versions if item.version == run.active_context_version),
            None,
        )
        if active_context_version is None:
            return RunResult(
                pending_record.run_id,
                RunStatus.FAILED,
                error=RunError(RunErrorCode.PERSISTENCE_FAILURE, "Run 活动 Context Version 不存在"),
            )
        record: ApprovalRecord | None = await self._approval_service.consume(approval_id)
        if record is None:
            return RunResult("", RunStatus.FAILED, error=RunError(RunErrorCode.INVALID_STATE, "审批记录不存在或已消费"))
        run = await self._run_repository.load_run(record.session_id, record.run_id)
        checkpoint = await self._checkpoint_repository.load(record.session_id, record.run_id)
        if run is None or checkpoint is None or run.status is not RunStatus.WAITING_APPROVAL:
            return RunResult(record.run_id, RunStatus.FAILED, error=RunError(RunErrorCode.INVALID_STATE, "审批恢复状态无效"))
        messages = await self._run_repository.load_messages(record.session_id, record.run_id)
        input_message = next((message for message in messages if message.message_id == run.input_message_id), None)
        if input_message is None:
            return RunResult(record.run_id, RunStatus.FAILED, error=RunError(RunErrorCode.PERSISTENCE_FAILURE, "缺少运行输入消息"))
        request: RunRequest = RunRequest(
            session_id=run.session_id,
            lease_id="approval-resume",
            agent_id=run.agent_id,
            user_message=ConversationMessage(input_message.message_id, MessageRole.USER, input_message.content, ""),
            conversation=_conversation_from_context_version(active_context_version),
        )
        state: AgentState = _state_from_checkpoint(checkpoint)
        transition = state.transition(ApprovalResolved(approval_id, approved))
        execution: RunExecution = RunExecution(
            run_id=run.run_id,
            request=request,
            policy=run.policy,
            state=transition.state,
            budget=RunBudget(max_iterations=run.policy.max_iterations),
            message_cursor=checkpoint.message_sequence,
            run_messages=messages,
            active_context_version=active_context_version,
            staged_history_compressions=run.staged_history_compressions,
            replay_active_context=True,
        )
        pending_calls: tuple[ToolCall, ...] = _calls_from_checkpoint(checkpoint)
        event_sequence: int = await self._event(
            run,
            checkpoint.event_sequence,
            RunEventType.APPROVAL_RESOLVED,
            (),
            "审批已通过" if approved else "审批已拒绝",
        )
        self._cancellation_service.register(run.run_id, execution.cancellation)
        try:
            if not approved:
                result: RunResult = await self._finish_cancelled(execution, run, messages, event_sequence, "审批被拒绝")
                await self._release_run_context_if_terminal(result)
                return result
            resumed_run: AgentRun = replace(
                run,
                status=RunStatus.RUNNING,
                resume_count=run.resume_count + 1,
            )
            await self._run_repository.save_run(resumed_run)
            event_sequence = await self._event(
                resumed_run,
                event_sequence,
                RunEventType.RUN_RESUMED,
                (),
                "审批通过后恢复运行",
            )
            result = await self._drive(execution, resumed_run, messages, pending_calls, event_sequence)
            await self._release_run_context_if_terminal(result)
            return result
        finally:
            self._cancellation_service.unregister(run.run_id)

    async def get_approval_session_id(self, approval_id: str) -> str | None:
        """返回待处理审批所属 Session，供协调器获取同一把租约锁。"""
        record = await self._approval_service.find_pending(approval_id)
        return record.session_id if record is not None else None

    async def get_run_session_id(self, run_id: str) -> str | None:
        """返回运行所属 Session，供取消操作遵守单 Session 串行约束。"""
        run: AgentRun | None = await self._run_repository.find_run(run_id)
        return run.session_id if run is not None else None

    async def recover_session(self, session_id: str) -> None:
        """将进程重启遗留的 RUNNING Run 转为仍占用 Session 的 INTERRUPTED。"""
        runs: tuple[AgentRun, ...] = await self._run_repository.list_active_runs(session_id)
        run: AgentRun
        for run in runs:
            if run.status is not RunStatus.RUNNING:
                continue
            error: RunError = RunError(RunErrorCode.PROCESS_RESTART, "进程重启导致运行中断", retryable=True)
            interrupted: AgentRun = replace(run, status=RunStatus.INTERRUPTED, error=error)
            await self._run_repository.save_run(interrupted)
            checkpoint: RunCheckpoint | None = await self._checkpoint_repository.load(run.session_id, run.run_id)
            if checkpoint is not None:
                event_sequence: int = await self._event(
                    interrupted,
                    checkpoint.event_sequence,
                    RunEventType.RUN_INTERRUPTED,
                    (),
                )
                await self._checkpoint_repository.save(replace(checkpoint, event_sequence=event_sequence))

    async def active_run(self, session_id: str) -> AgentRun | None:
        """返回当前占用 Session 的唯一非终态 Run。"""
        runs: tuple[AgentRun, ...] = await self._run_repository.list_active_runs(session_id)
        if len(runs) > 1:
            raise RuntimeError("同一 Session 存在多个未终态 Run")
        return runs[0] if runs else None

    async def retry_interrupted(self, run_id: str) -> RunResult:
        """依据 checkpoint 和活动 Context Version 重试被外部服务中断的 Run。"""
        run: AgentRun | None = await self._run_repository.find_run(run_id)
        if run is None or run.status is not RunStatus.INTERRUPTED:
            return RunResult(run_id, RunStatus.FAILED, error=RunError(RunErrorCode.INVALID_STATE, "Run 不处于可重试中断状态"))
        checkpoint: RunCheckpoint | None = await self._checkpoint_repository.load(run.session_id, run.run_id)
        if checkpoint is None or checkpoint.active_context_version is None or checkpoint.next_action is not AgentAction.INVOKE_LLM:
            return RunResult(run_id, RunStatus.FAILED, error=RunError(RunErrorCode.INVALID_STATE, "中断 Run 缺少可重试的 LLM checkpoint"))
        versions: tuple[ContextVersion, ...] = await self._run_repository.load_context_versions(run.session_id, run.run_id)
        version: ContextVersion | None = next((item for item in versions if item.version == checkpoint.active_context_version), None)
        if version is None:
            return RunResult(run_id, RunStatus.FAILED, error=RunError(RunErrorCode.PERSISTENCE_FAILURE, "checkpoint 引用的 Context Version 不存在"))
        messages: tuple[RunMessage, ...] = await self._run_repository.load_messages(run.session_id, run.run_id)
        input_message: RunMessage | None = next((item for item in messages if item.message_id == run.input_message_id), None)
        if input_message is None:
            return RunResult(run_id, RunStatus.FAILED, error=RunError(RunErrorCode.PERSISTENCE_FAILURE, "中断 Run 缺少用户输入消息"))
        request: RunRequest = RunRequest(
            run.session_id,
            "retry-interrupted",
            run.agent_id,
            ConversationMessage(input_message.message_id, MessageRole.USER, input_message.content, ""),
            _conversation_from_context_version(version),
            run_id=run.run_id,
        )
        execution: RunExecution = RunExecution(
            run.run_id,
            request,
            run.policy,
            _state_from_checkpoint(checkpoint),
            RunBudget(run.policy.max_iterations),
            message_cursor=checkpoint.message_sequence,
            run_messages=messages,
            active_context_version=version,
            staged_history_compressions=run.staged_history_compressions,
            replay_active_context=True,
        )
        resumed: AgentRun = replace(run, status=RunStatus.RUNNING, resume_count=run.resume_count + 1)
        await self._run_repository.save_run(resumed)
        event_sequence: int = await self._event(resumed, checkpoint.event_sequence, RunEventType.RUN_RESUMED, (), "重试中断 Run")
        self._cancellation_service.register(run.run_id, execution.cancellation)
        try:
            result: RunResult = await self._drive(execution, resumed, messages, (), event_sequence)
            await self._release_run_context_if_terminal(result)
            return result
        finally:
            self._cancellation_service.unregister(run.run_id)

    async def abandon_interrupted(self, run_id: str) -> RunResult:
        """放弃中断 Run，保留审计事实并解除其 Session 占用。"""
        run: AgentRun | None = await self._run_repository.find_run(run_id)
        if run is None or run.status is not RunStatus.INTERRUPTED:
            return RunResult(run_id, RunStatus.FAILED, error=RunError(RunErrorCode.INVALID_STATE, "Run 不处于可放弃中断状态"))
        checkpoint: RunCheckpoint | None = await self._checkpoint_repository.load(run.session_id, run.run_id)
        abandoned: AgentRun = replace(
            run,
            status=RunStatus.ABANDONED,
            ended_at=utc_now_iso(),
            error=RunError(RunErrorCode.CANCELLED, "已被新的用户请求放弃"),
        )
        await self._run_repository.save_run(abandoned)
        await self._checkpoint_repository.delete(run.session_id, run.run_id)
        if checkpoint is not None:
            await self._event(abandoned, checkpoint.event_sequence, RunEventType.RUN_ABANDONED, ())
        result: RunResult = RunResult(run.run_id, RunStatus.ABANDONED, error=abandoned.error)
        await self._release_run_context_if_terminal(result)
        return result

    async def cancel(self, run_id: str, reason: str) -> None:
        """请求活动 run 停止；等待中的 run 立即持久化为取消终态。"""
        active: bool = self._cancellation_service.request(run_id, reason)
        await self._llm_port.cancel(run_id)
        await self._tool_port.cancel(run_id)
        child_run_id: str | None = self._cancellation_service.delegated_run_id(run_id)
        if child_run_id is not None and self._delegation_port is not None:
            await self._delegation_port.cancel(child_run_id)
        if active:
            return
        run = await self._run_repository.find_run(run_id)
        if run is None or run.status is not RunStatus.WAITING_APPROVAL:
            return
        messages = await self._run_repository.load_messages(run.session_id, run.run_id)
        checkpoint = await self._checkpoint_repository.load(run.session_id, run.run_id)
        event_sequence: int = checkpoint.event_sequence if checkpoint is not None else 0
        execution = RunExecution(run.run_id, RunRequest(run.session_id, "cancel", run.agent_id, ConversationMessage(run.input_message_id, MessageRole.USER, "", ""), ConversationSnapshot(run.session_id, (), 0)), run.policy, AgentState(), RunBudget(run.policy.max_iterations))
        result: RunResult = await self._finish_cancelled(execution, run, messages, event_sequence, reason)
        await self._release_run_context_if_terminal(result)

    async def _release_run_context_if_terminal(self, result: RunResult) -> None:
        """仅在 Run 终态释放 Run Owner 的私有 Slot 实例。"""
        terminal_statuses: frozenset[RunStatus] = frozenset({
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.ABANDONED,
        })
        if result.status in terminal_statuses:
            await self._context_port.release_scope(ContextOwner.RUN, result.run_id)

    async def _drive(self, execution: RunExecution, run: AgentRun, initial_messages: tuple[RunMessage, ...], pending_calls: tuple[ToolCall, ...], event_sequence: int = 0) -> RunResult:
        """驱动局部状态机，并在每个事实边界按顺序持久化。"""
        messages: list[RunMessage] = list(initial_messages)
        execution.replace_run_messages(tuple(messages))
        sequence: int = len(messages)
        event_number: int = event_sequence
        if not messages:
            sequence += 1
            messages.append(RunMessage(execution.request.user_message.message_id, sequence, RunMessageKind.USER_INPUT, MessageRole.USER, execution.request.user_message.content))
            await self._save_messages(run, execution, messages)
            start_transition = execution.state.transition(RunStarted(run.input_message_id))
            execution.update_state(start_transition.state, start_transition.action)
            event_number = await self._event(run, event_number, RunEventType.RUN_STARTED, (run.input_message_id,))
        while not execution.state.is_terminal():
            if execution.cancellation.cancelled:
                return await self._finish_cancelled(execution, run, tuple(messages), event_number, execution.cancellation.reason)
            if execution.state.phase is AgentPhase.WAITING_TOOLS:
                tool_calls: tuple[ToolCall, ...] = pending_calls
                pending_calls = ()
                if not tool_calls:
                    return await self._fail(execution, run, tuple(messages), event_number, "缺少待执行工具调用")
                completed_message_ids: list[str] = []
                tool_index: int
                tool_call: ToolCall
                for tool_index, tool_call in enumerate(tool_calls):
                    delegation_request: DelegationRequest | None = _delegation_request(
                        execution.run_id,
                        run.root_run_id or run.run_id,
                        run.agent_id,
                        run.session_id,
                        tool_call,
                    )
                    if delegation_request is not None:
                        event_number = await self._tool_started_event(run, event_number, messages, tool_call)
                        delegation_result: DelegationDriveResult = await self._delegate(
                            execution,
                            run,
                            messages,
                            sequence,
                            event_number,
                            delegation_request,
                            manage_state=False,
                        )
                        if delegation_result.error is not None:
                            return delegation_result.error
                        run = delegation_result.run
                        sequence = delegation_result.message_sequence
                        event_number = delegation_result.event_sequence
                        completed_message_ids.append(messages[-1].message_id)
                        event_number = await self._tool_completed_event(run, event_number, tool_call, messages[-1].message_id, ToolResultStatus.COMPLETED)
                        continue
                    event_number = await self._tool_started_event(run, event_number, messages, tool_call)
                    try:
                        tool_result: ToolResult = await self._tool_port.execute(
                            ToolInvocation(execution.run_id, tool_call),
                            execution.view(),
                        )
                    except Exception as error:
                        event_number = await self._tool_completed_event(run, event_number, tool_call, None, ToolResultStatus.FAILED, _safe_error_summary(error))
                        return await self._fail(execution, run, tuple(messages), event_number, f"工具调用失败：{error}", RunErrorCode.TOOL_FAILURE)
                    if execution.cancellation.cancelled:
                        return await self._finish_cancelled(
                            execution,
                            run,
                            tuple(messages),
                            event_number,
                            execution.cancellation.reason,
                        )
                    sequence += 1
                    run = _with_tool_statistic(run)
                    tool_message: RunMessage = RunMessage(
                        f"tool-{execution.run_id}-{sequence}",
                        sequence,
                        RunMessageKind.TOOL_RESULT,
                        MessageRole.TOOL,
                        tool_result.output,
                        tool_call_id=tool_result.call_id,
                    )
                    messages.append(tool_message)
                    await self._save_messages(run, execution, messages)
                    event_number = await self._tool_completed_event(run, event_number, tool_call, tool_message.message_id, tool_result.status, _tool_error_summary(tool_result))
                    if tool_result.status is ToolResultStatus.FAILED:
                        error = tool_result.error.message if tool_result.error is not None else tool_result.output or "工具执行失败"
                        return await self._fail(execution, run, tuple(messages), event_number, error, RunErrorCode.TOOL_FAILURE)
                    if tool_result.status is ToolResultStatus.APPROVAL_REQUIRED:
                        record = await self._approval_service.create(run.run_id, run.session_id, tool_result.approval_id)
                        transition = execution.state.transition(ToolCompleted(
                            ToolCompletionKind.APPROVAL_REQUIRED,
                            (tool_message.message_id,),
                            record.approval_id,
                        ))
                        execution.update_state(transition.state, transition.action)
                        remaining_calls: tuple[ToolCall, ...] = tool_calls[tool_index:]
                        checkpoint = RunCheckpoint(
                            f"checkpoint-{run.run_id}",
                            run.run_id,
                            run.session_id,
                            1,
                            event_number + 1,
                            sequence,
                            execution.state.to_dict(),
                            transition.action,
                            {
                                "approval_id": record.approval_id,
                                "tool_calls": [call.to_dict() for call in remaining_calls],
                            },
                            execution.budget.to_dict(),
                            active_context_version=(
                                execution.active_context_version.version
                                if execution.active_context_version is not None else None
                            ),
                        )
                        await self._checkpoint_repository.save(checkpoint)
                        waiting_run: AgentRun = replace(run, status=RunStatus.WAITING_APPROVAL, latest_checkpoint_id=checkpoint.checkpoint_id)
                        await self._run_repository.save_run(waiting_run)
                        event_number = await self._event(run, event_number, RunEventType.WAITING_APPROVAL, (tool_message.message_id,))
                        return RunResult(run.run_id, RunStatus.WAITING_APPROVAL, approval_id=record.approval_id)
                    completed_message_ids.append(tool_message.message_id)
                transition = execution.state.transition(ToolCompleted(
                    ToolCompletionKind.COMPLETED,
                    tuple(completed_message_ids),
                ))
                execution.update_state(transition.state, transition.action)
                continue
            try:
                prepared: _PreparedContext = await self._prepare_context(execution, run, messages)
            except HistoryCompactorUnavailable as error:
                await self._checkpoint_repository.save(
                    _compaction_checkpoint(run, execution, event_number, sequence),
                )
                return await self._interrupt(execution, run, tuple(messages), event_number, str(error))
            except ContextBudgetRejected as error:
                return await self._fail(execution, run, tuple(messages), event_number, str(error), error.code)
            except Exception as error:
                return await self._fail(execution, run, tuple(messages), event_number, f"模型上下文构建失败：{error}")
            context: ContextBundle = prepared.context
            context_version: ContextVersion = await self._append_context_version(
                run,
                execution,
                context,
                messages,
            )
            if prepared.candidate is not None:
                run = await self._persist_staged_candidate(run, execution, prepared.candidate, context_version.version)
            execution.activate_context_version(context_version)
            await self._run_repository.set_active_context_version(
                run.session_id,
                run.run_id,
                context_version.version,
            )
            run = replace(run, active_context_version=context_version.version)
            checkpoint: RunCheckpoint = _llm_checkpoint(run, execution, event_number, sequence, context_version.version)
            await self._checkpoint_repository.save(checkpoint)
            event_number = await self._event(
                run,
                event_number,
                RunEventType.LLM_STARTED,
                tuple(message.message_id for message in messages),
                "模型调用开始",
                _llm_started_data(run, context_version, messages, context),
            )
            await self._checkpoint_repository.save(replace(checkpoint, event_sequence=event_number))
            try:
                response = await self._llm_port.complete(context, execution.view())
            except LLMUnavailableError as error:
                return await self._interrupt(execution, run, tuple(messages), event_number, str(error))
            except Exception as error:
                return await self._fail(execution, run, tuple(messages), event_number, f"模型调用失败：{error}", RunErrorCode.LLM_FAILURE)
            if response.metadata.get("has_streamed_text") is True:
                execution.mark_text_streamed()
            if execution.cancellation.cancelled:
                return await self._finish_cancelled(
                    execution,
                    run,
                    tuple(messages),
                    event_number,
                    execution.cancellation.reason,
                )
            sequence += 1
            run = _with_llm_statistics(run, response)
            final: bool = not response.tool_calls
            response_message = replace(response, message_id=f"response-{execution.run_id}-{sequence}", sequence=sequence, kind=RunMessageKind.FINAL_RESPONSE if final else RunMessageKind.LLM_RESPONSE)
            messages.append(response_message)
            await self._save_messages(run, execution, messages)
            event_number = await self._event(run, event_number, RunEventType.LLM_COMPLETED, (response_message.message_id,))
            transition = execution.state.transition(LLMCompleted(LLMCompletionKind.FINAL_RESPONSE if final else LLMCompletionKind.TOOL_CALLS, response_message.message_id, len(response.tool_calls)))
            execution.update_state(transition.state, transition.action)
            if final:
                completed = replace(run, status=RunStatus.COMPLETED, ended_at=utc_now_iso(), final_message_id=response_message.message_id)
                completed_event: RunEvent = RunEvent(
                    run_id=run.run_id,
                    sequence=event_number + 1,
                    event_type=RunEventType.RUN_COMPLETED,
                    occurred_at=utc_now_iso(),
                    message_ids=(response_message.message_id,),
                )
                success_intent: SuccessCommitIntent = SuccessCommitIntent(
                    conversation_id=f"conversation-{run.run_id}",
                    latest_candidate_id=_latest_staged_candidate_id(completed),
                    target_status=RunStatus.COMPLETED,
                    run_id=run.run_id,
                    session_id=run.session_id,
                )
                await self._run_repository.commit_success(
                    completed,
                    response_message,
                    completed_event,
                    success_intent,
                )
                if success_intent.latest_candidate_id is not None:
                    self._context_port.request_refresh("history_compressions", ContextOwner.SESSION, run.session_id)
                return RunResult(
                    run.run_id,
                    RunStatus.COMPLETED,
                    ConversationMessage(response_message.message_id, MessageRole.ASSISTANT, response_message.content, completed.ended_at or ""),
                    has_streamed_text=execution.has_streamed_text,
                )
            pending_calls = response.tool_calls
        return await self._fail(execution, run, tuple(messages), event_number, "状态机意外结束")

    async def _prepare_context(
        self,
        execution: RunExecution,
        run: AgentRun,
        messages: list[RunMessage],
    ) -> _PreparedContext:
        """在每次 LLM_STARTED 前构造、精确计数并必要时生成历史压缩候选。"""
        context: ContextBundle = await self._context_port.build(execution.request, execution.view())
        decision: ContextBudgetDecision = await self._budget_planner.plan(
            _token_request(context, execution.request, tuple(messages), _tokenizer_encoding(execution.policy.policy_data)),
            _context_window(execution.policy.policy_data),
        )
        execution.record_context_budget_decision(decision)
        if decision.status is ContextBudgetStatus.WITHIN_BUDGET:
            return _PreparedContext(context)
        if decision.status is ContextBudgetStatus.REJECTED:
            code: RunErrorCode = (
                RunErrorCode.TOKENIZER_UNAVAILABLE
                if decision.reason == "tokenizer_unavailable"
                else RunErrorCode.CONTEXT_BUDGET
            )
            raise ContextBudgetRejected(f"{code.value}：{decision.reason}", code)
        return await self._compact_and_rebuild(execution, run, messages)

    async def _compact_and_rebuild(
        self,
        execution: RunExecution,
        run: AgentRun,
        messages: list[RunMessage],
    ) -> _PreparedContext:
        """仅压缩最旧完整 Conversation，重建真实输入后必须再次精确计数。"""
        batches: tuple[ConversationBatch, ...] = await _conversation_batches(
            self._token_counter,
            _tokenizer_encoding(execution.policy.policy_data),
            execution.request.conversation.messages,
        )
        selected: tuple[ConversationBatch, ...] = select_oldest_conversations(batches)
        if not selected:
            raise ContextBudgetRejected("上下文超限且至少必须保留一条最新 Conversation 原文")
        previous_summary: str = (
            execution.request.conversation.compressed_history.content
            if execution.request.conversation.compressed_history is not None else ""
        )
        summary_result = await compact_in_batches(
            self._history_compactor,
            self._token_counter,
            previous_summary,
            selected,
            _context_window(execution.policy.policy_data),
            _tokenizer_encoding(execution.policy.policy_data),
        )
        rebuilt_request: RunRequest = _request_with_compressed_history(
            execution.request,
            selected,
            summary_result.summary,
        )
        execution.request = rebuilt_request
        rebuilt_context: ContextBundle = await self._context_port.build(rebuilt_request, execution.view())
        rebuilt_decision: ContextBudgetDecision = await self._budget_planner.plan(
            _token_request(rebuilt_context, rebuilt_request, tuple(messages), _tokenizer_encoding(execution.policy.policy_data)),
            _context_window(execution.policy.policy_data),
        )
        execution.record_context_budget_decision(rebuilt_decision)
        if rebuilt_decision.status is not ContextBudgetStatus.WITHIN_BUDGET:
            raise ContextBudgetRejected("历史压缩后真实输入仍超过上下文窗口")
        source_hash: str = _hash_json_value([
            {
                "conversation_id": batch.conversation_id,
                "messages": [message.to_dict() for message in batch.messages],
            }
            for batch in selected
        ])
        candidate: StagedHistoryCompression = StagedHistoryCompression(
            candidate_id=f"history-{run.run_id}-{len(execution.staged_history_compressions) + 1}",
            status=StagedHistoryCompressionStatus.STAGED,
            session_baseline_version=execution.request.conversation.version,
            covered_through_conversation_id=selected[-1].conversation_id,
            source_hash=source_hash,
            summary_hash=_hash_text(summary_result.summary),
            context_version=0,
        )
        return _PreparedContext(rebuilt_context, candidate)

    async def _persist_staged_candidate(
        self,
        run: AgentRun,
        execution: RunExecution,
        candidate: StagedHistoryCompression,
        context_version: int,
    ) -> AgentRun:
        """以活动版本引用保存控制信息，摘要正文只保留在 Context Version 中。"""
        finalized: StagedHistoryCompression = replace(candidate, context_version=context_version)
        candidates: list[StagedHistoryCompression] = []
        existing: StagedHistoryCompression
        for existing in execution.staged_history_compressions:
            candidates.append(
                replace(existing, status=StagedHistoryCompressionStatus.SUPERSEDED)
                if existing.status is StagedHistoryCompressionStatus.STAGED else existing
            )
        candidates.append(finalized)
        saved: tuple[StagedHistoryCompression, ...] = tuple(candidates)
        await self._run_repository.save_staged_history_compressions(run.session_id, run.run_id, saved)
        execution.staged_history_compressions = saved
        return replace(run, staged_history_compressions=saved)

    async def _save_messages(
        self,
        run: AgentRun,
        execution: RunExecution,
        messages: list[RunMessage],
    ) -> None:
        """原子保存运行完整消息，并同步给下一轮 ContextPort。"""
        stored_messages: tuple[RunMessage, ...] = tuple(messages)
        await self._run_repository.save_messages(run.session_id, run.run_id, stored_messages)
        execution.replace_run_messages(stored_messages)

    async def _delegate(
        self,
        execution: RunExecution,
        run: AgentRun,
        messages: list[RunMessage],
        sequence: int,
        event_sequence: int,
        request: DelegationRequest,
        manage_state: bool = True,
    ) -> "DelegationDriveResult":
        """提交、获取并持久化一次 delegation 结果，可选择是否推进独立 delegation 状态。"""
        if self._delegation_port is None:
            failed: RunResult = await self._fail(
                execution,
                run,
                tuple(messages),
                event_sequence,
                "当前 Runtime 未装配 DelegationPort",
                RunErrorCode.INVALID_STATE,
            )
            return DelegationDriveResult(error=failed)
        try:
            submission: DelegationSubmission = await self._delegation_port.submit(request)
            child_run_id: str = submission.child_run_id
            self._cancellation_service.register_delegated_run(execution.run_id, child_run_id)
            if manage_state:
                submitted_transition = execution.state.transition(DelegationSubmitted(child_run_id))
                execution.update_state(submitted_transition.state, submitted_transition.action)
            next_event_sequence: int = await self._event(
                run,
                event_sequence,
                RunEventType.DELEGATION_SUBMITTED,
                (),
                "已提交 delegation 子运行",
                {
                    "task_id": submission.task_id,
                    "child_run_id": child_run_id,
                    "target_agent_id": request.target_agent_id,
                    "target_session_id": submission.target_session_id,
                },
            )
            child_result: DelegationResult | None = await self._delegation_port.result(child_run_id)
        except Exception as error:
            failed = await self._fail(
                execution,
                run,
                tuple(messages),
                event_sequence,
                f"delegation 调用失败：{error}",
                RunErrorCode.TOOL_FAILURE,
            )
            return DelegationDriveResult(error=failed)
        finally:
            if 'child_run_id' in locals():
                self._cancellation_service.clear_delegated_run(execution.run_id, child_run_id)
        if execution.cancellation.cancelled:
            cancelled: RunResult = await self._finish_cancelled(
                execution,
                run,
                tuple(messages),
                next_event_sequence,
                execution.cancellation.reason,
            )
            return DelegationDriveResult(error=cancelled)
        if child_result is None:
            failed = await self._fail(
                execution,
                run,
                tuple(messages),
                next_event_sequence,
                "delegation 未返回子运行结果",
                RunErrorCode.INVALID_STATE,
            )
            return DelegationDriveResult(error=failed)
        next_sequence: int = sequence + 1
        child_output: str = child_result.output or (
            child_result.error.message if child_result.error is not None else "delegation 未返回输出"
        )
        result_message: RunMessage = RunMessage(
            message_id=f"delegation-{execution.run_id}-{next_sequence}",
            sequence=next_sequence,
            kind=RunMessageKind.DELEGATION_RESULT,
            role=MessageRole.TOOL,
            content=child_output,
            tool_call_id=request.source_tool_call_id,
            metadata={
                "task_id": submission.task_id,
                "child_run_id": child_result.child_run_id,
                "target_agent_id": request.target_agent_id,
                "target_session_id": submission.target_session_id,
            },
        )
        messages.append(result_message)
        delegated_run: AgentRun = _with_tool_statistic(run)
        await self._save_messages(delegated_run, execution, messages)
        next_event_sequence = await self._event(
            delegated_run,
            next_event_sequence,
            RunEventType.DELEGATION_COMPLETED,
            (result_message.message_id,),
            "delegation 子运行已完成",
            {
                "task_id": submission.task_id,
                "child_run_id": child_result.child_run_id,
                "status": child_result.status.value,
            },
        )
        succeeded: bool = child_result.status is RunStatus.COMPLETED
        if manage_state:
            transition = execution.state.transition(DelegationCompleted(
                child_result.child_run_id,
                succeeded,
                child_result.error,
            ))
            execution.update_state(transition.state, transition.action)
        if not succeeded:
            failed = await self._fail(
                execution,
                delegated_run,
                tuple(messages),
                next_event_sequence,
                child_result.error.message if child_result.error is not None else "delegation 子运行失败",
                RunErrorCode.TOOL_FAILURE,
            )
            return DelegationDriveResult(error=failed)
        return DelegationDriveResult(
            run=delegated_run,
            message_sequence=next_sequence,
            event_sequence=next_event_sequence,
        )

    async def _event(
        self,
        run: AgentRun,
        sequence: int,
        event_type: RunEventType,
        message_ids: tuple[str, ...],
        summary: str = "",
        data: JSONMap | None = None,
    ) -> int:
        """追加引用已保存消息的事件并返回下一序号。"""
        next_sequence: int = sequence + 1
        event: RunEvent = RunEvent(
            run.run_id,
            next_sequence,
            event_type,
            utc_now_iso(),
            message_ids,
            summary,
            data or {},
        )
        await self._run_repository.append_event(run.session_id, event)
        return next_sequence

    async def _tool_started_event(self, run: AgentRun, event_sequence: int, messages: list[RunMessage], tool_call: ToolCall) -> int:
        """在任何 ToolPort 或委派调用前写入无参数正文的开始审计事件。"""
        source_id: str = _source_response_message_id(messages, tool_call.call_id)
        ids: tuple[str, ...] = (source_id,) if source_id else ()
        return await self._event(run, event_sequence, RunEventType.TOOL_STARTED, ids, "工具调用开始", {"source_response_message_id": source_id, "call_id": tool_call.call_id, "tool_name": tool_call.name, "status": "started"})

    async def _tool_completed_event(self, run: AgentRun, event_sequence: int, tool_call: ToolCall, result_message_id: str | None, status: ToolResultStatus, error_summary: str = "") -> int:
        """为成功、审批和异常路径写入唯一的完成审计事件。"""
        ids: tuple[str, ...] = (result_message_id,) if result_message_id is not None else ()
        return await self._event(run, event_sequence, RunEventType.TOOL_COMPLETED, ids, "工具调用完成", {"result_message_id": result_message_id or "", "call_id": tool_call.call_id, "tool_name": tool_call.name, "status": status.value, "error_summary": error_summary})

    async def _append_context_version(
        self,
        run: AgentRun,
        execution: RunExecution,
        context: ContextBundle,
        messages: list[RunMessage],
    ) -> ContextVersion:
        """仅在实际输入内容变化时构造并追加下一版上下文事实。"""
        existing_versions: tuple[ContextVersion, ...] = await self._run_repository.load_context_versions(
            run.session_id,
            run.run_id,
        )
        slots: tuple[ContextSlotSnapshot, ...] = _context_slots_from_bundle(execution.request, context, messages)
        content_hash: str = _snapshot_content_hash(slots)
        tool_schema_hash: str = _tool_schema_hash(slots)
        active_version: ContextVersion | None = execution.active_context_version
        if active_version is not None and _is_same_context_version(
            active_version,
            slots,
            content_hash,
            tool_schema_hash,
        ):
            if not any(item.version == active_version.version for item in existing_versions):
                raise ValueError("活动 Context Version 尚未持久化")
            return active_version
        context_version: ContextVersion = new_context_version(
            version=len(existing_versions) + 1,
            slots=slots,
            content_hash=content_hash,
            tool_schema_hash=tool_schema_hash,
        )
        await self._run_repository.append_context_version(
            run.session_id,
            run.run_id,
            context_version,
        )
        return context_version

    async def _finish_cancelled(self, execution: RunExecution, run: AgentRun, messages: tuple[RunMessage, ...], event_sequence: int, reason: str) -> RunResult:
        """持久化取消终态、删除检查点且不投影 Conversation。"""
        cancelled = replace(run, status=RunStatus.CANCELLED, ended_at=utc_now_iso(), error=RunError(RunErrorCode.CANCELLED, reason))
        await self._run_repository.save_run(cancelled)
        await self._checkpoint_repository.delete(run.session_id, run.run_id)
        await self._event(run, event_sequence, RunEventType.RUN_CANCELLED, ())
        return RunResult(run.run_id, RunStatus.CANCELLED, error=cancelled.error)

    async def _interrupt(
        self,
        execution: RunExecution,
        run: AgentRun,
        messages: tuple[RunMessage, ...],
        event_sequence: int,
        reason: str,
    ) -> RunResult:
        """保留调用前 checkpoint，将可恢复外部不可用映射为 INTERRUPTED。"""
        error: RunError = RunError(RunErrorCode.LLM_FAILURE, reason, retryable=True)
        interrupted: AgentRun = replace(run, status=RunStatus.INTERRUPTED, error=error)
        await self._run_repository.save_run(interrupted)
        interrupted_event_sequence: int = await self._event(
            interrupted,
            event_sequence,
            RunEventType.RUN_INTERRUPTED,
            (),
        )
        checkpoint: RunCheckpoint | None = await self._checkpoint_repository.load(run.session_id, run.run_id)
        if checkpoint is not None:
            await self._checkpoint_repository.save(replace(checkpoint, event_sequence=interrupted_event_sequence))
        return RunResult(run.run_id, RunStatus.INTERRUPTED, error=error)

    async def _fail(
        self,
        execution: RunExecution,
        run: AgentRun,
        messages: tuple[RunMessage, ...],
        event_sequence: int,
        message: str,
        code: RunErrorCode = RunErrorCode.INVALID_STATE,
    ) -> RunResult:
        """持久化失败终态且不投影 Conversation。"""
        error = RunError(code, message)
        failed = replace(run, status=RunStatus.FAILED, ended_at=utc_now_iso(), error=error)
        await self._run_repository.save_run(failed)
        await self._checkpoint_repository.delete(run.session_id, run.run_id)
        await self._event(run, event_sequence, RunEventType.RUN_FAILED, ())
        return RunResult(run.run_id, RunStatus.FAILED, error=error)


def _state_from_checkpoint(checkpoint: RunCheckpoint) -> AgentState:
    """从最小检查点控制字段恢复纯领域状态。"""
    from ..domain.state import AgentPhase
    phase = AgentPhase(str(checkpoint.agent_state.get("phase", AgentPhase.IDLE.value)))
    iteration_value = checkpoint.agent_state.get("iteration", 0)
    iteration = iteration_value if isinstance(iteration_value, int) else 0
    return AgentState(phase=phase, iteration=iteration, waiting_control_id=checkpoint.pending.get("approval_id") if isinstance(checkpoint.pending.get("approval_id"), str) else None)


def _calls_from_checkpoint(checkpoint: RunCheckpoint) -> tuple[ToolCall, ...]:
    """从 pending 控制字段恢复等待审批的工具调用。"""
    raw_tool_calls = checkpoint.pending.get("tool_calls")
    if isinstance(raw_tool_calls, list):
        restored_calls: list[ToolCall] = []
        raw_tool_call: JSONValue
        for raw_tool_call in raw_tool_calls:
            if not isinstance(raw_tool_call, dict):
                return ()
            call_id = raw_tool_call.get("call_id")
            call_name = raw_tool_call.get("name")
            arguments = raw_tool_call.get("arguments")
            if not isinstance(call_id, str) or not isinstance(call_name, str) or not isinstance(arguments, dict):
                return ()
            restored_calls.append(ToolCall(call_id, call_name, arguments))
        return tuple(restored_calls)
    call_id = checkpoint.pending.get("call_id")
    call_name = checkpoint.pending.get("call_name")
    arguments = checkpoint.pending.get("arguments")
    if not isinstance(call_id, str) or not isinstance(call_name, str) or not isinstance(arguments, dict):
        return ()
    return (ToolCall(call_id, call_name, arguments),)


def _llm_started_data(
    run: AgentRun,
    context_version: ContextVersion,
    messages: list[RunMessage],
    context: ContextBundle,
) -> JSONMap:
    """构造可复原模型输入的审计字段，而不将完整输入重复写入消息流。"""
    return {
        "call_index": run.statistics.llm_call_count + 1,
        "model_id": run.policy.model_id,
        "context_version": context_version.version,
        "incremental_message_ids": list(context.metadata.fact_reference_message_ids),
        "context_hash": context_version.content_hash,
        "tool_schema_hash": context_version.tool_schema_hash,
    }


def _llm_checkpoint(
    run: AgentRun,
    execution: RunExecution,
    event_sequence: int,
    message_sequence: int,
    context_version: int,
) -> RunCheckpoint:
    """在业务模型调用前保存可恢复安全点，不记录重复上下文正文。"""
    return RunCheckpoint(
        checkpoint_id=f"checkpoint-{run.run_id}",
        run_id=run.run_id,
        session_id=run.session_id,
        checkpoint_sequence=1,
        event_sequence=event_sequence,
        message_sequence=message_sequence,
        agent_state=execution.state.to_dict(),
        next_action=AgentAction.INVOKE_LLM,
        pending={},
        budget=_checkpoint_budget(execution),
        active_context_version=context_version,
        staged_history_compression_ids=tuple(
            candidate.candidate_id for candidate in execution.staged_history_compressions
        ),
    )


def _compaction_checkpoint(
    run: AgentRun,
    execution: RunExecution,
    event_sequence: int,
    message_sequence: int,
) -> RunCheckpoint:
    """在压缩服务不可用时保存可重建输入的 checkpoint，且不生成半成品版本。"""
    return RunCheckpoint(
        checkpoint_id=f"checkpoint-{run.run_id}",
        run_id=run.run_id,
        session_id=run.session_id,
        checkpoint_sequence=1,
        event_sequence=event_sequence,
        message_sequence=message_sequence,
        agent_state=execution.state.to_dict(),
        next_action=AgentAction.INVOKE_LLM,
        pending={},
        budget=_checkpoint_budget(execution),
        staged_history_compression_ids=tuple(
            candidate.candidate_id for candidate in execution.staged_history_compressions
        ),
    )


def _checkpoint_budget(execution: RunExecution) -> JSONMap:
    """合并运行资源预算与本次真实输入预算结论，不保存上下文正文。"""
    budget: JSONMap = execution.budget.to_dict()
    decision: ContextBudgetDecision | None = execution.context_budget_decision
    if decision is not None:
        budget["context_budget"] = decision.to_dict()
    return budget


def _context_window(policy_data: JSONMap) -> int:
    """读取冻结的模型上下文窗口，缺失时以确定性错误拒绝。"""
    value: JSONValue | None = policy_data.get("context_window")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ContextBudgetRejected("Agent 策略缺少有效 context_window")
    return value


def _tokenizer_encoding(policy_data: JSONMap) -> str:
    """读取冻结 Tokenizer 编码，禁止回退到字符估算。"""
    value: JSONValue | None = policy_data.get("tokenizer_encoding")
    if not isinstance(value, str) or not value:
        raise ContextBudgetRejected("Agent 策略缺少 tokenizer_encoding")
    return value


def _token_request(
    context: ContextBundle,
    request: RunRequest,
    run_messages: tuple[RunMessage, ...],
    tokenizer_encoding: str,
) -> TokenCountRequest:
    """从实际 ContextBundle 还原精确计数的全部输入组成。"""
    compressed: HistoryCompressionSnapshot | None = request.conversation.compressed_history
    summary_message: str = _history_summary_message(compressed.content) if compressed is not None else ""
    system_contents: tuple[str, ...] = tuple(
        message.content
        for message in context.messages
        if message.role is MessageRole.SYSTEM and message.content != summary_message
    )
    history_messages: tuple[ConversationMessage, ...] = tuple(
        message for message in request.conversation.messages if message.role is not MessageRole.SYSTEM
    )
    input_run_messages: tuple[RunMessage, ...] = tuple(
        message for message in run_messages if message.message_id != request.user_message.message_id
    )
    return TokenCountRequest(
        tokenizer_encoding=tokenizer_encoding,
        system_contents=system_contents,
        history_summary="" if compressed is None else compressed.content,
        history_messages=history_messages,
        current_user_message=request.user_message,
        run_messages=input_run_messages,
        tools=context.tools,
        protocol_overhead_tokens=0,
    )


async def _conversation_batches(
    token_counter: TokenCounterPort,
    tokenizer_encoding: str,
    messages: tuple[ConversationMessage, ...],
) -> tuple[ConversationBatch, ...]:
    """按用户消息及其后续回答切分完整 Conversation，并使用同一 TokenPort 计数。"""
    batches: list[ConversationBatch] = []
    current_messages: list[ConversationMessage] = []
    current_id: str = ""
    message: ConversationMessage
    for message in messages:
        if message.role is MessageRole.SYSTEM:
            continue
        if message.role is MessageRole.USER:
            if current_messages:
                batches.append(await _count_conversation_batch(token_counter, tokenizer_encoding, current_id, tuple(current_messages)))
            current_id = message.message_id
            current_messages = [message]
            continue
        if current_messages:
            current_messages.append(message)
    if current_messages:
        batches.append(await _count_conversation_batch(token_counter, tokenizer_encoding, current_id, tuple(current_messages)))
    return tuple(batches)


async def _count_conversation_batch(
    token_counter: TokenCounterPort,
    tokenizer_encoding: str,
    conversation_id: str,
    messages: tuple[ConversationMessage, ...],
) -> ConversationBatch:
    """使用精确 TokenCounter 得到一条完整 Conversation 的压缩成本。"""
    result = await token_counter.count(TokenCountRequest(
        tokenizer_encoding=tokenizer_encoding,
        system_contents=(),
        history_summary="",
        history_messages=messages,
        current_user_message=ConversationMessage("budget-empty", MessageRole.USER, "", ""),
        run_messages=(),
        tools=(),
        protocol_overhead_tokens=0,
    ))
    if result.error_code is not None:
        raise ContextBudgetRejected(f"{result.error_code.value}：无法计算 Conversation Token")
    return ConversationBatch(conversation_id, messages, result.input_tokens)


def _request_with_compressed_history(
    request: RunRequest,
    selected: tuple[ConversationBatch, ...],
    summary: str,
) -> RunRequest:
    """以候选摘要替换已覆盖 Conversation，原 Session 绝不在此时被写入。"""
    selected_ids: frozenset[str] = frozenset(batch.conversation_id for batch in selected)
    remaining: list[ConversationMessage] = []
    current_conversation_id: str = ""
    message: ConversationMessage
    for message in request.conversation.messages:
        if message.role is MessageRole.SYSTEM:
            continue
        if message.role is MessageRole.USER:
            current_conversation_id = message.message_id
        if current_conversation_id not in selected_ids:
            remaining.append(message)
    previous: HistoryCompressionSnapshot | None = request.conversation.compressed_history
    compression_version: int = 1 if previous is None else previous.compression_version + 1
    compressed: HistoryCompressionSnapshot = HistoryCompressionSnapshot(
        compression_version=compression_version,
        covered_through_conversation_id=selected[-1].conversation_id,
        content=summary,
        content_hash=_hash_text(summary),
    )
    conversation: ConversationSnapshot = ConversationSnapshot(
        request.session_id,
        tuple(remaining),
        request.conversation.version,
        compressed,
    )
    return replace(request, conversation=conversation)


def _history_summary_message(summary: str) -> str:
    """统一摘要注入文本，保证预算输入与实际消息一致。"""
    return f"以下是此前对话的压缩摘要：\n{summary}"


def _context_slots_from_bundle(
    request: RunRequest,
    context: ContextBundle,
    run_messages: list[RunMessage],
) -> tuple[ContextSlotSnapshot, ...]:
    """将当前有效 system、Session 和 Run 贡献转换为可审计 Slot 快照。"""
    if not context.metadata.slot_snapshots:
        system_content: str = "\n\n".join(message.content for message in context.messages if message.role is MessageRole.SYSTEM)
        history_text: str = request.conversation.compressed_history.content if request.conversation.compressed_history is not None else ""
        conversation_content: ConversationMessagesSlotContent = ConversationMessagesSlotContent(tuple(ConversationSlotMessage(message.message_id, message.role, message.content, message.created_at) for message in request.conversation.messages if message.role is not MessageRole.SYSTEM))
        return (
            ContextSlotSnapshot("context_port", ContextOwner.AGENT, ContextContributionKind.SYSTEM_CONTENT, ContextPersistenceMode.SNAPSHOT, ContextSlotStatus.INCLUDED if system_content else ContextSlotStatus.EMPTY, 0, TextSlotContent(system_content), _hash_text(system_content) if system_content else ""),
            ContextSlotSnapshot("history_compressions", ContextOwner.SESSION, ContextContributionKind.HISTORY_COMPRESSIONS, ContextPersistenceMode.SNAPSHOT, ContextSlotStatus.INCLUDED if history_text else ContextSlotStatus.EMPTY, 1, TextSlotContent(history_text), _hash_text(history_text) if history_text else ""),
            ContextSlotSnapshot("conversation", ContextOwner.RUN, ContextContributionKind.CONVERSATION_MESSAGES, ContextPersistenceMode.SNAPSHOT, ContextSlotStatus.INCLUDED if conversation_content.messages else ContextSlotStatus.EMPTY, 2, conversation_content, _hash_json_value([message.to_dict() for message in conversation_content.messages]) if conversation_content.messages else ""),
        )
    return context.metadata.slot_snapshots


def _snapshot_content_hash(slots: tuple[ContextSlotSnapshot, ...]) -> str:
    """只哈希有序快照型 Slot 的稳定审计字段与规范化正文。"""
    normalized: list[JSONMap] = []
    for slot in slots:
        if slot.persistence_mode is not ContextPersistenceMode.SNAPSHOT:
            continue
        record: JSONMap = slot.to_dict()
        normalized.append({"slot_id": slot.slot_id, "owner": slot.owner.value, "contribution_kind": slot.contribution_kind.value, "injection_order": slot.injection_order, "status": slot.status.value, "content": record["content"]})
    return _hash_json_value(normalized)


def _tool_schema_hash(slots: tuple[ContextSlotSnapshot, ...]) -> str:
    """只哈希 tools Slot 中实际筛选后的工具 Schema。"""
    tools: ContextSlotSnapshot | None = next((slot for slot in slots if slot.slot_id == "tools"), None)
    return _hash_json_value([] if tools is None else tools.to_dict()["content"])


def _is_same_context_version(
    version: ContextVersion,
    slots: tuple[ContextSlotSnapshot, ...],
    content_hash: str,
    tool_schema_hash: str,
) -> bool:
    """比较实际输入与快照，确保重试仅复用内容完全相同的活动版本。"""
    return (
        version.slots == slots
        and version.content_hash == content_hash
        and version.tool_schema_hash == tool_schema_hash
    )


def _conversation_from_context_version(context_version: ContextVersion) -> ConversationSnapshot:
    """从 v4 的 Conversation 与摘要 Slot 重建审批恢复历史视图。"""
    conversation_slot: ContextSlotSnapshot | None = next((slot for slot in context_version.slots if slot.contribution_kind is ContextContributionKind.CONVERSATION_MESSAGES), None)
    if conversation_slot is None or not isinstance(conversation_slot.content, ConversationMessagesSlotContent):
        return ConversationSnapshot("", (), 0)
    history_slot: ContextSlotSnapshot | None = next((slot for slot in context_version.slots if slot.contribution_kind is ContextContributionKind.HISTORY_COMPRESSIONS), None)
    summary: str = history_slot.content.text if history_slot is not None and isinstance(history_slot.content, TextSlotContent) else ""
    messages: tuple[ConversationMessage, ...] = tuple(ConversationMessage(message.message_id, message.role, message.content, message.created_at) for message in conversation_slot.content.messages)
    compression: HistoryCompressionSnapshot | None = HistoryCompressionSnapshot(1, "", summary, _hash_text(summary)) if summary else None
    return ConversationSnapshot("", messages, 0, compression)


def _conversation_snapshot_from_dict(data: JSONMap) -> ConversationSnapshot:
    """从 Context Version 的严格 JSON 载荷恢复 ConversationSnapshot。"""
    raw_messages: JSONValue | None = data.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("Conversation 载荷缺少 messages 数组")
    messages: list[ConversationMessage] = []
    raw_message: JSONValue
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            raise ValueError("Conversation 消息必须是对象")
        message_id: JSONValue | None = raw_message.get("id")
        role: JSONValue | None = raw_message.get("role")
        content: JSONValue | None = raw_message.get("content")
        created_at: JSONValue | None = raw_message.get("created_at")
        if not all(isinstance(value, str) for value in (message_id, role, content, created_at)):
            raise ValueError("Conversation 消息字段必须是字符串")
        messages.append(ConversationMessage(
            message_id=message_id,
            role=MessageRole(role),
            content=content,
            created_at=created_at,
        ))
    raw_compression: JSONValue | None = data.get("compressed_history")
    compression: HistoryCompressionSnapshot | None = None
    if raw_compression is not None:
        if not isinstance(raw_compression, dict):
            raise ValueError("compressed_history 必须是对象或 null")
        raw_version: JSONValue | None = raw_compression.get("compression_version")
        raw_covered: JSONValue | None = raw_compression.get("covered_through_conversation_id")
        raw_content: JSONValue | None = raw_compression.get("content")
        raw_hash: JSONValue | None = raw_compression.get("content_hash")
        if (
            not isinstance(raw_version, int)
            or isinstance(raw_version, bool)
            or not all(isinstance(value, str) for value in (raw_covered, raw_content, raw_hash))
        ):
            raise ValueError("compressed_history 字段无效")
        compression = HistoryCompressionSnapshot(raw_version, raw_covered, raw_content, raw_hash)
    raw_session_id: JSONValue | None = data.get("session_id")
    raw_version: JSONValue | None = data.get("version")
    if (
        not isinstance(raw_session_id, str)
        or not isinstance(raw_version, int)
        or isinstance(raw_version, bool)
    ):
        raise ValueError("Conversation 载荷缺少 session_id 或 version")
    return ConversationSnapshot(raw_session_id, tuple(messages), raw_version, compression)


def _hash_text(content: str) -> str:
    """计算 UTF-8 文本的 SHA-256 摘要。"""
    return sha256(content.encode("utf-8")).hexdigest()


def _hash_json_value(value: JSONValue | list[JSONMap]) -> str:
    """以稳定 JSON 序列化计算审计 hash，避免字典顺序影响结果。"""
    serialized: str = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _hash_text(serialized)


def _latest_staged_candidate_id(run: AgentRun) -> str | None:
    """返回成功路径唯一可提交的最新历史压缩候选标识。"""
    candidates: tuple[StagedHistoryCompression, ...] = tuple(
        candidate
        for candidate in run.staged_history_compressions
        if candidate.status is StagedHistoryCompressionStatus.STAGED
    )
    return candidates[-1].candidate_id if candidates else None


def _source_response_message_id(messages: list[RunMessage], call_id: str) -> str:
    """定位产生指定 ToolCall 的唯一 LLM 响应消息。"""
    for message in reversed(messages):
        if any(call.call_id == call_id for call in message.tool_calls):
            return message.message_id
    return ""


def _safe_error_summary(error: Exception) -> str:
    """生成不包含异常栈或敏感参数的短错误摘要。"""
    return f"{type(error).__name__}: {str(error)[:200]}"


def _tool_error_summary(result: ToolResult) -> str:
    """提取工具失败结果的安全错误摘要，不复制完整工具正文。"""
    if result.error is not None:
        return result.error.message[:200]
    return result.output[:200] if result.status is ToolResultStatus.FAILED else ""


def _with_llm_statistics(run: AgentRun, response: RunMessage) -> AgentRun:
    """从标准化响应元数据累加模型调用与 token 统计。"""
    raw_input = response.metadata.get("input_tokens", 0)
    raw_output = response.metadata.get("output_tokens", 0)
    input_tokens = raw_input if isinstance(raw_input, int) and not isinstance(raw_input, bool) else 0
    output_tokens = raw_output if isinstance(raw_output, int) and not isinstance(raw_output, bool) else 0
    statistics = run.statistics
    return replace(run, statistics=RunStatistics(
        duration_ms=statistics.duration_ms,
        llm_call_count=statistics.llm_call_count + 1,
        tool_call_count=statistics.tool_call_count,
        tokens_in=statistics.tokens_in + input_tokens,
        tokens_out=statistics.tokens_out + output_tokens,
    ))


def _with_tool_statistic(run: AgentRun) -> AgentRun:
    """累加工具调用次数，保持其他运行统计不变。"""
    statistics = run.statistics
    return replace(run, statistics=RunStatistics(
        duration_ms=statistics.duration_ms,
        llm_call_count=statistics.llm_call_count,
        tool_call_count=statistics.tool_call_count + 1,
        tokens_in=statistics.tokens_in,
        tokens_out=statistics.tokens_out,
    ))


@dataclass(frozen=True)
class DelegationDriveResult:
    """单次 delegation 驱动后的局部持久化游标或终态结果。"""

    run: AgentRun | None = None
    message_sequence: int = 0
    event_sequence: int = 0
    error: RunResult | None = None


def _delegation_request(
    parent_run_id: str,
    root_run_id: str,
    source_agent_id: str,
    source_session_id: str,
    call: ToolCall,
) -> DelegationRequest | None:
    """将模型的 delegate 调用转换为 Runtime 独立的委托请求。"""
    if call.name != "delegate":
        return None
    target_agent_id = call.arguments.get("target_agent_id")
    title = call.arguments.get("title")
    objective = call.arguments.get("objective")
    if not isinstance(target_agent_id, str) or not isinstance(title, str) or not isinstance(objective, str):
        raise ValueError("delegate 调用必须包含 target_agent_id、title 和 objective")
    content: str = f"任务：{title}\n\n目标：{objective}"
    return DelegationRequest(
        parent_run_id=parent_run_id,
        root_run_id=root_run_id,
        target_agent_id=target_agent_id,
        input_message=ConversationMessage(
            message_id=f"delegation-request-{parent_run_id}-{call.call_id}",
            role=MessageRole.USER,
            content=content,
            created_at=utc_now_iso(),
        ),
        source_agent_id=source_agent_id,
        source_session_id=source_session_id,
        source_tool_call_id=call.call_id,
    )
