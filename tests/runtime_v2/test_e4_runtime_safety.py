"""E4 LLM_STARTED 安全点、动态压缩与可恢复中断集成测试。"""

from __future__ import annotations

from pathlib import Path

from dotclaw.runtime.adapters import ApprovalRepositoryAdapter, CheckpointRepositoryAdapter, InMemoryRunRepository
from dotclaw.runtime.application.approval_service import ApprovalService
from dotclaw.runtime.application.cancellation_service import CancellationService
from dotclaw.runtime.application.context_budget import TokenCountErrorCode, TokenCountRequest, TokenCountResult
from dotclaw.runtime.application.dto import ContextBundle, ContextMetadata, ContextRefreshSignal, ConversationMessage, ConversationSnapshot, RunRequest, RunResult, ToolInvocation, ToolResult, ToolResultStatus
from dotclaw.runtime.application.engine import RuntimeEngine
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.history_compaction import HistoryCompactionRequest, HistoryCompactionResult, HistoryCompactorUnavailable
from dotclaw.runtime.application.ports import ContextPort, HistoryCompactorPort, LLMPort, LLMUnavailableError, RunPolicyPort, ToolPort
from dotclaw.runtime.application.session_run_coordinator import SessionRunCoordinator
from dotclaw.runtime.domain.control import AgentAction
from dotclaw.runtime.domain.context import ContextOwner, ContextVersion
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, AgentRun, HistoryCompressionSnapshot, MessageRole, RunCheckpoint, RunError, RunErrorCode, RunMessage, RunMessageKind, RunStatus, ToolCall
from dotclaw.agent.agent import _display_result


class BudgetContext(ContextPort):
    """提供稳定 system 内容的 E4 ContextPort Fake。"""

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        """构造让 Engine 从 Request 读取历史的最小真实输入。"""
        message = RunMessage("system", 1, RunMessageKind.LLM_REQUEST, MessageRole.SYSTEM, "系统规则")
        return ContextBundle((message,), (), ContextMetadata(1))

    async def release_scope(self, owner: ContextOwner, owner_key: str) -> None:
        """测试 Port 不保存 Slot 实例。"""

    async def release_all(self) -> None:
        """测试 Port 不保存 Slot 实例。"""

    def request_refresh(self, slot_id: str, owner: ContextOwner, owner_key: str) -> None:
        """测试 ContextPort 不维护可刷新的 Slot 缓存。"""

    def publish_signal(self, signal: ContextRefreshSignal) -> None:
        """测试 ContextPort 不订阅外部刷新信号。"""


class BudgetPolicy(RunPolicyPort):
    """返回 E4 所需窗口和显式编码。"""

    def __init__(self, context_window: int = 50) -> None:
        """保存测试需要的冻结窗口值。"""
        self._context_window: int = context_window

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        """冻结固定模型预算策略。"""
        return AgentPolicySnapshot(
            request.agent_id,
            "policy-v1",
            "model-v1",
            4,
            policy_data={"context_window": self._context_window, "tokenizer_encoding": "cl100k_base"},
        )


class NoTool(ToolPort):
    """本测试不执行工具。"""

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """防止测试意外进入工具路径。"""
        return ToolResult(invocation.call.call_id, ToolResultStatus.FAILED)

    async def cancel(self, run_id: str) -> None:
        """测试没有远程工具调用。"""


class ScriptedCounter:
    """按请求组成返回确定性 Token 数，记录 Engine 是否二次计数。"""

    def __init__(self, initial_tokens: int) -> None:
        self._initial_tokens: int = initial_tokens
        self.requests: list[TokenCountRequest] = []

    async def count(self, request: TokenCountRequest) -> TokenCountResult:
        """区分完整历史、单 Conversation 和压缩后重建输入。"""
        self.requests.append(request)
        if len(request.history_messages) >= 4 and not request.history_summary:
            return TokenCountResult(self._initial_tokens)
        if request.history_summary:
            return TokenCountResult(12)
        if request.history_messages:
            return TokenCountResult(8)
        return TokenCountResult(0)


class ScriptedCompactor(HistoryCompactorPort):
    """记录完整 Conversation 批次并返回摘要。"""

    def __init__(self) -> None:
        self.requests: list[HistoryCompactionRequest] = []

    async def compact_history(self, request: HistoryCompactionRequest) -> HistoryCompactionResult:
        """返回稳定滚动摘要。"""
        self.requests.append(request)
        return HistoryCompactionResult("已压缩的历史事实")


