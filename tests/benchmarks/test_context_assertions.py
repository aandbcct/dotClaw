"""PR6 Context 断言单元测试。"""

import pytest

from benchmarks.context_assertions import assert_comparable_controls, tool_pair_break_count


def test_tool_pairs_must_remain_complete() -> None:
    """完整工具调用边界不计破坏，孤立结果必须计入。"""
    assert tool_pair_break_count(({"kind": "tool_call", "call_id": "a"}, {"kind": "tool_result", "call_id": "a"})) == 0
    assert tool_pair_break_count(({"kind": "tool_result", "call_id": "a"},)) == 1


def test_controls_reject_incomparable_measurements() -> None:
    """不同延迟的反事实对照不得计算性能变化。"""
    base = {"input_hash": "x", "provider_delay_ms": 1, "tokenizer": "cl100k_base", "budget_window": 1, "timing_scope": "resume"}
    with pytest.raises(ValueError, match="provider_delay_ms"):
        assert_comparable_controls(base, {**base, "provider_delay_ms": 2})
