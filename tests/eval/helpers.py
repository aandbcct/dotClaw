"""Eval 测试的共享构造器：集中组装合法 EvalCase 与各类 Fixture。"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotclaw.runtime.application.dto import (
    ConversationMessage,
    ToolDefinition,
    ToolResultStatus,
)
from dotclaw.runtime.domain.facts import (
    AgentPolicySnapshot,
    MessageRole,
    ToolCall,
)
from dotclaw.eval.models import (
    ApprovalFixture,
    ConversationFixture,
    ContextFixture,
    DelegationFixture,
    EvalCase,
    ExecutionMode,
    Expectation,
    LLMFixture,
    LLMResponseFixture,
    ToolFixture,
)

AGENT_ID = "agent-eval"
SESSION_ID = "sess-eval"


def make_policy(agent_id: str = AGENT_ID, max_iterations: int = 10) -> AgentPolicySnapshot:
    """构造冻结的 Agent 策略；policy_data 携带必需的上下文窗口与 tokenizer。"""
    return AgentPolicySnapshot(
        agent_id=agent_id,
        identity_version="v1",
        model_id="mock-model",
        max_iterations=max_iterations,
        policy_data={"context_window": 100000, "tokenizer_encoding": "cl100k_base"},
    )


def make_input(content: str = "用户问题") -> ConversationMessage:
    """构造用户入口消息。"""
    return ConversationMessage(
        message_id="u1",
        role=MessageRole.USER,
        content=content,
        created_at="2026-01-01T00:00:00Z",
    )


def make_conversation_fixture(session_id: str = SESSION_ID) -> ConversationFixture:
    """构造会话冻结事实（空消息）。"""
    return ConversationFixture(session_id=session_id, messages=(), version=0)


def llm_response(message_id: str, content: str = "", tool_calls=()) -> LLMResponseFixture:
    """构造一次脚本化 LLM 响应。"""
    return LLMResponseFixture(message_id=message_id, content=content, tool_calls=tuple(tool_calls))


def tool_call(call_id: str, name: str, arguments: dict) -> ToolCall:
    """构造工具调用。"""
    return ToolCall(call_id=call_id, name=name, arguments=dict(arguments))


def tool_definition(name: str, description: str = "", parameters: dict | None = None) -> ToolDefinition:
    """构造工具定义。"""
    return ToolDefinition(name=name, description=description, parameters=parameters or {})


def context_fixture(fixture_id: str, messages=(), tools=(), estimated_tokens: int = 1) -> ContextFixture:
    """构造一次冻结上下文构建结果。"""
    return ContextFixture(
        fixture_id=fixture_id,
        messages=tuple(messages),
        tools=tuple(tools),
        estimated_tokens=estimated_tokens,
    )


def make_llm_fixture(fixture_id: str, responses) -> LLMFixture:
    """构造脚本化 LLM Fixture。"""
    return LLMFixture(fixture_id=fixture_id, responses=tuple(responses))


def tool_fixture(
    fixture_id: str,
    tool_name: str,
    key_arguments: dict | None = None,
    status: ToolResultStatus = ToolResultStatus.COMPLETED,
    output: str = "",
    approval_id: str | None = None,
    error_message: str = "",
) -> ToolFixture:
    """构造工具冻结结果与匹配条件。"""
    return ToolFixture(
        fixture_id=fixture_id,
        tool_name=tool_name,
        key_arguments=key_arguments or {},
        status=status,
        output=output,
        approval_id=approval_id,
        error_message=error_message,
    )


def approval_fixture(fixture_id: str, approved: bool, approval_id: str | None = None) -> ApprovalFixture:
    """构造审批冻结决议。"""
    return ApprovalFixture(fixture_id=fixture_id, approved=approved, approval_id=approval_id)


def delegation_fixture(
    fixture_id: str,
    target_agent_id: str,
    child_run_id: str,
    outcome=None,
    output: str = "",
    task_id: str = "",
    target_session_id: str = "",
    error_message: str = "",
) -> DelegationFixture:
    """构造委派冻结受理与结果。"""
    return DelegationFixture(
        fixture_id=fixture_id,
        target_agent_id=target_agent_id,
        child_run_id=child_run_id,
        task_id=task_id,
        target_session_id=target_session_id,
        outcome=outcome,
        output=output,
        error_message=error_message,
    )


def build_case(
    *,
    case_id: str = "case-1",
    agent_id: str = AGENT_ID,
    execution_mode: ExecutionMode = ExecutionMode.PLAYBACK,
    input_message: ConversationMessage | None = None,
    conversation_fixture: ConversationFixture | None = None,
    policy: AgentPolicySnapshot | None = None,
    context_fixtures=(),
    llm_fixture: LLMFixture | None = None,
    tool_fixtures=(),
    approval_fixtures=(),
    delegation_fixtures=(),
    expectations=(),
    tags=(),
    source_trace: str | None = None,
) -> EvalCase:
    """按关键字组装合法 EvalCase；缺省提供一个单轮完成场景。"""
    if llm_fixture is None:
        llm_fixture = make_llm_fixture("llm-1", (llm_response("llm-resp-1", content="done"),))
    if not context_fixtures:
        context_fixtures = (context_fixture("ctx-1"),)
    return EvalCase(
        case_id=case_id,
        agent_id=agent_id,
        execution_mode=execution_mode,
        input=input_message or make_input(),
        conversation_fixture=conversation_fixture or make_conversation_fixture(),
        policy_fixture=policy or make_policy(agent_id),
        context_fixtures=tuple(context_fixtures),
        llm_fixture=llm_fixture,
        tool_fixtures=tuple(tool_fixtures),
        approval_fixtures=tuple(approval_fixtures),
        delegation_fixtures=tuple(delegation_fixtures),
        expectations=tuple(expectations),
        tags=tuple(tags),
        source_trace=source_trace,
    )
