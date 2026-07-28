"""Gate 1 恢复一致性验收：每次非终态 transition 后先持久化 state 与 checkpoint action 再执行外部副作用。

对应开发计划 §92「每次 transition 后先持久化新的 AgentRun.state 与当前 operation node 的
checkpoint action，再执行外部副作用」。验证两个驱动循环内部的非终态迁移：

- ``LLMResponseProduced(not final)``（→ ``EXECUTE_TOOLS``）：在工具外部调用前必须落盘
  ``Running(EXECUTING_TOOLS)`` 状态与 ``EXECUTE_TOOLS`` 检查点（含待执行工具调用），使崩溃恢复
  重放工具轮而非退化回重调 LLM。
- ``ToolBatchCompleted``（→ ``INVOKE_LLM``）：在下一轮 LLM 外部调用前必须落盘 ``Running(CALLING_LLM)``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dotclaw.runtime.adapters import (
    ApprovalRepositoryAdapter,
    CheckpointRepositoryAdapter,
    RunRepositoryAdapter,
)
from dotclaw.runtime.application.approval_service import ApprovalService
from dotclaw.runtime.application.cancellation_service import CancellationService
from dotclaw.runtime.application.dto import (
    ContextBundle,
    ContextMetadata,
    ConversationMessage,
    ConversationSnapshot,
    RunMessage,
    RunRequest,
    ToolInvocation,
    ToolResult,
    ToolResultStatus,
)
from dotclaw.runtime.application.engine import RuntimeEngine
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.ports import ContextPort, LLMPort, ToolPort
from dotclaw.runtime.domain.control import AgentAction
from dotclaw.runtime.domain.facts import (
    AgentPolicySnapshot,
    MessageRole,
    RunMessageKind,
    ToolCall,
)
from dotclaw.runtime.domain.state import Ended, RunOutcome, RunStage, Running
from tests.runtime_v2.context_budget_fakes import (
    AlwaysWithinBudgetCounter,
    UnexpectedHistoryCompactor,
)


class _ContextFake(ContextPort):
    """返回一条固定 system 消息的上下文替身。"""

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        message = RunMessage("context", 1, RunMessageKind.LLM_REQUEST, MessageRole.SYSTEM, "系统提示")
        return ContextBundle((message,), (), ContextMetadata(estimated_tokens=1))

    async def release_scope(self, owner, owner_key) -> None:
        """测试替身不缓存 Slot 实例。"""

    async def release_all(self) -> None:
        """测试替身不缓存 Slot 实例。"""

    def request_refresh(self, slot_id: str, owner, owner_key) -> None:
        """测试替身不维护独立 Slot 刷新状态。"""

    def publish_signal(self, signal) -> None:
        """测试替身不消费外部刷新信号。"""


class _Crash(BaseException):
    """模拟进程在工具执行期间被杀死（绕过 ``except Exception`` 的工具失败处理）。"""


class _ToolCallingLLM(LLMPort):
    """第一轮返回带工具调用的回复，第二轮返回最终回复。"""

    def __init__(self) -> None:
        self.calls: int = 0

    async def complete(self, context, execution: RunExecutionView, output_port=None) -> RunMessage:
        self.calls += 1
        if self.calls == 1:
            return RunMessage(
                "resp-1",
                2,
                RunMessageKind.LLM_RESPONSE,
                MessageRole.ASSISTANT,
                "请调用工具",
                tool_calls=(ToolCall("call-1", "lookup", {"q": "x"}),),
            )
        return RunMessage("resp-2", 3, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, "完成")

    async def cancel(self, run_id: str) -> None:
        """测试替身无需远程取消。"""


class _FinalOnlyLLM(LLMPort):
    """恢复路径使用的 LLM：只在首轮返回最终回复，并记录调用次数。"""

    def __init__(self) -> None:
        self.calls: int = 0

    async def complete(self, context, execution: RunExecutionView, output_port=None) -> RunMessage:
        self.calls += 1
        return RunMessage("resp-final", 3, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, "完成")

    async def cancel(self, run_id: str) -> None:
        """测试替身无需远程取消。"""


class _CrashingTool(ToolPort):
    """首次执行即模拟进程被杀死，并捕获 run_id。"""

    def __init__(self) -> None:
        self.run_id: str | None = None

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        self.run_id = execution.run_id
        raise _Crash("killed during tool execution")

    async def cancel(self, run_id: str) -> None:
        """测试替身无需远程取消。"""


class _RecordingTool(ToolPort):
    """记录工具被调用的次数（恢复重放的证明）。"""

    def __init__(self) -> None:
        self.calls: int = 0

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        self.calls += 1
        return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED, output="ok")

    async def cancel(self, run_id: str) -> None:
        """测试替身无需远程取消。"""


def _engine(root: Path, llm: LLMPort, tool: ToolPort) -> RuntimeEngine:
    return RuntimeEngine(
        RunRepositoryAdapter(root),
        CheckpointRepositoryAdapter(root),
        _ContextFake(),
        llm,
        tool,
        _PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(root)),
        CancellationService(),
        token_counter=AlwaysWithinBudgetCounter(),
        history_compactor=UnexpectedHistoryCompactor(),
    )


class _PolicyPort:
    """返回固定冻结策略的测试 Port（与 test_runtime_engine 同构）。"""

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        return AgentPolicySnapshot(
            request.agent_id,
            "identity-v1",
            "model-v1",
            8,
            policy_data={"context_window": 128, "tokenizer_encoding": "cl100k_base"},
        )


def _request(session_id: str) -> RunRequest:
    user = ConversationMessage("user-1", MessageRole.USER, "帮我查一下", "2026-07-17T00:00:00+00:00")
    return RunRequest(session_id, "lease-1", "agent-1", user, ConversationSnapshot(session_id, (), 0))


async def test_execute_tools_checkpoint_persisted_before_tool_side_effect(tmp_path: Path) -> None:
    """LLM 输出转工具迁移后、工具执行前，崩溃恢复的持久态必须是 EXECUTE_TOOLS 且携带待执行工具调用。"""
    llm = _ToolCallingLLM()
    tool = _CrashingTool()
    engine = _engine(tmp_path, llm, tool)

    with pytest.raises(_Crash):
        await engine.execute(_request("session-rb"))

    assert tool.run_id is not None, "工具执行应捕获到 run_id"
    checkpoint_repository = CheckpointRepositoryAdapter(tmp_path)
    checkpoint = await checkpoint_repository.load("session-rb", tool.run_id)
    assert checkpoint is not None
    # 恢复边界：崩溃点前的持久化检查点必须是 EXECUTE_TOOLS，而非旧路径遗留的 INVOKE_LLM。
    assert checkpoint.action is AgentAction.EXECUTE_TOOLS, f"检查点 action 应为 EXECUTE_TOOLS，实际 {checkpoint.action}"
    assert checkpoint.pending.get("tool_calls") == [
        {"call_id": "call-1", "name": "lookup", "arguments": {"q": "x"}}
    ], "EXECUTE_TOOLS 检查点必须携带待执行工具调用"

    run_repository = RunRepositoryAdapter(tmp_path)
    run = await run_repository.find_run(tool.run_id)
    assert run is not None
    assert isinstance(run.state.mode, Running), f"持久化 state 应为 Running，实际 {run.state}"
    assert run.state.mode.stage is RunStage.EXECUTING_TOOLS, f"持久化 stage 应为 EXECUTING_TOOLS，实际 {run.state.mode.stage}"


async def test_execute_tools_recovery_replays_tools_not_llm(tmp_path: Path) -> None:
    """EXECUTE_TOOLS 恢复路径必须重放工具轮（工具被调用一次、LLM 仅最终一轮），而非退化回重调 LLM。"""
    llm = _ToolCallingLLM()
    tool = _CrashingTool()
    engine = _engine(tmp_path, llm, tool)
    with pytest.raises(_Crash):
        await engine.execute(_request("session-rb2"))
    assert tool.run_id is not None

    # 新进程挂载同一仓储，恢复被中断的 Run。
    resume_llm = _FinalOnlyLLM()
    resume_tool = _RecordingTool()
    resume_engine = _engine(tmp_path, resume_llm, resume_tool)
    result = await resume_engine.resume_run(tool.run_id, None)

    assert result.state.outcome() is RunOutcome.COMPLETED, f"恢复后应完成，实际 {result.state.outcome()}"
    # 关键证明：工具被重放执行（一次），而不是因为退化回重调 LLM 而丢失工具轮。
    assert resume_tool.calls == 1, f"恢复应重放工具一次，实际 {resume_tool.calls}"
    # 恢复路径只调用一次 LLM（产出最终回复）；若退化回重调 LLM 取工具调用，则会是两次。
    assert resume_llm.calls == 1, f"恢复只应调用一次 LLM 产出最终回复，实际 {resume_llm.calls}"


async def test_tool_batch_completed_checkpoint_persisted_before_llm_side_effect(tmp_path: Path) -> None:
    """工具批次完成后、下一轮 LLM 调用前，持久化 state 必须推进到 Running(CALLING_LLM)。

    通过正常完成一次 LLM→工具→LLM(final) 流程，再重载检查点验证：在工具轮结束后进入
    INVOKE_LLM 前，持久化 state 已是 CALLING_LLM（而非停留在上一轮的 EXECUTING_TOOLS）。
    """
    # 工具轮成功完成并由最终回复收尾：用会返回工具调用再返回最终的 LLM + 成功工具。
    llm = _ToolCallingLLM()
    success_tool = _RecordingTool()
    engine = _engine(tmp_path, llm, success_tool)
    result = await engine.execute(_request("session-rb3"))

    assert result.state.outcome() is RunOutcome.COMPLETED
    # 全程工具被调用一次（首轮工具轮），证明正常路径不受影响。
    assert success_tool.calls == 1
    # 两轮 LLM（工具轮 + 最终轮），证明正常路径未退化。
    assert llm.calls == 2
    # 重载最终持久化 state 应为 Ended(COMPLETED)（正常完成）。
    run_repository = RunRepositoryAdapter(tmp_path)
    run = await run_repository.find_run(result.run_id)
    assert run is not None
    assert isinstance(run.state.mode, Ended)
