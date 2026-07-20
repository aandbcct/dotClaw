"""从既有 Session 快照创建 Runtime v3 的冻结 RunRequest。"""

from __future__ import annotations

import uuid
from typing import Protocol

from ..domain.facts import HistoryCompressionSnapshot, MessageRole, utc_now_iso
from .dto import ConversationMessage, ConversationSnapshot, RunRequest


class ConversationSnapshotSource(Protocol):
    """创建请求所需的既有 Conversation 只读字段。"""

    conversation_id: str
    user_query: str
    final_answer: str
    created_at: str


class HistoryCompressionSnapshotSource(Protocol):
    """创建请求所需的活动历史压缩只读字段。"""

    version: int
    covered_through_conversation_id: str
    content: str
    content_hash: str


class SessionSnapshotSource(Protocol):
    """创建请求所需的既有 Session 只读字段。"""

    id: str
    conversation_version: int
    conversations: list[ConversationSnapshotSource]

    def active_history_compression(self) -> HistoryCompressionSnapshotSource | None:
        """返回当前 Session 正在生效的历史压缩版本。"""


def create_run_request(session: SessionSnapshotSource, agent_id: str, user_message: str) -> RunRequest:
    """复制已有 Conversation 并创建不携带可变 Session 的运行请求。"""
    compressed_history: HistoryCompressionSnapshot | None = _active_compression_snapshot(session)
    history: list[ConversationMessage] = []
    if compressed_history is not None:
        history.append(ConversationMessage(
            message_id=f"history-compression-{compressed_history.compression_version}",
            role=MessageRole.SYSTEM,
            content=_history_summary_message(compressed_history.content),
            created_at=utc_now_iso(),
        ))
    conversation: ConversationSnapshotSource
    for conversation in _uncovered_conversations(session.conversations, compressed_history):
        history.append(ConversationMessage(
            message_id=conversation.conversation_id,
            role=MessageRole.USER,
            content=conversation.user_query,
            created_at=conversation.created_at,
        ))
        history.append(ConversationMessage(
            message_id=f"{conversation.conversation_id}:assistant",
            role=MessageRole.ASSISTANT,
            content=conversation.final_answer,
            created_at=conversation.created_at,
        ))
    return create_run_request_from_snapshot(
        session_id=session.id,
        agent_id=agent_id,
        user_message=user_message,
        conversation=ConversationSnapshot(
            session.id,
            tuple(history),
            session.conversation_version,
            compressed_history,
        ),
    )


def create_run_request_from_snapshot(
    session_id: str,
    agent_id: str,
    user_message: str,
    conversation: ConversationSnapshot,
) -> RunRequest:
    """基于已准备完成的冻结历史创建单次 RunRequest。"""
    input_message: ConversationMessage = ConversationMessage(
        message_id=f"input-{uuid.uuid4().hex}",
        role=MessageRole.USER,
        content=user_message,
        created_at=utc_now_iso(),
    )
    return RunRequest(
        session_id=session_id,
        lease_id=f"lease-{uuid.uuid4().hex}",
        agent_id=agent_id,
        user_message=input_message,
        conversation=conversation,
    )


def _active_compression_snapshot(session: SessionSnapshotSource) -> HistoryCompressionSnapshot | None:
    """将 Session 的活动摘要转换为 Runtime 不可变快照。"""
    compression: HistoryCompressionSnapshotSource | None = session.active_history_compression()
    if compression is None:
        return None
    return HistoryCompressionSnapshot(
        compression.version,
        compression.covered_through_conversation_id,
        compression.content,
        compression.content_hash,
    )


def _uncovered_conversations(
    conversations: list[ConversationSnapshotSource],
    compressed_history: HistoryCompressionSnapshot | None,
) -> tuple[ConversationSnapshotSource, ...]:
    """仅保留活动压缩边界之后仍需原文注入的 Conversation。"""
    if compressed_history is None:
        return tuple(conversations)
    boundary_index: int | None = next(
        (
            index
            for index, conversation in enumerate(conversations)
            if conversation.conversation_id == compressed_history.covered_through_conversation_id
        ),
        None,
    )
    if boundary_index is None:
        raise ValueError("活动历史压缩边界不属于当前 Session")
    return tuple(conversations[boundary_index + 1:])


def _history_summary_message(summary: str) -> str:
    """构造与 Engine 压缩路径完全一致的摘要注入文本。"""
    return f"以下是此前对话的压缩摘要：\n{summary}"
