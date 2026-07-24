"""ReasoningStreamParser 标签流解析器测试（开发计划阶段二）。

解析器测试只依赖 LLM 公共 DTO（ChatTextDelta、TextDeltaKind），不模拟 Provider SDK。
覆盖：完整标签、跨 chunk 标签、同包多段、未闭合、异常结束标签、嵌套开始标签。
"""

from __future__ import annotations

from dotclaw.llm.base import TextDeltaKind
from dotclaw.llm.reasoning import (
    ReasoningMode,
    ReasoningPolicy,
    ReasoningStreamParser,
)


def _join(deltas) -> list[tuple[str, str]]:
    """把 delta 列表拍平为 (kind, content) 对，便于断言。"""
    return [(d.kind.value, d.content) for d in deltas]


def _policy(mode: ReasoningMode = ReasoningMode.TAGS) -> ReasoningPolicy:
    return ReasoningPolicy(mode=mode)


# ── 配置与策略 ─────────────────────────────────────────────

def test_policy_from_mode_none_keeps_defaults() -> None:
    """none 模式策略只保留 mode，标签使用标准默认。"""
    policy = ReasoningPolicy(mode=ReasoningMode.NONE)
    assert policy.mode is ReasoningMode.NONE
    assert policy.reasoning_start == "<think>"
    assert policy.reasoning_end == "</think>"


def test_policy_from_mode_tags_keeps_default_tags() -> None:
    """tags 模式策略保留标准默认标签（未显式覆盖时）。"""
    policy = ReasoningPolicy(mode=ReasoningMode.TAGS)
    assert policy.mode is ReasoningMode.TAGS
    assert policy.reasoning_start == "<think>"
    assert policy.reasoning_end == "</think>"


# ── 完整标签（一个 feed 包内） ─────────────────────────────

def test_complete_tags_in_one_chunk() -> None:
    """完整 <think>...</think> 在一次 feed 内：先 reasoning 后 response。"""
    parser = ReasoningStreamParser(_policy())
    deltas = parser.feed("<think>让我想想</think>结论")
    assert _join(deltas) == [
        (TextDeltaKind.REASONING.value, "让我想想"),
        (TextDeltaKind.RESPONSE.value, "结论"),
    ]


def test_tags_only_yield_no_text() -> None:
    """仅有标签、无正文时不产生任何 delta。"""
    parser = ReasoningStreamParser(_policy())
    assert parser.feed("<think></think>") == []
    assert parser.flush() == []


# ── 同包多段 ───────────────────────────────────────────────

def test_same_packet_multi_segment() -> None:
    """同包内多段：response、reasoning、response 交替。"""
    parser = ReasoningStreamParser(_policy())
    deltas = parser.feed("前置<think>思考</think>中间<think>再想</think>结尾")
    assert _join(deltas) == [
        (TextDeltaKind.RESPONSE.value, "前置"),
        (TextDeltaKind.REASONING.value, "思考"),
        (TextDeltaKind.RESPONSE.value, "中间"),
        (TextDeltaKind.REASONING.value, "再想"),
        (TextDeltaKind.RESPONSE.value, "结尾"),
    ]


# ── 跨 chunk 标签 ──────────────────────────────────────────

def test_cross_chunk_tag_split() -> None:
    """标签被拆分到两次 feed，解析器跨调用缓冲不丢文本。"""
    parser = ReasoningStreamParser(_policy())
    first = parser.feed("先说点<")                    # `` 被拆为 "<" + "think>"
    second = parser.feed("think>思考</think>回答")  # 补全 `` + 正文
    # 第一段仅安全输出前缀前的文本（"<" 作为潜在标签前缀保留）
    assert _join(first) == [(TextDeltaKind.RESPONSE.value, "先说点")]
    assert _join(second) == [
        (TextDeltaKind.REASONING.value, "思考"),
        (TextDeltaKind.RESPONSE.value, "回答"),
    ]


def test_cross_chunk_tag_three_chunks() -> None:
    """标签跨三个 feed 拆分，缓冲区逐步累积，无输出直到标签完整。"""
    parser = ReasoningStreamParser(_policy())
    a = parser.feed("先说点<")                 # 缓冲区: "<"
    b = parser.feed("thi")                    # 缓冲区: "<thi"
    c = parser.feed("nk>思考</think>回答")   # 缓冲区补全为完整标签
    assert _join(a) == [(TextDeltaKind.RESPONSE.value, "先说点")]
    assert _join(b) == []                     # 仅部分标签，无输出
    assert _join(c) == [
        (TextDeltaKind.REASONING.value, "思考"),
        (TextDeltaKind.RESPONSE.value, "回答"),
    ]


