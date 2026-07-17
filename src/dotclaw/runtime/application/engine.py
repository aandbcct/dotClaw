"""仅依赖 Ports 的 Runtime v2 执行引擎。"""

from __future__ import annotations

import uuid
from dataclasses import replace

from ..domain.events import (
    ApprovalResolved,
    LLMCompleted,
    LLMCompletionKind,
    RunEvent,
    RunEventType,
    RunStarted,
    ToolCompleted,
    ToolCompletionKind,
)
from ..domain.execution import RunBudget, RunExecution
from ..domain.models import (
    AgentRun,
    ConversationMessage,
    ConversationSnapshot,
    MessageRole,
    RunCheckpoint,
    RunError,
    RunErrorCode,
    RunMessage,
    RunMessageKind,
    RunRequest,
    RunResult,
    RunStatistics,
    RunStatus,
    ToolCall,
    ToolInvocation,
    ToolResult,
    ToolResultStatus,
    utc_now_iso,
)
from ..domain.state import AgentState
from .approval_service import ApprovalService
from .cancellation_service import CancellationService
from .ports import CheckpointRepository, ContextPort, LLMPort, RunPolicyPort, RunRepository, ToolPort


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

    async def execute(self, request: RunRequest) -> RunResult:
        """创建新的 RunExecution，并执行到成功、失败、取消或审批等待。"""
        policy = await self._policy_port.resolve(request)
        run_id: str = uuid.uuid4().hex
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
        )
        await self._run_repository.create_run(run)
        self._cancellation_service.register(run_id, execution.cancellation)
        try:
            return await self._drive(execution, run, (), ())
        finally:
            self._cancellation_service.unregister(run_id)

    async def resolve_approval(self, approval_id: str, approved: bool) -> RunResult:
        """消费审批记录，并在同一 run_id 上恢复等待中的执行。"""
        record = await self._approval_service.consume(approval_id)
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
            conversation=ConversationSnapshot(run.session_id, (), 0),
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
        )
        pending_calls: tuple[ToolCall, ...] = _calls_from_checkpoint(checkpoint)
        self._cancellation_service.register(run.run_id, execution.cancellation)
        try:
            if not approved:
                return await self._finish_cancelled(execution, run, messages, checkpoint.event_sequence, "审批被拒绝")
            return await self._drive(execution, run, messages, pending_calls, checkpoint.event_sequence)
        finally:
            self._cancellation_service.unregister(run.run_id)

    async def cancel(self, run_id: str, reason: str) -> None:
        """请求活动 run 停止；等待中的 run 立即持久化为取消终态。"""
        active: bool = self._cancellation_service.request(run_id, reason)
        await self._llm_port.cancel(run_id)
        await self._tool_port.cancel(run_id)
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
        sequence: int = len(messages)
        event_number: int = event_sequence
        if not messages:
            sequence += 1
            messages.append(RunMessage(execution.request.user_message.message_id, sequence, RunMessageKind.USER_INPUT, MessageRole.USER, execution.request.user_message.content))
            await self._save_messages(run, messages)
            start_transition = execution.state.transition(RunStarted(run.input_message_id))
            execution.update_state(start_transition.state, start_transition.action)
            event_number = await self._event(run, event_number, RunEventType.RUN_STARTED, (run.input_message_id,))
        while not execution.state.is_terminal():
            if execution.cancellation.cancelled:
                return await self._finish_cancelled(execution, run, tuple(messages), event_number, execution.cancellation.reason)
            if execution.state.phase.value == "waiting_tools":
                tool_calls: tuple[ToolCall, ...] = pending_calls
                pending_calls = ()
                if not tool_calls:
                    return await self._fail(execution, run, tuple(messages), event_number, "缺少待执行工具调用")
                try:
                    tool_result = await self._tool_port.execute(ToolInvocation(execution.run_id, tool_calls[0]), execution.view())
                except Exception as error:
                    return await self._fail(execution, run, tuple(messages), event_number, f"工具调用失败：{error}", RunErrorCode.TOOL_FAILURE)
                sequence += 1
                run = _with_tool_statistic(run)
                tool_message = RunMessage(f"tool-{execution.run_id}-{sequence}", sequence, RunMessageKind.TOOL_RESULT, MessageRole.TOOL, tool_result.output, tool_call_id=tool_result.call_id)
                messages.append(tool_message)
                await self._save_messages(run, messages)
                if tool_result.status is ToolResultStatus.FAILED:
                    error = tool_result.error.message if tool_result.error is not None else tool_result.output or "工具执行失败"
                    return await self._fail(execution, run, tuple(messages), event_number, error, RunErrorCode.TOOL_FAILURE)
                if tool_result.status is ToolResultStatus.APPROVAL_REQUIRED:
                    record = await self._approval_service.create(run.run_id, run.session_id, tool_result.approval_id)
                    transition = execution.state.transition(ToolCompleted(ToolCompletionKind.APPROVAL_REQUIRED, (tool_message.message_id,), record.approval_id))
                    execution.update_state(transition.state, transition.action)
                    checkpoint = RunCheckpoint(f"checkpoint-{run.run_id}", run.run_id, run.session_id, 1, event_number + 1, sequence, execution.state.to_dict(), transition.action, {"approval_id": record.approval_id, "call_id": tool_calls[0].call_id, "call_name": tool_calls[0].name, "arguments": tool_calls[0].arguments}, execution.budget.to_dict())
                    await self._checkpoint_repository.save(checkpoint)
                    waiting_run = replace(run, status=RunStatus.WAITING_APPROVAL, latest_checkpoint_id=checkpoint.checkpoint_id)
                    await self._run_repository.save_run(waiting_run)
                    await self._event(run, event_number, RunEventType.WAITING_APPROVAL, (tool_message.message_id,))
                    return RunResult(run.run_id, RunStatus.WAITING_APPROVAL, approval_id=record.approval_id)
                transition = execution.state.transition(ToolCompleted(ToolCompletionKind.COMPLETED, (tool_message.message_id,)))
                execution.update_state(transition.state, transition.action)
                continue
            context = await self._context_port.build(execution.request, execution.view())
            context_message_ids: list[str] = []
            for context_message in context.messages:
                sequence += 1
                persisted_context_message = replace(context_message, message_id=f"context-{execution.run_id}-{sequence}", sequence=sequence)
                messages.append(persisted_context_message)
                context_message_ids.append(persisted_context_message.message_id)
            await self._save_messages(run, messages)
            event_number = await self._event(run, event_number, RunEventType.CONTEXT_BUILT, tuple(context_message_ids))
            try:
                response = await self._llm_port.complete(context, execution.view())
            except Exception as error:
                return await self._fail(execution, run, tuple(messages), event_number, f"模型调用失败：{error}", RunErrorCode.LLM_FAILURE)
            sequence += 1
            run = _with_llm_statistics(run, response)
            final: bool = not response.tool_calls
            response_message = replace(response, message_id=f"response-{execution.run_id}-{sequence}", sequence=sequence, kind=RunMessageKind.FINAL_RESPONSE if final else RunMessageKind.LLM_RESPONSE)
            messages.append(response_message)
            await self._save_messages(run, messages)
            event_number = await self._event(run, event_number, RunEventType.LLM_COMPLETED, (response_message.message_id,))
            transition = execution.state.transition(LLMCompleted(LLMCompletionKind.FINAL_RESPONSE if final else LLMCompletionKind.TOOL_CALLS, response_message.message_id, len(response.tool_calls)))
            execution.update_state(transition.state, transition.action)
            if final:
                completed = replace(run, status=RunStatus.COMPLETED, ended_at=utc_now_iso(), final_message_id=response_message.message_id)
                await self._run_repository.commit_success(completed, response_message)
                await self._checkpoint_repository.delete(run.session_id, run.run_id)
                await self._event(run, event_number, RunEventType.RUN_COMPLETED, (response_message.message_id,))
                return RunResult(run.run_id, RunStatus.COMPLETED, ConversationMessage(response_message.message_id, MessageRole.ASSISTANT, response_message.content, completed.ended_at or ""))
            pending_calls = response.tool_calls
        return await self._fail(execution, run, tuple(messages), event_number, "状态机意外结束")

    async def _save_messages(self, run: AgentRun, messages: list[RunMessage]) -> None:
        """原子保存运行完整消息。"""
        await self._run_repository.save_messages(run.session_id, run.run_id, tuple(messages))

    async def _event(self, run: AgentRun, sequence: int, event_type: RunEventType, message_ids: tuple[str, ...]) -> int:
        """追加引用已保存消息的事件并返回下一序号。"""
        next_sequence: int = sequence + 1
        await self._run_repository.append_event(run.session_id, RunEvent(run.run_id, next_sequence, event_type, utc_now_iso(), message_ids))
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
    call_id = checkpoint.pending.get("call_id")
    call_name = checkpoint.pending.get("call_name")
    arguments = checkpoint.pending.get("arguments")
    if not isinstance(call_id, str) or not isinstance(call_name, str) or not isinstance(arguments, dict):
        return ()
    return (ToolCall(call_id, call_name, arguments),)


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
