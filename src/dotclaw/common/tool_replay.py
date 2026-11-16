"""工具恢复策略、副作用与执行证据的中立契约。"""

from __future__ import annotations

from enum import StrEnum


class ToolReplayPolicy(StrEnum):
    """工具调用在中断后允许采用的恢复策略。"""

    SAFE_RETRY = "safe_retry"
    KEYED_IDEMPOTENT = "keyed_idempotent"
    NO_AUTO_RETRY = "no_auto_retry"


class ToolSideEffect(StrEnum):
    """工具对业务或外部环境产生副作用的声明。"""

    NONE = "none"
    PRESENT = "present"
    UNKNOWN = "unknown"


class ToolExecutionEvidence(StrEnum):
    """工具执行后对外部效果的证据强度。"""

    NOT_STARTED = "not_started"
    RETURNED = "returned"
    UNKNOWN = "unknown"
