"""纯脱敏常量与凭证模式——Trace 与 Eval 共用，不依赖任何业务包。

本模块仅包含静态数据，可被 ``dotclaw.trace``、``dotclaw.eval`` 引用，
不会引入循环导入或反向依赖。
"""

from __future__ import annotations

import re

from .models import CONTENT_REDACTED_MARKER

# 保持与 CONTENT_REDACTED_MARKER 一致，但本模块也直接导出以方便 import
REDACTED_MARKER: str = CONTENT_REDACTED_MARKER

SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset(
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
CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"),
    re.compile(r"\bghp_[0-9A-Za-z]{36}\b"),
)
