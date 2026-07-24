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
    """解析器内部状态（总体设计 §3.2 / 开发计划阶段二，response 标签生效版）。

    - OUTSIDE：标签外区域，正文归 RESPONSE；`` 与 `` 触发区域切换。
    - REASONING：推理区域，正文归 REASONING；`` 切回 OUTSIDE。
    - EXPLICIT_RESPONSE：显式 `` 区域，正文同样归 RESPONSE（仅剥离协议标签，
      不创建新的文本语义）。
    """
    OUTSIDE = "outside"
    REASONING = "reasoning"
    EXPLICIT_RESPONSE = "explicit_response"


class ReasoningStreamParser:
    """标签模式下的流式文本解析器（每次 chat() 新建）。

    三态状态机（response 标签生效，开发计划阶段二修订）：

    - OUTSIDE: `` → REASONING；`` → EXPLICIT_RESPONSE；普通文本 → RESPONSE。
    - REASONING: `` → OUTSIDE；其他标签 → 作为 reasoning 原文。
    - EXPLICIT_RESPONSE: `` → OUTSIDE；其他标签 → 作为 response 原文。

    约束：
    - OUTSIDE 与 EXPLICIT_RESPONSE 的正文都输出 RESPONSE；`` 的价值是剥离
      协议标签，不是创建新的文本语义（核心简化语义“标签外文本默认是 response”不变）。
    - 未匹配结束标签按当前区域原文输出。
    - 不支持嵌套；区域内出现其他开始标签按正文保留。
    - 标签可跨 raw chunk；不完整的标签前缀保留在缓冲区等待下次 feed()。
    - flush() 输出缓冲区剩余文本，按当前区域分类；REASONING→reasoning，其余→response。
    """

    def __init__(self, policy: ReasoningPolicy) -> None:
        self._policy = policy
        self._state: _Region = _Region.OUTSIDE
        self._buffer: str = ""

    def feed(self, text: str) -> list[ChatTextDelta]:
        """输入一段原始文本，返回解析出的有序文本增量。

        同一次 feed 可能产生多个 delta（标签前文本 + 标签后的状态切换）。
        """
        self._buffer += text
        return self._drain_buffer()

    def flush(self) -> list[ChatTextDelta]:
        """流结束时调用，输出缓冲区中剩余文本。

        未闭合 reasoning 区的正文仍按 reasoning 输出；未闭合 explicit response
        区的正文仍按 response 输出；标签本身不展示。
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
        """当前状态对应的文本语义类别（OUTSIDE / EXPLICIT_RESPONSE → RESPONSE）。"""
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

            pos, length, tag = match

            # 输出标签前的文本
            if pos > 0:
                before = self._buffer[:pos]
                deltas.append(ChatTextDelta(self._current_kind(), before))

            # 取出标签文本，从缓冲区移除
            tag_text = self._buffer[pos:pos + length]
            self._buffer = self._buffer[pos + length:]

            self._on_tag(tag, tag_text, deltas)

        # 输出安全文本（保留可能是部分标签的后缀）
        safe_len = self._safe_output_length(self._buffer)
        if safe_len > 0:
            safe_text = self._buffer[:safe_len]
            self._buffer = self._buffer[safe_len:]
            deltas.append(ChatTextDelta(self._current_kind(), safe_text))

        return deltas

    def _on_tag(self, tag: str, tag_text: str, deltas: list[ChatTextDelta]) -> None:
        """根据当前区域处理一个已识别标签。

        仅在当前区域遇到“匹配的切换标签”时才切换状态（协议标签被剥离，不产生 delta）；
        其余标签（未匹配结束标签、区域内其他开始标签）一律按当前区域原文输出。
        """
        if self._state is _Region.OUTSIDE:
            if tag == self._policy.reasoning_start:
                self._state = _Region.REASONING
            elif tag == self._policy.response_start:
                self._state = _Region.EXPLICIT_RESPONSE
            else:
                # 未匹配结束标签（`` / ``）按 response 原文输出
                deltas.append(ChatTextDelta(TextDeltaKind.RESPONSE, tag_text))
        elif self._state is _Region.REASONING:
            if tag == self._policy.reasoning_end:
                self._state = _Region.OUTSIDE
            else:
                # 区域内其他标签按 reasoning 原文输出（不支持嵌套）
                deltas.append(ChatTextDelta(TextDeltaKind.REASONING, tag_text))
        else:  # EXPLICIT_RESPONSE
            if tag == self._policy.response_end:
                self._state = _Region.OUTSIDE
            else:
                # 区域内其他标签按 response 原文输出（不支持嵌套）
                deltas.append(ChatTextDelta(TextDeltaKind.RESPONSE, tag_text))

    def _find_earliest_tag(self, buffer: str) -> tuple[int, int, str] | None:
        """在 buffer 中查找最早的完整标签（四类标签均参与识别）。

        返回 (位置, 长度, 标签文本)。同一位置出现多个标签时，优先匹配较长者
        （处理前缀重叠）。
        """
        tags = (
            self._policy.reasoning_start,
            self._policy.reasoning_end,
            self._policy.response_start,
            self._policy.response_end,
        )

        candidates: list[tuple[int, int, str]] = []
        for tag in tags:
            if not tag:
                continue
            pos = buffer.find(tag)
            if pos != -1:
                candidates.append((pos, len(tag), tag))

        if not candidates:
            return None

        # 最早位置优先；同位置时优先较长标签
        return min(candidates, key=lambda c: (c[0], -c[1]))

    def _safe_output_length(self, buffer: str) -> int:
        """返回可安全输出的长度，保留可能是部分标签的后缀。

        四类标签均可作为“部分前缀”被保留：例如缓冲区以 `` 的前缀 `` 结尾时，
        保留该前缀等待下次 feed() 补全。
        """
        if not buffer:
            return 0

        tags = (
            self._policy.reasoning_start,
            self._policy.reasoning_end,
            self._policy.response_start,
            self._policy.response_end,
        )

        max_prefix_len = 0

        for tag in tags:
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
