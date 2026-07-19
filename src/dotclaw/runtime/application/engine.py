"""仅依赖 Ports 的 Runtime v2 执行引擎。"""

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
from dotclaw.runtime.application.execution import RunBudget, RunExecution
from ..domain.facts import (
    AgentRun,
    ApprovalRecord,
    HistoryContextSnapshot,
    HistoryMessageSnapshot,
    InitialContextSnapshot,
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
    SystemContextSlot,
    SystemContextSlotScope,
    SystemContextSlotStatus,
    SystemContextSnapshot,
    ToolCall,
    utc_now_iso,
)
from ..domain.state import AgentPhase, AgentState
from .approval_service import ApprovalService
from .cancellation_service import CancellationService
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
from .ports import CheckpointRepository, ContextPort, DelegationPort, LLMPort, RunPolicyPort, RunRepository, ToolPort


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
            return await self._drive(execution, run, (), ())
        finally:
            self._cancellation_service.unregister(run_id)

    async def resolve_approval(self, approval_id: str, approved: bool) -> RunResult:
        """消费审批记录，并在同一 run_id 上恢复等待中的执行。"""
        pending_record: ApprovalRecord | None = await self._approval_service.find_pending(approval_id)
        if pending_record is None:
            return RunResult("", RunStatus.FAILED, error=RunError(RunErrorCode.INVALID_STATE, "审批记录不存在或已消费"))
        if await self._run_repository.requires_messages_migration(
            pending_record.session_id,
            pending_record.run_id,
        ):
            return RunResult(
                pending_record.run_id,
                RunStatus.FAILED,
                error=RunError(
                    RunErrorCode.PERSISTENCE_FAILURE,
                    "该 Run 使用 messages.json v1，请先执行 scripts/migrate_messages_v1_to_v2.py 后再处理审批",
                ),
            )
        initial_context: InitialContextSnapshot | None = await self._run_repository.load_initial_context(
            pending_record.session_id,
            pending_record.run_id,
        )
        if initial_context is None:
            return RunResult(
                pending_record.run_id,
                RunStatus.FAILED,
                error=RunError(RunErrorCode.PERSISTENCE_FAILURE, "Run 缺少冻结 initial_context，拒绝恢复审批"),
            )
        frozen_initial_context: InitialContextSnapshot = initial_context
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
            conversation=_conversation_from_initial_context(frozen_initial_context),
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
            initial_context=frozen_initial_context,
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
                return await self._finish_cancelled(execution, run, messages, event_sequence, "审批被拒绝")
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
            return await self._drive(execution, resumed_run, messages, pending_calls, event_sequence)
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
        await self._finish_cancelled(execution, run, messages, event_sequence, reason)

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
                        continue
                    try:
                        tool_result: ToolResult = await self._tool_port.execute(
                            ToolInvocation(execution.run_id, tool_call),
                            execution.view(),
                        )
                    except Exception as error:
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
                context = await self._context_port.build(execution.request, execution.view())
            except Exception as error:
                return await self._fail(execution, run, tuple(messages), event_number, f"模型上下文构建失败：{error}")
            if context.metadata.truncation_applied:
                return await self._fail(
                    execution,
                    run,
                    tuple(messages),
                    event_number,
                    "Runtime 不允许在 Run 内静默裁剪上下文，请在创建 Run 前压缩 Session 历史",
                )
            initial_context: InitialContextSnapshot | None = await self._run_repository.load_initial_context(
                run.session_id,
                run.run_id,
            )
            if initial_context is None:
                initial_context = _initial_context_from_request(execution.request, context)
                await self._run_repository.save_initial_context(
                    run.session_id,
                    run.run_id,
                    initial_context,
                )
            execution.freeze_initial_context(initial_context)
            event_number = await self._event(
                run,
                event_number,
                RunEventType.LLM_STARTED,
                tuple(message.message_id for message in messages),
                "模型调用开始",
                _llm_started_data(run, initial_context, messages, context),
            )
            try:
                response = await self._llm_port.complete(context, execution.view())
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
                await self._run_repository.commit_success(completed, response_message, completed_event)
                await self._checkpoint_repository.delete(run.session_id, run.run_id)
                return RunResult(
                    run.run_id,
                    RunStatus.COMPLETED,
                    ConversationMessage(response_message.message_id, MessageRole.ASSISTANT, response_message.content, completed.ended_at or ""),
                    has_streamed_text=execution.has_streamed_text,
                )
            pending_calls = response.tool_calls
        return await self._fail(execution, run, tuple(messages), event_number, "状态机意外结束")

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

    async def _finish_cancelled(self, execution: RunExecution, run: AgentRun, messages: tuple[RunMessage, ...], event_sequence: int, reason: str) -> RunResult:
        """持久化取消终态、删除检查点且不投影 Conversation。"""
        cancelled = replace(run, status=RunStatus.CANCELLED, ended_at=utc_now_iso(), error=RunError(RunErrorCode.CANCELLED, reason))
        await self._run_repository.save_run(cancelled)
        await self._checkpoint_repository.delete(run.session_id, run.run_id)
        await self._event(run, event_sequence, RunEventType.RUN_CANCELLED, ())
        return RunResult(run.run_id, RunStatus.CANCELLED, error=cancelled.error)

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


