"""dotClaw 主入口"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

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
from dotclaw.agent import Agent, build_agent
from dotclaw.session import Session, SessionManager
from dotclaw.bootstrap import RuntimeServices
from dotclaw.cli.banner import build_banner, console as rich_console
from dotclaw.mcp.provider import MCPToolProvider
from dotclaw.memory.dream import DeepDream
from dotclaw.skills.registry import SkillRegistry
from dotclaw.tools.base import ToolDefinition, ToolSource
from dotclaw.tools.executor import ToolExecutor


async def _run_cli() -> None:
    channel: CLIChannel = CLIChannel()

    channel.print_info("组件初始化中...")
    agent: Agent
    runtime_services: RuntimeServices
    session_mgr: SessionManager
    agent, runtime_services, session_mgr = await build_agent(channel=channel)

    if agent.config is not None:
        logging.getLogger().setLevel(agent.config.debug.level)

    sessions: list[Session] = await session_mgr.list_all()
    if sessions:
        current_session: Session = sessions[0]
    else:
        current_session = await session_mgr.create("主对话")

    from dotclaw.config import _find_project_root
    rich_console.print(build_banner(
        agent_name=agent.agent_name,
        model=agent._resolve_model(),
        session_title=current_session.title,
        workspace=str(_find_project_root()),
    ))

    while True:
        try:
            user_input: str = await channel.receive()
            if not user_input.strip():
                continue

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
                    current_session = await session_mgr.create(title)
                    channel.print_info(f"已创建并切换到新对话: [{current_session.id}] {title}")
                elif cmd == "/list":
                    await _cmd_list(channel, session_mgr, current_session)
                elif cmd == "/switch":
                    if args:
                        s: Session | None = await session_mgr.load(args)
                        if s:
                            current_session = s
                            channel.print_info(f"已切换到 [{s.id}] {s.title}")
                        else:
                            channel.print_error(f"未找到对话: {args}")
                    else:
                        channel.print_error("用法: /switch <对话ID>")
                elif cmd == "/delete":
                    if args:
                        deleted: bool = await session_mgr.delete(args)
                        if deleted:
                            channel.print_info(f"已删除对话: {args}")
                            if current_session.id == args:
                                ss = await session_mgr.list_all()
                                if ss:
                                    current_session = ss[0]
                                    channel.print_info(f"已切换到 [{current_session.id}] {current_session.title}")
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
                elif cmd == "/cancel":
                    if args:
                        await agent.cancel_run(args, "用户通过 CLI 取消")
                        channel.print_info(f"已提交取消请求: {args}")
                    else:
                        channel.print_error("用法: /cancel <run_id>")
                elif cmd == "/tools":
                    _cmd_tools(channel, agent.tool_executor)
                elif cmd == "/mcp":
                    _cmd_mcp(channel, agent.mcp_provider)
                elif cmd == "/skills":
                    _cmd_skills(channel, agent.skill_registry)
                elif cmd == "/model":
                    channel.print_info(f"当前模型: {agent._resolve_model()}")
                else:
                    channel.print_error(f"未知命令: {cmd}")
                continue

            # ── 正常对话 ──
            final_answer: str = await agent.process(current_session, user_input)
            final_answer = await _resolve_pending_approvals(channel, agent, final_answer)

            if not final_answer:
                channel.print_error("执行异常：未返回有效回复")

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
  /cancel <run_id>  取消指定运行
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


async def _resolve_pending_approvals(channel: CLIChannel, agent: Agent, current_text: str) -> str:
    """展示有限审批选项，并只向 Engine 提交 approval_id 与决定。"""
    answer: str = current_text
    while agent.last_run_result is not None and agent.last_run_result.status.value == "waiting_approval":
        approval_id = agent.last_run_result.approval_id
        if not approval_id:
            return "执行失败：等待审批运行缺少 approval_id"
        decision = await channel.ask_user("⚠️ 工具需要审批，确认执行？(y/n): ")
        approved = decision.strip().lower() in ("y", "yes")
        answer = await agent.resolve_approval(approval_id, approved)
    return answer


def main() -> None:
    try:
        asyncio.run(_run_cli())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
