"""将 Runtime v4 成功运行投影到既有 Session 的适配器。"""

from __future__ import annotations

from ..domain.facts import AgentRun, HistoryCompressionSnapshot, RunMessage
from ...session.session import Session, SessionManager


class SessionConversationProjector:
    """通过 SessionManager 持久化成功运行对应的一条 Conversation。"""

    def __init__(self, session_manager: SessionManager) -> None:
        """绑定既有会话存储管理器。"""
        self._session_manager: SessionManager = session_manager

    async def project_success(
        self,
        run: AgentRun,
        user_message: RunMessage,
        final_message: RunMessage,
        history_compression: HistoryCompressionSnapshot | None,
        source_conversation_hash: str,
    ) -> None:
        """将 Conversation 和最新压缩候选在同一次 Session 保存中原子写入。"""
        session: Session | None = await self._session_manager.load(run.session_id)
        if session is None:
            raise FileNotFoundError(f"Session {run.session_id} 不存在，无法投影成功运行")
        if self._contains_run(session, run.run_id):
            return
        session.add_conversation(
            user_query=user_message.content,
            final_answer=final_message.content,
            agent_run_ids=[run.run_id],
        )
        if history_compression is not None:
            session.append_history_compression(
                history_compression.compression_version,
                history_compression.covered_through_conversation_id,
                history_compression.content,
                history_compression.content_hash,
                source_conversation_hash,
                history_compression.compression_version - 1,
            )
        await self._session_manager.save(session)

    def _contains_run(self, session: Session, run_id: str) -> bool:
        """判断成功投影是否已经写入，保证重复提交不会生成重复 Conversation。"""
        return any(run_id in conversation.agent_run_ids for conversation in session.conversations)