def _initial_context_from_request(
    request: RunRequest,
    context: ContextBundle,
) -> InitialContextSnapshot:
    """将首次实际注入的 system 和 Session 历史冻结为 Run 的顶层快照。"""
    system_context: SystemContextSnapshot = context.metadata.system_context or _fallback_system_context(context)
    recent_messages: list[HistoryMessageSnapshot] = []
    conversation_message: ConversationMessage
    for conversation_message in request.conversation.messages:
        if _is_compression_summary_message(conversation_message):
            continue
        recent_messages.append(HistoryMessageSnapshot(
            conversation_id=conversation_message.message_id,
            role=conversation_message.role,
            content=conversation_message.content,
            created_at=conversation_message.created_at,
        ))
    history: HistoryContextSnapshot = HistoryContextSnapshot(
        source_session_id=request.conversation.session_id,
        source_conversation_version=request.conversation.version,
        recent_messages=tuple(recent_messages),
        content_hash=_hash_json_value({
            "compressed_history": (
                None
                if request.conversation.compressed_history is None
                else request.conversation.compressed_history.to_dict()
            ),
            "recent_messages": [message.to_dict() for message in recent_messages],
        }),
        compressed_history=request.conversation.compressed_history,
        truncation_applied=context.metadata.truncation_applied,
    )
    return InitialContextSnapshot(system_context=system_context, history=history)


def _fallback_system_context(context: ContextBundle) -> SystemContextSnapshot:
    """为旧 ContextPort 生成兼容快照，确保新运行不再持久化 LLM_REQUEST。"""
    system_content: str = "\n\n".join(
        message.content for message in context.messages if message.role is MessageRole.SYSTEM
    )
    slot: SystemContextSlot = SystemContextSlot(
        name="legacy_context_port",
        scope=SystemContextSlotScope.DYNAMIC,
        status=SystemContextSlotStatus.INCLUDED if system_content else SystemContextSlotStatus.EMPTY,
        content=system_content,
        content_hash=_hash_text(system_content) if system_content else "",
    )
    return SystemContextSnapshot(
        version=1,
        slot_order=(slot.name,),
        slots=(slot,),
        rendered_content_hash=_hash_text(system_content),
    )


def _llm_started_data(
    run: AgentRun,
    initial_context: InitialContextSnapshot,
    messages: list[RunMessage],
    context: ContextBundle,
) -> JSONMap:
    """构造可复原模型输入的审计字段，而不将完整输入重复写入消息流。"""
    history_version: int = (
        initial_context.history.compressed_history.compression_version
        if initial_context.history.compressed_history is not None
        else initial_context.history.source_conversation_version
    )
    return {
        "call_index": run.statistics.llm_call_count + 1,
        "model_id": run.policy.model_id,
        "system_context_version": initial_context.system_context.version,
        "history_version": history_version,
        "incremental_message_ids": [message.message_id for message in messages],
        "context_hash": _hash_json_value([message.to_dict() for message in context.messages]),
        "tool_schema_hash": _hash_json_value([tool.to_dict() for tool in context.tools]),
    }


def _conversation_from_initial_context(initial_context: InitialContextSnapshot) -> ConversationSnapshot:
    """由冻结快照重建审批恢复所需的历史，不查询当前 Session 可变状态。"""
    messages: list[ConversationMessage] = []
    compression = initial_context.history.compressed_history
    if compression is not None:
        messages.append(ConversationMessage(
            message_id=f"history-compression-{compression.compression_version}",
            role=MessageRole.SYSTEM,
            content=f"以下是此前对话的压缩摘要：\n{compression.content}",
            created_at="",
        ))
    history_message: HistoryMessageSnapshot
    for history_message in initial_context.history.recent_messages:
        messages.append(ConversationMessage(
            message_id=history_message.conversation_id,
            role=history_message.role,
            content=history_message.content,
            created_at=history_message.created_at,
        ))
    return ConversationSnapshot(
        session_id=initial_context.history.source_session_id,
        messages=tuple(messages),
        version=initial_context.history.source_conversation_version,
        compressed_history=compression,
    )


def _is_compression_summary_message(message: ConversationMessage) -> bool:
    """识别历史准备服务生成的摘要占位消息，避免在初始快照中重复保存。"""
    return message.message_id.startswith("history-compression-") and message.role is MessageRole.SYSTEM


def _hash_text(content: str) -> str:
    """计算 UTF-8 文本的 SHA-256 摘要。"""
    return sha256(content.encode("utf-8")).hexdigest()


def _hash_json_value(value: JSONValue | list[JSONMap]) -> str:
    """以稳定 JSON 序列化计算审计 hash，避免字典顺序影响结果。"""
    serialized: str = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _hash_text(serialized)


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
