"""Runtime v2 的组合根，只在此处组装基础设施适配器。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ..agent.identity import AgentIdentity
from ..config.settings import Config
from ..config.settings import load_router_config
from ..context import (
    ContextDependencies,
    ContextPlanConfigurationPort,
    InMemoryContextPlanConfiguration,
    build_context_provider,
    default_context_plan_configuration,
)
from ..runtime.adapters import (
    AgentPolicyResolver,
    ApprovalRepositoryAdapter,
    CheckpointRepositoryAdapter,
    RunRepositoryAdapter,
    TiktokenTokenCounter,
    LLMContextCompactor,
    LLMProxyAdapter,
    SessionConversationProjector,
    ToolExecutorAdapter,
)
from ..runtime.application.approval_service import ApprovalService
from ..runtime.application.cancellation_service import CancellationService
from ..runtime.application.engine import RuntimeEngine
from ..runtime.application.session_run_coordinator import SessionRunCoordinator
from ..runtime.application.ports import TextStreamPort
from ..runtime.application.ports import ContextPort
from ..runtime.domain.context import ContextOwner
from ..session.session import SessionManager
from ..llm.proxy import LLMProxy
from ..tools.executor import ToolExecutor
from ..skills.registry import SkillRegistry
from ..memory.manager import MemoryManager
from ..memory.dream import DeepDream
from ..orchestration.registry import AgentRegistry
from ..orchestration.dispatcher import AgentDispatcher
from ..orchestration.message_broker import TaskMessageBroker
from ..orchestration.runtime_delegation_adapter import RuntimeDelegationAdapter
from ..mcp.provider import MCPToolProvider


@dataclass(frozen=True)
class RuntimeServices:
    """普通入口需要的新版 Runtime 服务及兼容展示依赖。"""

    engine: RuntimeEngine
    context_port: ContextPort
    coordinator: SessionRunCoordinator
    run_repository: RunRepositoryAdapter
    """启动阶段用于补偿未决成功提交的本地运行仓储。"""
    tool_executor: ToolExecutor
    mcp_provider: MCPToolProvider | None
    skill_registry: SkillRegistry | None
    memory_dream: DeepDream | None
    """仅供 CLI 诊断展示的记忆蒸馏服务（阶段 2 起由 Host 统一持有）。"""
    agent_registry: AgentRegistry
    """所有可用 Identity 的目录，供 SessionInteractionService 路由与校验。"""


def build_runtime_services(
    *,
    config: Config,
    project_root: Path,
    identity: AgentIdentity,
    llm_proxy: LLMProxy,
    tool_executor: ToolExecutor,
    session_manager: SessionManager,
    skill_registry: SkillRegistry | None,
    memory_manager: MemoryManager | None,
    agent_registry: AgentRegistry,
    mcp_provider: MCPToolProvider | None,
    memory_dream: DeepDream | None = None,
    text_stream_port: TextStreamPort | None = None,
) -> RuntimeServices:
    """按 Port 边界装配 RuntimeEngine 与 SessionRunCoordinator。"""
    if tool_executor is None:
        raise RuntimeError("Runtime v2 普通入口需要 ToolExecutor")
    storage_root: Path = _storage_root(project_root, config.session.directory)
    context_port = build_context_provider(ContextDependencies(
        skill_registry=skill_registry,
        memory_manager=memory_manager,
        agent_registry=agent_registry,
        plan_configuration=_agent_context_plan_configuration(identity),
    ))
    session_manager.set_deletion_handler(_session_context_releaser(context_port))
    run_repository: RunRepositoryAdapter = RunRepositoryAdapter(
        storage_root,
        SessionConversationProjector(session_manager),
    )
    approval_repository: ApprovalRepositoryAdapter = ApprovalRepositoryAdapter(storage_root)
    dispatcher: AgentDispatcher = AgentDispatcher(TaskMessageBroker())
    delegation_port: RuntimeDelegationAdapter = RuntimeDelegationAdapter(
        session_manager,
        agent_registry,
        dispatcher,
    )
    engine = RuntimeEngine(
        run_repository=run_repository,
        checkpoint_repository=CheckpointRepositoryAdapter(storage_root),
        context_port=context_port,
        llm_port=LLMProxyAdapter(llm_proxy, text_stream_port),
        tool_port=ToolExecutorAdapter(tool_executor),
        policy_port=AgentPolicyResolver(
            identity,
            config,
            tool_executor,
            project_root,
            agent_registry,
            load_router_config(project_root / "model_router_config.yaml"),
        ),
        approval_service=ApprovalService(approval_repository),
        cancellation_service=CancellationService(),
        delegation_port=delegation_port,
        token_counter=TiktokenTokenCounter(),
        history_compactor=LLMContextCompactor(llm_proxy),
    )
    coordinator: SessionRunCoordinator = SessionRunCoordinator(engine)
    delegation_port.bind_coordinator(coordinator)
    return RuntimeServices(
        engine=engine,
        context_port=context_port,
        coordinator=coordinator,
        run_repository=run_repository,
        tool_executor=tool_executor,
        mcp_provider=mcp_provider,
        skill_registry=skill_registry,
        memory_dream=memory_dream,
        agent_registry=agent_registry,
    )


def _storage_root(project_root: Path, configured_directory: str) -> Path:
    """将 Session 存储目录解析为与 SessionManager 相同的绝对根目录。"""
    directory = Path(configured_directory)
    return directory if directory.is_absolute() else project_root / directory


def _agent_context_plan_configuration(identity: AgentIdentity) -> ContextPlanConfigurationPort | None:
    """将 Agent 显式 Slot 启用项覆盖到完整多 Owner 默认计划中。"""
    if identity.context_slot_ids is None:
        return None
    defaults: InMemoryContextPlanConfiguration = default_context_plan_configuration()
    return InMemoryContextPlanConfiguration(
        default_configurations=defaults.default_configurations,
        owner_configurations={ContextOwner.AGENT: {identity.agent_id: identity.context_slot_ids}},
    )


def _session_context_releaser(context_port: ContextPort) -> Callable[[str], Awaitable[None]]:
    """构造只释放指定 Session Owner 缓存的删除回调。"""
    async def release(session_id: str) -> None:
        """在 Session 文件删除后释放对应 Slot 实例。"""
        await context_port.release_scope(ContextOwner.SESSION, session_id)

    return release
