"""PR4 测试共享工具：组装真实隔离 Run 的 Trace，并构造合成 Trace 覆盖特定 Span 状态。

- ``tool_case`` / ``approval_required_case``：复用 PR3 隔离环境跑出真实 Trace；
- ``approval_trace`` / ``tool_status_trace`` / 带统计的合成 Trace：用于真实 Run 难以
  稳定产生的状态（已决议审批、失败工具、非零 token 统计）。
"""

from .helpers import (
    AGENT_ID,
    approval_fixture,
    build_case,
    context_fixture,
    llm_response,
    make_conversation_fixture,
    make_input,
    make_llm_fixture,
    make_policy,
    tool_call,
    tool_fixture,
)
from dotclaw.eval.environment import EvalEnvironment
from dotclaw.eval.models import ExecutionMode, Expectation
from dotclaw.runtime.application.dto import ToolResultStatus
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.facts import (
    AgentRun,
    MessageRole,
    RunMessage,
    RunMessageKind,
    RunStatistics,
)
from dotclaw.runtime.domain.state import (
    AgentRunState,
    Ended,
    RunOutcome,
    Suspended,
    SuspendReason,
)
from dotclaw.trace.assembler import assemble_trace


def sys_message() -> RunMessage:
    """构造冻结系统提示消息，供上下文保留评分使用。"""
    return RunMessage(
        message_id="sys-1",
        sequence=1,
        kind=RunMessageKind.LLM_RESPONSE,
        role=MessageRole.SYSTEM,
        content="SECRET_SYSTEM_TEXT protected",
    )


def tool_case(**kwargs) -> object:
    """构造与 PR4 冒烟一致的工具调用 Case；可覆盖 execution_mode / expectations 等。"""
    base = dict(
        case_id="tool-case",
        context_fixtures=(
            context_fixture("ctx-1", messages=(sys_message(),)),
            context_fixture("ctx-2"),
        ),
        llm_fixture=make_llm_fixture(
            "llm-1",
            (
                llm_response(
                    "llm-resp-1",
                    content="",
                    tool_calls=(tool_call("call-1", "search", {"q": "weather"}),),
                ),
                llm_response("llm-resp-2", content="The weather is sunny today"),
            ),
        ),
        tool_fixtures=(tool_fixture("tool-1", "search", key_arguments={"q": "weather"}, output="sunny"),),
        expectations=(),
    )
    base.update(kwargs)
    return build_case(**base)


def approval_required_case() -> object:
    """构造工具需审批、但未提供审批决议的 Case；隔离 Run 会挂起（部分 Trace）。"""
    return tool_case(
        case_id="approval-case",
        llm_fixture=make_llm_fixture(
            "llm-1",
            (llm_response("llm-resp-1", content="", tool_calls=(tool_call("call-1", "search", {"q": "weather"}),)),),
        ),
        tool_fixtures=(
            tool_fixture(
                "tool-1",
                "search",
                key_arguments={"q": "weather"},
                status=ToolResultStatus.APPROVAL_REQUIRED,
                approval_id="apr-1",
            ),
        ),
        execution_mode=ExecutionMode.PLAYBACK,
    )


def approval_resolved_case(approved: bool, **kwargs) -> object:
    """构造工具需审批、且由审批 Fixture 给出决议的 Case；隔离 Run 会自动批准/拒绝。

    两个分支的 Fixture 数量不同，且都必须被完整消费：

    * 批准：审批通过后引擎以 ``approved=True`` 重放同一工具调用并继续下一轮 LLM，
      因此需要两次上下文构建与两条模型响应；
    * 拒绝：决议当场以取消收口，工具不再执行，只需一次上下文与一条模型响应。
    """
    first = llm_response(
        "llm-resp-1",
        content="",
        tool_calls=(tool_call("call-1", "search", {"q": "weather"}),),
    )
    followups = (llm_response("llm-resp-2", content="The weather is sunny today"),) if approved else ()
    contexts = (
        (context_fixture("ctx-1", messages=(sys_message(),)), context_fixture("ctx-2"))
        if approved
        else (context_fixture("ctx-1", messages=(sys_message(),)),)
    )
    base = dict(
        case_id=f"approval-{'approved' if approved else 'rejected'}-case",
        context_fixtures=contexts,
        llm_fixture=make_llm_fixture("llm-1", (first, *followups)),
        tool_fixtures=(
            tool_fixture(
                "tool-1",
                "search",
                key_arguments={"q": "weather"},
                status=ToolResultStatus.APPROVAL_REQUIRED,
                approval_id="apr-1",
                output="sunny",
            ),
        ),
        approval_fixtures=(approval_fixture("apr-fix-1", approved=approved, approval_id="apr-1"),),
        execution_mode=ExecutionMode.PLAYBACK,
    )
    base.update(kwargs)
    return tool_case(**base)


