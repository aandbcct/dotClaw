"""Phase 4 普通入口迁移契约测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from dotclaw.agent.agent import Agent
from dotclaw.agent.identity import AgentIdentity
from dotclaw.bootstrap.runtime_factory import build_runtime_services
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


async def test_agent_process_submits_to_coordinator_and_projector_writes_conversation(tmp_path: Path) -> None:
    """普通消息经 Coordinator/Engine 执行，Agent 本身不直接写 Session。"""
    config = Config()
    config.session.directory = str(tmp_path)
    identity = AgentIdentity(agent_id="agent-1", agent_name="测试 Agent")
    session_manager = SessionManager(tmp_path)
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
        mcp_provider=None,
    )
    agent = Agent(identity, coordinator=services.coordinator, runtime_engine=services.engine, config=config)
    session = await session_manager.create(agent_id=identity.agent_id)

    answer = await agent.process(session, "你好")
    projected = await session_manager.load(session.id)

    assert answer == "已通过新版入口"
    assert projected is not None
    assert [(item.user_query, item.final_answer) for item in projected.conversations] == [("你好", "已通过新版入口")]


def test_normal_entry_files_have_no_legacy_runtime_or_direct_session_write() -> None:
    """普通入口不得重新引入旧 Runtime、审批管理器或 Session 直接写入。"""
    agent_source = (PROJECT_ROOT / "src/dotclaw/agent/agent.py").read_text(encoding="utf-8")
    main_source = (PROJECT_ROOT / "src/dotclaw/main.py").read_text(encoding="utf-8")

    assert "session.add_conversation" not in agent_source
    assert "session_mgr.save" not in agent_source
    assert "runtime.run" not in agent_source
    assert "Runtime.run" not in main_source
    assert "ApprovalManager" not in main_source
    assert "StateStore" not in main_source
    assert "journal." not in main_source
