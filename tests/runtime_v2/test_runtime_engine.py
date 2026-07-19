"""RuntimeEngine 的正常执行、审批恢复与取消验收测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from dotclaw.runtime.adapters import ApprovalRepositoryAdapter, CheckpointRepositoryAdapter, RunRepositoryAdapter
from dotclaw.runtime.application.approval_service import ApprovalService
from dotclaw.runtime.application.cancellation_service import CancellationService
from dotclaw.runtime.application.engine import RuntimeEngine
from dotclaw.runtime.application.ports import ContextPort, LLMPort, RunPolicyPort, ToolPort
from dotclaw.runtime.application.session_run_coordinator import SessionRunCoordinator
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.dto import (
    ContextBundle, ContextMetadata, ConversationMessage, ConversationSnapshot,
    RunRequest, RunResult, ToolInvocation, ToolResult, ToolResultStatus,
)
from dotclaw.runtime.domain.facts import (
    AgentPolicySnapshot, AgentRun, JSONMap, JSONValue, MessageRole, RunMessage, RunMessageKind, RunStatus, ToolCall,
    require_json_map,
)
from dotclaw.context import ContextDependencies, SlotContextProvider
from dotclaw.context.scoped_cache import SlotCacheScope
from dotclaw.context.slot_context import SlotContext
from dotclaw.context.slots import ContextSlot


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
    run_repository: RunRepositoryAdapter = RunRepositoryAdapter(root)
    approval_repository: ApprovalRepositoryAdapter = ApprovalRepositoryAdapter(root)
    return RuntimeEngine(
        run_repository,
        CheckpointRepositoryAdapter(root),
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
    run_repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
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


class ChangingSystemSlot(ContextSlot):
    """每次生成不同文本，用于验证恢复路径必须重放首次冻结 Slot。"""

    name: str = "changing"
    scope: SlotCacheScope = SlotCacheScope.DYNAMIC

    def __init__(self) -> None:
        self.calls: int = 0

    async def produce(self, context: SlotContext) -> str | None:
        """返回带调用序号的 system 内容。"""
        self.calls += 1
        return f"system-{self.calls}"


async def test_approval_resume_reuses_run_id_and_keeps_event_sequence(tmp_path: Path) -> None:
    """审批恢复继续原 run，完整记录审批决议和恢复事件。"""
    run_repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    engine = RuntimeEngine(
        run_repository,
        CheckpointRepositoryAdapter(tmp_path),
        ContextFake(),
        ApprovalLLM(),
        ApprovalTool(),
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
    )
    waiting = await engine.execute(_request("session-approval"))
    completed = await engine.resolve_approval("approval-1", approved=True)

    assert waiting.status.value == "waiting_approval"
    assert completed.status.value == "completed"
    assert completed.run_id == waiting.run_id
    events_path: Path = tmp_path / "session-approval" / "agent_runs" / waiting.run_id / "events.jsonl"
    event_types: list[str] = [
        json.loads(line)["event_type"]
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert event_types.index("approval_resolved") < event_types.index("run_resumed")
    assert event_types.index("run_resumed") < event_types.index("run_completed")


async def test_approval_refuses_v1_messages_without_consuming_pending_record(tmp_path: Path) -> None:
    """v1 Run 必须先迁移；恢复入口不得消费审批或改变等待中的 Run。"""
    run_repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    approval_service: ApprovalService = ApprovalService(ApprovalRepositoryAdapter(tmp_path))
    engine: RuntimeEngine = RuntimeEngine(
        run_repository,
        CheckpointRepositoryAdapter(tmp_path),
        ContextFake(),
        ApprovalLLM(),
        ApprovalTool(),
        PolicyPort(),
        approval_service,
        CancellationService(),
    )
    waiting: RunResult = await engine.execute(_request("session-v1-approval"))
    messages_path: Path = tmp_path / "session-v1-approval" / "agent_runs" / waiting.run_id / "messages.json"
    current_payload: JSONMap = require_json_map(json.loads(messages_path.read_text(encoding="utf-8")))
    raw_messages: JSONValue | None = current_payload.get("messages")
    assert isinstance(raw_messages, list)
    legacy_payload: JSONMap = {
        "run_id": waiting.run_id,
        "version": 1,
        "messages": raw_messages,
    }
    messages_path.write_text(json.dumps(legacy_payload, ensure_ascii=False), encoding="utf-8")

    result: RunResult = await engine.resolve_approval(waiting.approval_id or "", approved=True)

    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert "migrate_messages_v1_to_v2" in result.error.message
    assert await approval_service.find_pending(waiting.approval_id or "") is not None
    run: AgentRun | None = await run_repository.load_run("session-v1-approval", waiting.run_id)
    assert run is not None
    assert run.status is RunStatus.WAITING_APPROVAL


class ReActLLM(LLMPort):
    """记录两轮模型上下文，以验证工具证据能够进入下一轮 ReAct 请求。"""

    def __init__(self) -> None:
        """初始化模型调用记录。"""
        self.contexts: list[ContextBundle] = []

    async def complete(self, context: ContextBundle, execution: RunExecutionView) -> RunMessage:
        """首轮请求两个工具，次轮在断言上下文完整后返回最终回答。"""
        self.contexts.append(context)
        if len(self.contexts) == 1:
            return RunMessage(
                "tool-requests",
                1,
                RunMessageKind.LLM_RESPONSE,
                MessageRole.ASSISTANT,
                "我将查询两个来源。",
                tool_calls=(
                    ToolCall("call-1", "lookup", {"source": "first"}),
                    ToolCall("call-2", "lookup", {"source": "second"}),
                ),
            )
        return RunMessage("answer", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "两个来源均已查询。")

    async def cancel(self, run_id: str) -> None:
        """测试替身无需远程取消。"""


class CompletedTool(ToolPort):
    """记录所有工具调用并返回对应的确定性结果。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.calls: list[ToolInvocation] = []

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """保存调用顺序并返回调用标识对应的结果。"""
        self.calls.append(invocation)
        return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED, output=f"结果-{invocation.call.call_id}")

    async def cancel(self, run_id: str) -> None:
        """测试替身无需远程取消。"""


