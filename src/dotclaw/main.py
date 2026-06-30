"""dotClaw 主入口（v2）"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# 确保 src 在路径中
sys.path.insert(0, str(Path(__file__).parent.parent))

# Rich / 终端 UTF-8 支持（Block 字符需要）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 配置 logging 基础设施（模块级默认 WARNING，具体级别等 config 加载后调整）
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("./data/dotclaw.log", encoding="utf-8"),
    ],
)
# 屏蔽第三方库的 INFO 日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from dotclaw.channel.cli import CLIChannel
from dotclaw.agent import Agent, build_agent
from dotclaw.storage.conversation import ConversationManager, Conversation
from dotclaw.session.session import Session
from dotclaw.cli.banner import build_banner, console as rich_console


async def _run_cli() -> None:
    """运行 CLI 交互"""
    channel: CLIChannel = CLIChannel()

    # ── 工厂装配 Agent ──
    channel.print_info("组件初始化中...")
    agent: Agent
    conv_mgr: ConversationManager
    agent, conv_mgr = await build_agent(channel=channel)

    # 根据 config 设置日志级别
    if agent.config is not None:
        logging.getLogger().setLevel(agent.config.debug.level)

    # ── 恢复上次对话 ──
    conversations: list[Conversation] = await conv_mgr.list_all()
    if conversations:
        current_conv: Conversation = conversations[0]
    else:
        current_conv = await conv_mgr.create("主对话")

    # ── Banner ──
    from dotclaw.config import _find_project_root
    rich_console.print(build_banner(
        agent_name=agent.agent_name,
        model=agent._resolve_model(),
        session_title=current_conv.title,
        workspace=str(_find_project_root()),
    ))

    while True:
        try:
            user_input: str = await channel.receive()

            if not user_input.strip():
                continue

            # 处理命令
            if user_input.startswith("/"):
                cmd: str = user_input.split()[0].lower()
                args: str = user_input[len(cmd):].strip()

                if cmd == "/quit":
                    await agent.shutdown()
                    channel.print_info("再见！👋")
                    break
                elif cmd == "/help":
                    _print_help(channel)
                elif cmd == "/new":
                    title: str = args or "新对话"
                    current_conv = await conv_mgr.create(title)
                    channel.print_info(f"已创建并切换到新对话: [{current_conv.id}] {title}")
                elif cmd == "/list":
                    await _cmd_list(channel, conv_mgr, current_conv)
                elif cmd == "/switch":
                    if args:
                        conv: Conversation | None = await conv_mgr.load(args)
                        if conv:
                            current_conv = conv
                            channel.print_info(f"已切换到 [{conv.id}] {conv.title}")
                        else:
                            channel.print_error(f"未找到对话: {args}")
                    else:
                        channel.print_error("用法: /switch <对话ID>")
                elif cmd == "/delete":
                    if args:
                        deleted: bool = await conv_mgr.delete(args)
                        if deleted:
                            channel.print_info(f"已删除对话: {args}")
                            if current_conv.id == args:
                                convs = await conv_mgr.list_all()
                                if convs:
                                    current_conv = convs[0]
                                    channel.print_info(f"已切换到 [{current_conv.id}] {current_conv.title}")
                        else:
                            channel.print_error(f"未找到对话: {args}")
                    else:
                        channel.print_error("用法: /delete <对话ID>")
                elif cmd == "/dream":
                    dream = agent.memory_dream
                    if dream and hasattr(dream, 'run'):
                        await _cmd_dream_async(channel, dream)
                    else:
                        channel.print_error("Dream: 记忆系统未初始化")
                elif cmd == "/tools":
                    _cmd_tools(channel, agent.runtime.tool_executor)
                elif cmd == "/mcp":
                    _cmd_mcp(channel, agent.runtime.mcp_provider)
                elif cmd == "/skills":
                    _cmd_skills(channel, agent.runtime.skill_registry)
                elif cmd == "/model":
                    channel.print_info(f"当前模型: {agent._resolve_model()}")
                else:
                    channel.print_error(f"未知命令: {cmd}，输入 /help 查看可用命令")
                continue

            # 正常对话：创建 Session，调用 agent.run()
            session: Session = Session(conversation=current_conv)
            agent_run = await agent.run(session, user_input)

            if agent_run.error:
                channel.print_error(agent_run.error)
            else:
                await channel.send(agent_run.final_text)

            sys.stdout.flush()

        except KeyboardInterrupt:
            channel.print_info("\n(输入 /quit 退出)")
        except Exception as e:
            channel.print_error(f"错误: {e}")


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
  /model           查看当前模型
  /help            显示帮助
  /quit            退出
""")


