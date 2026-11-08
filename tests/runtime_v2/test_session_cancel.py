"""Session 级显式取消入口测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dotclaw.agent.identity import AgentIdentity
from dotclaw.bootstrap.session_interaction import SessionInteractionService
from dotclaw.orchestration.registry import AgentRegistry
from dotclaw.runtime.domain.cancellation import CancellationReceipt, CancellationDisposition
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, AgentRun
from dotclaw.runtime.domain.state import AgentRunState, RunStage, Running
from dotclaw.session.session import SessionManager


class _FakeRunRepository:
    """返回预设活动 Run 的仓储替身。"""

    def __init__(self, runs: tuple[AgentRun, ...]) -> None:
        self.runs: tuple[AgentRun, ...] = runs

    async def load_run_snapshot(self, session_id, run_id):
        run = next((run for run in self.runs if run.run_id == run_id), None)
        if run and run.session_id != session_id:
            raise ValueError("归属错误")
        return run


class _FakeCoordinator:
    """记录实际 Run 取消调用。"""

    def __init__(self) -> None:
        self.cancel_calls: list[tuple[str, str]] = []

    async def cancel(self, run_id: str, reason: str, *, origin=None, parent_request_id=None) -> None:
        """记录取消目标与原因。"""
        self.cancel_calls.append((run_id, reason))
        return CancellationReceipt(run_id, CancellationDisposition.ACCEPTED, "request", 1, None, "2026-11-06T00:00:00+00:00")


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
async def test_cancel_session_returns_receipt_for_explicit_target(tmp_path: Path) -> None:
    """明确目标Run必须被准确取消并返回持久化回执。"""
    service, coordinator = _service(tmp_path, (_run("run-1"),))

    run_id = await service.cancel_session("session-1", "用户停止", expected_run_id="run-1")

    assert run_id.run_id == "run-1" and run_id.disposition is CancellationDisposition.ACCEPTED
    assert coordinator.cancel_calls == [("run-1", "用户停止")]


@pytest.mark.asyncio
async def test_cancel_session_returns_not_found_receipt(tmp_path: Path) -> None:
    """明确目标不存在时返回not_found回执，不伪造取消调用。"""
    service, coordinator = _service(tmp_path, ())

    run_id = await service.cancel_session("session-1", "用户停止", expected_run_id="run-1")

    assert run_id.disposition is CancellationDisposition.NOT_FOUND
    assert coordinator.cancel_calls == []


@pytest.mark.asyncio
async def test_cancel_session_never_guesses_between_two_runs(tmp_path: Path) -> None:
    """即使存在两个Run，也只取消请求中明确的目标。"""
    service, coordinator = _service(
        tmp_path,
        (_run("run-1"), _run("run-2")),
    )

    receipt = await service.cancel_session("session-1", "用户停止", expected_run_id="run-2")
    assert receipt.run_id == "run-2"
    assert coordinator.cancel_calls == [("run-2", "用户停止")]