class UnavailableCompactor(HistoryCompactorPort):
    """模拟压缩模型重试耗尽。"""

    async def compact_history(self, request: HistoryCompactionRequest) -> HistoryCompactionResult:
        """返回可恢复的压缩服务不可用错误。"""
        raise HistoryCompactorUnavailable("压缩服务不可用")


class TokenizerUnavailableCounter(ScriptedCounter):
    """模拟缺失 Tokenizer 的确定性预算失败。"""

    async def count(self, request: TokenCountRequest) -> TokenCountResult:
        """返回不含 Prompt 正文的 Tokenizer 错误。"""
        self.requests.append(request)
        return TokenCountResult(0, TokenCountErrorCode.TOKENIZER_UNAVAILABLE, "Tokenizer 不可用")


class StillOverBudgetCounter(ScriptedCounter):
    """模拟历史摘要重建后真实输入依旧超过模型窗口。"""

    async def count(self, request: TokenCountRequest) -> TokenCountResult:
        """仅在最终带摘要的真实调用返回超限 Token 数。"""
        self.requests.append(request)
        if request.history_summary:
            return TokenCountResult(100)
        if len(request.history_messages) >= 4:
            return TokenCountResult(100)
        if request.history_messages:
            return TokenCountResult(8)
        return TokenCountResult(0)


class RepeatedCompressionCounter(ScriptedCounter):
    """模拟已有摘要仍超限，并验证再次压缩后仅保留新摘要。"""

    async def count(self, request: TokenCountRequest) -> TokenCountResult:
        """在旧摘要加三条原文时超限，替换后回到窗口内。"""
        self.requests.append(request)
        if request.history_summary and len(request.history_messages) >= 4:
            return TokenCountResult(100)
        if request.history_summary:
            return TokenCountResult(12)
        if request.history_messages:
            return TokenCountResult(8)
        return TokenCountResult(0)


class FinalLLM(LLMPort):
    """立即返回最终文本。"""

    async def complete(self, context: ContextBundle, execution: RunExecutionView, text_stream_port: TextStreamPort | None = None) -> RunMessage:
        """返回无工具调用的最终回答。"""
        return RunMessage("answer", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "完成")

    async def cancel(self, run_id: str) -> None:
        """测试没有可取消远端调用。"""


class FlakyLLM(LLMPort):
    """首次不可用、重试后成功，验证 checkpoint 恢复。"""

    def __init__(self) -> None:
        self.calls: int = 0

    async def complete(self, context: ContextBundle, execution: RunExecutionView, text_stream_port: TextStreamPort | None = None) -> RunMessage:
        """首次抛出可恢复代理错误，第二次给出最终回答。"""
        self.calls += 1
        if self.calls == 1:
            raise LLMUnavailableError("模型重试耗尽")
        return RunMessage("answer", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "恢复完成")

    async def cancel(self, run_id: str) -> None:
        """测试没有可取消远端调用。"""


class WaitingApprovalLLM(LLMPort):
    """先请求工具审批、恢复后输出最终回答的业务模型 Fake。"""

    def __init__(self) -> None:
        self._calls: int = 0

    async def complete(self, context: ContextBundle, execution: RunExecutionView, text_stream_port: TextStreamPort | None = None) -> RunMessage:
        """首轮产生审批工具调用，后续轮次给出最终回答。"""
        self._calls += 1
        if self._calls == 1:
            return RunMessage(
                "approval-call",
                1,
                RunMessageKind.LLM_RESPONSE,
                MessageRole.ASSISTANT,
                "",
                tool_calls=(ToolCall("call-approval", "危险工具", {}),),
            )
        return RunMessage("approval-final", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "审批恢复完成")

    async def cancel(self, run_id: str) -> None:
        """测试没有可取消远端调用。"""


class WaitingApprovalTool(ToolPort):
    """第一次请求审批，获批后返回工具结果。"""

    def __init__(self) -> None:
        self._calls: int = 0

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """模拟保存后可恢复的审批工具结果。"""
        self._calls += 1
        if self._calls == 1:
            return ToolResult(invocation.call.call_id, ToolResultStatus.APPROVAL_REQUIRED, approval_id="approval-e4")
        return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED, output="已审批工具结果")

    async def cancel(self, run_id: str) -> None:
        """测试没有可取消远端调用。"""


