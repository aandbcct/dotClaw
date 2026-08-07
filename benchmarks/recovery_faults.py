"""PR4 固定故障替身与冷重建装配。

本模块仅在隔离存储根使用记录型 LLM（大语言模型替身）和工具替身，不向生产
Runtime 注册故障钩子。每次恢复都会重新装配 RuntimeEngine（运行时驱动器）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from dotclaw.runtime.adapters import ApprovalRepositoryAdapter, CheckpointRepositoryAdapter, RunRepositoryAdapter, SessionConversationProjector
from dotclaw.runtime.application.approval_service import ApprovalService
from dotclaw.runtime.application.cancellation_service import CancellationService
from dotclaw.runtime.application.dto import ContextBundle, ContextMetadata, ConversationMessage, ConversationSnapshot, RunMessage, RunRequest, ToolInvocation, ToolResult, ToolResultStatus
from dotclaw.runtime.application.engine import RuntimeEngine
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.context_budget import TokenCountRequest, TokenCountResult
from dotclaw.runtime.application.history_compaction import HistoryCompactionRequest, HistoryCompactionResult
from dotclaw.runtime.application.ports import ContextPort, HistoryCompactorPort, LLMPort, SuccessCommitFaultPort, ToolPort
from dotclaw.runtime.domain.context import SuccessCommitFaultPoint, SuccessCommitIntent
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, AgentRun, MessageRole, RunCheckpoint, RunMessageKind, ToolCall
from dotclaw.runtime.domain.control import AgentAction
from dotclaw.runtime.domain.state import AgentRunState, Ended, RunOutcome, RunStage, Running
from dotclaw.session.session import SessionManager
from dataclasses import replace


class RecoveryInterrupted(BaseException):
    """模拟异常终止；故意不继承 Exception，避免 Runtime 将其转换为业务失败。"""


class RecoveryTokenCounter:
    """返回稳定小 token 数的 TokenCounterPort（令牌计数端口）替身。"""

    async def count(self, request: TokenCountRequest) -> TokenCountResult:
        return TokenCountResult(1)


class RecoveryHistoryCompactor(HistoryCompactorPort):
    """恢复场景不应进入历史压缩分支，进入即表示场景前提失效。"""

    async def compact_history(self, request: HistoryCompactionRequest) -> HistoryCompactionResult:
        raise AssertionError("恢复基准不应调用历史压缩器")


class RecoveryContext(ContextPort):
    """提供固定上下文，确保冷重建不会重新生成初始 ContextVersion（上下文版本）。"""

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        return ContextBundle((RunMessage("recovery-system", 1, RunMessageKind.LLM_REQUEST, MessageRole.SYSTEM, "recovery"),), (), ContextMetadata(estimated_tokens=1))

    async def release_scope(self, owner, owner_key) -> None:
        """替身不维护 Scope（作用域）缓存。"""

    async def release_all(self) -> None:
        """替身不维护资源。"""

    def request_refresh(self, slot_id: str, owner, owner_key) -> None:
        """替身不支持刷新。"""

    def publish_signal(self, signal) -> None:
        """替身不消费信号。"""


class RecoveryPolicy:
    """返回固定冻结策略的 PolicyPort（策略端口）替身。"""

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        return AgentPolicySnapshot(request.agent_id, "recovery-policy", "recovery-model", 8, policy_data={"context_window": 128, "tokenizer_encoding": "cl100k_base"})


@dataclass
class EffectLog:
    """跨重建保存的记录型副作用计数。"""

    root: Path

    @property
    def path(self) -> Path:
        return self.root / "recovery-effect-log.json"

    def append(self, kind: str) -> None:
        entries = self.read()
        entries.append(kind)
        self.path.write_text(json.dumps(entries), encoding="utf-8")

    def read(self) -> list[str]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            raise ValueError("记录型副作用日志格式损坏")
        return data

    def count(self, kind: str) -> int:
        return self.read().count(kind)


class ScriptedLLM(LLMPort):
    """按阶段执行的记录型 LLM 替身，可在发送前或响应未知点中断。"""

    def __init__(self, effects: EffectLog, mode: str, *, resume: bool) -> None:
        self._effects = effects
        self._mode = mode
        self._resume = resume

    async def complete(self, context, execution: RunExecutionView, output_port=None) -> RunMessage:
        if not self._resume and self._mode == "llm_before_send_failure":
            raise RecoveryInterrupted("llm_before_send")
        self._effects.append("llm_request")
        if not self._resume and self._mode == "llm_response_unknown":
            raise RecoveryInterrupted("llm_response_unknown")
        if self._mode.startswith("tool_") or self._mode == "approval_cold_rebuild":
            if not self._resume:
                return RunMessage("tool-request", 2, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "tool", tool_calls=(ToolCall("call-1", "record", {}),))
        return RunMessage("final", 3, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, "done")

    async def cancel(self, run_id: str) -> None:
        """替身没有远程请求。"""


class ScriptedTool(ToolPort):
    """记录工具副作用并在指定边界中断；审批模式首次只请求审批。"""

    def __init__(self, effects: EffectLog, mode: str, *, resume: bool) -> None:
        self._effects = effects
        self._mode = mode
        self._resume = resume

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        if self._mode == "approval_cold_rebuild" and not self._resume:
            return ToolResult(invocation.call.call_id, ToolResultStatus.APPROVAL_REQUIRED, approval_id="recovery-approval")
        if self._mode == "tool_before_effect" and not self._resume:
            raise RecoveryInterrupted("tool_before_effect")
        self._effects.append("tool_effect")
        if self._mode == "tool_after_effect" and not self._resume:
            raise RecoveryInterrupted("tool_after_effect")
        return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED, output="ok")

    async def cancel(self, run_id: str) -> None:
        """替身没有远程请求。"""


def make_engine(root: Path, mode: str, *, resume: bool) -> RuntimeEngine:
    """从同一存储根新装配服务对象，作为 PR4 冷重建唯一入口。"""
    effects = EffectLog(root)
    return RuntimeEngine(
        RunRepositoryAdapter(root, SessionConversationProjector(SessionManager(root))), CheckpointRepositoryAdapter(root), RecoveryContext(),
        ScriptedLLM(effects, mode, resume=resume), ScriptedTool(effects, mode, resume=resume),
        RecoveryPolicy(), ApprovalService(ApprovalRepositoryAdapter(root)), CancellationService(),
        token_counter=RecoveryTokenCounter(), history_compactor=RecoveryHistoryCompactor(),
    )


def make_request(session_id: str) -> RunRequest:
    """构造所有恢复场景共用的固定脚本请求。"""
    user = ConversationMessage("recovery-user", MessageRole.USER, "恢复测试", "2026-08-07T00:00:00+00:00")
    return RunRequest(session_id, "recovery-lease", "recovery-agent", user, ConversationSnapshot(session_id, (), 0))


@dataclass(frozen=True)
class FaultRun:
    """受控异常后的持久化恢复输入。"""

    run_id: str
    session_id: str
    mode: str


async def interrupt_initial_run(root: Path, mode: str, session_id: str) -> FaultRun:
    """运行至固定中断点并返回同一 Run 的恢复定位信息。"""
    manager = SessionManager(root)
    session = await manager.create(agent_id="recovery-agent")
    # Benchmark 需要固定 Session ID；创建器产生 UUID 后仅用于持久化投影，
    # 因此这里改用其真实 ID 作为请求键，调用方通过 FaultRun 回读。
    session_id = session.id
    engine = make_engine(root, mode, resume=False)
    try:
        result = await engine.execute(make_request(session_id))
    except RecoveryInterrupted:
        repository = RunRepositoryAdapter(root)
        active = await repository.list_active_runs(session_id)
        if len(active) != 1:
            raise AssertionError("故障后必须留下唯一可恢复 Run")
        return FaultRun(active[0].run_id, session_id, mode)
    raise AssertionError(f"故障场景未触发中断，得到 {result.state.outcome()}")


async def start_waiting_approval(root: Path, session_id: str) -> FaultRun:
    """让初始服务进入审批挂起；调用方随后必须丢弃它并冷重建。"""
    manager = SessionManager(root)
    session = await manager.create(agent_id="recovery-agent")
    session_id = session.id
    engine = make_engine(root, "approval_cold_rebuild", resume=False)
    result = await engine.execute(make_request(session_id))
    if not result.state.is_waiting_approval() or result.approval_id != "recovery-approval":
        raise AssertionError("审批场景未进入预期挂起状态")
    return FaultRun(result.run_id, session_id, "approval_cold_rebuild")


class SuccessCommitInterrupted(RuntimeError):
    """成功提交持久化边界的受控异常。"""


class SuccessCommitFaultInjector(SuccessCommitFaultPort):
    """复用生产接口的测试专用故障注入，不向 Runtime 暴露新 Port。"""

    def __init__(self, point: SuccessCommitFaultPoint) -> None:
        self.point = point
        self.enabled = True

    async def inject(self, point: SuccessCommitFaultPoint) -> None:
        if self.enabled and point is self.point:
            raise SuccessCommitInterrupted(point.value)


async def prepare_success_commit_fault(root: Path, point: SuccessCommitFaultPoint) -> tuple[str, str, SuccessCommitFaultInjector]:
    """建立待成功提交 Run 并在指定六个持久化边界中断。"""
    session = await SessionManager(root).create(agent_id="recovery-agent")
    injector = SuccessCommitFaultInjector(point)
    repository = RunRepositoryAdapter(root, SessionConversationProjector(SessionManager(root)), injector)
    checkpoint_repository = CheckpointRepositoryAdapter(root)
    running = AgentRun("success-run", session.id, "recovery-agent", AgentRunState(mode=Running(RunStage.CALLING_LLM)), "", AgentPolicySnapshot("recovery-agent", "recovery-policy", "recovery-model", 4), "success-input")
    final = RunMessage("success-final", 2, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, "done")
    completed = replace(running, state=AgentRunState(mode=Ended(RunOutcome.COMPLETED)), final_message_id=final.message_id)
    event = RunEvent(completed.run_id, 1, RunEventType.RUN_COMPLETED, "2026-08-07T00:00:00+00:00", (final.message_id,))
    intent = SuccessCommitIntent("success-conversation", None, RunOutcome.COMPLETED, completed.run_id, completed.session_id)
    checkpoint = RunCheckpoint("success-checkpoint", completed.run_id, completed.session_id, 1, 0, 2, {"phase": "finalizing"}, {}, action=AgentAction.INVOKE_LLM)
    await repository.create_run(running)
    await repository.save_messages(session.id, running.run_id, (RunMessage("success-input", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "input"), final))
    await checkpoint_repository.save(checkpoint)
    try:
        await repository.commit_success(completed, final, event, intent)
    except SuccessCommitInterrupted:
        return session.id, completed.run_id, injector
    raise AssertionError("成功提交故障注入未触发")


async def recover_success_commit(root: Path, injector: SuccessCommitFaultInjector) -> None:
    """用新 Repository（仓储）对象重复恢复两次，验证幂等收敛。"""
    injector.enabled = False
    repository = RunRepositoryAdapter(root, SessionConversationProjector(SessionManager(root)), injector)
    await repository.recover_pending_success_commits()
    await repository.recover_pending_success_commits()
