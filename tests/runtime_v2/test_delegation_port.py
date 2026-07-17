"""Runtime v2 DelegationPort 的父子运行与适配器契约测试。"""

from __future__ import annotations

import json
from pathlib import Path

from dotclaw.agent.identity import AgentIdentity
from dotclaw.orchestration.registry import AgentRegistry
from dotclaw.orchestration.runtime_delegation_adapter import RuntimeDelegationAdapter
from dotclaw.runtime.adapters import FileApprovalRepository, FileCheckpointRepository, FileRunRepository
from dotclaw.runtime.application.approval_service import ApprovalService
from dotclaw.runtime.application.cancellation_service import CancellationService
from dotclaw.runtime.application.engine import RuntimeEngine
from dotclaw.runtime.application.ports import ContextPort, DelegationPort, LLMPort, RunPolicyPort, ToolPort
from dotclaw.runtime.domain.execution import RunExecutionView
from dotclaw.runtime.domain.models import (
    AgentPolicySnapshot,
    ContextBundle,
    ContextMetadata,
    ConversationMessage,
    ConversationSnapshot,
    DelegationRequest,
    DelegationResult,
    JSONMap,
    MessageRole,
    RunMessage,
    RunMessageKind,
    RunRequest,
    RunResult,
    RunStatus,
    ToolCall,
    ToolInvocation,
    ToolResult,
    ToolResultStatus,
)
from dotclaw.session.session import SessionManager


class FixedPolicy(RunPolicyPort):
    """提供父运行和子运行共用的冻结策略。"""

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        """返回最小可执行策略。"""
        return AgentPolicySnapshot(request.agent_id, "policy-v1", "model", 5)


class MinimalContext(ContextPort):
    """构造最小 system 上下文。"""

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        """返回不含外部基础设施的确定性上下文。"""
        system_message: RunMessage = RunMessage(
            "system",
            1,
            RunMessageKind.LLM_REQUEST,
            MessageRole.SYSTEM,
            "system",
        )
        return ContextBundle((system_message,), (), ContextMetadata(1))


class DelegatingLLM(LLMPort):
    """先发起 delegate，再根据子运行结果输出最终回答。"""

    def __init__(self) -> None:
        self._calls: int = 0

    async def complete(self, context: ContextBundle, execution: RunExecutionView) -> RunMessage:
        """模拟父运行的 delegation 控制循环。"""
        self._calls += 1
        if self._calls == 1:
            return RunMessage(
                "delegate",
                1,
                RunMessageKind.LLM_RESPONSE,
                MessageRole.ASSISTANT,
                "",
                tool_calls=(ToolCall("call-delegate", "delegate", {
                    "target_agent_id": "target-agent",
                    "title": "子任务",
                    "objective": "完成调研",
                }),),
            )
        return RunMessage("answer", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "已整合子运行结果")

    async def cancel(self, run_id: str) -> None:
        """测试替身没有远程调用需要取消。"""


class NoToolExecution(ToolPort):
    """确保 delegate 由 DelegationPort 而非 ToolPort 执行。"""

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """若被调用则明确失败。"""
        return ToolResult(invocation.call.call_id, ToolResultStatus.FAILED)

    async def cancel(self, run_id: str) -> None:
        """测试替身没有工具调用需要取消。"""


class FinalLLM(LLMPort):
    """用于验证子运行摘要关系的最小完成模型。"""

    async def complete(self, context: ContextBundle, execution: RunExecutionView) -> RunMessage:
        """立即返回普通终态回答。"""
        return RunMessage("child-answer", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "子运行完成")

    async def cancel(self, run_id: str) -> None:
        """测试替身没有远程调用需要取消。"""


class CompletedDelegation(DelegationPort):
    """记录父请求并返回已完成子运行的 fake Port。"""

    def __init__(self) -> None:
        self.request: DelegationRequest | None = None

    async def submit(self, request: DelegationRequest) -> str:
        """保存结构化请求并返回固定子运行标识。"""
        self.request = request
        return "child-run"

    async def result(self, child_run_id: str) -> DelegationResult | None:
        """返回成功子运行的标准化结果。"""
        return DelegationResult(child_run_id, RunStatus.COMPLETED, "子运行输出")

    async def cancel(self, child_run_id: str) -> None:
        """测试不需要取消子运行。"""


