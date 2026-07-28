"""阶段 1：持久化唯一状态与 operation node 字段的契约测试。

覆盖开发计划 §4 验收：
- AgentRunState 往返序列化；
- AgentRun.state 由 status 派生且 is_ended() 正确；
- run.json 携带 state 并经仓储往返；
- list_active_runs 仅按 state.is_ended() 判断；
- checkpoint 携带 action 并经仓储往返（旧 next_action 仍可派生 action）；
- 成功提交仅接受 Ended(COMPLETED)；
- 仓储拒绝非 v4 旧格式。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from dotclaw.runtime.adapters.checkpoint_repository import CheckpointRepositoryAdapter
from dotclaw.runtime.adapters.in_memory_run_repository import InMemoryRunRepository
from dotclaw.runtime.adapters.run_repository import RunRepositoryAdapter
from dotclaw.runtime.application.dto import ConversationMessage
from dotclaw.runtime.domain.context import SuccessCommitIntent
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.facts import (
    AgentPolicySnapshot,
    AgentRun,
    RunCheckpoint,
    RunStatus,
    MessageRole,
)
from dotclaw.runtime.domain.control import AgentAction
from dotclaw.runtime.domain.state import (
    AgentRunState,
    Created,
    Ended,
    RunOutcome,
    RunStage,
    Running,
    Suspended,
    SuspendReason,
)


def _policy() -> AgentPolicySnapshot:
    return AgentPolicySnapshot("agent-1", "identity-v1", "model-v1", 8)


def _run(status: RunStatus = RunStatus.RUNNING, run_id: str = "run-1") -> AgentRun:
    return AgentRun(
        run_id=run_id,
        session_id="session-1",
        agent_id="agent-1",
        status=status,
        started_at="2026-07-20T00:00:00+00:00",
        policy=_policy(),
        input_message_id="user-1",
    )


# ---------------------------------------------------------------------------
# AgentRunState 序列化
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        AgentRunState(mode=Created()),
        AgentRunState(mode=Running(RunStage.CALLING_LLM)),
        AgentRunState(mode=Running(RunStage.EXECUTING_TOOLS)),
        AgentRunState(mode=Suspended(SuspendReason.APPROVAL, "approval-1", RunStage.EXECUTING_TOOLS)),
        AgentRunState(mode=Suspended(SuspendReason.DELEGATION, "child-1", RunStage.CALLING_LLM)),
        AgentRunState(mode=Ended(RunOutcome.COMPLETED)),
        AgentRunState(mode=Ended(RunOutcome.FAILED)),
    ],
)
def test_agent_run_state_roundtrip(state: AgentRunState) -> None:
    """四模式及其累计统计经 to_dict/from_dict 不丢失。"""
    restored: AgentRunState = AgentRunState.from_dict(state.to_dict())
    assert restored == state
    assert restored.is_ended() == isinstance(state.mode, Ended)


def test_agent_run_state_carries_statistics() -> None:
    """累计控制/统计值在往返后保留。"""
    state: AgentRunState = AgentRunState(
        mode=Running(RunStage.CALLING_LLM),
        iteration=3,
        retry_count=1,
        truncate_count=2,
        loop_fingerprint="fp-9",
    )
    restored: AgentRunState = AgentRunState.from_dict(state.to_dict())
    assert restored.iteration == 3
    assert restored.retry_count == 1
    assert restored.truncate_count == 2
    assert restored.loop_fingerprint == "fp-9"


# ---------------------------------------------------------------------------
# AgentRun.state 派生投影
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected_ended",
    [
        (RunStatus.RUNNING, False),
        (RunStatus.WAITING_APPROVAL, False),
        (RunStatus.COMPLETED, True),
        (RunStatus.FAILED, True),
        (RunStatus.CANCELLED, True),
        (RunStatus.ABANDONED, True),
    ],
)
def test_agent_run_state_derives_from_status(status: RunStatus, expected_ended: bool) -> None:
    """迁移期 state 由 status 派生，is_ended() 与终态语义一致。"""
    run: AgentRun = _run(status=status)
    assert isinstance(run.state, AgentRunState)
    assert run.state.is_ended() is expected_ended


def test_agent_run_state_completed_outcome() -> None:
    """完成的运行投影为 Ended(COMPLETED)。"""
    run: AgentRun = _run(status=RunStatus.COMPLETED)
    assert isinstance(run.state.mode, Ended)
    assert run.state.mode.outcome is RunOutcome.COMPLETED


def test_agent_run_to_dict_includes_state() -> None:
    """run.json 必须携带 state 字段。"""
    run: AgentRun = _run(status=RunStatus.RUNNING)
    assert "state" in run.to_dict()
    assert run.to_dict()["state"]["mode"]["type"] == "running"


# ---------------------------------------------------------------------------
# 仓储：list_active_runs 仅按 state.is_ended()
# ---------------------------------------------------------------------------


async def test_list_active_runs_uses_state_is_ended() -> None:
    """活跃判定统一基于 state.is_ended()，终态运行不再占用 Session。"""
    repository: InMemoryRunRepository = InMemoryRunRepository()
    active_statuses = (RunStatus.RUNNING, RunStatus.WAITING_APPROVAL)
    ended_statuses = (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.ABANDONED)
    for index, status in enumerate(active_statuses + ended_statuses):
        await repository.create_run(_run(status=status, run_id=f"run-{index}"))

    active: tuple[AgentRun, ...] = await repository.list_active_runs("session-1")
    active_ids: frozenset[str] = frozenset(run.run_id for run in active)
    assert active_ids == {f"run-{i}" for i in range(len(active_statuses))}


async def test_file_repository_list_active_runs(tmp_path) -> None:
    """文件仓储同样按 state.is_ended() 过滤。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    await repository.create_run(_run(status=RunStatus.RUNNING, run_id="active-1"))
    await repository.create_run(_run(status=RunStatus.COMPLETED, run_id="ended-1"))
    active: tuple[AgentRun, ...] = await repository.list_active_runs("session-1")
    assert {run.run_id for run in active} == {"active-1"}


