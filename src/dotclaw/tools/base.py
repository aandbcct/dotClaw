"""工具基础类型定义（Phase 5 重构）"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("dotclaw.tools")


class ToolSource(str, Enum):
    """工具来源"""
    BUILTIN = "builtin"
    MCP = "mcp"
    SKILL = "skill"
    CUSTOM = "custom"


@dataclass
class ToolDefinition:
    """工具定义（增强版 — Phase 5）"""
    name: str
    description: str
    parameters: dict = field(default_factory=dict)   # JSON Schema
    source: ToolSource = ToolSource.BUILTIN
    needs_approval: bool = False                    # 声明式审批需求
    timeout: float = 60.0                          # 执行超时（秒）
    metadata: dict = field(default_factory=dict)     # 扩展字段


@dataclass
class ToolResult:
    """工具执行结果（结构化扩展 — Phase 5）"""
    output: str = ""
    is_error: bool = False
    error_code: str | None = None      # 如 TIMEOUT / PROCESS_ERROR / HTTP_ERROR
    error_type: str | None = None      # 如 timeout / execution / parsing
    metadata: dict = field(default_factory=dict)  # 扩展字段


@dataclass
class ToolExecutionContext:
    """工具执行时的运行时上下文。

    Runtime 在每次工具调用时创建此上下文并传给 handler。
    Task delegation 工具（delegate/task_send_message/wait_task/task_status/cancel_task）
    从此上下文解析当前 Agent、Runtime 和 agentrun_id，
    不再通过工厂闭包捕获顶层 Agent。
    """

    timeout: float = 60.0
    """执行超时，来自 ToolDefinition.timeout"""

    agent: object | None = None
    """当前执行的 Agent 实例"""

    runtime: object | None = None
    """当前 Runtime 实例"""

    session_id: str = ""
    """当前 Session ID"""

    agentrun_id: str = ""
    """当前 AgentRun ID（父 AgentRun 的 run_id）"""

    task_id: str = ""
    """Harness 注入的当前 Task ID；仅 target delegation Runtime 使用。"""

    channel: object | None = None
    """当前通信 Channel"""
