"""dotClaw 主入口（Phase 5 升级）"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# 确保 src 在路径中
sys.path.insert(0, str(Path(__file__).parent.parent))

# 屏蔽第三方库的 INFO 日志（避免在交互界面输出 HTTP 请求日志）
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from dotclaw.config import get_config, load_config
from dotclaw.channel.cli import CLIChannel
from dotclaw.memory.store import SessionManager
from dotclaw.agent.loop import AgentLoop
from dotclaw.llm.proxy import LLMProxy


def _print_banner():
    banner = """
+============================================+
|         dotClaw v0.1.0                    |
|   Lightweight AI Agent Framework          |
+============================================+
"""
    print(banner)


async def _run_cli():
    """运行 CLI 交互"""
    _print_banner()

    config = load_config()
    channel = CLIChannel()

    # 初始化各组件
    session_mgr = SessionManager(config.session.directory)

    # ---- P2 新增：多供应商路由初始化 ----
    from dotclaw.config import (
        _find_project_root,
        load_router_config,
        _build_router_config_from_legacy,
    )
    from dotclaw.llm.model_router import ModelRouter
    from dotclaw.common.rate_limiter import RateLimiter, RateLimitConfig

    project_root = _find_project_root()
    router_config_path = project_root / "model_router_config.yaml"

    if router_config_path.exists():
        router_config = load_router_config(str(router_config_path))
    else:
        # 后向兼容：从旧 config.llm 构建 RouterConfig
        router_config = _build_router_config_from_legacy(config.llm)

    model_router = ModelRouter(router_config)

    # 从各 provider 配置提取 rate_limit
    rate_limit_configs = {}
    for prov_name, prov_cfg in router_config.providers.items():
        rl_raw = prov_cfg.rate_limit
        rate_limit_configs[prov_name] = RateLimitConfig(
            requests_per_minute=rl_raw.get("requests_per_minute", 0),
        )

    rate_limiter = RateLimiter(rate_limit_configs)
    llm_proxy = LLMProxy(model_router=model_router, rate_limiter=rate_limiter)
    # ---- P2 路由初始化结束 ----

    # ---- Phase 5 新增：工具层新架构初始化 ----
    from dotclaw.tools.registry import ToolRegistry
    from dotclaw.tools.executor import ToolExecutor
    from dotclaw.tools.approval import ApprovalManager
    from dotclaw.tools.builtin import register_all

    # 1. 创建注册表
    tool_registry = ToolRegistry()

    # 2. 注册内置工具（仅在 builtin_enabled 为 true 时）
    if config.tools.builtin_enabled:
        register_all(tool_registry)

    # 2b. 根据配置禁用指定工具（向后兼容旧 config.exec.enabled: false）
    for tool_name in config.tools.disabled_tools:
        tool_registry.unregister(tool_name)

    # 3. 创建审批管理器（从 config 加载）
    approval_mgr = ApprovalManager(
        approval_commands=config.tools.approval_commands,
    )

    # 4. 创建执行器
    tool_executor = ToolExecutor(
        registry=tool_registry,
        approval_manager=approval_mgr,
    )
    # ---- Phase 5 工具层初始化结束 ----

    # ---- P4 新增：记忆系统初始化 ----
    from dotclaw.config import _find_project_root as _root
    project_root = _root()
    memory_mgr = None
    memory_dream = None

    if hasattr(config, 'memory') and config.memory:
        try:
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

            # EmbeddingProvider 可选
            embedding = None
            if config.memory.embedding_provider and config.memory.embedding_api_key:
                from dotclaw.memory.embedding import OpenAIEmbeddingProvider, EmbeddingCache
                embedding = OpenAIEmbeddingProvider(
                    api_base=config.memory.embedding_api_base,
                    api_key=config.memory.embedding_api_key,
                    model=config.memory.embedding_model,
                    dimensions=config.memory.embedding_dimensions,
                )

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
                embedding_cache=EmbeddingCache(),
                sync_on_search=config.memory.sync_on_search,
                vector_weight=config.memory.vector_weight,
                keyword_weight=config.memory.keyword_weight,
                max_results=config.memory.max_results,
                min_score=config.memory.min_score,
            )

            # 初始化 DeepDream（稍后通过 /dream 命令触发）
            dream = DeepDream(config.memory.get_workspace(project_root), llm=llm_proxy)
            memory_dream = dream
        except Exception as e:
            channel.print_info(f"  记忆系统初始化失败（已降级为无记忆模式）: {e}")
            memory_mgr = None

    # ---- P4 记忆系统初始化结束 ----

    # 确保至少有默认会话
    sessions = await session_mgr.list_all()
    current_session = sessions[0] if sessions else await session_mgr.create("主对话")

    channel.print_info(f"当前对话: [{current_session.id}] {current_session.title}")
    channel.print_info(f"可用模型: {', '.join(llm_proxy.available_models)}")
    channel.print_info("输入 /help 查看命令\n")

    # ---- P3 新增：PromptBuilder + AgentLogger（Phase 5 升级） ----
    from dotclaw.agent.logger import AgentLogger
    from dotclaw.agent.prompt.builder import PromptBuilder
    from dotclaw.agent.prompt.providers import (
        RoleProvider, RulesProvider, ToolsProvider,
        MemoryProvider, SkillsProvider,
    )

    agent_logger = AgentLogger(
        level=config.debug.level,
        log_file=config.debug.log_file,
    )
    prompt_builder = PromptBuilder([
        RoleProvider(),
        RulesProvider(),
        ToolsProvider(),
        MemoryProvider(),
        # SkillsProvider(),    ← P7 激活
    ])

    agent = AgentLoop(
        llm=llm_proxy,
        session=current_session,
        session_mgr=session_mgr,
        channel=channel,
        config=config,
        tool_executor=tool_executor,
        prompt_builder=prompt_builder,   # P3 新增
        logger=agent_logger,             # P3 新增（Phase 5 合并 DebugManager）
        memory_mgr=memory_mgr,           # P4 新增
    )

    while True:
        try:
            user_input = await channel.receive()

            if not user_input.strip():
                continue

            # 处理命令
            if user_input.startswith("/"):
                cmd = user_input.split()[0].lower()
                args = user_input[len(cmd):].strip()

                if cmd == "/quit":
                    channel.print_info("再见！👋")
                    break
                elif cmd == "/help":
                    _print_help(channel)
                elif cmd == "/new":
                    title = args or "新对话"
                    current_session = await session_mgr.create(title)
                    agent.session = current_session
                    channel.print_info(f"已创建并切换到新对话: [{current_session.id}] {title}")
                elif cmd == "/list":
                    await _cmd_list(channel, session_mgr, current_session)
                elif cmd == "/switch":
                    if args:
                        await _cmd_switch(channel, session_mgr, agent, args)
                    else:
                        channel.print_error("用法: /switch <会话ID>")
                elif cmd == "/delete":
                    if args:
                        await _cmd_delete(channel, session_mgr, agent, args)
                    else:
                        channel.print_error("用法: /delete <会话ID>")
                elif cmd == "/debug":
                    agent.debug_trace(channel)
                elif cmd == "/dream":
                    await _cmd_dream_async(channel, memory_dream)
                elif cmd == "/tools":
                    _cmd_tools(channel, tool_executor)
                elif cmd == "/model":
                    if args:
                        agent.model = args
                        channel.print_info(f"已切换模型: {args}")
                    else:
                        channel.print_info(f"当前模型: {agent.model}")
                else:
                    channel.print_error(f"未知命令: {cmd}，输入 /help 查看可用命令")
                continue

            # 普通对话
            await agent.run(user_input)

        except KeyboardInterrupt:
            channel.print_info("\n(输入 /quit 退出)")
        except Exception as e:
            channel.print_error(f"错误: {e}")


def _print_help(channel: CLIChannel):
    channel.print_info("""