def test_cross_chunk_reasoning_closed_in_next_chunk() -> None:
    """reasoning 区跨两次 feed：第二次出现 </think> 才切回 response。"""
    parser = ReasoningStreamParser(_policy())
    first = parser.feed("<think>长思考")
    second = parser.feed("继续</think>回答")
    assert _join(first) == [(TextDeltaKind.REASONING.value, "长思考")]
    assert _join(second) == [
        (TextDeltaKind.REASONING.value, "继续"),
        (TextDeltaKind.RESPONSE.value, "回答"),
    ]


# ── 未闭合 ─────────────────────────────────────────────────

def test_unclosed_reasoning_text_emitted_as_reasoning() -> None:
    """未闭合 </think> 缺失：正文在 feed 阶段按 reasoning 输出。"""
    parser = ReasoningStreamParser(_policy())
    fed = parser.feed("<think>还没说完")
    assert _join(fed) == [(TextDeltaKind.REASONING.value, "还没说完")]
    assert parser.flush() == []  # 缓冲区已排空，不再重复输出


def test_unclosed_partial_tag_flushed_as_reasoning() -> None:
    """流结束时缓冲区残留部分标签前缀：flush 按 reasoning 输出（不补全为标签）。"""
    parser = ReasoningStreamParser(_policy())
    fed = parser.feed("<think>思考<thin")  # "<thin" 是 `` 的前缀，feed 阶段不输出
    flushed = parser.flush()
    assert _join(fed) == [(TextDeltaKind.REASONING.value, "思考")]
    assert _join(flushed) == [(TextDeltaKind.REASONING.value, "<thin")]


# ── 异常结束标签 ───────────────────────────────────────────

def test_unmatched_end_tag_emitted_as_response() -> None:
    """response 区出现未匹配 </think>：原样作为 response 输出。"""
    parser = ReasoningStreamParser(_policy())
    deltas = parser.feed("回答内容</think>后续")
    assert _join(deltas) == [
        (TextDeltaKind.RESPONSE.value, "回答内容"),
        (TextDeltaKind.RESPONSE.value, "</think>"),
        (TextDeltaKind.RESPONSE.value, "后续"),
    ]


# ── 嵌套开始标签 ───────────────────────────────────────────

def test_nested_start_tag_emitted_as_reasoning() -> None:
    """reasoning 区内嵌套 <think>：原样作为 reasoning 输出，不切换状态。"""
    parser = ReasoningStreamParser(_policy())
    deltas = parser.feed("<think>外层<think>内层</think>收尾")
    assert _join(deltas) == [
        (TextDeltaKind.REASONING.value, "外层"),
        (TextDeltaKind.REASONING.value, "<think>"),
        (TextDeltaKind.REASONING.value, "内层"),
        (TextDeltaKind.RESPONSE.value, "收尾"),
    ]


# ── 边界 ───────────────────────────────────────────────────

def test_empty_feed_and_flush() -> None:
    """空 feed 与空 flush 均不产生 delta。"""
    parser = ReasoningStreamParser(_policy())
    assert parser.feed("") == []
    assert parser.flush() == []


def test_plain_text_all_response() -> None:
    """无标签的纯文本全部归为 response。"""
    parser = ReasoningStreamParser(_policy())
    deltas = parser.feed("普通回答")
    assert _join(deltas) == [(TextDeltaKind.RESPONSE.value, "普通回答")]


# ── response 标签生效（阶段二修订） ─────────────────────────

def test_response_tag_strips_protocol_label() -> None:
    """<response> 仅剥离协议标签，正文仍归 response（不创建新语义）。"""
    parser = ReasoningStreamParser(_policy())
    deltas = parser.feed("前<response>中</response>后")
    assert _join(deltas) == [
        (TextDeltaKind.RESPONSE.value, "前"),
        (TextDeltaKind.RESPONSE.value, "中"),
        (TextDeltaKind.RESPONSE.value, "后"),
    ]


