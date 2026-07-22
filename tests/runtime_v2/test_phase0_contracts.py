"""ApplicationHost 收口 · 阶段 0 基线与契约测试。

依据《runtime-application-host收口开发计划.md》§3：冻结现有 Runtime v4 的有效行为，
为后续阶段（1–5）迁移建立最小契约测试。

本文件新增 5 项计划要求的契约（活动 Session 删除拆分为拒绝/清理两个角度，共 7 个测试）：
  1. 新 Session 必须写入非空 `agent_id`；未知 Identity 不得提交；
  2. 两个并发提交使用不同输出收集器时不得串流；
  3. 进程内状态清空（重启）后，已批准 Run 恢复不得再次请求审批；
  4. 两个 Identity 的不同 `context_slot_ids` 都应生效；
  5. 删除活动 Session 被拒绝；终态 Session 删除后其运行目录与审批记录均不可查询。

阶段 0 不修改生产代码：这些契约在旧实现上**明确失败（xfail）或标注待迁移**，
既有 Runtime 核心测试维持通过。后续阶段实现对应能力后，应移除相关 xfail 使其转为通过。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from dotclaw.agent.identity import AgentIdentity
from dotclaw.channel.runtime_text_stream import ChannelTextStreamAdapter
from dotclaw.orchestration.registry import AgentRegistry
from dotclaw.runtime.adapters.approval_repository import ApprovalRepositoryAdapter
from dotclaw.runtime.adapters.tool_executor_adapter import ToolExecutorAdapter
from dotclaw.runtime.application.dto import (
    RunRequest,
    ToolInvocation,
    ToolResultStatus,
)
from dotclaw.runtime.application.execution import RunBudget, RunExecutionView
from dotclaw.runtime.domain.context import ContextOwner
from dotclaw.runtime.domain.facts import (
    AgentPolicySnapshot,
    ApprovalRecord,
    ApprovalStatus,
    ToolCall,
)
from dotclaw.runtime.domain.state import AgentState
from dotclaw.session.session import SessionManager
from dotclaw.tools.base import ToolExecutionContext, ToolResult


# ============================================================================
# 测试替身与最小构造辅助
# ============================================================================

class ChannelCollector:
    """验证 Runtime 到 Channel 的文本流转发，不依赖真实终端。"""

    def __init__(self) -> None:
        """初始化收集到的文本块。"""
        self.chunks: list[str] = []

    async def receive(self) -> str:
        """本测试不读取用户输入。"""
        return ""

    async def send(self, message: str) -> None:
        """本测试不使用非流式发送。"""

    async def stream(self, chunk: str) -> None:
        """记录 Runtime 转发的文本块。"""
        self.chunks.append(chunk)

    async def ask_user(self, prompt: str) -> str:
        """本测试不触发交互式审批。"""
        return ""


class _FakeApprovalToolExecutor:
    """最小工具执行器替身：对指定工具名要求审批，其余直接成功执行。"""

    def __init__(self, requires_approval: set[str]) -> None:
        """记录需要审批的工具名集合。"""
        self._requires: set[str] = requires_approval

    def requires_approval(
        self, name: str, execution_context: ToolExecutionContext | None = None
    ) -> bool:
        """仅对声明的工具名要求审批。"""
        return name in self._requires

    async def execute_approved(
        self,
        name: str,
        arguments: dict,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        """返回成功结果，供适配器包装为 Runtime 标准结果。"""
        return ToolResult(name, ToolResultStatus.COMPLETED, output=f"ok:{name}")


def _make_execution_view(run_id: str, agent_id: str) -> RunExecutionView:
    """构造适配 ToolExecutorAdapter.execute 所需的最小执行视图。"""
    policy: AgentPolicySnapshot = AgentPolicySnapshot(
        agent_id=agent_id,
        identity_version="v1",
        model_id="qwen3.7-max",
        max_iterations=10,
    )
    return RunExecutionView(
        run_id=run_id,
        policy=policy,
        state=AgentState(),
        budget=RunBudget(max_iterations=10),
        message_cursor=0,
        pending_control=None,
    )


# ============================================================================
# 契约 1：新 Session 必须写入非空 agent_id（阶段 1）
# ============================================================================

@pytest.mark.phase0_contract
async def test_session_creation_requires_non_empty_agent_id(tmp_path: Path) -> None:
    """新 Session 必须持久化非空 agent_id；未指定时必须被拒绝。

    目标（开发计划阶段1）：SessionManager.create() 的 agent_id 改为必填，
    空值不得落盘；Host 负责显式优先、默认兜底的 Identity 选择并在创建时写入。
    """
    session_manager: SessionManager = SessionManager(tmp_path)

    # 目标：未指定 agent_id 的创建必须被拒绝（agent_id 为 Session 必填绑定）。
    with pytest.raises(ValueError):
        await session_manager.create(agent_id="")

    # 指定有效 agent_id 时必须落盘且非空，重载后保持一致。
    session = await session_manager.create(agent_id="agent-1")
    reloaded = await session_manager.load(session.id)
    assert reloaded is not None
    assert reloaded.agent_id == "agent-1"


# ============================================================================
# 契约 1：未知 Identity 不得提交（阶段 1）
# ============================================================================

@pytest.mark.phase0_contract
async def test_unknown_identity_submission_is_rejected(tmp_path: Path) -> None:
    """绑定未知 Identity 的 Session 不得经入口提交，必须返回明确错误。

    目标（开发计划阶段1）：SessionInteractionService 读取 session.agent_id，
    在 AgentRegistry 中验证 Identity；未知或空 Identity 必须返回明确错误，
    不能回退到默认 Identity。
    """
    from dotclaw.bootstrap.session_interaction import SessionInteractionService

    session_manager: SessionManager = SessionManager(tmp_path)
    registry: AgentRegistry = AgentRegistry()
    registry.register(AgentIdentity(agent_id="agent-1", agent_name="已知 Agent"))

    service = SessionInteractionService(
        session_manager=session_manager,
        agent_registry=registry,
        coordinator=None,  # type: ignore[arg-type]
    )

    # 创建绑定未知 Identity 的 Session。
    session = await session_manager.create(agent_id="ghost-agent")

    # 入口必须在提交前校验 session.agent_id 属于已注册 Identity，拒绝未知身份。
    with pytest.raises(ValueError):
        await service.submit(session.id, "你好", output_port=None)


# ============================================================================
# 契约 2：并发提交使用不同输出收集器不串流（阶段 3）
# ============================================================================

@pytest.mark.phase0_contract
@pytest.mark.xfail(strict=True, reason="阶段0契约：待阶段3实现 Run 级输出端口")
async def test_concurrent_submissions_do_not_cross_stream(tmp_path: Path) -> None:
    """共享同一 Runtime 的两个并发提交，各自只收到本 Run 的流式文本，不得串流。

    目标（开发计划阶段3）：输出端口仅属于本次提交/Run，Runtime 实例可被多个
    Channel 安全共享；LLMProxyAdapter 不再在构造期绑定单一 Channel。

    当前实现：LLMProxyAdapter 在构造期绑定单个 TextStreamPort，无法按提交隔离。
    """
    from collections.abc import AsyncIterator as _AsyncIterator

    # 延迟导入：目标组件在阶段2/3新增。
    from dotclaw.bootstrap.application_host import ApplicationHost

    class _PerSessionProxy:
        """按提交返回固定文本的极简 LLM 替身。"""

        async def chat(self, messages, tools, model, stream) -> _AsyncIterator:
            yield type("C", (), {"content": "answer", "is_final": True,
                                  "input_tokens": 1, "output_tokens": 1})()

    # 预期装配形态（待目标组件落地后校准）。
    host = ApplicationHost.build(  # type: ignore[attr-defined]
        config=None,  # type: ignore[arg-type]
        project_root=tmp_path,
        llm_proxy=_PerSessionProxy(),
        tool_executor=None,  # type: ignore[arg-type]
    )
    service = host.session_interaction

    collector_a: ChannelCollector = ChannelCollector()
    collector_b: ChannelCollector = ChannelCollector()
    session_a = await service.create_session(agent_id="agent-1")
    session_b = await service.create_session(agent_id="agent-2")

    async def submit_one(session_id: str, collector: ChannelCollector) -> str:
        return await service.submit(
            session_id, "你好", output_port=ChannelTextStreamAdapter(collector)
        )

    # 两个 Channel 并发提交，各自携带本次输出收集器。
    await asyncio.gather(
        submit_one(session_a.id, collector_a),
        submit_one(session_b.id, collector_b),
    )

    # 每个收集器只应收到本 Run 的分片，绝不得串流到对方的 Channel。
    assert collector_a.chunks == ["answer"]
    assert collector_b.chunks == ["answer"]


# ============================================================================
# 契约 3：重启后已批准 Run 恢复不重复请求审批（阶段 4）
# ============================================================================

@pytest.mark.phase0_contract
@pytest.mark.xfail(
    strict=True,
    reason="阶段0契约：待阶段4将审批权威从进程内 _waiting_calls 改为持久化 checkpoint/控制状态",
)
async def test_approved_run_recovery_does_not_rerequest_approval_after_restart(tmp_path: Path) -> None:
    """进程内状态清空（模拟重启）后，已批准的工具调用恢复时不得再次请求审批。

    目标（开发计划阶段4 + 总体设计 §5.1）：批准事实由 checkpoint/控制状态传递，
    Adapter 不保存恢复权威状态；重建 Adapter/Engine 后，同一审批通过仅执行一次工具，
    且不再等待审批。

    当前实现：ToolExecutorAdapter 以进程内 `_waiting_calls` 集合作为恢复权威，
    重建实例后该集合为空，恢复会再次返回 APPROVAL_REQUIRED。
    """
    executor = _FakeApprovalToolExecutor(requires_approval={"danger"})
    adapter: ToolExecutorAdapter = ToolExecutorAdapter(executor)
    view: RunExecutionView = _make_execution_view("run-1", "agent-1")
    call: ToolCall = ToolCall("c1", "danger", {})

    # 首次调用触发审批。
    first = await adapter.execute(ToolInvocation("run-1", call), view)
    assert first.status is ToolResultStatus.APPROVAL_REQUIRED

    # 模拟进程重启：重建 Adapter，进程内 _waiting_calls 已清空。
    # 同一审批已被持久化消费，恢复不得再次请求审批。
    restarted: ToolExecutorAdapter = ToolExecutorAdapter(executor)
    second = await restarted.execute(ToolInvocation("run-1", call), view)
    assert second.status is not ToolResultStatus.APPROVAL_REQUIRED


# ============================================================================
# 契约 4：多 Identity 的 context_slot_ids 均生效（阶段 4）
# ============================================================================

@pytest.mark.phase0_contract
@pytest.mark.xfail(
    strict=True,
    reason="阶段0契约：待阶段4实现基于完整 AgentRegistry 的 Context Plan 覆盖",
)
async def test_multi_identity_context_slots_both_effective(tmp_path: Path) -> None:
    """Identity Registry 中所有 Agent 的 context_slot_ids 覆盖都应生效。

    目标（开发计划阶段4）：将单 Identity 的 Context Plan 配置替换为基于完整
    AgentRegistry 的构造；默认 Slot 配置与各 Identity 显式覆盖同时保留。

    当前实现：build_runtime_services 仅按单个 identity 配置其 Slot 覆盖，
    其余 Identity 的 context_slot_ids 不生效。
    """
    # 延迟导入：目标构造器在阶段4新增（名称以最终实现为准）。
    from dotclaw.context.plan_resolver import build_context_plan_from_registry

    registry: AgentRegistry = AgentRegistry()
    registry.register(AgentIdentity(
        agent_id="a1", agent_name="A1", context_slot_ids=("skills", "memory")
    ))
    registry.register(AgentIdentity(
        agent_id="a2", agent_name="A2", context_slot_ids=("tools",)
    ))

    plan = build_context_plan_from_registry(registry)

    # 两个 Identity 的显式 Slot 覆盖都必须可被解析到。
    assert plan.enabled_slot_ids(ContextOwner.AGENT, "a1") == ("skills", "memory")
    assert plan.enabled_slot_ids(ContextOwner.AGENT, "a2") == ("tools",)


# ============================================================================
# 契约 5：活动 Session 删除被拒绝（阶段 5）
# ============================================================================

@pytest.mark.phase0_contract
@pytest.mark.xfail(strict=True, reason="阶段0契约：待阶段5实现应用级 Session 删除协调流程")
async def test_active_session_deletion_is_rejected(tmp_path: Path) -> None:
    """存在非终态 Run 的 Session 删除必须被拒绝，要求先取消/重试/放弃。

    目标（开发计划阶段5 + 总体设计 §5.2）：删除是应用级流程，查询到有非终态
    Run 时拒绝删除，避免产生部分删除与孤儿数据。

    当前实现：SessionManager.delete 不感知 Run 状态，任何情况下都直接删。
    """
    from dotclaw.bootstrap.session_interaction import (
        SessionDeletionRejected,
        SessionInteractionService,
    )

    session_manager: SessionManager = SessionManager(tmp_path)
    registry: AgentRegistry = AgentRegistry()
    registry.register(AgentIdentity(agent_id="agent-1", agent_name="已知 Agent"))

    service = SessionInteractionService(
        session_manager=session_manager,
        agent_registry=registry,
        coordinator=None,  # type: ignore[arg-type]
    )

    session = await session_manager.create(agent_id="agent-1")
    # 启动一个活动（非终态）Run 后，删除必须被明确拒绝。
    # 阶段5前 delete_session 尚未实现（属性缺失），本契约因此 xfail；
    # 阶段5实现后须抛出 SessionDeletionRejected，届时移除 xfail 即可收敛。
    with pytest.raises(SessionDeletionRejected):
        await service.delete_session(session.id)


# ============================================================================
# 契约 5：终态 Session 删除清理完整目录（阶段 5）
# ============================================================================

@pytest.mark.phase0_contract
@pytest.mark.xfail(
    strict=True,
    reason="阶段0契约：待阶段5实现应用级删除协调流程，清理完整目录与审批记录",
)
async def test_terminal_session_deletion_removes_session_directory_and_approvals(tmp_path: Path) -> None:
    """终态 Session 删除后，其完整目录（运行目录/消息/事件）与审批记录均不可查询。

    目标（开发计划阶段5 + 总体设计 §5.2）：应用级删除流程清理完整 Session 存储目录，
    并使该 Session 的待审批记录不可恢复。当前实现：删除仅删 session.json，
    运行目录与审批记录均残留（且 ApprovalRepository 暂无以 Session 清理的最小方法）。

    说明：审批仓库根目录与 Host 同源（此处用 tmp_path）；阶段5实现后若 Coordinator
    使用不同的审批仓库根，本契约的清理断言需按真实装配校准。
    """
    from dotclaw.bootstrap.session_interaction import SessionInteractionService

    session_manager: SessionManager = SessionManager(tmp_path)
    registry: AgentRegistry = AgentRegistry()
    registry.register(AgentIdentity(agent_id="agent-1", agent_name="已知 Agent"))

    service = SessionInteractionService(
        session_manager=session_manager,
        agent_registry=registry,
        coordinator=None,  # type: ignore[arg-type]
    )

    session = await session_manager.create(agent_id="agent-1")

    # 模拟终态 Run 遗留的运行目录与消息/事件文件。
    run_dir: Path = tmp_path / session.id / "agent_runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("{}", encoding="utf-8")
    (run_dir / "messages.json").write_text("{}", encoding="utf-8")

    # 模拟该 Session 的待审批记录（审批仓库与 Host 同源根目录）。
    approval_repo: ApprovalRepositoryAdapter = ApprovalRepositoryAdapter(tmp_path)
    approval_id = "apr-run-1"
    await approval_repo.create(ApprovalRecord(
        approval_id=approval_id,
        run_id="run-1",
        session_id=session.id,
        status=ApprovalStatus.PENDING,
        created_at="2026-07-22T00:00:00Z",
        metadata={},
    ))

    # 应用级删除：拒绝孤儿数据，清理运行目录与审批记录。
    await service.delete_session(session.id)

    # 目标：Session 目录整体消失，且其审批记录不可再查询。
    assert not (tmp_path / session.id).exists()
    assert await approval_repo.load(approval_id) is None
