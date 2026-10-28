"""dotClaw 本地 GUI 的 FastAPI 应用与 HTTP/SSE 适配。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ...bootstrap.application_host import ApplicationHost
from ...bootstrap.session_interaction import SessionDeletionRejected, UnknownIdentityError
from ...common.logging_setup import exception_diagnostic
from ...runtime.application.session_run_coordinator import SessionBusyError
from ...runtime.application.dto import RunResult
from ...runtime.domain.events import RunEvent, RunEventType
from ...runtime.domain.facts import AgentRun, JSONMap, MessageRole, RunError, RunErrorCode, RunMessage, RunMessageKind
from ...runtime.domain.state import RunOutcome, Suspended
from ...session.session import Conversation, Session
from .schemas import (
    CancelSessionRequest,
    CreateSessionRequest,
    AgentOptionResponse,
    ResolveApprovalRequest,
    RunActivityResponse,
    RuntimeSettingsResponse,
    GeneralSettingResponse,
    McpServerSettingResponse,
    ModelSettingResponse,
    SkillSettingResponse,
    SSEEvent,
    SSEEventType,
    SessionDetailResponse,
    SessionMessageResponse,
    SessionSummaryResponse,
    SubmitMessageRequest,
    ToolExecutionResponse,
    ToolSettingResponse,
    ToggleSettingRequest,
    UpdateSessionRequest,
    WebErrorCode,
    WebRunStatus,
    WorkspaceOptionsResponse,
    SwitchModelRequest,
    SearchHitResponse,
    SearchSessionsResponse,
)
from .sse import SSEOutputAdapter

logger = logging.getLogger("dotclaw.channel.web")

HostFactory = Callable[[], Awaitable[ApplicationHost]]
_DEFAULT_CANCEL_REASON = "用户从 GUI 停止运行"
_LOCAL_WEB_HOST = "127.0.0.1"
_DEFAULT_WEB_PORT = 8765


def create_app(
    *,
    host_factory: HostFactory | None = None,
    frontend_dir: Path | None = None,
) -> FastAPI:
    """创建可注入 Host 工厂的本地 Web 应用。"""
    factory: HostFactory = host_factory or ApplicationHost.build

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """持有唯一 ApplicationHost，并在退出时等待提交后关闭资源。"""
        host = await factory()
        _load_runtime_preferences(host)
        app.state.host = host
        app.state.submission_tasks = set()
        try:
            yield
        finally:
            tasks = _submission_tasks(app)
            if tasks:
                await asyncio.gather(*tuple(tasks), return_exceptions=True)
            await host.shutdown()

    app = FastAPI(title="dotClaw GUI", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        """把框架校验详情收敛为不泄露内部结构的稳定错误。"""
        del request, error
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            WebErrorCode.INVALID_REQUEST,
            "请求参数无效",
        )

    @app.get("/api/v1/sessions", response_model=list[SessionSummaryResponse])
    async def list_sessions(request: Request) -> list[SessionSummaryResponse]:
        """按更新时间倒序返回会话摘要。"""
        sessions = await _host(request).session_manager.list_all()
        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return [_session_summary(item) for item in sessions]

    @app.get("/api/v1/sessions/search", response_model=SearchSessionsResponse)
    async def search_sessions(
        request: Request,
        q: str = Query(min_length=1, max_length=100),
        archived: Literal["all", "true", "false"] = "all",
        limit: int = Query(default=30, ge=1, le=50),
    ) -> SearchSessionsResponse | JSONResponse:
        """按更新时间顺序扫描标题和完整问答，不创建额外检索索引。"""
        query = q.strip().casefold()
        if not query:
            return _error_response(422, WebErrorCode.INVALID_REQUEST, "搜索词不能为空")
        sessions = await _host(request).session_manager.list_all()
        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        hits: list[SearchHitResponse] = []
        for session in sessions:
            if archived != "all" and session.archived != (archived == "true"):
                continue
            matched_message = False
            for message in _session_detail(session).messages:
                position = message.content.casefold().find(query)
                if position < 0:
                    continue
                matched_message = True
                hits.append(SearchHitResponse(
                    session_id=session.id, title=session.title,
                    updated_at=session.updated_at, archived=session.archived,
                    message_id=message.id, role=message.role,
                    snippet=message.content[max(0, position - 80):position + len(query) + 80],
                ))
            if not matched_message and query in session.title.casefold():
                hits.append(SearchHitResponse(
                    session_id=session.id, title=session.title,
                    updated_at=session.updated_at, archived=session.archived,
                    snippet=session.title,
                ))
            if len(hits) > limit:
                break
        return SearchSessionsResponse(items=hits[:limit], has_more=len(hits) > limit)

    @app.patch("/api/v1/sessions/{session_id}/model", response_model=SessionSummaryResponse)
    async def switch_session_model(
        session_id: str, payload: SwitchModelRequest, request: Request,
    ) -> SessionSummaryResponse | JSONResponse:
        """通过应用服务在会话租约内重读并切换模型。"""
        try:
            session = await _host(request).session_interaction.switch_model(session_id, payload.model)
        except SessionBusyError:
            return _error_response(409, WebErrorCode.SESSION_BUSY, "会话正在运行，请结束后再切换")
        except LookupError:
            return _session_not_found()
        except ValueError as error:
            return _error_response(422, WebErrorCode.INVALID_REQUEST, str(error))
        return _session_summary(session)

    @app.delete("/api/v1/sessions/{session_id}", status_code=204, response_model=None)
    async def delete_session(session_id: str, request: Request) -> Response:
        """删除完整会话，幂等且保留应用层清理与运行保护。"""
        try:
            await _host(request).session_interaction.delete_session(session_id)
        except (SessionBusyError, SessionDeletionRejected):
            return _error_response(409, WebErrorCode.SESSION_BUSY, "会话仍有未结束运行，请打开会话处理后再删除")
        return Response(status_code=204)

    @app.get("/api/v1/options", response_model=WorkspaceOptionsResponse)
    async def workspace_options(request: Request) -> WorkspaceOptionsResponse:
        """返回新建会话可用的 Agent 与 chat 模型。"""
        host = _host(request)
        identities = host.agent_registry.list_all()
        return WorkspaceOptionsResponse(
            agents=[
                AgentOptionResponse(
                    id=item.agent_id,
                    name=item.agent_name or item.agent_id,
                    description=item.description,
                )
                for item in identities
            ],
            models=list(host.chat_models),
            default_agent_id=host.session_interaction.default_agent_id,
            default_model=host.preferred_chat_model,
        )

    @app.get("/api/v1/settings", response_model=RuntimeSettingsResponse)
    async def runtime_settings(request: Request) -> RuntimeSettingsResponse:
        """返回模型、工具、Skill 与 MCP 的脱敏运行时快照。"""
        return _runtime_settings(_host(request))

    @app.patch("/api/v1/settings/tools/{tool_name}", response_model=RuntimeSettingsResponse)
    async def toggle_tool(
        tool_name: str, payload: ToggleSettingRequest, request: Request,
    ) -> RuntimeSettingsResponse | JSONResponse:
        """让工具开关立即作用于后续 Run 的工具快照。"""
        host = _host(request)
        executor = host.tool_executor
        if executor is None or not executor.registry.set_enabled(tool_name, payload.enabled):
            return _error_response(status.HTTP_404_NOT_FOUND, WebErrorCode.INVALID_REQUEST, "工具不存在")
        _save_runtime_preferences(host)
        return _runtime_settings(host)

    @app.patch("/api/v1/settings/skills/{skill_name}", response_model=RuntimeSettingsResponse)
    async def toggle_skill(
        skill_name: str, payload: ToggleSettingRequest, request: Request,
    ) -> RuntimeSettingsResponse | JSONResponse:
        """让 Skill 开关立即作用于后续匹配与提示词注入。"""
        host = _host(request)
        registry = host.skill_registry
        if registry is None or not registry.set_enabled(skill_name, payload.enabled):
            return _error_response(status.HTTP_404_NOT_FOUND, WebErrorCode.INVALID_REQUEST, "Skill 不存在")
        _save_runtime_preferences(host)
        return _runtime_settings(host)

    @app.post("/api/v1/settings/mcp/reconnect", response_model=RuntimeSettingsResponse)
    async def reconnect_mcp(request: Request) -> RuntimeSettingsResponse | JSONResponse:
        """重新连接当前配置中的全部 MCP server。"""
        host = _host(request)
        provider = host.mcp_provider
        if provider is None:
            return _error_response(status.HTTP_409_CONFLICT, WebErrorCode.INVALID_REQUEST, "MCP 未启用或未配置 server")
        await provider.reconnect()
        return _runtime_settings(host)

    @app.patch("/api/v1/sessions/{session_id}", response_model=SessionSummaryResponse)
    async def update_session(
        session_id: str, payload: UpdateSessionRequest, request: Request,
    ) -> SessionSummaryResponse | JSONResponse:
        """通过会话交互入口持久化重命名或归档，不改变历史内容。"""
        try:
            session = await _host(request).session_interaction.update_session(
                session_id, title=payload.title, archived=payload.archived,
            )
        except LookupError:
            return _session_not_found()
        except ValueError:
            return _error_response(
                status.HTTP_409_CONFLICT, WebErrorCode.SESSION_BUSY,
                "会话正在运行，请结束后再修改",
            )
        return _session_summary(session)

    @app.post(
        "/api/v1/sessions",
        response_model=SessionSummaryResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_session(
        payload: CreateSessionRequest,
        request: Request,
    ) -> SessionSummaryResponse | JSONResponse:
        """通过现有会话交互入口创建并持久化 Session。"""
        try:
            session = await _host(request).session_interaction.create_session(
                agent_id=payload.agent_id,
                title=payload.title,
                model=payload.model,
            )
        except UnknownIdentityError as error:
            return _error_response(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                WebErrorCode.UNKNOWN_IDENTITY,
                str(error),
            )
        except ValueError:
            return _error_response(
                status.HTTP_409_CONFLICT,
                WebErrorCode.SESSION_CREATION_FAILED,
                "无法创建 Session",
            )
        return _session_summary(session)

    @app.get(
        "/api/v1/sessions/{session_id}",
        response_model=SessionDetailResponse,
    )
    async def get_session(
        session_id: str,
        request: Request,
    ) -> SessionDetailResponse | JSONResponse:
        """返回会话摘要和已成功投影的完整问答历史。"""
        session = await _host(request).session_manager.load(session_id)
        if session is None:
            return _session_not_found()
        return _session_detail(session)

    @app.post(
        "/api/v1/sessions/{session_id}/messages",
        response_model=None,
    )
    async def submit_message(
        session_id: str,
        payload: SubmitMessageRequest,
        request: Request,
    ) -> StreamingResponse | JSONResponse:
        """提交一条消息，并以 SSE 返回模型增量与唯一终态。"""
        host = _host(request)
        logger.info("web.message.received content_length=%d", len(payload.content), extra={"session_id": session_id})
        if not host.session_manager.exists(session_id):
            return _session_not_found()

        return _run_stream(
            request,
            session_id,
            lambda adapter: host.session_interaction.submit(
                session_id, payload.content, adapter,
            ),
        )

    @app.get(
        "/api/v1/sessions/{session_id}/activity",
        response_model=RunActivityResponse | None,
    )
    async def get_run_activity(
        session_id: str,
        request: Request,
        run_id: str | None = None,
    ) -> RunActivityResponse | JSONResponse | None:
        """查询当前或指定运行的真实状态和工具执行证据。"""
        host = _host(request)
        if await host.session_manager.load(session_id) is None:
            return _session_not_found()
        run = await host.session_interaction.get_run(session_id, run_id)
        if run is None:
            return None
        events, messages = await host.session_interaction.get_run_evidence(
            session_id, run.run_id,
        )
        executing = await host.session_interaction.is_session_executing(session_id)
        return _run_activity(run, events, messages, executing)

    @app.post("/api/v1/approvals/{approval_id}", response_model=None)
    async def resolve_approval(
        approval_id: str,
        payload: ResolveApprovalRequest,
        request: Request,
    ) -> StreamingResponse:
        """提交网页审批决定，并继续以 SSE 返回恢复后的运行。"""
        host = _host(request)
        return _run_stream(
            request,
            "",
            lambda adapter: host.session_interaction.resolve_approval(
                approval_id, payload.approved, adapter,
            ),
        )

    @app.post("/api/v1/delegations/{child_run_id}/resume", response_model=None)
    async def resume_delegation(
        child_run_id: str,
        request: Request,
    ) -> StreamingResponse:
        """将已完成的子运行结果回灌并恢复父运行。"""
        host = _host(request)
        return _run_stream(
            request,
            "",
            lambda adapter: host.session_interaction.resume_delegation(
                child_run_id, adapter,
            ),
        )

    @app.post("/api/v1/runs/{run_id}/resume", response_model=None)
    async def resume_run(run_id: str, request: Request) -> StreamingResponse:
        """恢复进程重启后留下的未结束运行。"""
        host = _host(request)
        return _run_stream(
            request,
            "",
            lambda adapter: host.session_interaction.resume_run(run_id, adapter),
        )

    @app.post(
        "/api/v1/sessions/{session_id}/cancel",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=None,
    )
    async def cancel_session(
        session_id: str,
        request: Request,
        payload: CancelSessionRequest | None = None,
    ) -> JSONMap | JSONResponse:
        """按 Session 定位并取消唯一活动 Run。"""
        host = _host(request)
        if await host.session_manager.load(session_id) is None:
            return _session_not_found()
        reason = payload.reason if payload is not None else _DEFAULT_CANCEL_REASON
        run_id = await host.session_interaction.cancel_session(session_id, reason)
        if run_id is None:
            return _error_response(
                status.HTTP_409_CONFLICT,
                WebErrorCode.NO_ACTIVE_RUN,
                "Session 没有活动 Run",
            )
        return {
            "session_id": session_id,
            "run_id": run_id,
            "status": WebRunStatus.CANCELLING.value,
        }

    static_directory = frontend_dir or _frontend_dist_directory()
    if (static_directory / "index.html").is_file():
        # API 路由必须先注册；根挂载只负责构建后的前端文件。
        app.mount(
            "/",
            StaticFiles(directory=static_directory, html=True),
            name="frontend",
        )

    return app


def _frontend_dist_directory() -> Path:
    """定位源码工作区中的 Vite 构建目录。"""
    return Path(__file__).resolve().parents[4] / "frontend" / "dist"


def _host(request: Request) -> ApplicationHost:
    """取得 lifespan 已初始化的唯一应用宿主。"""
    return cast(ApplicationHost, request.app.state.host)


def _runtime_settings(host: ApplicationHost) -> RuntimeSettingsResponse:
    """把宿主持有的真实配置和目录投影为脱敏网页数据。"""
    config = host.config
    router = host.router_config
    preferred = host.preferred_chat_model
    models = []
    for name, model in router.models.items():
        provider = router.providers.get(model.provider)
        models.append(ModelSettingResponse(
            name=name,
            provider=model.provider,
            model_id=model.model_id,
            context_window=model.context_window,
            capabilities=list(model.capabilities),
            status=model.status,
            reasoning_mode=model.reasoning.mode,
            api_key_configured=bool(provider and provider.api_key),
            preferred=name == preferred,
        ))

    executor = host.tool_executor
    tools = []
    if executor is not None:
        tools = [
            ToolSettingResponse(
                name=item.name,
                description=item.description,
                source=item.source.value,
                enabled=executor.registry.is_enabled(item.name),
                needs_approval=item.needs_approval or item.name in config.tools.approval_commands,
            )
            for item in executor.registry.catalog_snapshot()
        ]

    skill_registry = host.skill_registry
    skills = []
    if skill_registry is not None:
        skills = [
            SkillSettingResponse(
                name=item.name,
                description=item.truncated_description(100),
                emoji=item.emoji,
                enabled=skill_registry.is_enabled(item.name),
                always_load=item.always_load,
            )
            for item in skill_registry.list_catalog()
        ]

    provider = host.mcp_provider
    states = provider.get_server_states() if provider is not None else {}
    mcp_servers = []
    for server in config.tools.mcp_servers:
        state, detail = states.get(server.name, (None, "未连接"))
        prefix = f"mcp.{server.name}."
        mcp_servers.append(McpServerSettingResponse(
            name=server.name,
            transport=server.transport,
            endpoint=_safe_mcp_endpoint(server.transport, server.command, server.url),
            state=state.value if state is not None else "disconnected",
            detail=detail,
            tool_count=sum(1 for item in tools if item.name.startswith(prefix)),
        ))

    return RuntimeSettingsResponse(
        general=GeneralSettingResponse(
            stream=config.llm.stream,
            max_context_tokens=config.agent.max_context_tokens,
            keep_recent_messages=config.agent.keep_recent_messages,
            exec_timeout=config.tools.exec_timeout,
            scheduler_enabled=config.scheduler.enabled,
        ),
        models=models,
        tools=sorted(tools, key=lambda item: (item.source, item.name)),
        skills=sorted(skills, key=lambda item: item.name),
        mcp_enabled=config.tools.mcp_enabled,
        mcp_servers=mcp_servers,
    )


def _safe_mcp_endpoint(transport: str, command: str, url: str) -> str:
    """隐藏 HTTP 查询参数，避免设置页泄露令牌。"""
    if transport == "stdio":
        return command
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _runtime_preferences_path(host: ApplicationHost) -> Path | None:
    """定位 Web 运行开关文件；测试替身未声明项目根时跳过持久化。"""
    root = getattr(host, "project_root", None)
    return Path(root) / "data" / "web-settings.json" if root is not None else None


def _load_runtime_preferences(host: ApplicationHost) -> None:
    """在网页服务启动时恢复工具与 Skill 的关闭列表。"""
    path = _runtime_preferences_path(host)
    if path is None or not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        logger.warning("Web 运行设置读取失败，已使用默认状态: %s", error)
        return
    executor = host.tool_executor
    if executor is not None:
        for name in payload.get("disabled_tools", []):
            if isinstance(name, str):
                executor.registry.set_enabled(name, False)
    registry = host.skill_registry
    if registry is not None:
        for name in payload.get("disabled_skills", []):
            if isinstance(name, str):
                registry.set_enabled(name, False)


def _save_runtime_preferences(host: ApplicationHost) -> None:
    """原子写入当前关闭列表，避免进程中断留下半个 JSON 文件。"""
    path = _runtime_preferences_path(host)
    if path is None:
        return
    executor = host.tool_executor
    disabled_tools = []
    if executor is not None:
        disabled_tools = [
            item.name for item in executor.registry.catalog_snapshot()
            if not executor.registry.is_enabled(item.name)
        ]
    registry = host.skill_registry
    disabled_skills = []
    if registry is not None:
        disabled_skills = [
            item.name for item in registry.list_catalog()
            if not registry.is_enabled(item.name)
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({
            "disabled_tools": sorted(disabled_tools),
            "disabled_skills": sorted(disabled_skills),
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _submission_tasks(app: FastAPI) -> set[asyncio.Task[None]]:
    """取得用于保证断连后任务存活的强引用集合。"""
    return cast(set[asyncio.Task[None]], app.state.submission_tasks)


def _run_stream(
    request: Request,
    session_id: str,
    operation: Callable[[SSEOutputAdapter], Awaitable[RunResult]],
) -> StreamingResponse:
    """把提交、审批和恢复统一包装为断连不取消的 SSE 运行。"""
    adapter = SSEOutputAdapter()

    async def produce() -> None:
        """独立执行运行控制；HTTP 订阅断开不取消任务。"""
        try:
            result = await operation(adapter)
            result_session_id = session_id
            if not result_session_id and result.run_id:
                run = await _host(request).session_interaction.get_run_by_id(result.run_id)
                result_session_id = "" if run is None else run.session_id
            logger.log(logging.INFO if result.error is None else logging.WARNING, "web.run.result outcome=%s error_code=%s", result.state.outcome().value if result.state.outcome() is not None else "suspended", result.error.code.value if result.error is not None else "null", extra={"session_id": result_session_id or "-", "run_id": result.run_id or "-"})
            await adapter.finish(_result_event(result_session_id, result))
        except Exception as error:
            logger.error(
                "web.run.exception session_id=%s diagnostic=%s",
                session_id,
                exception_diagnostic(error),
            )
            await adapter.finish(_stream_error_event(session_id))

    async def body() -> AsyncIterator[str]:
        """在响应生命周期内消费事件，断连时只关闭订阅。"""
        task = asyncio.create_task(produce())
        tasks = _submission_tasks(request.app)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        try:
            async for chunk in adapter.stream():
                yield chunk
        finally:
            adapter.disconnect()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _session_summary(session: Session) -> SessionSummaryResponse:
    """从现有 Session 投影稳定的 Web 摘要。"""
    return SessionSummaryResponse(
        id=session.id,
        title=session.title,
        archived=session.archived,
        agent_id=session.agent_id,
        model=session.model,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _session_detail(session: Session) -> SessionDetailResponse:
    """把每条完整 Conversation 展开为 user/assistant 两条消息。"""
    messages: list[SessionMessageResponse] = []
    conversation: Conversation
    for conversation in session.conversations:
        messages.extend(
            (
                SessionMessageResponse(
                    id=conversation.conversation_id,
                    role=MessageRole.USER,
                    content=conversation.user_query,
                    created_at=conversation.created_at,
                ),
                SessionMessageResponse(
                    id=f"{conversation.conversation_id}:assistant",
                    role=MessageRole.ASSISTANT,
                    content=conversation.final_answer,
                    created_at=conversation.created_at,
                    run_ids=list(conversation.agent_run_ids),
                ),
            )
        )
    summary = _session_summary(session)
    return SessionDetailResponse(**summary.model_dump(), messages=messages)


def _run_activity(
    run: AgentRun,
    events: tuple[RunEvent, ...],
    messages: tuple[RunMessage, ...],
    executing: bool,
) -> RunActivityResponse:
    """把运行事实整理为前端可增量刷新的工具执行视图。"""
    calls = {
        call.call_id: call
        for message in messages
        for call in message.tool_calls
    }
    results = {
        message.tool_call_id: message.content
        for message in messages
        if message.kind is RunMessageKind.TOOL_RESULT and message.tool_call_id
    }
    steps: dict[str, ToolExecutionResponse] = {}
    for event in events:
        if event.event_type not in {
            RunEventType.TOOL_STARTED,
            RunEventType.TOOL_COMPLETED,
        }:
            continue
        call_id = str(event.data.get("call_id", ""))
        if not call_id:
            continue
        call = calls.get(call_id)
        current = steps.get(call_id)
        if current is None:
            current = ToolExecutionResponse(
                call_id=call_id,
                name=str(event.data.get("tool_name", "工具")),
                status="running",
                started_at=event.occurred_at if event.event_type is RunEventType.TOOL_STARTED else "",
                arguments={} if call is None else call.arguments,
            )
            steps[call_id] = current
        if event.event_type is RunEventType.TOOL_STARTED:
            current.started_at = current.started_at or event.occurred_at
            current.status = "running"
        else:
            current.completed_at = event.occurred_at
            current.status = str(event.data.get("status", "completed"))
            current.result = results.get(call_id, "")
            current.error = str(event.data.get("error_summary", ""))

    approval_id: str | None = None
    child_run_id: str | None = None
    if run.state.is_waiting_approval() and isinstance(run.state.mode, Suspended):
        status_value = WebRunStatus.WAITING_APPROVAL.value
        approval_id = run.state.mode.control_id
    elif run.state.is_waiting_delegation() and isinstance(run.state.mode, Suspended):
        status_value = WebRunStatus.WAITING_DELEGATION.value
        child_run_id = run.state.mode.control_id
    elif run.state.is_ended():
        outcome = run.state.outcome()
        status_value = outcome.value if outcome is not None else WebRunStatus.FAILED.value
    else:
        status_value = (
            WebRunStatus.RUNNING.value if executing else WebRunStatus.RECOVERABLE.value
        )
    return RunActivityResponse(
        run_id=run.run_id,
        status=status_value,
        approval_id=approval_id,
        child_run_id=child_run_id,
        tools=list(steps.values()),
        error=run.error,
    )


def _result_event(session_id: str, result: RunResult) -> SSEEvent:
    """把 Runtime 结果归一化为 Web 唯一终态事件。"""
    common: JSONMap = {
        "session_id": session_id,
        "run_id": result.run_id,
    }
    outcome = result.state.outcome()
    if outcome is RunOutcome.COMPLETED:
        common.update(
            {
                "status": WebRunStatus.COMPLETED.value,
                "final_message": (
                    None if result.final_message is None else result.final_message.to_dict()
                ),
                "has_streamed_response": result.has_streamed_response,
            }
        )
        return SSEEvent(event=SSEEventType.RUN_COMPLETED, data=common)
    if outcome is RunOutcome.CANCELLED:
        common["status"] = WebRunStatus.CANCELLED.value
        return SSEEvent(event=SSEEventType.RUN_CANCELLED, data=common)
    if result.state.is_waiting_approval():
        common.update(
            {
                "status": WebRunStatus.WAITING_APPROVAL.value,
                "approval_id": result.approval_id,
            }
        )
        return SSEEvent(event=SSEEventType.RUN_SUSPENDED, data=common)
    if result.state.is_waiting_delegation():
        common.update(
            {
                "status": WebRunStatus.WAITING_DELEGATION.value,
                "child_run_id": result.child_run_id,
            }
        )
        return SSEEvent(event=SSEEventType.RUN_SUSPENDED, data=common)

    error = result.error or RunError(
        RunErrorCode.INVALID_STATE,
        "运行未返回可展示终态",
    )
    common.update({"status": WebRunStatus.FAILED.value, "error": error.to_dict()})
    return SSEEvent(event=SSEEventType.RUN_FAILED, data=common)


def _stream_error_event(session_id: str) -> SSEEvent:
    """生成不携带异常正文的 Web 适配层失败事件。"""
    return SSEEvent(
        event=SSEEventType.STREAM_ERROR,
        data={
            "session_id": session_id,
            "run_id": "",
            "status": WebRunStatus.FAILED.value,
            "error": {
                "code": WebErrorCode.INTERNAL_ERROR.value,
                "message": "Web 消息流执行失败",
                "retryable": False,
            },
        },
    )


def _session_not_found() -> JSONResponse:
    """返回统一的 Session 不存在错误。"""
    return _error_response(
        status.HTTP_404_NOT_FOUND,
        WebErrorCode.SESSION_NOT_FOUND,
        "Session 不存在",
    )


def _error_response(
    status_code: int,
    code: WebErrorCode,
    message: str,
) -> JSONResponse:
    """构造统一且脱敏的 JSON 错误结构。"""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code.value, "message": message}},
    )


def main() -> None:
    """仅在本机回环地址启动 GUI 后端。"""
    # Uvicorn 是 gui 可选依赖，延迟导入避免普通 CLI 启动依赖 Web 服务器。
    import uvicorn

    uvicorn.run(
        "dotclaw.channel.web.app:create_app",
        factory=True,
        host=_LOCAL_WEB_HOST,
        port=_DEFAULT_WEB_PORT,
    )
