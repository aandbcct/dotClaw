"""ToolExecutorAdapter 的审批隔离与执行映射测试。"""

from __future__ import annotations

from dotclaw.runtime.adapters import ToolExecutorAdapter
from dotclaw.runtime.adapters import ApprovalRepositoryAdapter, CheckpointRepositoryAdapter, RunRepositoryAdapter
from dotclaw.runtime.application.approval_service import ApprovalService
from dotclaw.runtime.application.cancellation_service import CancellationService
from dotclaw.runtime.application.engine import RuntimeEngine
from tests.runtime_v2.tool_recovery_testkit import file_tool_recovery
from dotclaw.runtime.application.ports import ContextPort, LLMPort, LLMOutputPort, RunPolicyPort
from dotclaw.runtime.application.execution import RunBudget, RunExecutionView
from dotclaw.runtime.application.dto import (
    ContextBundle, ContextMetadata, ContextRefreshSignal, ConversationMessage, ConversationSnapshot,
    RunRequest, ToolInvocation, ToolResultStatus,
)
from dotclaw.runtime.domain.facts import (
    AgentPolicySnapshot, MessageRole, RunMessage, RunMessageKind, ToolCall,
)
from dotclaw.runtime.domain.context import ContextOwner
from dotclaw.runtime.domain.state import AgentRunState, Created, Running, Suspended, Ended, RunStage, SuspendReason, RunOutcome
from dotclaw.tools.approval import ApprovalManager
from dotclaw.tools.decorator import get_tool_meta, tool
from dotclaw.tools.executor import ToolExecutor
from dotclaw.tools.function_handler import FunctionToolHandler
from dotclaw.tools.registry import ToolRegistry
from tests.runtime_v2.context_budget_fakes import AlwaysWithinBudgetCounter, UnexpectedHistoryCompactor


def _execution() -> RunExecutionView:
    """构造最小只读运行视图。"""
    return RunExecutionView("run-1", AgentPolicySnapshot("agent", "v1", "model", 3), AgentRunState(mode=Running(RunStage.CALLING_LLM)), RunBudget(3), 0, None)


async def test_tool_executor_adapter_requires_approval_and_has_no_process_local_replay_guard() -> None:
    """审批前不执行工具；获批重复直调由账本层负责防重，而非 Adapter 缓存。"""
    executions: list[str] = []

    @tool(name="dangerous", description="危险操作", needs_approval=True)
    async def dangerous() -> str:
        executions.append("done")
        return "完成"

    registry = ToolRegistry()
    registry.register(FunctionToolHandler(dangerous, get_tool_meta(dangerous)))
    executor = ToolExecutor(registry, ApprovalManager())
    port: ToolExecutorAdapter = ToolExecutorAdapter(executor)
    invocation = ToolInvocation("run-1", ToolCall("call-1", "dangerous", {}))

    waiting = await port.execute(invocation, _execution())
    repeated_waiting = await port.execute(invocation, _execution())
    assert executions == []
    completed = await port.execute(ToolInvocation("run-1", invocation.call, approved=True), _execution())
    repeated = await port.execute(ToolInvocation("run-1", invocation.call, approved=True), _execution())

    assert waiting.status is ToolResultStatus.APPROVAL_REQUIRED
    assert waiting.approval_id
    assert repeated_waiting.status is ToolResultStatus.APPROVAL_REQUIRED
    assert completed.status is ToolResultStatus.COMPLETED
    assert completed.output == "完成"
    assert repeated.status is ToolResultStatus.COMPLETED
    assert executions == ["done", "done"]


