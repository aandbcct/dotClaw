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


def make_terminal_trace(run_id: str = "run-1", *, ended: bool = True, statistics=None):
    """构造含 Tool + Approval + Delegation 的 RunTrace，供 PR5 转换测试使用。

    ``ended=False`` 时返回部分（语义不完整）Trace，用于验证转换拒绝；
    ``statistics`` 用于注入非零 Token / 调用次数，验证基线提取。
    """
    import dataclasses

    from dotclaw.runtime.domain.context import ContextVersion
    from dotclaw.runtime.domain.events import RunEvent, RunEventType
    from dotclaw.runtime.domain.facts import MessageRole, RunMessage, RunMessageKind, ToolCall
    from dotclaw.trace.assembler import assemble_trace
    from tests.trace.helpers import make_run, make_message, make_event

    run = make_run(run_id=run_id, ended=ended)
    if statistics is not None:
        run = dataclasses.replace(run, statistics=statistics)
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "do it"),
        make_message(
            "msg-llm1", 2, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "",
            tool_calls=(ToolCall(call_id="c1", name="t", arguments={"x": 1}),),
        ),
        make_message("msg-tool1", 3, RunMessageKind.TOOL_RESULT, MessageRole.TOOL, "ok", tool_call_id="c1"),
        make_message(
            "msg-llm2", 4, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "",
            tool_calls=(ToolCall(call_id="c2", name="danger", arguments={}),),
        ),
        make_message("msg-tool2", 5, RunMessageKind.TOOL_RESULT, MessageRole.TOOL, "allowed", tool_call_id="c2"),
        make_message(
            "msg-llm3", 6, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "",
            tool_calls=(ToolCall(call_id="c3", name="delegate", arguments={}),),
        ),
        make_message("msg-del1", 7, RunMessageKind.DELEGATION_RESULT, MessageRole.ASSISTANT, "delegated done"),
        make_message("msg-llm4", 8, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, "final answer"),
    )
    events = (
        make_event(run_id, 1, RunEventType.RUN_STARTED),
        make_event(run_id, 2, RunEventType.LLM_STARTED, data={"call_index": 1, "model_id": "m", "context_version": 1}),
        make_event(run_id, 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm1",)),
        make_event(run_id, 4, RunEventType.TOOL_STARTED, data={"call_id": "c1", "tool_name": "t", "source_response_message_id": "msg-llm1"}),
        make_event(run_id, 5, RunEventType.TOOL_COMPLETED, data={"call_id": "c1", "status": "completed", "result_message_id": "msg-tool1"}),
        make_event(run_id, 6, RunEventType.LLM_STARTED, data={"call_index": 2, "model_id": "m", "context_version": 1}),
        make_event(run_id, 7, RunEventType.LLM_COMPLETED, message_ids=("msg-llm2",)),
        make_event(run_id, 8, RunEventType.TOOL_STARTED, data={"call_id": "c2", "tool_name": "danger", "source_response_message_id": "msg-llm2"}),
        make_event(run_id, 9, RunEventType.WAITING_APPROVAL, data={"approval_id": "a1", "call_id": "c2"}),
        make_event(run_id, 10, RunEventType.APPROVAL_RESOLVED, data={"approval_id": "a1", "approved": True}),
        make_event(run_id, 11, RunEventType.TOOL_COMPLETED, data={"call_id": "c2", "status": "completed", "result_message_id": "msg-tool2"}),
        make_event(run_id, 12, RunEventType.LLM_STARTED, data={"call_index": 3, "model_id": "m", "context_version": 1}),
        make_event(run_id, 13, RunEventType.LLM_COMPLETED, message_ids=("msg-llm3",)),
        make_event(run_id, 14, RunEventType.DELEGATION_REQUESTED, data={"tool_call_id": "c3", "target_agent_id": "agent-2"}),
        make_event(run_id, 15, RunEventType.DELEGATION_SUBMITTED, data={"child_run_id": "child-1", "task_id": "task-1", "target_agent_id": "agent-2"}),
        make_event(run_id, 16, RunEventType.DELEGATION_COMPLETED, data={"child_run_id": "child-1", "outcome": "completed"}, message_ids=("msg-del1",)),
        make_event(run_id, 17, RunEventType.LLM_STARTED, data={"call_index": 4, "model_id": "m", "context_version": 1}),
        make_event(run_id, 18, RunEventType.LLM_COMPLETED, message_ids=("msg-llm4",)),
        make_event(run_id, 19, RunEventType.RUN_COMPLETED),
    )
    # 直接构造带固定 created_at 的上下文版本：new_context_version 使用当前时刻，
    # 会让同一份权威事实在不同时刻算出不同 record_hash，破坏转换的可复现性断言。
    context_version = ContextVersion(
        version=1,
        created_at="2026-07-31T00:00:00+00:00",
        slots=(),
        content_hash="chash-1",
        tool_schema_hash="thash-1",
    )
    return assemble_trace(run, events, messages, (context_version,))


def make_simple_trace(run_id: str = "run-simple", *, statistics=None):
    """构造最小工具调用 Trace（无审批、无委派），供 Playback E2E 使用。

    结构：用户输入 → 工具调用 → 工具结果 → 最终回复 → 完成。
    ``statistics`` 用于注入非零统计基线。
    """
    import dataclasses

    from dotclaw.runtime.domain.context import ContextVersion
    from dotclaw.runtime.domain.facts import MessageRole, RunMessage, RunMessageKind, ToolCall, RunStatistics
    from dotclaw.runtime.domain.events import RunEvent, RunEventType
    from dotclaw.trace.assembler import assemble_trace
    from tests.trace.helpers import make_run, make_message, make_event, make_policy

    stats = statistics or RunStatistics(
        duration_ms=100, llm_call_count=2, tool_call_count=1, tokens_in=100, tokens_out=50
    )
    run = make_run(run_id=run_id, ended=True)
    run = dataclasses.replace(run, statistics=stats, policy=make_policy())
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, MessageRole.USER, "weather please"),
        make_message(
            "msg-llm1", 2, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "",
            tool_calls=(ToolCall(call_id="call-1", name="search", arguments={"q": "weather"}),),
        ),
        make_message("msg-tool1", 3, RunMessageKind.TOOL_RESULT, MessageRole.TOOL, "sunny", tool_call_id="call-1"),
        make_message("msg-llm2", 4, RunMessageKind.FINAL_RESPONSE, MessageRole.ASSISTANT, "The weather is sunny today"),
    )
    events = (
        make_event(run_id, 1, RunEventType.RUN_STARTED),
        make_event(run_id, 2, RunEventType.LLM_STARTED, data={"call_index": 1, "model_id": "m", "context_version": 1}),
        make_event(run_id, 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm1",)),
        make_event(run_id, 4, RunEventType.TOOL_STARTED, data={"call_id": "call-1", "tool_name": "search", "source_response_message_id": "msg-llm1"}),
        make_event(run_id, 5, RunEventType.TOOL_COMPLETED, data={"call_id": "call-1", "status": "completed", "result_message_id": "msg-tool1"}),
        make_event(run_id, 6, RunEventType.LLM_STARTED, data={"call_index": 2, "model_id": "m", "context_version": 1}),
        make_event(run_id, 7, RunEventType.LLM_COMPLETED, message_ids=("msg-llm2",)),
        make_event(run_id, 8, RunEventType.RUN_COMPLETED),
    )
    context_version = ContextVersion(
        version=1,
        created_at="2026-07-31T00:00:00+00:00",
        slots=(),
        content_hash="chash-1",
        tool_schema_hash="thash-1",
    )
    return assemble_trace(run, events, messages, (context_version,))
