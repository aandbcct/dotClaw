"""Agent 工厂 —— 把配置和组件装配成可运行的 Agent 实例。

职责：
- 按 config.yaml + daily-assistant.yaml 创建所有依赖
- 恢复上次状态（agent_id、session_id）
- 统一初始化失败策略（critical 崩 vs degradable 降级）
- 返回完全就绪的 Agent 实例
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, TypeVar

if TYPE_CHECKING:
    from dotclaw.channel.base import Channel
    from dotclaw.context.ports import AgentDirectoryPort, MemorySearchPort, SkillRegistryPort
    from dotclaw.runtime.application.ports import ContextPort

logger = logging.getLogger("dotclaw.factory")

ComponentType = TypeVar("ComponentType")
"""初始化辅助函数返回的具体组件类型。"""


# ============================================================================
# 初始化失败策略
# ============================================================================

CRITICAL = "critical"
DEGRADE = "degrade"


def _init_sync(
    name: str,
    fn: Callable[[], ComponentType],
    on_fail: str = DEGRADE,
) -> ComponentType | None:
    """同步初始化一个组件，失败时按策略处理。"""
    try:
        return fn()
    except Exception as e:
        if on_fail == CRITICAL:
            raise
        logger.warning("%s 初始化失败（已降级）: %s", name, e)
        return None


async def _init_async(
    name: str,
    coro: Awaitable[ComponentType],
    on_fail: str = DEGRADE,
) -> ComponentType | None:
    """异步初始化一个组件，失败时按策略处理。"""
    try:
        return await coro
    except Exception as e:
        if on_fail == CRITICAL:
            raise
        logger.warning("%s 初始化失败（已降级）: %s", name, e)
        return None


# ============================================================================
# 状态持久化
# ============================================================================

def _state_path(project_root: Path) -> Path:
    return project_root / ".dotclaw" / "state.json"


def _load_state(project_root: Path) -> dict:
    """加载持久化状态，文件不存在时返回空字典。"""
    sp = _state_path(project_root)
    if not sp.exists():
        return {}
    try:
        return json.loads(sp.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(project_root: Path, state: dict) -> None:
    """写入持久化状态。"""
    sp = _state_path(project_root)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ============================================================================
# 组件构建
# ============================================================================

def _build_llm(config, project_root: Path):
    """构建 LLMProxy ← ModelRouter（内持 RateLimiter + CircuitBreaker）。"""
    from dotclaw.config import load_router_config, _build_router_config_from_legacy
    from dotclaw.llm.model_router import ModelRouter
    from dotclaw.llm.rate_limiter import RateLimiter, RateLimitConfig
    from dotclaw.llm.circuit_breaker import CircuitBreaker, BreakerConfig
    from dotclaw.llm.proxy import LLMProxy

    router_config_path = project_root / "model_router_config.yaml"
    if router_config_path.exists():
        router_config = load_router_config(str(router_config_path))
    else:
        router_config = _build_router_config_from_legacy(config.llm)

    rate_limit_configs = {}
    breaker_configs = {}
    for prov_name, prov_cfg in router_config.providers.items():
        rl_raw = prov_cfg.rate_limit
        rate_limit_configs[prov_name] = RateLimitConfig(
            requests_per_minute=rl_raw.get("requests_per_minute", 0),
        )
        cb_raw = prov_cfg.circuit_breaker if hasattr(prov_cfg, "circuit_breaker") else {}
        breaker_configs[prov_name] = BreakerConfig(
            failure_threshold=cb_raw.get("failure_threshold", 5),
            cooldown_seconds=cb_raw.get("cooldown_seconds", 30),
            half_open_max=cb_raw.get("half_open_max", 1),
        )

    rate_limiter = RateLimiter(rate_limit_configs)
    circuit_breaker = CircuitBreaker(breaker_configs)
    model_router = ModelRouter(router_config, rate_limiter, circuit_breaker)

    return LLMProxy(model_router=model_router)


def _build_skills(config, project_root: Path):
    """构建 SkillRegistry，技能未启用时返回 None。"""
    if not config.skills.enabled:
        return None

    from dotclaw.skills.scanner import SkillScanner
    from dotclaw.skills.registry import SkillRegistry

    skill_dirs = config.skills.directory
    if isinstance(skill_dirs, str):
        skill_dirs = [skill_dirs]

    skill_paths = []
    for d in skill_dirs:
        p = Path(d)
        skill_paths.append(str(p) if p.is_absolute() else str(project_root / d))

    scanner = SkillScanner(skill_paths, skip_prefix=config.skills.skip_prefix)
    metas = scanner.scan()

    registry = SkillRegistry()
    for meta in metas:
        registry.register(meta)

    logger.info("已加载 %d 个 Skill", len(metas))
    return registry


def _build_tools(config, skill_registry):
    """构建 ToolExecutor + ToolRegistry + ApprovalManager。"""
    from dotclaw.tools.registry import ToolRegistry
    from dotclaw.tools.executor import ToolExecutor
    from dotclaw.tools.approval import ApprovalManager
    from dotclaw.tools.builtin import register_all
    from dotclaw.tools.parser import SkillParser

    registry = ToolRegistry()

    if config.tools.builtin_enabled:
        register_all(registry)

    for tool_name in config.tools.disabled_tools:
        registry.unregister(tool_name)

    approval_mgr = ApprovalManager(
        approval_commands=config.tools.approval_commands,
    )

    skill_parser = SkillParser(skill_registry) if skill_registry else None
    executor = ToolExecutor(
        registry=registry,
        approval_manager=approval_mgr,
        skill_parser=skill_parser,
    )
    return executor


def _build_memory(config, llm_proxy, project_root: Path):
    """构建 MemoryManager + DeepDream。"""

    async def _init():
        if not (hasattr(config, "memory") and config.memory):
            return None, None

        from dotclaw.memory.storage import MemoryStorage
        from dotclaw.memory.chunker import TextChunker
        from dotclaw.memory.manager import MemoryManager
        from dotclaw.memory.flush import MemoryFlushManager
        from dotclaw.memory.dream import DeepDream
        from dotclaw.memory.embedding import EmbeddingCache

        storage: MemoryStorage = MemoryStorage(config.memory.get_db_path(project_root))
        chunker: TextChunker = TextChunker(
            max_tokens=config.memory.chunk_max_tokens,
            overlap_tokens=config.memory.chunk_overlap_tokens,
        )

        # Embedding 由 llm 模块统一管理，MemoryManager 直接通过 llm_proxy 调用
        embedding_cache: EmbeddingCache = EmbeddingCache()

        flush_mgr: MemoryFlushManager = MemoryFlushManager(
            workspace_dir=config.memory.get_workspace(project_root),
            llm=llm_proxy,
        )

        memory_mgr: MemoryManager = MemoryManager(
            storage=storage,
            chunker=chunker,
            workspace=config.memory.get_workspace(project_root),
            llm_proxy=llm_proxy,
            flush_manager=flush_mgr,
            embedding_cache=embedding_cache,
            embedding_dimensions=config.memory.embedding_dimensions,
            sync_on_search=config.memory.sync_on_search,
            vector_weight=config.memory.vector_weight,
            keyword_weight=config.memory.keyword_weight,
            max_results=config.memory.max_results,
            min_score=config.memory.min_score,
        )
        dream: DeepDream = DeepDream(
            config.memory.get_workspace(project_root),
            llm=llm_proxy,
            memory_manager=memory_mgr,
        )
        return memory_mgr, dream

    return _init()


def _build_mcp(config, tool_registry):
    """构建 MCPToolProvider，MCP 未启用时返回 (None, None)。"""

    async def _init():
        if not (config.tools.mcp_enabled and config.tools.mcp_servers):
            return None, None

        from dotclaw.mcp import MCPToolProvider

        provider = MCPToolProvider(
            global_config=config.tools.mcp_global,
            server_configs=config.tools.mcp_servers,
            registry=tool_registry,
            approval_commands=config.tools.approval_commands,
        )

        async def _load():
            try:
                tool_names = await provider.start()
                logger.info("已加载 %d 个 MCP 工具", len(tool_names))
            except Exception as e:
                logger.warning("MCP 加载失败: %s", e)

        task = asyncio.create_task(_load())
        return provider, task

    return _init()


def _build_context_port(
    skill_registry: SkillRegistryPort | None,
    memory_manager: MemorySearchPort | None,
    agent_registry: AgentDirectoryPort,
) -> ContextPort:
    """构建 Runtime v2 ContextPort 与作用域缓存。"""
    from dotclaw.context import (
        AvailableAgentsSlot,
        ContextDependencies,
        IdentitySlot,
        KnowledgeSlot,
        MemorySlot,
        ProjectSlot,
        SkillsSlot,
        SlotContextProvider,
        ToolsSlot,
        UserInfoSlot,
        WorkspaceSlot,
    )
    return SlotContextProvider(
        slots=(
            IdentitySlot(),
            ToolsSlot(),
            SkillsSlot(),
            AvailableAgentsSlot(),
            WorkspaceSlot(),
            UserInfoSlot(),
            MemorySlot(),
            KnowledgeSlot(),
            ProjectSlot(),
        ),
        dependencies=ContextDependencies(
            skill_registry=skill_registry,
            memory_manager=memory_manager,
            agent_registry=agent_registry,
        ),
    )

# ============================================================================
# 主工厂函数
# ============================================================================

async def build_agent(
    agent_id: str | None = None,
    channel: Channel | None = None,
) -> tuple[AgentCls, RuntimeServices, SessionManager]:
    """装配一个完全就绪的 Agent + Runtime v2 服务 + SessionManager。

    从 config.yaml + agent YAML + 上次状态重建所有依赖。

    Args:
        agent_id: Agent 标识。None = 恢复上次使用的 agent
        channel: 通信通道

    Returns:
        (Agent, Runtime, SessionManager)

    普通消息路径只装配 RuntimeEngine 与 SessionRunCoordinator。
    """
    from dotclaw.config import get_config, _find_project_root
    from dotclaw.session.session import SessionManager
    from dotclaw.agent import Agent as AgentCls
    from dotclaw.agent.identity import load_agent_config as load_id
    from dotclaw.bootstrap.runtime_factory import RuntimeServices, build_runtime_services
    from dotclaw.channel.runtime_text_stream import ChannelTextStreamAdapter
    from dotclaw.runtime.adapters.llm_context_compactor import LLMContextCompactor
    from dotclaw.runtime.application.session_history_preparation import (
        HistoryPreparationPolicy,
        SessionHistoryPreparationService,
    )

    config = get_config()
    project_root = _find_project_root()

    if agent_id is None:
        agent_id = "default"

    # ── 关键组件 ──
    llm_proxy = _build_llm(config, project_root)
    session_mgr: SessionManager = SessionManager(config.session.directory)
    # ── 可降级组件 ──
    skill_registry = _init_sync("技能", lambda: _build_skills(config, project_root))
    tool_executor = _init_sync("工具", lambda: _build_tools(config, skill_registry))
    memory_mgr, memory_dream = await _init_async("记忆", _build_memory(config, llm_proxy, project_root)) or (None, None)

    # MCP 需要 tool_registry
    tool_registry = tool_executor.registry if tool_executor else None
    mcp_provider, mcp_task = await _init_async(
        "MCP", _build_mcp(config, tool_registry)
    ) or (None, None)

    # ── AgentIdentity：直接从 YAML 加载 ──
    identity = load_id(agent_id=agent_id)

    # ── AgentRegistry：加载所有 Agent 配置 ──
    from dotclaw.orchestration.registry import AgentRegistry
    agent_registry = AgentRegistry()
    agent_config_dir = project_root / ".dotclaw" / "agentConfig"
    agent_registry.load_all(agent_config_dir)
    text_stream_port: ChannelTextStreamAdapter | None = (
        ChannelTextStreamAdapter(channel) if channel is not None else None
    )
    runtime_services = build_runtime_services(
        config=config,
        project_root=project_root,
        identity=identity,
        llm_proxy=llm_proxy,
        tool_executor=tool_executor,
        session_manager=session_mgr,
        skill_registry=skill_registry,
        memory_manager=memory_mgr,
        agent_registry=agent_registry,
        mcp_provider=mcp_provider,
        text_stream_port=text_stream_port,
    )
    await runtime_services.run_repository.recover_pending_success_commits()
    reserved_context_tokens: int = max(1024, config.agent.max_context_tokens // 4)
    history_preparation_service: SessionHistoryPreparationService = SessionHistoryPreparationService(
        store=session_mgr,
        compactor=LLMContextCompactor(llm_proxy),
        policy=HistoryPreparationPolicy(
            max_context_tokens=config.agent.max_context_tokens,
            max_recent_conversations=config.agent.keep_recent_messages,
            reserved_tokens=reserved_context_tokens,
        ),
    )

    agent: AgentCls = AgentCls(
        identity=identity,
        coordinator=runtime_services.coordinator,
        config=config,
        tool_executor=tool_executor,
        mcp_provider=mcp_provider,
        skill_registry=skill_registry,
        memory_dream=memory_dream,
        mcp_task=mcp_task,
        history_preparation_service=history_preparation_service,
    )
    logger.info("Agent [%s] 的 Runtime v2 服务已就绪", agent.agent_id)
    return agent, runtime_services, session_mgr

