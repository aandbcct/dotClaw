"""dotClaw 本地 GUI 的 FastAPI 应用与 HTTP/SSE 适配。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ...bootstrap.application_host import ApplicationHost
from ...bootstrap.session_interaction import UnknownIdentityError
from ...runtime.application.dto import RunResult
from ...runtime.domain.facts import JSONMap, MessageRole, RunError, RunErrorCode
from ...runtime.domain.state import RunOutcome
from ...session.session import Conversation, Session
from .schemas import (
    CancelSessionRequest,
    CreateSessionRequest,
    SSEEvent,
    SSEEventType,
    SessionDetailResponse,
    SessionMessageResponse,
    SessionSummaryResponse,
    SubmitMessageRequest,
    WebErrorCode,
    WebRunStatus,
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
        if await host.session_manager.load(session_id) is None:
            return _session_not_found()

        adapter = SSEOutputAdapter()

        async def produce() -> None:
            """独立执行 Run；HTTP 订阅断开不会取消该任务。"""
            try:
                result = await host.session_interaction.submit(
                    session_id,
                    payload.content,
                    adapter,
                )
                await adapter.finish(_result_event(session_id, result))
            except Exception as error:
                logger.error(
                    "Web 消息流执行失败：session_id=%s error_type=%s",
                    session_id,
                    type(error).__name__,
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
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
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


def _submission_tasks(app: FastAPI) -> set[asyncio.Task[None]]:
    """取得用于保证断连后任务存活的强引用集合。"""
    return cast(set[asyncio.Task[None]], app.state.submission_tasks)


def _session_summary(session: Session) -> SessionSummaryResponse:
    """从现有 Session 投影稳定的 Web 摘要。"""
    return SessionSummaryResponse(
        id=session.id,
        title=session.title,
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
                ),
            )
        )
    summary = _session_summary(session)
    return SessionDetailResponse(**summary.model_dump(), messages=messages)


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
