"""工具基础类型定义（Tool v1 阶段一扩展）。

本模块只声明类型与契约，不涉及注册、校验或执行。所有新增注释使用中文。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("dotclaw.tools")


class ToolSource(str, Enum):
    """工具来源。"""

    BUILTIN = "builtin"
    MCP = "mcp"
    SKILL = "skill"
    CUSTOM = "custom"


class ToolErrorCode(str, Enum):
    """统一工具错误码（总体设计 §4.5）。

    所有工具的失败结果都应映射到此有限集合，便于审计与上游统一处理。
    """

    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    POLICY_DENIED = "POLICY_DENIED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    MCP_UNAVAILABLE = "MCP_UNAVAILABLE"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    EXECUTOR_ERROR = "EXECUTOR_ERROR"


class ToolErrorType(str, Enum):
    """错误类别，与 ToolErrorCode 配套，用于审计归类与脱敏策略。"""

    VALIDATION = "validation"
    POLICY = "policy"
    APPROVAL = "approval"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    EXECUTION = "execution"
    EXECUTOR = "executor"
    MCP_UNAVAILABLE = "mcp_unavailable"


@dataclass
class ToolDefinition:
    """面向 LLM 的工具定义（稳定契约）。

    字段在迁移期间保持稳定：新增字段均带默认值，旧构造方式保持兼容。
    """

    name: str
    description: str
    parameters: dict = field(default_factory=dict)   # JSON Schema（来自 args_model 或手写）
    source: ToolSource = ToolSource.BUILTIN
    needs_approval: bool = False                      # 声明式审批需求
    timeout: float = 60.0                             # 执行超时（秒）
    metadata: dict = field(default_factory=dict)      # 扩展字段
    policy_profile: str | None = None                 # ToolPolicy 档案值，Policy 阶段使用


@dataclass
class ToolResult:
    """工具执行结果（结构化契约）。

    output 在兼容期继续保留，供现有调用方读取；内部已为内容块表达预留空间。
    """

    output: str = ""
    is_error: bool = False
    error_code: str | None = None      # ToolErrorCode 的值
    error_type: str | None = None      # ToolErrorType 的值
    metadata: dict = field(default_factory=dict)  # 扩展字段

    @classmethod
    def from_error(
        cls,
        *,
        code: ToolErrorCode,
        message: str,
        error_type: ToolErrorType | None = None,
    ) -> "ToolResult":
        """由标准错误码构造统一的错误结果，避免调用方重复指定类别。"""
        if error_type is None:
            error_type = _default_error_type(code)
        return cls(
            output=message,
            is_error=True,
            error_code=code.value,
            error_type=error_type.value,
        )


def _default_error_type(code: ToolErrorCode) -> ToolErrorType:
    """为错误码提供缺省类别映射。"""
    return {
        ToolErrorCode.INVALID_ARGUMENTS: ToolErrorType.VALIDATION,
        ToolErrorCode.POLICY_DENIED: ToolErrorType.POLICY,
        ToolErrorCode.APPROVAL_DENIED: ToolErrorType.APPROVAL,
        ToolErrorCode.TOOL_NOT_FOUND: ToolErrorType.NOT_FOUND,
        ToolErrorCode.TIMEOUT: ToolErrorType.TIMEOUT,
        ToolErrorCode.MCP_UNAVAILABLE: ToolErrorType.MCP_UNAVAILABLE,
        ToolErrorCode.EXECUTION_ERROR: ToolErrorType.EXECUTION,
        ToolErrorCode.EXECUTOR_ERROR: ToolErrorType.EXECUTOR,
    }[code]


@dataclass
class ToolExecutionContext:
    """工具执行时的最小运行上下文（稳定字段）。

    由 Runtime 在每次调用时构造并注入；字段在阶段一固定，后续阶段如需扩展
    只读能力（如 workspace 根目录）再追加。
    """

    timeout: float = 60.0
    """执行超时，来自 ToolDefinition.timeout。"""

    agentrun_id: str = ""
    """当前 Run 标识，用于工具审计与审批关联。"""

    session_id: str = ""
    """会话标识，用于审计归类。"""

    agent_id: str = ""
    """Agent 标识，用于策略收窄与审计。"""


# ToolContext 在模块末尾绑定为 ToolExecutionContext 的别名，供函数签名使用。
ToolContext = ToolExecutionContext  # type: ignore[assignment]
