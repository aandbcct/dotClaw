"""Runtime.run() 的公开执行契约测试。"""

from __future__ import annotations

from dataclasses import dataclass, field
import json

import pytest

from dotclaw.agent.agent import Agent
from dotclaw.agent.identity import AgentIdentity
from dotclaw.config.settings import AgentConfig, Config, LLMConfig, ToolsConfig
from dotclaw.journal import Journal
from dotclaw.llm.base import ChatChunk, ToolCall
from dotclaw.runtime import Runtime, StateStore
from dotclaw.session.session import Session
from dotclaw.tools.approval import ApprovalManager
from dotclaw.tools.executor import ToolExecutor
from dotclaw.tools.handler import BuiltinToolHandler
from dotclaw.tools.registry import ToolRegistry


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


class SequenceLLM:
    """按顺序返回流式响应，并记录 Runtime 传入的上下文。"""

    def __init__(self, responses: list[list[ChatChunk]]) -> None:
        self._responses = responses
        self.calls: list[list[object]] = []

    async def chat(self, *, messages: list[object], **_: object):
        self.calls.append(list(messages))
        for chunk in self._responses.pop(0):
            yield chunk


class FakeChannel:
    """记录 Runtime 对用户通道的可见输出。"""

    def __init__(self) -> None:
        self.streams: list[str] = []
        self.infos: list[str] = []

    async def stream(self, chunk: str) -> None:
        self.streams.append(chunk)

    def print_info(self, message: str) -> None:
        self.infos.append(message)


@dataclass
class FakeRunManager:
    """记录 Runtime 写入的运行记录，避免测试依赖文件布局。"""

    saved: list[tuple[object, str]] = field(default_factory=list)

    async def list(self, *_: object, **__: object) -> list[object]:
        return []

    async def save(self, run: object, session_id: str) -> None:
        self.saved.append((run, session_id))


def _build_runtime(
    tmp_path,
    llm: object,
    run_manager: FakeRunManager,
    *,
    tool_executor: ToolExecutor | None = None,
    channel: FakeChannel | None = None,
    config: Config | None = None,
) -> Runtime:
    """构造具备完整运行依赖的最小 Runtime。"""
    return Runtime(
        llm=llm,  # type: ignore[arg-type]
        tool_executor=tool_executor,
        assembler=None,
        agent_registry=None,  # type: ignore[arg-type]
        session_mgr=None,  # type: ignore[arg-type]
        run_mgr=run_manager,  # type: ignore[arg-type]
        journal=Journal(),
        state_store=StateStore(tmp_path / "state"),
        channel=channel,  # type: ignore[arg-type]
        config=config,
    )


def _build_agent() -> Agent:
    """构造没有额外权限限制的测试 Agent。"""
    return Agent(AgentIdentity(
        agent_id="test-agent",
        model="test-model",
        system_prompt_template="你是测试助手。",
    ))


@pytest.mark.asyncio
async def test_run_returns_answer_and_persists_completed_run(tmp_path) -> None:
    """一次纯文本运行应返回答案，并保存一条完成态 AgentRun。"""
    run_manager = FakeRunManager()
    runtime = _build_runtime(tmp_path, FakeLLM(), run_manager)
    agent = _build_agent()
    session = Session(id="test-session")

    answer, run_ids = await runtime.run(session, agent, "你好")

    assert answer == "运行完成"
    assert len(run_ids) == 1
    assert len(run_manager.saved) == 1
    saved_run, saved_session_id = run_manager.saved[0]
    assert saved_session_id == session.id
    assert saved_run.run_id == run_ids[0]
    assert saved_run.end_status == "completed"


@pytest.mark.asyncio
async def test_run_streams_text_to_channel(tmp_path) -> None:
    """Runtime 应逐块透传流式文本，并在结束时拼接最终答案。"""
    llm = SequenceLLM([[
        ChatChunk(content="你好"),
        ChatChunk(content="，世界", is_final=True, finish_reason="stop"),
    ]])
    channel = FakeChannel()
    runtime = _build_runtime(tmp_path, llm, FakeRunManager(), channel=channel)

    answer, _ = await runtime.run(Session(id="stream-session"), _build_agent(), "问候")

    assert answer == "你好，世界"
    assert channel.streams == ["你好", "，世界"]


@pytest.mark.asyncio
async def test_run_injects_assistant_and_tool_messages_into_next_llm_call(tmp_path) -> None:
    """工具轮次后的下一次 LLM 调用必须收到 assistant 调用与 tool 结果。"""
    registry = ToolRegistry()

    async def get_time() -> str:
        return "2026-05-28 17:30:00"

    registry.register(BuiltinToolHandler(
        name="get_time",
        description="获取当前时间",
        parameters={"type": "object", "properties": {}},
        handler_fn=get_time,
    ))
    llm = SequenceLLM([
        [
            ChatChunk(tool_call=ToolCall(
                id="time-1",
                name="get_time",
                arguments=json.dumps({}),
            )),
            ChatChunk(is_final=True, finish_reason="tool_calls"),
        ],
        [ChatChunk(content="现在是 17:30", is_final=True, finish_reason="stop")],
    ])
    runtime = _build_runtime(
        tmp_path,
        llm,
        FakeRunManager(),
        tool_executor=ToolExecutor(registry),
    )

    answer, _ = await runtime.run(Session(id="tool-session"), _build_agent(), "现在几点？")

    assert answer == "现在是 17:30"
    assert len(llm.calls) == 2
    second_call_roles = [message.role for message in llm.calls[1]]
    assert "assistant" in second_call_roles
    assert "tool" in second_call_roles


@pytest.mark.asyncio
async def test_run_persists_waiting_state_for_approval_required_tool(tmp_path) -> None:
    """审批工具应挂起本次运行，而不是在 Runtime 内同步执行。"""
    registry = ToolRegistry()

    async def execute_command(command: str) -> str:
        return command

    registry.register(BuiltinToolHandler(
        name="exec",
        description="执行命令",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}},
        handler_fn=execute_command,
        needs_approval=True,
    ))
    llm = SequenceLLM([[
        ChatChunk(tool_call=ToolCall(
            id="exec-1",
            name="exec",
            arguments=json.dumps({"command": "echo hello"}),
        )),
        ChatChunk(is_final=True, finish_reason="tool_calls"),
    ]])
    run_manager = FakeRunManager()
    config = Config(
        llm=LLMConfig(stream=False),
        agent=AgentConfig(),
        tools=ToolsConfig(approval_commands=["exec"]),
    )
    runtime = _build_runtime(
        tmp_path,
        llm,
        run_manager,
        tool_executor=ToolExecutor(registry, ApprovalManager(["exec"])),
        config=config,
    )

    answer, _ = await runtime.run(Session(id="approval-session"), _build_agent(), "执行命令")

    assert answer == Runtime.WAIT_SENTINEL
    assert len(run_manager.saved) == 1
    saved_run, _ = run_manager.saved[0]
    assert saved_run.end_status == "waiting"
