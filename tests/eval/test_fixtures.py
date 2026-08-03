"""Fixture Port 的 STRICT / NORMAL 匹配与默认拒绝行为。"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotclaw.runtime.application.dto import (
    ConversationMessage,
    ConversationSnapshot,
    DelegationRequest,
    RunRequest,
    ToolInvocation,
)
from dotclaw.runtime.domain.facts import ApprovalRecord, ApprovalStatus, MessageRole
from dotclaw.eval.fixtures import FixtureConfigurationError
from dotclaw.eval.models import FixtureMatchMode

from helpers import (
    approval_fixture,
    context_fixture,
    delegation_fixture,
    make_llm_fixture,
    llm_response,
    make_input,
    make_policy,
    tool_call,
    tool_fixture,
)

from dotclaw.eval.fixtures import (
    FixtureApprovalRepository,
    FixtureContextPort,
    FixtureDelegationPort,
    FixtureRunPolicyPort,
    FixtureToolPort,
    ScriptedLLMPort,
)


STRICT = FixtureMatchMode.STRICT
NORMAL = FixtureMatchMode.NORMAL


def _request(agent_id: str = "agent-eval") -> RunRequest:
    """构造最小运行请求。"""
    return RunRequest(
        session_id="sess-eval",
        lease_id="lease",
        agent_id=agent_id,
        user_message=make_input(),
        conversation=ConversationSnapshot(session_id="sess-eval", messages=(), version=0),
    )


def _tool_call_invocation(name: str, arguments: dict, call_id: str = "c1") -> ToolInvocation:
    """构造工具调用请求。"""
    return ToolInvocation(run_id="run-1", call=tool_call(call_id, name, arguments))


def _delegation_request(target_agent_id: str) -> DelegationRequest:
    """构造委派请求。"""
    return DelegationRequest(
        parent_run_id="run-1",
        root_run_id="run-1",
        target_agent_id=target_agent_id,
        input_message=make_input(),
    )


# --------------------------------------------------------------------------- #
# LLM（STRICT：顺序消费，额外/缺失均拒绝）
# --------------------------------------------------------------------------- #


async def test_llm_strict_extra_call_rejected() -> None:
    """超出脚本长度的额外 LLM 调用必须拒绝。"""
    port = ScriptedLLMPort(make_llm_fixture("llm-1", (llm_response("r1", content="a"),)), STRICT)
    await port.complete(None, None)
    try:
        await port.complete(None, None)
        raise AssertionError("额外 LLM 调用应当被拒绝")
    except FixtureConfigurationError:
        pass


async def test_llm_verify_fully_consumed() -> None:
    """完整消费脚本后校验通过；未消费则失败。"""
    port = ScriptedLLMPort(make_llm_fixture("llm-1", (llm_response("r1"), llm_response("r2"))), STRICT)
    try:
        port.verify_fully_consumed()  # 尚未消费应失败
        raise AssertionError("未消费应失败")
    except FixtureConfigurationError:
        pass
    await port.complete(None, None)
    try:
        port.verify_fully_consumed()
        raise AssertionError("未完整消费应失败")
    except FixtureConfigurationError:
        pass
    await port.complete(None, None)
    port.verify_fully_consumed()


# --------------------------------------------------------------------------- #
# Tool（STRICT：顺序 + 全参数精确；NORMAL：名称 + 关键参数）
# --------------------------------------------------------------------------- #


async def test_tool_strict_order_and_params() -> None:
    """STRICT 按记录顺序且全部参数一致消费。"""
    port = FixtureToolPort((
        tool_fixture("t1", "search", {"q": "x"}),
        tool_fixture("t2", "calc", {"a": 1}),
    ), STRICT)
    r1 = await port.execute(_tool_call_invocation("search", {"q": "x"}), None)
    assert r1.status.value == "completed"
    r2 = await port.execute(_tool_call_invocation("calc", {"a": 1}), None)
    assert r2.status.value == "completed"


async def test_tool_strict_wrong_order_rejected() -> None:
    """STRICT 顺序错误必须拒绝。"""
    port = FixtureToolPort((tool_fixture("t1", "search", {"q": "x"}), tool_fixture("t2", "calc", {"a": 1})), STRICT)
    try:
        await port.execute(_tool_call_invocation("calc", {"a": 1}), None)
        raise AssertionError("工具调用顺序错误应当被拒绝")
    except FixtureConfigurationError:
        pass


async def test_tool_strict_wrong_param_rejected() -> None:
    """STRICT 参数不一致必须拒绝。"""
    port = FixtureToolPort((tool_fixture("t1", "search", {"q": "x"}),), STRICT)
    try:
        await port.execute(_tool_call_invocation("search", {"q": "y"}), None)
        raise AssertionError("工具参数不一致应当被拒绝")
    except FixtureConfigurationError:
        pass


async def test_tool_strict_extra_call_rejected() -> None:
    """STRICT 额外工具调用必须拒绝。"""
    port = FixtureToolPort((tool_fixture("t1", "search", {"q": "x"}),), STRICT)
    await port.execute(_tool_call_invocation("search", {"q": "x"}), None)
    try:
        await port.execute(_tool_call_invocation("search", {"q": "x"}), None)
        raise AssertionError("额外工具调用应当被拒绝")
    except FixtureConfigurationError:
        pass


async def test_tool_strict_unknown_rejected() -> None:
    """STRICT 未知工具必须拒绝。"""
    port = FixtureToolPort((tool_fixture("t1", "search", {"q": "x"}),), STRICT)
    try:
        await port.execute(_tool_call_invocation("unknown", {}), None)
        raise AssertionError("未知工具应当被拒绝")
    except FixtureConfigurationError:
        pass


async def test_tool_normal_allows_undeclared_param() -> None:
    """NORMAL 允许未声明非关键参数变化。"""
    port = FixtureToolPort((tool_fixture("t1", "search", {"q": "x"}),), NORMAL)
    result = await port.execute(_tool_call_invocation("search", {"q": "x", "extra": 9}), None)
    assert result.status.value == "completed"


async def test_tool_normal_unknown_rejected() -> None:
    """NORMAL 仍拒绝未知工具。"""
    port = FixtureToolPort((tool_fixture("t1", "search", {"q": "x"}),), NORMAL)
    try:
        await port.execute(_tool_call_invocation("unknown", {}), None)
        raise AssertionError("NORMAL 下未知工具应当被拒绝")
    except FixtureConfigurationError:
        pass


async def test_tool_normal_key_mismatch_rejected() -> None:
    """NORMAL 关键参数不一致必须拒绝。"""
    port = FixtureToolPort((tool_fixture("t1", "search", {"q": "x"}),), NORMAL)
    try:
        await port.execute(_tool_call_invocation("search", {"q": "other"}), None)
        raise AssertionError("关键参数不一致应当被拒绝")
    except FixtureConfigurationError:
        pass


# --------------------------------------------------------------------------- #
# Approval（STRICT：顺序 + approval_id；NORMAL：按 approval_id 匹配）
# --------------------------------------------------------------------------- #


async def test_approval_strict_order() -> None:
    """STRICT 按记录顺序消费审批决议。"""
    repo = FixtureApprovalRepository((
        approval_fixture("a1", True, "appr-1"),
        approval_fixture("a2", False, "appr-2"),
    ), STRICT)
    assert repo.next_decision("appr-1") is True
    assert repo.next_decision("appr-2") is False
    try:
        repo.next_decision("appr-3")
        raise AssertionError("额外审批决议应当被拒绝")
    except FixtureConfigurationError:
        pass


async def test_approval_strict_order_mismatch_rejected() -> None:
    """STRICT 审批顺序不符必须拒绝。"""
    repo = FixtureApprovalRepository((approval_fixture("a1", True, "appr-1"),), STRICT)
    try:
        repo.next_decision("appr-other")
        raise AssertionError("审批顺序不符应当被拒绝")
    except FixtureConfigurationError:
        pass


async def test_approval_normal_matches_by_id() -> None:
    """NORMAL 按 approval_id 匹配决议。"""
    repo = FixtureApprovalRepository((
        approval_fixture("a1", True, "appr-1"),
        approval_fixture("a2", False, "appr-2"),
    ), NORMAL)
    assert repo.next_decision("appr-2") is False
    assert repo.next_decision("appr-1") is True


# --------------------------------------------------------------------------- #
# Delegation（STRICT：顺序 + 目标 Agent；NORMAL：按目标 Agent）
# --------------------------------------------------------------------------- #


async def test_delegation_strict_order() -> None:
    """STRICT 按记录顺序受理委派。"""
    port = FixtureDelegationPort((
        delegation_fixture("d1", "agent-b", "child-1"),
        delegation_fixture("d2", "agent-c", "child-2"),
    ), STRICT)
    sub = await port.submit(_delegation_request("agent-b"))
    assert sub.child_run_id == "child-1"
    sub2 = await port.submit(_delegation_request("agent-c"))
    assert sub2.child_run_id == "child-2"


async def test_delegation_strict_target_mismatch_rejected() -> None:
    """STRICT 委派目标顺序不符必须拒绝。"""
    port = FixtureDelegationPort((delegation_fixture("d1", "agent-b", "child-1"),), STRICT)
    try:
        await port.submit(_delegation_request("agent-x"))
        raise AssertionError("委派目标不符应当被拒绝")
    except FixtureConfigurationError:
        pass


async def test_delegation_result_unknown_child_rejected() -> None:
    """未受理的子运行查询结果必须拒绝。"""
    port = FixtureDelegationPort((delegation_fixture("d1", "agent-b", "child-1"),), STRICT)
    try:
        await port.result("unsubmitted")
        raise AssertionError("未受理子运行应当被拒绝")
    except FixtureConfigurationError:
        pass


async def test_delegation_normal_matches_by_target() -> None:
    """NORMAL 按目标 Agent 匹配委派。"""
    port = FixtureDelegationPort((delegation_fixture("d1", "agent-b", "child-1"),), NORMAL)
    sub = await port.submit(_delegation_request("agent-b"))
    assert sub.child_run_id == "child-1"


# --------------------------------------------------------------------------- #
# Policy / Context（冻结，默认拒绝）
# --------------------------------------------------------------------------- #


async def test_policy_resolves_frozen() -> None:
    """策略 Fixture 返回冻结策略；Agent 不符必须拒绝。"""
    policy = make_policy("agent-eval")
    port = FixtureRunPolicyPort(policy)
    assert (await port.resolve(_request("agent-eval"))).agent_id == "agent-eval"
    try:
        await port.resolve(_request("other-agent"))
        raise AssertionError("策略 Agent 不符应当被拒绝")
    except FixtureConfigurationError:
        pass


async def test_context_builds_in_order() -> None:
    """上下文 Fixture 按记录顺序构建；额外构建必须拒绝。"""
    port = FixtureContextPort((context_fixture("ctx-1"), context_fixture("ctx-2")), STRICT)
    assert (await port.build(_request(), None)).metadata.estimated_tokens == 1
    assert (await port.build(_request(), None)).metadata.estimated_tokens == 1
    try:
        await port.build(_request(), None)
        raise AssertionError("额外上下文构建应当被拒绝")
    except FixtureConfigurationError:
        pass
    port.verify_fully_consumed()
