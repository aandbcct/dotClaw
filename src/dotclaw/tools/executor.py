"""工具执行调度器（Phase 5 新增）— 审批 + 超时 + 错误处理"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import ToolExecutionContext, ToolResult
from .handler import ToolHandler
from .registry import ToolRegistry
from .approval import ApprovalManager
from dotclaw.journal import Journal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .parser import SkillParser

logger = logging.getLogger("dotclaw.tools.executor")


class ToolExecutor:
    """工具执行调度器 — 审批 + 超时 + 错误处理"""

    def __init__(
        self,
        registry: ToolRegistry,
        approval_manager: ApprovalManager | None = None,
        skill_parser: "SkillParser | None" = None,
    ):
        self._registry = registry
        self._approval = approval_manager or ApprovalManager()
        self._skill_parser = skill_parser

    def get_definitions(self) -> list:
        """返回所有工具定义（转发给 Registry）。"""
        return self._registry.get_definitions()

    def get_handler(self, name: str) -> ToolHandler | None:
        """按名称获取 Handler（转发给 Registry）。"""
        return self._registry.get(name)

    def _check_skill(self, tool_name: str, args: dict,
                     journal: Any, status: str) -> None:
        """工具执行后检查是否命中 skill，命中则发射对应 journal 事件。"""
        if not self._skill_parser:
            return
        result = self._skill_parser.parse(tool_name, args)
        if result is None:
            return
        skill_name, part, osname = result
        if part == "body":
            journal.skill_body_loaded(skill_name, status=status)
        elif part == "reference":
            journal.skill_reference_load(skill_name, osname, status=status)
        elif part == "script":
            journal.skill_script_exec(skill_name, osname, status=status)

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        channel: Any | None = None,
        journal: Journal | None = None,
    ) -> ToolResult:
        """执行工具：查找 Handler → 审批检查 → 超时控制 → 返回 ToolResult"""
        # ── Journal：工具执行开始 ──
        if journal:
            journal.tool_start(name, args=arguments)

        handler = self._registry.get(name)
        if not handler:
            result = ToolResult(
                output=f"错误：未找到工具 '{name}'",
                is_error=True,
                error_code="TOOL_NOT_FOUND",
                error_type="not_found",
            )
            if journal:
                journal.tool_end(name, result_len=len(result.output),
                                 status="error", error_type=result.error_type)
            return result

        definition = handler.definition()
        # todo 将安全模块独立出去，包括workspace限制、鉴权、工具审批、
        # 审批检查
        if definition.needs_approval:
            approved = await self._approval.check(
                tool_name=name,
                arguments=arguments,
                channel=channel,
            )
            if not approved:
                result = ToolResult(
                    output=f"用户拒绝了 {name} 的执行",
                    is_error=True,
                    error_code="APPROVAL_DENIED",
                    error_type="approval",
                )
                if journal:
                    journal.tool_end(name, result_len=len(result.output),
                                     status="error", error_type=result.error_type)
                return result

        # 超时控制 + 执行
        timeout = definition.timeout
        ctx = ToolExecutionContext(timeout=timeout)

        try:
            result = await asyncio.wait_for(
                handler.execute(arguments, ctx),
                timeout=timeout,
            )
            if journal:
                status = "error" if result.is_error else "success"
                journal.tool_end(name, result_len=len(result.output),
                                 status=status, error_type=result.error_type if result.is_error else "")
                self._check_skill(name, arguments, journal, status)
            return result
        except asyncio.TimeoutError:
            logger.warning(f"工具 {name} 执行超时（{timeout}s）")
            result = ToolResult(
                output=f"错误：工具执行超时（{int(timeout)}秒）",
                is_error=True,
                error_code="TIMEOUT",
                error_type="timeout",
            )
            if journal:
                journal.tool_end(name, result_len=len(result.output),
                                 status="error", error_type="timeout")
            return result
        except Exception as e:
            logger.exception(f"工具 {name} 调度出错")
            result = ToolResult(
                output=f"错误：工具调度异常 - {e}",
                is_error=True,
                error_code="EXECUTOR_ERROR",
                error_type="executor",
            )
            if journal:
                journal.tool_end(name, result_len=len(result.output),
                                 status="error", error_type="executor")
            return result
