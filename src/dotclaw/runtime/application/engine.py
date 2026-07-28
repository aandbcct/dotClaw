"""仅依赖 Ports 的 Runtime v4 执行引擎。"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, replace
from hashlib import sha256

logger = logging.getLogger(__name__)

from ..domain.events import (
    AbandonRequested,
    AgentRunEvent,
    ApprovalGranted,
    ApprovalRejected,
    CancelRequested,
    DelegationCompleted,
    DelegationSubmitted,
    LLMCallFailed,
    LLMResponseProduced,
    RunEvent,
    RunEventType,
    RunStarted,
    ToolApprovalRequired,
    ToolAuditStatus,
    ToolBatchCompleted,
    ToolBatchFailed,
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
    ToolCall,
    utc_now_iso,
)
from ..domain.state import (
    AgentRunState,
    Created,
    Ended,
    InvalidTransition,
    RunOutcome,
    RunStage,
    Running,
    StateTransition,
    Suspended,
    SuspendReason,
    transition,
)
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
from .ports import CheckpointRepository, ContextPort, DelegationPort, HistoryCompactorPort, LLMPort, LLMOutputPort, LLMUnavailableError, RunPolicyPort, RunRepository, TokenCounterPort, ToolPort


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

    async def execute(
        self,
        request: RunRequest,
        output_port: LLMOutputPort | None = None,
    ) -> RunResult:
        """创建新的 RunExecution，并执行到成功、失败、取消或审批等待。

        ``output_port`` 为本提交的运行级输出端口，仅本次 LLM 调用使用。
        """
        policy = await self._policy_port.resolve(request)
        run_id: str = request.run_id or uuid.uuid4().hex
        execution: RunExecution = RunExecution(
            run_id=run_id,
            request=request,
            policy=policy,
            state=AgentRunState(mode=Created()),
            budget=RunBudget(max_iterations=policy.max_iterations),
        )
        run: AgentRun = AgentRun(
            run_id=run_id,
            session_id=request.session_id,
            agent_id=request.agent_id,
            state=AgentRunState(mode=Running(RunStage.CALLING_LLM)),
            started_at=utc_now_iso(),
            policy=policy,
            input_message_id=request.user_message.message_id,
            parent_run_id=request.parent_run_id,
            root_run_id=request.root_run_id or request.parent_run_id,
        )
        await self._run_repository.create_run(run)
        self._cancellation_service.register(run_id, execution.cancellation)
        try:
            result: RunResult = await self._drive(execution, run, (), (), output_port=output_port)
            await self._release_run_context_if_terminal(result)
            return result
        finally:
            self._cancellation_service.unregister(run_id)

    async def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
        output_port: LLMOutputPort | None = None,
    ) -> RunResult:
        """消费审批记录，并在同一 run_id 上恢复等待中的执行。"""
        pending_record: ApprovalRecord | None = await self._approval_service.find_pending(approval_id)
        if pending_record is None:
            return RunResult("", AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.INVALID_STATE, "审批记录不存在或已消费"))
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
                AgentRunState(mode=Ended(RunOutcome.FAILED)),
                error=RunError(RunErrorCode.PERSISTENCE_FAILURE, "Run 缺少活动 Context Version，拒绝恢复审批"),
            )
        active_context_version: ContextVersion | None = next(
            (item for item in context_versions if item.version == run.active_context_version),
            None,
        )
        if active_context_version is None:
            return RunResult(
                pending_record.run_id,
                AgentRunState(mode=Ended(RunOutcome.FAILED)),
                error=RunError(RunErrorCode.PERSISTENCE_FAILURE, "Run 活动 Context Version 不存在"),
            )
        record: ApprovalRecord | None = await self._approval_service.consume(approval_id)
        if record is None:
            return RunResult("", AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.INVALID_STATE, "审批记录不存在或已消费"))
        run = await self._run_repository.load_run(record.session_id, record.run_id)
        checkpoint = await self._checkpoint_repository.load(record.session_id, record.run_id)
        if run is None or checkpoint is None:
            return RunResult(record.run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.INVALID_STATE, "审批恢复状态无效"))
        # 阶段3：审批恢复改走状态机校验，不再依赖已删除的旧状态枚举。
        state: AgentRunState = run.state
        if not (isinstance(state.mode, Suspended) and state.mode.reason is SuspendReason.APPROVAL):
            return RunResult(record.run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.INVALID_STATE, "审批恢复状态无效"))
        messages = await self._run_repository.load_messages(record.session_id, record.run_id)
        input_message = next((message for message in messages if message.message_id == run.input_message_id), None)
        if input_message is None:
            return RunResult(record.run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.PERSISTENCE_FAILURE, "缺少运行输入消息"))
        request: RunRequest = RunRequest(
            session_id=run.session_id,
            lease_id="approval-resume",
            agent_id=run.agent_id,
            user_message=ConversationMessage(input_message.message_id, MessageRole.USER, input_message.content, ""),
            conversation=_conversation_from_context_version(active_context_version),
        )
        approval_event = ApprovalGranted(approval_id) if approved else ApprovalRejected(approval_id)
        tr = transition(state, approval_event)
        execution: RunExecution = RunExecution(
            run_id=run.run_id,
            request=request,
            policy=run.policy,
            state=tr.state,
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
                state=AgentRunState(mode=Running(RunStage.CALLING_LLM)),
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
            result = await self._drive(
                execution,
                resumed_run,
                messages,
                pending_calls,
                event_sequence,
                output_port,
                resume_approved_call_id=pending_calls[0].call_id if pending_calls else None,
            )
            await self._release_run_context_if_terminal(result)
            return result
        finally:
            self._cancellation_service.unregister(run.run_id)

    async def resume_delegation(
        self,
        child_run_id: str,
        output_port: LLMOutputPort | None = None,
    ) -> RunResult:
        """查询子运行结果，回灌 delegation result 消息并经 DelegationCompleted 恢复父运行。

        无论子运行成功、失败、取消或放弃，均生成父运行的 delegation result 消息并回到
        ``AgentRunState(mode=Running(CALLING_LLM))``；child_run_id 不匹配经状态机拒绝并记录
        ``STATE_TRANSITION_REJECTED``，父运行保持不变。
        """
        child_run: AgentRun | None = await self._run_repository.find_run(child_run_id)
        if child_run is None or child_run.parent_run_id is None:
            return RunResult("", AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.INVALID_STATE, "子运行不存在或缺少父运行关联"))
        # 子运行由适配器创建在独立目标 Session，需经跨 Session 定位父运行取得其所属 Session。
        parent_run_id: str = child_run.parent_run_id
        parent_run: AgentRun | None = await self._run_repository.find_run(parent_run_id)
        if parent_run is None:
            return RunResult(parent_run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.INVALID_STATE, "父运行不存在"))
        session_id: str = parent_run.session_id
        checkpoint: RunCheckpoint | None = await self._checkpoint_repository.load(session_id, parent_run_id)
        if checkpoint is None:
            return RunResult(parent_run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.INVALID_STATE, "父运行或检查点不存在"))
        state: AgentRunState = parent_run.state
        if not (isinstance(state.mode, Suspended) and state.mode.reason is SuspendReason.DELEGATION):
            return RunResult(parent_run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.INVALID_STATE, "父运行不处于 delegation 挂起"))
        child_result = await self._delegation_port.result(child_run_id) if self._delegation_port is not None else None
        if child_result is None:
            # 子运行尚未结束：父运行继续挂起，不修改任何持久化状态。
            return RunResult(parent_run_id, AgentRunState(mode=Suspended(SuspendReason.DELEGATION, child_run_id, RunStage.CALLING_LLM)), child_run_id=child_run_id)
        messages = list(await self._run_repository.load_messages(session_id, parent_run_id))
        input_message = next((message for message in messages if message.message_id == parent_run.input_message_id), None)
        if input_message is None:
            return RunResult(parent_run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.PERSISTENCE_FAILURE, "缺少运行输入消息"))
        context_versions: tuple[ContextVersion, ...] = await self._run_repository.load_context_versions(session_id, parent_run_id)
        active_context_version: ContextVersion | None = next(
            (item for item in context_versions if item.version == parent_run.active_context_version),
            None,
        )
        if active_context_version is None:
            return RunResult(parent_run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.PERSISTENCE_FAILURE, "父运行活动 Context Version 不存在"))
        request: RunRequest = RunRequest(
            session_id=session_id,
            lease_id="delegation-resume",
            agent_id=parent_run.agent_id,
            user_message=ConversationMessage(input_message.message_id, MessageRole.USER, input_message.content, ""),
            conversation=_conversation_from_context_version(active_context_version),
        )
        execution: RunExecution = RunExecution(
            run_id=parent_run.run_id,
            request=request,
            policy=parent_run.policy,
            state=state,
            budget=RunBudget(max_iterations=parent_run.policy.max_iterations),
            message_cursor=checkpoint.message_sequence,
            run_messages=messages,
            active_context_version=active_context_version,
            staged_history_compressions=parent_run.staged_history_compressions,
            replay_active_context=True,
        )
        pending: JSONMap = checkpoint.pending if isinstance(checkpoint.pending, dict) else {}
        next_sequence: int = checkpoint.message_sequence + 1
        child_output: str = child_result.output or (
            child_result.error.message if child_result.error is not None else "delegation 未返回输出"
        )
        result_message: RunMessage = RunMessage(
            message_id=f"delegation-{parent_run.run_id}-{next_sequence}",
            sequence=next_sequence,
            kind=RunMessageKind.DELEGATION_RESULT,
            role=MessageRole.TOOL,
            content=child_output,
            tool_call_id=pending.get("source_tool_call_id") if isinstance(pending.get("source_tool_call_id"), str) else None,
            metadata={
                "child_run_id": child_run_id,
                "target_agent_id": pending.get("target_agent_id"),
                "target_session_id": pending.get("target_session_id"),
            },
        )
        messages.append(result_message)
        await self._save_messages(parent_run, execution, messages)
        event_sequence: int = checkpoint.event_sequence
        # 新状态机仅用 child_run_id 校验挂起控制标识；succeeded 供旧状态机兼容路径使用。
        completion_transition = await self._apply_transition(
            execution,
            parent_run,
            DelegationCompleted(child_run_id, child_result.outcome is RunOutcome.COMPLETED),
            event_sequence,
        )
        if completion_transition is None:
            # 非法迁移（child_run_id 不匹配等）：父运行不变，返回失败。
            return RunResult(parent_run.run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.INVALID_STATE, "状态机拒绝 DelegationCompleted 迁移"))
        execution.update_state(completion_transition.state, completion_transition.action)
        source_tool_call_id = pending.get("source_tool_call_id")
        if isinstance(source_tool_call_id, str):
            await self._tool_completed_event(
                parent_run,
                event_sequence,
                ToolCall(source_tool_call_id, "delegate", {}),
                result_message.message_id,
                ToolAuditStatus.FAILED if child_result.outcome is not RunOutcome.COMPLETED else ToolAuditStatus.COMPLETED,
                child_result.error.message if child_result.error is not None else "",
            )
            event_sequence += 1
        event_sequence = await self._event(
            parent_run,
            event_sequence,
            RunEventType.DELEGATION_COMPLETED,
            (result_message.message_id,),
            "delegation 子运行已完成",
            {
                "child_run_id": child_run_id,
                "outcome": child_result.outcome.value if child_result.outcome is not None else "",
            },
        )
        resumed_run: AgentRun = replace(parent_run, state=AgentRunState(mode=Running(RunStage.CALLING_LLM)), resume_count=parent_run.resume_count + 1)
        await self._run_repository.save_run(resumed_run)
        event_sequence = await self._event(resumed_run, event_sequence, RunEventType.RUN_RESUMED, (), "delegation 恢复运行")
        self._cancellation_service.register(parent_run.run_id, execution.cancellation)
        try:
            result = await self._drive(execution, resumed_run, messages, (), event_sequence, output_port)
            await self._release_run_context_if_terminal(result)
            return result
        finally:
            self._cancellation_service.unregister(parent_run.run_id)

    async def get_approval_session_id(self, approval_id: str) -> str | None:
        """返回待处理审批所属 Session，供协调器获取同一把租约锁。"""
        record = await self._approval_service.find_pending(approval_id)
        return record.session_id if record is not None else None

    async def get_delegation_session_id(self, child_run_id: str) -> str | None:
        """返回 delegation 父运行所属 Session，供协调器获取同一把租约锁。

        子运行由适配器创建在独立目标 Session，故需经父运行标识跨 Session 反查其父 Session。
        """
        child_run: AgentRun | None = await self._run_repository.find_run(child_run_id)
        if child_run is None or child_run.parent_run_id is None:
            return None
        parent_run: AgentRun | None = await self._run_repository.find_run(child_run.parent_run_id)
        return parent_run.session_id if parent_run is not None else None

    async def get_run_session_id(self, run_id: str) -> str | None:
        """返回运行所属 Session，供取消操作遵守单 Session 串行约束。"""
        run: AgentRun | None = await self._run_repository.find_run(run_id)
        return run.session_id if run is not None else None

    async def recover_session(self, session_id: str) -> None:
        """进程重启后的 Session 恢复入口。

        阶段 4 起不再将遗留 ``RUNNING`` 改写为伪状态：未结束 Run 保持其最后持久化的
        非终态（``Created`` / ``Running`` / ``Suspended``），占用判定与恢复均由
        ``active_run`` 与 ``resume_run`` / ``abandon_run`` 负责，普通新消息只返回
        ``SESSION_BUSY``，不会自动放弃旧 Run。
        """
        # 故意不改写领域状态：具体节点恢复继续委托 checkpoint/resume（见 resume_run）。

    async def active_run(self, session_id: str) -> AgentRun | None:
        """返回当前占用 Session 的唯一非终态 Run。"""
        runs: tuple[AgentRun, ...] = await self._run_repository.list_active_runs(session_id)
        if len(runs) > 1:
            raise RuntimeError("同一 Session 存在多个未终态 Run")
        return runs[0] if runs else None

    async def resume_run(
        self,
        run_id: str,
        output_port: LLMOutputPort | None = None,
    ) -> RunResult:
        """依据 checkpoint 和活动 Context Version 恢复未结束 Run（同一 run_id）。

        前置条件：Run 未 ``Ended`` 且存在有效的 LLM checkpoint；具体节点恢复继续委托
        checkpoint/resume，不会产生新的初始 Context Version。
        """
        run: AgentRun | None = await self._run_repository.find_run(run_id)
        if run is None or run.state.is_ended():
            return RunResult(run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.INVALID_STATE, "Run 不存在或已结束，无法恢复"))
        checkpoint: RunCheckpoint | None = await self._checkpoint_repository.load(run.session_id, run.run_id)
        if checkpoint is None or checkpoint.active_context_version is None or checkpoint.action is not AgentAction.INVOKE_LLM:
            return RunResult(run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.INVALID_STATE, "Run 缺少可恢复的 LLM checkpoint"))
        versions: tuple[ContextVersion, ...] = await self._run_repository.load_context_versions(run.session_id, run.run_id)
        version: ContextVersion | None = next((item for item in versions if item.version == checkpoint.active_context_version), None)
        if version is None:
            return RunResult(run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.PERSISTENCE_FAILURE, "checkpoint 引用的 Context Version 不存在"))
        messages: tuple[RunMessage, ...] = await self._run_repository.load_messages(run.session_id, run.run_id)
        input_message: RunMessage | None = next((item for item in messages if item.message_id == run.input_message_id), None)
        if input_message is None:
            return RunResult(run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.PERSISTENCE_FAILURE, "Run 缺少用户输入消息"))
        request: RunRequest = RunRequest(
            run.session_id,
            "resume-run",
            run.agent_id,
            ConversationMessage(input_message.message_id, MessageRole.USER, input_message.content, ""),
            _conversation_from_context_version(version),
            run_id=run.run_id,
        )
        execution: RunExecution = RunExecution(
            run.run_id,
            request,
            run.policy,
            run.state,
            RunBudget(run.policy.max_iterations),
            message_cursor=checkpoint.message_sequence,
            run_messages=messages,
            active_context_version=version,
            staged_history_compressions=run.staged_history_compressions,
            replay_active_context=True,
        )
        resumed: AgentRun = replace(run, state=AgentRunState(mode=Running(RunStage.CALLING_LLM)), resume_count=run.resume_count + 1)
        await self._run_repository.save_run(resumed)
        event_sequence: int = await self._event(resumed, checkpoint.event_sequence, RunEventType.RUN_RESUMED, (), "恢复未结束 Run")
        self._cancellation_service.register(run.run_id, execution.cancellation)
        try:
            result: RunResult = await self._drive(execution, resumed, messages, (), event_sequence, output_port)
            await self._release_run_context_if_terminal(result)
            return result
        finally:
            self._cancellation_service.unregister(run.run_id)

    async def abandon_run(self, run_id: str) -> RunResult:
        """显式放弃未结束 Run：经状态机收口为 AgentRunState(mode=Ended(ABANDONED)) 并删除检查点。"""
        run: AgentRun | None = await self._run_repository.find_run(run_id)
        if run is None or run.state.is_ended():
            return RunResult(run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.INVALID_STATE, "Run 不存在或已结束，无法放弃"))
        checkpoint: RunCheckpoint | None = await self._checkpoint_repository.load(run.session_id, run.run_id)
        event_sequence: int = checkpoint.event_sequence if checkpoint is not None else 0
        execution: RunExecution = RunExecution(
            run.run_id,
            RunRequest(
                run.session_id,
                "abandon",
                run.agent_id,
                ConversationMessage(run.input_message_id, MessageRole.USER, "", ""),
                ConversationSnapshot(run.session_id, (), 0),
            ),
            run.policy,
            run.state,
            RunBudget(run.policy.max_iterations),
        )
        abandon_transition = await self._apply_transition(execution, run, AbandonRequested(), event_sequence)
        if abandon_transition is None:
            return RunResult(run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)), error=RunError(RunErrorCode.INVALID_STATE, "放弃被状态机拒绝"))
        abandoned: AgentRun = replace(
            run,
            state=AgentRunState(mode=Ended(RunOutcome.ABANDONED)),
            ended_at=utc_now_iso(),
            error=RunError(RunErrorCode.CANCELLED, "已被显式放弃"),
        )
        await self._run_repository.save_run(abandoned)
        await self._checkpoint_repository.delete(run.session_id, run.run_id)
        await self._event(abandoned, event_sequence, RunEventType.RUN_ABANDONED, ())
        result: RunResult = RunResult(run.run_id, AgentRunState(mode=Ended(RunOutcome.ABANDONED)), error=abandoned.error)
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
        # 阶段3：等待审批或等待 delegation 子运行的挂起 Run 取消时立即收口为取消终态；
        # 子运行取消已由前面 _delegation_port.cancel 经取消服务传播。
        if run is None or not (
            isinstance(run.state.mode, Suspended)
            and run.state.mode.reason in (SuspendReason.APPROVAL, SuspendReason.DELEGATION)
        ):
            return
        messages = await self._run_repository.load_messages(run.session_id, run.run_id)
        checkpoint = await self._checkpoint_repository.load(run.session_id, run.run_id)
        event_sequence: int = checkpoint.event_sequence if checkpoint is not None else 0
        execution = RunExecution(run.run_id, RunRequest(run.session_id, "cancel", run.agent_id, ConversationMessage(run.input_message_id, MessageRole.USER, "", ""), ConversationSnapshot(run.session_id, (), 0)), run.policy, AgentRunState(mode=Created()), RunBudget(run.policy.max_iterations))
        result: RunResult = await self._finish_cancelled(execution, run, messages, event_sequence, reason)
        await self._release_run_context_if_terminal(result)

    async def _release_run_context_if_terminal(self, result: RunResult) -> None:
        """仅在 Run 终态释放 Run Owner 的私有 Slot 实例，并清理工具端口的进程内缓存。"""
        if result.state.is_ended():
            await self._context_port.release_scope(ContextOwner.RUN, result.run_id)
            # 可选能力：工具端口若缓存了本 Run 的恢复状态，终态时清理，避免 Adapter 内存累积。
            clear_run = getattr(self._tool_port, "clear_run", None)
            if clear_run is not None:
                await clear_run(result.run_id)

    async def _drive(self, execution: RunExecution, run: AgentRun, initial_messages: tuple[RunMessage, ...], pending_calls: tuple[ToolCall, ...], event_sequence: int = 0, output_port: LLMOutputPort | None = None, resume_approved_call_id: str | None = None) -> RunResult:
        """驱动局部状态机，并在每个事实边界按顺序持久化。

        主循环消费 ``AgentRunState`` 当前分支对应的动作：``EXECUTING_TOOLS`` 走工具动作、
        ``CALLING_LLM`` 走模型动作。任一动作的副作用完成后回灌为 ``AgentRunEvent``，
        经 ``transition()`` 计算下一状态与动作，再进入下一轮。
        """
        messages: list[RunMessage] = list(initial_messages)
        execution.replace_run_messages(tuple(messages))
        sequence: int = len(messages)
        event_number: int = event_sequence
        if not messages:
            sequence += 1
            messages.append(RunMessage(execution.request.user_message.message_id, sequence, RunMessageKind.USER_INPUT, MessageRole.USER, execution.request.user_message.content))
            await self._save_messages(run, execution, messages)
            start_transition = await self._apply_transition(execution, run, RunStarted(run.input_message_id), event_number)
            if start_transition is None:
                return await self._fail(execution, run, tuple(messages), event_number, "状态机拒绝 RunStarted 迁移")
            execution.update_state(start_transition.state, start_transition.action)
            event_number = await self._event(run, event_number, RunEventType.RUN_STARTED, (run.input_message_id,))
        while not execution.state.is_ended():
            if execution.cancellation.cancelled:
                cancel_transition = await self._apply_transition(execution, run, CancelRequested(), event_number)
                if cancel_transition is not None:
                    execution.update_state(cancel_transition.state, cancel_transition.action)
                return await self._finish_cancelled(execution, run, tuple(messages), event_number, execution.cancellation.reason)
            if isinstance(execution.state.mode, Running) and execution.state.mode.stage is RunStage.EXECUTING_TOOLS:
                run, sequence, event_number, pending_calls, terminal = await self._execute_tools_action(
                    execution, run, messages, sequence, event_number, pending_calls, resume_approved_call_id,
                )
                if terminal is not None:
                    return terminal
                continue
            run, sequence, event_number, pending_calls, terminal = await self._invoke_llm_action(
                execution, run, messages, sequence, event_number, output_port,
            )
            if terminal is not None:
                return terminal
        return await self._fail(execution, run, tuple(messages), event_number, "状态机意外结束")

    async def _execute_tools_action(
        self,
        execution: RunExecution,
        run: AgentRun,
        messages: list[RunMessage],
        sequence: int,
        event_number: int,
        pending_calls: tuple[ToolCall, ...],
        resume_approved_call_id: str | None,
    ) -> tuple[AgentRun, int, int, tuple[ToolCall, ...], RunResult | None]:
        """执行 ``EXECUTING_TOOLS`` 动作：逐个工具调用，回灌 ToolBatchCompleted / ToolApprovalRequired。

        返回 (run, sequence, event_number, pending_calls, terminal)：``terminal`` 非 None 表示已
        收口（审批挂起或失败），主循环直接返回；否则主循环以空 pending_calls 继续下一轮。
        """
        tool_calls: tuple[ToolCall, ...] = pending_calls
        pending_calls = ()
        if not tool_calls:
            return run, sequence, event_number, pending_calls, await self._fail(execution, run, tuple(messages), event_number, "缺少待执行工具调用")
        completed_message_ids: list[str] = []
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
                submitted = await self._submit_delegation_action(
                    execution,
                    run,
                    messages,
                    sequence,
                    event_number,
                    delegation_request,
                    tool_call,
                    tool_calls,
                    tool_index,
                )
                # 提交后父 Run 持久化为 AgentRunState(mode=Suspended(DELEGATION)) 并返回，交由外部 resume_delegation 恢复。
                return run, sequence, event_number, pending_calls, submitted
            event_number = await self._tool_started_event(run, event_number, messages, tool_call)
            try:
                approved: bool = (
                    resume_approved_call_id is not None
                    and tool_call.call_id == resume_approved_call_id
                )
                tool_result: ToolResult = await self._tool_port.execute(
                    ToolInvocation(execution.run_id, tool_call, approved=approved),
                    execution.view(),
                )
            except Exception as error:
                event_number = await self._tool_completed_event(run, event_number, tool_call, None, ToolAuditStatus.FAILED, _safe_error_summary(error))
                return run, sequence, event_number, pending_calls, await self._fail(execution, run, tuple(messages), event_number, f"工具调用失败：{error}", RunErrorCode.TOOL_FAILURE)
            if execution.cancellation.cancelled:
                event_number = await self._tool_completed_event(
                    run,
                    event_number,
                    tool_call,
                    None,
                    ToolAuditStatus.CANCELLED,
                    "工具执行后已取消",
                )
                return run, sequence, event_number, pending_calls, await self._finish_cancelled(
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
            event_number = await self._tool_completed_event(run, event_number, tool_call, tool_message.message_id, _tool_audit_status(tool_result.status), _tool_error_summary(tool_result))
            if tool_result.status is ToolResultStatus.FAILED:
                error = tool_result.error.message if tool_result.error is not None else tool_result.output or "工具执行失败"
                return run, sequence, event_number, pending_calls, await self._fail(execution, run, tuple(messages), event_number, error, RunErrorCode.TOOL_FAILURE)
            if tool_result.status is ToolResultStatus.APPROVAL_REQUIRED:
                return run, sequence, event_number, pending_calls, await self._suspend_action(
                    execution, run, messages, sequence, event_number, tool_call, tool_calls, tool_index, tool_result,
                )
            completed_message_ids.append(tool_message.message_id)
        batch_transition = await self._apply_transition(execution, run, ToolBatchCompleted(tuple(completed_message_ids)), event_number)
        if batch_transition is None:
            return run, sequence, event_number, pending_calls, await self._fail(execution, run, tuple(messages), event_number, "状态机拒绝 ToolBatchCompleted 迁移")
        execution.update_state(batch_transition.state, batch_transition.action)
        return run, sequence, event_number, pending_calls, None

    async def _suspend_action(
        self,
        execution: RunExecution,
        run: AgentRun,
        messages: list[RunMessage],
        sequence: int,
        event_number: int,
        tool_call: ToolCall,
        tool_calls: tuple[ToolCall, ...],
        tool_index: int,
        tool_result: ToolResult,
    ) -> RunResult:
        """执行审批挂起动作：经 ToolApprovalRequired 迁移到 AgentRunState(mode=Suspended(APPROVAL)) 并持久化检查点。"""
        record = await self._approval_service.create(run.run_id, run.session_id, tool_result.approval_id)
        suspend_transition = await self._apply_transition(execution, run, ToolApprovalRequired(record.approval_id), event_number)
        if suspend_transition is None:
            return await self._fail(execution, run, tuple(messages), event_number, "状态机拒绝 ToolApprovalRequired 迁移")
        execution.update_state(suspend_transition.state, suspend_transition.action)
        remaining_calls: tuple[ToolCall, ...] = tool_calls[tool_index:]
        checkpoint = RunCheckpoint(
            checkpoint_id=f"checkpoint-{run.run_id}",
            run_id=run.run_id,
            session_id=run.session_id,
            checkpoint_sequence=1,
            event_sequence=event_number + 1,
            message_sequence=sequence,
            action=suspend_transition.action,
            pending={
                "approval_id": record.approval_id,
                "tool_calls": [call.to_dict() for call in remaining_calls],
            },
            budget=execution.budget.to_dict(),
            active_context_version=(
                execution.active_context_version.version
                if execution.active_context_version is not None else None
            ),
        )
        await self._checkpoint_repository.save(checkpoint)
        waiting_run: AgentRun = replace(
            run,
            state=AgentRunState(mode=Suspended(SuspendReason.APPROVAL, record.approval_id, RunStage.EXECUTING_TOOLS)),
            latest_checkpoint_id=checkpoint.checkpoint_id,
        )
        await self._run_repository.save_run(waiting_run)
        event_number = await self._event(run, event_number, RunEventType.WAITING_APPROVAL, (messages[-1].message_id,))
        return RunResult(run.run_id, AgentRunState(mode=Suspended(SuspendReason.APPROVAL, record.approval_id, RunStage.EXECUTING_TOOLS)), approval_id=record.approval_id)

    async def _invoke_llm_action(
        self,
        execution: RunExecution,
        run: AgentRun,
        messages: list[RunMessage],
        sequence: int,
        event_number: int,
        output_port: LLMOutputPort | None,
    ) -> tuple[AgentRun, int, int, tuple[ToolCall, ...], RunResult | None]:
        """执行 ``CALLING_LLM`` 动作：构建上下文、调用模型、回灌 LLMResponseProduced。

        返回 (run, sequence, event_number, pending_calls, terminal)：终态（收尾 / 失败 / 取消 /
        中断）时 ``terminal`` 非 None；否则 ``pending_calls`` 为模型请求的工具调用，主循环继续。
        """
        try:
            prepared: _PreparedContext = await self._prepare_context(execution, run, messages)
        except HistoryCompactorUnavailable as error:
            await self._checkpoint_repository.save(
                _compaction_checkpoint(run, execution, event_number, sequence),
            )
            return run, sequence, event_number, (), await self._suspend_on_unavailable(execution, run, tuple(messages), event_number, str(error))
        except ContextBudgetRejected as error:
            return run, sequence, event_number, (), await self._fail(execution, run, tuple(messages), event_number, str(error), error.code)
        except Exception as error:
            return run, sequence, event_number, (), await self._fail(execution, run, tuple(messages), event_number, f"模型上下文构建失败：{error}")
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
            response = await self._llm_port.complete(context, execution.view(), output_port)
        except LLMUnavailableError as error:
            return run, sequence, event_number, (), await self._suspend_on_unavailable(execution, run, tuple(messages), event_number, str(error))
        except Exception as error:
            return run, sequence, event_number, (), await self._fail(execution, run, tuple(messages), event_number, f"模型调用失败：{error}", RunErrorCode.LLM_FAILURE)
        if response.metadata.get("has_streamed_response") is True:
            execution.mark_response_streamed()
        if execution.cancellation.cancelled:
            return run, sequence, event_number, (), await self._finish_cancelled(
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
        response_transition = await self._apply_transition(execution, run, LLMResponseProduced(final, response_message.message_id, len(response.tool_calls)), event_number)
        if response_transition is None:
            return run, sequence, event_number, (), await self._fail(execution, run, tuple(messages), event_number, "状态机拒绝 LLMResponseProduced 迁移")
        execution.update_state(response_transition.state, response_transition.action)
        if final:
            return run, sequence, event_number, (), await self._finalize_action(execution, run, response_message, event_number)
        return run, sequence, event_number, response.tool_calls, None

    async def _finalize_action(
        self,
        execution: RunExecution,
        run: AgentRun,
        response_message: RunMessage,
        event_number: int,
    ) -> RunResult:
        """执行 FINALIZE 动作：状态机已到 AgentRunState(mode=Ended(COMPLETED))，经 SuccessCommitIntent 收口对话与 Run。"""
        completed = replace(run, state=AgentRunState(mode=Ended(RunOutcome.COMPLETED)), ended_at=utc_now_iso(), final_message_id=response_message.message_id)
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
            target_outcome=RunOutcome.COMPLETED,
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
            AgentRunState(mode=Ended(RunOutcome.COMPLETED)),
            ConversationMessage(response_message.message_id, MessageRole.ASSISTANT, response_message.content, completed.ended_at or ""),
            has_streamed_response=execution.has_streamed_response,
        )

    async def _apply_transition(
        self,
        execution: RunExecution,
        run: AgentRun,
        event: "AgentRunEvent",
        event_sequence: int,
    ) -> "StateTransition | None":
        """调用纯状态机；若迁移非法则在 Application 边界记录审计并返回 None。

        调用方应在返回 None 时收口为失败，并据此判断 ``execution.state`` 是否已被更新。
        """
        try:
            return transition(execution.state, event)
        except InvalidTransition as error:
            logger.error(
                "AgentRun 状态机拒绝迁移：mode=%s event=%s reason=%s run_id=%s",
                error.current_mode, error.event_type, error.reason, run.run_id,
            )
            # 拒绝迁移不产生新消息，故 message_ids 为空；审计信息写入 data，避免引用未保存消息。
            await self._event(
                run, event_sequence, RunEventType.STATE_TRANSITION_REJECTED, (),
                summary=f"拒绝迁移：{error.event_type} / {error.reason}",
                data={"event_type": error.event_type, "reason": error.reason, "current_mode": error.current_mode},
            )
            return None

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

    async def _submit_delegation_action(
        self,
        execution: RunExecution,
        run: AgentRun,
        messages: list[RunMessage],
        sequence: int,
        event_sequence: int,
        request: DelegationRequest,
        tool_call: ToolCall,
        tool_calls: tuple[ToolCall, ...],
        tool_index: int,
    ) -> RunResult:
        """执行 delegation 提交动作：经 DelegationSubmitted 迁移到 AgentRunState(mode=Suspended(DELEGATION)) 并持久化检查点后返回。

        不再内联等待子运行结果；子运行结果由独立的 ``resume_delegation`` 恢复入口回灌。
        提交成功后将子运行注册到取消服务，使挂起期间的父运行取消能传播到子运行。
        """
        if self._delegation_port is None:
            event_number = await self._tool_completed_event(run, event_sequence, tool_call, None, ToolAuditStatus.FAILED, "未装配 DelegationPort")
            return await self._fail(execution, run, tuple(messages), event_number, "当前 Runtime 未装配 DelegationPort", RunErrorCode.INVALID_STATE)
        try:
            submission: DelegationSubmission = await self._delegation_port.submit(request)
            child_run_id: str = submission.child_run_id
            self._cancellation_service.register_delegated_run(execution.run_id, child_run_id)
            submit_transition = await self._apply_transition(execution, run, DelegationSubmitted(child_run_id), event_sequence)
            if submit_transition is None:
                return await self._fail(execution, run, tuple(messages), event_sequence, "状态机拒绝 DelegationSubmitted 迁移")
            execution.update_state(submit_transition.state, submit_transition.action)
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
            if execution.cancellation.cancelled:
                return await self._finish_cancelled(execution, run, tuple(messages), next_event_sequence, execution.cancellation.reason)
            remaining_calls: tuple[ToolCall, ...] = tool_calls[tool_index + 1:]
            checkpoint = RunCheckpoint(
                checkpoint_id=f"checkpoint-{run.run_id}",
                run_id=run.run_id,
                session_id=run.session_id,
                checkpoint_sequence=1,
                event_sequence=next_event_sequence,
                message_sequence=sequence,
                action=submit_transition.action,
                pending={
                    "child_run_id": child_run_id,
                    "source_tool_call_id": tool_call.call_id,
                    "target_agent_id": request.target_agent_id,
                    "target_session_id": submission.target_session_id,
                    "tool_calls": [call.to_dict() for call in remaining_calls],
                },
                budget=execution.budget.to_dict(),
                active_context_version=(
                    execution.active_context_version.version
                    if execution.active_context_version is not None else None
                ),
            )
            await self._checkpoint_repository.save(checkpoint)
            suspended_run: AgentRun = replace(
                run,
                state=AgentRunState(mode=Suspended(SuspendReason.DELEGATION, child_run_id, RunStage.CALLING_LLM)),
                latest_checkpoint_id=checkpoint.checkpoint_id,
            )
            await self._run_repository.save_run(suspended_run)
            return RunResult(run.run_id, AgentRunState(mode=Suspended(SuspendReason.DELEGATION, child_run_id, RunStage.CALLING_LLM)), child_run_id=child_run_id)
        except Exception as error:
            event_number = await self._tool_completed_event(run, event_sequence, tool_call, None, ToolAuditStatus.FAILED, _safe_error_summary(error))
            if 'child_run_id' in locals():
                self._cancellation_service.clear_delegated_run(execution.run_id, child_run_id)
            return await self._fail(execution, run, tuple(messages), event_number, f"delegation 提交失败：{error}", RunErrorCode.TOOL_FAILURE)

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
        return await self._event(run, event_sequence, RunEventType.TOOL_STARTED, ids, "工具调用开始", {"source_response_message_id": source_id, "call_id": tool_call.call_id, "tool_name": tool_call.name, "status": ToolAuditStatus.STARTED.value})

    async def _tool_completed_event(self, run: AgentRun, event_sequence: int, tool_call: ToolCall, result_message_id: str | None, status: ToolAuditStatus, error_summary: str = "") -> int:
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
        cancelled = replace(run, state=AgentRunState(mode=Ended(RunOutcome.CANCELLED)), ended_at=utc_now_iso(), error=RunError(RunErrorCode.CANCELLED, reason))
        await self._run_repository.save_run(cancelled)
        await self._checkpoint_repository.delete(run.session_id, run.run_id)
        await self._event(run, event_sequence, RunEventType.RUN_CANCELLED, ())
        return RunResult(run.run_id, AgentRunState(mode=Ended(RunOutcome.CANCELLED)), error=cancelled.error)

    async def _suspend_on_unavailable(
        self,
        execution: RunExecution,
        run: AgentRun,
        messages: tuple[RunMessage, ...],
        event_sequence: int,
        reason: str,
    ) -> RunResult:
        """外部服务瞬时不可用：保留最后一次持久化状态与检查点，不做终态收口、不产生中断事件。

        进程重启、临时外部不可用不使 Run 进入 ``Ended``；Run 保持进入本次驱动前持久化的
        非终态（通常为 ``Running``），后续由 ``resume_run`` 基于同一 ``run_id`` 与既有
        checkpoint 继续。故不再映射为独立的伪中断状态。
        """
        error: RunError = RunError(RunErrorCode.LLM_FAILURE, reason, retryable=True)
        # 不修改 run.state：保留既有非终态与检查点，使 Run 可被 resume_run 恢复。
        return RunResult(run.run_id, run.state, error=error)

    async def _fail(
        self,
        execution: RunExecution,
        run: AgentRun,
        messages: tuple[RunMessage, ...],
        event_sequence: int,
        message: str,
        code: RunErrorCode = RunErrorCode.INVALID_STATE,
    ) -> RunResult:
        """持久化失败终态且不投影 Conversation；先经状态机收口为 AgentRunState(mode=Ended(FAILED))。"""
        failure_event: "AgentRunEvent" = (
            ToolBatchFailed() if (isinstance(execution.state.mode, Running) and execution.state.mode.stage is RunStage.EXECUTING_TOOLS) else LLMCallFailed()
        )
        fail_transition = await self._apply_transition(execution, run, failure_event, event_sequence)
        if fail_transition is not None:
            execution.update_state(fail_transition.state, fail_transition.action)
        error = RunError(code, message)
        failed = replace(run, state=AgentRunState(mode=Ended(RunOutcome.FAILED)), ended_at=utc_now_iso(), error=error)
        await self._run_repository.save_run(failed)
        await self._checkpoint_repository.delete(run.session_id, run.run_id)
        await self._event(run, event_sequence, RunEventType.RUN_FAILED, ())
        return RunResult(run.run_id, AgentRunState(mode=Ended(RunOutcome.FAILED)), error=error)


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
        action=AgentAction.INVOKE_LLM,
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
        action=AgentAction.INVOKE_LLM,
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


def _tool_audit_status(status: ToolResultStatus) -> ToolAuditStatus:
    """将 ToolPort 结果类别映射为不混淆 Run 取消语义的审计终态。"""
    if status is ToolResultStatus.COMPLETED:
        return ToolAuditStatus.COMPLETED
    if status is ToolResultStatus.APPROVAL_REQUIRED:
        return ToolAuditStatus.APPROVAL_REQUIRED
    return ToolAuditStatus.FAILED


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
