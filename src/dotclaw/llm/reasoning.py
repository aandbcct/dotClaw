"""模型推理输出策略与标签流解析器（总体设计 §3.2）。

依赖方向：llm 可依赖 config，但 reasoning.py 本身不依赖 runtime。
ReasoningPolicy 由 ModelRouter 在创建缓存 Client 时从 ModelReasoningConfig 转换；
ReasoningStreamParser 在每次 chat() 调用时新建，仅 tags 模式创建。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from .base import ChatTextDelta, TextDeltaKind

if TYPE_CHECKING:
    from ..config.settings import ModelReasoningConfig


class ReasoningMode(StrEnum):
    """推理输出模式（总体设计 §3.2）。

    - none：不识别推理内容，所有 content 原样成为 response。
    - native：从 Provider 原生字段 reasoning_content 提取，由 Provider Client 处理。
    - tags：通过 ```
      等标签从 content 文本中解析出 reasoning 与 response。
    """
    NONE = "none"
    NATIVE = "native"
    TAGS = "tags"


# 标签模式下的标准默认标签（总体设计 §3.2）。
_DEFAULT_REASONING_START = "<think>"
_DEFAULT_REASONING_END = "</think>"
_DEFAULT_RESPONSE_START = "<response>"
_DEFAULT_RESPONSE_END = "</response>"


@dataclass(frozen=True)
class ReasoningPolicy:
    """不可变推理策略，由 ModelRouter 从 ModelReasoningConfig 转换。

    Provider Client 仅保存此策略，不自行从 YAML 读取。
    """
    mode: ReasoningMode = ReasoningMode.NONE
    reasoning_start: str = _DEFAULT_REASONING_START
    reasoning_end: str = _DEFAULT_REASONING_END
    response_start: str = _DEFAULT_RESPONSE_START
    response_end: str = _DEFAULT_RESPONSE_END

    @classmethod
    def from_config(cls, config: "ModelReasoningConfig") -> ReasoningPolicy:
        """从 ModelReasoningConfig 转换为不可变策略。

        tags 模式保留标签值；none/native 模式仅保留 mode，标签值使用标准默认。
        """
        mode = ReasoningMode(config.mode)
        if mode is ReasoningMode.TAGS:
            return cls(
                mode=mode,
                reasoning_start=config.reasoning_start,
                reasoning_end=config.reasoning_end,
                response_start=config.response_start,
                response_end=config.response_end,
            )
        return cls(mode=mode)


class _Region(StrEnum):
    """解析器内部状态：当前文本属于 response 还是 reasoning。"""
    RESPONSE = "response"
    REASONING = "reasoning"


class ReasoningStreamParser:
    """标签模式下的流式文本解析器（每次 chat() 新建）。

    状态机规则（总体设计 §3.2 / 开发计划阶段二）：

    - 默认状态 RESPONSE（标签外文本为 response）。
    - reasoning_start 在 RESPONSE → 切换到 REASONING，标签本身不产生 delta。
    - reasoning_start 在 REASONING → 嵌套开始标签，按 reasoning 原文输出。
    - reasoning_end 在 REASONING → 切换到 RESPONSE，标签本身不产生 delta。
    - reasoning_end 在 RESPONSE → 未匹配结束标签，按 response 原文输出。
    - 标签可跨 raw chunk；不完整的标签前缀保留在缓冲区等待下次 feed()。
    - flush() 输出缓冲区剩余文本，按当前状态分类；未闭合 reasoning 的正文仍为 reasoning。
    """

    def __init__(self, policy: ReasoningPolicy) -> None:
        self._policy = policy
        self._state: _Region = _Region.RESPONSE
        self._buffer: str = ""

    def feed(self, text: str) -> list[ChatTextDelta]:
        """输入一段原始文本，返回解析出的有序文本增量。

        同一次 feed 可能产生多个 delta（标签前文本 + 标签后的状态切换）。
        """
        self._buffer += text
        return self._drain_buffer()

    def flush(self) -> list[ChatTextDelta]:
        """流结束时调用，输出缓冲区中剩余文本。

        未闭合 reasoning 区的正文仍按 reasoning 输出；
        标签本身不展示。
        """
        if self._buffer:
            text = self._buffer
            self._buffer = ""
            return [ChatTextDelta(self._current_kind(), text)]
        return []

    # ============================================================
    # 内部实现
    # ============================================================

    def _current_kind(self) -> TextDeltaKind:
        """当前状态对应的文本语义类别。"""
        if self._state is _Region.REASONING:
            return TextDeltaKind.REASONING
        return TextDeltaKind.RESPONSE

    def _drain_buffer(self) -> list[ChatTextDelta]:
        """处理缓冲区：查找完整标签、输出安全文本。"""
        deltas: list[ChatTextDelta] = []

        while True:
            match = self._find_earliest_tag(self._buffer)
            if match is None:
                break

            pos, length, is_start = match

            # 输出标签前的文本
            if pos > 0:
                before = self._buffer[:pos]
                deltas.append(ChatTextDelta(self._current_kind(), before))

            # 取出标签文本，从缓冲区移除
            tag_text = self._buffer[pos:pos + length]
            self._buffer = self._buffer[pos + length:]

            if is_start:
                self._on_reasoning_start(tag_text, deltas)
            else:
                self._on_reasoning_end(tag_text, deltas)

        # 输出安全文本（保留可能是部分标签的后缀）
        safe_len = self._safe_output_length(self._buffer)
        if safe_len > 0:
            safe_text = self._buffer[:safe_len]
            self._buffer = self._buffer[safe_len:]
            deltas.append(ChatTextDelta(self._current_kind(), safe_text))

        return deltas

    def _on_reasoning_start(self, tag_text: str, deltas: list[ChatTextDelta]) -> None:
        """处理 reasoning_start 标签。"""
        if self._state is _Region.RESPONSE:
            self._state = _Region.REASONING
        else:
            # 嵌套开始标签 → 按 reasoning 原文输出
            deltas.append(ChatTextDelta(TextDeltaKind.REASONING, tag_text))

    def _on_reasoning_end(self, tag_text: str, deltas: list[ChatTextDelta]) -> None:
        """处理 reasoning_end 标签。"""
        if self._state is _Region.REASONING:
            self._state = _Region.RESPONSE
        else:
            # 未匹配结束标签 → 按 response 原文输出
            deltas.append(ChatTextDelta(TextDeltaKind.RESPONSE, tag_text))

    def _find_earliest_tag(self, buffer: str) -> tuple[int, int, bool] | None:
        """在 buffer 中查找最早的完整标签。

        返回 (位置, 长度, 是否为开始标签)。
        同一位置出现多个标签时，优先匹配较长者（处理前缀重叠）。
        """
        rstart = self._policy.reasoning_start
        rend = self._policy.reasoning_end

        candidates: list[tuple[int, int, bool]] = []

        if rstart:
            pos = buffer.find(rstart)
            if pos != -1:
                candidates.append((pos, len(rstart), True))

        if rend:
            pos = buffer.find(rend)
            if pos != -1:
                candidates.append((pos, len(rend), False))

        if not candidates:
            return None

        # 最早位置优先；同位置时优先较长标签
        return min(candidates, key=lambda c: (c[0], -c[1]))

    def _safe_output_length(self, buffer: str) -> int:
        """返回可安全输出的长度，保留可能是部分标签的后缀。

        例如缓冲区以 `` 的前缀 `` 结尾时，保留该前缀等待下次 feed() 补全。
        """
        if not buffer:
            return 0

        rstart = self._policy.reasoning_start
        rend = self._policy.reasoning_end

        max_prefix_len = 0

        for tag in (rstart, rend):
            if not tag:
                continue
            # 从长到短检查：buffer 的后缀是否是 tag 的前缀
            max_check = min(len(buffer), len(tag))
            for i in range(max_check, 0, -1):
                if buffer.endswith(tag[:i]):
                    if i > max_prefix_len:
                        max_prefix_len = i
                    break  # 找到最长前缀即可

        return len(buffer) - max_prefix_len
