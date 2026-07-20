"""基于 tiktoken 的精确结构化 LLM 输入 Token 计数器。"""

from __future__ import annotations

import json
import logging

from ..application.context_budget import TokenCountErrorCode, TokenCountRequest, TokenCountResult

logger = logging.getLogger(__name__)


class TiktokenTokenCounter:
    """仅使用显式编码计数，缺少编码时拒绝而不回退字符估算。"""
    async def count(self, request: TokenCountRequest) -> TokenCountResult:
        """统计系统、历史、当前输入、Run Message、工具与协议开销。"""
        if request.protocol_overhead_tokens < 0:
            return TokenCountResult(0, TokenCountErrorCode.INVALID_REQUEST, "协议开销不能为负数")
        try:
            import tiktoken
            encoding = tiktoken.get_encoding(request.tokenizer_encoding)
        except Exception:
            logger.warning("Tokenizer 不可用：encoding=%s", request.tokenizer_encoding)
            return TokenCountResult(0, TokenCountErrorCode.TOKENIZER_UNAVAILABLE, "Tokenizer 不可用")
        contents: tuple[str, ...] = _countable_contents(request)
        input_tokens: int = sum(len(encoding.encode(content)) for content in contents)
        return TokenCountResult(input_tokens + request.protocol_overhead_tokens)


def _countable_contents(request: TokenCountRequest) -> tuple[str, ...]:
    """按结构化请求顺序收集所有真实输入，工具 Schema 使用稳定 JSON。"""
    contents: list[str] = list(request.system_contents)
    if request.history_summary:
        contents.append(request.history_summary)
    contents.extend(message.content for message in request.history_messages)
    contents.append(request.current_user_message.content)
    contents.extend(message.content for message in request.run_messages)
    contents.extend(json.dumps(tool.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) for tool in request.tools)
    return tuple(contents)
