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
from dotclaw.runtime.adapters.run_repository import RunRepositoryAdapter
from dotclaw.runtime.adapters.session_conversation_projector import SessionConversationProjector
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
        return ToolResult(output=f"ok:{name}")


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
        await service.submit(session.id, "你好", text_stream_port=None)


# ============================================================================
# 契约 2：并发提交使用不同输出收集器不串流（阶段 3）
# ============================================================================

@pytest.mark.phase0_contract
async def test_concurrent_submissions_do_not_cross_stream(tmp_path: Path) -> None:
    """共享同一 Runtime 的两个并发提交，各自只收到本 Run 的流式文本，不得串流。

    目标（开发计划阶段3）：输出端口仅属于本次提交/Run，Runtime 实例可被多个
    Channel 安全共享；LLMProxyAdapter 不再在构造期绑定单一 Channel，文本流只在
    ``complete(context, execution, text_stream_port)`` 调用时按提交隔离。

    本测试用真实 RuntimeEngine + SessionRunCoordinator + LLMProxyAdapter（旧 LLM
    替身）装配 Runtime，对两个不同 Session 并发提交、各自携带本次输出收集器，
    断言二者的文本流互不串扰。
    """
    from collections.abc import AsyncIterator
    from pathlib import Path as _Path

    from dotclaw.bootstrap.runtime_factory import build_runtime_services
    from dotclaw.bootstrap.session_interaction import SessionInteractionService
    from dotclaw.config.settings import Config
    from dotclaw.llm.base import ChatChunk, ChatTextDelta, TextDeltaKind, TokenUsage
    from dotclaw.tools.executor import ToolExecutor
    from dotclaw.tools.registry import ToolRegistry

    project_root: _Path = _Path(__file__).resolve().parents[2]

    class _PerSessionProxy:
        """统一返回固定文本的极简 LLM 替身。"""

        async def chat(self, messages, tools, model, stream) -> AsyncIterator[ChatChunk]:
            yield ChatChunk(text_deltas=(ChatTextDelta(TextDeltaKind.RESPONSE, "answer"),), finish_reason="stop", usage=TokenUsage(1, 1))

    config = Config()
    config.session.directory = str(tmp_path)
    identity = AgentIdentity(agent_id="agent-1", agent_name="已知 Agent", model="qwen3.7-max")
    session_manager = SessionManager(tmp_path)
    registry = AgentRegistry()
    registry.register(identity)
    services = build_runtime_services(
        config=config,
        project_root=project_root,
        identity=identity,
        llm_proxy=_PerSessionProxy(),
        tool_executor=ToolExecutor(ToolRegistry()),
        session_manager=session_manager,
        skill_registry=None,
        memory_manager=None,
        agent_registry=registry,
    )
    service = SessionInteractionService(
        session_manager=session_manager,
        agent_registry=registry,
        coordinator=services.coordinator,
        default_agent_id=identity.agent_id,
    )

    collector_a: ChannelCollector = ChannelCollector()
    collector_b: ChannelCollector = ChannelCollector()
    session_a = await service.create_session(agent_id="agent-1")
    session_b = await service.create_session(agent_id="agent-1")

    async def submit_one(session_id: str, collector: ChannelCollector) -> str:
        return await service.submit(
            session_id, "你好", text_stream_port=ChannelTextStreamAdapter(collector)
        )

    # 两个 Session 并发提交，各自携带本次输出收集器（不同 Session 走不同串行锁）。
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
async def test_approved_run_recovery_does_not_rerequest_approval_after_restart(tmp_path: Path) -> None:
    """进程内状态清空（模拟重启）后，已批准的工具调用恢复时不得再次请求审批。

    开发计划阶段4 + 总体设计 §5.1：批准事实由 checkpoint/控制状态传递，
    Adapter 不保存恢复权威状态；重建 Adapter/Engine 后，引擎在恢复时显式以
    ``approved=True`` 重驱该调用，仅执行一次且不再等待审批。
    """
    executor = _FakeApprovalToolExecutor(requires_approval={"danger"})
    adapter: ToolExecutorAdapter = ToolExecutorAdapter(executor)
    view: RunExecutionView = _make_execution_view("run-1", "agent-1")
    call: ToolCall = ToolCall("c1", "danger", {})

    # 首次调用触发审批。
    first = await adapter.execute(ToolInvocation("run-1", call), view)
    assert first.status is ToolResultStatus.APPROVAL_REQUIRED

    # 模拟进程重启：重建 Adapter，进程内 _waiting_calls 已清空。
    # 引擎在恢复时以 approved=True 重驱该调用，不得再次请求审批。
    restarted: ToolExecutorAdapter = ToolExecutorAdapter(executor)
    second = await restarted.execute(ToolInvocation("run-1", call, approved=True), view)
    assert second.status is ToolResultStatus.COMPLETED


