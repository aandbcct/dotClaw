"""将旧 ToolExecutor 适配为无 Channel 副作用的 Runtime v4 ToolPort。"""

from __future__ import annotations

import uuid

from ...tools.base import ToolExecutionContext
from ...tools.executor import ToolExecutor
from ..application.dto import ToolInvocation, ToolResult, ToolResultStatus
from ..application.ports import ToolPort
from dotclaw.runtime.application.execution import RunExecutionView
from ..domain.facts import RunError, RunErrorCode


class ToolExecutorAdapter(ToolPort):
    """以 run_id 与 call_id 隔离审批状态，并只执行获准工具一次的适配器。"""

    def __init__(self, executor: ToolExecutor) -> None:
        """绑定既有工具执行器。"""
        self._executor: ToolExecutor = executor
        self._waiting_calls: set[tuple[str, str]] = set()
        self._executed_calls: set[tuple[str, str]] = set()

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """返回审批需求或执行已获准的调用，不向 Channel 提问。"""
        key = (invocation.run_id, invocation.call.call_id)
        if key in self._executed_calls:
            return ToolResult(
                call_id=invocation.call.call_id,
                status=ToolResultStatus.FAILED,
                error=RunError(RunErrorCode.TOOL_FAILURE, "同一工具调用不能重复执行"),
            )
        if self._executor.requires_approval(
            invocation.call.name,
            ToolExecutionContext(
                agentrun_id=invocation.run_id,
                agent_id=execution.policy.agent_id,
            ),
        ) and key not in self._waiting_calls:
            self._waiting_calls.add(key)
            return ToolResult(
                call_id=invocation.call.call_id,
                status=ToolResultStatus.APPROVAL_REQUIRED,
                approval_id=_approval_id(invocation.run_id, invocation.call.call_id),
            )
        self._waiting_calls.discard(key)
        self._executed_calls.add(key)
        legacy_result = await self._executor.execute_approved(
            invocation.call.name,
            invocation.call.arguments,
            ToolExecutionContext(
                agentrun_id=invocation.run_id,
                agent_id=execution.policy.agent_id,
            ),
        )
        if legacy_result.is_error:
            return ToolResult(
                call_id=invocation.call.call_id,
                status=ToolResultStatus.FAILED,
                output=legacy_result.output,
                error=RunError(RunErrorCode.TOOL_FAILURE, legacy_result.output),
            )
        return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED, legacy_result.output)

    async def cancel(self, run_id: str) -> None:
        """旧执行器不持有可取消句柄；清理尚未执行的审批调用。"""
        self._waiting_calls = {key for key in self._waiting_calls if key[0] != run_id}


def _approval_id(run_id: str, call_id: str) -> str:
    """为同一运行工具调用生成稳定且文件安全的审批标识。"""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"dotclaw:approval:{run_id}:{call_id}").hex
