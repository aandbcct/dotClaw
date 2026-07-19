"""从既有 Session 快照创建 Runtime v2 的冻结 RunRequest。"""

from __future__ import annotations

import uuid
from typing import Protocol

from ..domain.facts import MessageRole, utc_now_iso
from .dto import ConversationMessage, ConversationSnapshot, RunRequest


class ConversationSnapshotSource(Protocol):
    """创建请求所需的既有 Conversation 只读字段。"""

    user_query: str
    final_answer: str
    created_at: str


class SessionSnapshotSource(Protocol):
    """创建请求所需的既有 Session 只读字段。"""

    id: str
    conversations: list[ConversationSnapshotSource]


def create_run_request(session: SessionSnapshotSource, agent_id: str, user_message: str) -> RunRequest:
    """复制已有 Conversation 并创建不携带可变 Session 的运行请求。"""
    history: list[ConversationMessage] = []
    for index, conversation in enumerate(session.conversations, start=1):
        history.append(ConversationMessage(
            message_id=f"conversation-{session.id}-{index}-user",
            role=MessageRole.USER,
            content=conversation.user_query,
            created_at=conversation.created_at,
        ))
        history.append(ConversationMessage(
            message_id=f"conversation-{session.id}-{index}-assistant",
            role=MessageRole.ASSISTANT,
            content=conversation.final_answer,
            created_at=conversation.created_at,
        ))
    return create_run_request_from_snapshot(
        session_id=session.id,
        agent_id=agent_id,
        user_message=user_message,
        conversation=ConversationSnapshot(session.id, tuple(history), len(session.conversations)),
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
