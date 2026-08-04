"""Draft 候选 Case 的可序列化载荷脱敏。

脱敏只作用于 ``EvalCase`` 的 JSON 可序列化副本，不接触 Runtime，也不读取文件：

* **字段名命中**（``token`` / ``api_key`` / ``password`` / ``authorization`` /
  ``cookie`` / ``secret``）：确定性地把整个值替换为固定标记，安全，**不**触发人工复核。
* **已知凭证模式**（Bearer Token、私钥块、常见 API Key 格式）：对自由文本做启发式
  替换，并置 ``requires_review=True`` 交由人工确认——启发式无法保证全覆盖，故需复核。
* 普通 LLM 回复不含上述敏感模式时既不脱敏也不触发复核；含敏感模式时同样经过本脱敏器。

本模块不声称能识别任意自然语言敏感信息，也不猜测如何生成替代 Fixture。
"""

from __future__ import annotations

import re
from typing import Any

from .draft import EvalCaseDraft
from .models import EvalCase

REDACTED_MARKER: str = "[redacted]"
"""脱敏后的固定替换标记。"""

_SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "token",
        "api_key",
        "password",
        "authorization",
        "cookie",
        "secret",
    }
)

# 已知凭证模式：命中即视为需要人工复核（启发式，无法保证全覆盖）。
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"),
    re.compile(r"\bghp_[0-9A-Za-z]{36}\b"),
)


def _redact_string(text: str) -> tuple[str, bool]:
    """对字符串逐模式脱敏，返回（脱敏后文本, 是否触发启发式）。"""
    result: str = text
    hit: bool = False
    for pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(result):
            result = pattern.sub(REDACTED_MARKER, result)
            hit = True
    return result, hit


def _redact_node(node: Any) -> tuple[Any, bool, bool]:
    """递归脱敏；返回（脱敏后节点, 是否发生脱敏, 是否触发需人工复核）。"""
    redacted: bool = False
    unsafe: bool = False
    if isinstance(node, dict):
        new_dict: dict[str, Any] = {}
        for key, value in node.items():
            if isinstance(key, str) and key.lower() in _SENSITIVE_FIELD_NAMES:
                # 字段名命中：确定性替换整个值，安全不触发复核。
                new_dict[key] = REDACTED_MARKER
                redacted = True
            else:
                child, child_redacted, child_unsafe = _redact_node(value)
                new_dict[key] = child
                redacted = redacted or child_redacted
                unsafe = unsafe or child_unsafe
        return new_dict, redacted, unsafe
    if isinstance(node, (list, tuple)):
        items: list[Any] = []
        for item in node:
            child, child_redacted, child_unsafe = _redact_node(item)
            items.append(child)
            redacted = redacted or child_redacted
            unsafe = unsafe or child_unsafe
        return (items if isinstance(node, list) else tuple(items)), redacted, unsafe
    if isinstance(node, str):
        new_text, hit = _redact_string(node)
        if hit:
            # 自由文本启发式脱敏：无法保证全覆盖，置需复核。
            return new_text, True, True
        return node, False, False
    return node, False, False


def redact_draft(draft: EvalCaseDraft) -> EvalCaseDraft:
    """对草案候选 Case 载荷递归脱敏，返回脱敏后的新草案。

    字段名命中的敏感值确定性替换（安全，不触发复核）；自由文本中命中已知凭证
    模式时做启发式替换并置 ``requires_review=True``，交由人工确认。普通 LLM 回复
    不含敏感模式时不脱敏也不触发复核。
    """
    payload: dict[str, Any]
    payload, _redacted, unsafe = _redact_node(draft.case.to_dict())
    redacted_case: EvalCase = EvalCase.from_dict(payload)
    return EvalCaseDraft(
        draft_id=draft.draft_id,
        source_run_id=draft.source_run_id,
        source_record_hash=draft.source_record_hash,
        source_trace_schema_version=draft.source_trace_schema_version,
        case=redacted_case,
        requires_review=bool(draft.requires_review) or unsafe,
        confirmed_case_id=draft.confirmed_case_id,
    )
