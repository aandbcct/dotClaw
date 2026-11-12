"""将旧 ToolExecutor 适配为无 Channel 副作用的 Runtime v4 ToolPort。"""

from __future__ import annotations

import uuid

from ...common.tool_replay import ToolExecutionEvidence

from ...tools.base import ToolExecutionContext
from ...tools.executor import ToolExecutor
from ..application.dto import ToolInvocation, ToolResult, ToolResultStatus, ToolRecoveryContract
from ..application.ports import ToolPort
from dotclaw.runtime.application.execution import RunExecutionView
from ..domain.facts import RunError, RunErrorCode, ToolCall


class ToolExecutorAdapter(ToolPort):
    """以 run_id 与 call_id 隔离审批状态，并只执行获准工具一次的适配器。"""

    def __init__(self, executor: ToolExecutor) -> None:
        """绑定既有工具执行器。"""
        self._executor: ToolExecutor = executor

    async def execute(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """返回审批需求或执行已获准的调用，不向 Channel 提问。

        审批恢复权威来自持久化 checkpoint：当 ``invocation.approved`` 为真时，
        该调用已由上层审批状态确认，适配器直接执行。
        """
        context = self._context(invocation, execution)
        legacy_result = await self._executor.execute_for_runtime(
            invocation.call.name,
            invocation.call.arguments,
            context,
            approved=invocation.approved,
        )
        if legacy_result.approval_required:
            return ToolResult(
                call_id=invocation.call.call_id,
                status=ToolResultStatus.APPROVAL_REQUIRED,
                approval_id=_approval_id(invocation.run_id, invocation.call.call_id),
                execution_evidence=ToolExecutionEvidence.NOT_STARTED,
            )
        if legacy_result.is_error:
            return ToolResult(
                call_id=invocation.call.call_id,
                status=ToolResultStatus.FAILED,
                output=legacy_result.output,
                error=RunError(RunErrorCode.TOOL_FAILURE, legacy_result.output),
                execution_evidence=legacy_result.execution_evidence,
                tool_error_code=legacy_result.error_code,
                tool_error_type=legacy_result.error_type,
            )
        return ToolResult(invocation.call.call_id, ToolResultStatus.COMPLETED, legacy_result.output,
            execution_evidence=legacy_result.execution_evidence,
            tool_error_code=legacy_result.error_code, tool_error_type=legacy_result.error_type)

    async def preflight(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolResult:
        """用真实参数和安全策略检查审批需求，不启动处理函数。"""
        result = await self._executor.preflight_for_runtime(
            invocation.call.name, invocation.call.arguments, self._context(invocation, execution)
        )
        if result.approval_required:
            return ToolResult(invocation.call.call_id, ToolResultStatus.APPROVAL_REQUIRED,
                approval_id=_approval_id(invocation.run_id, invocation.call.call_id),
                execution_evidence=ToolExecutionEvidence.NOT_STARTED)
        return ToolResult(invocation.call.call_id,
            ToolResultStatus.FAILED if result.is_error else ToolResultStatus.COMPLETED,
            output=result.output, execution_evidence=ToolExecutionEvidence.NOT_STARTED,
            tool_error_code=result.error_code, tool_error_type=result.error_type)

    def _context(self, invocation: ToolInvocation, execution: RunExecutionView) -> ToolExecutionContext:
        """把账本身份与稳定幂等键注入底层处理上下文。"""
        return ToolExecutionContext(
            agentrun_id=invocation.run_id, session_id=execution.session_id,
            agent_id=execution.policy.agent_id, tool_call_id=invocation.call.call_id,
            attempt=invocation.attempt, idempotency_key=invocation.idempotency_key,
        )

    async def clear_run(self, run_id: str) -> None:
        """兼容旧调用方；账本负责防止恢复时重复调用。"""
        return None

    async def describe_recovery(self, call: ToolCall, execution: RunExecutionView) -> ToolRecoveryContract:
        """从已注册工具读取冻结前恢复契约，未知工具按禁止重试处理。"""
        return self._executor.describe_recovery(call.name, ToolExecutionContext(
            agentrun_id=execution.run_id, session_id=execution.session_id,
            agent_id=execution.policy.agent_id,
        ))



def _approval_id(run_id: str, call_id: str) -> str:
    """为同一运行工具调用生成稳定且文件安全的审批标识。"""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"dotclaw:approval:{run_id}:{call_id}").hex
