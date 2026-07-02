"""测试 AgentMessaging —— A2A 通信层。"""

import uuid
from unittest.mock import AsyncMock

import pytest

from dotclaw.agent.identity import AgentIdentity
from dotclaw.agent.registry import AgentRegistry
from dotclaw.agent.runtime import AgentRuntime
from dotclaw.agent.task import Task, TaskStatus
from dotclaw.agent.messaging import AgentMessaging
from dotclaw.session.session import Session, SessionManager
from dotclaw.session.agent_run import AgentRun, AgentRunManager
from dotclaw.llm.base import Message


# ============================================================================
# 工厂
# ============================================================================

def _make_agent_run(run_id: str, agent_id: str, end_status: str,
                    final: str, error: str = "") -> AgentRun:
    return AgentRun(
        run_id=run_id,
        agent_id=agent_id,
        messages=[Message(role="assistant", content=final)],
        end_status=end_status,
        error=error if error else None,
        tool_calls=0,
        tokens_in=0,
        tokens_out=0,
        iterations=1,
        duration_ms=0,
    )


# ============================================================================
# 可控组件
# ============================================================================

class FakeSessionManager(SessionManager):
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self.create_count: int = 0

    async def create(self, title: str = "新对话", model: str = "",
                     agent_id: str = "") -> Session:
        sid: str = uuid.uuid4().hex[:8]
        s = Session(id=sid, title=title, agent_id=agent_id)
        self._sessions[sid] = s
        self.create_count += 1
        return s

    async def load(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def save(self, session: Session) -> None:
        pass

    async def list_all(self) -> list[Session]:
        return list(self._sessions.values())

    async def delete(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None


class FakeRunManager(AgentRunManager):
    def __init__(self) -> None:
        import tempfile
        self._tmp = tempfile.mkdtemp()
        super().__init__(self._tmp)

    async def save(self, run: AgentRun) -> None:
        pass

    async def load(self, run_id: str) -> AgentRun | None:
        return None


def _make_minimal_runtime() -> AgentRuntime:
    return AgentRuntime(
        llm=object(),
        tool_executor=None,
        assembler=None,
        session_mgr=FakeSessionManager(),
        run_mgr=FakeRunManager(),
    )


class TestAgentMessaging:
    """AgentMessaging.send() 端到端。"""

    @pytest.fixture
    def registry(self) -> AgentRegistry:
        r = AgentRegistry()
        r.register(AgentIdentity(agent_id="researcher", agent_name="Researcher"))
        return r

    @pytest.fixture
    def runtime(self) -> AgentRuntime:
        return _make_minimal_runtime()

    @pytest.fixture
    def messaging(self, registry: AgentRegistry, runtime: AgentRuntime) -> AgentMessaging:
        return AgentMessaging(registry=registry, base_runtime=runtime)

    @pytest.mark.asyncio
    async def test_send_completes(self, messaging: AgentMessaging, runtime: AgentRuntime) -> None:
        """send() 返回 completed Task。"""
        runtime.run = AsyncMock(return_value=_make_agent_run(  # type: ignore[method-assign]
            "sub-1", "researcher", "completed", "搜索完成：3个结果"
        ))
        runtime.derive = lambda: runtime  # type: ignore[method-assign]

        task = await messaging.send(
            requester="main",
            target_agent_id="researcher",
            description="搜索资料",
        )
        assert task.status == TaskStatus.COMPLETED
        assert task.final_result == "搜索完成：3个结果"

    @pytest.mark.asyncio
    async def test_send_fills_sub_run_id(self, messaging: AgentMessaging, runtime: AgentRuntime) -> None:
        """send() 填充 sub_run_id。"""
        runtime.run = AsyncMock(return_value=_make_agent_run(  # type: ignore[method-assign]
            "sub-2", "researcher", "completed", "ok"
        ))
        runtime.derive = lambda: runtime  # type: ignore[method-assign]

        task = await messaging.send("main", "researcher", "x")
        assert task.sub_run_id == "sub-2"

    @pytest.mark.asyncio
    async def test_send_target_not_found(self, messaging: AgentMessaging) -> None:
        """目标 Agent 不存在时返回 failed Task。"""
        task = await messaging.send("main", "ghost", "x")
        assert task.status == TaskStatus.FAILED
        assert "ghost" in task.error

    @pytest.mark.asyncio
    async def test_send_passes_parent_run_id(self, messaging: AgentMessaging, runtime: AgentRuntime) -> None:
        """send() 将 parent_run_id 写入 Task。"""
        runtime.run = AsyncMock(return_value=_make_agent_run(  # type: ignore[method-assign]
            "sub-3", "researcher", "completed", "ok"
        ))
        runtime.derive = lambda: runtime  # type: ignore[method-assign]

        task = await messaging.send(
            requester="main",
            target_agent_id="researcher",
            description="x",
            parent_run_id="parent-001",
        )
        assert task.parent_run_id == "parent-001"

    @pytest.mark.asyncio
    async def test_send_passes_context_and_constraints(
        self, messaging: AgentMessaging, runtime: AgentRuntime
    ) -> None:
        """send() 传递 context 和 constraints。"""
        runtime.run = AsyncMock(return_value=_make_agent_run(  # type: ignore[method-assign]
            "sub-4", "researcher", "completed", "done"
        ))
        runtime.derive = lambda: runtime  # type: ignore[method-assign]

        task = await messaging.send(
            requester="main",
            target_agent_id="researcher",
            description="分析",
            context="日志上下文",
            constraints="仅用内置工具",
        )
        assert task.context == "日志上下文"
        assert task.constraints == "仅用内置工具"

    @pytest.mark.asyncio
    async def test_send_handles_failure(self, messaging: AgentMessaging, runtime: AgentRuntime) -> None:
        """子 Agent 执行失败时返回 failed Task。"""
        runtime.run = AsyncMock(return_value=_make_agent_run(  # type: ignore[method-assign]
            "sub-5", "researcher", "failed", "", error="超时"
        ))
        runtime.derive = lambda: runtime  # type: ignore[method-assign]

        task = await messaging.send("main", "researcher", "x")
        assert task.status == TaskStatus.FAILED
        assert task.error == "超时"

    @pytest.mark.asyncio
    async def test_send_creates_isolated_session(
        self, messaging: AgentMessaging, runtime: AgentRuntime
    ) -> None:
        """每次 send 创建独立 Session。"""
        sess_mgr = runtime.session_mgr
        assert isinstance(sess_mgr, FakeSessionManager)
        before = sess_mgr.create_count

        runtime.run = AsyncMock(return_value=_make_agent_run(  # type: ignore[method-assign]
            "sub-6", "researcher", "completed", "ok"
        ))
        runtime.derive = lambda: runtime  # type: ignore[method-assign]

        await messaging.send("main", "researcher", "x")
        assert sess_mgr.create_count == before + 1
