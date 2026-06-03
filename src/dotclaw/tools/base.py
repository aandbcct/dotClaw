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
    """工具执行时的运行时上下文（最小集 — Phase 5）"""
    timeout: float = 60.0              # 执行超时，来自 ToolDefinition.timeout