def test_response_region_then_reasoning() -> None:
    """<response> 区结束后再进入 <think> 区，状态正确切换。"""
    parser = ReasoningStreamParser(_policy())
    deltas = parser.feed("<response>答</response><think>想</think>回")
    assert _join(deltas) == [
        (TextDeltaKind.RESPONSE.value, "答"),
        (TextDeltaKind.REASONING.value, "想"),
        (TextDeltaKind.RESPONSE.value, "回"),
    ]


def test_think_inside_response_is_literal() -> None:
    """显式 response 区内出现 <think>：作为 response 原文保留（不支持嵌套）。"""
    parser = ReasoningStreamParser(_policy())
    deltas = parser.feed("<response>答<think>假想</response>后")
    assert _join(deltas) == [
        (TextDeltaKind.RESPONSE.value, "答"),
        (TextDeltaKind.RESPONSE.value, "<think>"),
        (TextDeltaKind.RESPONSE.value, "假想"),
        (TextDeltaKind.RESPONSE.value, "后"),
    ]


def test_response_inside_think_is_literal() -> None:
    """reasoning 区内出现的 <response>/</response>：作为 reasoning 原文保留。"""
    parser = ReasoningStreamParser(_policy())
    deltas = parser.feed("<think>想<response>假应答</response>继续")
    assert _join(deltas) == [
        (TextDeltaKind.REASONING.value, "想"),
        (TextDeltaKind.REASONING.value, "<response>"),
        (TextDeltaKind.REASONING.value, "假应答"),
        (TextDeltaKind.REASONING.value, "</response>"),
        (TextDeltaKind.REASONING.value, "继续"),
    ]


def test_cross_chunk_response_tag() -> None:
    """<response> 标签被拆到两次 feed，解析器跨调用缓冲不丢文本。"""
    parser = ReasoningStreamParser(_policy())
    first = parser.feed("a<res")                 # `` 被拆为 "<res" 前缀
    second = parser.feed("ponse>中</response>后")  # 补全 `` + 正文
    assert _join(first) == [(TextDeltaKind.RESPONSE.value, "a")]
    assert _join(second) == [
        (TextDeltaKind.RESPONSE.value, "中"),
        (TextDeltaKind.RESPONSE.value, "后"),
    ]


def test_unclosed_response_tag() -> None:
    """未闭合的 <response>：正文在 feed 阶段按 response 输出。"""
    parser = ReasoningStreamParser(_policy())
    fed = parser.feed("x<response>未闭合")
    assert _join(fed) == [
        (TextDeltaKind.RESPONSE.value, "x"),
        (TextDeltaKind.RESPONSE.value, "未闭合"),
    ]
    assert parser.flush() == []  # 缓冲区已排空


def test_unclosed_response_partial_tag_flushed_as_response() -> None:
    """流结束时缓冲区残留 <response> 的部分前缀：flush 按 response 输出。"""
    parser = ReasoningStreamParser(_policy())
    fed = parser.feed("<response>答<res")  # "<res" 是 `` 的前缀，feed 阶段不输出
    flushed = parser.flush()
    assert _join(fed) == [(TextDeltaKind.RESPONSE.value, "答")]
    assert _join(flushed) == [(TextDeltaKind.RESPONSE.value, "<res")]


def test_unmatched_response_end_as_response() -> None:
    """OUTSIDE 区出现未匹配 </response>：原样作为 response 输出。"""
    parser = ReasoningStreamParser(_policy())
    deltas = parser.feed("a</response>b")
    assert _join(deltas) == [
        (TextDeltaKind.RESPONSE.value, "a"),
        (TextDeltaKind.RESPONSE.value, "</response>"),
        (TextDeltaKind.RESPONSE.value, "b"),
    ]


def test_custom_response_tags() -> None:
    """自定义 response 标签同样被识别与剥离。"""
    policy = ReasoningPolicy(
        mode=ReasoningMode.TAGS,
        response_start="[[r]]",
        response_end="[[/r]]",
    )
    parser = ReasoningStreamParser(policy)
    deltas = parser.feed("前[[r]]中[[/r]]后")
    assert _join(deltas) == [
        (TextDeltaKind.RESPONSE.value, "前"),
        (TextDeltaKind.RESPONSE.value, "中"),
        (TextDeltaKind.RESPONSE.value, "后"),
    ]
