"""固定助理会话入口。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..session.session import Session, SessionManager
from .service import ASSISTANT_AGENT_ID, ASSISTANT_SESSION_ID, AssistantService
from .sqlite_repository import SqliteAssistantRepository

if TYPE_CHECKING:
    from ..bootstrap.session_interaction import SessionInteractionService


class AssistantSessionMissingError(RuntimeError):
    """存在助理业务记录但固定会话文件缺失。"""


class AssistantSessionInvalidError(RuntimeError):
    """固定助理会话文件损坏或身份状态不匹配。"""


class AssistantEntryService:
    """以固定 ID 确保唯一助理会话，并同步请求控制投影。"""

    def __init__(
        self,
        session_interaction: SessionInteractionService,
        session_manager: SessionManager,
        assistant_service: AssistantService,
        assistant_repository: SqliteAssistantRepository,
    ) -> None:
        self._session_interaction = session_interaction
        self._session_manager = session_manager
        self._assistant_service = assistant_service
        self._assistant_repository = assistant_repository
        self._ensure_lock = asyncio.Lock()

    async def ensure_session(self) -> Session:
        """锁内读取或创建固定 ID 会话，发现损坏或孤立业务记录时明确失败。"""
        async with self._ensure_lock:
            session_directory = self._session_manager.session_directory(ASSISTANT_SESSION_ID)
            session_file = session_directory / "session.json"
            if session_file.exists():
                session = await self._session_manager.load(ASSISTANT_SESSION_ID)
                if session is None:
                    raise AssistantSessionInvalidError(f"固定助理会话文件损坏: {session_file}")
                if session.id != ASSISTANT_SESSION_ID or session.agent_id != ASSISTANT_AGENT_ID or session.archived:
                    raise AssistantSessionInvalidError(f"固定助理会话身份或归档状态无效: {session_file}")
                if session.model not in self._session_interaction.chat_models:
                    raise AssistantSessionInvalidError(f"固定助理会话绑定了不可用模型: {session_file}")
                self._session_interaction.get_identity(session)
                return session
            if await self._assistant_repository.has_session_data(ASSISTANT_SESSION_ID):
                raise AssistantSessionMissingError(
                    f"助理数据库已有会话记录，但固定会话文件缺失: {session_file}"
                )
            if session_directory.exists():
                raise AssistantSessionInvalidError(f"固定助理会话目录缺少 session.json: {session_file}")
            return await self._session_interaction.ensure_assistant_session()

    async def before_submit(self) -> None:
        """提交新消息前同步已终结的批量审批结果。"""
        await self._assistant_service.reconcile_controls(ASSISTANT_SESSION_ID)