async def test_clear_run_remains_noop_compatibility_method() -> None:
    """clear_run 为旧调用方保留无状态兼容入口。"""

    @tool(name="dangerous", description="危险操作", needs_approval=True)
    async def dangerous() -> str:
        return "完成"

    registry = ToolRegistry()
    registry.register(FunctionToolHandler(dangerous, get_tool_meta(dangerous)))
    executor = ToolExecutor(registry, ApprovalManager())
    port: ToolExecutorAdapter = ToolExecutorAdapter(executor)
    invocation = ToolInvocation("run-9", ToolCall("call-9", "dangerous", {}))

    # 走完审批后执行。
    await port.execute(invocation, _execution())  # APPROVAL_REQUIRED
    await port.execute(ToolInvocation("run-9", invocation.call, approved=True), _execution())  # COMPLETED
    await port.clear_run("run-9")


class FixedPolicy(RunPolicyPort):
    """为 Port bridge 集成测试提供固定策略。"""

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        """返回最小运行策略。"""
        return AgentPolicySnapshot(
            request.agent_id,
            "v1",
            "model",
            4,
            policy_data={"context_window": 128, "tokenizer_encoding": "cl100k_base"},
        )


class EmptyContext(ContextPort):
    """返回最小模型上下文。"""

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        """构造一条 system 消息。"""
        return ContextBundle(
            (RunMessage("context", 1, RunMessageKind.LLM_REQUEST, MessageRole.SYSTEM, "system"),),
            (),
            ContextMetadata(1),
        )

    async def release_scope(self, owner: ContextOwner, owner_key: str) -> None:
        """测试替身不缓存 Slot 实例。"""

    async def release_all(self) -> None:
        """测试替身不缓存 Slot 实例。"""

    def request_refresh(self, slot_id: str, owner: ContextOwner, owner_key: str) -> None:
        """测试替身没有可刷新的 Slot。"""

    def publish_signal(self, signal: ContextRefreshSignal) -> None:
        """测试替身不消费刷新信号。"""


class ToolThenFinalLLM(LLMPort):
    """先请求工具、随后返回最终回复的 LLM 替身。"""

    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name
        self._calls = 0

    async def complete(self, context: ContextBundle, execution: RunExecutionView, output_port: LLMOutputPort | None = None) -> RunMessage:
        """依次返回工具调用和普通回答。"""
        self._calls += 1
        if self._calls == 1:
            return RunMessage("tool-request", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "", tool_calls=(ToolCall("call-1", self._tool_name, {}),))
        return RunMessage("final", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "工具已完成")



def _request() -> RunRequest:
    """创建桥接集成测试的普通请求。"""
    user = ConversationMessage("input", MessageRole.USER, "执行工具", "")
    return RunRequest("session", "lease", "agent", user, ConversationSnapshot("session", (), 0))


async def test_tool_executor_adapter_drives_engine_approval_resume_with_same_run_id(tmp_path) -> None:
    """真实 ToolExecutor bridge 经 Engine 等待、批准恢复且工具只执行一次。"""
    executions: list[str] = []

    @tool(name="dangerous", description="危险操作", needs_approval=True)
    async def dangerous() -> str:
        executions.append("done")
        return "工具输出"

    registry = ToolRegistry()
    registry.register(FunctionToolHandler(dangerous, get_tool_meta(dangerous)))
    bridge: ToolExecutorAdapter = ToolExecutorAdapter(ToolExecutor(registry, ApprovalManager()))
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    engine = RuntimeEngine(
        repository,
        CheckpointRepositoryAdapter(tmp_path),
        EmptyContext(),
        ToolThenFinalLLM("dangerous"),
        bridge,
        FixedPolicy(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
        token_counter=AlwaysWithinBudgetCounter(),
        tool_recovery=file_tool_recovery(tmp_path),
        history_compactor=UnexpectedHistoryCompactor(),
    )

    waiting = await engine.execute(_request())
    completed = await engine.resolve_approval(waiting.approval_id or "", True)
    run = await repository.load_run("session", waiting.run_id)

    assert waiting.state.is_waiting_approval()
    assert completed.state.outcome().value == "completed"
    assert completed.run_id == waiting.run_id
    assert executions == ["done"]
    assert run is not None
    assert run.statistics.tool_call_count == 2
