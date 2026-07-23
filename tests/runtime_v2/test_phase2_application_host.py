"""阶段 2：ApplicationHost 作为唯一组合根与资源生命周期的验证。

覆盖开发计划 §5 的验证门槛：
- RuntimeServices 收缩为只暴露 Runtime 装配与恢复所需依赖（工具/MCP/Skills/记忆移出）；
- ApplicationHost.shutdown() 等待/取消后台 MCP 初始化并释放 Context 缓存；
- 关键依赖（Identity 注册）缺失时启动必须明确失败。
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from dotclaw.agent.identity import AgentIdentity
from dotclaw.bootstrap import application_host as app_host_mod
from dotclaw.bootstrap.application_host import ApplicationHost
from dotclaw.bootstrap.runtime_factory import RuntimeServices
from dotclaw.session.session import SessionManager


# ============================================================================
# 测试替身
# ============================================================================

class _FakeRunRepository:
    def __init__(self) -> None:
        self.recovered = False

    async def recover_pending_success_commits(self) -> None:
        self.recovered = True


class _FakeContextPort:
    def __init__(self) -> None:
        self.released = False

    async def release_all(self) -> None:
        self.released = True


class _FakeCoordinator:
    pass


class _FakeLLM:
    pass


class _FakeMCP:
    def __init__(self) -> None:
        self.shutdown_called = False

    async def shutdown(self) -> None:
        self.shutdown_called = True


class _FakeTools:
    registry = None
    policy_engine = None
    capability_broker = None


class _FakeApprovalRepository:
    """审批仓储替身：阶段 5 新增，供 SessionInteractionService 按 Session 清理审批。"""

    async def delete_by_session(self, session_id: str) -> None:
        pass

    def load(self, approval_id: str) -> object | None:
        return None


def _fake_config(session_dir: str) -> types.SimpleNamespace:
    """构造 initialize() 实际读取的最小配置（其余组件已打桩，不再深度读取）。"""
    tools_policy = types.SimpleNamespace(
        workspace_root="",
        rules={},
        denied_paths=[],
        allowed_mcp_servers=[],
    )
    tools = types.SimpleNamespace(
        builtin_enabled=False,
        disabled_tools=[],
        policy=tools_policy,
        approval_commands=[],
        mcp_enabled=False,
        mcp_servers={},
        mcp_global={},
    )
    skills = types.SimpleNamespace(enabled=False, directory=[], skip_prefix=[])
    session = types.SimpleNamespace(directory=session_dir)
    memory = None
    llm = types.SimpleNamespace(default_model="x")
    return types.SimpleNamespace(session=session, skills=skills, tools=tools, memory=memory, llm=llm)


def _patch_registry(identities: list[AgentIdentity]):
    """将 AgentRegistry 替换为按测试注入列表加载的替身。"""

    class _Registry:
        def __init__(self) -> None:
            self._identities: dict[str, AgentIdentity] = {}

        def load_all(self, agent_config_dir: Path) -> None:
            for identity in identities:
                self._identities[identity.agent_id] = identity

        def get(self, agent_id: str):
            return self._identities.get(agent_id)

        def list_all(self) -> list[AgentIdentity]:
            return list(self._identities.values())

    return _Registry


async def _noop_mcp():
    """可降级 MCP 构建桩：返回 None（未启用/发现失败）。"""
    return None


# ============================================================================
# 测试
# ============================================================================

def test_runtime_services_has_no_diagnostic_fields() -> None:
    """RuntimeServices 只暴露 engine/context_port/coordinator/run_repository/agent_registry。"""
    rs = RuntimeServices(
        engine=_FakeLLM(),
        context_port=_FakeContextPort(),
        coordinator=_FakeCoordinator(),
        run_repository=_FakeRunRepository(),
        agent_registry=_patch_registry([])(),
        approval_repository=_FakeApprovalRepository(),
    )
    for required in ("engine", "context_port", "coordinator", "run_repository", "agent_registry",
                     "approval_repository"):
        assert hasattr(rs, required), f"缺失必需字段 {required}"
    for removed in ("tool_executor", "mcp_provider", "skill_registry", "memory_dream"):
        assert not hasattr(rs, removed), f"诊断字段 {removed} 不应再出现在 RuntimeServices"


async def test_application_host_shutdown_releases_context_and_closes_mcp(tmp_path: Path) -> None:
    """Host 关闭：关闭已就绪的 MCP Provider、释放 Context 缓存（启动就绪语义，幂等）。"""
    host = ApplicationHost(_fake_config(str(tmp_path / "sessions")), tmp_path)

    fake_context = _FakeContextPort()
    fake_mcp = _FakeMCP()

    host._context_port = fake_context
    host._mcp_provider = fake_mcp
    host._session_manager = SessionManager(str(tmp_path / "sessions"))
    host._agent_registry = _patch_registry([AgentIdentity(agent_id="default")])()
    host._session_interaction = object()  # type: ignore[assignment]
    host._runtime_services = RuntimeServices(
        engine=_FakeLLM(),
        context_port=fake_context,
        coordinator=_FakeCoordinator(),
        run_repository=_FakeRunRepository(),
        agent_registry=host._agent_registry,
        approval_repository=_FakeApprovalRepository(),
    )

    await host.shutdown()
    assert fake_mcp.shutdown_called, "MCP Provider 应被关闭"
    assert fake_context.released, "Context 缓存应被释放"

    # 幂等：二次关闭不应报错，且已置空的资源不再重复操作。
    await host.shutdown()


async def test_application_host_build_fails_without_identities(tmp_path: Path, monkeypatch) -> None:
    """关键依赖（Identity 注册）缺失时，启动必须明确失败。"""
    import dotclaw.config as config_mod

    monkeypatch.setattr(config_mod, "get_config", lambda: _fake_config(str(tmp_path / "sessions")))
    monkeypatch.setattr(config_mod, "_find_project_root", lambda: tmp_path)
    monkeypatch.setattr(app_host_mod, "_build_llm", lambda config, root: _FakeLLM())
    monkeypatch.setattr(app_host_mod, "_build_skills", lambda config, root: None)
    monkeypatch.setattr(app_host_mod, "_build_tools", lambda config, skill_registry, http_client=None: _FakeTools())
    monkeypatch.setattr(app_host_mod, "_build_memory", lambda config, llm_proxy, root: (None, None))
    monkeypatch.setattr(app_host_mod, "_build_mcp", lambda config, tool_executor: _noop_mcp())

    def _fake_build_runtime_services(*, config, project_root, identity, llm_proxy, tool_executor,
                                      session_manager, skill_registry, memory_manager, agent_registry):
        return RuntimeServices(
            engine=_FakeLLM(),
            context_port=_FakeContextPort(),
            coordinator=_FakeCoordinator(),
            run_repository=_FakeRunRepository(),
            agent_registry=agent_registry,
            approval_repository=_FakeApprovalRepository(),
        )

    monkeypatch.setattr(app_host_mod, "build_runtime_services", _fake_build_runtime_services)
    monkeypatch.setattr(app_host_mod, "AgentRegistry", _patch_registry([]))

    with pytest.raises(RuntimeError):
        await ApplicationHost.build()


async def test_application_host_build_exposes_interaction_and_manager(tmp_path: Path, monkeypatch) -> None:
    """正常启动后暴露 SessionInteractionService 与 SessionManager（关键依赖齐备）。"""
    import dotclaw.config as config_mod

    monkeypatch.setattr(config_mod, "get_config", lambda: _fake_config(str(tmp_path / "sessions")))
    monkeypatch.setattr(config_mod, "_find_project_root", lambda: tmp_path)
    monkeypatch.setattr(app_host_mod, "_build_llm", lambda config, root: _FakeLLM())
    monkeypatch.setattr(app_host_mod, "_build_skills", lambda config, root: None)
    monkeypatch.setattr(app_host_mod, "_build_tools", lambda config, skill_registry, http_client=None: _FakeTools())
    monkeypatch.setattr(app_host_mod, "_build_memory", lambda config, llm_proxy, root: (None, None))
    monkeypatch.setattr(app_host_mod, "_build_mcp", lambda config, tool_executor: _noop_mcp())

    def _fake_build_runtime_services(*, config, project_root, identity, llm_proxy, tool_executor,
                                      session_manager, skill_registry, memory_manager, agent_registry):
        return RuntimeServices(
            engine=_FakeLLM(),
            context_port=_FakeContextPort(),
            coordinator=_FakeCoordinator(),
            run_repository=_FakeRunRepository(),
            agent_registry=agent_registry,
            approval_repository=_FakeApprovalRepository(),
        )

    monkeypatch.setattr(app_host_mod, "build_runtime_services", _fake_build_runtime_services)
    monkeypatch.setattr(
        app_host_mod, "AgentRegistry",
        _patch_registry([AgentIdentity(agent_id="default", agent_name="默认")]),
    )

    host = await ApplicationHost.build()
    try:
        assert host.session_interaction is not None
        assert host.session_manager is not None
        assert host.agent_registry.get("default") is not None
    finally:
        await host.shutdown()


async def test_application_host_build_cleans_up_partial_resources_on_init_failure(tmp_path: Path, monkeypatch) -> None:
    """initialize() 中途失败（空 Identity）时，build() 应先回收已建的 MCP Provider 再抛出。"""
    import dotclaw.config as config_mod

    monkeypatch.setattr(config_mod, "get_config", lambda: _fake_config(str(tmp_path / "sessions")))
    monkeypatch.setattr(config_mod, "_find_project_root", lambda: tmp_path)
    monkeypatch.setattr(app_host_mod, "_build_llm", lambda config, root: _FakeLLM())
    monkeypatch.setattr(app_host_mod, "_build_skills", lambda config, root: None)
    monkeypatch.setattr(app_host_mod, "_build_tools", lambda config, skill_registry, http_client=None: _FakeTools())
    monkeypatch.setattr(app_host_mod, "_build_memory", lambda config, llm_proxy, root: (None, None))

    fake_mcp = _FakeMCP()

    async def _fake_mcp():
        return fake_mcp

    monkeypatch.setattr(app_host_mod, "_build_mcp", lambda config, tool_executor: _fake_mcp())

    def _fake_build_runtime_services(*, config, project_root, identity, llm_proxy, tool_executor,
                                      session_manager, skill_registry, memory_manager, agent_registry):
        return RuntimeServices(
            engine=_FakeLLM(),
            context_port=_FakeContextPort(),
            coordinator=_FakeCoordinator(),
            run_repository=_FakeRunRepository(),
            agent_registry=agent_registry,
            approval_repository=_FakeApprovalRepository(),
        )

    monkeypatch.setattr(app_host_mod, "build_runtime_services", _fake_build_runtime_services)
    monkeypatch.setattr(app_host_mod, "AgentRegistry", _patch_registry([]))

    with pytest.raises(RuntimeError):
        await ApplicationHost.build()
    assert fake_mcp.shutdown_called, "initialize 失败后应回收已构建的 MCP Provider"
