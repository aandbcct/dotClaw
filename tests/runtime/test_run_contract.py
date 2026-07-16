"""Runtime.run() 的公开执行契约测试。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from dotclaw.agent.agent import Agent
from dotclaw.agent.identity import AgentIdentity
from dotclaw.journal import Journal
from dotclaw.llm.base import ChatChunk
from dotclaw.runtime import Runtime, StateStore
from dotclaw.session.session import Session


class FakeLLM:
    """返回固定最终回复的最小 LLM 替身。"""

    async def chat(self, **_: object):
        yield ChatChunk(
            content="运行完成",
            is_final=True,
            finish_reason="stop",
            input_tokens=12,
            output_tokens=4,
        )


@dataclass
class FakeRunManager:
    """记录 Runtime 写入的运行记录，避免测试依赖文件布局。"""

    saved: list[tuple[object, str]] = field(default_factory=list)

    async def list(self, *_: object, **__: object) -> list[object]:
        return []

    async def save(self, run: object, session_id: str) -> None:
        self.saved.append((run, session_id))


@pytest.mark.asyncio
async def test_run_returns_answer_and_persists_completed_run(tmp_path) -> None:
    """一次纯文本运行应返回答案，并保存一条完成态 AgentRun。"""
    run_manager = FakeRunManager()
    runtime = Runtime(
        llm=FakeLLM(),
        tool_executor=None,
        assembler=None,
        agent_registry=None,  # type: ignore[arg-type]
        session_mgr=None,  # type: ignore[arg-type]
        run_mgr=run_manager,  # type: ignore[arg-type]
        journal=Journal(),
        state_store=StateStore(tmp_path / "state"),
    )
    agent = Agent(AgentIdentity(
        agent_id="test-agent",
        model="test-model",
        system_prompt_template="你是测试助手。",
    ))
    session = Session(id="test-session")

    answer, run_ids = await runtime.run(session, agent, "你好")

    assert answer == "运行完成"
    assert len(run_ids) == 1
    assert len(run_manager.saved) == 1
    saved_run, saved_session_id = run_manager.saved[0]
    assert saved_session_id == session.id
    assert saved_run.run_id == run_ids[0]
    assert saved_run.end_status == "completed"
