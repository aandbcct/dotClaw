"""dotClaw 主入口"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).parent.parent))

if TYPE_CHECKING:
    from dotclaw.config.settings import Config

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("./data/dotclaw.log", encoding="utf-8"),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from dotclaw.channel.cli import CLIChannel
from dotclaw.channel.runtime_text_stream import ChannelTextStreamAdapter
from dotclaw.session import Session, SessionManager
from dotclaw.bootstrap import ApplicationHost
from dotclaw.bootstrap.session_interaction import (
    SessionDeletionRejected,
    SessionInteractionService,
    format_run_result,
)
from dotclaw.cli.banner import build_banner, console as rich_console
from dotclaw.mcp.provider import MCPToolProvider
from dotclaw.memory.dream import DeepDream
from dotclaw.skills.registry import SkillRegistry
from dotclaw.runtime.application.dto import RunResult
from dotclaw.runtime.application.ports import LLMOutputPort
from dotclaw.runtime.domain.facts import RunErrorCode, RunStatus
from dotclaw.tools.base import ToolDefinition, ToolSource
from dotclaw.tools.executor import ToolExecutor


async def _run_cli() -> None:
    channel: CLIChannel = CLIChannel()

    channel.print_info("组件初始化中...")
    # 阶段 2：ApplicationHost 作为唯一组合根，统一装配与持有全部资源。
    host: ApplicationHost = await ApplicationHost.build(channel=channel)
    try:
        config = host.config
        logging.getLogger().setLevel(config.debug.level)

        service: SessionInteractionService = host.session_interaction
        session_mgr: SessionManager = host.session_manager

        sessions: list[Session] = await session_mgr.list_all()
        if sessions:
            current_session: Session = sessions[0]
        else:
            current_session = await service.create_session(title="主对话")

        # 按当前 Session 绑定的 Identity 取得展示信息并打印 Banner。
        _refresh_banner(service, current_session, config)

        while True:
            try:
                user_input: str = await channel.receive()
                if not user_input.strip():
                    continue

                # 本次消息的运行级输出端口：CLI 每次消息构造，只服务本 Run。
                output_port: LLMOutputPort = ChannelTextStreamAdapter(channel)

                # 每次交互按当前 Session 绑定的 Identity 路由，提交严格由 Session 权威驱动。

                if user_input.startswith("/"):
                    cmd: str = user_input.split()[0].lower()
                    args: str = user_input[len(cmd):].strip()

                    if cmd == "/quit":
                        channel.print_info("再见！👋")
                        break
                    elif cmd == "/help":
                        _print_help(channel)
                    elif cmd == "/new":
                        title: str = args or "新对话"
                        current_session = await service.create_session(title=title)
                        channel.print_info(f"已创建并切换到新对话: [{current_session.id}] {title}")
                        _refresh_banner(service, current_session, config)
                    elif cmd == "/list":
                        await _cmd_list(channel, session_mgr, current_session)
                    elif cmd == "/switch":
                        if args:
                            s: Session | None = await session_mgr.load(args)
                            if s:
                                current_session = s
                                channel.print_info(f"已切换到 [{s.id}] {s.title}")
                                _refresh_banner(service, current_session, config)
                            else:
                                channel.print_error(f"未找到对话: {args}")
                        else:
                            channel.print_error("用法: /switch <对话ID>")
                    elif cmd == "/delete":
                        if args:
                            existing: Session | None = await session_mgr.load(args)
                            if existing is None:
                                channel.print_error(f"未找到对话: {args}")
                            else:
                                # 阶段 5：应用级删除协调流程，活动 Run 会被明确拒绝。
                                try:
                                    await service.delete_session(args)
                                except SessionDeletionRejected as e:
                                    channel.print_error(f"无法删除对话 {args}：{e}")
                                else:
                                    channel.print_info(f"已删除对话: {args}")
                                    if current_session.id == args:
                                        ss = await session_mgr.list_all()
                                        if ss:
                                            current_session = ss[0]
                                            channel.print_info(f"已切换到 [{current_session.id}] {current_session.title}")
                                            _refresh_banner(service, current_session, config)
                        else:
                            channel.print_error("用法: /delete <对话ID>")
                    elif cmd == "/dream":
                        dream = host.memory_dream
                        if dream and hasattr(dream, 'run'):
                            await _cmd_dream_async(channel, dream)
                        else:
                            channel.print_error("Dream: 记忆系统未初始化")
                    elif cmd == "/cancel":
                        if args:
                            await service.cancel(args, "用户通过 CLI 取消")
                            channel.print_info(f"已提交取消请求: {args}")
                        else:
                            channel.print_error("用法: /cancel <run_id>")
                    elif cmd == "/retry":
                        if args:
                            result: RunResult = await service.retry_interrupted(args, output_port)
                            await _render_result(channel, result)
                        else:
                            channel.print_error("用法: /retry <run_id>")
                    elif cmd == "/abandon":
                        if args:
                            result = await service.abandon_interrupted(args)
                            await _render_result(channel, result)
                        else:
                            channel.print_error("用法: /abandon <run_id>")
                    elif cmd == "/tools":
                        _cmd_tools(channel, host.tool_executor)
                    elif cmd == "/mcp":
                        _cmd_mcp(channel, host.mcp_provider)
                    elif cmd == "/skills":
                        _cmd_skills(channel, host.skill_registry)
                    elif cmd == "/model":
                        identity = service.get_identity(current_session)
                        channel.print_info(f"当前模型: {identity.resolve_model(config.llm.default_model)}")
                    else:
                        channel.print_error(f"未知命令: {cmd}")
                    continue

                # ── 正常对话 ──
                result: RunResult = await service.submit(current_session, user_input, output_port)
                result = await _resolve_pending_approvals(channel, service, result, output_port)
                await _render_result(channel, result)

                sys.stdout.flush()

            except KeyboardInterrupt:
                # 中断信号传播至外层 finally，确保 Host 按依赖逆序关闭后再退出。
                raise
            except Exception as e:
                channel.print_error(f"错误: {e}")
    finally:
        # 阶段 2：Host 作为资源生命周期所有者，退出前释放 MCP Provider 与 Context 缓存。
        await host.shutdown()


def _print_help(channel: CLIChannel) -> None:
    channel.print_info("""