def _request_with_history(session_id: str) -> RunRequest:
    """构造含既有 Conversation 的请求，用于审批恢复上下文回放测试。"""
    history: ConversationMessage = ConversationMessage(
        "history-1",
        MessageRole.ASSISTANT,
        "这是之前的回答。",
        "2026-07-16T00:00:00+00:00",
    )
    user: ConversationMessage = ConversationMessage(
        "user-1",
        MessageRole.USER,
        "请查询资料。",
        "2026-07-17T00:00:00+00:00",
    )
    return RunRequest(
        session_id,
        f"lease-{session_id}",
        "agent-1",
        user,
        ConversationSnapshot(session_id, (history,), 1),
    )


async def test_react_context_contains_all_tool_calls_and_results_in_next_llm_round(tmp_path: Path) -> None:
    """首轮的 assistant 工具调用及全部 tool result 必须进入第二轮模型上下文。"""
    llm: ReActLLM = ReActLLM()
    tool_port: CompletedTool = CompletedTool()
    engine: RuntimeEngine = RuntimeEngine(
        RunRepositoryAdapter(tmp_path),
        CheckpointRepositoryAdapter(tmp_path),
        SlotContextProvider((), ContextDependencies()),
        llm,
        tool_port,
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
    )

    result: RunResult = await engine.execute(_request_with_history("session-react"))

    assert result.status is RunStatus.COMPLETED
    assert [invocation.call.call_id for invocation in tool_port.calls] == ["call-1", "call-2"]
    second_context: ContextBundle = llm.contexts[1]
    assert [message.content for message in second_context.messages] == [
        "",
        "这是之前的回答。",
        "请查询资料。",
        "我将查询两个来源。",
        "结果-call-1",
        "结果-call-2",
    ]
    assert second_context.messages[3].tool_calls == (
        ToolCall("call-1", "lookup", {"source": "first"}),
        ToolCall("call-2", "lookup", {"source": "second"}),
    )
    assert [message.tool_call_id for message in second_context.messages[4:]] == ["call-1", "call-2"]


