"""EvalEnvironment 的隔离执行、默认拒绝真实依赖与环境间隔离。"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotclaw.eval.environment import EvalDependencies, EvalEnvironment
from dotclaw.eval.fixtures import FixtureConfigurationError
from dotclaw.eval.models import ExecutionMode, FixtureMatchMode
from dotclaw.runtime.application.dto import ToolResult, ToolResultStatus
from dotclaw.runtime.domain.facts import MessageRole, RunMessage, RunMessageKind
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


class _RecordLLM(_FailLLM):
    """记录调用并返回合法最终响应的真实 LLM 替身（用于正向回退验证）。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: int = 0

    async def complete(self, context, execution, output_port=None):
        self.calls += 1
        return RunMessage(
            message_id="real-llm",
            sequence=1,
            kind=RunMessageKind.FINAL_RESPONSE,
            role=MessageRole.ASSISTANT,
            content="real answer",
            tool_calls=(),
        )


class _RecordTool(_FailTool):
    """记录调用并返回合法结果的真实工具替身（用于正向回退验证）。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: int = 0

    async def execute(self, invocation, execution):
        self.calls += 1
        return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED, output="real tool result")


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
    """Re-execution 下完整 Fixture 覆盖时，注入的“被调用即失败”替身不应被触及。

    注意：Playback 已禁止注入真实依赖（见 test_playback_rejects_real_dependencies），
    因此该“fixture 全命中则不回退”的反向验证只在允许回退的 Re-execution 下进行。
    """
    case = build_case(case_id="isolation", execution_mode=ExecutionMode.REEXECUTION)
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
# 隔离边界反向测试：Playback 禁止回退真实依赖（用户指出的缺口）
# --------------------------------------------------------------------------- #


async def test_playback_rejects_real_dependencies() -> None:
    """Playback 模式下即便注入了真实依赖，构造时也必须明确拒绝。"""
    case = build_case(case_id="playback-forbid", execution_mode=ExecutionMode.PLAYBACK)
    deps = EvalDependencies(llm_port=_FailLLM(), tool_port=_FailTool())
    try:
        EvalEnvironment(case, dependencies=deps)
        raise AssertionError("Playback 不应接受真实依赖")
    except FixtureConfigurationError:
        pass


async def test_playback_missing_fixture_fails_do_not_silent_succeed() -> None:
    """Playback 下 Fixture 缺失/不匹配必须明确失败，而非静默成功。"""
    case = build_case(
        case_id="playback-missing-tool",
        execution_mode=ExecutionMode.PLAYBACK,
        llm_fixture=make_llm_fixture("llm-1", (
            llm_response("llm-resp-1", content="", tool_calls=(tool_call("c1", "search", {"q": "x"}),)),
        )),
        tool_fixtures=(),  # 没有对应的工具 fixture
    )
    env = EvalEnvironment(case)
    outcome = await env.run()
    # 未匹配的工具调用必须导致失败，绝不能伪装成完成
    assert isinstance(outcome.state.mode, Ended)
    assert outcome.state.mode.outcome is RunOutcome.FAILED


async def test_playback_composite_never_falls_back_to_real() -> None:
    """即使组合端口装配了真实端口，Playback（禁止回退）也必须拒绝而非调用真实端口。"""
    from dotclaw.eval.environment import _LLMComposite
    from dotclaw.eval.fixtures import ScriptedLLMPort

    real = _RecordLLM()
    fixture = ScriptedLLMPort(
        make_llm_fixture("llm-1", (llm_response("r1", content="a"),)),
        FixtureMatchMode.STRICT,
    )
    composite = _LLMComposite(fixture, real, allow_fallback=False)
    await composite.complete(None, None)  # 消费唯一 fixture
    try:
        await composite.complete(None, None)  # 耗尽后应拒绝
        raise AssertionError("禁止回退的组合端口不应调用真实端口")
    except FixtureConfigurationError:
        pass
    assert real.calls == 0, "真实 LLM 端口被错误回退调用"


async def test_reexecution_falls_back_to_real_port() -> None:
    """Re-execution 下 Fixture 缺失时允许回退到注入的真实端口（设计允许的行为）。"""
    real_llm = _RecordLLM()
    real_tool = _RecordTool()
    case = build_case(
        case_id="reexec-fallback",
        execution_mode=ExecutionMode.REEXECUTION,
        context_fixtures=(context_fixture("ctx-1"), context_fixture("ctx-2")),
        llm_fixture=make_llm_fixture("llm-1", (
            llm_response("llm-resp-1", content="", tool_calls=(tool_call("c1", "search", {"q": "x"}),)),
        )),
        tool_fixtures=(),
    )
    deps = EvalDependencies(llm_port=real_llm, tool_port=real_tool)
    env = EvalEnvironment(case, dependencies=deps)
    outcome = await env.run()

    assert isinstance(outcome.state.mode, Ended)
    assert outcome.state.mode.outcome is RunOutcome.COMPLETED
    # 缺失的工具 fixture 回退到真实工具端口，LLM 第二轮也回退到真实 LLM 端口
    assert env.tool_port.real_served == 1
    assert env.llm_port.real_served == 1
    assert env.tool_port.fixture_served == 0
    env.verify_fixtures_consumed()


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
