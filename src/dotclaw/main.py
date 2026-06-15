"""dotClaw 主入口"""

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
from dotclaw.agent import build_agent
from dotclaw.cli.banner import build_banner, console as rich_console

async def _run_cli():
    """运行 CLI 交互"""
    channel = CLIChannel()

    # ── 工厂装配 Agent ──
    channel.print_info("组件初始化中...")
    agent = await build_agent(channel=channel)

    # 根据 config 设置日志级别
    logging.getLogger().setLevel(agent.config.debug.level)

    # ── Banner ──
    from dotclaw.config import _find_project_root
    rich_console.print(build_banner(
        agent_name=agent.agent_name,
        model=agent.model,
        session_title=agent.session.title if agent.session else "无",
        workspace=str(_find_project_root()),
    ))

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
                    await agent.shutdown()
                    channel.print_info("再见！👋")
                    break
                elif cmd == "/help":
                    _print_help(channel)
                elif cmd == "/new":
                    title = args or "新对话"
                    session = await agent.new_session(title)
                    channel.print_info(f"已创建并切换到新对话: [{session.id}] {title}")
                elif cmd == "/list":
                    await _cmd_list(channel, agent)
                elif cmd == "/switch":
                    if args:
                        session = await agent.switch_session(args)
                        if session:
                            channel.print_info(f"已切换到 [{session.id}] {session.title}")
                        else:
                            channel.print_error(f"未找到会话: {args}")
                    else:
                        channel.print_error("用法: /switch <会话ID>")
                elif cmd == "/delete":
                    if args:
                        deleted = await agent._session_mgr.delete(args)
                        if deleted:
                            channel.print_info(f"已删除会话: {args}")
                            if agent.session and agent.session.id == args:
                                sessions = await agent.list_sessions()
                                if sessions:
                                    agent.session = sessions[0]
                                    channel.print_info(f"已切换到 [{agent.session.id}] {agent.session.title}")
                        else:
                            channel.print_error(f"未找到会话: {args}")
                    else:
                        channel.print_error("用法: /delete <会话ID>")
                elif cmd == "/dream":
                    await _cmd_dream_async(channel, agent.memory_dream)
                elif cmd == "/tools":
                    _cmd_tools(channel, agent.tool_executor)
                elif cmd == "/mcp":
                    _cmd_mcp(channel, agent.mcp_provider)
                elif cmd == "/skills":
                    _cmd_skills(channel, agent.skill_registry)
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
            await agent.chat(user_input)
            sys.stdout.flush()

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
  /delete <id>     删除对话
  /tools           列出可用工具
  /mcp             查看 MCP servers 状态
  /skills          列出已加载技能
  /dream           触发记忆蒸馏
  /model <名称>    切换模型
  /help            显示帮助
  /quit            退出
""")


async def _cmd_list(channel, agent):
    sessions = await agent.list_sessions()
    channel.print_info("所有对话:")
    for s in sessions:
        current_id = agent.session.id if agent.session else ""
        mark = " ← 当前" if s.id == current_id else ""
        channel.print_info(f"  [{s.id}] {s.title} ({s.updated_at[:10]}){mark}")


def _cmd_tools(channel, tool_executor):
    """列出所有可用工具（按来源分组显示）"""
    from dotclaw.tools.base import ToolSource

    if not tool_executor:
        channel.print_info("(没有注册任何工具)")
        return

    definitions = tool_executor.get_definitions()
    if not definitions:
        channel.print_info("(没有注册任何工具)")
        return

    total = len(definitions)
    channel.print_info(f"可用工具 ({total} 个):")

    builtin = [d for d in definitions if d.source == ToolSource.BUILTIN]
    mcp_tools = [d for d in definitions if d.source == ToolSource.MCP]
    other = [d for d in definitions if d.source not in (ToolSource.BUILTIN, ToolSource.MCP)]

    if builtin:
        channel.print_info(f"  内置工具 ({len(builtin)} 个):")
        for d in builtin:
            handler = tool_executor.get_handler(d.name)
            mark = " [需审批]" if handler and handler.definition().needs_approval else ""
            channel.print_info(f"    {d.name}{mark}: {d.description}")

    if mcp_tools:
        by_server: dict[str, list] = {}
        for d in mcp_tools:
            server = d.metadata.get("server", "unknown")
            by_server.setdefault(server, []).append(d)

        channel.print_info(f"  MCP 工具 ({len(mcp_tools)} 个):")
        for server, tools in by_server.items():
            channel.print_info(f"    [{server}]")
            for d in tools:
                handler = tool_executor.get_handler(d.name)
                mark = " [需审批]" if handler and handler.definition().needs_approval else ""
                channel.print_info(f"      {d.name}{mark}: {d.description}")

    if other:
        channel.print_info(f"  其他工具 ({len(other)} 个):")
        for d in other:
            channel.print_info(f"    {d.name}: {d.description}")


def _cmd_mcp(channel, mcp_provider):
    """查看 MCP servers 状态"""
    if not mcp_provider:
        channel.print_info("MCP 未启用")
        return

    from dotclaw.mcp import McpClientState

    states = mcp_provider.get_server_states()
    if not states:
        channel.print_info("(未配置 MCP server)")
        return

    channel.print_info("MCP servers:")
    state_labels = {
        McpClientState.STARTING: "⏳",
        McpClientState.CONNECTED: "✅",
        McpClientState.CRASHED: "💥",
        McpClientState.FAILED: "❌",
        McpClientState.SHUTDOWN: "🛑",
    }
    for name, (state, message) in states.items():
        icon = state_labels.get(state, "❓")
        msg = f" — {message}" if message else ""
        channel.print_info(f"  {icon} [{name}] {state.value}{msg}")


def _cmd_skills(channel, skill_registry):
    """列出所有已加载的 Skill"""
    if not skill_registry:
        channel.print_info("Skill 系统未启用")
        return

    metas = skill_registry.list_all()
    if not metas:
        channel.print_info("(没有加载任何 Skill)")
        return

    channel.print_info(f"已加载 Skill ({len(metas)} 个):")
    for meta in sorted(metas, key=lambda m: m.name):
        desc_line = meta.truncated_description(max_len=40)
        channel.print_info(f"  {meta.name}: {desc_line}")


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


def main():
    try:
        asyncio.run(_run_cli())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
