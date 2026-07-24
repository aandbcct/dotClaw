"""CLI 提交语义的静态契约测试。"""

from __future__ import annotations

import types
from pathlib import Path


def test_cli_uses_service_entry_and_returns_run_result() -> None:
    """CLI 仅通过 SessionInteractionService 提交普通消息、审批决定、取消与重试/放弃。"""
    source = (Path(__file__).resolve().parents[2] / "src/dotclaw/main.py").read_text(encoding="utf-8")

    assert "service.submit(current_session, user_input, output_port)" in source
    assert "service.resolve_approval(result.approval_id, approved, output_port)" in source
    assert "service.cancel(args, \"用户通过 CLI 取消\")" in source
    assert "service.retry_interrupted(args, output_port)" in source
    assert "service.abandon_interrupted(args)" in source
    assert "service.get_identity(" in source
    assert "Runtime.run" not in source
    assert "channel.print_markdown(" in source
    assert "has_streamed_response" in source
    assert "await channel.stream(\"\\n\")" in source
    # 不得重新引入运行时 Agent 门面。
    assert "agent.process(" not in source
    assert "from dotclaw.agent import Agent" not in source


async def test_refresh_banner_resolves_current_session_identity(tmp_path: Path, monkeypatch) -> None:
    """``/new`` 与 ``/switch`` 后 Banner 必须按当前 Session 重新解析 Identity（fix 文档 §3.3）。"""
    from dotclaw.agent.identity import AgentIdentity
    from dotclaw.bootstrap.session_interaction import SessionInteractionService
    from dotclaw.main import _refresh_banner
    from dotclaw.orchestration.registry import AgentRegistry
    from dotclaw.session.session import SessionManager

    registry: AgentRegistry = AgentRegistry()
    registry.register(AgentIdentity(agent_id="a1", agent_name="A1"))
    registry.register(AgentIdentity(agent_id="a2", agent_name="A2"))
    manager: SessionManager = SessionManager(tmp_path)
    service: SessionInteractionService = SessionInteractionService(
        session_manager=manager,
        agent_registry=registry,
        coordinator=object(),
    )

    class _FakeConfig:
        llm = types.SimpleNamespace(default_model="default-model")
        debug = types.SimpleNamespace(level=0)

    captured: dict[str, str] = {}

    def _fake_build_banner(agent_name, model, session_title, workspace):  # type: ignore[no-untyped-def]
        captured["agent_name"] = agent_name
        captured["session_title"] = session_title
        return None

    monkeypatch.setattr("dotclaw.main.build_banner", _fake_build_banner)
    monkeypatch.setattr("dotclaw.main.rich_console", types.SimpleNamespace(print=lambda *a, **k: None))
    monkeypatch.setattr("dotclaw.config._find_project_root", lambda: tmp_path)

    s1 = await manager.create(agent_id="a1")
    _refresh_banner(service, s1, _FakeConfig())
    assert captured["agent_name"] == "A1"
    assert captured["session_title"] == s1.title

    s2 = await manager.create(agent_id="a2")
    _refresh_banner(service, s2, _FakeConfig())
    assert captured["agent_name"] == "A2"
    assert captured["session_title"] == s2.title


async def test_render_result_suppresses_final_when_response_streamed() -> None:
    """response 已流式发送时 _render_result 只补换行，不得重复打印最终回答。"""
    from dotclaw.main import _render_result
    from dotclaw.runtime.application.dto import ConversationMessage, RunResult
    from dotclaw.runtime.domain.facts import MessageRole, RunStatus

    class _FakeChannel:
        def __init__(self) -> None:
            self.streamed: list[str] = []
            self.markdowns: list[str] = []

        async def stream(self, chunk: str) -> None:
            self.streamed.append(chunk)

        async def print_markdown(self, text: str) -> None:
            self.markdowns.append(text)

    result = RunResult(
        run_id="run-1",
        status=RunStatus.COMPLETED,
        final_message=ConversationMessage("m1", MessageRole.ASSISTANT, "已通过流式展示", "t"),
        has_streamed_response=True,
    )
    channel = _FakeChannel()
    await _render_result(channel, result)

    # 终态只补齐换行，最终回答已由输出端口流式呈现，不得重复打印。
    assert channel.streamed == ["\n"]
    assert channel.markdowns == []


async def test_render_result_prints_final_when_response_not_streamed() -> None:
    """response 未流式发送（如 reasoning-only）时 _render_result 必须打印最终回答。"""
    from dotclaw.main import _render_result
    from dotclaw.runtime.application.dto import ConversationMessage, RunResult
    from dotclaw.runtime.domain.facts import MessageRole, RunStatus

    class _FakeChannel:
        def __init__(self) -> None:
            self.streamed: list[str] = []
            self.markdowns: list[str] = []

        async def stream(self, chunk: str) -> None:
            self.streamed.append(chunk)

        async def print_markdown(self, text: str) -> None:
            self.markdowns.append(text)

    # reasoning-only 场景下 LLMProxyAdapter 不标记 has_streamed_response。
    result = RunResult(
        run_id="run-2",
        status=RunStatus.COMPLETED,
        final_message=ConversationMessage("m1", MessageRole.ASSISTANT, "最终结论", "t"),
        has_streamed_response=False,
    )
    channel = _FakeChannel()
    await _render_result(channel, result)

    # 未流式展示 response，必须由 CLI 渲染最终回答，仅补换行不得触发。
    assert channel.streamed == []
    assert channel.markdowns == ["最终结论"]