def _request() -> RunRequest:
    """构造父运行请求。"""
    return RunRequest(
        "parent-session",
        "parent-lease",
        "parent-agent",
        ConversationMessage("input", MessageRole.USER, "请委托", ""),
        ConversationSnapshot("parent-session", (), 0),
    )


async def test_engine_records_delegation_parent_child_events_without_dispatcher(tmp_path: Path) -> None:
    """Engine 只依赖 DelegationPort，并持久化父子运行关系和事件。"""
    delegation = CompletedDelegation()
    repository = FileRunRepository(tmp_path)
    engine = RuntimeEngine(
        repository,
        FileCheckpointRepository(tmp_path),
        MinimalContext(),
        DelegatingLLM(),
        NoToolExecution(),
        FixedPolicy(),
        ApprovalService(FileApprovalRepository(tmp_path)),
        CancellationService(),
        delegation,
    )

    result: RunResult = await engine.execute(_request())
    events_path: Path = tmp_path / "parent-session" / "agent_runs" / result.run_id / "events.jsonl"
    events: list[JSONMap] = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]

    assert result.status is RunStatus.COMPLETED
    assert delegation.request is not None
    assert delegation.request.parent_run_id == result.run_id
    assert delegation.request.root_run_id == result.run_id
    assert [event["event_type"] for event in events].count("delegation_submitted") == 1
    assert [event["event_type"] for event in events].count("delegation_completed") == 1
    completed_event = next(event for event in events if event["event_type"] == "delegation_completed")
    assert completed_event["data"] == {"child_run_id": "child-run", "status": "completed"}


class RecordingCoordinator:
    """记录适配器创建的子运行请求。"""

    def __init__(self) -> None:
        self.request: RunRequest | None = None
        self.cancelled_run_id: str = ""

    async def submit(self, request: RunRequest) -> RunResult:
        """返回已完成的 child Run，并保留请求用于断言。"""
        self.request = request
        answer: ConversationMessage = ConversationMessage("child-answer", MessageRole.ASSISTANT, "目标完成", "")
        return RunResult("child-run", RunStatus.COMPLETED, answer)

    async def cancel(self, run_id: str, reason: str) -> None:
        """记录取消请求。"""
        self.cancelled_run_id = run_id


async def test_runtime_delegation_adapter_creates_target_run_with_parent_and_root(tmp_path: Path) -> None:
    """适配器创建独立 target Session，并保留 parent/root 运行关系。"""
    target: AgentIdentity = AgentIdentity(agent_id="target-agent", agent_name="目标 Agent", model="model")
    registry = AgentRegistry()
    registry.register(target)
    coordinator = RecordingCoordinator()
    adapter = RuntimeDelegationAdapter(SessionManager(tmp_path), registry)
    adapter.bind_coordinator(coordinator)
    request = DelegationRequest(
        parent_run_id="parent-run",
        root_run_id="root-run",
        target_agent_id=target.agent_id,
        input_message=ConversationMessage("delegation", MessageRole.USER, "执行子任务", ""),
    )

    child_run_id: str = await adapter.submit(request)
    result: DelegationResult | None = await adapter.result(child_run_id)
    assert coordinator.request is not None
    assert coordinator.request.parent_run_id == "parent-run"
    assert coordinator.request.root_run_id == "root-run"
    assert coordinator.request.agent_id == target.agent_id
    assert result == DelegationResult("child-run", RunStatus.COMPLETED, "目标完成")


async def test_child_run_persists_parent_and_root_relationship(tmp_path: Path) -> None:
    """Engine 必须将 adapter 传入的 parent/root 写入子 AgentRun 摘要。"""
    repository = FileRunRepository(tmp_path)
    engine = RuntimeEngine(
        repository,
        FileCheckpointRepository(tmp_path),
        MinimalContext(),
        FinalLLM(),
        NoToolExecution(),
        FixedPolicy(),
        ApprovalService(FileApprovalRepository(tmp_path)),
        CancellationService(),
    )
    child_request = RunRequest(
        "child-session",
        "child-lease",
        "target-agent",
        ConversationMessage("child-input", MessageRole.USER, "子任务", ""),
        ConversationSnapshot("child-session", (), 0),
        parent_run_id="parent-run",
        root_run_id="root-run",
    )

    result: RunResult = await engine.execute(child_request)
    run = await repository.load_run("child-session", result.run_id)
    assert run is not None
    assert run.parent_run_id == "parent-run"
    assert run.root_run_id == "root-run"
