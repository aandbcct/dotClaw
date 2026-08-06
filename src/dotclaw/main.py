"""dotClaw 主入口"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
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
from dotclaw.channel.runtime_llm_output import ChannelLLMOutputAdapter
from dotclaw.eval.draft_service import EvalCaseDraftService
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
from dotclaw.runtime.domain.facts import RunErrorCode
from dotclaw.trace.service import TraceService
from dotclaw.tools.base import ToolDefinition, ToolSource
from dotclaw.tools.executor import ToolExecutor


async def _run_cli(show_reasoning: bool = True) -> None:
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

                # 本次消息的运行级输出端口：CLI 每次消息构造，只服务本 Run；
                # 适配器按语义分区展示思考/回答，模型文本走纯文本路径。
                output_port: LLMOutputPort = ChannelLLMOutputAdapter(
                    channel,
                    show_reasoning=show_reasoning,
                )

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
                            result: RunResult = await service.resume_run(args, output_port)
                            await _render_result(channel, result)
                        else:
                            channel.print_error("用法: /retry <run_id>")
                    elif cmd == "/abandon":
                        if args:
                            result = await service.abandon_run(args)
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
                    elif cmd == "/trace":
                        await _cmd_trace(channel, host.trace_service, args)
                    elif cmd == "/eval":
                        await _cmd_eval(channel, host.eval_draft_service, host.trace_service, args)
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
  /trace <run_id>  查看指定运行的追踪摘要
  /eval            评测草案：create/list/show/review/confirm/run <dataset> ...
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
    while result.state.is_waiting_approval() and result.approval_id:
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


async def _eval_run(
    channel: CLIChannel,
    datasets_root: Path,
    parts: list[str],
) -> None:
    """运行 Dataset 的全部 Case 并产出 Gate 报告。

    用法: /eval run <dataset> [--mode playback|reexecution]
    默认 playback；reexecution 仅供观察不进 Gate。
    """
    from dotclaw.eval.playback import PlaybackRunner
    from dotclaw.eval.reexecution import ReexecutionRunner

    if len(parts) < 2:
        channel.print_error("用法: /eval run <dataset> [--mode playback|reexecution]")
        return
    dataset_name = parts[1]
    mode = "playback"
    if len(parts) >= 4 and parts[2] == "--mode":
        mode = parts[3].lower()
        if mode not in ("playback", "reexecution"):
            channel.print_error(f"不支持的模式: {mode}，可选 playback / reexecution")
            return

    if mode == "reexecution":
        runner = ReexecutionRunner()
        results = await runner.run_dataset(datasets_root, dataset_name)
        passed_count = sum(1 for r in results if r.passed)
        channel.print_info(
            f"Re-execution 完成：{passed_count}/{len(results)} Case 通过"
        )
        for result in results:
            status = "✓" if result.passed else "✗"
            channel.print_info(
                f"  {status} {result.case_id}"
                + (f" — {result.failure_kind.value}" if result.failure_kind else "")
            )
    else:
        runner = PlaybackRunner()
        report = await runner.run_and_gate(datasets_root, dataset_name)
        passed_count = sum(1 for c in report.case_results if c.passed)
        channel.print_info(
            f"Gate 判定: {report.overall_status}  ({passed_count}/{len(report.case_results)} Case 通过)"
        )
        for case_result in report.case_results:
            status = "✓" if case_result.passed else "✗"
            channel.print_info(
                f"  {status} {case_result.case_id}"
                + (f" — {case_result.failure_kind}" if case_result.failure_kind else "")
            )
        if report.error_detail:
            channel.print_error(f"ERROR 详情: {report.error_detail}")


async def _cmd_trace(
    channel: CLIChannel,
    trace_service: TraceService,
    run_id: str,
) -> None:
    """读取指定 Run 的 Trace 并输出不含正文的摘要。"""
    if not run_id:
        channel.print_error("用法: /trace <run_id>")
        return
    try:
        trace = await trace_service.get_trace(run_id)
    except LookupError as error:
        channel.print_error(f"trace 错误: {error}")
        return

    metrics = trace.metrics
    channel.print_info(
        f"Trace {trace.source.run_id}: partial={trace.is_partial}, "
        f"spans={len(trace.spans)}, issues={len(trace.issues)}"
    )
    channel.print_info(
        f"  critical_path={metrics.critical_path_ms}ms, "
        f"llm={metrics.llm_duration_ms}ms, tool={metrics.tool_duration_ms}ms, "
        f"failed_tools={metrics.failed_tool_count}, incomplete_spans={metrics.incomplete_span_count}"
    )


async def _cmd_eval(
    channel: CLIChannel,
    service: EvalCaseDraftService,  # 由 ApplicationHost 注入
    trace_service: TraceService,
    arg_str: str,
) -> None:
    """评测草案的 Channel 命令；仅经服务读写，不直接访问 Dataset 文件。"""
    parts = arg_str.split()
    if not parts:
        channel.print_info("用法: /eval <create|list|show|review|confirm|run> <dataset> ...")
        return
    sub = parts[0]
    try:
        if sub == "create":
            if len(parts) < 3 or len(parts) > 4:
                channel.print_error("用法: /eval create <dataset> <run_id> [case_id]（先读取 Trace）")
                return
            # run_id 仅用于定位权威记录；Draft 的直接来源始终是重建后的 Trace。
            trace = await trace_service.get_trace(parts[2])
            case_id = parts[3] if len(parts) == 4 else None
            draft = await service.create_draft_from_trace(parts[1], trace, case_id=case_id)
            channel.print_info(
                f"已创建 Draft: {draft.draft_id} "
                f"(requires_review={draft.requires_review})"
            )
        elif sub == "list":
            if len(parts) < 2:
                channel.print_error("用法: /eval list <dataset>")
                return
            dataset_name = parts[1]
            drafts = await service.list_drafts(dataset_name)
            cases = await service.list_cases(dataset_name)
            channel.print_info(f"数据集 {dataset_name}: {len(drafts)} 个草案, {len(cases)} 个 Case")
            for draft_id in drafts:
                channel.print_info(f"  [draft] {draft_id}")
            for case in cases:
                channel.print_info(f"  [case]  {case.case_id} ({case.agent_id})")
        elif sub == "show":
            if len(parts) < 3:
                channel.print_error("用法: /eval show <dataset> <draft_id>")
                return
            draft = await service.load_draft(parts[1], parts[2])
            channel.print_info(
                f"草案 {draft.draft_id}（来源 run={draft.source_run_id}, "
                f"hash={draft.source_record_hash[:8]}…）"
            )
            channel.print_info(
                f"  requires_review={draft.requires_review}, "
                f"confirmed_case_id={draft.confirmed_case_id}"
            )
            channel.print_info(
                f"  case_id={draft.case.case_id}, agent_id={draft.case.agent_id}, "
                f"fixtures: llm={len(draft.case.llm_fixture.responses)}, "
                f"tools={len(draft.case.tool_fixtures)}, "
                f"approvals={len(draft.case.approval_fixtures)}, "
                f"delegations={len(draft.case.delegation_fixtures)}, "
                f"contexts={len(draft.case.context_fixtures)}, "
                f"expectations={len(draft.case.expectations)}"
            )
        elif sub == "review":
            if len(parts) < 3:
                channel.print_error("用法: /eval review <dataset> <draft_id>")
                return
            draft = await service.load_draft(parts[1], parts[2])
            reviewed = await service.save_reviewed_draft(parts[1], parts[2], draft)
            channel.print_info(f"已保存审阅（requires_review={reviewed.requires_review}）: {reviewed.draft_id}")
        elif sub == "confirm":
            if len(parts) < 4:
                channel.print_error("用法: /eval confirm <dataset> <draft_id> <case_id>")
                return
            case = await service.confirm_draft(parts[1], parts[2], parts[3])
            channel.print_info(f"已确认 Case: {case.case_id}")
        elif sub == "run":
            await _eval_run(channel, service.datasets_root, parts)
        else:
            channel.print_error(f"未知 /eval 子命令: {sub}")
    except (FileNotFoundError, FileExistsError, LookupError, ValueError) as error:
        channel.print_error(f"eval 错误: {error}")


def _parse_show_reasoning(args: Sequence[str] | None = None) -> bool:
    """解析 CLI 的思考展示开关，默认展示 reasoning 增量。"""
    parser = argparse.ArgumentParser(description="dotClaw 命令行客户端")
    parser.add_argument(
        "--hide-thinking",
        action="store_false",
        dest="show_reasoning",
        help="隐藏模型的思考过程，仅展示最终回答",
    )
    parser.add_argument(
        "--eval-ci",
        metavar="DATASET",
        dest="eval_ci_dataset",
        help="CI 模式：对指定 Dataset 运行 Playback 闸门并退出（PASS→0, REGRESSION/ERROR→1）",
    )
    parsed = parser.parse_args(args)
    return bool(parsed.show_reasoning), parsed.eval_ci_dataset


async def _eval_ci(dataset_name: str) -> int:
    """CI 模式：创建最小 Host，运行 Playback Gate，退出码 0=PASS / 1=非 PASS。"""
    from dotclaw.config import _find_project_root, get_config
    from dotclaw.bootstrap import ApplicationHost

    config = get_config()
    project_root = _find_project_root()
    host = ApplicationHost(config, project_root)
    await host.initialize()

    try:
        root = host.eval_draft_service.datasets_root
        report = await host.playback_runner.run_and_gate(root, dataset_name)
        print(f"Gate: {report.overall_status}  ({sum(1 for c in report.case_results if c.passed)}/{len(report.case_results)} Case 通过)")
        for c in report.case_results:
            mark = "✓" if c.passed else "✗"
            extra = f" — {c.failure_kind}" if c.failure_kind else ""
            print(f"  {mark} {c.case_id}{extra}")
        if report.error_detail:
            print(f"ERROR: {report.error_detail}")
        return 0 if report.overall_status == "PASS" else 1
    finally:
        await host.shutdown()


def main() -> None:
    try:
        show_reasoning, ci_dataset = _parse_show_reasoning()
        if ci_dataset:
            sys.exit(asyncio.run(_eval_ci(ci_dataset)))
        asyncio.run(_run_cli(show_reasoning=show_reasoning))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