dotClaw 命令:
  /new [标题]      新建对话
  /list            列出所有对话
  /switch <id>     切换到指定对话
  /delete <id>      删除对话
  /debug           查看最近推理过程
  /tools           列出可用工具
  /dream           触发记忆蒸馏
  /model <名称>    切换模型
  /help            显示帮助
  /quit            退出
""")


async def _cmd_list(channel, session_mgr, current):
    sessions = await session_mgr.list_all()
    channel.print_info("所有对话:")
    for s in sessions:
        mark = " ← 当前" if s.id == current.id else ""
        channel.print_info(f"  [{s.id}] {s.title} ({s.updated_at[:10]}){mark}")


def _cmd_tools(channel, tool_executor):
    """列出所有可用工具（Phase 5 升级 — 从 ToolExecutor 读取审批状态）"""
    definitions = tool_executor.get_definitions()
    if not definitions:
        channel.print_info("(没有注册任何工具)")
        return
    channel.print_info(f"可用工具 ({len(definitions)} 个):")
    for d in definitions:
        handler = tool_executor.get_handler(d.name)
        mark = ""
        if handler and handler.definition().needs_approval:
            mark = " [需审批]"
        channel.print_info(f"  {d.name}{mark}: {d.description}")


async def _cmd_dream_async(channel, dream):
    """手动触发 Deep Dream 蒸馏"""
    if not dream:
        channel.print_error("Dream: 记忆系统未初始化")
        return
    try:
        result = await dream.run()
        channel.print_info(f"Dream: {result}")
    except Exception as e:
        channel.print_error(f"Dream 失败: {e}")


async def _cmd_switch(channel, session_mgr, agent, session_id: str):
    session = await session_mgr.load(session_id)
    if session:
        agent.session = session
        channel.print_info(f"已切换到 [{session.id}] {session.title}")
    else:
        channel.print_error(f"未找到会话: {session_id}")


async def _cmd_delete(channel, session_mgr, agent, session_id: str):
    deleted = await session_mgr.delete(session_id)
    if deleted:
        channel.print_info(f"已删除会话: {session_id}")
        if agent.session.id == session_id:
            sessions = await session_mgr.list_all()
            if sessions:
                agent.session = sessions[0]
                channel.print_info(f"已切换到 [{agent.session.id}] {agent.session.title}")
    else:
        channel.print_error(f"未找到会话: {session_id}")


def main():
    try:
        asyncio.run(_run_cli())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
