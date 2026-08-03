"""EvalEnvironment 的隔离执行、默认拒绝真实依赖与环境间隔离。"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotclaw.eval.environment import EvalDependencies, EvalEnvironment
from dotclaw.eval.fixtures import FixtureConfigurationError
from dotclaw.eval.models import FixtureMatchMode
from dotclaw.runtime.application.dto import ToolResultStatus
from dotclaw.runtime.domain.state import Ended, RunOutcome

from helpers import (
    build_case,
    context_fixture,
    make_llm_fixture,
    llm_response,
    tool_call,
    tool_fixture,
)


# --------------------------------------------------------------------------- #
# 被调用即失败的替身：core 方法记录并抛错，生命周期方法仅记录
# --------------------------------------------------------------------------- #


class _FailIfCalled:
    """记录调用次数的基础替身。"""

    def __init__(self) -> None:
        self.core_calls: int = 0
        self.lifecycle_calls: int = 0


class _FailLLM(_FailIfCalled):
    async def complete(self, context, execution, output_port=None):
        self.core_calls += 1
        raise AssertionError("生产 LLM 被调用")

    async def cancel(self, run_id):
        self.lifecycle_calls += 1


class _FailTool(_FailIfCalled):
    async def execute(self, invocation, execution):
        self.core_calls += 1
        raise AssertionError("生产工具被调用")

    async def cancel(self, run_id):
        self.lifecycle_calls += 1


class _FailContext(_FailIfCalled):
    async def build(self, request, execution):
        self.core_calls += 1
        raise AssertionError("生产上下文被调用")

    async def release_scope(self, owner, owner_key):
        self.lifecycle_calls += 1

    async def release_all(self):
        self.lifecycle_calls += 1

    def request_refresh(self, slot_id, owner, owner_key):
        self.lifecycle_calls += 1

    def publish_signal(self, signal):
        self.lifecycle_calls += 1


class _FailPolicy(_FailIfCalled):
    async def resolve(self, request):
        self.core_calls += 1
        raise AssertionError("生产策略被调用")


class _FailDelegation(_FailIfCalled):
    async def submit(self, request):
        self.core_calls += 1
        raise AssertionError("生产委派被调用")

    async def result(self, child_run_id):
        self.core_calls += 1
        raise AssertionError("生产委派被调用")

    async def cancel(self, child_run_id):
        self.lifecycle_calls += 1


class _FailApproval(_FailIfCalled):
    async def create(self, record):
        self.core_calls += 1
        raise AssertionError("生产审批被调用")

    async def load(self, approval_id):
        self.core_calls += 1
        raise AssertionError("生产审批被调用")

    async def consume(self, approval_id):
        self.core_calls += 1
        raise AssertionError("生产审批被调用")


# --------------------------------------------------------------------------- #
# 验收 #4：固定 Policy / Context 驱动多轮 LLM
# --------------------------------------------------------------------------- #


async def test_multi_round_llm_driven_by_fixtures() -> None:
    """冻结策略与上下文应驱动“工具轮 + 终态”多轮执行。"""
    case = build_case(
        case_id="multi-round",
        context_fixtures=(context_fixture("ctx-1"), context_fixture("ctx-2")),
        llm_fixture=make_llm_fixture("llm-1", (
            llm_response("llm-resp-1", content="", tool_calls=(tool_call("c1", "search", {"q": "x"}),)),
            llm_response("llm-resp-2", content="最终答案"),
        )),
        tool_fixtures=(tool_fixture("tool-1", "search", {"q": "x"}, ToolResultStatus.COMPLETED, output="结果"),),
    )
    env = EvalEnvironment(case)
    outcome = await env.run()

    assert isinstance(outcome.state.mode, Ended)
    assert outcome.state.mode.outcome is RunOutcome.COMPLETED
    # 两轮 LLM：首轮带工具调用，次轮为最终答案
    assert env.scripted_llm.consumed == 2
    assert env.fixture_context.remaining == ()
    assert env.fixture_tool.remaining == ()
    # 所有 Fixture 已被完整消费
    env.verify_fixtures_consumed()


# --------------------------------------------------------------------------- #
# 验收 #5：每个真实 Port 都配置被调用即失败的替身，证明不回退生产依赖
# --------------------------------------------------------------------------- #


async def test_no_production_fallback() -> None:
    """完整 Fixture 覆盖下，注入的“被调用即失败”替身不应被触及。"""
    case = build_case(case_id="isolation")
    deps = EvalDependencies(
        llm_port=_FailLLM(),
        tool_port=_FailTool(),
        context_port=_FailContext(),
        policy_port=_FailPolicy(),
        delegation_port=_FailDelegation(),
        approval_repository=_FailApproval(),
    )
    env = EvalEnvironment(case, dependencies=deps)
    outcome = await env.run()

    assert isinstance(outcome.state.mode, Ended)
    assert outcome.state.mode.outcome is RunOutcome.COMPLETED

    stubs = [
        deps.llm_port,
        deps.tool_port,
        deps.context_port,
        deps.policy_port,
        deps.delegation_port,
        deps.approval_repository,
    ]
    for stub in stubs:
        assert stub.core_calls == 0, f"真实端口被回退调用：{stub!r}"


# --------------------------------------------------------------------------- #
# 验收 #6：两次独立 Environment 不共享 Run / Checkpoint / Fixture 游标
# --------------------------------------------------------------------------- #


async def test_two_environments_isolated() -> None:
    """两个独立环境应使用独立仓储与独立 Fixture 消费游标。"""
    case = build_case(case_id="isolated")
    env1 = EvalEnvironment(case)
    out1 = await env1.run()
    env2 = EvalEnvironment(case)
    out2 = await env2.run()

    # 仓储实例互不共享
    assert env1.run_repository is not env2.run_repository
    assert env1.checkpoint_repository is not env2.checkpoint_repository
    assert env1.scripted_llm is not env2.scripted_llm
    # 各自独立消费 Fixture 游标
    assert env1.scripted_llm.consumed == 1
    assert env2.scripted_llm.consumed == 1
    # 互不包含对方运行
    run1 = await env1.run_repository.load_run(case.conversation_fixture.session_id, out1.run_id)
    assert run1 is not None
    assert await env2.run_repository.load_run(case.conversation_fixture.session_id, out1.run_id) is None
    assert out1.run_id != out2.run_id


# --------------------------------------------------------------------------- #
# 默认拒绝：未完整消费的 Fixture 必须被校验捕获（缺失调用）
# --------------------------------------------------------------------------- #


async def test_missing_fixture_detected_on_verify() -> None:
    """脚本声明 2 条 LLM 响应但仅消耗 1 条时，校验必须失败。"""
    case = build_case(
        case_id="missing",
        llm_fixture=make_llm_fixture("llm-1", (
            llm_response("llm-resp-1", content="done"),
            llm_response("llm-resp-2", content="unused"),
        )),
    )
    env = EvalEnvironment(case)
    outcome = await env.run()
    assert isinstance(outcome.state.mode, Ended)
    assert outcome.state.mode.outcome is RunOutcome.COMPLETED
    # 仅消耗首条响应，第二条 LLM fixture 未被消费
    assert env.scripted_llm.consumed == 1
    try:
        env.verify_fixtures_consumed()
        raise AssertionError("未消费的 Fixture 应被校验捕获")
    except FixtureConfigurationError:
        pass
