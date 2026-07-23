"""真实 Session 历史压缩提交与下一次请求注入的端到端验收。"""

from __future__ import annotations

from pathlib import Path

from dotclaw.context import ContextDependencies, build_context_provider
from dotclaw.runtime.adapters import ApprovalRepositoryAdapter, CheckpointRepositoryAdapter, RunRepositoryAdapter, SessionConversationProjector
from dotclaw.runtime.application.approval_service import ApprovalService
from dotclaw.runtime.application.cancellation_service import CancellationService
from dotclaw.runtime.application.context_budget import TokenCountRequest, TokenCountResult
from dotclaw.runtime.application.dto import ContextBundle, RunRequest, RunResult, ToolInvocation, ToolResult, ToolResultStatus
from dotclaw.runtime.application.engine import RuntimeEngine
from dotclaw.runtime.application.execution import RunExecutionView
from dotclaw.runtime.application.history_compaction import HistoryCompactionRequest, HistoryCompactionResult
from dotclaw.runtime.application.ports import HistoryCompactorPort, LLMPort, RunPolicyPort, ToolPort
from dotclaw.runtime.application.request_factory import create_run_request
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, MessageRole, RunMessage, RunMessageKind, RunStatus
from dotclaw.session.session import Conversation, HistoryCompression, Session, SessionManager


class CompressionPolicy(RunPolicyPort):
    """提供必然触发首次历史压缩的冻结模型窗口。"""

    async def resolve(self, request: RunRequest) -> AgentPolicySnapshot:
        """返回带精确 tokenizer 编码的容量策略。"""
        return AgentPolicySnapshot(
            request.agent_id,
            "identity-v1",
            "model-v1",
            4,
            policy_data={"context_window": 50, "tokenizer_encoding": "cl100k_base"},
        )


class SessionLoopCounter:
    """让原始三条历史超限、压缩后的真实输入通过的 TokenCounter Fake。"""

    async def count(self, request: TokenCountRequest) -> TokenCountResult:
        """按请求组成返回可重复的精确计数结果。"""
        if request.history_summary:
            return TokenCountResult(12)
        if len(request.history_messages) >= 6:
            return TokenCountResult(100)
        if request.history_messages:
            return TokenCountResult(4)
        return TokenCountResult(1)


class SessionLoopCompactor(HistoryCompactorPort):
    """记录压缩输入并返回确定性摘要。"""

    def __init__(self) -> None:
        self.requests: list[HistoryCompactionRequest] = []

    async def compact_history(self, request: HistoryCompactionRequest) -> HistoryCompactionResult:
        """保存批次以验证边界直接来源于真实 Conversation ID。"""
        self.requests.append(request)
        return HistoryCompactionResult("已归纳最早两轮对话")


class FinalLLM(LLMPort):
    """成功结束运行，驱动 SessionConversationProjector 提交候选。"""

    async def complete(self, context: ContextBundle, execution: RunExecutionView, text_stream_port: TextStreamPort | None = None) -> RunMessage:
        """返回最终回答。"""
        return RunMessage("final", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "本轮回答")

    async def cancel(self, run_id: str) -> None:
        """测试没有远端模型调用需要取消。"""


class NoTool(ToolPort):
    """防止端到端路径意外进入工具调用。"""

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """返回失败结果以暴露意外工具调用。"""
        return ToolResult(invocation.call.call_id, ToolResultStatus.FAILED)

    async def cancel(self, run_id: str) -> None:
        """测试没有工具调用需要取消。"""


async def test_real_session_history_compression_commits_and_is_injected_on_next_request(tmp_path: Path) -> None:
    """真实 Session 的候选边界可提交，下一次请求只带摘要和边界后的原文。"""
    session_manager: SessionManager = SessionManager(tmp_path)
    session: Session = await session_manager.create(agent_id="agent-history")
    first: Conversation = session.add_conversation("旧问题一", "旧回答一", ["old-run-1"])
    second: Conversation = session.add_conversation("旧问题二", "旧回答二", ["old-run-2"])
    third: Conversation = session.add_conversation("最新旧问题", "最新旧回答", ["old-run-3"])
    await session_manager.save(session)
    request: RunRequest = create_run_request(session, "agent-history", "本轮问题")
    compactor: SessionLoopCompactor = SessionLoopCompactor()
    engine: RuntimeEngine = RuntimeEngine(
        RunRepositoryAdapter(tmp_path, SessionConversationProjector(session_manager)),
        CheckpointRepositoryAdapter(tmp_path),
        build_context_provider(ContextDependencies()),
        FinalLLM(),
        NoTool(),
        CompressionPolicy(),
        ApprovalService(ApprovalRepositoryAdapter(tmp_path)),
        CancellationService(),
        token_counter=SessionLoopCounter(),
        history_compactor=compactor,
    )

    result: RunResult = await engine.execute(request)
    persisted: Session | None = await session_manager.load(session.id)

    assert result.status is RunStatus.COMPLETED
    assert [batch.conversation_id for batch in compactor.requests[0].batches] == [
        first.conversation_id,
        second.conversation_id,
    ]
    assert persisted is not None
    active: HistoryCompression | None = persisted.active_history_compression()
    assert active is not None
    assert active.covered_through_conversation_id == second.conversation_id
    assert active.content == "已归纳最早两轮对话"

    next_request: RunRequest = create_run_request(persisted, "agent-history", "下一轮问题")

    assert next_request.conversation.compressed_history is not None
    assert next_request.conversation.compressed_history.covered_through_conversation_id == second.conversation_id
    assert [message.message_id for message in next_request.conversation.messages] == [
        third.conversation_id,
        f"{third.conversation_id}:assistant",
        persisted.conversations[-1].conversation_id,
        f"{persisted.conversations[-1].conversation_id}:assistant",
    ]
    assert next_request.conversation.messages[0].content == "最新旧问题"
    assert next_request.conversation.version == persisted.conversation_version