# ============================================================================
# 契约 4：多 Identity 的 context_slot_ids 均生效（阶段 4）
# ============================================================================

@pytest.mark.phase0_contract
async def test_multi_identity_context_slots_both_effective(tmp_path: Path) -> None:
    """Identity Registry 中所有 Agent 的 context_slot_ids 覆盖都应生效。

    目标（开发计划阶段4 修改项1）：将单 Identity 的 Context Plan 配置替换为
    基于完整 AgentRegistry 的构造；默认 Slot 配置与各 Identity 显式覆盖同时
    保留，且每个 Identity 的覆盖应对应到实际解析出来的 Context Plan 绑定
    （即实际 Context Bundle 的 Slot 来源）。
    """
    # 阶段4 新增构造器：基于完整 AgentRegistry 构造配置。
    from dotclaw.context import ContextDependencies
    from dotclaw.context.contracts import ContextOwnerSnapshot
    from dotclaw.context.defaults import build_context_provider
    from dotclaw.context.plan_resolver import build_context_plan_from_registry

    registry: AgentRegistry = AgentRegistry()
    # 仅声明 AGENT 拥有的 Slot（identity/tools/skills），避免解析时 Owner 不一致。
    registry.register(AgentIdentity(
        agent_id="a1", agent_name="A1", context_slot_ids=("identity", "skills")
    ))
    registry.register(AgentIdentity(
        agent_id="a2", agent_name="A2", context_slot_ids=("tools",)
    ))

    plan = build_context_plan_from_registry(registry)

    # 配置层：两个 Identity 的显式 Slot 覆盖都必须可被解析到；未声明的回退默认。
    assert plan.enabled_slot_ids(ContextOwner.AGENT, "a1") == ("identity", "skills")
    assert plan.enabled_slot_ids(ContextOwner.AGENT, "a2") == ("tools",)
    assert plan.enabled_slot_ids(ContextOwner.AGENT, "general") == ("identity", "tools", "skills")

    # 实际 Context Plan 层：用默认 Slot 注册表解析出每个 Identity 的绑定，
    # 验证覆盖确实对应到实际 Bundle 的 Slot 来源。
    provider = build_context_provider(ContextDependencies(plan_configuration=plan))
    a1_plan = provider._resolver.resolve({ContextOwner.AGENT: ContextOwnerSnapshot("a1", {})})
    a2_plan = provider._resolver.resolve({ContextOwner.AGENT: ContextOwnerSnapshot("a2", {})})
    general_plan = provider._resolver.resolve({ContextOwner.AGENT: ContextOwnerSnapshot("general", {})})

    assert tuple(binding.descriptor.slot_id for binding in a1_plan.bindings) == ("identity", "skills")
    assert tuple(binding.descriptor.slot_id for binding in a2_plan.bindings) == ("tools",)
    assert tuple(binding.descriptor.slot_id for binding in general_plan.bindings) == ("identity", "tools", "skills")


# ============================================================================
# 契约 5：活动 Session 删除被拒绝（阶段 5）
# ============================================================================