async def _cmd_list(channel: CLIChannel, conv_mgr: ConversationManager, current: Conversation) -> None:
    convs: list[Conversation] = await conv_mgr.list_all()
    channel.print_info("所有对话:")
    for c in convs:
        mark: str = " ← 当前" if c.id == current.id else ""
        channel.print_info(f"  [{c.id}] {c.title} ({c.updated_at[:10]}){mark}")


def _cmd_tools(channel: CLIChannel, tool_executor: object) -> None:
    """列出所有可用工具（按来源分组显示）"""
    from dotclaw.tools.base import ToolSource

    if tool_executor is None:
        channel.print_info("(没有注册任何工具)")
        return

    definitions = tool_executor.get_definitions()  # type: ignore[union-attr]
    if not definitions:
        channel.print_info("(没有注册任何工具)")
        return

    total: int = len(definitions)
    channel.print_info(f"可用工具 ({total} 个):")

    builtin = [d for d in definitions if d.source == ToolSource.BUILTIN]
    mcp_tools = [d for d in definitions if d.source == ToolSource.MCP]
    other = [d for d in definitions if d.source not in (ToolSource.BUILTIN, ToolSource.MCP)]

    if builtin:
        channel.print_info(f"  内置工具 ({len(builtin)} 个):")
        for d in builtin:
            handler = tool_executor.get_handler(d.name)  # type: ignore[union-attr]
            mark: str = " [需审批]" if handler and handler.definition().needs_approval else ""
            channel.print_info(f"    {d.name}{mark}: {d.description}")

    if mcp_tools:
        by_server: dict[str, list] = {}
        for d in mcp_tools:
            server: str = d.metadata.get("server", "unknown")
            by_server.setdefault(server, []).append(d)

        channel.print_info(f"  MCP 工具 ({len(mcp_tools)} 个):")
        for server, tools in by_server.items():
            channel.print_info(f"    [{server}]")
            for d in tools:
                handler = tool_executor.get_handler(d.name)  # type: ignore[union-attr]
                mark = " [需审批]" if handler and handler.definition().needs_approval else ""
                channel.print_info(f"      {d.name}{mark}: {d.description}")

    if other:
        channel.print_info(f"  其他工具 ({len(other)} 个):")
        for d in other:
            channel.print_info(f"    {d.name}: {d.description}")


def _cmd_mcp(channel: CLIChannel, mcp_provider: object) -> None:
    """查看 MCP servers 状态"""
    if mcp_provider is None:
        channel.print_info("MCP 未启用")
        return

    from dotclaw.mcp import McpClientState

    states = mcp_provider.get_server_states()  # type: ignore[union-attr]
    if not states:
        channel.print_info("(未配置 MCP server)")
        return

    channel.print_info("MCP servers:")
    state_labels: dict = {
        McpClientState.STARTING: "⏳",
        McpClientState.CONNECTED: "✅",
        McpClientState.CRASHED: "💥",
        McpClientState.FAILED: "❌",
        McpClientState.SHUTDOWN: "🛑",
    }
    for name, (state, message) in states.items():
        icon: str = state_labels.get(state, "❓")
        msg: str = f" — {message}" if message else ""
        channel.print_info(f"  {icon} [{name}] {state.value}{msg}")


def _cmd_skills(channel: CLIChannel, skill_registry: object) -> None:
    """列出所有已加载的 Skill"""
    if skill_registry is None:
        channel.print_info("Skill 系统未启用")
        return

    metas = skill_registry.list_all()  # type: ignore[union-attr]
    if not metas:
        channel.print_info("(没有加载任何 Skill)")
        return

    channel.print_info(f"已加载 Skill ({len(metas)} 个):")
    for meta in sorted(metas, key=lambda m: m.name):
        desc_line: str = meta.truncated_description(max_len=40)
        channel.print_info(f"  {meta.name}: {desc_line}")


async def _cmd_dream_async(channel: CLIChannel, dream: object) -> None:
    """手动触发 Deep Dream 蒸馏"""
    try:
        result = await dream.run()  # type: ignore[union-attr]
        channel.print_info(f"Dream: {result}")
    except Exception as e:
        channel.print_error(f"Dream 失败: {e}")


def main() -> None:
    try:
        asyncio.run(_run_cli())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
