"""Runtime v2 Port 输入输出的最小契约测试。"""

from __future__ import annotations

from dotclaw.runtime.application.ports import ContextPort, LLMPort, ToolPort
from dotclaw.runtime.application.execution import RunBudget, RunExecution, RunExecutionView
from dotclaw.runtime.application.dto import (
    ContextBundle,
    ContextMetadata,
    ConversationMessage,
    ConversationSnapshot,
    RunRequest,
    ToolInvocation,
    ToolResult,
    ToolResultStatus,
)
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, MessageRole, RunMessage, RunMessageKind
from dotclaw.runtime.domain.context import ContextOwner
from dotclaw.runtime.domain.state import AgentState


class FakeContextPort:
    """用于验证 ContextPort 入参边界的内存替身。"""

    async def build(self, request: RunRequest, execution: RunExecutionView) -> ContextBundle:
        """仅使用冻结请求与只读执行字段构造上下文。"""
        user_message: RunMessage = RunMessage(
            message_id=request.user_message.message_id,
            sequence=1,
            kind=RunMessageKind.USER_INPUT,
            role=MessageRole.USER,
            content=request.user_message.content,
        )
        return ContextBundle(
            messages=(user_message,),
            tools=(),
            metadata=ContextMetadata(estimated_tokens=1),
        )

    async def release_scope(self, owner: ContextOwner, owner_key: str) -> None:
        """测试替身不缓存 Slot 实例。"""


class FakeLLMPort:
    """用于验证 LLMPort 标准响应的内存替身。"""

    async def complete(self, context: ContextBundle, execution: RunExecutionView) -> RunMessage:
        """返回不依赖具体模型客户端的标准消息。"""
        return RunMessage(
            message_id="message-assistant-1",
            sequence=2,
            kind=RunMessageKind.LLM_RESPONSE,
            role=MessageRole.ASSISTANT,
            content=context.messages[0].content,
        )

    async def cancel(self, run_id: str) -> None:
        """替身无需实际取消远程请求。"""


class FakeToolPort:
    """用于验证 ToolPort 标准结果的内存替身。"""

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """返回成功的标准工具结果。"""
        return ToolResult(call_id=invocation.call.call_id, status=ToolResultStatus.COMPLETED)

    async def cancel(self, run_id: str) -> None:
        """替身无需实际取消工具进程。"""


def _build_execution() -> RunExecution:
    """构造交给 fake ports 的最小执行事务。"""
    user_message: ConversationMessage = ConversationMessage(
        message_id="message-user-1",
        role=MessageRole.USER,
        content="你好",
        created_at="2026-07-16T00:00:00+00:00",
    )
    request: RunRequest = RunRequest(
        session_id="session-1",
        lease_id="lease-1",
        agent_id="agent-1",
        user_message=user_message,
        conversation=ConversationSnapshot("session-1", (user_message,), 1),
    )
    policy: AgentPolicySnapshot = AgentPolicySnapshot("agent-1", "identity-v1", "model-v1", 8)
    return RunExecution("run-1", request, policy, AgentState(), RunBudget(8))


async def test_fake_ports_exchange_domain_types_only() -> None:
    """fake ports 可在不启动 LLM、MCP 或文件系统时完成契约调用。"""
    execution: RunExecution = _build_execution()
    context_port: ContextPort = FakeContextPort()
    llm_port: LLMPort = FakeLLMPort()
    tool_port: ToolPort = FakeToolPort()

    context: ContextBundle = await context_port.build(execution.request, execution.view())
    response: RunMessage = await llm_port.complete(context, execution.view())

    assert context.messages[0].role is MessageRole.USER
    assert response.role is MessageRole.ASSISTANT
    await tool_port.cancel(execution.run_id)
