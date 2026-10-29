"""Session 级显式取消入口测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dotclaw.agent.identity import AgentIdentity
from dotclaw.bootstrap.session_interaction import SessionInteractionService
from dotclaw.orchestration.registry import AgentRegistry
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, AgentRun
from dotclaw.runtime.domain.state import AgentRunState, RunStage, Running
from dotclaw.session.session import SessionManager


class _FakeRunRepository:
    """返回预设活动 Run 的仓储替身。"""

    def __init__(self, runs: tuple[AgentRun, ...]) -> None:
        self.runs: tuple[AgentRun, ...] = runs

    async def list_active_runs(self, session_id: str) -> tuple[AgentRun, ...]:
        """只返回属于目标 Session 的预设活动运行。"""
        return tuple(run for run in self.runs if run.session_id == session_id)


class _FakeCoordinator:
    """记录实际 Run 取消调用。"""

    def __init__(self) -> None:
        self.cancel_calls: list[tuple[str, str]] = []

    async def cancel(self, run_id: str, reason: str) -> None:
        """记录取消目标与原因。"""
        self.cancel_calls.append((run_id, reason))


def _run(run_id: str, session_id: str = "session-1") -> AgentRun:
    """构造最小活动 Run。"""
    return AgentRun(
        run_id=run_id,
        session_id=session_id,
        agent_id="default",
        state=AgentRunState(mode=Running(RunStage.CALLING_LLM)),
        started_at="2026-09-01T09:00:00+08:00",
        policy=AgentPolicySnapshot("default", "v1", "chat-model", 10),
        input_message_id="input-1",
    )


def _service(
    tmp_path: Path,
    runs: tuple[AgentRun, ...],
) -> tuple[SessionInteractionService, _FakeCoordinator]:
    """装配只包含取消依赖的会话交互服务。"""
    registry = AgentRegistry()
    registry.register(AgentIdentity(agent_id="default", agent_name="默认 Agent"))
    coordinator = _FakeCoordinator()
    service = SessionInteractionService(
        session_manager=SessionManager(tmp_path),
        agent_registry=registry,
        coordinator=coordinator,  # type: ignore[arg-type]
        run_repository=_FakeRunRepository(runs),  # type: ignore[arg-type]
    )
    return service, coordinator


@pytest.mark.asyncio
async def test_cancel_session_cancels_the_only_active_run(tmp_path: Path) -> None:
    """唯一活动 Run 必须被准确取消并返回标识。"""
    service, coordinator = _service(tmp_path, (_run("run-1"),))

    run_id = await service.cancel_session("session-1", "用户停止")

    assert run_id == "run-1"
    assert coordinator.cancel_calls == [("run-1", "用户停止")]


@pytest.mark.asyncio
async def test_cancel_session_returns_none_without_active_run(tmp_path: Path) -> None:
    """没有活动 Run 时返回 None，不伪造取消调用。"""
    service, coordinator = _service(tmp_path, ())

    run_id = await service.cancel_session("session-1", "用户停止")

    assert run_id is None
    assert coordinator.cancel_calls == []


@pytest.mark.asyncio
async def test_cancel_session_rejects_multiple_active_runs(tmp_path: Path) -> None:
    """持久化事实违反单 Session 单活动 Run 不变量时必须拒绝猜测。"""
    service, coordinator = _service(
        tmp_path,
        (_run("run-1"), _run("run-2")),
    )

    with pytest.raises(RuntimeError, match="多个活动 Run"):
        await service.cancel_session("session-1", "用户停止")

    assert coordinator.cancel_calls == []