# ---------------------------------------------------------------------------
# 仓储：AgentRun 经 run.json 往返且 state 一致
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.RUNNING,
        RunStatus.WAITING_APPROVAL,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.ABANDONED,
    ],
)
async def test_agent_run_roundtrip_preserves_state(tmp_path, status: RunStatus) -> None:
    """新建/审批等待/成功/失败/取消/放弃经 run.json 往返后 state 投影保持一致。"""
    repository: RunRepositoryAdapter = RunRepositoryAdapter(tmp_path)
    run_id: str = f"run-{status.value}"
    await repository.create_run(_run(status=status, run_id=run_id))

    loaded: AgentRun | None = await repository.load_run("session-1", run_id)
    assert loaded is not None
    assert loaded.status is status
    assert loaded.state == _run(status=status).state
    assert loaded.state.is_ended() == _run(status=status).state.is_ended()


# ---------------------------------------------------------------------------
# 仓储：checkpoint 携带 action 并经往返
# ---------------------------------------------------------------------------


async def test_checkpoint_action_roundtrip(tmp_path) -> None:
    """checkpoint 写入并读回 action；新格式字段出现在 payload。"""
    repository: CheckpointRepositoryAdapter = CheckpointRepositoryAdapter(tmp_path)
    checkpoint: RunCheckpoint = RunCheckpoint(
        checkpoint_id="checkpoint-1",
        run_id="run-1",
        session_id="session-1",
        checkpoint_sequence=1,
        event_sequence=2,
        message_sequence=3,
        agent_state={"phase": "waiting_llm"},
        next_action=AgentAction.INVOKE_LLM,
        action=AgentAction.SUSPEND,
        pending={},
        budget={"max_iterations": 8},
    )
    await repository.save(checkpoint)
    loaded: RunCheckpoint | None = await repository.load(checkpoint.session_id, checkpoint.run_id)
    assert loaded is not None
    assert loaded == checkpoint
    assert loaded.action.value == "suspend"


async def test_checkpoint_derives_action_from_next_action(tmp_path) -> None:
    """旧格式 checkpoint（仅 next_action）仍能派生出 action。"""
    payload: dict[str, object] = {
        "version": 4,
        "checkpoint_id": "checkpoint-old",
        "run_id": "run-1",
        "session_id": "session-1",
        "checkpoint_sequence": 1,
        "event_sequence": 2,
        "message_sequence": 3,
        "agent_state": {"phase": "waiting_llm"},
        "next_action": "invoke_llm",
        "pending": {},
        "budget": {},
    }
    (tmp_path / "session-1" / "agent_runs" / "run-1").mkdir(parents=True)
    (tmp_path / "session-1" / "agent_runs" / "run-1" / "checkpoint.json").write_text(
        __import__("json").dumps(payload), encoding="utf-8",
    )
    loaded: RunCheckpoint | None = await CheckpointRepositoryAdapter(tmp_path).load("session-1", "run-1")
    assert loaded is not None
    assert loaded.action is loaded.next_action
    assert loaded.action.value == "invoke_llm"


# ---------------------------------------------------------------------------
# 仓储：成功提交仅接受 Ended(COMPLETED)
# ---------------------------------------------------------------------------


async def test_success_commit_requires_completed_state() -> None:
    """成功提交投影必须基于 Ended(COMPLETED) 的运行。"""
    repository: InMemoryRunRepository = InMemoryRunRepository()
    completed: AgentRun = _run(status=RunStatus.COMPLETED, run_id="ok-1")
    final_message: ConversationMessage = ConversationMessage("m1", MessageRole.ASSISTANT, "回答", "2026-07-20T00:00:00+00:00")
    completed_event: RunEvent = RunEvent(
        run_id="ok-1", sequence=1, event_type=RunEventType.RUN_COMPLETED, occurred_at="2026-07-20T00:00:00+00:00",
    )
    intent: SuccessCommitIntent = SuccessCommitIntent(
        conversation_id="conversation-ok-1",
        latest_candidate_id=None,
        target_outcome=RunOutcome.COMPLETED,
        run_id="ok-1",
        session_id="session-1",
    )
    # 不应抛出异常。
    await repository.create_run(completed)
    await repository.commit_success(completed, final_message, completed_event, intent)

    running: AgentRun = _run(status=RunStatus.RUNNING, run_id="bad-1")
    bad_intent: SuccessCommitIntent = replace(intent, run_id="bad-1")
    with pytest.raises(ValueError, match="成功提交必须包含完成 Run"):
        await repository.commit_success(running, final_message, completed_event, bad_intent)


# ---------------------------------------------------------------------------
# 仓储：拒绝非 v4 旧格式
# ---------------------------------------------------------------------------


async def test_checkpoint_repository_rejects_non_v4(tmp_path) -> None:
    """checkpoint 仓储拒绝非 v4 文件，避免隐式迁移产生第二套事实。"""
    (tmp_path / "session-1" / "agent_runs" / "run-1").mkdir(parents=True)
    (tmp_path / "session-1" / "agent_runs" / "run-1" / "checkpoint.json").write_text(
        __import__("json").dumps({"version": 2, "checkpoint_id": "x"}), encoding="utf-8",
    )
    with pytest.raises(ValueError, match="仅支持 v4"):
        await CheckpointRepositoryAdapter(tmp_path).load("session-1", "run-1")
