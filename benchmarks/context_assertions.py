"""PR6 ContextVersion（版本化上下文）与污染边界断言。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence

from dotclaw.runtime.domain.context import ContextOwner, ContextVersion


def normalized_slot_hashes(version: ContextVersion) -> dict[str, str]:
    """按注入顺序计算 Slot 的稳定内容哈希，刻意不包含 created_at。"""
    return {
        slot.slot_id: hashlib.sha256(
            json.dumps(slot.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        for slot in sorted(version.slots, key=lambda item: item.injection_order)
    }


def assert_context_versions_equal(expected: ContextVersion, actual: ContextVersion) -> None:
    """比较真正决定输入的结构事实，排除版本号和创建时间。"""
    if expected.content_hash != actual.content_hash:
        raise AssertionError("ContextVersion content_hash 漂移")
    if expected.tool_schema_hash != actual.tool_schema_hash:
        raise AssertionError("ContextVersion tool_schema_hash 漂移")
    if normalized_slot_hashes(expected) != normalized_slot_hashes(actual):
        raise AssertionError("ContextVersion Slot 内容或顺序漂移")


def owner_leak_counts(
    visible_text: str,
    allowed_owner: ContextOwner,
    identifiers: Mapping[ContextOwner, str],
) -> dict[str, int]:
    """统计非当前 Owner 标识泄漏数；允许标识不计为泄漏。"""
    return {
        owner.value: int(owner is not allowed_owner and marker in visible_text)
        for owner, marker in identifiers.items()
    }


def tool_pair_break_count(messages: Sequence[Mapping[str, str]]) -> int:
    """检测 Tool Call（工具调用）与 Tool Result（工具结果）是否被压缩边界拆开。"""
    pending: set[str] = set()
    broken: int = 0
    for message in messages:
        kind = message.get("kind")
        call_id = message.get("call_id", "")
        if kind == "tool_call":
            pending.add(call_id)
        elif kind == "tool_result":
            if call_id not in pending:
                broken += 1
            pending.discard(call_id)
    return broken + len(pending)


def assert_comparable_controls(left: Mapping[str, object], right: Mapping[str, object]) -> None:
    """拒绝输入、延迟、窗口或计时边界不一致的反事实比较。"""
    keys: Iterable[str] = ("input_hash", "provider_delay_ms", "tokenizer", "budget_window", "timing_scope")
    mismatched = [key for key in keys if left.get(key) != right.get(key)]
    if mismatched:
        raise ValueError(f"强制重建对照不可比：{', '.join(mismatched)} 不一致")
