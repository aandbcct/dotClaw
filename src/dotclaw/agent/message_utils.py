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
    按 token 预算裁剪消息列表（从旧到新逐条移除）。

    保护规则：
    - system 消息始终不可裁
    - (assistant(tool_calls), tool_1, tool_2, ...) 配对组不可拆散
    - 配对组要么全保留，要么全裁
    - 极端情况（单条 > max_tokens）：记录 warning，保留该消息
    """
    if not messages:
        return []

    # 分离 system 消息
    system_msgs = [m for m in messages if m.role == "system"]
    body_msgs = [m for m in messages if m.role != "system"]

    # 计算 system 的 token 数
    system_tokens = sum(_msg_tokens(m) for m in system_msgs)
    budget = max_tokens - system_tokens

    if budget <= 0:
        logger.warning(f"system 消息已超出 token 预算 ({system_tokens} > {max_tokens})")
        return system_msgs + body_msgs  # 保留全部

    # 构建配对组索引
    pairing = _build_pairing_groups(body_msgs)

    # 从后向前累计 token，找到裁剪边界
    total = system_tokens
    keep_from = len(body_msgs)  # 默认保留到最前面

    for i in range(len(body_msgs) - 1, -1, -1):
        group = pairing.get(i)
        if group is not None:
            # 当前消息属于一个配对组，跳过整个组
            # 配对组已经在配对构建时标记了范围
            continue

        t = _msg_tokens(body_msgs[i])
        if total + t <= max_tokens:
            total += t
            keep_from = i
        else:
            break

    # 配对组处理：如果 keep_from 落在配对组内部，扩展到组开始
    for i in range(keep_from, len(body_msgs)):
        group = pairing.get(i)
        if group is not None and group["start"] < keep_from:
            keep_from = group["start"]
            break

    # 极端情况：单条消息超过预算
    if keep_from == len(body_msgs) and body_msgs:
        logger.warning(
            f"单条消息 token 超出预算 ({_msg_tokens(body_msgs[-1])} > {budget})，保留该消息"
        )
        keep_from = len(body_msgs) - 1

    return system_msgs + body_msgs[keep_from:]


def _build_pairing_groups(messages: list["Message"]) -> dict[int, dict]:
    """
    构建配对组索引。

    返回：{msg_index: {"start": start_idx, "end": end_idx}}
    配对组 = assistant(tool_calls=[id_a,id_b]) + 所有 tool_call_id in {id_a,id_b} 的 tool 消息
    """
    pairing: dict[int, dict] = {}

    for i, msg in enumerate(messages):
        if msg.role == "assistant" and msg.tool_calls:
            call_ids = {tc.id for tc in msg.tool_calls if tc.id}
            if not call_ids:
                continue

            # 找到所有匹配的 tool 消息
            matched_indices = [i]
            for j in range(i + 1, len(messages)):
                if messages[j].role == "tool" and messages[j].tool_call_id in call_ids:
                    matched_indices.append(j)
                    # 消息已匹配则停止（tool 消息只属于一个配对组）
                    if len(matched_indices) - 1 >= len(call_ids):
                        break

            start = matched_indices[0]
            end = matched_indices[-1]
            for idx in matched_indices:
                pairing[idx] = {"start": start, "end": end}

    return pairing


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
