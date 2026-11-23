"""自然语言助理固定会话与只读结果路由。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from ...assistant.entry_service import AssistantSessionInvalidError, AssistantSessionMissingError
from ...assistant.models import AssistantProposalPreview, AssistantRunResults
from ...assistant.service import (
    ASSISTANT_SESSION_ID,
    AssistantConfigurationError,
    AssistantRequestNotFound,
    AssistantRequestSourceError,
)
from ...assistant.sqlite_repository import AssistantRequestConflict
from ...personal_tasks.repository import PersonalTaskStorageError
from ...runtime.domain.facts import MessageRole
from .schemas import SessionDetailResponse, SessionMessageResponse, WebErrorCode

logger = logging.getLogger("dotclaw.channel.web.assistant")
router = APIRouter()


def _error(status_code: int, code: WebErrorCode, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code.value, "message": message}})


@router.post("/api/v1/assistant/session", response_model=SessionDetailResponse)
async def ensure_assistant_session(request: Request) -> SessionDetailResponse | JSONResponse:
    """确保助理固定会话存在并同步已结束控制。"""
    host = request.app.state.host
    try:
        session = await host.assistant_entry_service.ensure_session()
        await host.assistant_service.reconcile_controls(ASSISTANT_SESSION_ID)
    except AssistantSessionMissingError as error:
        logger.error("助理业务数据缺少固定会话: %s", error)
        return _error(409, WebErrorCode.ASSISTANT_SESSION_MISSING, "助理会话记录缺失，请检查存储")
    except AssistantSessionInvalidError as error:
        logger.error("固定助理会话不可用: %s", error)
        return _error(500, WebErrorCode.INTERNAL_ERROR, "助理会话暂时无法打开")
    except (AssistantConfigurationError, PersonalTaskStorageError, OSError, ValueError) as error:
        logger.error("助理入口读取失败: %s", error)
        return _error(500, WebErrorCode.INTERNAL_ERROR, "助理会话暂时无法读取")
    messages: list[SessionMessageResponse] = []
    for conversation in session.conversations:
        messages.extend((
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
        ))
    return SessionDetailResponse(
        id=session.id,
        title=session.title,
        archived=session.archived,
        agent_id=session.agent_id,
        model=session.model,
        created_at=session.created_at,
        updated_at=session.updated_at,
        messages=messages,
    )


@router.get("/api/v1/assistant/results", response_model=AssistantRunResults)
async def get_assistant_results(
    request: Request,
    run_id: str = Query(min_length=1, max_length=200),
) -> AssistantRunResults | JSONResponse:
    """按固定会话归属返回已持久化回执与结果快照。"""
    host = request.app.state.host
    try:
        return await host.assistant_service.get_run_results(ASSISTANT_SESSION_ID, run_id)
    except AssistantRequestSourceError:
        return _error(404, WebErrorCode.RUN_NOT_FOUND, "运行不存在或不属于助理会话")
    except (AssistantConfigurationError, PersonalTaskStorageError, OSError, ValueError) as error:
        logger.error("助理运行结果读取失败: %s", error)
        return _error(500, WebErrorCode.INTERNAL_ERROR, "助理运行结果暂时无法读取")


@router.get("/api/v1/assistant/requests/{request_id}/proposal", response_model=AssistantProposalPreview)
async def get_assistant_proposal(
    request_id: str,
    request: Request,
    version: int = Query(ge=1),
    hash: str = Query(pattern=r"^[0-9a-f]{64}$"),
) -> AssistantProposalPreview | JSONResponse:
    """仅返回版本与哈希匹配的已保存提案快照。"""
    host = request.app.state.host
    try:
        return await host.assistant_service.get_proposal(ASSISTANT_SESSION_ID, request_id, version, hash)
    except AssistantRequestNotFound:
        return _error(404, WebErrorCode.INVALID_REQUEST, "助理请求不存在")
    except AssistantRequestConflict:
        return _error(409, WebErrorCode.REQUEST_CONFLICT, "提案已变化，请刷新后重试")
    except AssistantRequestSourceError:
        return _error(404, WebErrorCode.RUN_NOT_FOUND, "提案来源运行不存在")
    except (AssistantConfigurationError, PersonalTaskStorageError, OSError, ValueError) as error:
        logger.error("助理提案读取失败: %s", error)
        return _error(500, WebErrorCode.INTERNAL_ERROR, "助理提案暂时无法读取")
