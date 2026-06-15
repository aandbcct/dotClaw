"""Agent 工厂 —— 把配置和组件装配成可运行的 Agent 实例。

职责：
- 按 config.yaml + coding-assistant.yaml 创建所有依赖
- 恢复上次状态（agent_id、session_id）
- 统一初始化失败策略（critical 崩 vs degradable 降级）
- 返回完全就绪的 Agent 实例
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("dotclaw.factory")


# ============================================================================
# 初始化失败策略
# ============================================================================

CRITICAL = "critical"
DEGRADE = "degrade"


def _init_sync(name: str, fn, on_fail: str = DEGRADE) -> Any:
    """同步初始化一个组件，失败时按策略处理。"""
    try:
        return fn()
    except Exception as e:
        if on_fail == CRITICAL:
            raise
        logger.warning("%s 初始化失败（已降级）: %s", name, e)
        return None


async def _init_async(name: str, coro, on_fail: str = DEGRADE) -> Any:
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
    """构建 LLMProxy + ModelRouter + RateLimiter。"""
    from dotclaw.config import load_router_config, _build_router_config_from_legacy
    from dotclaw.llm.model_router import ModelRouter
    from dotclaw.common.rate_limiter import RateLimiter, RateLimitConfig
    from dotclaw.llm.proxy import LLMProxy

    router_config_path = project_root / "model_router_config.yaml"
    if router_config_path.exists():
        router_config = load_router_config(str(router_config_path))
    else:
        router_config = _build_router_config_from_legacy(config.llm)

    model_router = ModelRouter(router_config)

    rate_limit_configs = {}
    for prov_name, prov_cfg in router_config.providers.items():
        rl_raw = prov_cfg.rate_limit
        rate_limit_configs[prov_name] = RateLimitConfig(
            requests_per_minute=rl_raw.get("requests_per_minute", 0),
        )
    rate_limiter = RateLimiter(rate_limit_configs)

    return LLMProxy(model_router=model_router, rate_limiter=rate_limiter)


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

        storage = MemoryStorage(config.memory.get_db_path(project_root))
        chunker = TextChunker(
            max_tokens=config.memory.chunk_max_tokens,
            overlap_tokens=config.memory.chunk_overlap_tokens,
        )

        embedding = None
        embedding_cache = None
        if config.memory.embedding_provider and config.memory.embedding_api_key:
            from dotclaw.memory.embedding import OpenAIEmbeddingProvider, EmbeddingCache
            embedding = OpenAIEmbeddingProvider(
                api_base=config.memory.embedding_api_base,
                api_key=config.memory.embedding_api_key,
                model=config.memory.embedding_model,
                dimensions=config.memory.embedding_dimensions,
            )
            embedding_cache = EmbeddingCache()

        flush_mgr = MemoryFlushManager(
            workspace_dir=config.memory.get_workspace(project_root),
            llm=llm_proxy,
        )

        memory_mgr = MemoryManager(
            storage=storage,
            chunker=chunker,
            workspace=config.memory.get_workspace(project_root),
            embedding_provider=embedding,
            flush_manager=flush_mgr,
            embedding_cache=embedding_cache,
            sync_on_search=config.memory.sync_on_search,
            vector_weight=config.memory.vector_weight,
            keyword_weight=config.memory.keyword_weight,
            max_results=config.memory.max_results,
            min_score=config.memory.min_score,
        )
        dream = DeepDream(config.memory.get_workspace(project_root), llm=llm_proxy)
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


def _build_prompt_builder():
    """构建 PromptBuilder。"""
    from dotclaw.agent.prompt.builder import PromptBuilder
    from dotclaw.agent.prompt.providers import (
        RoleProvider, RulesProvider, ToolsProvider,
        MemoryProvider, SkillsProvider,
    )
    return PromptBuilder([
        RoleProvider(),
        RulesProvider(),
        ToolsProvider(),
        MemoryProvider(),
        SkillsProvider(),
    ])


# ============================================================================
# 主工厂函数
# ============================================================================

async def build_agent(
    agent_id: str | None = None,
    channel: Any = None,
) -> "Agent":
    """装配一个完全就绪的 Agent 实例。

    从 config.yaml + coding-assistant.yaml + 上次状态重建所有依赖，
    失败组件按策略降级（nonexistent）或崩溃（critical）。

    Args:
        agent_id: Agent 标识。None = 恢复上次使用的 agent，或使用 "default"
        channel: 通信通道。None = 由 main() 提供

    Returns:
        完全就绪的 Agent
    """
    from dotclaw.config import get_config, _find_project_root
    from dotclaw.memory.store import SessionManager
    from dotclaw.agent import Agent, load_agent_config

    config = get_config()
    project_root = _find_project_root()

    # ── 加载上次状态 ──
    state = _load_state(project_root)
    if agent_id is None:
        agent_id = state.get("last_agent_id", "default")

    # ── 关键组件：失败则崩 ──
    llm_proxy = _build_llm(config, project_root)
    session_mgr = SessionManager(config.session.directory)
    prompt_builder = _build_prompt_builder()

    # ── 可降级组件 ──
    skill_registry = _init_sync("技能", lambda: _build_skills(config, project_root))
    tool_executor = _init_sync("工具", lambda: _build_tools(config, skill_registry))
    memory_mgr, memory_dream = await _init_async("记忆", _build_memory(config, llm_proxy, project_root)) or (None, None)

    # MCP 需要 tool_registry，所以从 executor 拿
    tool_registry = tool_executor.registry if tool_executor else None
    mcp_provider, mcp_task = await _init_async(
        "MCP", _build_mcp(config, tool_registry)
    ) or (None, None)

    # ── Agent 配置 ──
    agent_config = load_agent_config(agent_id=agent_id)

    # ── 组装 ──
    from dotclaw.agent import Agent as AgentCls
    agent = AgentCls(
        agent_config=agent_config,
        config=config,
        llm=llm_proxy,
        session_mgr=session_mgr,
        channel=channel,
        tool_executor=tool_executor,
        prompt_builder=prompt_builder,
        memory_mgr=memory_mgr,
        skill_registry=skill_registry,
        mcp_provider=mcp_provider,
        memory_dream=memory_dream,
        mcp_task=mcp_task,
    )

    # ── 恢复上次 session ──
    sessions = await session_mgr.list_all()
    last_session_id = state.get("last_session_id")
    if last_session_id:
        for s in sessions:
            if s.id == last_session_id:
                agent.session = s
                break
    if agent.session is None:
        agent.session = sessions[0] if sessions else await agent.new_session("主对话")

    # ── 保存状态 ──
    _save_state(project_root, {
        "last_agent_id": agent.agent_id,
        "last_session_id": agent.session.id if agent.session else "",
    })

    logger.info("Agent [%s] 就绪，session: %s", agent.agent_id,
                agent.session.id if agent.session else "none")
    return agent
