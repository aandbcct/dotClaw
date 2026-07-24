"""阶段 1 验证：Session 收敛入口与显式 Identity 路由（开发计划 §4）。

覆盖开发计划阶段 1 的两条验证门槛：

- 不同 Session 可分别绑定不同 Identity，并生成对应 Run 策略；
- 提交严格按 Session 绑定的 ``agent_id`` 路由，不依赖任何运行时 Agent 对象。
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from dotclaw.agent.identity import AgentIdentity
from dotclaw.bootstrap.session_interaction import (
    SessionInteractionService,
    UnknownIdentityError,
)
from dotclaw.orchestration.registry import AgentRegistry
from dotclaw.session.session import SessionManager


@pytest.fixture
def registry() -> AgentRegistry:
    reg: AgentRegistry = AgentRegistry()
    reg.register(AgentIdentity(agent_id="a1", agent_name="A1"))
    reg.register(AgentIdentity(agent_id="a2", agent_name="A2"))
    return reg


@pytest.fixture
def session_manager(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path)


def _build_service(
    session_manager: SessionManager,
    registry: AgentRegistry,
    coordinator=None,
) -> SessionInteractionService:
    return SessionInteractionService(
        session_manager=session_manager,
        agent_registry=registry,
        coordinator=coordinator,
    )


async def test_distinct_sessions_route_to_distinct_identities(
    session_manager: SessionManager, registry: AgentRegistry
) -> None:
    """验证门槛(a)：两个 Session 绑定不同 Identity，路由到对应 Identity。"""
    s1: Session = await session_manager.create(agent_id="a1")
    s2: Session = await session_manager.create(agent_id="a2")

    service: SessionInteractionService = _build_service(session_manager, registry)
    id1 = service.get_identity(s1)
    id2 = service.get_identity(s2)

    assert id1.agent_id == "a1"
    assert id2.agent_id == "a2"
    assert id1.agent_id != id2.agent_id


async def test_create_session_binds_explicit_identity(
    session_manager: SessionManager, registry: AgentRegistry
) -> None:
    """SessionInteractionService 创建时显式落盘 agent_id。"""
    service: SessionInteractionService = _build_service(session_manager, registry)
    session: Session = await service.create_session(agent_id="a2", title="第二身份")
    assert session.agent_id == "a2"
    reloaded: Session | None = await session_manager.load(session.id)
    assert reloaded is not None and reloaded.agent_id == "a2"


async def test_create_session_default_identity_fallback(
    session_manager: SessionManager, tmp_path: Path
) -> None:
    """Host 默认兜底：仅一个 Identity 时，create_session() 绑定它。"""
    single: AgentRegistry = AgentRegistry()
    single.register(AgentIdentity(agent_id="only", agent_name="唯一"))
    service: SessionInteractionService = _build_service(session_manager, single)
    session: Session = await service.create_session()
    assert session.agent_id == "only"


async def test_unknown_session_identity_is_rejected(
    session_manager: SessionManager, registry: AgentRegistry
) -> None:
    """验证门槛(b)基础：Session 绑定未注册 Identity 时，路由必须明确报错。"""
    s: Session = await session_manager.create(agent_id="a1")
    s.agent_id = "ghost"  # 模拟持久化/外部注入未知 Identity
    service: SessionInteractionService = _build_service(session_manager, registry)
    with pytest.raises(UnknownIdentityError):
        service.get_identity(s)


async def test_submit_routes_by_session_identity(
    session_manager: SessionManager, registry: AgentRegistry
) -> None:
    """验证门槛(b)运行态：submit 经服务路由，RunRequest.agent_id 取 Session 绑定值。"""
    s1: Session = await session_manager.create(agent_id="a1")
    captured: dict[str, str] = {}

    class _FakeResult:
        final_message = types.SimpleNamespace(content="ok")
        status = None
        error = None
        has_streamed_response = False

    class _FakeCoordinator:
        async def submit_prepared(self, session_id: str, create_request, output_port=None) -> _FakeResult:
            req = await create_request()
            captured["agent_id"] = req.agent_id
            return _FakeResult()

    service: SessionInteractionService = _build_service(
        session_manager, registry, coordinator=_FakeCoordinator()
    )
    await service.submit(s1, "你好")
    assert captured["agent_id"] == "a1"


def test_service_has_no_config_dependency_minimal_entry(tmp_path: Path) -> None:
    """门面删除后 SessionInteractionService 不再持有 Config，符合“最小入口”收口目标。"""
    registry: AgentRegistry = AgentRegistry()
    registry.register(AgentIdentity(agent_id="a1", agent_name="A1"))
    service: SessionInteractionService = SessionInteractionService(
        session_manager=SessionManager(tmp_path),
        agent_registry=registry,
        coordinator=object(),
    )
    # 构造期不应再接收或存储 config 依赖。
    assert not hasattr(service, "_config")
