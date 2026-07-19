"""上下文压缩用例的通用数据契约。

本模块只定义 Application 层输入与输出。阶段 A 不在 RuntimeEngine 中启用压缩，
后续 Session 历史压缩和每次 LLM 调用前的 Run 上下文压缩复用同一套 DTO。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.facts import ContextCompactionScope, MessageRole


@dataclass(frozen=True)
class ContextFragment:
    """参与压缩的有序上下文片段。"""

    fragment_id: str
    role: MessageRole
    content: str


@dataclass(frozen=True)
class ContextCompactionRequest:
    """请求将已有摘要与新增片段合成为下一版摘要。"""

    scope: ContextCompactionScope
    source_version: int
    target_token_budget: int
    fragments: tuple[ContextFragment, ...]
    previous_summary: str = ""
    previous_summary_version: int = 0


@dataclass(frozen=True)
class ContextCompactionResult:
    """一次压缩产生的版本化摘要结果。"""

    scope: ContextCompactionScope
    version: int
    covered_through_fragment_id: str
    content: str
    content_hash: str
    source_hash: str
