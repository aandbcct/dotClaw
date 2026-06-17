"""消息工具函数

纯函数集合，无内部状态。函数签名均为 (list[Message], ...) -> list[Message] | list[str]。
链式调用友好：validate(messages) → trim(messages) → clean(messages)

P3 用中英文差异化公式估算 token 数。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..llm.base import Message

logger = logging.getLogger("dotclaw.agent.message_utils")


# ---- Token 估算（中英文差异化公式） ----

def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数。中文 ~1 char/token，其他 ~4 chars/token。"""
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    return chinese_chars + (other_chars // 4)


def _msg_tokens(msg: "Message") -> int:
    """估算单条消息的 token 数"""
    tokens = _estimate_tokens(msg.content)
    if msg.tool_calls:
        for tc in msg.tool_calls:
            tokens += _estimate_tokens(tc.name) + _estimate_tokens(tc.arguments)
    return tokens


# ---- validate ----

def validate(messages: list["Message"]) -> list[str]:
    """
    验证消息列表合法性，返回问题描述列表（空列表 = 合法）。

    检查项：
    - tool_use/tool_result 配对完整性
    - 角色顺序合法性
    - 无连续相同角色消息（user 除外）
    """
    issues: list[str] = []

    # 收集 assistant 中声明的 tool_call_id
    declared_ids: set[str] = set()
    # 收集 tool 消息提供的 tool_call_id
    provided_ids: set[str] = set()

    for i, msg in enumerate(messages):
        # 收集 tool_call IDs
        if msg.role == "assistant" and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.id:
                    declared_ids.add(tc.id)

        if msg.role == "tool" and msg.tool_call_id:
            provided_ids.add(msg.tool_call_id)

        # 无连续相同角色（user 除外，多轮 user-user 可能被 merge）
        if i > 0:
            prev = messages[i - 1]
            if msg.role == prev.role and msg.role not in ("user",):
                issues.append(f"连续相同角色 '{msg.role}' at index {i}")

    # 孤立的 tool_call_id（声明了但无 tool 响应）
    orphan_declared = declared_ids - provided_ids
    if orphan_declared:
        issues.append(f"孤立的 tool_call_id（声明但未响应）: {orphan_declared}")

    # 孤立的 tool 响应（有 tool 结果但无对应 assistant 声明）
    orphan_provided = provided_ids - declared_ids
    if orphan_provided:
        issues.append(f"孤立的 tool 响应（无对应 assistant 声明）: {orphan_provided}")

    return issues


# ---- trim ----

def trim(messages: list["Message"], max_tokens: int) -> list["Message"]:
    """
    按 token 预算从旧到新移除消息组。

    保护规则：
    - system 消息始终不可裁
    - (assistant(tool_calls), tool_1, tool_2, ...) 作为一个整体，要么全留、要么全裁
    - 即使最后（最新）一组超出预算也完整保留（不可拆散）
    """
    if not messages:
        return []

    groups = _build_groups(messages)

    # system 组始终保留
    system_groups = [g for g in groups if g[0].role == "system"]
    body_groups = [g for g in groups if g[0].role != "system"]

    system_tokens = sum(_group_tokens(g) for g in system_groups)
    budget = max_tokens - system_tokens

    if budget <= 0:
        logger.warning("system 消息已超出 token 预算 (%d > %d)", system_tokens, max_tokens)
        return _flatten(system_groups + body_groups)

    # 从后向前，累计可以塞进 budget 的组
    total = 0
    keep_from = len(body_groups)
    for i in range(len(body_groups) - 1, -1, -1):
        t = _group_tokens(body_groups[i])
        if total + t <= budget:
            total += t
            keep_from = i
        else:
            break

    # 极端情况：最后一组就超出预算 → 保留它（不可拆散）
    if keep_from == len(body_groups) and body_groups:
        keep_from = len(body_groups) - 1

    return _flatten(system_groups + body_groups[keep_from:])


def _build_groups(messages: list["Message"]) -> list[list["Message"]]:
    """将消息列表划分为不可拆散的组。

    system 消息：每一条单独成组。
    assistant+tool 配对：assistant(tool_calls) + 后续匹配的 tool 消息形成一个组。
    其他消息：每条单独成组。
    """
    groups: list[list["Message"]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.role == "system":
            groups.append([msg])
            i += 1
        elif msg.role == "assistant" and msg.tool_calls:
            call_ids = {tc.id for tc in msg.tool_calls if tc.id}
            group = [msg]
            i += 1
            # 收集后续匹配的 tool 消息
            matched = 0
            while i < len(messages) and matched < len(call_ids):
                nxt = messages[i]
                if nxt.role == "tool" and nxt.tool_call_id in call_ids:
                    group.append(nxt)
                    matched += 1
                i += 1
            groups.append(group)
        else:
            groups.append([msg])
            i += 1
    return groups


def _group_tokens(group: list["Message"]) -> int:
    """计算一组消息的总 token 数"""
    return sum(_msg_tokens(m) for m in group)


def _flatten(groups: list[list["Message"]]) -> list["Message"]:
    """将消息组展平为单层列表"""
    result: list["Message"] = []
    for g in groups:
        result.extend(g)
    return result


# ---- clean ----

def clean(messages: list["Message"]) -> list["Message"]:
    """
    清理消息列表：
    - 去除空 content 消息（tool 消息除外，空 tool 结果有意义）
    - 去除连续 system 消息（保留首条）
    - 修复孤立的 tool 消息（无对应 assistant 声明则移除）
    """
    result: list["Message"] = []

    # 收集所有 assistant 声明的 tool_call_id
    declared = set()
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                if tc.id:
                    declared.add(tc.id)

    for i, msg in enumerate(messages):
        # 去除空 content（tool 消息除外）
        if not msg.content and msg.role != "tool":
            continue

        # 修复孤立 tool 消息
        if msg.role == "tool" and msg.tool_call_id:
            if msg.tool_call_id not in declared:
                logger.debug(f"移除孤立 tool 消息 (tool_call_id={msg.tool_call_id})")
                continue

        # 连续 system 消息去重
        if msg.role == "system" and result and result[-1].role == "system":
            logger.debug("跳过连续 system 消息")
            continue

        result.append(msg)

    return result
