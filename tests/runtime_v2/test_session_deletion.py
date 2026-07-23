"""阶段 5：应用级 Session 删除协调流程测试（开发计划 §8 + 总体设计 §5.2）。

覆盖：活动 Run 拒绝删除、终态 Session 完整清理、幂等、审批按 Session 作用域清理、
Run/Session 范围 Context 缓存释放。使用真实仓储与最小替身，避免与生产装配耦合。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dotclaw.agent.identity import AgentIdentity
from dotclaw.bootstrap.session_interaction import (
    SessionDeletionRejected,
    SessionInteractionService,
)
from dotclaw.orchestration.registry import AgentRegistry
from dotclaw.runtime.adapters.approval_repository import ApprovalRepositoryAdapter
from dotclaw.runtime.adapters.run_repository import RunRepositoryAdapter
from dotclaw.runtime.adapters.session_conversation_projector import (
    SessionConversationProjector,
)
from dotclaw.runtime.domain.context import ContextOwner
from dotclaw.runtime.domain.facts import ApprovalRecord, ApprovalStatus
from dotclaw.session.session import SessionManager


def _registry() -> AgentRegistry:
    registry: AgentRegistry = AgentRegistry()
    registry.register(AgentIdentity(agent_id="agent-1", agent_name="已知 Agent"))
    return registry


def _service(
    tmp_path: Path,
    *,
    context_port: object | None = None,
) -> tuple[SessionManager, SessionInteractionService, RunRepositoryAdapter, ApprovalRepositoryAdapter]:
    session_manager: SessionManager = SessionManager(tmp_path)
    run_repository: RunRepositoryAdapter = RunRepositoryAdapter(
        tmp_path, SessionConversationProjector(session_manager)
    )
    approval_repository: ApprovalRepositoryAdapter = ApprovalRepositoryAdapter(tmp_path)
    service = SessionInteractionService(
        session_manager=session_manager,
        agent_registry=_registry(),
        coordinator=None,  # type: ignore[arg-type]
        run_repository=run_repository,
        approval_repository=approval_repository,
        context_port=context_port,  # type: ignore[arg-type]
    )
    return session_manager, service, run_repository, approval_repository


def _write_run(tmp_path: Path, session_id: str, run_id: str, status: str) -> None:
    """写入一个最小可解析的 run.json（status 控制是否终态）。"""
    run_dir: Path = tmp_path / session_id / "agent_runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        '{"run_id": "%s", "session_id": "%s", "agent_id": "agent-1", "status": "%s"}'
        % (run_id, session_id, status),
        encoding="utf-8",
    )


class FakeContextPort:
    """记录 release_scope 调用的替身（不持有真实缓存）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[ContextOwner, str]] = []

    async def release_scope(self, owner: ContextOwner, owner_key: str) -> None:
        """记录每次释放调用的 (owner, owner_key)。"""
        self.calls.append((owner, owner_key))

    async def release_all(self) -> None:
        """Host 关闭时使用，本测试不涉及。"""


async def test_active_run_rejection(tmp_path: Path) -> None:
    """存在非终态 Run 时删除被明确拒绝，且目录保持完整（不产生部分删除）。"""
    session_manager, service, _, _ = _service(tmp_path)
    session = await session_manager.create(agent_id="agent-1")
    _write_run(tmp_path, session.id, "run-1", "running")

    with pytest.raises(SessionDeletionRejected):
        await service.delete_session(session.id)
    assert (tmp_path / session.id).is_dir()


async def test_terminal_session_full_cleanup(tmp_path: Path) -> None:
    """终态 Session 删除后，完整目录与待审批记录均不可查询。"""
    session_manager, service, _, approval_repo = _service(tmp_path)
    session = await session_manager.create(agent_id="agent-1")
    _write_run(tmp_path, session.id, "run-1", "completed")  # 终态
    await approval_repo.create(ApprovalRecord(
        approval_id="apr-1",
        run_id="run-1",
        session_id=session.id,
        status=ApprovalStatus.PENDING,
        created_at="2026-07-22T00:00:00Z",
        metadata={},
    ))

    await service.delete_session(session.id)

    assert not (tmp_path / session.id).exists()
    assert await approval_repo.load("apr-1") is None


async def test_idempotent_when_session_missing(tmp_path: Path) -> None:
    """删除不存在的 Session 应幂等、不抛错（避免重复删除报错）。"""
    _, service, _, _ = _service(tmp_path)
    await service.delete_session("no-such-session")
    assert not (tmp_path / "no-such-session").exists()


async def test_approval_cleanup_scoped_to_session(tmp_path: Path) -> None:
    """按 Session 清理审批时，只移除目标 Session 的记录，保留其他 Session 的审批。"""
    session_manager, service, _, approval_repo = _service(tmp_path)
    s1 = await session_manager.create(agent_id="agent-1")
    s2 = await session_manager.create(agent_id="agent-1")
    await approval_repo.create(ApprovalRecord(
        approval_id="apr-s1", run_id="r1", session_id=s1.id,
        status=ApprovalStatus.PENDING, created_at="t", metadata={},
    ))
    await approval_repo.create(ApprovalRecord(
        approval_id="apr-s2", run_id="r2", session_id=s2.id,
        status=ApprovalStatus.PENDING, created_at="t", metadata={},
    ))

    await service.delete_session(s1.id)

    assert await approval_repo.load("apr-s1") is None
    assert await approval_repo.load("apr-s2") is not None


async def test_run_and_session_context_cache_released(tmp_path: Path) -> None:
    """删除终态 Session 时释放 Session 与 Run 范围的 Context 缓存。"""
    context_port = FakeContextPort()
    session_manager, service, _, _ = _service(tmp_path, context_port=context_port)
    session = await session_manager.create(agent_id="agent-1")
    _write_run(tmp_path, session.id, "run-1", "completed")

    await service.delete_session(session.id)

    assert (ContextOwner.SESSION, session.id) in context_port.calls
    assert (ContextOwner.RUN, "run-1") in context_port.calls


async def test_delete_session_keeps_shared_agent_cache_for_other_sessions(tmp_path: Path) -> None:
    """同一 Identity 下有两个 Session：删除其一，只释放目标 Session 与其 Run 的缓存，
    不释放 AGENT 范围缓存，也不误伤另一 Session（总体设计 §5.2 第 4 步）。

    AGENT 范围缓存的 owner_key 是 agent_id，跨同 Identity 的所有 Session 共享；
    随单 Session 删除释放会误伤其他活 Session 的上下文，因此仅在 Identity/Host
    生命周期终点（如 ApplicationHost.shutdown 的 release_all）释放。
    """
    context_port = FakeContextPort()
    session_manager, service, _, _ = _service(tmp_path, context_port=context_port)
    s1 = await session_manager.create(agent_id="agent-1")
    s2 = await session_manager.create(agent_id="agent-1")  # 同 Identity 的另一 Session
    _write_run(tmp_path, s1.id, "run-1", "completed")  # 仅被删 Session 拥有 Run

    await service.delete_session(s1.id)

    # 目标 Session 与其 Run 的缓存被释放
    assert (ContextOwner.SESSION, s1.id) in context_port.calls
    assert (ContextOwner.RUN, "run-1") in context_port.calls
    # AGENT 范围缓存（按 agent_id 共享）不得随单 Session 删除释放
    assert (ContextOwner.AGENT, "agent-1") not in context_port.calls
    # 同一 Identity 下另一 Session 的缓存不得被误伤
    assert (ContextOwner.SESSION, s2.id) not in context_port.calls

