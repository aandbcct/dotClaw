"""工具执行调度器（Phase 5 新增）— 审批 + 超时 + 错误处理"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import ToolExecutionContext, ToolResult
from .handler import ToolHandler
from .registry import ToolRegistry
from .approval import ApprovalManager

logger = logging.getLogger("dotclaw.tools.executor")


class ToolExecutor:
    """工具执行调度器 — 审批 + 超时 + 错误处理"""

    def __init__(
        self,
        registry: ToolRegistry,
        approval_manager: ApprovalManager | None = None,
    ):
        self._registry = registry
        self._approval = approval_manager or ApprovalManager()

    def get_definitions(self) -> list:
        """返回所有工具定义（转发给 Registry）。"""
        return self._registry.get_definitions()

    def get_handler(self, name: str) -> ToolHandler | None:
        """按名称获取 Handler（转发给 Registry）。"""
        return self._registry.get(name)

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        channel: Any | None = None,
        journal: Any | None = None,
    ) -> ToolResult:
        """执行工具：查找 Handler → 审批检查 → 超时控制 → 返回 ToolResult"""
        handler = self._registry.get(name)
        if not handler:
            return ToolResult(
                output=f"错误：未找到工具 '{name}'",
                is_error=True,
                error_code="TOOL_NOT_FOUND",
                error_type="not_found",
            )

        definition = handler.definition()

        # 审批检查
        if definition.needs_approval:
            approved = await self._approval.check(
                tool_name=name,
                arguments=arguments,
                channel=channel,
            )
            if not approved:
                return ToolResult(
                    output=f"用户拒绝了 {name} 的执行",
                    is_error=True,
                    error_code="APPROVAL_DENIED",
                    error_type="approval",
                )

        # 超时控制 + 执行
        timeout = definition.timeout
        ctx = ToolExecutionContext(timeout=timeout)

        try:
            result = await asyncio.wait_for(
                handler.execute(arguments, ctx),
                timeout=timeout,
            )
            return result
        except asyncio.TimeoutError:
            logger.warning(f"工具 {name} 执行超时（{timeout}s）")
            return ToolResult(
                output=f"错误：工具执行超时（{int(timeout)}秒）",
                is_error=True,
                error_code="TIMEOUT",
                error_type="timeout",
            )
        except Exception as e:
            logger.exception(f"工具 {name} 调度出错")
            return ToolResult(
                output=f"错误：工具调度异常 - {e}",
                is_error=True,
                error_code="EXECUTOR_ERROR",
                error_type="executor",
            )
