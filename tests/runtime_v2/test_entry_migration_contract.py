"""Phase 4 普通入口迁移契约测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from dotclaw.agent.identity import AgentIdentity
from dotclaw.bootstrap.runtime_factory import build_runtime_services
from dotclaw.bootstrap.session_interaction import SessionInteractionService
from dotclaw.channel.base import Channel
from dotclaw.channel.runtime_text_stream import ChannelTextStreamAdapter
from dotclaw.config.settings import Config
from dotclaw.llm.base import ChatChunk
from dotclaw.orchestration.registry import AgentRegistry
from dotclaw.session.session import SessionManager
from dotclaw.tools.executor import ToolExecutor
from dotclaw.tools.registry import ToolRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FinalProxy:
    """供组合根测试使用的最小旧 LLMProxy 替身。"""

    async def chat(self, messages, tools, model, stream) -> AsyncIterator[ChatChunk]:
        """返回一个普通完成回复。"""
        yield ChatChunk(content="已通过新版入口", is_final=True, input_tokens=2, output_tokens=2)


class ChannelCollector(Channel):
    """验证 Runtime 到 Channel 的文本流转发，不依赖真实终端。"""

    def __init__(self) -> None:
        """初始化收集到的文本块。"""
        self.chunks: list[str] = []

    async def receive(self) -> str:
        """本测试不读取用户输入。"""
        return ""

    async def send(self, message: str) -> None:
        """本测试不使用非流式发送。"""
        pass

    async def stream(self, chunk: str) -> None:
        """记录 Runtime 转发的文本块。"""
        self.chunks.append(chunk)

    async def ask_user(self, prompt: str) -> str:
        """本测试不触发交互式审批。"""
        return ""


async def test_submit_writes_conversation_through_coordinator_and_projector(tmp_path: Path) -> None:
    """普通消息经 Service 直接提交 Coordinator/Engine，Agent 本身不直接写 Session。"""
    config = Config()
    config.session.directory = str(tmp_path)
    identity = AgentIdentity(agent_id="agent-1", agent_name="测试 Agent", model="qwen3.7-max")
    session_manager = SessionManager(tmp_path)
    channel: ChannelCollector = ChannelCollector()
    services = build_runtime_services(
        config=config,
        project_root=PROJECT_ROOT,
        identity=identity,
        llm_proxy=FinalProxy(),
        tool_executor=ToolExecutor(ToolRegistry()),
        session_manager=session_manager,
        skill_registry=None,
        memory_manager=None,
        agent_registry=AgentRegistry(),
    )
    registry = AgentRegistry()
    registry.register(identity)
    service = SessionInteractionService(
        session_manager=session_manager,
        agent_registry=registry,
        coordinator=services.coordinator,
    )
    session = await session_manager.create(agent_id=identity.agent_id)

    result = await service.submit(session, "你好", text_stream_port=ChannelTextStreamAdapter(channel))
    projected = await session_manager.load(session.id)

    assert result.final_message is not None
    assert result.final_message.content == "已通过新版入口"
    assert result.has_streamed_text is True
    assert channel.chunks == ["已通过新版入口"]
    assert projected is not None
    assert [(item.user_query, item.final_answer) for item in projected.conversations] == [("你好", "已通过新版入口")]


def test_normal_entry_files_have_no_legacy_runtime_or_direct_session_write() -> None:
    """普通入口不得重新引入旧 Runtime、审批管理器或 Session 直接写入。"""
    main_source = (PROJECT_ROOT / "src/dotclaw/main.py").read_text(encoding="utf-8")

    assert "Runtime.run" not in main_source
    assert "ApprovalManager" not in main_source
    assert "StateStore" not in main_source
    assert "journal." not in main_source
    # 入口仅经 SessionInteractionService 提交，不持有运行时 Agent 门面。
    assert "from dotclaw.agent import Agent" not in main_source
    assert "agent.process(" not in main_source
