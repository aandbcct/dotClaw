"""GUI v0.1 HTTP 接口契约测试。

本文件只固定 Web 适配边界，不调用真实模型、工具或网络。
生产实现位于 ``dotclaw.channel.web``，测试用于锁定后端 HTTP/SSE 契约。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from dotclaw.bootstrap.session_interaction import SessionInteractionService, UnknownIdentityError
from dotclaw.agent.identity import AgentIdentity
from dotclaw.session.session import Conversation, Session, SessionManager
from dotclaw.orchestration.registry import AgentRegistry
from dotclaw.runtime.application.dto import RunResult
from dotclaw.runtime.application.session_run_coordinator import SessionRunCoordinator, RuntimeControlPort
from dotclaw.runtime.domain.events import RunEvent, RunEventType
from dotclaw.runtime.domain.facts import AgentPolicySnapshot, AgentRun, MessageRole, RunMessage, RunMessageKind, ToolCall
from dotclaw.runtime.domain.state import AgentRunState, Ended, RunOutcome, RunStage, Suspended, SuspendReason
from dotclaw.channel.web.app import create_app


class _FakeSessionManager:
    """仅提供 Web 查询契约需要的 Session 读取能力。"""

    def __init__(self, sessions: tuple[Session, ...] = ()) -> None:
        self.sessions: dict[str, Session] = {session.id: session for session in sessions}

    async def list_all(self) -> list[Session]:
        """故意返回插入顺序，由 Web 接口负责稳定倒序输出。"""
        return list(self.sessions.values())

    async def load(self, session_id: str) -> Session | None:
        """按标识返回测试 Session。"""
        return self.sessions.get(session_id)


class _FakeSessionInteraction:
    """记录创建与取消调用的最小会话交互替身。"""

    def __init__(self, manager: _FakeSessionManager) -> None:
        self._manager: _FakeSessionManager = manager
        self.create_error: Exception | None = None
        self.cancelled_run_id: str | None = "run-active"
        self.create_calls: list[tuple[str | None, str, str | None]] = []
        self.cancel_calls: list[tuple[str, str]] = []
        self.activity_run: AgentRun | None = None
        self.activity_events: tuple[RunEvent, ...] = ()
        self.activity_messages: tuple[RunMessage, ...] = ()
        self.executing = False
        self.approval_calls: list[tuple[str, bool]] = []
        self.delegation_resume_calls: list[str] = []
        self.run_resume_calls: list[str] = []

    @property
    def default_agent_id(self) -> str:
        """返回测试配置中的默认 Agent。"""
        return "default"

    async def create_session(
        self,
        agent_id: str | None = None,
        title: str = "新对话",
        model: str | None = None,
    ) -> Session:
        """创建固定测试会话，或抛出预设业务错误。"""
        self.create_calls.append((agent_id, title, model))
        if self.create_error is not None:
            raise self.create_error
        session = _session(
            "created-1",
            title=title,
            agent_id=agent_id or "default",
            model=model or "chat-model",
            updated_at="2026-09-01T12:00:00+08:00",
        )
        self._manager.sessions[session.id] = session
        return session

    async def cancel_session(self, session_id: str, reason: str) -> str | None:
        """返回预设活动 Run，并记录显式取消原因。"""
        self.cancel_calls.append((session_id, reason))
        return self.cancelled_run_id

    async def get_run(self, session_id: str, run_id: str | None = None) -> AgentRun | None:
        """返回预设运行，同时校验 Session 与可选运行标识。"""
        run = self.activity_run
        if run is None or run.session_id != session_id:
            return None
        return run if run_id is None or run.run_id == run_id else None

    async def get_run_by_id(self, run_id: str) -> AgentRun | None:
        """按标识返回预设运行。"""
        return self.activity_run if self.activity_run and self.activity_run.run_id == run_id else None

    async def get_run_evidence(
        self, session_id: str, run_id: str,
    ) -> tuple[tuple[RunEvent, ...], tuple[RunMessage, ...]]:
        """返回预设工具执行证据。"""
        del session_id, run_id
        return self.activity_events, self.activity_messages

    async def is_session_executing(self, session_id: str) -> bool:
        """返回当前进程是否正在执行预设运行。"""
        del session_id
        return self.executing

    async def resolve_approval(
        self, approval_id: str, approved: bool, output_port=None,
    ) -> RunResult:
        """记录网页审批并返回已取消的测试终态。"""
        del output_port
        self.approval_calls.append((approval_id, approved))
        return RunResult("run-1", AgentRunState(mode=Ended(RunOutcome.CANCELLED)))

    async def resume_delegation(self, child_run_id: str, output_port=None) -> RunResult:
        """记录网页端的父运行恢复请求。"""
        del output_port
        self.delegation_resume_calls.append(child_run_id)
        return RunResult("run-1", AgentRunState(mode=Ended(RunOutcome.CANCELLED)))

    async def resume_run(self, run_id: str, output_port=None) -> RunResult:
        """记录网页端的检查点恢复请求。"""
        del output_port
        self.run_resume_calls.append(run_id)
        return RunResult("run-1", AgentRunState(mode=Ended(RunOutcome.CANCELLED)))


class _FakeHost:
    """模拟 Web lifespan 持有的应用宿主。"""

    def __init__(
        self,
        manager: _FakeSessionManager,
        interaction: _FakeSessionInteraction,
    ) -> None:
        self.session_manager: _FakeSessionManager = manager
        self.session_interaction: _FakeSessionInteraction = interaction
        self.agent_registry = AgentRegistry()
        self.agent_registry.register(AgentIdentity("default", "默认助手", description="通用任务"))
        self.agent_registry.register(AgentIdentity("writer", "写作助手", description="内容写作"))
        self.chat_models = ("chat-model", "fast-model")
        self.preferred_chat_model = "chat-model"
        self.shutdown_called: bool = False

    async def shutdown(self) -> None:
        """记录 lifespan 是否释放宿主。"""
        self.shutdown_called = True


def _session(
    session_id: str,
    *,
    title: str = "新对话",
    agent_id: str = "default",
    updated_at: str = "2026-09-01T10:00:00+08:00",
    conversations: list[Conversation] | None = None,
    model: str = "chat-model",
) -> Session:
    """构造字段完整的持久化 Session。"""
    return Session(
        id=session_id,
        title=title,
        agent_id=agent_id,
        model=model,
        created_at="2026-09-01T09:00:00+08:00",
        updated_at=updated_at,
        conversations=conversations or [],
    )


async def _ready_host(host: _FakeHost) -> _FakeHost:
    """把已构造的测试宿主包装为异步工厂。"""
    return host


def _persistent_host(tmp_path: Path) -> tuple[_FakeHost, SessionRunCoordinator]:
    """使用真实持久化与会话租约，仅替换未执行的模型引擎。"""
    manager = SessionManager(tmp_path)
    coordinator = SessionRunCoordinator(Mock(spec=RuntimeControlPort))
    interaction = SessionInteractionService(manager, AgentRegistry(), coordinator)
    return _FakeHost(manager, interaction), coordinator  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_metadata_patch_persists_without_losing_conversations(tmp_path: Path) -> None:
    """标题、归档和恢复落盘，且不删除已有问答。"""
    host, _ = _persistent_host(tmp_path)
    session = await host.session_manager.create(agent_id="default")
    session.add_conversation("已有问题", "已有回答", ["run-1"])
    await host.session_manager.save(session)
    async with _client(host) as client:
        response = await client.patch(f"/api/v1/sessions/{session.id}", json={"title": "  新标题  ", "archived": True})
        assert response.status_code == 200
        assert response.json()["title"] == "新标题"
        assert response.json()["archived"] is True
        persisted = await SessionManager(tmp_path).load(session.id)
        assert persisted is not None and persisted.archived
        assert persisted.title == "新标题"
        assert persisted.conversations[0].final_answer == "已有回答"
        listing = await client.get("/api/v1/sessions")
        assert listing.json()[0]["archived"] is True
        restored = await client.patch(f"/api/v1/sessions/{session.id}", json={"archived": False})
        assert restored.status_code == 200
        detail = await client.get(f"/api/v1/sessions/{session.id}")
        assert detail.json()["archived"] is False
        assert len(detail.json()["messages"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"title": "  "}, {"title": None}, {"archived": None}, {"model": "other"}])
async def test_metadata_patch_rejects_invalid_updates(tmp_path: Path, payload: dict) -> None:
    """空值与越界字段不能写入会话。"""
    host, _ = _persistent_host(tmp_path)
    async with _client(host) as client:
        response = await client.patch("/api/v1/sessions/missing", json=payload)
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_metadata_patch_missing_and_busy_session(tmp_path: Path) -> None:
    """不存在返回 404；执行租约占用时返回 409 且不改变标题。"""
    host, coordinator = _persistent_host(tmp_path)
    session = await host.session_manager.create(agent_id="default")
    async with _client(host) as client:
        missing = await client.patch("/api/v1/sessions/missing", json={"title": "新标题"})
        assert missing.status_code == 404
        async with coordinator.edit_session(session.id):
            busy = await client.patch(f"/api/v1/sessions/{session.id}", json={"title": "新标题"})
        assert busy.status_code == 409
        persisted = await host.session_manager.load(session.id)
        assert persisted is not None and persisted.title == "新对话"


def test_legacy_session_defaults_to_unarchived() -> None:
    """已有磁盘记录无需迁移即可读取。"""
    payload = _session("legacy").to_dict()
    del payload["archived"]
    assert Session.from_dict(payload).archived is False


@asynccontextmanager
async def _client(
    host: _FakeHost,
    *,
    frontend_dir: Path | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """启动应用 lifespan，并通过内存 ASGI 传输访问接口。"""
    app = create_app(
        host_factory=lambda: _ready_host(host),
        frontend_dir=frontend_dir,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client


def _host(*sessions: Session) -> tuple[_FakeHost, _FakeSessionInteraction]:
    """构造共享同一会话存储的 Host 与交互替身。"""
    manager = _FakeSessionManager(sessions)
    interaction = _FakeSessionInteraction(manager)
    return _FakeHost(manager, interaction), interaction


@pytest.mark.asyncio
async def test_built_frontend_is_served_without_shadowing_api(tmp_path: Path) -> None:
    """构建后的首页由同一进程托管，且不得遮蔽既有 API。"""
    (tmp_path / "index.html").write_text(
        "<html><body>dotClaw GUI</body></html>",
        encoding="utf-8",
    )
    host, _ = _host()

    async with _client(host, frontend_dir=tmp_path) as client:
        page_response = await client.get("/")
        api_response = await client.get("/api/v1/sessions")

    assert page_response.status_code == 200
    assert "dotClaw GUI" in page_response.text
    assert api_response.status_code == 200
    assert api_response.json() == []


@pytest.mark.asyncio
async def test_list_sessions_returns_empty_array() -> None:
    """没有持久化 Session 时必须返回空数组。"""
    host, _ = _host()

    async with _client(host) as client:
        response = await client.get("/api/v1/sessions")

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_list_sessions_returns_stable_updated_descending_summaries() -> None:
    """会话列表只暴露摘要字段，并按更新时间倒序。"""
    older = _session("older", title="旧会话", updated_at="2026-09-01T10:00:00+08:00")
    newer = _session("newer", title="新会话", updated_at="2026-09-01T11:00:00+08:00")
    host, _ = _host(older, newer)

    async with _client(host) as client:
        response = await client.get("/api/v1/sessions")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "newer",
            "title": "新会话",
            "archived": False,
            "agent_id": "default",
            "model": "chat-model",
            "created_at": "2026-09-01T09:00:00+08:00",
            "updated_at": "2026-09-01T11:00:00+08:00",
        },
        {
            "id": "older",
            "title": "旧会话",
            "archived": False,
            "agent_id": "default",
            "model": "chat-model",
            "created_at": "2026-09-01T09:00:00+08:00",
            "updated_at": "2026-09-01T10:00:00+08:00",
        },
    ]


@pytest.mark.asyncio
async def test_create_session_uses_interaction_service_and_returns_201() -> None:
    """创建接口必须复用现有会话交互入口。"""
    host, interaction = _host()

    async with _client(host) as client:
        response = await client.post(
            "/api/v1/sessions",
            json={"title": "GUI 会话", "agent_id": "writer"},
        )

    assert response.status_code == 201
    assert response.json() == {
        "id": "created-1",
        "title": "GUI 会话",
        "archived": False,
        "agent_id": "writer",
        "model": "chat-model",
        "created_at": "2026-09-01T09:00:00+08:00",
        "updated_at": "2026-09-01T12:00:00+08:00",
    }
    assert interaction.create_calls == [("writer", "GUI 会话", None)]


@pytest.mark.asyncio
async def test_options_and_explicit_model_are_exposed_for_session_creation() -> None:
    """网页能读取真实 Agent/模型选项，并把显式模型写入新会话。"""
    host, interaction = _host()

    async with _client(host) as client:
        options = await client.get("/api/v1/options")
        created = await client.post(
            "/api/v1/sessions",
            json={"agent_id": "writer", "model": "fast-model"},
        )

    assert options.status_code == 200
    assert options.json() == {
        "agents": [
            {"id": "default", "name": "默认助手", "description": "通用任务"},
            {"id": "writer", "name": "写作助手", "description": "内容写作"},
        ],
        "models": ["chat-model", "fast-model"],
        "default_agent_id": "default",
        "default_model": "chat-model",
    }
    assert created.status_code == 201
    assert created.json()["agent_id"] == "writer"
    assert created.json()["model"] == "fast-model"
    assert interaction.create_calls == [("writer", "新对话", "fast-model")]


@pytest.mark.asyncio
async def test_activity_exposes_tool_evidence_and_web_approval_stream() -> None:
    """工具参数、结果和待审批状态来自运行事实，审批后继续返回 SSE 终态。"""
    host, interaction = _host(_session("session-1"))
    interaction.activity_run = AgentRun(
        run_id="run-1",
        session_id="session-1",
        agent_id="default",
        state=AgentRunState(
            mode=Suspended(SuspendReason.APPROVAL, "approval-1", RunStage.EXECUTING_TOOLS),
        ),
        started_at="2026-09-01T09:00:00+08:00",
        policy=AgentPolicySnapshot("default", "v1", "chat-model", 10),
        input_message_id="input-1",
    )
    interaction.activity_messages = (
        RunMessage(
            "assistant-1", 2, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "",
            tool_calls=(ToolCall("call-1", "read_file", {"path": "README.md"}),),
        ),
        RunMessage(
            "tool-1", 3, RunMessageKind.TOOL_RESULT, MessageRole.TOOL, "文件内容",
            tool_call_id="call-1", name="read_file",
        ),
    )
    interaction.activity_events = (
        RunEvent("run-1", 1, RunEventType.TOOL_STARTED, "2026-09-01T09:00:01+08:00", ("assistant-1",), data={"call_id": "call-1", "tool_name": "read_file"}),
        RunEvent("run-1", 2, RunEventType.TOOL_COMPLETED, "2026-09-01T09:00:02+08:00", ("tool-1",), data={"call_id": "call-1", "tool_name": "read_file", "status": "approval_required"}),
    )

    async with _client(host) as client:
        activity = await client.get("/api/v1/sessions/session-1/activity")
        decision = await client.post("/api/v1/approvals/approval-1", json={"approved": False})

    assert activity.status_code == 200
    assert activity.json()["status"] == "waiting_approval"
    assert activity.json()["approval_id"] == "approval-1"
    assert activity.json()["tools"] == [{
        "call_id": "call-1",
        "name": "read_file",
        "status": "approval_required",
        "started_at": "2026-09-01T09:00:01+08:00",
        "completed_at": "2026-09-01T09:00:02+08:00",
        "arguments": {"path": "README.md"},
        "result": "文件内容",
        "error": "",
    }]
    assert decision.status_code == 200
    assert "event: run.cancelled" in decision.text
    assert '"session_id":"session-1"' in decision.text
    assert interaction.approval_calls == [("approval-1", False)]


@pytest.mark.asyncio
async def test_web_can_resume_delegation_and_interrupted_run() -> None:
    """网页恢复入口必须调用既有控制能力，并继续返回规范 SSE 终态。"""
    host, interaction = _host(_session("session-1"))
    interaction.activity_run = AgentRun(
        run_id="run-1",
        session_id="session-1",
        agent_id="default",
        state=AgentRunState(mode=Suspended(SuspendReason.APPROVAL, "approval-1", RunStage.EXECUTING_TOOLS)),
        started_at="2026-09-01T09:00:00+08:00",
        policy=AgentPolicySnapshot("default", "v1", "chat-model", 10),
        input_message_id="input-1",
    )

    async with _client(host) as client:
        delegation = await client.post("/api/v1/delegations/child-1/resume")
        interrupted = await client.post("/api/v1/runs/run-1/resume")

    assert delegation.status_code == 200
    assert interrupted.status_code == 200
    assert "event: run.cancelled" in delegation.text
    assert "event: run.cancelled" in interrupted.text
    assert interaction.delegation_resume_calls == ["child-1"]
    assert interaction.run_resume_calls == ["run-1"]


@pytest.mark.asyncio
async def test_create_session_maps_unknown_identity_to_safe_422() -> None:
    """未知 Identity 必须返回统一错误结构，不泄露内部异常。"""
    host, interaction = _host()
    interaction.create_error = UnknownIdentityError("未知 Identity: secret-agent")

    async with _client(host) as client:
        response = await client.post(
            "/api/v1/sessions",
            json={"agent_id": "secret-agent"},
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "unknown_identity",
            "message": "未知 Identity: secret-agent",
        }
    }


@pytest.mark.asyncio
async def test_session_detail_flattens_only_completed_conversations() -> None:
    """详情页按现有 Conversation 投影展开稳定的 user/assistant 历史。"""
    conversation = Conversation(
        user_query="问题",
        conversation_id="conversation-1",
        final_answer="回答",
        agent_run_ids=["run-1"],
        created_at="2026-09-01T09:30:00+08:00",
    )
    host, _ = _host(_session("session-1", conversations=[conversation]))

    async with _client(host) as client:
        response = await client.get("/api/v1/sessions/session-1")

    assert response.status_code == 200
    assert response.json() == {
        "id": "session-1",
        "title": "新对话",
        "archived": False,
        "agent_id": "default",
        "model": "chat-model",
        "created_at": "2026-09-01T09:00:00+08:00",
        "updated_at": "2026-09-01T10:00:00+08:00",
        "messages": [
            {
                "id": "conversation-1",
                "role": "user",
                "content": "问题",
                "created_at": "2026-09-01T09:30:00+08:00",
            },
            {
                "id": "conversation-1:assistant",
                "role": "assistant",
                "content": "回答",
                "created_at": "2026-09-01T09:30:00+08:00",
            },
        ],
    }


@pytest.mark.asyncio
async def test_missing_session_uses_common_404_envelope() -> None:
    """不存在的会话必须返回稳定的 404 错误结构。"""
    host, _ = _host()

    async with _client(host) as client:
        response = await client.get("/api/v1/sessions/missing")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "session_not_found", "message": "Session 不存在"}
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", "   "])
async def test_empty_message_uses_common_422_envelope(content: str) -> None:
    """空字符串和纯空白消息都不得进入 Runtime。"""
    host, _ = _host(_session("session-1"))

    async with _client(host) as client:
        response = await client.post(
            "/api/v1/sessions/session-1/messages",
            json={"content": content},
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_request", "message": "请求参数无效"}
    }


@pytest.mark.asyncio
async def test_cancel_session_returns_accepted_run_reference() -> None:
    """显式停止按 Session 定位活动 Run，并返回正在取消状态。"""
    host, interaction = _host(_session("session-1"))

    async with _client(host) as client:
        response = await client.post(
            "/api/v1/sessions/session-1/cancel",
            json={"reason": "用户点击停止"},
        )

    assert response.status_code == 202
    assert response.json() == {
        "session_id": "session-1",
        "run_id": "run-active",
        "status": "cancelling",
    }
    assert interaction.cancel_calls == [("session-1", "用户点击停止")]


@pytest.mark.asyncio
async def test_cancel_without_active_run_returns_409() -> None:
    """没有活动 Run 时不得伪造取消成功。"""
    host, interaction = _host(_session("session-1"))
    interaction.cancelled_run_id = None

    async with _client(host) as client:
        response = await client.post(
            "/api/v1/sessions/session-1/cancel",
            json={},
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "no_active_run", "message": "Session 没有活动 Run"}
    }
    assert interaction.cancel_calls == [
        ("session-1", "用户从 GUI 停止运行"),
    ]


@pytest.mark.asyncio
async def test_cancel_missing_session_returns_404_without_calling_service() -> None:
    """不存在的 Session 必须在调用取消服务前被拒绝。"""
    host, interaction = _host()

    async with _client(host) as client:
        response = await client.post(
            "/api/v1/sessions/missing/cancel",
            json={},
        )

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "session_not_found", "message": "Session 不存在"}
    }
    assert interaction.cancel_calls == []


@pytest.mark.asyncio
async def test_lifespan_shuts_down_injected_host() -> None:
    """Web 应用退出时必须释放唯一应用宿主。"""
    host, _ = _host()

    async with _client(host):
        assert host.shutdown_called is False

    assert host.shutdown_called is True


def test_web_command_binds_only_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """本地 Web 启动命令不得监听全部网卡。"""
    captured: dict[str, str | int | bool] = {}

    def fake_run(
        app: str,
        *,
        factory: bool,
        host: str,
        port: int,
    ) -> None:
        """记录 Uvicorn 启动参数。"""
        captured.update(
            {"app": app, "factory": factory, "host": host, "port": port}
        )

    monkeypatch.setattr("uvicorn.run", fake_run)
    from dotclaw.channel.web.app import main

    main()

    assert captured == {
        "app": "dotclaw.channel.web.app:create_app",
        "factory": True,
        "host": "127.0.0.1",
        "port": 8765,
    }
