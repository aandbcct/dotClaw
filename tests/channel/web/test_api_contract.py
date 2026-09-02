"""GUI v0.1 HTTP 接口契约测试。

本文件只固定 Web 适配边界，不调用真实模型、工具或网络。
生产实现位于 ``dotclaw.channel.web``，测试用于锁定后端 HTTP/SSE 契约。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from dotclaw.bootstrap.session_interaction import UnknownIdentityError
from dotclaw.session.session import Conversation, Session
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
        self.create_calls: list[tuple[str | None, str]] = []
        self.cancel_calls: list[tuple[str, str]] = []

    async def create_session(
        self,
        agent_id: str | None = None,
        title: str = "新对话",
    ) -> Session:
        """创建固定测试会话，或抛出预设业务错误。"""
        self.create_calls.append((agent_id, title))
        if self.create_error is not None:
            raise self.create_error
        session = _session(
            "created-1",
            title=title,
            agent_id=agent_id or "default",
            updated_at="2026-09-01T12:00:00+08:00",
        )
        self._manager.sessions[session.id] = session
        return session

    async def cancel_session(self, session_id: str, reason: str) -> str | None:
        """返回预设活动 Run，并记录显式取消原因。"""
        self.cancel_calls.append((session_id, reason))
        return self.cancelled_run_id


class _FakeHost:
    """模拟 Web lifespan 持有的应用宿主。"""

    def __init__(
        self,
        manager: _FakeSessionManager,
        interaction: _FakeSessionInteraction,
    ) -> None:
        self.session_manager: _FakeSessionManager = manager
        self.session_interaction: _FakeSessionInteraction = interaction
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
) -> Session:
    """构造字段完整的持久化 Session。"""
    return Session(
        id=session_id,
        title=title,
        agent_id=agent_id,
        model="chat-model",
        created_at="2026-09-01T09:00:00+08:00",
        updated_at=updated_at,
        conversations=conversations or [],
    )


async def _ready_host(host: _FakeHost) -> _FakeHost:
    """把已构造的测试宿主包装为异步工厂。"""
    return host


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
            "agent_id": "default",
            "model": "chat-model",
            "created_at": "2026-09-01T09:00:00+08:00",
            "updated_at": "2026-09-01T11:00:00+08:00",
        },
        {
            "id": "older",
            "title": "旧会话",
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
        "agent_id": "writer",
        "model": "chat-model",
        "created_at": "2026-09-01T09:00:00+08:00",
        "updated_at": "2026-09-01T12:00:00+08:00",
    }
    assert interaction.create_calls == [("writer", "GUI 会话")]


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
