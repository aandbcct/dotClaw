"""RuntimeEngine 的正常执行、审批恢复与取消验收测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dotclaw.runtime.adapters import FileApprovalRepository, FileCheckpointRepository, FileRunRepository
from dotclaw.runtime.application.approval_service import ApprovalService
from dotclaw.runtime.application.cancellation_service import CancellationService
from dotclaw.runtime.application.engine import RuntimeEngine
from dotclaw.runtime.application.ports import ContextPort, LLMPort, RunPolicyPort, ToolPort
from dotclaw.runtime.application.session_run_coordinator import SessionRunCoordinator
from dotclaw.runtime.domain.execution import RunExecutionView
from dotclaw.runtime.domain.models import (
    AgentPolicySnapshot, ContextBundle, ContextMetadata, ConversationMessage, ConversationSnapshot,
    MessageRole, RunMessage, RunMessageKind, RunRequest, ToolCall, ToolInvocation, ToolResult, ToolResultStatus,
)


class PolicyPort(RunPolicyPort):
    """返回固定冻结策略的测试 Port。"""

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        """构造最小策略快照。"""
        return AgentPolicySnapshot(request.agent_id, "identity-v1", "model-v1", 8)


class ContextFake(ContextPort):
    """返回一条固定 system 消息的上下文替身。"""

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        """构造完整模型请求的最小上下文。"""
        message = RunMessage("context", 1, RunMessageKind.LLM_REQUEST, MessageRole.SYSTEM, "系统提示")
        return ContextBundle((message,), (), ContextMetadata(estimated_tokens=1))


class FinalLLM(LLMPort):
    """立即返回普通最终回答的 LLM 替身。"""

    async def complete(self, context: ContextBundle, execution: RunExecutionView) -> RunMessage:
        """返回不含工具调用的最终回复。"""
        return RunMessage("answer", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "请补充信息")

    async def cancel(self, run_id: str) -> None:
        """测试替身无需远程取消。"""


class ToolFake(ToolPort):
    """正常文本路径无需调用的工具替身。"""

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """返回失败以防测试意外走入工具路径。"""
        return ToolResult(invocation.call.call_id, ToolResultStatus.FAILED, error=None)

    async def cancel(self, run_id: str) -> None:
        """测试替身无需远程取消。"""


def _request(session_id: str) -> RunRequest:
    """构造一条普通用户请求。"""
    user = ConversationMessage("user-1", MessageRole.USER, "我需要帮助", "2026-07-17T00:00:00+00:00")
    return RunRequest(session_id, f"lease-{session_id}", "agent-1", user, ConversationSnapshot(session_id, (), 0))


def _engine(root: Path) -> RuntimeEngine:
    """使用真实文件仓储和 fake 外部端口构造 Engine。"""
    run_repository = FileRunRepository(root)
    approval_repository = FileApprovalRepository(root)
    return RuntimeEngine(
        run_repository,
        FileCheckpointRepository(root),
        ContextFake(),
        FinalLLM(),
        ToolFake(),
        PolicyPort(),
        ApprovalService(approval_repository),
        CancellationService(),
    )


async def test_engine_completes_clarification_as_normal_conversation(tmp_path: Path) -> None:
    """澄清回复是完成态，成功投影 Conversation 且不锁住 Session。"""
    engine = _engine(tmp_path)
    result = await engine.execute(_request("session-1"))

    assert result.status.value == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "请补充信息"
    run_repository = FileRunRepository(tmp_path)
    conversation = await run_repository.load_conversation("session-1")
    assert conversation[0].content == "请补充信息"


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

    async def complete(self, context: ContextBundle, execution: RunExecutionView) -> RunMessage:
        """第一次返回工具调用，第二次返回普通完成消息。"""
        self._call_count += 1
        if self._call_count == 1:
            return RunMessage("tool-request", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "", tool_calls=(ToolCall("call-1", "dangerous", {}),))
        return RunMessage("answer-after-approval", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "审批后完成")

    async def cancel(self, run_id: str) -> None:
        """测试替身无需远程取消。"""


class ApprovalTool(ToolPort):
    """首次要求审批、恢复后完成同一工具调用的替身。"""

    def __init__(self) -> None:
        self._call_count: int = 0

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """按调用次数模拟审批与批准后的真实执行。"""
        self._call_count += 1
        if self._call_count == 1:
            return ToolResult(invocation.call.call_id, ToolResultStatus.APPROVAL_REQUIRED, approval_id="approval-1")
        return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED, output="已执行")

    async def cancel(self, run_id: str) -> None:
        """测试替身无需远程取消。"""


async def test_approval_resume_reuses_run_id_and_keeps_event_sequence(tmp_path: Path) -> None:
    """审批恢复继续原 run，消息与事件序号不重置。"""
    run_repository = FileRunRepository(tmp_path)
    engine = RuntimeEngine(
        run_repository,
        FileCheckpointRepository(tmp_path),
        ContextFake(),
        ApprovalLLM(),
        ApprovalTool(),
        PolicyPort(),
        ApprovalService(FileApprovalRepository(tmp_path)),
        CancellationService(),
    )
    waiting = await engine.execute(_request("session-approval"))
    completed = await engine.resolve_approval("approval-1", approved=True)

    assert waiting.status.value == "waiting_approval"
    assert completed.status.value == "completed"
    assert completed.run_id == waiting.run_id
    events_path: Path = tmp_path / "session-approval" / "agent_runs" / waiting.run_id / "events.jsonl"
    assert len(events_path.read_text(encoding="utf-8").splitlines()) >= 6


async def test_cancel_waiting_run_does_not_write_conversation(tmp_path: Path) -> None:
    """取消审批等待中的 run 会删除 checkpoint 且不投影 assistant 消息。"""
    run_repository = FileRunRepository(tmp_path)
    engine = RuntimeEngine(
        run_repository,
        FileCheckpointRepository(tmp_path),
        ContextFake(),
        ApprovalLLM(),
        ApprovalTool(),
        PolicyPort(),
        ApprovalService(FileApprovalRepository(tmp_path)),
        CancellationService(),
    )
    waiting = await engine.execute(_request("session-cancel"))
    await engine.cancel(waiting.run_id, "用户取消")

    cancelled = await run_repository.load_run("session-cancel", waiting.run_id)
    assert cancelled is not None
    assert cancelled.status.value == "cancelled"
    assert await run_repository.load_conversation("session-cancel") == ()
    assert await FileCheckpointRepository(tmp_path).load("session-cancel", waiting.run_id) is None


class OrderedEngine:
    """记录协调器执行顺序的最小 Engine 替身。"""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.release: asyncio.Event = asyncio.Event()

    async def execute(self, request: RunRequest):
        """记录请求顺序；首条请求等待以制造竞争窗口。"""
        self.started.append(request.lease_id)
        if request.lease_id == "lease-fifo-1":
            await self.release.wait()
        from dotclaw.runtime.domain.models import RunResult, RunStatus
        return RunResult(request.lease_id, RunStatus.COMPLETED)


async def test_session_coordinator_serializes_same_session_fifo() -> None:
    """同 Session 请求严格串行，后请求不得在前请求完成前启动。"""
    engine = OrderedEngine()
    coordinator = SessionRunCoordinator(engine)
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