async def test_engine_freezes_initial_context_and_audits_llm_calls_without_request_message_copies(tmp_path: Path) -> None:
    """新 Run 只保存增量事实，LLM 调用通过事件引用冻结上下文和消息序列。"""
    llm: ReActLLM = ReActLLM()
    engine: RuntimeEngine = RuntimeEngine(
        RunRepositoryAdapter(tmp_path),
        CheckpointRepositoryAdapter(tmp_path),
        SlotContextProvider((), ContextDependencies()),
        llm,
        CompletedTool(),
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
    )

    result: RunResult = await engine.execute(_request_with_history("session-initial-context"))

    messages_path: Path = tmp_path / "session-initial-context" / "agent_runs" / result.run_id / "messages.json"
    events_path: Path = tmp_path / "session-initial-context" / "agent_runs" / result.run_id / "events.jsonl"
    payload: JSONMap = require_json_map(json.loads(messages_path.read_text(encoding="utf-8")))
    raw_initial_context: JSONValue | None = payload.get("initial_context")
    assert isinstance(raw_initial_context, dict)
    initial_context: JSONMap = raw_initial_context
    raw_history: JSONValue | None = initial_context.get("history")
    assert isinstance(raw_history, dict)
    history: JSONMap = raw_history
    assert history["recent_messages"] == [{
        "conversation_id": "history-1",
        "role": "assistant",
        "content": "这是之前的回答。",
        "created_at": "2026-07-16T00:00:00+00:00",
    }]
    raw_stored_messages: JSONValue | None = payload.get("messages")
    assert isinstance(raw_stored_messages, list)
    stored_messages: list[JSONValue] = raw_stored_messages
    assert [message["kind"] for message in stored_messages if isinstance(message, dict)] == [
        "user_input",
        "llm_response",
        "tool_result",
        "tool_result",
        "final_response",
    ]
    assert all(message["kind"] != "llm_request" for message in stored_messages if isinstance(message, dict))

    events: list[JSONMap] = [
        require_json_map(json.loads(line))
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    llm_started_events: list[JSONMap] = [
        event for event in events if event["event_type"] == "llm_started"
    ]
    assert len(llm_started_events) == 2
    raw_first_data: JSONValue | None = llm_started_events[0].get("data")
    raw_second_data: JSONValue | None = llm_started_events[1].get("data")
    assert isinstance(raw_first_data, dict)
    assert isinstance(raw_second_data, dict)
    first_data: JSONMap = raw_first_data
    second_data: JSONMap = raw_second_data
    assert first_data["incremental_message_ids"] == ["user-1"]
    assert second_data["incremental_message_ids"] == [
        "user-1",
        f"response-{result.run_id}-2",
        f"tool-{result.run_id}-3",
        f"tool-{result.run_id}-4",
    ]
    assert all(event["event_type"] != "context_built" for event in events)


async def test_approval_resume_rebuilds_conversation_and_react_context(tmp_path: Path) -> None:
    """审批批准后应保留首轮 Conversation、assistant 工具调用和工具执行结果。"""
    llm: ReActLLM = ReActLLM()
    approval_tool: ApprovalTool = ApprovalTool()
    engine: RuntimeEngine = RuntimeEngine(
        RunRepositoryAdapter(tmp_path),
        CheckpointRepositoryAdapter(tmp_path),
        SlotContextProvider((), ContextDependencies()),
        llm,
        approval_tool,
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
    )

    waiting: RunResult = await engine.execute(_request_with_history("session-approval-context"))
    completed: RunResult = await engine.resolve_approval(waiting.approval_id or "", approved=True)

    assert completed.status is RunStatus.COMPLETED
    second_context: ContextBundle = llm.contexts[1]
    assert [message.content for message in second_context.messages] == [
        "",
        "这是之前的回答。",
        "请查询资料。",
        "我将查询两个来源。",
        "",
        "已执行",
        "已执行",
    ]
    assert second_context.messages[3].tool_calls == (
        ToolCall("call-1", "lookup", {"source": "first"}),
        ToolCall("call-2", "lookup", {"source": "second"}),
    )


async def test_approval_resume_replays_frozen_system_slots(tmp_path: Path) -> None:
    """审批恢复不得重新计算动态 Slot，后续上下文必须使用首次冻结文本。"""
    llm: ReActLLM = ReActLLM()
    changing_slot: ChangingSystemSlot = ChangingSystemSlot()
    engine: RuntimeEngine = RuntimeEngine(
        RunRepositoryAdapter(tmp_path),
        CheckpointRepositoryAdapter(tmp_path),
        SlotContextProvider((changing_slot,), ContextDependencies()),
        llm,
        ApprovalTool(),
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
    )

    waiting: RunResult = await engine.execute(_request_with_history("session-frozen-slots"))
    completed: RunResult = await engine.resolve_approval(waiting.approval_id or "", approved=True)

    assert completed.status is RunStatus.COMPLETED
    assert changing_slot.calls == 1
    assert llm.contexts[0].messages[0].content == "system-1"
    assert llm.contexts[1].messages[0].content == "system-1"


async def test_rejected_approval_records_decision_and_cancels_without_conversation(tmp_path: Path) -> None:
    """拒绝审批应记录决议并结束原 Run，不投影 assistant Conversation。"""
    run_repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    engine = RuntimeEngine(
        run_repository,
        CheckpointRepositoryAdapter(tmp_path),
        ContextFake(),
        ApprovalLLM(),
        ApprovalTool(),
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
    )

    waiting: RunResult = await engine.execute(_request("session-rejected"))
    rejected: RunResult = await engine.resolve_approval("approval-1", approved=False)
    events_path: Path = tmp_path / "session-rejected" / "agent_runs" / waiting.run_id / "events.jsonl"
    event_types: list[str] = [
        json.loads(line)["event_type"]
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]

    assert rejected.status is RunStatus.CANCELLED
    assert rejected.run_id == waiting.run_id
    assert "approval_resolved" in event_types
    assert "run_cancelled" in event_types
    assert await run_repository.load_conversation("session-rejected") == ()


async def test_cancel_waiting_run_does_not_write_conversation(tmp_path: Path) -> None:
    """取消审批等待中的 run 会删除 checkpoint 且不投影 assistant 消息。"""
    run_repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    engine = RuntimeEngine(
        run_repository,
        CheckpointRepositoryAdapter(tmp_path),
        ContextFake(),
        ApprovalLLM(),
        ApprovalTool(),
        PolicyPort(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
    )
    waiting = await engine.execute(_request("session-cancel"))
    await engine.cancel(waiting.run_id, "用户取消")

    cancelled = await run_repository.load_run("session-cancel", waiting.run_id)
    assert cancelled is not None
    assert cancelled.status.value == "cancelled"
    assert await run_repository.load_conversation("session-cancel") == ()
    assert await CheckpointRepositoryAdapter(tmp_path).load("session-cancel", waiting.run_id) is None


class OrderedEngine:
    """记录协调器执行顺序的最小 Engine 替身。"""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.release: asyncio.Event = asyncio.Event()

    async def execute(self, request: RunRequest) -> RunResult:
        """记录请求顺序；首条请求等待以制造竞争窗口。"""
        self.started.append(request.lease_id)
        if request.lease_id == "lease-fifo-1":
            await self.release.wait()
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


class ControlOrderedEngine:
    """模拟审批恢复期间的 Session 控制入口，验证协调器复用租约。"""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.approval_entered: asyncio.Event = asyncio.Event()
        self.release_approval: asyncio.Event = asyncio.Event()

    async def execute(self, request: RunRequest) -> RunResult:
        """记录普通请求何时真正获得执行机会。"""
        self.started.append(f"submit:{request.lease_id}")
        return RunResult(request.lease_id, RunStatus.COMPLETED)

    async def get_approval_session_id(self, approval_id: str) -> str | None:
        """将测试审批固定映射到同一 Session。"""
        return "session-control" if approval_id == "approval-control" else None

    async def resolve_approval(self, approval_id: str, approved: bool) -> RunResult:
        """阻塞审批恢复，制造与普通消息竞争同一租约的窗口。"""
        self.started.append(f"approval:{approval_id}")
        self.approval_entered.set()
        await self.release_approval.wait()
        return RunResult("run-control", RunStatus.COMPLETED)

    async def get_run_session_id(self, run_id: str) -> str | None:
        """测试不触发取消路径。"""
        return None

    async def cancel(self, run_id: str, reason: str) -> None:
        """测试替身不需要取消行为。"""


async def test_session_coordinator_serializes_approval_resume_with_new_message() -> None:
    """审批恢复与新消息必须竞争同一 Session 租约，避免并发执行。"""
    engine = ControlOrderedEngine()
    coordinator = SessionRunCoordinator(engine)
    approval_task: asyncio.Task[RunResult] = asyncio.create_task(
        coordinator.resolve_approval("approval-control", True),
    )
    await engine.approval_entered.wait()
    request: RunRequest = _request("session-control")
    submit_task: asyncio.Task[RunResult] = asyncio.create_task(coordinator.submit(request))
    await asyncio.sleep(0)

    assert engine.started == ["approval:approval-control"]
    engine.release_approval.set()
    await asyncio.gather(approval_task, submit_task)
    assert engine.started == ["approval:approval-control", f"submit:{request.lease_id}"]
