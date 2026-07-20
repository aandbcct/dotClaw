"""基于既有 LLMProxy 的上下文压缩适配器。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Protocol

from ...llm.base import ChatChunk, Message, ToolDefinition
from ...llm.base import LLMUsage
from ..application.context_compaction import ContextCompactionRequest, ContextCompactionResult, ContextFragment
from ..application.dto import ConversationMessage
from ..application.history_compaction import ConversationBatch, HistoryCompactionRequest, HistoryCompactionResult, HistoryCompactorUnavailable
from ..domain.facts import ContextCompactionScope


class LLMCompactionClient(Protocol):
    """上下文压缩适配器所需的最小模型调用能力。"""

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        model: str | None = None,
        purpose: str = "chat",
        stream: bool = True,
    ) -> AsyncIterator[ChatChunk]:
        """调用模型并返回响应块。"""


class LLMContextCompactor:
    """将 Application 层上下文压缩请求转换为一次非流式 LLM 调用。"""

    def __init__(self, llm_proxy: LLMCompactionClient) -> None:
        """绑定既有模型代理，模型路由仍由代理自身负责。"""
        self._llm_proxy: LLMCompactionClient = llm_proxy

    async def compact(self, request: ContextCompactionRequest) -> ContextCompactionResult:
        """生成下一版摘要；空摘要视为压缩失败，避免丢失原始历史。"""
        messages: list[Message] = _build_compaction_messages(request)
        try:
            chunks: AsyncIterator[ChatChunk] = self._llm_proxy.chat(
                messages=messages,
                tools=None,
                purpose=LLMUsage.CONTEXT_COMPACTION,
                stream=False,
            )
        except Exception as error:
            raise HistoryCompactorUnavailable("上下文压缩服务不可用") from error
        content_parts: list[str] = []
        try:
            async for chunk in chunks:
                if chunk.content:
                    content_parts.append(chunk.content)
        except Exception as error:
            raise HistoryCompactorUnavailable("上下文压缩服务不可用") from error
        summary: str = "".join(content_parts).strip()
        if not summary:
            raise RuntimeError("上下文压缩模型未返回有效摘要")
        last_fragment_id: str = request.fragments[-1].fragment_id
        source_hash: str = _source_hash(request)
        return ContextCompactionResult(
            scope=request.scope,
            version=request.previous_summary_version + 1,
            covered_through_fragment_id=last_fragment_id,
            content=summary,
            content_hash=_hash_text(summary),
            source_hash=source_hash,
        )

    async def compact_history(self, request: HistoryCompactionRequest) -> HistoryCompactionResult:
        """实现 HistoryCompactorPort，使用真实压缩路由处理完整 Conversation 批次。"""
        fragments: list[ContextFragment] = []
        batch: ConversationBatch
        for batch in request.batches:
            message: ConversationMessage
            for message in batch.messages:
                fragments.append(ContextFragment(f"{batch.conversation_id}:{message.message_id}", message.role, message.content))
        result: ContextCompactionResult = await self.compact(ContextCompactionRequest(
            scope=ContextCompactionScope.SESSION_HISTORY,
            source_version=0,
            target_token_budget=request.source_context_window,
            fragments=tuple(fragments),
            previous_summary=request.previous_summary,
        ))
        return HistoryCompactionResult(result.content)


def _build_compaction_messages(request: ContextCompactionRequest) -> list[Message]:
    """构造不携带工具的压缩提示，保持压缩器没有执行副作用。"""
    fragments: list[dict[str, str]] = [
        {"id": fragment.fragment_id, "role": fragment.role.value, "content": fragment.content}
        for fragment in request.fragments
    ]
    payload: str = json.dumps({
        "scope": request.scope.value,
        "previous_summary": request.previous_summary,
        "fragments": fragments,
        "target_token_budget": request.target_token_budget,
    }, ensure_ascii=False)
    return [
        Message("system", "你负责压缩对话上下文。保留用户目标、已确认事实、约束、未完成事项与关键工具结论；不要编造信息，只输出可供后续模型使用的中文摘要。"),
        Message("user", payload),
    ]


def _source_hash(request: ContextCompactionRequest) -> str:
    """生成摘要来源的稳定内容 hash，供审计验证。"""
    source: str = json.dumps({
        "scope": request.scope.value,
        "previous_summary": request.previous_summary,
        "fragments": [
            {"id": fragment.fragment_id, "role": fragment.role.value, "content": fragment.content}
            for fragment in request.fragments
        ],
    }, ensure_ascii=False, sort_keys=True)
    return _hash_text(source)


def _hash_text(content: str) -> str:
    """返回带算法前缀的 UTF-8 文本 hash。"""
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"
