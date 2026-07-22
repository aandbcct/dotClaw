"""Host 私有的组件装配辅助（阶段 2）。

从原 ``agent/factory.py`` 迁入，作为 ``ApplicationHost`` 的私有构建块。
集中配置读取、关键/可降级初始化策略与各类基础设施的构造，不承载对话业务规则。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, TypeVar

if TYPE_CHECKING:
    from dotclaw.channel.base import Channel
    from dotclaw.config.settings import Config
    from dotclaw.context.ports import AgentDirectoryPort, MemorySearchPort, SkillRegistryPort
    from dotclaw.runtime.application.ports import ContextPort
    from dotclaw.skills.registry import SkillRegistry

logger = logging.getLogger("dotclaw.bootstrap.components")

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
# 组件构建
# ============================================================================

def _build_llm(config: Config, project_root: Path):
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


def _build_skills(config: Config, project_root: Path) -> "SkillRegistry | None":
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

    registry: SkillRegistry = SkillRegistry()
    for meta in metas:
        registry.register(meta)

    logger.info("已加载 %d 个 Skill", len(metas))
    return registry


def _build_tools(config: Config, skill_registry: "SkillRegistryPort | None"):
    """构建 ToolExecutor + ToolRegistry + ApprovalManager。

    基础 PolicyScope 只保留全局上限与资源约束。所有 Agent（含主 Agent）的
    收窄规则均通过 agent_policy_resolver(agent_id) 在每次 Run 冻结独立作用域，
    不写入共享 scope，避免 delegation 子 Agent 继承主 Agent 规则（四次审计修复）。
    """
    from dotclaw.tools.registry import ToolRegistry
    from dotclaw.tools.executor import ToolExecutor
    from dotclaw.tools.approval import ApprovalManager
    from dotclaw.tools.discovery import ToolDiscovery
    from dotclaw.tools.parser import SkillParser
    from dotclaw.tools.policy import (
        PolicyEngine,
        PolicyDecision,
        default_policy_scope,
    )
    from dotclaw.tools.capability import CapabilityBroker
    from dotclaw.agent.identity import load_agent_config as _load_id

    registry = ToolRegistry()

    # 阶段二：通过可信包 Discovery 自动发现并注册 builtin，不再手工 register_all。
    # Discovery 完成后才应用 disabled_tools（使用迁移后的新规范名）。
    if config.tools.builtin_enabled:
        for handler in ToolDiscovery.discover_builtin():
            registry.register(handler)

    for tool_name in config.tools.disabled_tools:
        registry.unregister(tool_name)

    # 阶段三：构造策略作用域（全局上限 + 资源约束），合并配置覆盖后回退设计默认值。
    scope = default_policy_scope(workspace_root=config.tools.policy.workspace_root)
    for profile, decision in config.tools.policy.rules.items():
        scope.global_rules[profile] = PolicyDecision(decision)
    if config.tools.policy.denied_paths:
        scope.denied_paths = config.tools.policy.denied_paths
    if config.tools.policy.allowed_mcp_servers:
        scope.allowed_mcp_servers = config.tools.policy.allowed_mcp_servers

    policy_engine = PolicyEngine(scope)
    capability_broker = CapabilityBroker()
    approval_mgr = ApprovalManager()

    # Agent 级策略按 agent_id 解析（带缓存），供 ToolExecutor 每次调用冻结独立作用域
    # （P1 修复：避免 delegation 子 Agent 继承主 Agent 规则，或全局作用域污染所有 Agent）。
    _policy_rules_cache: dict[str, object] = {}

    def _resolve_agent_policy_rules(agent_id: str) -> "dict[str, str] | None":
        if agent_id in _policy_rules_cache:
            return _policy_rules_cache[agent_id]  # type: ignore[return-value]
        try:
            rules = _load_id(agent_id=agent_id).policy_rules
        except Exception:
            rules = None
        _policy_rules_cache[agent_id] = rules
        return rules

    skill_parser = SkillParser(skill_registry) if skill_registry else None
    executor = ToolExecutor(
        registry=registry,
        approval_manager=approval_mgr,
        policy_engine=policy_engine,
        capability_broker=capability_broker,
        skill_parser=skill_parser,
        approval_commands=set(config.tools.approval_commands),
        agent_policy_resolver=_resolve_agent_policy_rules,
    )
    return executor


def _build_memory(config: Config, llm_proxy, project_root: Path):
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


def _build_mcp(config: Config, tool_executor):
    """构建 MCPToolProvider，MCP 未启用时返回 (None, None)。

    tool_executor 必须先行构建，Provider 复用其 registry / policy_engine /
    capability_broker，避免重复构造安全组件。
    """

    async def _init():
        if not (config.tools.mcp_enabled and config.tools.mcp_servers):
            return None, None

        from dotclaw.mcp import MCPToolProvider

        provider = MCPToolProvider(
            global_config=config.tools.mcp_global,
            server_configs=config.tools.mcp_servers,
            registry=tool_executor.registry,
            policy_engine=tool_executor.policy_engine,
            capability_broker=tool_executor.capability_broker,
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
    skill_registry: "SkillRegistryPort | None",
    memory_manager: "MemorySearchPort | None",
    agent_registry: "AgentDirectoryPort",
) -> "ContextPort":
    """构建基于注册表与 Plan Resolver 的 Runtime ContextPort。"""
    from dotclaw.context import ContextDependencies, build_context_provider

    return build_context_provider(ContextDependencies(
        skill_registry=skill_registry,
        memory_manager=memory_manager,
        agent_registry=agent_registry,
    ))
