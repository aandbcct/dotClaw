"""ApplicationHost —— 唯一公开组合根与资源生命周期宿主（阶段 2）。

负责读取配置、加载全部 Identity、按关键/可降级策略创建基础设施、装配 Runtime、
执行启动恢复，并在 ``shutdown()`` 中按依赖逆序关闭资源。Host 不承载对话业务规则、
渲染逻辑或 Runtime 状态机；``runtime_factory`` 与 ``_host_components`` 作为其私有
装配实现（总体设计 §4.1、§5.3）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ._host_components import (
    CRITICAL,
    _build_llm,
    _build_mcp,
    _build_memory,
    _build_skills,
    _build_tools,
    _init_async,
    _init_sync,
)
from .runtime_factory import RuntimeServices, build_runtime_services
from .session_interaction import SessionInteractionService
from ..orchestration.registry import AgentRegistry
from ..session.session import SessionManager

if TYPE_CHECKING:
    from dotclaw.channel.base import Channel
    from dotclaw.config.settings import Config
    from dotclaw.context.ports import ContextPort
    from dotclaw.llm.proxy import LLMProxy
    from dotclaw.mcp.provider import MCPToolProvider
    from dotclaw.memory.dream import DeepDream
    from dotclaw.skills.registry import SkillRegistry
    from dotclaw.tools.executor import ToolExecutor

logger = logging.getLogger("dotclaw.bootstrap.host")

AGENT_CONFIG_DIRNAME = ".dotclaw/agentConfig"


class ApplicationHost:
    """唯一公开启动对象：创建并持有全部应用级资源，统一生命周期。"""

    def __init__(self, config: Config, project_root: Path, channel: "Channel | None" = None) -> None:
        """绑定配置与项目根；实际资源在 ``initialize()`` 中装配。"""
        self._config: Config = config
        self._project_root: Path = project_root
        self._channel: Channel | None = channel

        # 运行期资源（由 initialize 填充）
        self._llm_proxy: LLMProxy | None = None
        self._session_manager: SessionManager | None = None
        self._agent_registry: AgentRegistry | None = None
        self._tool_executor: ToolExecutor | None = None
        self._mcp_provider: MCPToolProvider | None = None
        self._skill_registry: SkillRegistry | None = None
        self._memory_dream: DeepDream | None = None
        self._context_port: ContextPort | None = None
        self._runtime_services: RuntimeServices | None = None
        self._session_interaction: SessionInteractionService | None = None

    # ── 构建 ──

    @classmethod
    async def build(cls, channel: "Channel | None" = None) -> "ApplicationHost":
        """读取配置与项目根，装配全部资源并返回就绪的 Host。

        ``initialize()`` 中途失败时，先幂等 ``shutdown()`` 回收已创建的 MCP/Context
        等部分资源，再向上抛出异常，避免半初始化资源泄漏（总体设计 §5.3）。
        """
        from dotclaw.config import _find_project_root, get_config

        config = get_config()
        project_root = _find_project_root()
        host = cls(config, project_root, channel=channel)
        try:
            await host.initialize()
        except Exception:
            await host.shutdown()
            raise
        return host

    async def initialize(self) -> None:
        """按关键/可降级策略装配全部基础设施与 Runtime（总体设计 §5.3）。"""
        config = self._config
        root = self._project_root

        # ── 关键组件 ──
        self._llm_proxy = _build_llm(config, root)
        self._session_manager = SessionManager(config.session.directory)

        # ── 可降级组件 ──
        self._skill_registry = _init_sync("技能", lambda: _build_skills(config, root))
        # 工具缺失将明确终止启动（ToolExecutor 是 Runtime 必要依赖）。
        self._tool_executor = _init_sync(
            "工具", lambda: _build_tools(config, self._skill_registry), on_fail=CRITICAL
        )
        memory_mgr, self._memory_dream = await _init_async(
            "记忆", _build_memory(config, self._llm_proxy, root)
        ) or (None, None)

        # MCP 复用 tool_executor 的 registry / policy_engine / capability_broker。
        # 启动就绪语义：直接 await 首次发现完成（provider.start() 内部可并行发现各
        # server，失败 server 降级为 failed_servers），保证首个 Run 不遗漏 MCP 工具；
        # MCP 为可降级依赖，整体发现异常由 ``_init_async`` 降级为 None。
        self._mcp_provider = await _init_async(
            "MCP", _build_mcp(config, self._tool_executor)
        ) or None

        # ── 关键组件：加载全部 Identity ──
        self._agent_registry = AgentRegistry()
        self._agent_registry.load_all(root / AGENT_CONFIG_DIRNAME)
        if not self._agent_registry.list_all():
            raise RuntimeError("未加载任何 Identity，ApplicationHost 无法启动")

        # ── 默认 Identity（context plan 与创建会话兜底共用）──
        default_identity = self._resolve_default_agent_id()

        # ── 装配 Runtime（Host 私有组装函数）──
        text_stream_port = None
        if self._channel is not None:
            from dotclaw.channel.runtime_text_stream import ChannelTextStreamAdapter

            text_stream_port = ChannelTextStreamAdapter(self._channel)
        self._runtime_services = build_runtime_services(
            config=config,
            project_root=root,
            identity=default_identity,
            llm_proxy=self._llm_proxy,
            tool_executor=self._tool_executor,
            session_manager=self._session_manager,
            skill_registry=self._skill_registry,
            memory_manager=memory_mgr,
            agent_registry=self._agent_registry,
            text_stream_port=text_stream_port,
        )
        # Host 启动时补偿未决成功提交（总体设计 §5.3）。
        await self._runtime_services.run_repository.recover_pending_success_commits()
        self._context_port = self._runtime_services.context_port

        # ── 交互入口 ──
        self._session_interaction = SessionInteractionService(
            session_manager=self._session_manager,
            agent_registry=self._agent_registry,
            coordinator=self._runtime_services.coordinator,
            config=config,
            default_agent_id=default_identity.agent_id,
        )
        logger.info("ApplicationHost 就绪：%d 个 Identity 已注册", len(self._agent_registry.list_all()))

    def _resolve_default_agent_id(self):
        """显式优先、默认兜底的 Identity 选择（总体设计 §5.1）。"""
        registry = self._agent_registry
        assert registry is not None
        if registry.get("default") is not None:
            return registry.get("default")  # type: ignore[return-value]
        identities = registry.list_all()
        if len(identities) == 1:
            return identities[0]
        raise RuntimeError("无法确定默认 Identity，请确认已注册 default 或仅一个 Identity")

    # ── 对外能力 ──

    @property
    def config(self) -> Config:
        """返回当前全局配置。"""
        return self._config

    @property
    def session_interaction(self) -> SessionInteractionService:
        """返回按 Session 路由的交互入口。"""
        if self._session_interaction is None:
            raise RuntimeError("ApplicationHost 尚未初始化")
        return self._session_interaction

    @property
    def session_manager(self) -> SessionManager:
        """返回 Session 管理器。"""
        if self._session_manager is None:
            raise RuntimeError("ApplicationHost 尚未初始化")
        return self._session_manager

    @property
    def agent_registry(self) -> AgentRegistry:
        """返回全部已注册 Identity 的目录。"""
        if self._agent_registry is None:
            raise RuntimeError("ApplicationHost 尚未初始化")
        return self._agent_registry

    @property
    def tool_executor(self) -> "ToolExecutor | None":
        """返回仅供 CLI 诊断展示的工具执行器。"""
        return self._tool_executor

    @property
    def mcp_provider(self) -> "MCPToolProvider | None":
        """返回仅供 CLI 诊断展示的 MCP 提供者。"""
        return self._mcp_provider

    @property
    def skill_registry(self) -> "SkillRegistry | None":
        """返回仅供 CLI 诊断展示的技能目录。"""
        return self._skill_registry

    @property
    def memory_dream(self) -> "DeepDream | None":
        """返回可选的记忆蒸馏服务（CLI /dream 使用）。"""
        return self._memory_dream

    # ── 关闭 ──

    async def shutdown(self) -> None:
        """按依赖逆序关闭资源（总体设计 §5.3，启动就绪语义）。

        顺序：关闭已完成首次发现的 MCP Provider → 释放 Context 缓存
        → 释放其他可关闭资源。幂等：可重复调用，亦可在半初始化失败后回收部分资源。
        Agent 不参与资源关闭。
        """
        # 1) 关闭 MCP Provider（首次发现已在启动时完成；MCP 为可降级依赖）
        if self._mcp_provider is not None:
            try:
                await self._mcp_provider.shutdown()
            except Exception as e:
                logger.warning("MCP Provider 关闭异常: %s", e)
            self._mcp_provider = None
        # 2) 释放 Agent/Session/Run Context 缓存
        if self._context_port is not None:
            try:
                await self._context_port.release_all()
            except Exception as e:
                logger.warning("Context 缓存释放异常: %s", e)
            self._context_port = None
        # 3) 其他可关闭资源（当前无进程级资源需显式释放）
        logger.info("ApplicationHost 已关闭")