def _request(history_count: int = 3) -> RunRequest:
    """构造由完整 user/assistant Conversation 组成的请求。"""
    messages: list[ConversationMessage] = []
    index: int
    for index in range(history_count):
        messages.append(ConversationMessage(f"conversation-{index}-user", MessageRole.USER, f"问题-{index}", ""))
        messages.append(ConversationMessage(f"conversation-{index}-assistant", MessageRole.ASSISTANT, f"回答-{index}", ""))
    user = ConversationMessage("input-1", MessageRole.USER, "当前问题", "")
    return RunRequest("session-e4", "lease-e4", "agent-e4", user, ConversationSnapshot("session-e4", tuple(messages), history_count))


def _engine(
    tmp_path: Path,
    counter: ScriptedCounter,
    compactor: HistoryCompactorPort,
    llm: LLMPort,
    repository: InMemoryRunRepository | None = None,
    tool_port: ToolPort | None = None,
    policy_port: RunPolicyPort | None = None,
) -> tuple[RuntimeEngine, InMemoryRunRepository]:
    """使用 E4 所有真实 Port 边界构造 Engine。"""
    run_repository = repository or InMemoryRunRepository()
    engine = RuntimeEngine(
        run_repository,
        CheckpointRepositoryAdapter(tmp_path),
        BudgetContext(),
        llm,
        tool_port or NoTool(),
        policy_port or BudgetPolicy(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
        token_counter=counter,
        history_compactor=compactor,
    )
    return engine, run_repository


async def test_over_budget_compresses_oldest_conversations_then_recounts_and_stages_candidate(tmp_path: Path) -> None:
    """超限只压缩最旧 75%，候选在复计数成功后引用新 Context Version。"""
    counter = ScriptedCounter(100)
    compactor = ScriptedCompactor()
    engine, repository = _engine(tmp_path, counter, compactor, FinalLLM())

    result = await engine.execute(_request())
    run = await repository.find_run(result.run_id)
    versions = await repository.load_context_versions("session-e4", result.run_id)

    assert result.status is RunStatus.COMPLETED
    assert len(compactor.requests) == 1
    assert [batch.conversation_id for batch in compactor.requests[0].batches] == ["conversation-0-user", "conversation-1-user"]
    assert len(counter.requests) >= 5
    assert run is not None
    assert len(run.staged_history_compressions) == 1
    assert run.staged_history_compressions[0].context_version == 1
    history_slot = next(slot for slot in versions[0].slots if slot.contribution_kind.value == "history_compressions")
    assert "已压缩的历史事实" in history_slot.content.text


async def test_within_budget_does_not_create_history_compression_candidate(tmp_path: Path) -> None:
    """实际输入未超限时只保存 Context Version，禁止创建空压缩候选。"""
    counter: ScriptedCounter = ScriptedCounter(10)
    compactor: ScriptedCompactor = ScriptedCompactor()
    engine, repository = _engine(tmp_path, counter, compactor, FinalLLM())

    result = await engine.execute(_request(1))
    run = await repository.find_run(result.run_id)

    assert result.status is RunStatus.COMPLETED
    assert not compactor.requests
    assert run is not None
    assert run.staged_history_compressions == ()


async def test_llm_unavailable_keeps_checkpoint_and_retry_reuses_context_version(tmp_path: Path) -> None:
    """业务模型不可用转 INTERRUPTED；重试不生成新的初始上下文版本。"""
    counter = ScriptedCounter(10)
    compactor = ScriptedCompactor()
    llm = FlakyLLM()
    engine, repository = _engine(tmp_path, counter, compactor, llm)

    interrupted = await engine.execute(_request(1))
    checkpoint = await CheckpointRepositoryAdapter(tmp_path).load("session-e4", interrupted.run_id)
    before_versions = await repository.load_context_versions("session-e4", interrupted.run_id)
    completed = await engine.retry_interrupted(interrupted.run_id)

    assert interrupted.status is RunStatus.INTERRUPTED
    assert checkpoint is not None
    assert checkpoint.next_action.value == "invoke_llm"
    assert checkpoint.budget["context_budget"] == {
        "status": "within_budget",
        "input_tokens": 8,
        "context_window": 50,
        "reason": "",
    }
    assert len(before_versions) == 1
    assert completed.status is RunStatus.COMPLETED
    assert llm.calls == 2
    after_versions: tuple[ContextVersion, ...] = await repository.load_context_versions("session-e4", interrupted.run_id)
    completed_run: AgentRun | None = await repository.find_run(interrupted.run_id)
    assert [version.version for version in after_versions] == [1]
    assert completed_run is not None
    assert completed_run.active_context_version == 1


async def test_new_request_abandons_interrupted_run_before_creating_replacement(tmp_path: Path) -> None:
    """同 Session 新请求先放弃旧 INTERRUPTED Run，再允许创建替代 Run。"""
    counter = ScriptedCounter(10)
    compactor = ScriptedCompactor()
    engine, repository = _engine(tmp_path, counter, compactor, FlakyLLM())
    interrupted = await engine.execute(_request(1))
    coordinator = SessionRunCoordinator(engine)
    replacement = await coordinator.submit(_request(1))
    old_run = await repository.find_run(interrupted.run_id)

    assert old_run is not None
    assert old_run.status is RunStatus.ABANDONED
    assert replacement.status is RunStatus.COMPLETED


async def test_compactor_unavailable_interrupts_without_half_finished_context_version_or_candidate(tmp_path: Path) -> None:
    """压缩服务不可用保留可重试 checkpoint，但不生成 Context Version 或候选。"""
    counter = ScriptedCounter(100)
    engine, repository = _engine(tmp_path, counter, UnavailableCompactor(), FinalLLM())

    result = await engine.execute(_request())
    versions = await repository.load_context_versions("session-e4", result.run_id)
    run = await repository.find_run(result.run_id)
    checkpoint = await CheckpointRepositoryAdapter(tmp_path).load("session-e4", result.run_id)

    assert result.status is RunStatus.INTERRUPTED
    assert versions == ()
    assert run is not None
    assert run.staged_history_compressions == ()
    assert checkpoint is not None
    assert checkpoint.pending == {}


async def test_tokenizer_rejection_fails_without_checkpoint_or_context_version(tmp_path: Path) -> None:
    """Tokenizer 不可用属于确定性失败，禁止误标为可恢复中断。"""
    counter = TokenizerUnavailableCounter(0)
    compactor = ScriptedCompactor()
    engine, repository = _engine(tmp_path, counter, compactor, FinalLLM())

    result = await engine.execute(_request(1))
    versions = await repository.load_context_versions("session-e4", result.run_id)
    checkpoint = await CheckpointRepositoryAdapter(tmp_path).load("session-e4", result.run_id)

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code is RunErrorCode.TOKENIZER_UNAVAILABLE
    assert versions == ()
    assert checkpoint is None


async def test_context_still_over_budget_after_compression_fails_without_candidate(tmp_path: Path) -> None:
    """摘要重建后的真实调用仍超限必须失败，不能遗留候选或检查点。"""
    counter: StillOverBudgetCounter = StillOverBudgetCounter(100)
    compactor: ScriptedCompactor = ScriptedCompactor()
    engine, repository = _engine(tmp_path, counter, compactor, FinalLLM())

    result = await engine.execute(_request())
    run = await repository.find_run(result.run_id)
    versions = await repository.load_context_versions("session-e4", result.run_id)
    checkpoint = await CheckpointRepositoryAdapter(tmp_path).load("session-e4", result.run_id)

    assert result.status is RunStatus.FAILED
    assert run is not None
    assert run.staged_history_compressions == ()
    assert versions == ()
    assert checkpoint is None


async def test_repeated_compression_replaces_prior_summary_in_context_version(tmp_path: Path) -> None:
    """已有摘要再次压缩时，新 Context Version 不能重复保留旧 system 摘要。"""
    base_request: RunRequest = _request()
    prior_summary: ConversationMessage = ConversationMessage(
        "history-compression-1",
        MessageRole.SYSTEM,
        "以下是此前对话的压缩摘要：\n旧摘要",
        "",
    )
    prior_compression: HistoryCompressionSnapshot = HistoryCompressionSnapshot(
        1,
        "conversation-before-window",
        "旧摘要",
        "old-hash",
    )
    request: RunRequest = RunRequest(
        base_request.session_id,
        base_request.lease_id,
        base_request.agent_id,
        base_request.user_message,
        ConversationSnapshot(
            base_request.conversation.session_id,
            (prior_summary, *base_request.conversation.messages),
            base_request.conversation.version,
            prior_compression,
        ),
    )
    counter: RepeatedCompressionCounter = RepeatedCompressionCounter(100)
    compactor: ScriptedCompactor = ScriptedCompactor()
    engine, repository = _engine(tmp_path, counter, compactor, FinalLLM())

    result: RunResult = await engine.execute(request)
    versions = await repository.load_context_versions(request.session_id, result.run_id)
    history_slot = next(slot for slot in versions[0].slots if slot.contribution_kind.value == "history_compressions")

    assert result.status is RunStatus.COMPLETED
    assert "已压缩的历史事实" in history_slot.content.text
    assert "旧摘要" not in history_slot.content.text


async def test_invalid_context_window_fails_deterministically_without_checkpoint(tmp_path: Path) -> None:
    """策略参数错误属于确定性失败，不得标记为外部服务中断。"""
    counter: ScriptedCounter = ScriptedCounter(10)
    compactor: ScriptedCompactor = ScriptedCompactor()
    engine, repository = _engine(
        tmp_path,
        counter,
        compactor,
        FinalLLM(),
        policy_port=BudgetPolicy(0),
    )

    result = await engine.execute(_request(1))
    checkpoint = await CheckpointRepositoryAdapter(tmp_path).load("session-e4", result.run_id)
    versions = await repository.load_context_versions("session-e4", result.run_id)

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code is RunErrorCode.CONTEXT_BUDGET
    assert checkpoint is None
    assert versions == ()


async def test_recovery_turns_orphan_running_run_into_interrupted_and_new_request_abandons_it(tmp_path: Path) -> None:
    """首次访问 Session 时遗留 RUNNING 必须转中断，并由新请求先放弃。"""
    counter = ScriptedCounter(10)
    compactor = ScriptedCompactor()
    engine, repository = _engine(tmp_path, counter, compactor, FinalLLM())
    policy = await BudgetPolicy().resolve(_request(1))
    orphan = AgentRun("orphan-run", "session-e4", "agent-e4", RunStatus.RUNNING, "", policy, "input-orphan")
    await repository.create_run(orphan)
    await CheckpointRepositoryAdapter(tmp_path).save(RunCheckpoint(
        "checkpoint-orphan",
        orphan.run_id,
        orphan.session_id,
        1,
        0,
        0,
        {"phase": "waiting_llm", "iteration": 1},
        AgentAction.INVOKE_LLM,
        {},
        {},
    ))
    await engine.recover_session("session-e4")
    interrupted = await repository.find_run(orphan.run_id)
    coordinator = SessionRunCoordinator(engine)

    replacement = await coordinator.submit(_request(1))
    recovered = await repository.find_run(orphan.run_id)

    assert interrupted is not None
    assert interrupted.status is RunStatus.INTERRUPTED
    assert interrupted.error is not None
    assert interrupted.error.code is RunErrorCode.PROCESS_RESTART
    assert recovered is not None
    assert recovered.status is RunStatus.ABANDONED
    assert recovered.error is not None
    assert recovered.error.code is RunErrorCode.CANCELLED
    assert replacement.status is RunStatus.COMPLETED


async def test_waiting_approval_occupies_session_and_restores_prior_run_context(tmp_path: Path) -> None:
    """审批等待期间拒绝普通请求，获批后继续同一 Run 的消息与上下文版本。"""
    counter: ScriptedCounter = ScriptedCounter(10)
    compactor: ScriptedCompactor = ScriptedCompactor()
    repository: InMemoryRunRepository = InMemoryRunRepository()
    engine, _ = _engine(
        tmp_path,
        counter,
        compactor,
        WaitingApprovalLLM(),
        repository,
        WaitingApprovalTool(),
    )
    coordinator: SessionRunCoordinator = SessionRunCoordinator(engine)

    waiting = await coordinator.submit(_request(1))
    busy = await coordinator.submit(_request(1))
    completed = await coordinator.resolve_approval(waiting.approval_id or "", True)
    messages = await repository.load_messages("session-e4", waiting.run_id)
    versions = await repository.load_context_versions("session-e4", waiting.run_id)

    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert busy.status is RunStatus.FAILED
    assert busy.error is not None
    assert busy.error.code is RunErrorCode.SESSION_BUSY
    assert completed.status is RunStatus.COMPLETED
    assert any(message.tool_call_id == "call-approval" for message in messages)
    assert len(versions) >= 1


def test_channel_display_maps_busy_interrupted_and_abandoned_results() -> None:
    """入口展示将 E4 三类控制结果转换为用户可理解的文本。"""
    busy: RunResult = RunResult(
        "run-busy",
        RunStatus.FAILED,
        error=RunError(RunErrorCode.SESSION_BUSY, "Session 占用"),
    )
    interrupted: RunResult = RunResult("run-interrupted", RunStatus.INTERRUPTED)
    abandoned: RunResult = RunResult("run-abandoned", RunStatus.ABANDONED)

    assert "未完成运行" in _display_result(busy)
    assert "可重试" in _display_result(interrupted)
    assert "已放弃" in _display_result(abandoned)