async def run_case_to_trace(case) -> object:
    """执行隔离 Run 并组装 Trace（不评分），用于 Scorer 直接测试。"""
    env = EvalEnvironment(case)
    outcome = await env.run()
    return assemble_trace(outcome.run, outcome.events, outcome.messages, outcome.context_versions)


# --------------------------------------------------------------------------- #
# 合成 Trace 构造：覆盖真实 Run 难以稳定产生的 Span 状态
# --------------------------------------------------------------------------- #


def _ev(seq: int, etype: RunEventType, data=None, message_ids=()) -> RunEvent:
    """构造一条运行事件。"""
    return RunEvent(
        run_id="r1",
        sequence=seq,
        event_type=etype,
        occurred_at="2026-01-01T00:00:00Z",
        message_ids=tuple(message_ids),
        data=data or {},
    )


def _msg(message_id: str, content: str, role=MessageRole.ASSISTANT, kind=RunMessageKind.FINAL_RESPONSE, tool_calls=()):
    """构造一条运行消息。"""
    return RunMessage(
        message_id=message_id,
        sequence=0,
        kind=kind,
        role=role,
        content=content,
        tool_calls=tuple(tool_calls),
    )


def _agent_run(state, statistics=None, final_message_id=None) -> AgentRun:
    """构造最小但合法的 AgentRun。"""
    return AgentRun(
        run_id="r1",
        session_id="s1",
        agent_id=AGENT_ID,
        state=state,
        started_at="2026-01-01T00:00:00Z",
        policy=make_policy(),
        input_message_id="u1",
        ended_at="2026-01-01T00:00:01Z",
        final_message_id=final_message_id,
        statistics=statistics or RunStatistics(),
    )


def synthetic_trace(
    events,
    messages=(),
    run_state=None,
    statistics=None,
    final_message_id=None,
    context_versions=(),
):
    """把手工事件/消息组装为 RunTrace。"""
    run = _agent_run(
        run_state or AgentRunState(mode=Ended(RunOutcome.COMPLETED)),
        statistics=statistics,
        final_message_id=final_message_id,
    )
    return assemble_trace(run, tuple(events), tuple(messages), tuple(context_versions))


def approval_trace(approved: bool):
    """带已决议审批 Span 的 Trace（approved=True→COMPLETED，False→CANCELLED）。"""
    events = [
        _ev(1, RunEventType.RUN_STARTED),
        _ev(2, RunEventType.LLM_STARTED, {"call_index": 0, "model_id": "m"}),
        _ev(3, RunEventType.LLM_COMPLETED, {}, message_ids=["m-llm"]),
        _ev(4, RunEventType.TOOL_STARTED, {"call_id": "call-1", "tool_name": "search", "status": "started"}),
        _ev(5, RunEventType.TOOL_COMPLETED, {"call_id": "call-1", "status": "approval_required"}),
        _ev(6, RunEventType.WAITING_APPROVAL, {"approval_id": "apr-1", "call_id": "call-1"}),
        _ev(7, RunEventType.APPROVAL_RESOLVED, {"approval_id": "apr-1", "approved": approved}),
        _ev(8, RunEventType.RUN_COMPLETED),
    ]
    return synthetic_trace(events, final_message_id="m-llm")


def tool_status_trace(tool_status: str, call_id: str = "call-1", tool_name: str = "search"):
    """带指定终态工具 Span 的 Trace（completed/failed/cancelled）。"""
    events = [
        _ev(1, RunEventType.RUN_STARTED),
        _ev(2, RunEventType.LLM_STARTED, {"call_index": 0, "model_id": "m"}),
        _ev(3, RunEventType.LLM_COMPLETED, {}, message_ids=["m-llm"]),
        _ev(4, RunEventType.TOOL_STARTED, {"call_id": call_id, "tool_name": tool_name, "status": "started"}),
        _ev(5, RunEventType.TOOL_COMPLETED, {"call_id": call_id, "status": tool_status}),
        _ev(6, RunEventType.RUN_COMPLETED),
    ]
    return synthetic_trace(events, final_message_id="m-llm")
