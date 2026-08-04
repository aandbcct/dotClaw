"""PR2 RunTrace 测试共享构造器。

集中构造最小但合法的 Runtime 权威事实，避免每个用例重复样板；所有时间戳使用固定
ISO 字符串以保证 record_hash 可复现。
"""

from __future__ import annotations

from dotclaw.runtime.domain.context import new_context_version
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.facts import (
    AgentPolicySnapshot,
    AgentRun,
    MessageRole,
    RunMessage,
    RunMessageKind,
    RunStatistics,
    ToolCall,
)
from dotclaw.runtime.domain.state import AgentRunState, Ended, RunOutcome, Running, RunStage


def make_policy(model_id: str = "model-1") -> AgentPolicySnapshot:
    return AgentPolicySnapshot(
        agent_id="agent-1",
        identity_version="v1",
        model_id=model_id,
        max_iterations=10,
        policy_data={"context_window": 100000, "tokenizer_encoding": "cl100k_base"},
    )


def make_run(
    run_id: str = "run-1",
    session_id: str = "sess-1",
    *,
    ended: bool = True,
    outcome: RunOutcome = RunOutcome.COMPLETED,
    started_at: str = "2026-07-31T00:00:00+00:00",
    ended_at: str | None = "2026-07-31T00:00:05+00:00",
) -> AgentRun:
    state = AgentRunState(mode=Ended(outcome) if ended else Running(RunStage.CALLING_LLM))
    return AgentRun(
        run_id=run_id,
        session_id=session_id,
        agent_id="agent-1",
        state=state,
        started_at=started_at,
        policy=make_policy(),
        input_message_id="msg-input",
        ended_at=ended_at,
        statistics=RunStatistics(),
    )


def make_message(
    message_id: str,
    sequence: int,
    kind: RunMessageKind,
    role: MessageRole = MessageRole.ASSISTANT,
    content: str = "hello",
    *,
    tool_call_id: str | None = None,
    name: str | None = None,
    tool_calls: tuple[ToolCall, ...] = (),
) -> RunMessage:
    return RunMessage(
        message_id=message_id,
        sequence=sequence,
        kind=kind,
        role=role,
        content=content,
        tool_call_id=tool_call_id,
        name=name,
        tool_calls=tool_calls,
    )


def make_event(
    run_id: str,
    sequence: int,
    event_type: RunEventType,
    occurred_at: str = "2026-07-31T00:00:01+00:00",
    *,
    message_ids: tuple[str, ...] = (),
    data: dict | None = None,
) -> RunEvent:
    return RunEvent(
        run_id=run_id,
        sequence=sequence,
        event_type=event_type,
        occurred_at=occurred_at,
        message_ids=message_ids,
        summary="",
        data=data or {},
    )


def make_context_version(version: int = 1) -> object:
    return new_context_version(version, (), f"chash-{version}", f"thash-{version}")