dotClaw 命令:
  /new [标题]      新建对话
  /list            列出所有对话
  /switch <id>     切换到指定对话
  /delete <id>     删除对话
  /tools           列出可用工具
  /mcp             查看 MCP servers 状态
  /skills          列出已加载技能
  /dream           触发记忆蒸馏
  /cancel <run_id>  取消指定运行
  /retry <run_id>   重试中断运行
  /abandon <run_id> 放弃中断运行
  /model           查看当前模型
  /help            显示帮助
  /quit            退出
""")


async def _cmd_list(channel: CLIChannel, mgr: SessionManager, cur: Session) -> None:
    ss: list[Session] = await mgr.list_all()
    channel.print_info("所有对话:")
    for s in ss:
        mark: str = " ← 当前" if s.id == cur.id else ""
        channel.print_info(f"  [{s.id}] {s.title} ({s.updated_at[:10]}){mark}")


def _cmd_tools(channel: CLIChannel, tool_executor: ToolExecutor | None) -> None:
    """展示既有工具注册表，不参与运行控制或审批决策。"""
    if tool_executor is None:
        channel.print_info("(没有注册任何工具)")
        return
    definitions: list[ToolDefinition] = tool_executor.get_definitions()
    if not definitions:
        channel.print_info("(没有注册任何工具)")
        return
    total: int = len(definitions)
    channel.print_info(f"可用工具 ({total} 个):")
    builtin: list[ToolDefinition] = [definition for definition in definitions if definition.source is ToolSource.BUILTIN]
    mcp_tools: list[ToolDefinition] = [definition for definition in definitions if definition.source is ToolSource.MCP]
    if builtin:
        channel.print_info(f"  内置工具 ({len(builtin)} 个):")
        for definition in builtin:
            handler = tool_executor.get_handler(definition.name)
            mark: str = " [需审批]" if handler and handler.definition().needs_approval else ""
            channel.print_info(f"    {definition.name}{mark}: {definition.description}")
    if mcp_tools:
        by_server: dict[str, list[ToolDefinition]] = {}
        for definition in mcp_tools:
            server: str = str(definition.metadata.get("server", "unknown"))
            by_server.setdefault(server, []).append(definition)
        channel.print_info(f"  MCP 工具 ({len(mcp_tools)} 个):")
        for server, tools in by_server.items():
            channel.print_info(f"    [{server}]")
            for definition in tools:
                handler = tool_executor.get_handler(definition.name)
                mark = " [需审批]" if handler and handler.definition().needs_approval else ""
                channel.print_info(f"      {definition.name}{mark}: {definition.description}")


def _cmd_mcp(channel: CLIChannel, mcp_provider: MCPToolProvider | None) -> None:
    """展示 MCP 服务状态，不访问 Runtime 内部状态。"""
    if mcp_provider is None:
        channel.print_info("MCP 未启用")
        return
    from dotclaw.mcp import McpClientState
    states = mcp_provider.get_server_states()
    if not states:
        channel.print_info("(未配置 MCP server)")
        return
    channel.print_info("MCP servers:")
    state_labels: dict[McpClientState, str] = {
        McpClientState.STARTING: "⏳",
        McpClientState.CONNECTED: "✅",
        McpClientState.CRASHED: "💥",
        McpClientState.FAILED: "❌",
        McpClientState.SHUTDOWN: "🛑",
    }
    for name, (st, message) in states.items():
        icon: str = state_labels.get(st, "❓")
        msg: str = f" — {message}" if message else ""
        channel.print_info(f"  {icon} [{name}] {st.value}{msg}")


def _cmd_skills(channel: CLIChannel, skill_registry: SkillRegistry | None) -> None:
    """展示已注册 Skill，不参与运行控制。"""
    if skill_registry is None:
        channel.print_info("Skill 系统未启用")
        return
    metas = skill_registry.list_all()
    if not metas:
        channel.print_info("(没有加载任何 Skill)")
        return
    channel.print_info(f"已加载 Skill ({len(metas)} 个):")
    for meta in sorted(metas, key=lambda m: m.name):
        desc_line: str = meta.truncated_description(max_len=40)
        channel.print_info(f"  {meta.name}: {desc_line}")


async def _cmd_dream_async(channel: CLIChannel, dream: DeepDream) -> None:
    """执行已初始化的记忆蒸馏任务。"""
    try:
        result = await dream.run()
        channel.print_info(f"Dream: {result}")
    except Exception as e:
        channel.print_error(f"Dream 失败: {e}")


async def _resolve_pending_approvals(
    channel: CLIChannel,
    service: SessionInteractionService,
    result: RunResult,
    output_port: LLMOutputPort | None = None,
) -> RunResult:
    """循环处理等待审批的运行：仅向服务提交 approval_id 与决定，返回最终 RunResult。

    透传运行级输出端口；不保存任何 Agent 实例状态。
    """
    while result.status is RunStatus.WAITING_APPROVAL and result.approval_id:
        decision = await channel.ask_user("⚠️ 工具需要审批，确认执行？(y/n): ")
        approved = decision.strip().lower() in ("y", "yes")
        result = await service.resolve_approval(result.approval_id, approved, output_port)
    return result


async def _render_result(channel: CLIChannel, result: RunResult) -> None:
    """将结构化 RunResult 渲染给用户：流式已在运行期间输出则仅补换行，否则打印文本。"""
    if result.has_streamed_response:
        # 文本增量已在运行期间输出，此处仅补齐终端换行，避免重复显示最终回复。
        await channel.stream("\n")
    else:
        text: str = format_run_result(result)
        if text:
            # 最终回复由 CLI 入口负责呈现，Runtime 仅返回执行结果以保持边界解耦。
            await channel.print_markdown(text)


def _refresh_banner(service: SessionInteractionService, current_session: Session, config: Config) -> None:
    """按当前 Session 绑定的 Identity 重建并打印 Banner。

    初次启动、``/new``、``/switch``、``/delete`` 切到其它会话后都应调用，
    确保身份展示始终反映当前会话（fix 文档 §3.3）。
    """
    identity = service.get_identity(current_session)
    from dotclaw.config import _find_project_root
    rich_console.print(build_banner(
        agent_name=identity.agent_name,
        model=identity.resolve_model(config.llm.default_model),
        session_title=current_session.title,
        workspace=str(_find_project_root()),
    ))


def main() -> None:
    try:
        asyncio.run(_run_cli())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