@pytest.mark.phase0_contract
async def test_active_session_deletion_is_rejected(tmp_path: Path) -> None:
    """存在非终态 Run 的 Session 删除必须被拒绝，要求先取消/重试/放弃。

    目标（开发计划阶段5 + 总体设计 §5.2）：删除是应用级流程，查询到有非终态
    Run 时拒绝删除，避免产生部分删除与孤儿数据。
    """
    from dotclaw.bootstrap.session_interaction import (
        SessionDeletionRejected,
        SessionInteractionService,
    )

    session_manager: SessionManager = SessionManager(tmp_path)
    registry: AgentRegistry = AgentRegistry()
    registry.register(AgentIdentity(agent_id="agent-1", agent_name="已知 Agent"))
    run_repository: RunRepositoryAdapter = RunRepositoryAdapter(
        tmp_path, SessionConversationProjector(session_manager)
    )
    service = SessionInteractionService(
        session_manager=session_manager,
        agent_registry=registry,
        coordinator=None,  # type: ignore[arg-type]
        run_repository=run_repository,
    )

    session = await session_manager.create(agent_id="agent-1")
    # 模拟一个非终态（运行中）Run 遗留的 run.json。
    run_dir: Path = tmp_path / session.id / "agent_runs" / "run-active"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        '{"run_id": "run-active", "session_id": "%s", "agent_id": "agent-1", "status": "running"}'
        % session.id,
        encoding="utf-8",
    )

    # 活动 Run 存在时删除必须被明确拒绝，且目录保持完整（不产生部分删除）。
    with pytest.raises(SessionDeletionRejected):
        await service.delete_session(session.id)
    assert (tmp_path / session.id).is_dir()


# ============================================================================
# 契约 5：终态 Session 删除清理完整目录（阶段 5）
# ============================================================================

@pytest.mark.phase0_contract
async def test_terminal_session_deletion_removes_session_directory_and_approvals(tmp_path: Path) -> None:
    """终态 Session 删除后，其完整目录（运行目录/消息/事件）与审批记录均不可查询。

    目标（开发计划阶段5 + 总体设计 §5.2）：应用级删除流程清理完整 Session 存储目录，
    并使该 Session 的待审批记录不可恢复。审批仓库根目录与 Host 同源（此处用 tmp_path），
    由 ApprovalRepositoryAdapter 独占文件布局，SessionManager 不直接了解。
    """
    from dotclaw.bootstrap.session_interaction import SessionInteractionService

    session_manager: SessionManager = SessionManager(tmp_path)
    registry: AgentRegistry = AgentRegistry()
    registry.register(AgentIdentity(agent_id="agent-1", agent_name="已知 Agent"))

    # 模拟该 Session 的待审批记录（审批仓库与 Host 同源根目录）。
    approval_repo: ApprovalRepositoryAdapter = ApprovalRepositoryAdapter(tmp_path)
    approval_id = "apr-run-1"

    service = SessionInteractionService(
        session_manager=session_manager,
        agent_registry=registry,
        coordinator=None,  # type: ignore[arg-type]
        approval_repository=approval_repo,
    )

    session = await session_manager.create(agent_id="agent-1")

    # 模拟终态 Run 遗留的运行目录与消息/事件文件。
    run_dir: Path = tmp_path / session.id / "agent_runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("{}", encoding="utf-8")
    (run_dir / "messages.json").write_text("{}", encoding="utf-8")

    await approval_repo.create(ApprovalRecord(
        approval_id=approval_id,
        run_id="run-1",
        session_id=session.id,
        status=ApprovalStatus.PENDING,
        created_at="2026-07-22T00:00:00Z",
        metadata={},
    ))
    assert await approval_repo.load(approval_id) is not None  # 清理前可查询

    # 应用级删除：清理运行目录与审批记录，不留孤儿数据。
    await service.delete_session(session.id)

    # 目标：Session 目录整体消失，且其审批记录不可再查询。
    assert not (tmp_path / session.id).exists()
    assert await approval_repo.load(approval_id) is None
