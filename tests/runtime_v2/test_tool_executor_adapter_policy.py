"""ToolExecutorAdapter 预检尊重 Agent 级收窄策略的回归测试。

背景（阶段五安全审计三次复盘 P0）：Adapter 曾调用 requires_approval(name) 时
不传 agent_id，仅按全局规则判断；当某 Agent 把全局 allow 的 workspace.read 收窄
为 ask 时，预检返回 False，Adapter 随即 execute_approved(pre_approved=True) 直接
执行，绕过"Agent 只能收窄"与"无审批不得执行"的安全要求。

本文件验证：requires_approval 与 Adapter 预检均按 per-run agent_id 的有效策略判断，
受限 Agent 的首次调用返回 APPROVAL_REQUIRED，获批后才执行；未收窄 Agent 仍直接执行。
"""

from __future__ import annotations

from dotclaw.runtime.adapters import ToolExecutorAdapter
from dotclaw.runtime.application.dto import ToolCall, ToolInvocation, ToolResultStatus
from dotclaw.runtime.application.execution import RunBudget, RunExecutionView
from dotclaw.runtime.domain.facts import AgentPolicySnapshot
from dotclaw.runtime.domain.state import AgentState
from dotclaw.tools.approval import ApprovalManager
from dotclaw.tools.decorator import ToolPolicy, get_tool_meta, tool
from dotclaw.tools.executor import ToolExecutor
from dotclaw.tools.function_handler import FunctionToolHandler
from dotclaw.tools.policy import PolicyDecision, PolicyEngine, PolicyScope
from dotclaw.tools.registry import ToolRegistry
from pydantic import BaseModel

_executions: list[str] = []


class _PathArgs(BaseModel):
    path: str = "a.txt"


def _reader_tool() -> FunctionToolHandler:
    """构造一个档案为 workspace.read 的只读工具，记录实际执行。"""

    @tool(name="reader", description="读取文件", policy=ToolPolicy.WORKSPACE_READ, args_model=_PathArgs)
    async def reader(args: _PathArgs, context):
        _executions.append(args.path)
        return f"read:{args.path}"

    return FunctionToolHandler(reader, get_tool_meta(reader))


def _build_executor() -> ToolExecutor:
    """全局 workspace.read=allow，但 narrow-agent 收窄为 ask。"""
    registry = ToolRegistry()
    registry.register(_reader_tool())
    scope = PolicyScope(
        global_rules={"workspace.read": PolicyDecision.ALLOW},
        workspace_root=".",
    )

    def resolver(agent_id: str) -> dict[str, str] | None:
        if agent_id == "narrow-agent":
            return {"workspace.read": "ask"}
        return None

    return ToolExecutor(
        registry,
        ApprovalManager(),
        PolicyEngine(scope),
        agent_policy_resolver=resolver,
    )


def _execution(agent_id: str) -> RunExecutionView:
    return RunExecutionView(
        "run-1",
        AgentPolicySnapshot(agent_id, "v1", "model", 3),
        AgentState(),
        RunBudget(3),
        0,
        None,
    )


def _invocation() -> ToolInvocation:
    return ToolInvocation("run-1", ToolCall("call-1", "reader", {"path": "a.txt"}))


async def test_narrowed_agent_requires_approval_then_executes() -> None:
    """受限 Agent 首次调用返回 APPROVAL_REQUIRED，二次（获批）才执行。"""
    executor = _build_executor()
    port = ToolExecutorAdapter(executor)
    execution = _execution("narrow-agent")
    invocation = _invocation()

    _executions.clear()
    first = await port.execute(invocation, execution)
    # 首次审批前不得执行工具（证伪"预检绕过导致直接执行"）。
    assert first.status is ToolResultStatus.APPROVAL_REQUIRED
    assert _executions == []
    second = await port.execute(invocation, execution)
    # 二次（获批）才执行一次。
    assert second.status is ToolResultStatus.COMPLETED
    assert len(_executions) == 1


async def test_non_narrowed_agent_executes_without_approval() -> None:
    """未收窄 Agent 直接执行，不触发审批。"""
    executor = _build_executor()
    port = ToolExecutorAdapter(executor)
    execution = _execution("wide-agent")
    invocation = _invocation()

    _executions.clear()
    result = await port.execute(invocation, execution)

    # 未收窄 Agent 直接执行（不触发审批），且工具实际执行一次。
    assert result.status is ToolResultStatus.COMPLETED
    assert "a.txt" in result.output
    assert len(_executions) == 1
